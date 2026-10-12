#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""SET surface flow with a duplicate-edge-safe FEM mass matrix.

TriMeshPy's historical ``mass_matrix`` stores triangle indices as sparse
matrix values and then replaces those values with triangle areas.  SciPy sums
values when more than one triangle contributes the same directed edge.  The
summed value is no longer a triangle index and can either select the wrong area
or exceed the triangle array.  This implementation directly accumulates the
corresponding triangle areas.  It is algebraically identical on meshes without
duplicate directed edges and well-defined on the affected ADNI meshes.
"""

import argparse
import logging
from os.path import splitext

import numpy as np
from scipy.sparse import coo_matrix, diags
from trimeshpy.io import load_mesh_from_file
from trimeshpy.math.area import triangles_area
from trimeshpy.math.mesh_global import G_DTYPE
import trimeshpy.math.matrix as matrix_math
import trimeshpy.math.smooth as smooth_math
from scilpy.io.utils import (
    add_overwrite_arg,
    assert_inputs_exist,
    assert_outputs_exist,
)


DESCRIPTION = """
Script to compute the surface positive constrained mass-stiffness flow [1].
"""

EPILOG = """
References:
[1] St-Onge, E., Daducci, A., Girard, G. and Descoteaux, M. 2018.
    Surface-enhanced tractography (SET). NeuroImage.
"""


def duplicate_edge_safe_mass_matrix(triangles, vertices):
    """Return the original SET FEM mass matrix without index aggregation.

    Every oriented triangle edge contributes its triangle area.  Converting
    the COO matrix to CSC sums areas for repeated directed edges, which is the
    intended physical quantity.  Symmetrization and diagonal construction are
    unchanged from TriMeshPy's published implementation.
    """

    tri = np.asarray(triangles, dtype=np.int64)
    if tri.ndim != 2 or tri.shape[1] != 3:
        raise ValueError("Expected an (N, 3) triangle array")

    vertex_count = int(vertices.shape[0])
    if tri.size:
        if int(tri.min()) < 0 or int(tri.max()) >= vertex_count:
            raise ValueError("Triangle vertex index is outside the mesh")

    tri_area = np.asarray(
        triangles_area(triangles, vertices), dtype=G_DTYPE
    ).reshape(-1)
    if len(tri_area) != len(tri):
        raise ValueError("Triangle-area count does not match triangle count")
    if not np.isfinite(tri_area).all() or (tri_area < 0).any():
        raise ValueError("Triangle areas must be finite and nonnegative")

    rows = np.hstack((tri[:, 0], tri[:, 1], tri[:, 2]))
    cols = np.hstack((tri[:, 1], tri[:, 2], tri[:, 0]))
    values = np.tile(tri_area, 3)

    edge_area = coo_matrix(
        (values, (rows, cols)),
        shape=(vertex_count, vertex_count),
        dtype=G_DTYPE,
    ).tocsc()
    edge_area = (edge_area + edge_area.T) / 12.0
    weights = np.asarray(edge_area.sum(axis=1)).reshape(-1)
    return edge_area + diags(weights, 0)


# TriMeshPy imports mass_matrix into the smooth module namespace.  Patch both
# references before invoking TriMesh.positive_mass_stiffness_smooth().
matrix_math.mass_matrix = duplicate_edge_safe_mass_matrix
smooth_math.mass_matrix = duplicate_edge_safe_mass_matrix


def build_args_parser():
    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "surface", help="Input surface (Freesurfer or supported by VTK)"
    )
    parser.add_argument(
        "out_surface", help="Output surface (formats supported by VTK)"
    )
    parser.add_argument(
        "-m", "--vts_mask", help="Vertices mask, where to apply the flow (.npy)"
    )
    parser.add_argument(
        "-n",
        "--nb_steps",
        type=int,
        default=100,
        help="Number of steps for the flow [%(default)s]",
    )
    parser.add_argument(
        "-s",
        "--step_size",
        type=float,
        default=1.0,
        help="Flow step size [%(default)s]",
    )
    parser.add_argument("--out_flow", help="Resulting surface flow (.hdf5)")
    parser.add_argument(
        "--subsample_flow",
        type=int,
        default=1,
        help="subsample to flow output to reduce file size",
    )
    parser.add_argument(
        "--gaussian_threshold",
        type=float,
        default=0.2,
        help="DEBUG TOOL, advance only",
    )
    parser.add_argument(
        "--angle_threshold",
        type=float,
        default=2,
        help="DEBUG TOOL, advance only",
    )
    parser.add_argument(
        "-v", action="store_true", dest="verbose", help="Print filtering information"
    )
    add_overwrite_arg(parser)
    return parser


def main():
    parser = build_args_parser()
    args = parser.parse_args()

    assert_inputs_exist(
        parser, required=[args.surface], optional=[args.vts_mask]
    )
    assert_outputs_exist(
        parser, args, [args.out_surface], optional=[args.out_flow]
    )

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    if args.nb_steps < 4:
        parser.error("Number of steps should be 4 or higher")
    if args.step_size <= 0.0:
        parser.error("Step size should be strictly positive")
    if args.out_flow and splitext(args.out_flow)[1].lower() != ".hdf5":
        parser.error("Out flow must be in '.hdf5' format")

    mesh = load_mesh_from_file(args.surface)
    step_size_per_vertex = args.step_size
    if args.vts_mask:
        mask = np.load(args.vts_mask)
        step_size_per_vertex *= mask.astype(np.float64)

    flow_file = args.out_flow if args.out_flow else None
    vertices = mesh.positive_mass_stiffness_smooth(
        nb_iter=args.nb_steps,
        diffusion_step=step_size_per_vertex,
        flow_file=flow_file,
        gaussian_threshold=args.gaussian_threshold,
        angle_threshold=args.angle_threshold,
        subsample_file=args.subsample_flow,
    )
    mesh.set_vertices(vertices)
    mesh.save(args.out_surface)


if __name__ == "__main__":
    main()
