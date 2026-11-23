#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert an ASCII legacy VTK POLYDATA surface to concon's ``.m`` format.

VTK 9 writes legacy POLYDATA cell arrays using separate OFFSETS and
CONNECTIVITY sections (file format 5.1).  The original SBCI converter only
understood the older interleaved ``3 v0 v1 v2`` representation.  It therefore
silently emitted grids with vertices but no faces when used with current VTK,
which in turn made concon write an empty kernel.

This module accepts both representations and deliberately rejects empty or
non-triangular meshes before they reach concon.
"""

import argparse
import logging
from os.path import isfile


DESCRIPTION = """
  Convert an ASCII legacy .vtk mesh file to a .m mesh file (concon format).
"""


def _build_args_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description=DESCRIPTION)
    parser.add_argument('--surface', required=True, metavar='SURFACE',
                        help='Path of the input ASCII legacy .vtk mesh file.')
    parser.add_argument('--output', required=True, metavar='OUTPUT',
                        help='Path of the output .m mesh file.')
    parser.add_argument('-f', action='store_true', dest='overwrite',
                        help='Overwrite the output if it already exists.')
    return parser


def _find_token(tokens, value, start=0):
    try:
        return tokens.index(value, start)
    except ValueError as exc:
        raise ValueError('VTK file is missing the {0} section.'.format(value)) from exc


def _parse_cells_interleaved(values, cell_count):
    cells = []
    cursor = 0
    for _ in range(cell_count):
        if cursor >= len(values):
            raise ValueError('POLYGONS cell data ended before all cells were read.')
        size = values[cursor]
        cursor += 1
        end = cursor + size
        if end > len(values):
            raise ValueError('A POLYGONS cell extends past the available connectivity data.')
        cells.append(values[cursor:end])
        cursor = end

    if cursor != len(values):
        raise ValueError('POLYGONS contains unused or malformed connectivity values.')
    return cells


def _parse_cells_offsets(offsets, connectivity):
    if len(offsets) < 2:
        raise ValueError('POLYGONS OFFSETS must contain at least two values.')
    if offsets[0] != 0 or offsets[-1] != len(connectivity):
        raise ValueError(
            'POLYGONS OFFSETS do not span the CONNECTIVITY array '
            '(first={0}, last={1}, connectivity={2}).'.format(
                offsets[0], offsets[-1], len(connectivity)))
    if any(end < start for start, end in zip(offsets, offsets[1:])):
        raise ValueError('POLYGONS OFFSETS must be monotonically increasing.')
    return [connectivity[start:end]
            for start, end in zip(offsets, offsets[1:])]


def read_legacy_polydata(filename):
    """Return ``(points, faces)`` from an ASCII legacy VTK POLYDATA file."""
    with open(filename, 'r', encoding='utf-8') as vtk_file:
        contents = vtk_file.read()

    header_lines = contents.splitlines()[:4]
    if any(line.strip().upper() == 'BINARY' for line in header_lines):
        raise ValueError('Binary legacy VTK is not supported; write the surface as ASCII.')

    tokens = contents.split()
    points_pos = _find_token(tokens, 'POINTS')
    try:
        point_count = int(tokens[points_pos + 1])
    except (IndexError, ValueError) as exc:
        raise ValueError('Invalid POINTS header.') from exc

    point_start = points_pos + 3  # POINTS <count> <data_type>
    point_end = point_start + 3 * point_count
    if point_end > len(tokens):
        raise ValueError('POINTS data ended before all coordinates were read.')
    try:
        coordinates = [float(value) for value in tokens[point_start:point_end]]
    except ValueError as exc:
        raise ValueError('POINTS contains a non-numeric coordinate.') from exc
    points = [coordinates[i:i + 3] for i in range(0, len(coordinates), 3)]

    polygons_pos = _find_token(tokens, 'POLYGONS', point_end)
    try:
        first_count = int(tokens[polygons_pos + 1])
        second_count = int(tokens[polygons_pos + 2])
    except (IndexError, ValueError) as exc:
        raise ValueError('Invalid POLYGONS header.') from exc

    cursor = polygons_pos + 3
    if cursor < len(tokens) and tokens[cursor] == 'OFFSETS':
        # VTK 5.1: the first header count is the number of offsets, which is
        # one greater than the number of cells.  The second is connectivity
        # length.  Each section also carries a data-type token.
        cursor += 2
        offsets_end = cursor + first_count
        try:
            offsets = [int(value) for value in tokens[cursor:offsets_end]]
        except ValueError as exc:
            raise ValueError('POLYGONS OFFSETS contains a non-integer value.') from exc
        if len(offsets) != first_count:
            raise ValueError('POLYGONS OFFSETS ended before all values were read.')

        cursor = offsets_end
        if cursor + 1 >= len(tokens) or tokens[cursor] != 'CONNECTIVITY':
            raise ValueError('POLYGONS OFFSETS is not followed by CONNECTIVITY.')
        cursor += 2
        connectivity_end = cursor + second_count
        try:
            connectivity = [int(value)
                            for value in tokens[cursor:connectivity_end]]
        except ValueError as exc:
            raise ValueError('POLYGONS CONNECTIVITY contains a non-integer value.') from exc
        if len(connectivity) != second_count:
            raise ValueError('POLYGONS CONNECTIVITY ended before all values were read.')
        faces = _parse_cells_offsets(offsets, connectivity)
    else:
        # VTK 2/3: second_count includes each per-cell size prefix.
        values_end = cursor + second_count
        try:
            values = [int(value) for value in tokens[cursor:values_end]]
        except ValueError as exc:
            raise ValueError('POLYGONS contains a non-integer value.') from exc
        if len(values) != second_count:
            raise ValueError('POLYGONS data ended before all values were read.')
        faces = _parse_cells_interleaved(values, first_count)

    if not points:
        raise ValueError('The VTK surface has no vertices.')
    if not faces:
        raise ValueError('The VTK surface has no polygon faces.')
    if any(len(face) != 3 for face in faces):
        raise ValueError('The concon grid must contain triangles only.')
    if any(vertex < 0 or vertex >= len(points) for face in faces for vertex in face):
        raise ValueError('A polygon references a vertex outside the POINTS array.')
    return points, faces


def write_concon_mesh(points, faces, filename):
    with open(filename, 'w', encoding='utf-8', newline='\n') as output_file:
        for index, point in enumerate(points, start=1):
            output_file.write('Vertex {0} {1}\n'.format(
                index, ' '.join('{0:.17g}'.format(value) for value in point)))
        for index, face in enumerate(faces, start=1):
            output_file.write('Face {0} {1}\n'.format(
                index, ' '.join(str(vertex + 1) for vertex in face)))


def main():
    parser = _build_args_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if not isfile(args.surface):
        parser.error('The file "{0}" must exist.'.format(args.surface))
    if isfile(args.output) and not args.overwrite:
        parser.error('The file "{0}" already exists. Use -f to overwrite it.'.format(
            args.output))

    try:
        points, faces = read_legacy_polydata(args.surface)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))

    logging.info('Writing concon grid with %d vertices and %d faces.',
                 len(points), len(faces))
    write_concon_mesh(points, faces, args.output)


if __name__ == '__main__':
    main()
