#!/usr/bin/env python3
"""Create a tiny non-scientific SBCI layout fixture for metadata-preflight tests."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import nibabel as nib
import numpy as np


ROIS = [4, 8, 10, 11, 12, 13, 17, 18, 26, 43, 47, 49, 50, 51, 52, 53, 54, 58, 16]


def copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def save(data: np.ndarray, affine: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(data, affine), str(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pipeline-root", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    pipeline = Path(args.pipeline_root).resolve()
    subject = output / "ADNI-fixture"
    reference = pipeline / "data" / "fsaverage"
    affine = np.diag([2.0, 2.0, 2.0, 1.0])
    shape = (5, 5, 5)

    save(np.full(shape, 0.5, dtype=np.float32), affine, subject / "dwi_pipeline/diffusion/dti/fa.nii.gz")
    save(np.full(shape + (6,), 0.1, dtype=np.float32), affine, subject / "dwi_pipeline/diffusion/fodf/fodf.nii.gz")
    for name, value in (("map_wm", 0.4), ("map_gm", 0.3), ("map_csf", 0.3)):
        save(np.full(shape, value, dtype=np.float32), affine, subject / f"dwi_pipeline/structure/{name}.nii.gz")
    aparc = np.zeros(shape, dtype=np.int16)
    aparc.ravel()[: len(ROIS)] = ROIS
    save(aparc, affine, subject / "dwi_pipeline/structure/aparc.a2009s+aseg.nii.gz")

    fs_root = subject / "dwi_pipeline/structure/t1_freesurfer"
    for hemisphere in ("lh", "rh"):
        for surface in ("sphere", "sphere.reg", "white", "pial", "inflated"):
            copy(reference / "surf" / f"{hemisphere}.white_avg", fs_root / "surf" / f"{hemisphere}.{surface}")
        copy(reference / "label" / f"{hemisphere}.aparc.a2009s.annot", fs_root / "label" / f"{hemisphere}.aparc.a2009s.annot")
    copy(reference / "mri.2mm" / "aseg.mgz", fs_root / "mri" / "wmparc.mgz")

    atlas = nib.load(str(reference / "mri.2mm" / "aseg.mgz"))
    run = subject / "fmri" / "bold" / "001"
    cortical = np.linspace(0.0, 1.0, 163842 * 3, dtype=np.float32).reshape((163842, 1, 1, 3))
    save(cortical, np.eye(4), run / "fc.surface.lh.nii.gz")
    save(cortical[::-1].copy(), np.eye(4), run / "fc.surface.rh.nii.gz")
    mni = np.ones(tuple(int(value) for value in atlas.shape) + (3,), dtype=np.float32)
    save(mni, atlas.affine, run / "fc.mni.nii.gz")

    output.mkdir(parents=True, exist_ok=True)
    (output / "sbci_local.json").write_text(
        json.dumps({"schema_version": "1.0", "mode": "inspect", "approved": False}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(subject)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
