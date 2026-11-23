#!/usr/bin/env python3
"""Patient-free integrity test for CyberNeuro's local SBCI runtime bundle."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from workflow.c3_asset import materialize_c3_main
except ModuleNotFoundError:  # Direct execution sets workflow/ as sys.path[0].
    from c3_asset import materialize_c3_main


EXPECTED_POINTS = 2562
EXPECTED_FACES = 5120


def run_check(checks: dict[str, Any], name: str, action: Callable[[], dict[str, Any]]) -> None:
    try:
        checks[name] = {"ready": True, **action()}
    except Exception as exc:  # Keep testing other components after one failure.
        checks[name] = {"ready": False, "error": f"{type(exc).__name__}: {exc}"}


def check_npz(path: Path) -> dict[str, Any]:
    import numpy as np
    from scipy import sparse

    if not path.is_file():
        raise FileNotFoundError(path)
    arrays: dict[str, Any] = {}
    # These fixed, bundled caches include trusted SciPy sparse matrices stored
    # in NumPy object arrays. Never apply allow_pickle=True to user input.
    with np.load(path, allow_pickle=True) as archive:
        if not archive.files:
            raise ValueError("archive contains no arrays")
        for key in archive.files:
            value = np.asarray(archive[key])
            if value.size == 0:
                raise ValueError(f"array {key!r} is empty")
            detail: dict[str, Any] = {"shape": list(value.shape), "dtype": str(value.dtype)}
            if value.dtype == object:
                object_types: set[str] = set()
                item_sizes: list[int] = []
                total_sparse_nnz = 0
                for item in value.ravel():
                    object_types.add(type(item).__name__)
                    if sparse.issparse(item):
                        if item.nnz == 0:
                            raise ValueError(f"sparse object in {key!r} is empty")
                        if not np.isfinite(item.data).all():
                            raise ValueError(f"sparse object in {key!r} contains non-finite values")
                        item_sizes.append(int(item.shape[0] * item.shape[1]))
                        total_sparse_nnz += int(item.nnz)
                    else:
                        item_array = np.asarray(item)
                        if item_array.size == 0 or item_array.dtype == object:
                            raise ValueError(f"unsupported or empty object in {key!r}")
                        if np.issubdtype(item_array.dtype, np.number) and not np.isfinite(item_array).all():
                            raise ValueError(f"object in {key!r} contains non-finite values")
                        item_sizes.append(int(item_array.size))
                detail["objects"] = {
                    "count": int(value.size),
                    "types": sorted(object_types),
                    "totalValues": sum(item_sizes),
                    "minItemSize": min(item_sizes),
                    "maxItemSize": max(item_sizes),
                    "totalSparseNnz": total_sparse_nnz,
                }
            elif np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
                raise ValueError(f"array {key!r} contains non-finite values")
            arrays[key] = detail
    return {"path": str(path), "bytes": path.stat().st_size, "arrays": arrays}


def check_matlab_mesh(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    vertices = 0
    faces = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            stripped = line.lstrip()
            vertices += stripped.startswith("Vertex ")
            faces += stripped.startswith("Face ")
    if (vertices, faces) != (EXPECTED_POINTS, EXPECTED_FACES):
        raise ValueError(
            f"expected {EXPECTED_POINTS} vertices/{EXPECTED_FACES} faces; found {vertices}/{faces}"
        )
    return {"path": str(path), "vertices": vertices, "faces": faces}


def read_vtk_mesh(path: Path) -> tuple[int, int]:
    import vtk

    if not path.is_file():
        raise FileNotFoundError(path)
    reader = vtk.vtkPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    mesh = reader.GetOutput()
    return int(mesh.GetNumberOfPoints()), int(mesh.GetNumberOfPolys())


def check_vtk_mesh(path: Path, exact: bool = True) -> dict[str, Any]:
    points, faces = read_vtk_mesh(path)
    if exact and (points, faces) != (EXPECTED_POINTS, EXPECTED_FACES):
        raise ValueError(
            f"expected {EXPECTED_POINTS} points/{EXPECTED_FACES} polygons; found {points}/{faces}"
        )
    if not exact and (points <= 0 or faces <= 0):
        raise ValueError(f"mesh is empty: {points} points/{faces} polygons")
    return {"path": str(path), "points": points, "faces": faces}


def check_command(command: Path) -> dict[str, Any]:
    if not command.is_file():
        raise FileNotFoundError(command)
    completed = subprocess.run(
        [str(command), "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no output").strip()[:500]
        raise RuntimeError(f"--help exited with {completed.returncode}: {detail}")
    return {"path": str(command), "helpExitCode": completed.returncode}


def check_concon_kernel(root: Path, scratch: Path) -> dict[str, Any]:
    target = scratch / "runtime_assets" / "concon" / "c3_main"
    report = materialize_c3_main(root, target)
    completed = subprocess.run(
        ["ldd", str(target)], capture_output=True, text=True, check=False, timeout=60
    )
    if completed.returncode != 0 or "not found" in completed.stdout:
        detail = (completed.stderr or completed.stdout or "ldd returned no output").strip()[:1000]
        raise RuntimeError(f"c3_main shared-library validation failed: {detail}")
    return {
        "path": str(target),
        "bytes": report["bytes"],
        "sha256": report["sha256"],
        "parts": len(report["parts"]),
        "ldd": completed.stdout.strip(),
    }


def run_converter(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no output").strip()[:500]
        raise RuntimeError(f"converter exited with {completed.returncode}: {detail}")


def check_volume_converters(compat: Path, scratch: Path) -> dict[str, Any]:
    import nibabel as nib
    import numpy as np

    source = scratch / "self_test_input.nii.gz"
    converted = scratch / "self_test_mri_convert.nii.gz"
    restrided = scratch / "self_test_mrconvert.nii.gz"
    nib.save(nib.Nifti1Image(np.arange(27, dtype=np.float32).reshape((3, 3, 3)), np.eye(4)), source)
    run_converter([sys.executable, str(compat / "mri_convert"), str(source), str(converted)])
    run_converter(
        [sys.executable, str(compat / "mrconvert"), str(converted), "-stride", "1,2,3", str(restrided), "-force"]
    )
    image = nib.load(str(restrided))
    if image.shape != (3, 3, 3):
        raise ValueError(f"unexpected converted volume shape: {image.shape}")
    return {"shape": list(image.shape), "output": str(restrided)}


def check_surface_converter(compat: Path, assets: Path, scratch: Path) -> dict[str, Any]:
    source = assets / "data" / "fsaverage" / "surf" / "lh.white_avg"
    converted = scratch / "self_test_lh_white.vtk"
    run_converter([sys.executable, str(compat / "mris_convert"), str(source), str(converted)])
    return check_vtk_mesh(converted, exact=False)


def check_reference_bundle(assets: Path) -> dict[str, Any]:
    import nibabel as nib
    import nibabel.freesurfer.io as fsio

    reference = assets / "data" / "fsaverage"
    atlas = nib.load(str(reference / "mri.2mm" / "aseg.mgz"))
    if len(atlas.shape) != 3 or min(atlas.shape) <= 0:
        raise ValueError(f"invalid fsaverage aseg shape: {atlas.shape}")
    annotations: dict[str, int] = {}
    for hemisphere in ("lh", "rh"):
        labels, _, _ = fsio.read_annot(str(reference / "label" / f"{hemisphere}.aparc.a2009s.annot"))
        if labels.size != 163842:
            raise ValueError(f"{hemisphere} annotation has {labels.size} labels; expected 163842")
        annotations[hemisphere] = int(labels.size)
    return {"atlasShape": [int(value) for value in atlas.shape], "annotationVertices": annotations}


def check_stage_entrypoints(scripts: Path) -> dict[str, Any]:
    wrappers = scripts
    shell_scripts = sorted(wrappers.glob("sbci_step*.sh"))
    if len(shell_scripts) != 6:
        raise ValueError(f"expected six stage wrappers; found {len(shell_scripts)}")
    references: set[str] = set()
    pattern = re.compile(r'\$\{SBCI_SCRIPTS\}/([^"\\\s]+\.py)')
    for wrapper in shell_scripts:
        syntax = subprocess.run(["bash", "-n", str(wrapper)], capture_output=True, text=True, check=False, timeout=30)
        if syntax.returncode != 0:
            raise RuntimeError(f"bash -n failed for {wrapper.name}: {syntax.stderr.strip()[:500]}")
        for line in wrapper.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lstrip().startswith("#"):
                continue
            references.update(pattern.findall(line))
    failures: list[str] = []
    for relative in sorted(references):
        script = scripts / relative
        if not script.is_file():
            failures.append(f"missing: {relative}")
            continue
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "no output").strip()[:300]
            failures.append(f"{relative}: exit {completed.returncode}: {detail}")
    if failures:
        raise RuntimeError("; ".join(failures))
    return {
        "bashSyntaxChecked": [path.name for path in shell_scripts],
        "pythonEntrypointsChecked": len(references),
        "pythonEntrypoints": sorted(references),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-root", required=True)
    parser.add_argument("--scratch", required=True)
    args = parser.parse_args(argv)

    root = Path(args.pipeline_root).resolve()
    scratch = Path(args.scratch).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    scripts = root / "pipeline"
    assets = root
    grid = assets / "data" / "grid_ico4"
    compat = root / "workflow" / "compat_bin"
    checks: dict[str, Any] = {}

    run_check(checks, "conconKernel", lambda: check_concon_kernel(root, scratch))

    for name in ("mapping_avg_ico4.npz", "grid_coords_ico4.npz"):
        run_check(checks, name, lambda name=name: check_npz(grid / name))
    for hemisphere in ("lh", "rh"):
        matlab = f"{hemisphere}_grid_avg_ico4.m"
        vtk_name = f"{hemisphere}_grid_avg_ico4.vtk"
        run_check(checks, matlab, lambda matlab=matlab: check_matlab_mesh(grid / matlab))
        run_check(checks, vtk_name, lambda vtk_name=vtk_name: check_vtk_mesh(grid / vtk_name))

    runtime_bin = Path(sys.executable).parent
    for command in (
        "scil_surface_flip",
        "scil_surface_smooth",
        "scil_tracking_pft_maps",
        "scil_tractogram_convert",
    ):
        run_check(checks, command, lambda command=command: check_command(runtime_bin / command))

    run_check(checks, "volumeConverters", lambda: check_volume_converters(compat, scratch))
    run_check(checks, "surfaceConverter", lambda: check_surface_converter(compat, assets, scratch))
    run_check(checks, "referenceBundle", lambda: check_reference_bundle(assets))
    run_check(checks, "stageEntrypoints", lambda: check_stage_entrypoints(scripts))

    ready = bool(checks) and all(item.get("ready") for item in checks.values())
    result = {
        "ready": ready,
        "patientDataUsed": False,
        "scientificAlgorithmRan": False,
        "checks": checks,
        "message": (
            "Local SBCI runtime and bundled assets passed the patient-free self-test."
            if ready
            else "One or more local SBCI runtime self-test checks failed."
        ),
    }
    print(json.dumps(result, indent=2))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
