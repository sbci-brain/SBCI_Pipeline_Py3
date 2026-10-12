#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""SET surface flow with guarded face-orientation and mass-matrix fixes.

This is intentionally compatible with both Python 2.7 and Python 3.  It keeps
the published TriMeshPy flow algorithm, but repairs inconsistent triangle
winding on otherwise closed, manifold meshes before flow and replaces the
historical sparse-index mass-matrix construction with direct area summation.
"""

from __future__ import print_function

import argparse
from collections import deque
import logging
import os
from os.path import splitext
import tempfile

import numpy as np
from scipy.sparse import coo_matrix, diags
import vtk
from vtk.util.numpy_support import numpy_to_vtkIdTypeArray, vtk_to_numpy

from trimeshpy.io import load_mesh_from_file
from trimeshpy.math.area import triangles_area
from trimeshpy.math.mesh_global import G_DTYPE
import trimeshpy.math.matrix as matrix_math
import trimeshpy.math.smooth as smooth_math


DESCRIPTION = """
Script to compute the surface positive constrained mass-stiffness flow [1].
"""


def add_overwrite_arg(parser):
    parser.add_argument(
        "-f", "--force", action="store_true", help="Overwrite output files"
    )


def validate_paths(parser, args):
    inputs = [args.surface]
    if args.vts_mask:
        inputs.append(args.vts_mask)
    for path in inputs:
        if not os.path.isfile(path):
            parser.error("Input file does not exist: %s" % path)

    outputs = [args.out_surface]
    if args.out_flow:
        outputs.append(args.out_flow)
    if not args.force:
        for path in outputs:
            if os.path.exists(path):
                parser.error("Output exists; use -f to overwrite: %s" % path)

EPILOG = """
References:
[1] St-Onge, E., Daducci, A., Girard, G. and Descoteaux, M. 2018.
    Surface-enhanced tractography (SET). NeuroImage.
"""


def duplicate_edge_safe_mass_matrix(triangles, vertices):
    """Build the published FEM mass matrix without sparse index summation."""

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


# TriMeshPy imports mass_matrix into smooth.py at module import time.
matrix_math.mass_matrix = duplicate_edge_safe_mass_matrix
smooth_math.mass_matrix = duplicate_edge_safe_mass_matrix


def read_polydata(path):
    reader = vtk.vtkPolyDataReader()
    reader.SetFileName(path)
    reader.Update()
    polydata = reader.GetOutput()
    if polydata is None or polydata.GetNumberOfPoints() == 0:
        raise ValueError("Unable to load VTK polydata: %s" % path)
    return polydata


def polydata_triangles(polydata):
    polygons = polydata.GetPolys()
    polygon_count = int(polydata.GetNumberOfPolys())

    if hasattr(polygons, "GetOffsetsArray"):
        offsets_array = polygons.GetOffsetsArray()
        connectivity_array = polygons.GetConnectivityArray()
        if offsets_array is not None and connectivity_array is not None:
            offsets = np.asarray(vtk_to_numpy(offsets_array), dtype=np.int64)
            connectivity = np.asarray(
                vtk_to_numpy(connectivity_array), dtype=np.int64
            )
            sizes = np.diff(offsets)
            if len(sizes) != polygon_count or not np.all(sizes == 3):
                raise ValueError("Surface contains non-triangle polygons")
            if len(connectivity) != polygon_count * 3:
                raise ValueError("Triangle connectivity length is inconsistent")
            return connectivity.reshape((polygon_count, 3))

    legacy = np.asarray(vtk_to_numpy(polygons.GetData()), dtype=np.int64)
    if len(legacy) != polygon_count * 4:
        raise ValueError("Unsupported legacy VTK polygon layout")
    legacy = legacy.reshape((polygon_count, 4))
    if not np.all(legacy[:, 0] == 3):
        raise ValueError("Surface contains non-triangle polygons")
    return legacy[:, 1:4]


def orientation_statistics(triangles, point_count):
    triangles = np.asarray(triangles, dtype=np.int64)
    if triangles.ndim != 2 or triangles.shape[1] != 3:
        raise ValueError("Expected triangular faces")
    if triangles.size:
        if int(triangles.min()) < 0 or int(triangles.max()) >= point_count:
            raise ValueError("Triangle vertex index is outside the mesh")
    if np.any(
        (triangles[:, 0] == triangles[:, 1])
        | (triangles[:, 1] == triangles[:, 2])
        | (triangles[:, 2] == triangles[:, 0])
    ):
        raise ValueError("Surface contains degenerate triangle indices")

    first = np.hstack(
        (triangles[:, 0], triangles[:, 1], triangles[:, 2])
    )
    second = np.hstack(
        (triangles[:, 1], triangles[:, 2], triangles[:, 0])
    )
    low = np.minimum(first, second)
    high = np.maximum(first, second)
    direction = first == low

    order = np.lexsort((high, low))
    low = low[order]
    high = high[order]
    direction = direction[order]

    changed = np.empty(len(low) + 1, dtype=np.bool_)
    changed[0] = True
    changed[-1] = True
    if len(low) > 1:
        changed[1:-1] = (low[1:] != low[:-1]) | (high[1:] != high[:-1])

    boundaries = np.flatnonzero(changed)
    counts = np.diff(boundaries)
    starts = boundaries[:-1]
    paired_starts = starts[counts == 2]

    boundary_edges = int(np.count_nonzero(counts == 1))
    nonmanifold_edges = int(np.count_nonzero(counts > 2))
    same_direction_edges = int(
        np.count_nonzero(
            direction[paired_starts] == direction[paired_starts + 1]
        )
    )
    return boundary_edges, nonmanifold_edges, same_direction_edges


def consistently_orient_triangles(triangles):
    """Orient every connected component using shared-edge parity constraints."""

    triangles = np.asarray(triangles, dtype=np.int64)
    face_count = len(triangles)

    first = np.hstack(
        (triangles[:, 0], triangles[:, 1], triangles[:, 2])
    )
    second = np.hstack(
        (triangles[:, 1], triangles[:, 2], triangles[:, 0])
    )
    face_ids = np.tile(np.arange(face_count, dtype=np.int64), 3)
    low = np.minimum(first, second)
    high = np.maximum(first, second)
    direction = first == low

    order = np.lexsort((high, low))
    sorted_low = low[order]
    sorted_high = high[order]

    changed = np.empty(len(order) + 1, dtype=np.bool_)
    changed[0] = True
    changed[-1] = True
    if len(order) > 1:
        changed[1:-1] = (
            (sorted_low[1:] != sorted_low[:-1])
            | (sorted_high[1:] != sorted_high[:-1])
        )
    boundaries = np.flatnonzero(changed)
    counts = np.diff(boundaries)
    if not np.all(counts == 2):
        raise ValueError(
            "Orientation repair requires exactly two faces per edge"
        )

    starts = boundaries[:-1]
    first_positions = order[starts]
    second_positions = order[starts + 1]
    left_faces = face_ids[first_positions]
    right_faces = face_ids[second_positions]
    parity = direction[first_positions] == direction[second_positions]

    sources = np.hstack((left_faces, right_faces))
    destinations = np.hstack((right_faces, left_faces))
    constraints = np.hstack((parity, parity))
    adjacency_order = np.argsort(sources, kind="mergesort")
    sources = sources[adjacency_order]
    destinations = destinations[adjacency_order]
    constraints = constraints[adjacency_order]

    degree = np.bincount(sources, minlength=face_count)
    if len(degree) != face_count or not np.all(degree == 3):
        raise ValueError("Every triangular face must have three neighbors")

    neighbors = destinations.reshape((face_count, 3))
    neighbor_constraints = constraints.reshape((face_count, 3))
    flips = np.full(face_count, -1, dtype=np.int8)
    pending = deque()

    for root in range(face_count):
        if flips[root] != -1:
            continue
        flips[root] = 0
        pending.append(root)

        while pending:
            face = pending.popleft()
            for slot in range(3):
                neighbor = int(neighbors[face, slot])
                expected = int(flips[face]) ^ int(
                    neighbor_constraints[face, slot]
                )
                if flips[neighbor] == -1:
                    flips[neighbor] = expected
                    pending.append(neighbor)
                elif int(flips[neighbor]) != expected:
                    raise ValueError(
                        "Surface orientation constraints are inconsistent"
                    )

    repaired = triangles.copy()
    selected = flips == 1
    repaired[selected, 1] = triangles[selected, 2]
    repaired[selected, 2] = triangles[selected, 1]
    return repaired, int(np.count_nonzero(selected))


def polydata_with_triangles(polydata, triangles):
    triangles = np.asarray(triangles, dtype=np.int64)
    legacy = np.empty((len(triangles), 4), dtype=np.int64)
    legacy[:, 0] = 3
    legacy[:, 1:4] = triangles

    vtk_ids = numpy_to_vtkIdTypeArray(
        np.ascontiguousarray(legacy).reshape(-1), deep=True
    )
    polygons = vtk.vtkCellArray()
    polygons.SetCells(len(triangles), vtk_ids)

    repaired = vtk.vtkPolyData()
    repaired.DeepCopy(polydata)
    repaired.SetPolys(polygons)
    repaired.Modified()
    return repaired


def vtk_set_input(algorithm, polydata):
    if hasattr(algorithm, "SetInputData"):
        algorithm.SetInputData(polydata)
    else:  # VTK 5/6 compatibility
        algorithm.SetInput(polydata)


def write_polydata(polydata, path):
    writer = vtk.vtkPolyDataWriter()
    writer.SetFileName(path)
    vtk_set_input(writer, polydata)
    writer.SetFileTypeToBinary()
    if writer.Write() != 1:
        raise IOError("Unable to write oriented VTK polydata: %s" % path)


def consistently_oriented_surface(path):
    """Return a loadable path and an optional temporary path to remove."""

    polydata = read_polydata(path)
    point_count = int(polydata.GetNumberOfPoints())
    polygon_count = int(polydata.GetNumberOfPolys())
    triangles = polydata_triangles(polydata)
    before = orientation_statistics(triangles, point_count)

    logging.info(
        "Surface orientation before flow: boundary=%d nonmanifold=%d "
        "same_direction=%d",
        before[0],
        before[1],
        before[2],
    )

    if before[0] or before[1]:
        raise ValueError(
            "Face orientation repair requires a closed 2-manifold surface"
        )
    if before[2] == 0:
        return path, None

    repaired_triangles, flipped_count = consistently_orient_triangles(
        triangles
    )
    logging.info("Surface orientation repair flipped %d faces", flipped_count)
    repaired = polydata_with_triangles(polydata, repaired_triangles)

    if int(repaired.GetNumberOfPoints()) != point_count:
        raise ValueError("Orientation repair changed the point count")
    if int(repaired.GetNumberOfPolys()) != polygon_count:
        raise ValueError("Orientation repair changed the polygon count")

    original_points = vtk_to_numpy(polydata.GetPoints().GetData())
    repaired_points = vtk_to_numpy(repaired.GetPoints().GetData())
    if not np.array_equal(original_points, repaired_points):
        raise ValueError("Orientation repair changed point values or ordering")

    repaired_triangles = polydata_triangles(repaired)
    if not np.array_equal(
        np.sort(triangles, axis=1),
        np.sort(repaired_triangles, axis=1),
    ):
        raise ValueError(
            "Orientation repair changed triangle identities or cell ordering"
        )
    after = orientation_statistics(repaired_triangles, point_count)
    logging.info(
        "Surface orientation after repair: boundary=%d nonmanifold=%d "
        "same_direction=%d",
        after[0],
        after[1],
        after[2],
    )
    if after != (0, 0, 0):
        raise ValueError("VTK did not produce a consistently oriented manifold")

    handle, temporary_path = tempfile.mkstemp(
        prefix=".sbci_oriented_",
        suffix=".vtk",
        dir=os.path.dirname(os.path.abspath(path)),
    )
    os.close(handle)
    write_polydata(repaired, temporary_path)
    return temporary_path, temporary_path


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
        "--nb_step",
        dest="nb_steps",
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
        help="subsample flow output to reduce file size",
    )
    parser.add_argument(
        "--gaussian_threshold", type=float, default=0.2, help="advanced option"
    )
    parser.add_argument(
        "--angle_threshold", type=float, default=2, help="advanced option"
    )
    parser.add_argument("-v", action="store_true", dest="verbose")
    add_overwrite_arg(parser)
    return parser


def main():
    parser = build_args_parser()
    args = parser.parse_args()
    validate_paths(parser, args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(message)s",
    )
    if args.nb_steps < 4:
        parser.error("Number of steps should be 4 or higher")
    if args.step_size <= 0.0:
        parser.error("Step size should be strictly positive")
    if args.out_flow and splitext(args.out_flow)[1].lower() != ".hdf5":
        parser.error("Out flow must be in '.hdf5' format")
    if splitext(args.surface)[1].lower() != ".vtk":
        parser.error("Guarded orientation repair currently requires VTK input")

    load_path = args.surface
    temporary_path = None
    try:
        load_path, temporary_path = consistently_oriented_surface(args.surface)
        mesh = load_mesh_from_file(load_path)

        step_size_per_vertex = args.step_size
        if args.vts_mask:
            mask = np.load(args.vts_mask)
            if len(mask) != len(mesh.get_vertices()):
                raise ValueError("Vertex mask length does not match the surface")
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
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


if __name__ == "__main__":
    main()
