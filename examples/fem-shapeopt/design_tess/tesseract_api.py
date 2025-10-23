from typing import Any

import numpy as np
import pyvista as pv
import trimesh
from pydantic import BaseModel, Field
from pysdf import SDF
from tesseract_core.runtime import Array, Differentiable, Float32, ShapeDType

#
# Schemata
#


class InputSchema(BaseModel):
    bar_params: Differentiable[
        Array[
            (None, None, 3),
            Float32,
        ]
    ] = Field(
        description=(
            "Vertex positions of the bar geometry. "
            "The shape is (num_bars, num_vertices, 3), where num_bars is the number of bars "
            "and num_vertices is the number of vertices per bar. The last dimension represents "
            "the x, y, z coordinates of each vertex."
        )
    )

    bar_radius: float = Field(
        default=1.5,
        description=("Radius of the bars in the geometry. "),
    )

    Lx: float = Field(
        default=60.0,
        description=("Length of the SDF box in the x direction. "),
    )
    Ly: float = Field(
        default=30.0,
        description=("Length of the SDF box in the y direction. "),
    )
    Lz: float = Field(
        default=30.0,
        description=("Length of the SDF box in the z direction. "),
    )
    Nx: int = Field(
        default=60,
        description=("Number of elements in the x direction. "),
    )
    Ny: int = Field(
        default=30,
        description=("Number of elements in the y direction. "),
    )
    Nz: int = Field(
        default=30,
        description=("Number of elements in the z direction. "),
    )
    epsilon: float = Field(
        default=1e-5,
        description=(
            "Epsilon value for finite difference approximation of the Jacobian. "
        ),
    )


class TriangularMesh(BaseModel):
    points: Array[(None, 3), Float32] = Field(description="Array of vertex positions.")
    faces: Array[(None, 3), Float32] = Field(
        description="Array of triangular faces defined by indices into the points array."
    )
    n_points: int = Field(
        default=0, description="Number of valid points in the points array."
    )
    n_faces: int = Field(
        default=0, description="Number of valid faces in the faces array."
    )


class OutputSchema(BaseModel):
    mesh: TriangularMesh = Field(
        description="Triangular mesh representation of the geometry"
    )
    sdf: Differentiable[
        Array[
            (None, None, None),
            Float32,
        ]
    ] = Field(description="SDF field of the geometry")


#
# Helper functions
#


def build_geometry(
    params: np.ndarray,
    radius: float,
) -> list[pv.PolyData]:
    """Build a pyvista geometry from the parameters.

    The parameters are expected to be of shape (n_chains, n_edges_per_chain + 1, 3),
    """
    n_chains = params.shape[0]
    geometry = []

    for chain in range(n_chains):
        tube = pv.Spline(points=params[chain]).tube(radius=radius, capping=True)
        tube = tube.triangulate()
        geometry.append(tube)

    return geometry


def pyvista_to_trimesh(mesh: pv.PolyData) -> trimesh.Trimesh:
    """Convert a pyvista mesh to a trimesh style polygon mesh."""
    points = mesh.points
    points_per_face = mesh.faces[0]
    n_faces = mesh.faces.shape[0] // (points_per_face + 1)

    faces = mesh.faces.reshape(n_faces, (points_per_face + 1))[:, 1:]

    return trimesh.Trimesh(vertices=points, faces=faces)


def compute_sdf(
    geometry: trimesh.Trimesh,
    Lx: float,
    Ly: float,
    Lz: float,
    Nx: int,
    Ny: int,
    Nz: int,
) -> np.ndarray:
    """Create a pyvista plane that has the SDF values stored as a vertex attribute.

    The SDF field is computed based on the geometry defined by the parameters.
    """
    x, y, z = np.meshgrid(
        np.linspace(-Lx / 2, Lx / 2, Nx),
        np.linspace(-Ly / 2, Ly / 2, Ny),
        np.linspace(-Lz / 2, Lz / 2, Nz),
        indexing="ij",
    )

    points, faces = geometry.vertices, geometry.faces

    sdf_function = SDF(points, faces)

    grid_points = np.vstack((x.ravel(), y.ravel(), z.ravel())).T
    sdf_values = sdf_function(grid_points).astype(np.float32)

    sd_field = sdf_values.reshape((Nx, Ny, Nz))

    return sd_field


def apply_fn(
    params: np.ndarray,
    radius: float,
    Lx: float,
    Ly: float,
    Lz: float,
    Nx: int,
    Ny: int,
    Nz: int,
) -> tuple[np.ndarray, trimesh.Trimesh]:
    """Get the sdf values of a the geometry defined by the parameters as a 2D array."""
    geometries = build_geometry(
        params,
        radius=radius,
    )

    # convert each geometry in a trimesh style mesh and combine them
    base = pyvista_to_trimesh(geometries[0])

    for geom in geometries[1:]:
        other = pyvista_to_trimesh(geom)

        base = base.union(other)

    sd_field = compute_sdf(
        base,
        Lx=Lx,
        Ly=Ly,
        Lz=Lz,
        Nx=Nx,
        Ny=Ny,
        Nz=Nz,
    )

    return sd_field, base


def jac_sdf_wrt_params(
    params: np.ndarray,
    radius: float,
    Lx: float,
    Ly: float,
    Lz: float,
    Nx: int,
    Ny: int,
    Nz: int,
    epsilon: float,
) -> np.ndarray:
    """Compute the Jacobian of the SDF values with respect to the parameters.

    The Jacobian is computed by finite differences.
    The shape of the Jacobian is (n_chains, n_edges_per_chain + 1, 3, Nx, Ny).
    """
    n_chains = params.shape[0]
    n_edges_per_chain = params.shape[1] - 1

    jac = np.zeros(
        (
            n_chains,
            n_edges_per_chain + 1,
            3,  # number of dimensions (x, y, z)
            Nx,
            Ny,
        )
    )

    sd_field_base, _ = apply_fn(
        params,
        radius=radius,
        Lx=Lx,
        Ly=Ly,
        Lz=Lz,
        Nx=Nx,
        Ny=Ny,
        Nz=Nz,
    )

    for chain in range(n_chains):
        for vertex in range(0, n_edges_per_chain + 1):
            # we only care about the y coordinate
            i = 1
            params_eps = params.copy()
            params_eps[chain, vertex, i] += epsilon

            sdf_epsilon = apply_fn(
                params_eps,
                radius=radius,
                Lx=Lx,
                Ly=Ly,
                Lz=Lz,
                Nx=Nx,
                Ny=Ny,
                Nz=Nz,
            )
            jac[chain, vertex, i] = (sdf_epsilon - sd_field_base) / epsilon

    return jac


#
# Tesseract endpoints
#

N_POINTS = 1000
N_FACES = 2000


def apply(inputs: InputSchema) -> OutputSchema:
    sdf, mesh = apply_fn(
        inputs.bar_params,
        radius=inputs.bar_radius,
        Lx=inputs.Lx,
        Ly=inputs.Ly,
        Lz=inputs.Lz,
        Nx=inputs.Nx,
        Ny=inputs.Ny,
        Nz=inputs.Nz,
    )
    points = np.zeros((N_POINTS, 3), dtype=np.float32)
    faces = np.zeros((N_FACES, 3), dtype=np.float32)

    points[: mesh.vertices.shape[0], :] = mesh.vertices.astype(np.float32)
    faces[: mesh.faces.shape[0], :] = mesh.faces.astype(np.float32)

    return OutputSchema(
        sdf=sdf,
        mesh=TriangularMesh(
            points=points,
            faces=faces,
            n_points=mesh.vertices.shape[0],
            n_faces=mesh.faces.shape[0],
        ),
    )


def vector_jacobian_product(
    inputs: InputSchema,
    vjp_inputs: set[str],
    vjp_outputs: set[str],
    cotangent_vector: dict[str, Any],
):
    assert vjp_inputs == {"bar_params"}
    assert vjp_outputs == {"sdf"}

    jac = jac_sdf_wrt_params(
        inputs.bar_params,
        radius=inputs.bar_radius,
        Lx=inputs.Lx,
        Ly=inputs.Ly,
        Lz=inputs.Lz,
        Nx=inputs.Nx,
        Ny=inputs.Ny,
        Nz=inputs.Nz,
        epsilon=inputs.epsilon,
    )
    # Reduce the cotangent vector to the shape of the Jacobian, to compute VJP by hand
    vjp = np.einsum("ijklm,lm->ijk", jac, cotangent_vector["sdf"]).astype(np.float32)
    return {"bar_params": vjp}


def abstract_eval(abstract_inputs):
    """Calculate output shape of apply from the shape of its inputs."""
    return {
        "sdf": ShapeDType(
            shape=(abstract_inputs.Nx, abstract_inputs.Ny), dtype="float32"
        ),
        "triangular_mesh": {
            "points": ShapeDType(shape=(N_POINTS, 3), dtype="float32"),
            "faces": ShapeDType(shape=(N_FACES, 3), dtype="float32"),
            "n_points": ShapeDType(shape=(), dtype="int32"),
            "n_faces": ShapeDType(shape=(), dtype="int32"),
        },
    }
