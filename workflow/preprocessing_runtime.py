#!/usr/bin/env python3
"""Inspect and plan CyberNeuro's local preprocessing runtime for SBCI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable


FSL_CHANNEL = "https://fsl.fmrib.ox.ac.uk/fsldownloads/fslconda/public/"
SYNB0_DEFAULT_IMAGE = "leonyichencai/synb0-disco:v3.1"

LEGACY_COMMAND_MAP = {
    "scil_convert_gradient_fsl_to_mrtrix.py": "scil_gradients_convert",
    "scil_flip_grad.py": "scil_gradients_modify_axes",
    "scil_compute_dti_metrics.py": "scil_dti_metrics",
    "scil_compute_ssst_frf.py": "scil_frf_ssst",
    "scil_compute_fodf.py": "scil_fodf_ssst",
    "scil_compute_pft_dipy.py": "scil_tracking_pft",
}

COMPONENTS: dict[str, dict[str, Any]] = {
    "fsl": {
        "label": "FSL CPU preprocessing",
        "commands": ("topup", "applytopup", "eddy", "bet", "fast", "flirt", "fugue", "fslmaths", "fslroi", "fslmerge"),
        "packages": ("fsl-topup", "fsl-eddy", "fsl-bet2", "fsl-fast4", "fsl-flirt", "fsl-fugue", "fsl-avwutils"),
        "channels": (FSL_CHANNEL, "conda-forge"),
        "prefix": "~/.cyberneuro/envs/sbci-fsl",
        "installable": True,
    },
    "ants": {
        "label": "ANTs registration",
        "commands": ("N4BiasFieldCorrection", "antsBrainExtraction.sh", "antsRegistration", "antsApplyTransforms"),
        "packages": ("ants",),
        "channels": ("conda-forge",),
        "prefix": "~/.cyberneuro/envs/ants",
        "installable": True,
    },
    "mrtrix3": {
        "label": "MRtrix3 diffusion tools",
        "commands": ("mrconvert", "mrcalc", "dwinormalise", "mrthreshold"),
        "packages": ("mrtrix3",),
        "channels": ("conda-forge", "bioconda"),
        "prefix": "~/.cyberneuro/envs/sbci-mrtrix",
        "installable": True,
    },
    "freesurfer": {
        "label": "FreeSurfer and FSFast",
        "commands": (
            "recon-all", "mri_convert", "preproc-sess", "fcseed-config",
            "fcseed-sess", "mkanalysis-sess", "selxavg3-sess",
        ),
        "packages": (),
        "channels": (),
        "prefix": "$FREESURFER_HOME",
        "installable": False,
    },
    "scilpy_preprocessing": {
        "label": "SBCI-compatible scilpy preprocessing commands",
        "commands": (
            "scil_gradients_convert", "scil_gradients_modify_axes",
            "scil_dwi_extract_b0", "scil_dwi_apply_bias_field", "scil_volume_crop",
            "scil_volume_resample", "scil_denoising_nlmeans", "scil_dwi_extract_shell",
            "scil_dti_metrics", "scil_frf_ssst", "scil_fodf_ssst", "scil_frf_msmt", "scil_fodf_msmt",
            "scil_fodf_max_in_ventricles", "scil_fodf_metrics",
        ),
        "packages": (),
        "channels": (),
        "prefix": "$SBCI_ENV",
        "installable": False,
    },
    "matlab_fsfast": {
        "label": "MATLAB for FSFast selxavg3 (or a complete FreeSurfer MCR)",
        "commands": ("matlab",),
        "packages": (),
        "channels": (),
        "prefix": "",
        "installable": False,
    },
    "psc": {
        "label": "Optional legacy PSC branch",
        "commands": (
            "scil_compute_maps_for_particle_filter_tracking.py", "seeding_map_dilation.py",
            "scil_count_non_zero_voxels.py", "scil_compute_pft_dipy.py",
            "scil_remove_invalid_coordinates_from_streamlines.py",
            "extraction_sccm_withfeatures_cortical.py", "extraction_sccm_withfeatures_subcortical.py",
        ),
        "packages": (),
        "channels": (),
        "prefix": "user-supplied PSC environment",
        "installable": False,
    },
}


def _split_paths(value: str | None) -> list[Path]:
    return [Path(item).expanduser() for item in (value or "").split(os.pathsep) if item]


def candidate_bin_dirs(extra: Iterable[Path] = ()) -> list[Path]:
    """Return known CyberNeuro bin locations in deterministic priority order."""
    # Tools installed one environment each under this prefix are found without activating them.
    root = Path(os.environ.get("CYBERNEURO_HOME") or Path.home() / ".cyberneuro").expanduser()
    values = [
        *_split_paths(os.environ.get("CYBERNEURO_PREPROCESS_BIN")),
        *extra,
        root / "bin",
        root / "envs" / "sbci-fsl" / "bin",
        root / "envs" / "ants" / "bin",
        root / "envs" / "sbci-mrtrix" / "bin",
        root / "envs" / "medical" / "bin",
    ]
    # The image's SBCI Python environment (scilpy), after everything on PATH.
    if os.environ.get("SBCI_CORE_PY"):
        values.append(Path(os.environ["SBCI_CORE_PY"]).expanduser().parent)
    freesurfer_home = os.environ.get("FREESURFER_HOME")
    if freesurfer_home:
        fs_home = Path(freesurfer_home).expanduser()
        values.extend((fs_home / "bin", fs_home / "fsfast" / "bin"))
    unique: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def resolve_command(command: str, bins: Iterable[Path], path_value: str | None = None) -> str | None:
    resolved = shutil.which(command, path=path_value)
    if resolved:
        return resolved
    suffixes = ("", ".exe", ".bat", ".cmd") if os.name == "nt" else ("",)
    for folder in bins:
        for suffix in suffixes:
            candidate = folder / f"{command}{suffix}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def resolve_freesurfer_license(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("FS_LICENSE"):
        candidates.append(Path(os.environ["FS_LICENSE"]).expanduser())
    if os.environ.get("FREESURFER_HOME"):
        candidates.append(Path(os.environ["FREESURFER_HOME"]).expanduser() / "license.txt")
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def inspect_preprocessing_runtime(
    *,
    run_psc: bool = False,
    require_distortion_correction: bool = True,
    preprocessing_required: bool = True,
    freesurfer_license: str | Path | None = None,
    synb0_image: str | None = None,
    extra_bin_dirs: Iterable[Path] = (),
    path_value: str | None = None,
) -> dict[str, Any]:
    """Return installed, installable, and user-action runtime states without mutation."""
    bins = candidate_bin_dirs(extra_bin_dirs)
    path_value = os.environ.get("PATH", "") if path_value is None else path_value
    all_commands = sorted({command for component in COMPONENTS.values() for command in component["commands"]})
    all_commands.extend(command for command in ("apptainer", "docker", "synb0") if command not in all_commands)
    commands = {command: resolve_command(command, bins, path_value) for command in all_commands}
    license_path = resolve_freesurfer_license(freesurfer_license)

    required_ids = ["fsl", "ants", "mrtrix3", "freesurfer", "scilpy_preprocessing"] if preprocessing_required else []
    if preprocessing_required and run_psc:
        required_ids.append("psc")
    rows: dict[str, dict[str, Any]] = {}
    installable: list[str] = []
    blocked_user_input: list[dict[str, str]] = []
    for component_id, spec in COMPONENTS.items():
        missing = [command for command in spec["commands"] if not commands.get(command)]
        required = component_id in required_ids
        if not missing and component_id == "freesurfer" and license_path is None:
            status = "user_action"
            message = "FreeSurfer commands were found, but a readable license file was not configured."
        elif not missing:
            status = "installed"
            message = "All required commands were found."
        elif spec["installable"]:
            status = "installable"
            message = "CyberNeuro can provision this isolated runtime after user confirmation."
            if required:
                installable.append(component_id)
        elif component_id == "psc" and not run_psc:
            status = "optional_not_selected"
            message = "PSC is optional and is not required by SBCI."
        else:
            status = "user_action"
            message = "This component requires a compatible user-supplied installation or adapter."
        row = {
            "id": component_id,
            "label": spec["label"],
            "required": required,
            "status": status,
            "commands": {command: commands.get(command) for command in spec["commands"]},
            "missingCommands": missing,
            "installable": spec["installable"],
            "packages": list(spec["packages"]),
            "channels": list(spec["channels"]),
            "prefix": spec["prefix"],
            "message": message,
        }
        rows[component_id] = row
        if required and status == "user_action":
            action = message
            if component_id == "freesurfer":
                action = "Install FreeSurfer/FSFast and select its non-empty license.txt file."
            elif component_id == "scilpy_preprocessing":
                action = "Provide the exact legacy scilpy environment required by the approved ADNI stage scripts."
            elif component_id == "psc":
                action = "Provide a separately validated PSC Pipeline environment, or leave workflow.run_psc=false."
            blocked_user_input.append({"component": component_id, "message": action})

    synb0_value = (synb0_image or "").strip()
    container_runner = commands.get("apptainer") or commands.get("docker")
    synb0_required = preprocessing_required and require_distortion_correction
    if not preprocessing_required:
        synb0_status = "not_required"
        synb0_message = "The selected subject already contains SBCI-ready preprocessing products."
    elif not require_distortion_correction:
        synb0_status = "not_required"
        synb0_message = "Distortion correction was explicitly disabled; the manifest must label that exception."
    elif commands.get("synb0"):
        synb0_status = "configured"
        synb0_message = "The image's built-in Synb0-DISCO is used; no Synb0 image or container runner is needed."
    elif not synb0_value:
        synb0_status = "user_action"
        synb0_message = f"Select a Synb0-DISCO SIF or approve the container image {SYNB0_DEFAULT_IMAGE}."
    elif not container_runner:
        synb0_status = "user_action"
        synb0_message = "Configure Docker or Apptainer in the CyberNeuro Linux sandbox."
    else:
        synb0_status = "configured"
        synb0_message = "A container runner and Synb0-DISCO image reference are configured; execution must still verify the image."
    synb0 = {
        "id": "synb0",
        "label": "Synb0-DISCO distortion correction",
        "required": synb0_required,
        "status": synb0_status,
        "image": synb0_value or None,
        "defaultImage": SYNB0_DEFAULT_IMAGE,
        "runner": commands.get("synb0") or container_runner,
        "message": synb0_message,
    }
    if synb0_required and synb0_status == "user_action":
        blocked_user_input.append({"component": "synb0", "message": synb0_message})

    required_ready = all(rows[item]["status"] == "installed" for item in required_ids)
    ready = required_ready and (not synb0_required or synb0_status == "configured")
    return {
        "ready": ready,
        "preprocessingRequired": preprocessing_required,
        "inspectionOnly": True,
        "commands": commands,
        "missing": sorted(command for command, value in commands.items() if not value),
        "candidateBinDirs": [str(path) for path in bins],
        "components": rows,
        "legacyCommandMap": LEGACY_COMMAND_MAP,
        "synb0": synb0,
        "freesurferLicense": str(license_path) if license_path else None,
        "installableComponents": installable,
        "blockedUserInput": blocked_user_input,
        "note": "Inspection never downloads packages, starts containers, or changes the host.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-psc", action="store_true")
    parser.add_argument("--allow-uncorrected", action="store_true")
    parser.add_argument("--prepared-input", action="store_true", help="Report requirements for an already SBCI-ready subject")
    parser.add_argument("--freesurfer-license")
    parser.add_argument("--synb0-image")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    result = inspect_preprocessing_runtime(
        run_psc=args.run_psc,
        require_distortion_correction=not args.allow_uncorrected,
        preprocessing_required=not args.prepared_input,
        freesurfer_license=args.freesurfer_license,
        synb0_image=args.synb0_image,
    )
    print(json.dumps(result, indent=2 if args.pretty else None, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
