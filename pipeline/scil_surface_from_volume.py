#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Correct ROI-to-surface conversion for the Python 3 ADNI SET branch.

The implementation matches the intended scilpy 1.x semantics used by the
Python 2 branch: ``--index`` selects the ROI and ``--max_label`` retains its
largest connected component.  Opening is reduced only when the requested
iterations would otherwise erase a present small ROI.
"""

import argparse
import logging
from pathlib import Path

import nibabel as nib
import numpy as np
import vtk
import vtk.util.numpy_support as ns
from scipy import ndimage


def build_parser():
    parser = argparse.ArgumentParser(
        description="Extract an ROI surface from a labelled NIfTI volume."
    )
    parser.add_argument("volume")
    parser.add_argument("output")
    parser.add_argument("--index", type=int, nargs="+")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--closing", type=int, default=0)
    parser.add_argument("--opening", type=int, default=0)
    parser.add_argument("--erosion", type=int, default=0)
    parser.add_argument("--dilation", type=int, default=0)
    parser.add_argument("--smooth", type=int, default=0)
    parser.add_argument("--vox2vtk", action="store_true")
    parser.add_argument("--fill", action="store_true")
    parser.add_argument("--max_label", action="store_true")
    parser.add_argument("-f", "--force", action="store_true")
    return parser


def largest_component(mask, parser):
    objects, count = ndimage.label(mask)
    if count < 1:
        parser.error("No connected component remains after morphology.")
    sizes = np.bincount(objects.ravel())
    sizes[0] = 0
    return objects == int(np.argmax(sizes))


def adaptive_morphology(mask, args, parser):
    base = mask.astype(bool)
    if args.erosion > 0:
        base = ndimage.binary_erosion(base, iterations=args.erosion)
    if args.dilation > 0:
        base = ndimage.binary_dilation(base, iterations=args.dilation)
    if not np.any(base):
        parser.error("ROI was erased by erosion/dilation.")

    requested = args.opening
    for opening in range(requested, -1, -1):
        candidate = base
        if opening > 0:
            candidate = ndimage.binary_opening(
                candidate, iterations=opening
            )
        if args.closing > 0:
            candidate = ndimage.binary_closing(
                candidate, iterations=args.closing
            )
        if np.any(candidate):
            if opening != requested:
                logging.warning(
                    "Opening reduced from %d to %d because the requested "
                    "morphology erased this ROI.", requested, opening
                )
            return candidate

    parser.error("ROI is empty after all morphology fallbacks.")


def marching_cubes(mask, affine, vox2vtk, parser):
    image_data = vtk.vtkImageData()
    image_data.SetDimensions(*mask.shape)
    image_data.SetSpacing(1.0, 1.0, 1.0)
    image_data.SetOrigin(0.0, 0.0, 0.0)

    array = ns.numpy_to_vtk(
        mask.flatten(order="F").astype(np.float32),
        deep=True, array_type=vtk.VTK_FLOAT
    )
    array.SetName("mask")
    image_data.GetPointData().SetScalars(array)

    extractor = vtk.vtkMarchingCubes()
    extractor.SetInputData(image_data)
    extractor.SetValue(0, 0.5)
    extractor.Update()
    surface = extractor.GetOutput()

    if surface.GetNumberOfPoints() == 0 or surface.GetNumberOfCells() == 0:
        parser.error("Marching cubes produced an empty surface.")

    if vox2vtk:
        # VTK/SET space is LPS: legacy trimeshpy vox_to_vtk applied diag(-1,-1,1,1) after
        # the (RAS) NIfTI affine, matching the cortical surfaces step 2 flips to LPS.
        affine = np.diag([-1.0, -1.0, 1.0, 1.0]) @ affine
        points = ns.vtk_to_numpy(surface.GetPoints().GetData())
        homogeneous = np.hstack(
            [points, np.ones((points.shape[0], 1), dtype=points.dtype)]
        )
        transformed = (affine @ homogeneous.T).T[:, :3]
        vtk_points = vtk.vtkPoints()
        vtk_points.SetData(ns.numpy_to_vtk(
            transformed.astype(np.float32), deep=True
        ))
        surface.SetPoints(vtk_points)

    return surface


def main():
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s:%(message)s")

    source = Path(args.volume)
    output = Path(args.output)
    if not source.is_file():
        parser.error(f"Input volume does not exist: {source}")
    if output.exists() and not args.force:
        parser.error(f"Output exists; use -f: {output}")

    image = nib.load(str(source))
    volume = np.asarray(image.dataobj)

    if args.index:
        mask = np.zeros(volume.shape, dtype=bool)
        for index in args.index:
            mask = np.logical_or(mask, volume == index)
        selected_description = ",".join(map(str, args.index))
    elif args.all:
        mask = volume > 0
        selected_description = "all-positive"
    else:
        parser.error("Specify --index or --all.")

    raw_voxels = int(np.count_nonzero(mask))
    if raw_voxels == 0:
        parser.error("Requested ROI is absent from the input volume.")

    mask = adaptive_morphology(mask, args, parser)
    if args.max_label:
        mask = largest_component(mask, parser)
    if args.fill:
        mask = ndimage.binary_fill_holes(mask)

    final_voxels = int(np.count_nonzero(mask))
    if final_voxels == 0:
        parser.error("ROI is empty immediately before marching cubes.")

    surface = marching_cubes(mask, image.affine, args.vox2vtk, parser)
    if args.smooth > 0:
        smoother = vtk.vtkSmoothPolyDataFilter()
        smoother.SetInputData(surface)
        smoother.SetNumberOfIterations(args.smooth)
        smoother.Update()
        surface = smoother.GetOutput()

    writer = vtk.vtkPolyDataWriter()
    writer.SetInputData(surface)
    writer.SetFileName(str(output))
    writer.Write()

    if not output.is_file() or output.stat().st_size == 0:
        parser.error("Surface writer returned without a nonempty output.")

    logging.info(
        "ROI %s: raw_voxels=%d final_voxels=%d points=%d cells=%d",
        selected_description, raw_voxels, final_voxels,
        surface.GetNumberOfPoints(), surface.GetNumberOfCells()
    )


if __name__ == "__main__":
    main()
