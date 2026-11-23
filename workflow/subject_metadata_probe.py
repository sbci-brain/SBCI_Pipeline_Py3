#!/usr/bin/env python3
"""Validate SBCI-ready ADNI image grids, surfaces, labels, and fMRI dimensions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import nibabel as nib
import nibabel.freesurfer.io as fsio
import numpy as np


SURFACES = ("sphere", "sphere.reg", "white", "pial", "inflated")
FC_FILES = ("fc.surface.lh.nii.gz", "fc.surface.rh.nii.gz", "fc.mni.nii.gz")


def add_error(errors: list[dict[str, str]], code: str, message: str, path: Path) -> None:
    errors.append({"code": code, "message": message, "path": str(path)})


def load_image(path: Path, errors: list[dict[str, str]]) -> nib.spatialimages.SpatialImage | None:
    try:
        image = nib.load(str(path))
        if not np.isfinite(image.affine).all():
            raise ValueError("affine contains non-finite values")
        if any(int(size) <= 0 for size in image.shape):
            raise ValueError(f"invalid shape {image.shape}")
        return image
    except Exception as exc:
        add_error(errors, "unreadable_image", f"{type(exc).__name__}: {exc}", path)
        return None


def same_grid(left: nib.spatialimages.SpatialImage, right: nib.spatialimages.SpatialImage) -> bool:
    return tuple(left.shape[:3]) == tuple(right.shape[:3]) and np.allclose(left.affine, right.affine, atol=1e-3)


def finite_range(image: nib.spatialimages.SpatialImage) -> tuple[bool, float, float]:
    data = np.asanyarray(image.dataobj)
    finite = bool(np.isfinite(data).all())
    return finite, float(np.nanmin(data)), float(np.nanmax(data))


def sampled_timeseries_finite(image: nib.spatialimages.SpatialImage) -> bool:
    if len(image.shape) != 4 or image.shape[3] <= 0:
        return False
    indices = sorted({0, int(image.shape[3] // 2), int(image.shape[3] - 1)})
    return all(np.isfinite(np.asanyarray(image.dataobj[..., index])).all() for index in indices)


def reference_surface_vertex_counts(
    pipeline_root: Path,
    errors: list[dict[str, str]],
) -> dict[str, int]:
    """Return the fsaverage vertex counts consumed by SBCI functional mapping."""
    counts: dict[str, int] = {}
    reference = pipeline_root / "data" / "fsaverage" / "label"
    for hemisphere in ("lh", "rh"):
        annotation = reference / f"{hemisphere}.aparc.a2009s.annot"
        try:
            labels, _, _ = fsio.read_annot(str(annotation))
            if labels.size <= 0:
                raise ValueError("annotation is empty")
            counts[hemisphere] = int(labels.size)
        except Exception as exc:
            add_error(errors, "invalid_reference_annotation", f"{type(exc).__name__}: {exc}", annotation)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--pipeline-root", required=True)
    parser.add_argument("--rois", required=True, help="Comma-separated integer ROI labels")
    args = parser.parse_args(argv)

    subject = Path(args.subject).resolve()
    pipeline_root = Path(args.pipeline_root).resolve()
    rois = [int(value) for value in args.rois.split(",") if value]
    dwi = subject / "dwi_pipeline"
    structure = dwi / "structure"
    diffusion = dwi / "diffusion"
    errors: list[dict[str, str]] = []
    summary: dict[str, Any] = {"subject": str(subject), "volumes": {}, "surfaces": {}, "fmriRuns": {}}

    volume_paths = {
        "fa": diffusion / "dti" / "fa.nii.gz",
        "fodf": diffusion / "fodf" / "fodf.nii.gz",
        "aparc": structure / "aparc.a2009s+aseg.nii.gz",
        "wm": structure / "map_wm.nii.gz",
        "gm": structure / "map_gm.nii.gz",
        "csf": structure / "map_csf.nii.gz",
    }
    images = {name: load_image(path, errors) for name, path in volume_paths.items()}
    for name, image in images.items():
        if image is not None:
            summary["volumes"][name] = {"shape": list(image.shape), "affine": np.asarray(image.affine).round(6).tolist()}

    fa = images.get("fa")
    fodf = images.get("fodf")
    if fa is not None and len(fa.shape) != 3:
        add_error(errors, "invalid_fa_dimensions", f"FA must be 3D; found {fa.shape}", volume_paths["fa"])
    if fodf is not None and (len(fodf.shape) != 4 or fodf.shape[3] < 6):
        add_error(errors, "invalid_fodf_dimensions", f"fODF must be 4D with SH coefficients; found {fodf.shape}", volume_paths["fodf"])
    if fa is not None:
        for name in ("fodf", "wm", "gm", "csf"):
            image = images.get(name)
            if image is not None and not same_grid(fa, image):
                add_error(
                    errors,
                    "diffusion_grid_mismatch",
                    f"{name} grid/affine does not match the FA tracking grid",
                    volume_paths[name],
                )

    for name in ("fa", "fodf", "wm", "gm", "csf"):
        image = images.get(name)
        if image is None:
            continue
        try:
            finite, minimum, maximum = finite_range(image)
            summary["volumes"][name].update({"finite": finite, "minimum": minimum, "maximum": maximum})
            if not finite:
                add_error(errors, "nonfinite_volume", f"{name} contains NaN or infinite values", volume_paths[name])
            if name in {"fa", "wm", "gm", "csf"} and (minimum < -1e-4 or maximum > 1.0001):
                add_error(errors, "invalid_probability_range", f"{name} values must be within [0, 1]; found [{minimum}, {maximum}]", volume_paths[name])
            if max(abs(minimum), abs(maximum)) == 0:
                add_error(errors, "empty_signal_volume", f"{name} contains only zeros", volume_paths[name])
        except Exception as exc:
            add_error(errors, "unreadable_voxels", f"Could not inspect {name} voxels: {exc}", volume_paths[name])

    aparc = images.get("aparc")
    if aparc is not None:
        try:
            labels = np.unique(np.asanyarray(aparc.dataobj).astype(np.int32, copy=False))
            missing_rois = sorted(set(rois).difference(int(value) for value in labels))
            summary["volumes"]["aparc"].update({"labelCount": int(labels.size), "missingRequestedRois": missing_rois})
            if missing_rois:
                add_error(errors, "missing_aparc_rois", f"Requested ROI labels are absent: {missing_rois}", volume_paths["aparc"])
        except Exception as exc:
            add_error(errors, "unreadable_aparc_labels", str(exc), volume_paths["aparc"])

    fs_root = structure / "t1_freesurfer"
    wmparc_path = fs_root / "mri" / "wmparc.mgz"
    wmparc = load_image(wmparc_path, errors)
    if wmparc is not None:
        summary["volumes"]["wmparc"] = {"shape": [int(value) for value in wmparc.shape]}
    hemisphere_vertices: dict[str, int] = {}
    for hemisphere in ("lh", "rh"):
        counts: dict[str, dict[str, int]] = {}
        for name in SURFACES:
            path = fs_root / "surf" / f"{hemisphere}.{name}"
            try:
                vertices, faces = fsio.read_geometry(str(path))
                if vertices.size == 0 or faces.size == 0 or not np.isfinite(vertices).all():
                    raise ValueError("surface is empty or has non-finite vertices")
                counts[name] = {"vertices": int(vertices.shape[0]), "faces": int(faces.shape[0])}
            except Exception as exc:
                add_error(errors, "invalid_freesurfer_surface", f"{type(exc).__name__}: {exc}", path)
        vertex_counts = {value["vertices"] for value in counts.values()}
        if len(vertex_counts) != 1:
            add_error(errors, "surface_vertex_mismatch", f"{hemisphere} surfaces have inconsistent vertex counts", fs_root / "surf")
        elif vertex_counts:
            hemisphere_vertices[hemisphere] = next(iter(vertex_counts))
        annotation = fs_root / "label" / f"{hemisphere}.aparc.a2009s.annot"
        try:
            annot_labels, _, _ = fsio.read_annot(str(annotation))
            if hemisphere in hemisphere_vertices and annot_labels.size != hemisphere_vertices[hemisphere]:
                raise ValueError(f"annotation has {annot_labels.size} labels for {hemisphere_vertices[hemisphere]} vertices")
            counts["annotation"] = {"vertices": int(annot_labels.size), "faces": 0}
        except Exception as exc:
            add_error(errors, "invalid_freesurfer_annotation", f"{type(exc).__name__}: {exc}", annotation)
        summary["surfaces"][hemisphere] = counts

    reference_atlas_path = pipeline_root / "data" / "fsaverage" / "mri.2mm" / "aseg.mgz"
    reference_atlas = load_image(reference_atlas_path, errors)
    if reference_atlas is not None:
        try:
            reference_labels = np.unique(np.asanyarray(reference_atlas.dataobj).astype(np.int32, copy=False))
            missing_reference_rois = sorted(set(rois).difference(int(value) for value in reference_labels))
            summary["referenceAtlas"] = {
                "shape": [int(value) for value in reference_atlas.shape],
                "missingRequestedRois": missing_reference_rois,
            }
            if missing_reference_rois:
                add_error(errors, "missing_reference_rois", f"Bundled functional atlas lacks requested ROI labels: {missing_reference_rois}", reference_atlas_path)
        except Exception as exc:
            add_error(errors, "unreadable_reference_labels", str(exc), reference_atlas_path)
    reference_vertices = reference_surface_vertex_counts(pipeline_root, errors)
    summary["referenceSurfaces"] = {
        hemisphere: {"vertices": count} for hemisphere, count in reference_vertices.items()
    }
    bold = subject / "fmri" / "bold"
    runs = sorted(path for path in bold.iterdir() if path.is_dir() and path.name.isdigit()) if bold.is_dir() else []
    for run in runs:
        run_images = {name: load_image(run / name, errors) for name in FC_FILES}
        lh = run_images.get("fc.surface.lh.nii.gz")
        rh = run_images.get("fc.surface.rh.nii.gz")
        mni = run_images.get("fc.mni.nii.gz")
        detail: dict[str, Any] = {name: list(image.shape) for name, image in run_images.items() if image is not None}
        timepoints: set[int] = set()
        for hemisphere, image in (("lh", lh), ("rh", rh)):
            if image is None:
                continue
            if len(image.shape) != 4 or image.shape[1:3] != (1, 1):
                add_error(errors, "invalid_surface_timeseries_shape", f"{hemisphere} FSFast residual must have shape (vertices,1,1,time); found {image.shape}", run / f"fc.surface.{hemisphere}.nii.gz")
            else:
                timepoints.add(int(image.shape[3]))
                # FSFast residuals are projected to fsaverage. SBCI step 6 consumes
                # them with the bundled fsaverage atlas/mapping, not the subject's
                # native FreeSurfer mesh (whose vertex count normally differs).
                expected_vertices = reference_vertices.get(hemisphere)
                if expected_vertices is not None and image.shape[0] != expected_vertices:
                    add_error(errors, "surface_timeseries_vertex_mismatch", f"{hemisphere} residual has {image.shape[0]} vertices; fsaverage reference has {expected_vertices}", run / f"fc.surface.{hemisphere}.nii.gz")
                if not sampled_timeseries_finite(image):
                    add_error(errors, "nonfinite_fmri", f"{hemisphere} residual contains non-finite sampled values", run / f"fc.surface.{hemisphere}.nii.gz")
        if mni is not None:
            if len(mni.shape) != 4:
                add_error(errors, "invalid_mni_timeseries_shape", f"MNI residual must be 4D; found {mni.shape}", run / "fc.mni.nii.gz")
            else:
                timepoints.add(int(mni.shape[3]))
                if reference_atlas is not None and tuple(mni.shape[:3]) != tuple(reference_atlas.shape[:3]):
                    add_error(errors, "mni_atlas_shape_mismatch", f"MNI residual spatial shape {mni.shape[:3]} does not match bundled atlas {reference_atlas.shape[:3]}", run / "fc.mni.nii.gz")
                elif reference_atlas is not None and not np.allclose(mni.affine, reference_atlas.affine, atol=1e-3):
                    add_error(errors, "mni_atlas_affine_mismatch", "MNI residual affine does not match the bundled atlas used for ROI indexing", run / "fc.mni.nii.gz")
                if not sampled_timeseries_finite(mni):
                    add_error(errors, "nonfinite_fmri", "MNI residual contains non-finite sampled values", run / "fc.mni.nii.gz")
        if len(timepoints) != 1 or (timepoints and next(iter(timepoints)) < 3):
            add_error(errors, "fmri_timepoint_mismatch", f"Cortical and MNI residuals must share at least 3 timepoints; found {sorted(timepoints)}", run)
        detail["timepoints"] = sorted(timepoints)
        summary["fmriRuns"][run.name] = detail

    result = {
        "ready": not errors,
        "errors": errors,
        "summary": summary,
        "message": "ADNI/SBCI metadata preflight passed." if not errors else f"ADNI/SBCI metadata preflight found {len(errors)} blocker(s).",
    }
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
