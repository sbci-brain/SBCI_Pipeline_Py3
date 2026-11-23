#!/usr/bin/env python3
"""Local, sequential CyberNeuro executor for SBCI-ready subject data."""

from __future__ import annotations

import argparse
import html
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from workflow.planner import STAGE_ORDER, inventory_subject, looks_like_subject, plan_workflow, probe_commands
from workflow.c3_asset import AssetError, inspect_c3_asset, materialize_c3_main
from workflow.orchestration import ORCHESTRATION_MODES, build_orchestration_plan, consume_handoff, handoff_template
from workflow.preprocessing_executor import execute_prepare_data, source_snapshot
from workflow.workflow_executor import (
    REFERENCE_SCRIPTS,
    available_cpus,
    execute_workflow_stage_safely,
    profile_mismatches,
    profile_path,
    template_problem,
)


PIPELINE_NAME = "sbci-pipeline"
SCHEMA_VERSION = "1.0"
CONFIG_NAMES = ("sbci_local.json", "local.sbci.json")
INPUT_PROFILES = ("auto", "raw_bids", "preprocessed", "sbci_ready", "completed_connectivity", "py3_preprocessed")
INPUT_PROFILE_ALIASES = {"py3_preprocessed": "preprocessed"}
# The Python that runs the SBCI stage when SBCI_CORE_PY (the image's) is not set: this one.
RUNTIME_PYTHON_DEFAULT = Path(sys.executable)
FS_SURFACES = (
    "lh.sphere", "rh.sphere", "lh.sphere.reg", "rh.sphere.reg",
    "lh.white", "rh.white", "lh.pial", "rh.pial",
    "lh.inflated", "rh.inflated",
)
FSFAST_FILES = ("fc.surface.lh.nii.gz", "fc.surface.rh.nii.gz", "fc.mni.nii.gz")
DEFAULT_PARAMETERS = {
    "n_runs": 4,
    "n_seed": 1000,
    "steps": 75,
    "rng": 1234,
    "bandwidth": 0.005,
    "resolution": "ico4",
    "rois": [4, 8, 10, 11, 12, 13, 17, 18, 26, 43, 47, 49, 50, 51, 52, 53, 54, 58, 16],
}
QUICK_PARAMETERS = {**DEFAULT_PARAMETERS, "n_runs": 1, "n_seed": 1, "steps": 5}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def issue(code: str, message: str, path: Path | None = None) -> dict[str, str]:
    value = {"severity": "error", "code": code, "message": message}
    if path is not None:
        value["path"] = str(path)
    return value


def find_named(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    for name in names:
        matches = sorted(root.glob(f"*/{name}"))
        if matches:
            return matches[0]
    return None


# workflow.stages: which part of the run a skill asks for. "preprocess" stops before SBCI;
# "tractography" runs SBCI steps 2-5 (SC) and "fc" step 6 (FC); "all" runs everything.
STAGE_SETS = ("all", "preprocess", "tractography", "fc")


def requested_sbci_steps(workflow_config: dict[str, Any]) -> tuple[bool, bool]:
    """(structural, functional) SBCI steps for workflow.stages."""
    stages = (workflow_config or {}).get("stages", "all")
    return stages in ("all", "tractography"), stages in ("all", "fc")


def load_config(input_dir: Path) -> tuple[dict[str, Any], Path | None, list[dict[str, str]]]:
    config_path = find_named(input_dir, CONFIG_NAMES)
    if config_path is None:
        return {"schema_version": SCHEMA_VERSION, "mode": "inspect", "approved": False}, None, []
    try:
        value = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, config_path, [issue("invalid_local_config", str(exc), config_path)]
    if not isinstance(value, dict):
        return {}, config_path, [issue("invalid_local_config", "sbci_local.json must contain a JSON object", config_path)]
    errors: list[dict[str, str]] = []
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append(issue("unsupported_schema", f"schema_version must be {SCHEMA_VERSION}", config_path))
    if value.get("mode", "inspect") not in {"self_test", "inspect", "preprocess", "quick_test", "production"}:
        errors.append(issue("invalid_mode", "mode must be self_test, inspect, preprocess, quick_test, or production", config_path))
    stages = (value.get("workflow") or {}).get("stages", "all") if isinstance(value.get("workflow"), dict) else "all"
    if stages not in STAGE_SETS:
        errors.append(issue("invalid_stages", f"workflow.stages must be one of: {', '.join(STAGE_SETS)}", config_path))
    return value, config_path, errors


def config_template() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "inspect",
        "approved": False,
        "workflow": {
            "input_profile": "auto",
            "run_psc": False,
            "resume": True,
            "require_distortion_correction": True,
            "distortion_corrected_input": False,
            "synb0_image": "",
            "freesurfer_license": "",
            "reference_scripts_dir": "",
            "preprocessing_config": "datasets/<dataset>/profile.conf",
            "orchestration": {
                "mode": "manual_shared_artifacts",
                "shared_work_dir": "",
                "handoff_manifest": "",
                "verify_hashes": False,
                "consume_handoff": False,
            },
        },
        "parameters": DEFAULT_PARAMETERS,
        "notes": {
            "self_test": "Patient-free runtime and bundled-asset integrity test; produces no connectivity matrices.",
            "quick_test": "Reduced 1-run/1000-seed smoke test; not valid for scientific interpretation.",
            "preprocess": "Run only the next raw-BIDS, preprocessing, PSC, or SBCI stage in an output-side work tree; requires approved=true.",
            "production": "Uses the displayed parameters and requires approved=true.",
        },
    }


def find_subjects(input_dir: Path, input_profile: str = "auto") -> list[Path]:
    """Find subject/session folders by their contents, independent of root names."""
    candidates: list[Path] = []
    roots = [input_dir]
    if input_profile != "raw_bids":
        # These are discovery conveniences for older uploads, not requirements.
        roots.extend((input_dir / "py3" / "preprocessed", input_dir / "preprocessed"))
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        children = sorted(item for item in root.iterdir() if item.is_dir())
        possible = [root, *children]
        # ADNI/BIDS trees commonly use <root>/sub-*/ses-*/. Limit discovery to
        # two levels so selecting a large root remains predictable.
        for child in children:
            if child.name.startswith("sub-"):
                possible.extend(sorted(item for item in child.iterdir() if item.is_dir()))
        for candidate in possible:
            try:
                key = str(candidate.resolve())
            except OSError:
                key = str(candidate)
            if key not in seen and looks_like_subject(candidate):
                seen.add(key)
                candidates.append(candidate)
    return candidates


def resolved_input_profile(requested: str, subject: Path | None, inventory: dict[str, Any] | None) -> str:
    requested = INPUT_PROFILE_ALIASES.get(requested, requested)
    if requested != "auto":
        return requested
    if inventory and inventory.get("completedConnectivity"):
        return "completed_connectivity"
    if inventory and inventory.get("sbciReady"):
        return "sbci_ready"
    if inventory and any(inventory.get("raw", {}).values()):
        return "raw_bids"
    return "preprocessed"


def effective_work_subject(subject: Path, output_dir: Path) -> Path:
    """Use a previously created output-side subject tree when it exists."""
    candidate = output_dir / "files" / "preprocessing_work" / subject.name
    provenance = output_dir / "files" / "preprocessing_work" / "source_provenance.json"
    if not candidate.is_dir() or not provenance.is_file():
        return subject
    try:
        value = json.loads(provenance.read_text(encoding="utf-8"))
        if value.get("source", {}).get("fingerprint") == source_snapshot(subject)["fingerprint"]:
            return candidate
    except (OSError, json.JSONDecodeError):
        pass
    return subject


def nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def inspect_subject(subject: Path) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    dwi = subject / "dwi_pipeline"
    diffusion = dwi / "diffusion"
    structure = dwi / "structure"
    for path, code, label in (
        (diffusion / "dti" / "fa.nii.gz", "missing_fa", "DTI FA image"),
        (diffusion / "fodf" / "fodf.nii.gz", "missing_fodf", "fODF image"),
        (structure / "aparc.a2009s+aseg.nii.gz", "missing_aparc", "aparc.a2009s+aseg image"),
        (structure / "map_wm.nii.gz", "missing_wm_map", "white-matter map"),
        (structure / "map_gm.nii.gz", "missing_gm_map", "gray-matter map"),
        (structure / "map_csf.nii.gz", "missing_csf_map", "CSF map"),
    ):
        if not nonempty(path):
            errors.append(issue(code, f"Required {label} is missing or empty", path))
    fs_root = structure / "t1_freesurfer"
    for name in FS_SURFACES:
        path = fs_root / "surf" / name
        if not nonempty(path):
            errors.append(issue("missing_freesurfer_surface", f"Required FreeSurfer surface is missing: {name}", path))
    for relative in ("label/lh.aparc.a2009s.annot", "label/rh.aparc.a2009s.annot", "mri/wmparc.mgz"):
        path = fs_root / relative
        if not nonempty(path):
            errors.append(issue("missing_freesurfer_product", f"Required FreeSurfer product is missing: {relative}", path))
    bold = subject / "fmri" / "bold"
    runs = sorted(path for path in bold.glob("[0-9]*") if path.is_dir() and path.name.isdigit()) if bold.is_dir() else []
    if not runs:
        errors.append(issue("missing_fmri_runs", "No numeric preprocessed fMRI run folders were found", bold))
    for run in runs:
        for name in FSFAST_FILES:
            path = run / name
            if not nonempty(path):
                errors.append(issue("missing_fsfast_residual", f"Required FSFast residual is missing: {name}", path))
    return errors


def resolve_runtime_python(home: Path | None = None) -> dict[str, Any]:
    """Select a usable SBCI Python without relying on an interactive shell."""
    configured = os.environ.get("SBCI_CORE_PY")
    if configured:
        candidates = [("environment", Path(configured).expanduser())]
    else:
        user_home = home or Path.home()
        candidates = [
            ("current_python", RUNTIME_PYTHON_DEFAULT),
            ("user_conda_sbci_py3", user_home / ".conda" / "envs" / "sbci_py3" / "bin" / "python"),
        ]

    attempts: list[dict[str, Any]] = []
    for source, path in candidates:
        exists = path.is_file()
        exit_code: int | None = None
        ready = False
        if exists:
            try:
                completed = subprocess.run(
                    [str(path), "-c", "import numpy, scipy"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                exit_code = completed.returncode
                ready = exit_code == 0
            except OSError:
                ready = False
        attempts.append({
            "source": source,
            "path": str(path),
            "exists": exists,
            "numpyScipyReady": ready,
            "probeExitCode": exit_code,
        })
        if ready:
            return {"path": path, "source": source, "ready": True, "candidates": attempts}

    source, path = candidates[0]
    return {"path": path, "source": source, "ready": False, "candidates": attempts}


def runtime_probe(pipeline_root: Path) -> dict[str, Any]:
    resolution = resolve_runtime_python()
    python = resolution["path"]
    required = pipeline_root / "pipeline"
    assets = pipeline_root / "data"
    reference = assets / "fsaverage"
    grid = assets / "grid_ico4"
    required_reference_files = (
        reference / "surf" / "lh.white_avg",
        reference / "surf" / "rh.white_avg",
        reference / "surf" / "lh.sphere.reg.avg",
        reference / "surf" / "rh.sphere.reg.avg",
        reference / "surf" / "lh.inflated_avg",
        reference / "surf" / "rh.inflated_avg",
        reference / "label" / "lh.aparc.a2009s.annot",
        reference / "label" / "rh.aparc.a2009s.annot",
        reference / "mri.2mm" / "aseg.mgz",
    )
    required_grid_files = tuple(
        grid / name
        for name in (
            "mapping_avg_ico4.npz",
            "grid_coords_ico4.npz",
            "lh_grid_avg_ico4.m",
            "rh_grid_avg_ico4.m",
            "lh_grid_avg_ico4.vtk",
            "rh_grid_avg_ico4.vtk",
        )
    )
    try:
        concon_asset = inspect_c3_asset(pipeline_root)
    except (AssetError, OSError) as exc:
        concon_asset = {"ready": False, "error": str(exc)}
    checks = {
        "python": str(python),
        "pythonSource": resolution["source"],
        "pythonResolution": {
            "ready": resolution["ready"],
            "candidates": resolution["candidates"],
        },
        "pythonExists": python.is_file(),
        "bashExists": bool(shutil.which("bash")),
        "pipelineScriptsExist": required.is_dir(),
        "conconExists": bool(concon_asset.get("ready")),
        "conconAsset": concon_asset,
        "adapterScripts": {
            name: nonempty(pipeline_root / "workflow" / name)
            for name in ("runtime_self_test.py", "subject_metadata_probe.py")
        },
        "compatCommands": {
            name: nonempty(pipeline_root / "workflow" / "compat_bin" / name)
            for name in ("mris_convert", "mri_convert", "mrconvert")
        },
        "stageWrappers": {
            name: nonempty(required / name)
            for name in (
                "sbci_step1_process_grid.sh",
                "sbci_step2_prepare_set.sh",
                "sbci_step3_run_set.sh",
                "sbci_step4_process_surfaces.sh",
                "sbci_step5_structural.sh",
                "sbci_step6_functional.sh",
            )
        },
        "referenceExists": all(nonempty(path) for path in required_reference_files),
        "referenceFiles": {str(path.relative_to(pipeline_root)): nonempty(path) for path in required_reference_files},
        "ico4GridCacheExists": all(nonempty(path) for path in required_grid_files),
        "ico4GridFiles": {str(path.relative_to(pipeline_root)): nonempty(path) for path in required_grid_files},
    }
    if checks["pythonExists"]:
        code = (
            "import importlib.util,json;"
            "n=['numpy','scipy','nibabel','dipy','vtk','trimeshpy','scilpy','h5py','tqdm'];"
            "print(json.dumps({x:bool(importlib.util.find_spec(x)) for x in n}))"
        )
        completed = subprocess.run([str(python), "-c", code], capture_output=True, text=True, check=False)
        try:
            checks["packages"] = json.loads(completed.stdout)
        except json.JSONDecodeError:
            checks["packages"] = {}
        checks["pythonExitCode"] = completed.returncode
    runtime_bin = python.parent
    commands = (
        "scil_surface_flip",
        "scil_surface_smooth",
        "scil_tracking_pft_maps",
        "scil_tractogram_convert",
    )
    checks["commands"] = {name: (runtime_bin / name).is_file() for name in commands}
    concon = assets / "concon" / "c3_main"
    temporary: str | None = None
    try:
        if not concon.is_file() and concon_asset.get("ready"):
            temporary = tempfile.mkdtemp(prefix="cyberneuro-c3-probe-")
            concon = Path(temporary) / "c3_main"
            materialize_c3_main(pipeline_root, concon)
        if concon.is_file() and shutil.which("ldd"):
            completed = subprocess.run(["ldd", str(concon)], capture_output=True, text=True, check=False)
            checks["conconLibrariesReady"] = completed.returncode == 0 and "not found" not in completed.stdout
            checks["conconLdd"] = completed.stdout.strip()
        else:
            checks["conconLibrariesReady"] = False
    except (AssetError, OSError) as exc:
        checks["conconLibrariesReady"] = False
        checks["conconLdd"] = str(exc)
    finally:
        if temporary is not None:
            # On NFS (a TMPDIR on /work) the rmdir can fail with "Directory not empty" just after ldd;
            # a leftover probe folder is no reason to fail the preflight.
            shutil.rmtree(temporary, ignore_errors=True)
    packages = checks.get("packages") or {}
    checks["ready"] = bool(
        checks["pythonExists"]
        and checks["bashExists"]
        and checks["pipelineScriptsExist"]
        and checks["conconExists"]
        and all(checks["adapterScripts"].values())
        and all(checks["compatCommands"].values())
        and all(checks["stageWrappers"].values())
        and checks["referenceExists"]
        and checks["ico4GridCacheExists"]
        and packages
        and all(packages.values())
        and all(checks["commands"].values())
        and checks["conconLibrariesReady"]
    )
    return checks


def run_runtime_self_test(pipeline_root: Path, output_dir: Path, runtime_python: Path) -> dict[str, Any]:
    """Exercise the real local runtime and bundled assets without patient data."""
    script = pipeline_root / "workflow" / "runtime_self_test.py"
    scratch = output_dir / "files" / "runtime_self_test_work"
    started = time.time()
    completed = subprocess.run(
        [
            str(runtime_python),
            str(script),
            "--pipeline-root",
            str(pipeline_root),
            "--scratch",
            str(scratch),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    log = output_dir / "logs" / "runtime_self_test.log"
    log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8", errors="replace")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = {
            "ready": False,
            "patientDataUsed": False,
            "scientificAlgorithmRan": False,
            "checks": {},
            "message": "The runtime self-test did not return valid JSON.",
        }
    result.update({
        "exitCode": completed.returncode,
        "elapsedSeconds": round(time.time() - started, 1),
        "log": str(log),
    })
    result["ready"] = bool(result.get("ready") and completed.returncode == 0)
    write_json(output_dir / "files" / "runtime_self_test.json", result)
    return result


def run_subject_metadata_probe(
    pipeline_root: Path,
    subject: Path,
    output_dir: Path,
    runtime_python: Path,
    rois: list[int],
) -> dict[str, Any]:
    """Check image grids and surface/time-series dimensions before expensive stages."""
    script = pipeline_root / "workflow" / "subject_metadata_probe.py"
    completed = subprocess.run(
        [
            str(runtime_python),
            str(script),
            "--subject",
            str(subject),
            "--pipeline-root",
            str(pipeline_root),
            "--rois",
            ",".join(str(value) for value in rois),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    log = output_dir / "logs" / "subject_metadata_probe.log"
    log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8", errors="replace")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = {"ready": False, "errors": [], "summary": {}, "message": "Metadata probe did not return valid JSON."}
    result.update({"exitCode": completed.returncode, "log": str(log)})
    result["ready"] = bool(result.get("ready") and completed.returncode == 0)
    write_json(output_dir / "files" / "subject_metadata.json", result)
    return result


def validate_connectivity(
    connectivity: Path,
    parameters: dict[str, Any],
    runtime_python: Path,
    structural: bool = True,
    functional: bool = True,
) -> dict[str, Any]:
    """Require parseable, non-empty SC and/or FC artifacts (the ones requested) before claiming completion."""
    bandwidth = parameters["bandwidth"]
    resolution = parameters["resolution"]
    expected = {}
    if structural:
        expected.update({
            "structuralMat": connectivity / f"smoothed_sc_avg_{bandwidth}_{resolution}.mat",
            "structuralSparse": connectivity / f"smoothed_sc_avg_{bandwidth}_{resolution}.npz",
            "subcorticalStructural": connectivity / f"sub_sc_avg_{bandwidth}_{resolution}.mat",
        })
    if functional:
        expected["functionalMat"] = connectivity / f"fc_avg_{resolution}.mat"
    result: dict[str, Any] = {
        "expected": {key: str(path) for key, path in expected.items()},
        "nonempty": {key: nonempty(path) for key, path in expected.items()},
    }
    if not all(result["nonempty"].values()):
        result["ready"] = False
        result["message"] = "One or more required connectivity outputs are missing or empty."
        return result
    expected_vertices = 2 * (10 * (4 ** int(resolution.removeprefix("ico"))) + 2)
    code = """
import json, sys
import numpy as np
from scipy import io, sparse

paths = json.loads(sys.argv[1])
expected_n = int(sys.argv[2])
summary = {}
for key, path in paths.items():
    if path.endswith('.npz'):
        value = sparse.load_npz(path)
        summary[key] = {
            'shape': list(value.shape),
            'finite': bool(np.isfinite(value.data).all()),
            'nonzero': int(value.nnz),
            'valid': bool(value.shape == (expected_n, expected_n) and value.nnz > 0 and np.isfinite(value.data).all()),
        }
    else:
        value = io.loadmat(path)
        arrays = {k: v for k, v in value.items() if not k.startswith('__') and hasattr(v, 'shape')}
        finite = bool(arrays and all(np.isfinite(v).all() for v in arrays.values()))
        detail = {
            'arrays': {k: list(v.shape) for k, v in arrays.items()},
            'finite': finite,
            'nonzero': {k: int(np.count_nonzero(v)) for k, v in arrays.items()},
        }
        if key == 'structuralMat':
            matrix = arrays.get('sc')
            valid = matrix is not None and matrix.shape == (expected_n, expected_n) and np.count_nonzero(matrix) > 0
        elif key == 'functionalMat':
            matrix = arrays.get('fc')
            valid = matrix is not None and matrix.shape == (expected_n, expected_n)
            if valid:
                off_diagonal = matrix.copy()
                np.fill_diagonal(off_diagonal, 0)
                valid = np.count_nonzero(off_diagonal) > 0
        elif key == 'subcorticalStructural':
            sub_sub = arrays.get('sub_sub_sc')
            sub_surf = arrays.get('sub_surf_sc')
            valid = (
                sub_sub is not None and sub_sub.ndim == 2 and sub_sub.shape[0] == sub_sub.shape[1]
                and sub_surf is not None and sub_surf.ndim == 2 and sub_surf.shape[1] == expected_n
                and (np.count_nonzero(sub_sub) + np.count_nonzero(sub_surf)) > 0
            )
        else:
            valid = False
        detail['valid'] = bool(finite and valid)
        summary[key] = detail
print(json.dumps(summary))
"""
    completed = subprocess.run(
        [
            str(runtime_python),
            "-c",
            code,
            json.dumps({key: str(path) for key, path in expected.items()}),
            str(expected_vertices),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    result["parserExitCode"] = completed.returncode
    try:
        result["parsed"] = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result["parsed"] = {}
    result["parserError"] = completed.stderr.strip()
    result["ready"] = bool(
        completed.returncode == 0
        and result["parsed"]
        and all(item.get("valid") for item in result["parsed"].values())
    )
    result["message"] = (
        "Required SC and FC artifacts have validated ico4 dimensions, finite values, and non-empty connectivity."
        if result["ready"]
        else "Connectivity outputs failed parse, dimension, finite-value, or non-empty connectivity validation."
    )
    return result


def validate_completed_connectivity(
    connectivity: Path,
    parameters: dict[str, Any],
    runtime_python: Path,
) -> dict[str, Any]:
    """Read-only validation for the cleaned ADNI terminal MAT-only layout."""
    bandwidth = parameters["bandwidth"]
    resolution = parameters["resolution"]
    expected = {
        "structuralMat": connectivity / f"smoothed_sc_avg_{bandwidth}_{resolution}.mat",
        "subcorticalStructural": connectivity / f"sub_sc_avg_{bandwidth}_{resolution}.mat",
        "functionalMat": connectivity / f"fc_avg_{resolution}.mat",
    }
    result: dict[str, Any] = {
        "route": "completed_connectivity",
        "readOnly": True,
        "expected": {key: str(path) for key, path in expected.items()},
        "nonempty": {key: nonempty(path) for key, path in expected.items()},
    }
    if not all(result["nonempty"].values()):
        result["ready"] = False
        result["message"] = "One or more required completed-connectivity MAT files are missing or empty."
        return result
    expected_vertices = 2 * (10 * (4 ** int(resolution.removeprefix("ico"))) + 2)
    code = """
import json, sys
import numpy as np
from scipy import io

paths = json.loads(sys.argv[1])
expected_n = int(sys.argv[2])
summary = {}
for key, path in paths.items():
    value = io.loadmat(path)
    arrays = {k: v for k, v in value.items() if not k.startswith('__') and hasattr(v, 'shape')}
    finite = bool(arrays and all(np.isfinite(v).all() for v in arrays.values()))
    if key == 'structuralMat':
        matrix = arrays.get('sc')
        valid = matrix is not None and matrix.shape == (expected_n, expected_n) and np.count_nonzero(matrix) > 0
    elif key == 'functionalMat':
        matrix = arrays.get('fc')
        valid = matrix is not None and matrix.shape == (expected_n, expected_n)
        if valid:
            off_diagonal = matrix.copy()
            np.fill_diagonal(off_diagonal, 0)
            valid = np.count_nonzero(off_diagonal) > 0
    else:
        sub_sub = arrays.get('sub_sub_sc')
        sub_surf = arrays.get('sub_surf_sc')
        valid = (
            sub_sub is not None and sub_sub.shape == (19, 19)
            and sub_surf is not None and sub_surf.shape == (19, expected_n)
            and (np.count_nonzero(sub_sub) + np.count_nonzero(sub_surf)) > 0
        )
    summary[key] = {
        'arrays': {k: list(v.shape) for k, v in arrays.items()},
        'finite': finite,
        'valid': bool(finite and valid),
    }
print(json.dumps(summary))
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code, json.dumps({key: str(path) for key, path in expected.items()}), str(expected_vertices)],
        capture_output=True,
        text=True,
        check=False,
    )
    result["parserExitCode"] = completed.returncode
    try:
        result["parsed"] = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result["parsed"] = {}
    result["parserError"] = completed.stderr.strip()
    result["ready"] = bool(
        completed.returncode == 0
        and result["parsed"]
        and all(item.get("valid") for item in result["parsed"].values())
    )
    result["message"] = (
        "Completed SC/FC matrices passed read-only ico4 and subcortical validation."
        if result["ready"]
        else "Completed connectivity failed MAT parsing, dimensions, finite-value, or non-empty checks."
    )
    return result


def parameter_values(config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    mode = config.get("mode", "inspect")
    if mode == "quick_test":
        return dict(QUICK_PARAMETERS), []
    supplied = config.get("parameters", DEFAULT_PARAMETERS)
    if not isinstance(supplied, dict):
        return {}, [issue("invalid_parameters", "parameters must be a JSON object")]
    merged = {**DEFAULT_PARAMETERS, **supplied}
    errors: list[dict[str, str]] = []
    for key, minimum, maximum in (("n_runs", 1, 32), ("n_seed", 1, 100000), ("steps", 1, 1000), ("rng", 0, 2147483647)):
        value = merged.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            errors.append(issue("invalid_parameter", f"{key} must be an integer from {minimum} through {maximum}"))
    bandwidth = merged.get("bandwidth")
    if isinstance(bandwidth, bool) or not isinstance(bandwidth, (int, float)) or not 0 < bandwidth <= 1:
        errors.append(issue("invalid_parameter", "bandwidth must be greater than 0 and at most 1"))
    if merged.get("resolution") != "ico4":
        errors.append(issue("invalid_parameter", "This validated local bundle currently supports resolution=ico4 only"))
    rois = merged.get("rois")
    if not isinstance(rois, list) or not rois or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in rois):
        errors.append(issue("invalid_parameter", "rois must be a non-empty array of non-negative integers"))
    return merged, errors


def generated_config(
    pipeline_root: Path,
    grid_dir: Path,
    parameters: dict[str, Any],
    concon_dir: Path | None = None,
) -> str:
    concon_dir = concon_dir or pipeline_root / "data" / "concon"
    rois = " ".join(str(value) for value in parameters["rois"])
    return "\n".join((
        "# Generated by the SBCI workflow for the SBCI stage (pipeline/sbci_step*.sh).",
        "DATASET_NAME=SBCI",
        "DTI_BVALS=(0 1000)",
        "FODF_BVALS=(0 1000)",
        f"CONCON_PATH='{concon_dir}'",
        f"OUTPUT_PATH='{grid_dir}'",
        f"REFDIR='{pipeline_root / 'data' / 'fsaverage'}'",
        f"N_RUNS={parameters['n_runs']}",
        f"N_SEED={parameters['n_seed']}",
        f"STEPS={parameters['steps']}",
        f"RNG={parameters['rng']}",
        f"BANDWIDTH={parameters['bandwidth']}",
        f"RESOLUTION='{parameters['resolution']}'",
        f"ROIS=({rois})",
        "",
    ))


def link_directory(source: Path, destination: Path) -> None:
    link_path(source, destination, target_is_directory=True)


def link_path(source: Path, destination: Path, *, target_is_directory: bool) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Refusing to replace staging path: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source, target_is_directory=target_is_directory)


def synb0_configured(runtime: dict[str, Any]) -> bool:
    """Synb0 image and runner configured, and a FreeSurfer license readable (DWI correction can run)."""
    return (runtime.get("synb0") or {}).get("status") == "configured" and bool(runtime.get("freesurferLicense"))


def start_work_tree(subject: Path, work_subject: Path, keep_existing: bool = False) -> Path | None:
    """Stage a fresh SBCI work tree. An earlier attempt's tree (a failed or interrupted
    run) is renamed <subject>.attempt-<time> beside it, kept for inspection; returns it.
    keep_existing reuses a tree that is already there (an FC-only call after tractography)."""
    retired = None
    if keep_existing and work_subject.is_dir():
        return None
    if work_subject.exists() or work_subject.is_symlink():
        retired = work_subject.with_name(f"{work_subject.name}.attempt-{time.strftime('%Y%m%dT%H%M%S')}")
        work_subject.rename(retired)
    prepare_work_tree(subject, work_subject)
    return retired


def prepare_work_tree(subject: Path, work_subject: Path) -> None:
    source_dwi = subject / "dwi_pipeline"
    target_dwi = work_subject / "dwi_pipeline"
    target_dwi.mkdir(parents=True, exist_ok=False)
    for name in ("diffusion", "psc_connectome"):
        source = source_dwi / name
        if source.exists():
            link_directory(source, target_dwi / name)
    # Step 2 writes set_map_include/exclude/interface into structure/. Keep the
    # container real while linking its large, read-only FreeSurfer inputs so
    # the source ADNI tree cannot be modified through a directory symlink.
    source_structure = source_dwi / "structure"
    target_structure = target_dwi / "structure"
    target_structure.mkdir()
    generated_structure_names = {
        "set_map_include.nii.gz",
        "set_map_exclude.nii.gz",
        "set_interface.nii.gz",
    }
    for source in sorted(source_structure.iterdir()):
        if source.name in generated_structure_names:
            continue
        link_path(source, target_structure / source.name, target_is_directory=source.is_dir())
    link_directory(subject / "fmri", work_subject / "fmri")


def run_stage(name: str, command: list[str], cwd: Path, environment: dict[str, str], logs: Path) -> dict[str, Any]:
    started = time.time()
    log = logs / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("w", encoding="utf-8", errors="replace") as stream:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        exit_code = completed.returncode
    except OSError as exc:
        log.write_text(f"Could not start stage: {type(exc).__name__}: {exc}\n", encoding="utf-8")
        exit_code = 127
    return {
        "name": name,
        "exitCode": exit_code,
        "elapsedSeconds": round(time.time() - started, 1),
        "log": str(log),
    }


def execute_local(
    pipeline_root: Path,
    subject: Path,
    output_dir: Path,
    parameters: dict[str, Any],
    runtime_python: Path,
    functional: bool = True,
    structural: bool = True,
) -> tuple[list[dict[str, Any]], Path]:
    """Run SBCI steps 1-6: structural = steps 2-5 (SET tractography, SC), functional = step 6 (FC)."""
    scripts = pipeline_root / "pipeline"
    wrappers = scripts
    compat_bin = pipeline_root / "workflow" / "compat_bin"
    runtime_bin = runtime_python.parent
    concon = output_dir / "files" / "runtime_assets" / "concon" / "c3_main"
    materialize_c3_main(pipeline_root, concon)
    for executable in (
        compat_bin / "mris_convert",
        compat_bin / "mri_convert",
        compat_bin / "mrconvert",
        concon,
    ):
        try:
            executable.chmod(0o755)
        except OSError:
            pass
    work_subject = output_dir / "files" / "work" / subject.name
    start_work_tree(subject, work_subject, keep_existing=not structural)
    grid = output_dir / "files" / "grid"
    grid.mkdir(parents=True, exist_ok=True)
    cached_grid = pipeline_root / "data" / f"grid_{parameters['resolution']}"
    required_grid = (
        f"mapping_avg_{parameters['resolution']}.npz",
        f"grid_coords_{parameters['resolution']}.npz",
        f"lh_grid_avg_{parameters['resolution']}.m",
        f"rh_grid_avg_{parameters['resolution']}.m",
        f"lh_grid_avg_{parameters['resolution']}.vtk",
        f"rh_grid_avg_{parameters['resolution']}.vtk",
    )
    use_cached_grid = cached_grid.is_dir() and all(nonempty(cached_grid / name) for name in required_grid)
    if use_cached_grid:
        for name in required_grid:
            shutil.copy2(cached_grid / name, grid / name)
    config = output_dir / "files" / "generated_sbci_config"
    # write_bytes, not write_text(newline=...): that kwarg needs Python 3.10 and
    # the host python3 may be older (Longleaf's default is 3.9).
    config.write_bytes(
        generated_config(pipeline_root, grid, parameters, concon.parent).encode("utf-8")
    )

    environment = os.environ.copy()
    environment.update({
        "SBCI_CONFIG": str(config),
        "SBCI_SCRIPTS": str(scripts),
        "PYTHONPATH": str(scripts) + os.pathsep + environment.get("PYTHONPATH", ""),
        "PATH": os.pathsep.join((str(compat_bin), str(runtime_bin), environment.get("PATH", ""))),
    })
    stages: list[tuple[str, list[str], Path]] = []
    results: list[dict[str, Any]] = []
    if use_cached_grid:
        log = output_dir / "logs" / "step1_grid_cached.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            f"Reused validated bundled {parameters['resolution']} grid from {cached_grid}.\n",
            encoding="utf-8",
        )
        results.append({"name": "step1_grid_cached", "exitCode": 0, "elapsedSeconds": 0.0, "log": str(log)})
    else:
        stages.append(("step1_grid", ["bash", str(wrappers / "sbci_step1_process_grid.sh")], output_dir))
    if structural:
        stages.append(("step2_prepare", ["bash", str(wrappers / "sbci_step2_prepare_set.sh")], work_subject))
        for run in range(1, int(parameters["n_runs"]) + 1):
            stages.append((f"step3_set_{run}", ["bash", str(wrappers / "sbci_step3_run_set.sh"), str(run)], work_subject))
        stages.extend((
            ("step4_surfaces", ["bash", str(wrappers / "sbci_step4_process_surfaces.sh")], work_subject),
            ("step5_structural", ["bash", str(wrappers / "sbci_step5_structural.sh")], work_subject),
        ))
    if functional:
        stages.append(("step6_functional", ["bash", str(wrappers / "sbci_step6_functional.sh")], work_subject))
    # SET repetitions are independent (each has its own seed and files), so they run side by
    # side, one per CPU up to N_RUNS; SBCI_SET_PROCESSES caps that (e.g. for memory).
    set_processes = min(available_cpus(), int(os.environ.get("SBCI_SET_PROCESSES") or 0) or available_cpus())
    for is_set, group in itertools.groupby(stages, key=lambda stage: stage[0].startswith("step3_set_")):
        group = list(group)
        if is_set:
            with ThreadPoolExecutor(max_workers=max(1, min(len(group), set_processes))) as pool:
                batch = list(pool.map(lambda s: run_stage(s[0], s[1], s[2], environment, output_dir / "logs"), group))
        else:
            batch = []
            for name, command, cwd in group:
                batch.append(run_stage(name, command, cwd, environment, output_dir / "logs"))
                if batch[-1]["exitCode"] != 0:
                    break
        results.extend(batch)
        if any(row["exitCode"] != 0 for row in batch):
            break

    connectivity = work_subject / "dwi_pipeline" / "sbci_connectome"
    export = output_dir / "files" / "connectivity"
    if connectivity.is_dir():
        export.mkdir(parents=True, exist_ok=True)
        for path in connectivity.iterdir():
            if path.is_file() and path.suffix.lower() in {".npz", ".mat", ".raw", ".tsv"}:
                shutil.copy2(path, export / path.name)
    return results, connectivity


def build_report(
    output_dir: Path,
    status: str,
    mode: str,
    subject: Path | None,
    parameters: dict[str, Any],
    issues: list[dict[str, str]],
    runtime: dict[str, Any],
    stages: list[dict[str, Any]],
    self_test: dict[str, Any],
    input_metadata: dict[str, Any],
    workflow: dict[str, Any],
) -> Path:
    issue_rows = "".join(
        f"<li><b>{html.escape(item.get('code', 'issue'))}</b>: {html.escape(item.get('message', ''))}"
        + (f"<br><code>{html.escape(item['path'])}</code>" if item.get("path") else "") + "</li>"
        for item in issues
    ) or "<li>No blockers detected.</li>"
    stage_rows = "".join(
        f"<tr><td>{html.escape(row['name'])}</td><td>{row['exitCode']}</td><td>{row['elapsedSeconds']}</td><td><code>{html.escape(row['log'])}</code></td></tr>"
        for row in stages
    ) or "<tr><td colspan='4'>No scientific stage executed.</td></tr>"
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>Local SBCI report</title>
<style>body{{font-family:system-ui,Segoe UI,Arial;max-width:1050px;margin:32px auto;color:#18232b}}
.card{{border:1px solid #d8dee4;border-radius:10px;padding:16px;margin:14px 0}}pre{{white-space:pre-wrap;background:#f6f8fa;padding:14px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #d8dee4;padding:7px;text-align:left}}code{{overflow-wrap:anywhere}}</style></head><body>
<h1>Local SBCI pipeline</h1><p><b>Status:</b> {html.escape(status)} · <b>Mode:</b> {html.escape(mode)}</p>
<div class="card"><h2>Meaning</h2><p>This run used, or prepared to use, CyberNeuro's local Ubuntu runtime. No SSH or Slurm connection was used.</p>
<p><b>Input subject:</b> <code>{html.escape(str(subject) if subject else '(not detected)')}</code></p></div>
<div class="card"><h2>Blockers</h2><ul>{issue_rows}</ul></div>
<div class="card"><h2>Stages</h2><table><tr><th>Stage</th><th>Exit</th><th>Seconds</th><th>Log</th></tr>{stage_rows}</table></div>
<details><summary>Scientific parameters</summary><pre>{html.escape(json.dumps(parameters, indent=2))}</pre></details>
<details><summary>Runtime checks</summary><pre>{html.escape(json.dumps(runtime, indent=2))}</pre></details>
<details><summary>Patient-free self-test</summary><pre>{html.escape(json.dumps(self_test, indent=2))}</pre></details>
<details><summary>ADNI input metadata</summary><pre>{html.escape(json.dumps(input_metadata, indent=2))}</pre></details>
<details open><summary>End-to-end ADNI workflow</summary><pre>{html.escape(json.dumps(workflow, indent=2))}</pre></details>
</body></html>"""
    path = output_dir / "reports" / "sbci_local_report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", dest="input_dir", required=True)
    parser.add_argument("--output", dest="output_dir", required=True)
    args = parser.parse_args(argv)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    pipeline_root = Path(__file__).resolve().parents[1]
    for name in ("files", "reports", "logs", "html"):
        (output_dir / name).mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "files" / "sbci_local.template.json", config_template())
    write_json(output_dir / "files" / "sbci_handoff.template.json", handoff_template())

    config, config_path, issues = load_config(input_dir)
    mode = str(config.get("mode") or "inspect")
    parameters, parameter_issues = parameter_values(config)
    issues.extend(parameter_issues)
    workflow_config = config.get("workflow", {})
    if not isinstance(workflow_config, dict):
        issues.append(issue("invalid_workflow", "workflow must be a JSON object", config_path))
        workflow_config = {}
    orchestration_config = workflow_config.get("orchestration", {})
    if not isinstance(orchestration_config, dict):
        issues.append(issue("invalid_orchestration", "workflow.orchestration must be a JSON object", config_path))
        orchestration_config = {}
    orchestration_mode = orchestration_config.get("mode", "manual_shared_artifacts")
    if orchestration_mode not in ORCHESTRATION_MODES:
        issues.append(issue(
            "invalid_orchestration_mode",
            f"workflow.orchestration.mode must be one of: {', '.join(ORCHESTRATION_MODES)}",
            config_path,
        ))
    for key in ("shared_work_dir", "handoff_manifest"):
        if key in orchestration_config and not isinstance(orchestration_config[key], str):
            issues.append(issue("invalid_orchestration", f"workflow.orchestration.{key} must be a string", config_path))
    if "verify_hashes" in orchestration_config and not isinstance(orchestration_config["verify_hashes"], bool):
        issues.append(issue("invalid_orchestration", "workflow.orchestration.verify_hashes must be true or false", config_path))
    if "consume_handoff" in orchestration_config and not isinstance(orchestration_config["consume_handoff"], bool):
        issues.append(issue("invalid_orchestration", "workflow.orchestration.consume_handoff must be true or false", config_path))
    runtime_config = config.get("runtime", {})
    if not isinstance(runtime_config, dict):
        issues.append(issue("invalid_runtime", "runtime must be a JSON object", config_path))
        runtime_config = {}
    requested_backend = runtime_config.get("backend", "auto")
    if requested_backend not in {"auto", "container", "wsl"}:
        issues.append(issue("invalid_runtime", "runtime.backend must be auto, container, or wsl", config_path))
    container_image = runtime_config.get("container_image")
    if "container_image" in runtime_config and (not isinstance(container_image, str) or not container_image.strip()):
        issues.append(issue("invalid_runtime", "runtime.container_image must be a non-empty string", config_path))
    for key in ("input_profile", "synb0_image", "freesurfer_license", "reference_scripts_dir", "preprocessing_config"):
        if key in workflow_config and not isinstance(workflow_config[key], str):
            issues.append(issue("invalid_workflow", f"workflow.{key} must be a string", config_path))
    requested_profile = workflow_config.get("input_profile", "auto")
    if not isinstance(requested_profile, str):
        requested_profile = "auto"
    if requested_profile not in INPUT_PROFILES:
        issues.append(issue(
            "invalid_input_profile",
            f"workflow.input_profile must be one of: {', '.join(INPUT_PROFILES)}",
            config_path,
        ))
        requested_profile = "auto"
    normalized_profile = INPUT_PROFILE_ALIASES.get(requested_profile, requested_profile)
    subjects = [] if mode == "self_test" else find_subjects(input_dir, requested_profile)
    subject = subjects[0] if len(subjects) == 1 else None
    active_subject = effective_work_subject(subject, output_dir) if subject is not None else None
    inventory = inventory_subject(active_subject) if active_subject is not None else None
    preprocessing_required = mode != "self_test" and not bool(
        inventory and (inventory.get("sbciReady") or inventory.get("completedConnectivity"))
    )
    run_psc = bool(workflow_config.get("run_psc", False))
    require_distortion_correction = bool(workflow_config.get("require_distortion_correction", True))
    # Declared upstream correction: the correction stages copy instead of running
    # Synb0/topup/eddy, so their runtime and metadata are not required.
    distortion_corrected_input = workflow_config.get("distortion_corrected_input") is True
    upstream_runtime = probe_commands(
        run_psc=run_psc,
        require_distortion_correction=require_distortion_correction and not distortion_corrected_input,
        preprocessing_required=preprocessing_required,
        freesurfer_license=workflow_config.get("freesurfer_license") if isinstance(workflow_config.get("freesurfer_license"), str) else None,
        synb0_image=workflow_config.get("synb0_image") if isinstance(workflow_config.get("synb0_image"), str) else None,
    )
    workflow: dict[str, Any] = {
        "inventory": inventory,
        "plan": None,
        "runtime": upstream_runtime,
        "inputRoute": {
            "requestedProfile": requested_profile,
            "normalizedProfile": normalized_profile,
            "resolvedProfile": resolved_input_profile(normalized_profile, subject, inventory),
            "preprocessingRequired": preprocessing_required,
            "selectedSubject": str(subject) if subject else None,
            "existingProductsValidatedBeforeRun": bool(inventory and inventory.get("sbciReady")),
        },
        "execution": None,
        "executor": {
            "implementedStages": list(STAGE_ORDER),
            "requested": mode == "preprocess",
            "requiresMode": "preprocess",
            "requiresApproval": True,
            "nextStage": None,
            "eligible": False,
        },
        "orchestration": None,
    }
    subject_issues: list[dict[str, str]] = []
    if mode != "self_test":
        if not subjects:
            issues.append(issue("missing_subject", "Select a folder containing one raw-BIDS, partially preprocessed, or SBCI-ready subject/session", input_dir))
        elif len(subjects) > 1:
            issues.append(issue("multiple_subjects", "Local mode currently runs one subject per CyberNeuro input batch", input_dir))
        if subject is not None and active_subject is not None and inventory is not None:
            for key in ("run_psc", "resume", "require_distortion_correction", "distortion_corrected_input"):
                if key in workflow_config and not isinstance(workflow_config[key], bool):
                    issues.append(issue("invalid_workflow", f"workflow.{key} must be true or false", config_path))
            workflow["plan"] = plan_workflow(
                inventory,
                run_psc=run_psc,
                resume=bool(workflow_config.get("resume", True)),
                require_distortion_correction=require_distortion_correction,
                distortion_corrected_input=distortion_corrected_input,
                command_paths=workflow["runtime"]["commands"],
                synb0_configured=synb0_configured(workflow["runtime"]),
            )
            for message in profile_mismatches(workflow_config, inventory, workflow["plan"]):
                workflow["plan"]["blockers"].append({"stage": "input", "message": message, "code": "profile_mismatch"})
                workflow["plan"]["readyToRun"] = False
                issues.append(issue("profile_mismatch", message, config_path))
            registration = next((row for row in workflow["plan"]["stages"] if row["id"] == "t1_dwi_registration"), None)
            message = template_problem() if registration and registration.get("state") != "complete" else None
            if message:
                workflow["plan"]["blockers"].append({"stage": "t1_dwi_registration", "message": message, "code": "missing_template"})
                workflow["plan"]["readyToRun"] = False
                issues.append(issue("missing_template", message))
            next_stage = next(
                (row for row in workflow["plan"]["stages"] if row.get("state") != "complete"),
                None,
            )
            workflow["executor"]["nextStage"] = next_stage
            workflow["executor"]["eligible"] = bool(next_stage and next_stage.get("state") == "pending")
            workflow["executor"]["sourceSubject"] = str(subject)
            workflow["executor"]["workSubject"] = str(active_subject) if active_subject != subject else None
            if inventory["sbciReady"]:
                subject_issues = inspect_subject(active_subject)
                issues.extend(subject_issues)
            elif mode in {"quick_test", "production"}:
                if inventory.get("completedConnectivity"):
                    issues.append(issue(
                        "completed_connectivity_read_only",
                        "This selection contains completed SBCI matrices but not rerunnable preprocessing inputs. Inspect or analyze it; do not rerun preprocessing/SBCI.",
                        subject,
                    ))
                else:
                    issues.append(issue(
                        "upstream_preprocessing_required",
                        "This subject is not SBCI-ready. Review workflow.plan and workflow.orchestration for the next producer skill or local stage.",
                        subject,
                    ))
            workflow["orchestration"] = build_orchestration_plan(
                subject,
                inventory,
                workflow["plan"],
                orchestration_config,
            )
            write_json(output_dir / "files" / "orchestration_plan.json", workflow["orchestration"])
    runtime = runtime_probe(pipeline_root)
    runtime_python = Path(
        runtime.get("python")
        or os.environ.get("SBCI_CORE_PY")
        or RUNTIME_PYTHON_DEFAULT
    )
    runtime["launcher"] = {
        "backend": os.environ.get("SBCI_EXECUTION_BACKEND_ACTIVE") or "linux-local",
        "requestedBackend": requested_backend,
        "containerImageReference": os.environ.get("SBCI_CONTAINER_IMAGE_REF"),
        "containerImageId": os.environ.get("SBCI_CONTAINER_IMAGE_ID"),
    }
    if not runtime.get("ready"):
        issues.append(issue("missing_local_runtime", "The local SBCI Python/runtime bundle is not installed or incomplete"))
    if mode in {"preprocess", "production"} and config.get("approved") is not True:
        issues.append(issue("approval_required", f"{mode} mode requires approved=true after reviewing the displayed workflow and paths"))

    stages: list[dict[str, Any]] = []
    self_test: dict[str, Any] = {"ready": False, "message": "Runtime self-test was not requested."}
    input_metadata: dict[str, Any] = {"ready": False, "message": "No complete subject was available for metadata checks."}
    sbci_ready = bool(workflow.get("inventory") and workflow["inventory"].get("sbciReady"))
    if subject is not None and sbci_ready and not subject_issues and not parameter_issues and runtime.get("ready"):
        input_metadata = run_subject_metadata_probe(
            pipeline_root,
            active_subject,
            output_dir,
            runtime_python,
            parameters.get("rois", []),
        )
        if not input_metadata.get("ready"):
            issues.append(
                issue(
                    "subject_metadata_failed",
                    input_metadata.get("message", "ADNI/SBCI metadata preflight failed"),
                    Path(input_metadata["log"]),
                )
            )
    validation: dict[str, Any] = {"ready": False, "message": "No scientific run was requested."}
    completed_connectivity = bool(
        workflow.get("inventory") and workflow["inventory"].get("completedConnectivity")
    )
    if subject is not None and completed_connectivity and mode == "inspect" and runtime.get("ready"):
        completed_root = Path(str(workflow["inventory"]["completedConnectivityRoot"]))
        validation = validate_completed_connectivity(
            completed_root / "sbci_connectome",
            parameters,
            runtime_python,
        )
        workflow["completedConnectivityValidation"] = validation
        if not validation.get("ready"):
            issues.append(issue(
                "completed_connectivity_validation_failed",
                validation.get("message", "Completed connectivity validation failed."),
                completed_root / "sbci_connectome",
            ))
    preprocessing: dict[str, Any] = {"state": "not_requested", "message": "Preprocessing execution was not requested."}
    status = "documented_blocked_local_preflight" if issues else "completed_local_surrogate"
    completed = False
    scientific = False
    if not issues and mode == "self_test":
        self_test = run_runtime_self_test(
            pipeline_root,
            output_dir,
            runtime_python,
        )
        if self_test.get("ready"):
            status = "completed_local_runtime_self_test"
            completed = True
        else:
            status = "failed_runtime_self_test"
            issues.append(
                issue(
                    "runtime_self_test_failed",
                    self_test.get("message", "Runtime self-test failed"),
                    Path(self_test["log"]),
                )
            )
    elif not issues and mode in {"quick_test", "production"} and subject is not None:
        stages, connectivity = execute_local(pipeline_root, active_subject, output_dir, parameters, runtime_python)
        failed = next((row for row in stages if row["exitCode"] != 0), None)
        if failed:
            status = "failed_local_stage"
            issues.append(issue("stage_failed", f"{failed['name']} exited with code {failed['exitCode']}", Path(failed["log"])))
        else:
            validation = validate_connectivity(
                connectivity,
                parameters,
                runtime_python,
            )
            if not validation.get("ready"):
                status = "failed_output_validation"
                issues.append(issue("output_validation_failed", validation["message"], connectivity))
            elif mode == "quick_test":
                status = "completed_local_smoke_not_scientific"
                completed = True
            else:
                status = "completed_official_runtime"
                completed = True
                scientific = True
    elif not issues and mode == "preprocess" and subject is not None:
        plan_rows = (workflow.get("plan") or {}).get("stages") or []
        next_stage = next((row for row in plan_rows if row.get("state") != "complete"), None)
        consume_requested = orchestration_config.get("consume_handoff") is True
        orchestration_plan = workflow.get("orchestration") or {}
        handoff_validation = orchestration_plan.get("handoffValidation") or {}
        if consume_requested:
            stage_id = str(next_stage.get("id")) if next_stage is not None else ""
            manifest_value = orchestration_plan.get("handoffManifest")
            if next_stage is None:
                status = "documented_blocked_local_preflight"
                issues.append(issue(
                    "handoff_not_needed",
                    "No incomplete workflow stage remains, so the supplied handoff was not consumed.",
                    subject,
                ))
                preprocessing = {"state": "blocked", "stage": None, "message": issues[-1]["message"]}
            elif not manifest_value or not handoff_validation.get("ready"):
                status = "documented_blocked_local_preflight"
                issues.append(issue(
                    "handoff_not_ready",
                    "Handoff consumption was requested, but the manifest is missing, invalid, or incompatible with the next stage.",
                    Path(str(manifest_value)) if manifest_value else subject,
                ))
                preprocessing = {
                    "state": "blocked",
                    "stage": stage_id,
                    "handoffValidation": handoff_validation,
                }
            else:
                try:
                    shared_value = orchestration_plan.get("sharedWorkDir")
                    preprocessing = consume_handoff(
                        Path(str(manifest_value)),
                        subject,
                        output_dir,
                        shared_work_dir=Path(str(shared_value)) if shared_value else None,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    status = "failed_preprocessing_stage"
                    issues.append(issue("handoff_consumption_failed", str(exc), Path(str(manifest_value))))
                    preprocessing = {"state": "failed_handoff", "stage": stage_id, "message": str(exc)}
                else:
                    # A validated handoff is the single completed stage for this
                    # invocation. Re-plan from the copied work tree, but do not
                    # start the newly eligible next stage automatically.
                    active_subject = effective_work_subject(subject, output_dir)
                    inventory = inventory_subject(active_subject)
                    workflow["inventory"] = inventory
                    workflow["inputRoute"]["preprocessingRequired"] = not bool(
                        inventory.get("sbciReady") or inventory.get("completedConnectivity")
                    )
                    workflow["plan"] = plan_workflow(
                        inventory,
                        run_psc=run_psc,
                        resume=bool(workflow_config.get("resume", True)),
                        require_distortion_correction=require_distortion_correction,
                        distortion_corrected_input=distortion_corrected_input,
                        command_paths=workflow["runtime"]["commands"],
                        synb0_configured=synb0_configured(workflow["runtime"]),
                    )
                    next_after_handoff = next(
                        (row for row in workflow["plan"]["stages"] if row.get("state") != "complete"),
                        None,
                    )
                    workflow["executor"]["nextStage"] = next_after_handoff
                    workflow["executor"]["eligible"] = bool(
                        next_after_handoff and next_after_handoff.get("state") == "pending"
                    )
                    workflow["executor"]["workSubject"] = str(active_subject)
                    next_orchestration_config = dict(orchestration_config)
                    next_orchestration_config["handoff_manifest"] = ""
                    next_orchestration_config["consume_handoff"] = False
                    next_orchestration = build_orchestration_plan(
                        subject,
                        inventory,
                        workflow["plan"],
                        next_orchestration_config,
                    )
                    next_orchestration["consumption"] = {
                        "previousPlan": orchestration_plan,
                        "result": preprocessing,
                    }
                    workflow["orchestration"] = next_orchestration
                    write_json(output_dir / "files" / "orchestration_plan.json", next_orchestration)
                    workflow["execution"] = preprocessing
                    status = "completed_preprocessing_stage"
        elif next_stage is None:
            status = "completed_preprocessing_trunk"
            preprocessing = {
                "state": "trunk_complete",
                "message": "All shared preprocessing stages are already complete; SBCI/optional PSC can run next.",
            }
        elif next_stage.get("state") == "blocked":
            status = "documented_blocked_local_preflight"
            issues.append(issue(
                "preprocessing_runtime_blocked",
                f"{next_stage.get('id')} cannot start until its displayed commands and user-managed assets are available.",
                subject,
            ))
            preprocessing = {"state": "blocked", "nextStage": next_stage}
        else:
            stage_id = str(next_stage.get("id"))
            if stage_id in REFERENCE_SCRIPTS and not profile_path(workflow_config.get("preprocessing_config")):
                status = "documented_blocked_local_preflight"
                issues.append(issue(
                    "stage_adapter_configuration_required",
                    f"{stage_id} requires workflow.preprocessing_config: a dataset profile such as "
                    "datasets/adni/profile.conf.",
                    subject,
                ))
                preprocessing = {"state": "blocked", "nextStage": next_stage}
            elif stage_id == "prepare_data":
                preprocessing = execute_prepare_data(
                    subject,
                    output_dir,
                    workflow["runtime"]["commands"],
                    resume=bool(workflow_config.get("resume", True)),
                )
            elif stage_id == "sbci" and workflow_config.get("stages") == "preprocess":
                preprocessing = {"stage": "sbci", "state": "not_requested",
                                 "message": "workflow.stages is preprocess: preprocessing is complete; SBCI was not requested."}
            elif stage_id == "sbci":
                structural, functional = requested_sbci_steps(workflow_config)
                stages, connectivity = execute_local(pipeline_root, active_subject, output_dir, parameters, runtime_python,
                                                     functional=functional, structural=structural)
                failed = next((row for row in stages if row["exitCode"] != 0), None)
                if failed:
                    preprocessing = {"stage": "sbci", "state": "failed_command", "failedStage": failed}
                else:
                    validation = validate_connectivity(
                        connectivity, parameters,
                        runtime_python, structural=structural, functional=functional,
                    )
                    preprocessing = {
                        "stage": "sbci",
                        "state": "complete" if validation.get("ready") else "failed_validation",
                        "validation": validation,
                    }
                    scientific = bool(validation.get("ready"))
                    completed = scientific
            else:
                preprocessing = execute_workflow_stage_safely(
                    stage_id,
                    subject,
                    output_dir,
                    workflow["runtime"]["commands"],
                    workflow_config,
                    resume=bool(workflow_config.get("resume", True)),
                    parameters=parameters,
                )
            workflow["execution"] = preprocessing
            if preprocessing.get("state") in {"complete", "skipped_complete"}:
                status = "completed_official_runtime" if stage_id == "sbci" and scientific else "completed_preprocessing_stage"
                # Re-derive the plan from disk. Without this the written
                # orchestration_plan.json still names the stage that just
                # finished as nextStage; the handoff branch below already
                # refreshes it, the ordinary stage path did not.
                refreshed_subject = effective_work_subject(subject, output_dir)
                refreshed_inventory = inventory_subject(refreshed_subject)
                workflow["inventory"] = refreshed_inventory
                workflow["plan"] = plan_workflow(
                    refreshed_inventory,
                    run_psc=run_psc,
                    resume=bool(workflow_config.get("resume", True)),
                    require_distortion_correction=require_distortion_correction,
                    distortion_corrected_input=distortion_corrected_input,
                    command_paths=workflow["runtime"]["commands"],
                    synb0_configured=synb0_configured(workflow["runtime"]),
                )
                workflow["orchestration"] = build_orchestration_plan(
                    subject,
                    refreshed_inventory,
                    workflow["plan"],
                    orchestration_config,
                )
                write_json(
                    output_dir / "files" / "orchestration_plan.json",
                    workflow["orchestration"],
                )
            else:
                status = "failed_preprocessing_stage"
                # A pre-flight block carries its reason in "message"; without it
                # the manifest would say only "ended in state blocked".
                reason = f": {preprocessing['message']}" if preprocessing.get("message") else ""
                issues.append(issue(
                    "preprocessing_stage_failed",
                    f"{stage_id} ended in state {preprocessing.get('state')}{reason}",
                    Path(preprocessing.get("workSubject") or subject),
                ))
    elif not issues and mode == "inspect":
        if completed_connectivity:
            status = "completed_connectivity_validated"
        else:
            status = "completed_local_surrogate" if sbci_ready else "completed_local_workflow_plan"

    report = build_report(
        output_dir,
        status,
        mode,
        subject,
        parameters,
        issues,
        runtime,
        stages,
        self_test,
        input_metadata,
        workflow,
    )
    manifest = {
        "status": status,
        "completed": completed,
        "inspectionOnly": mode == "inspect",
        "scientificAlgorithmRan": bool(stages),
        "preprocessingStageRan": preprocessing.get("state") == "complete" and preprocessing.get("stage") != "sbci",
        "workflowStageRan": preprocessing.get("state") == "complete",
        "officialScientificPipelineRan": scientific,
        "runtimeSelfTestRan": mode == "self_test" and "exitCode" in self_test,
        "pipelineName": PIPELINE_NAME,
        "inputBatch": os.environ.get("CYBERNEURO_INPUT_BATCH") or input_dir.name,
        "input": str(input_dir),
        "output": str(output_dir),
        "configFile": str(config_path) if config_path else None,
        "mode": mode,
        "parameters": parameters,
        "runtime": runtime,
        "issues": issues,
        "stages": stages,
        "validation": validation,
        "preprocessing": preprocessing,
        "selfTest": self_test,
        "inputMetadata": input_metadata,
        "workflow": workflow,
        "report": str(report.relative_to(output_dir)).replace("\\", "/"),
        "message": (
            "Local SBCI container execution; no SSH, Slurm, or runtime network was used."
            if runtime["launcher"]["backend"] == "container"
            else "Local SBCI execution; no SSH or Slurm connection was used."
        ),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_json(output_dir / "local_adapter_manifest.json", manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0 if status not in {"failed_local_stage", "failed_output_validation", "failed_runtime_self_test", "failed_preprocessing_stage"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
