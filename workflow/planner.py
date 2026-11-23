#!/usr/bin/env python3
"""Inventory and plan an end-to-end ADNI preprocessing/PSC/SBCI workflow."""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path
from typing import Any

from workflow.preprocessing_runtime import inspect_preprocessing_runtime


STAGE_ORDER = (
    "dwi_correction",
    "fmri_correction",
    "prepare_data",
    "t1_dwi_registration",
    "freesurfer",
    "fodf_tissue",
    "fsfast",
    "psc",
    "sbci",
)

STAGE_LABELS = {
    "dwi_correction": "Correct DWI distortion, eddy currents, and motion",
    "fmri_correction": "Correct BOLD susceptibility distortion",
    "prepare_data": "Reorient images and gradients",
    "t1_dwi_registration": "Register T1 to DWI and calculate diffusion tensors",
    "freesurfer": "Reconstruct cortical surfaces with FreeSurfer",
    "fodf_tissue": "Create tissue maps and estimate fODF",
    "fsfast": "Clean and project resting-state fMRI with FSFast",
    "psc": "Create the optional PSC atlas connectome",
    "sbci": "Create SBCI surface structural and functional connectivity",
}

STAGE_COMMANDS = {
    "dwi_correction": ("apptainer", "topup", "eddy", "bet", "fslroi", "fslmerge", "fslmaths"),
    "fmri_correction": ("bet", "flirt", "fugue", "fslmaths", "fslmerge", "topup", "applytopup"),
    "prepare_data": ("mrconvert", "scil_gradients_convert", "scil_gradients_modify_axes"),
    "t1_dwi_registration": (
        "bet", "mrcalc", "dwinormalise", "mrthreshold", "fslmaths",
        "N4BiasFieldCorrection", "antsBrainExtraction.sh", "antsRegistration", "antsApplyTransforms",
        "scil_dwi_extract_b0", "scil_dwi_apply_bias_field", "scil_volume_crop",
        "scil_volume_resample", "scil_denoising_nlmeans", "scil_dwi_extract_shell",
        "scil_dti_metrics",
    ),
    "freesurfer": ("recon-all", "mri_convert", "mrconvert", "antsApplyTransforms"),
    "fodf_tissue": (
        "fast", "scil_frf_ssst", "scil_fodf_ssst", "scil_frf_msmt", "scil_fodf_msmt",
        "scil_fodf_max_in_ventricles", "scil_fodf_metrics",
    ),
    "fsfast": ("preproc-sess", "fcseed-config", "fcseed-sess", "mkanalysis-sess", "selxavg3-sess",
               "matlab"),
    "psc": (
        "scil_compute_maps_for_particle_filter_tracking.py", "seeding_map_dilation.py",
        "scil_count_non_zero_voxels.py", "scil_compute_pft_dipy.py",
        "scil_remove_invalid_coordinates_from_streamlines.py",
        "extraction_sccm_withfeatures_cortical.py", "extraction_sccm_withfeatures_subcortical.py",
    ),
    "sbci": (),
}


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _first(root: Path, patterns: tuple[str, ...]) -> Path | None:
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    return None


def _nifti_base(path: Path | None) -> Path | None:
    if path is None:
        return None
    name = path.name
    if name.endswith(".nii.gz"):
        name = name[:-7]
    elif name.endswith(".nii"):
        name = name[:-4]
    return path.with_name(name)


def _sidecar(path: Path | None, suffix: str) -> Path | None:
    base = _nifti_base(path)
    candidate = base.with_suffix(suffix) if base is not None else None
    return candidate if candidate is not None and candidate.is_file() else None


def _read_json(path: Path | None) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    return (value, None) if isinstance(value, dict) else ({}, "JSON root must be an object")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize_metadata(metadata: dict[str, Any], *, modality: str) -> dict[str, Any]:
    phase_encoding = metadata.get("PhaseEncodingDirection") or metadata.get("PhaseEncodingAxis")
    total_readout = _number(metadata.get("TotalReadoutTime"))
    if total_readout is None:
        total_readout = _number(metadata.get("EstimatedTotalReadoutTime"))
    effective_echo_spacing = _number(metadata.get("EffectiveEchoSpacing"))
    if effective_echo_spacing is None:
        effective_echo_spacing = _number(metadata.get("EstimatedEffectiveEchoSpacing"))
    if total_readout is None and effective_echo_spacing is not None:
        matrix_pe = metadata.get("ReconMatrixPE") or metadata.get("AcquisitionMatrixPE")
        if isinstance(matrix_pe, int) and not isinstance(matrix_pe, bool) and matrix_pe > 1:
            total_readout = effective_echo_spacing * (matrix_pe - 1)
    return {
        "modality": modality,
        "manufacturer": metadata.get("Manufacturer"),
        "manufacturersModelName": metadata.get("ManufacturersModelName"),
        "phaseEncodingDirection": phase_encoding,
        "totalReadoutTime": total_readout,
        "effectiveEchoSpacing": effective_echo_spacing,
        "repetitionTime": _number(metadata.get("RepetitionTime")),
        "echoTime": _number(metadata.get("EchoTime")),
        "sliceTimingPresent": isinstance(metadata.get("SliceTiming"), list) and bool(metadata.get("SliceTiming")),
    }


def _nifti1_header(path: Path | None) -> tuple[str, bytes] | None:
    """(struct endianness, 348-byte header) of a NIfTI-1 file, or None if it is not one."""
    if path is None:
        return None
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rb") as stream:
            header = stream.read(348)
    except (OSError, EOFError):
        return None
    if len(header) < 348:
        return None
    endian = next((e for e in "<>" if struct.unpack(e + "i", header[:4])[0] == 348), None)
    return (endian, header) if endian else None


def nifti_shape(path: Path | None) -> tuple[int, int, int] | None:
    """The spatial grid (dim[1:4]) of a NIfTI-1 image, or None if the header is unreadable."""
    parsed = _nifti1_header(path)
    if parsed is None:
        return None
    endian, header = parsed
    dims = struct.unpack(endian + "8h", header[40:56])
    return dims[1], dims[2], dims[3]


def nifti_volume_count(path: Path | None) -> int | None:
    """Volumes in a NIfTI-1 image (dim[4], 1 for 3-D), or None if the header is unreadable."""
    parsed = _nifti1_header(path)
    if parsed is None:
        return None
    endian, header = parsed
    dims = struct.unpack(endian + "8h", header[40:56])
    return max(dims[4], 1) if dims[0] >= 4 else 1


def _nifti_tr_seconds(path: Path | None) -> float | None:
    """The subject's own TR from a NIfTI-1 header (pixdim[4]).

    Used only when no BIDS JSON gives RepetitionTime (e.g. the lab's HCP trees), and
    only when the header declares its time unit -- an unlabelled value is not guessed at.
    """
    parsed = _nifti1_header(path)
    if parsed is None:
        return None
    endian, header = parsed
    tr = struct.unpack(endian + "f", header[92:96])[0]
    scale = {8: 1.0, 16: 1e-3, 24: 1e-6}.get(header[123] & 0x38)
    return tr * scale if scale and tr > 0 else None


def _gradient_counts(bval: Path | None, bvec: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {"bvals": None, "bvecs": None, "consistent": False}
    try:
        if bval is not None:
            result["bvals"] = len(bval.read_text(encoding="utf-8-sig").split())
        if bvec is not None:
            rows = [line.split() for line in bvec.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            if len(rows) == 3 and len({len(row) for row in rows}) == 1:
                result["bvecs"] = len(rows[0])
            elif rows and all(len(row) == 3 for row in rows):
                result["bvecs"] = len(rows)
    except OSError:
        return result
    result["consistent"] = bool(result["bvals"] and result["bvals"] == result["bvecs"])
    return result


REST_BOLD_PATTERNS = (
    "func/sub-*_task-rest*_bold.nii", "func/sub-*_task-rest*_bold.nii.gz",
    "func/*_task-rest*_bold.nii", "func/*_task-rest*_bold.nii.gz",
)


def rest_bold_runs(subject: Path) -> list[Path]:
    """Every resting-state BOLD run, in run order; the first is inventory's raw BOLD."""
    for pattern in REST_BOLD_PATTERNS:
        matches = sorted(subject.glob(pattern))  # the same order _first uses
        if matches:
            return matches
    return []


def bold_run_tr(bold: Path) -> float | None:
    """A BOLD run's TR: its BIDS JSON RepetitionTime, else a unit-labelled NIfTI header."""
    tr = _number(_read_json(_sidecar(bold, ".json"))[0].get("RepetitionTime"))
    return tr if tr is not None else _nifti_tr_seconds(bold)


def looks_like_subject(path: Path) -> bool:
    """Return true for raw BIDS, partially processed, or SBCI-ready subjects."""
    if not path.is_dir():
        return False
    if (path / "dwi_pipeline").is_dir():
        return True
    if (path / "psc_sbci_final_files").is_dir():
        return True
    return any((path / name).is_dir() for name in ("anat", "dwi", "func", "fmri")) and (
        _first(path, ("anat/*_T1w.nii", "anat/*_T1w.nii.gz")) is not None
    )


def inventory_subject(subject: Path) -> dict[str, Any]:
    """Detect raw inputs and durable completion markers without changing data."""
    dwi_pipeline = subject / "dwi_pipeline"
    final_files = subject / "psc_sbci_final_files"
    structure = dwi_pipeline / "structure"
    diffusion = dwi_pipeline / "diffusion"
    raw_t1 = _first(subject, ("anat/sub-*_T1w.nii", "anat/sub-*_T1w.nii.gz", "anat/*_T1w.nii", "anat/*_T1w.nii.gz"))
    # Prefer canonical BIDS names so generated ap_dwi.nii.gz is never mistaken
    # for the immutable raw series during resume.
    raw_dwi = _first(subject, ("dwi/sub-*_dwi.nii", "dwi/sub-*_dwi.nii.gz", "dwi/*_dwi.nii", "dwi/*_dwi.nii.gz"))
    raw_bold = _first(subject, REST_BOLD_PATTERNS)
    t1_json = _sidecar(raw_t1, ".json")
    dwi_json = _sidecar(raw_dwi, ".json")
    dwi_bval = _sidecar(raw_dwi, ".bval")
    dwi_bvec = _sidecar(raw_dwi, ".bvec")
    bold_json = _sidecar(raw_bold, ".json")
    dwi_metadata_raw, dwi_json_error = _read_json(dwi_json)
    bold_metadata_raw, bold_json_error = _read_json(bold_json)
    gradient_counts = _gradient_counts(dwi_bval, dwi_bvec)
    input_issues: list[dict[str, str]] = []
    for path, code, message in (
        (dwi_bval, "missing_dwi_bval", "DWI .bval sidecar is missing."),
        (dwi_bvec, "missing_dwi_bvec", "DWI .bvec sidecar is missing."),
        (dwi_json, "missing_dwi_json", "DWI JSON sidecar is missing."),
        (bold_json, "missing_bold_json", "Resting-state BOLD JSON sidecar is missing."),
    ):
        if raw_dwi is not None and code.startswith("missing_dwi") and path is None:
            input_issues.append({"code": code, "message": message})
        if raw_bold is not None and code == "missing_bold_json" and path is None:
            input_issues.append({"code": code, "message": message})
    if dwi_json_error:
        input_issues.append({"code": "invalid_dwi_json", "message": dwi_json_error, "path": str(dwi_json)})
    if bold_json_error:
        input_issues.append({"code": "invalid_bold_json", "message": bold_json_error, "path": str(bold_json)})
    dwi_metadata = _normalize_metadata(dwi_metadata_raw, modality="dwi")
    bold_metadata = _normalize_metadata(bold_metadata_raw, modality="bold")
    bold_metadata["repetitionTimeSource"] = "json" if bold_metadata["repetitionTime"] is not None else None
    if bold_metadata["repetitionTime"] is None:
        header_tr = _nifti_tr_seconds(raw_bold)
        if header_tr is not None:
            bold_metadata["repetitionTime"] = round(header_tr, 6)
            bold_metadata["repetitionTimeSource"] = "nifti_header"
    if dwi_json is not None and not dwi_metadata["phaseEncodingDirection"]:
        input_issues.append({"code": "missing_dwi_phase_encoding", "message": "DWI phase-encoding direction is missing from JSON."})
    if dwi_json is not None and dwi_metadata["totalReadoutTime"] is None:
        input_issues.append({"code": "missing_dwi_readout", "message": "DWI total readout time cannot be derived from JSON."})
    if bold_json is not None and bold_metadata["repetitionTime"] is None:
        input_issues.append({"code": "missing_bold_tr", "message": "BOLD RepetitionTime is missing from JSON."})
    if dwi_bval is not None and dwi_bvec is not None and not gradient_counts["consistent"]:
        input_issues.append({"code": "gradient_count_mismatch", "message": "DWI b-value and b-vector counts are empty, malformed, or inconsistent."})
    bold_runs = sorted(
        run for run in (subject / "fmri" / "bold").glob("[0-9]*")
        if run.is_dir() and run.name.isdigit()
    ) if (subject / "fmri" / "bold").is_dir() else []
    fsfast_ready = bool(bold_runs) and all(
        _nonempty(run / name)
        for run in bold_runs
        for name in ("fc.surface.lh.nii.gz", "fc.surface.rh.nii.gz", "fc.mni.nii.gz")
    )
    connectome_root = final_files if final_files.is_dir() else dwi_pipeline
    cortical_sc = _first(connectome_root / "sbci_connectome", ("smoothed_sc_avg_*_ico4.mat",))
    functional_fc = _first(connectome_root / "sbci_connectome", ("fc_avg_ico4.mat",))
    psc = _first(connectome_root / "psc_connectome", ("*_desikan_cm_count_processed.mat",))
    completed_connectivity = bool(
        final_files.is_dir()
        and cortical_sc is not None and functional_fc is not None
        and _nonempty(cortical_sc) and _nonempty(functional_fc)
    )
    completed = {
        "dwi_correction": all(_nonempty(subject / "dwi" / name) for name in (
            "eddy_corrected_data.nii.gz", "eddy_corrected_data.eddy_rotated_bvecs", "ap_dwi.bval",
        )),
        "fmri_correction": _nonempty(subject / "fmri" / "bold_distcorr.nii.gz"),
        # All four prepared inputs, matching validate_prepare_outputs: a run that
        # dies after converting data.nii.gz but before the gradients must not
        # read as complete, or the planner skips straight past it.
        "prepare_data": all(_nonempty(dwi_pipeline / name) for name in (
            "t1.nii.gz", "data.nii.gz", "flip_x.bval", "flip_x.bvec",
        )) or diffusion.is_dir(),
        "t1_dwi_registration": _nonempty(diffusion / "dti" / "fa.nii.gz") and _nonempty(structure / "t1_warped.nii.gz"),
        "freesurfer": all(_nonempty(structure / "t1_freesurfer" / relative) for relative in (
            "surf/lh.white", "surf/rh.white", "mri/wmparc.mgz",
            "label/lh.aparc.a2009s.annot", "label/rh.aparc.a2009s.annot",
        )),
        "fodf_tissue": _nonempty(diffusion / "fodf" / "fodf.nii.gz") and all(
            _nonempty(structure / name) for name in ("map_wm.nii.gz", "map_gm.nii.gz", "map_csf.nii.gz")
        ),
        "fsfast": fsfast_ready,
        "psc": psc is not None and _nonempty(psc),
        "sbci": cortical_sc is not None and functional_fc is not None and _nonempty(cortical_sc) and _nonempty(functional_fc),
    }
    sbci_ready = (
        _nonempty(diffusion / "dti" / "fa.nii.gz")
        and completed["freesurfer"]
        and completed["fodf_tissue"]
        and completed["fsfast"]
        and _nonempty(structure / "aparc.a2009s+aseg.nii.gz")
    )
    return {
        "subject": str(subject),
        "raw": {
            "t1": str(raw_t1) if raw_t1 else None,
            "dwi": str(raw_dwi) if raw_dwi else None,
            "bold": str(raw_bold) if raw_bold else None,
        },
        "sidecars": {
            "t1Json": str(t1_json) if t1_json else None,
            "dwiJson": str(dwi_json) if dwi_json else None,
            "dwiBval": str(dwi_bval) if dwi_bval else None,
            "dwiBvec": str(dwi_bvec) if dwi_bvec else None,
            "boldJson": str(bold_json) if bold_json else None,
        },
        "rawMetadata": {
            "dwi": dwi_metadata,
            "bold": bold_metadata,
            "gradientCounts": gradient_counts,
        },
        "inputIssues": input_issues,
        "inputLevel": "completed_connectivity" if completed_connectivity else ("sbci_ready" if sbci_ready else "preprocessed_or_raw"),
        "completedConnectivity": completed_connectivity,
        "completedConnectivityRoot": str(final_files) if completed_connectivity else None,
        "completed": completed,
        "sbciReady": sbci_ready,
        "pscReady": _nonempty(diffusion / "dti" / "fa.nii.gz") and completed["fodf_tissue"] and completed["freesurfer"],
    }


def probe_commands(
    *,
    run_psc: bool = False,
    require_distortion_correction: bool = True,
    preprocessing_required: bool = True,
    freesurfer_license: str | Path | None = None,
    synb0_image: str | None = None,
) -> dict[str, Any]:
    """Inspect PATH plus CyberNeuro's known isolated runtime prefixes."""
    return inspect_preprocessing_runtime(
        run_psc=run_psc,
        require_distortion_correction=require_distortion_correction,
        preprocessing_required=preprocessing_required,
        freesurfer_license=freesurfer_license,
        synb0_image=synb0_image,
    )


def plan_workflow(
    inventory: dict[str, Any],
    *,
    run_psc: bool = False,
    resume: bool = True,
    require_distortion_correction: bool = True,
    command_paths: dict[str, str | None] | None = None,
    distortion_corrected_input: bool = False,
    synb0_configured: bool = False,
) -> dict[str, Any]:
    """Plan the stages. `distortion_corrected_input` declares that DWI and BOLD were
    corrected upstream: the two correction stages then copy rather than compute, so
    they need no commands and no phase-encoding/readout metadata."""
    correction_stages = ("dwi_correction", "fmri_correction")
    # Sidecars needed only to correct distortion; BOLD TR falls back to the NIfTI
    # header, and the fsfast pre-flight still blocks if no TR can be found.
    correction_only_issues = {
        "missing_dwi_phase_encoding", "missing_dwi_readout", "missing_dwi_json", "missing_bold_json",
    }
    completed = dict(inventory["completed"])
    # Cleaned or imported subjects may no longer retain early intermediate
    # markers. Durable downstream products prove that their prerequisites ran.
    if inventory["sbciReady"]:
        for stage in STAGE_ORDER[: STAGE_ORDER.index("psc")]:
            completed[stage] = True
    if completed["psc"]:
        for stage in STAGE_ORDER[: STAGE_ORDER.index("psc")]:
            if stage != "fsfast":
                completed[stage] = True
    if completed["sbci"]:
        for stage in STAGE_ORDER[: STAGE_ORDER.index("psc")]:
            completed[stage] = True
    raw = inventory["raw"]
    command_paths = command_paths if command_paths is not None else probe_commands()["commands"]
    rows: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    selected = [stage for stage in STAGE_ORDER if stage != "psc" or run_psc]
    for stage in selected:
        if completed[stage] and resume:
            state = "complete"
            missing_commands = []
        else:
            missing_commands = [command for command in STAGE_COMMANDS[stage] if not command_paths.get(command)]
            if distortion_corrected_input and stage in correction_stages:
                missing_commands = []
            if stage == "dwi_correction" and (command_paths.get("docker") or command_paths.get("synb0")):
                missing_commands = [command for command in missing_commands if command != "apptainer"]
            if missing_commands:
                state = "blocked"
                blockers.append({
                    "stage": stage,
                    "message": "Missing commands: " + ", ".join(missing_commands),
                })
            else:
                state = "pending"
        rows.append({
            "id": stage,
            "label": STAGE_LABELS[stage],
            "state": state,
            "missingCommands": missing_commands,
        })
    for key, label in (("t1", "T1w anatomical scan"), ("dwi", "DWI scan"), ("bold", "resting-state BOLD scan")):
        if raw[key] is None and not inventory["sbciReady"] and not completed["sbci"]:
            blockers.append({"stage": "input", "message": f"Missing {label}."})
    if not inventory["sbciReady"] and not completed["sbci"]:
        blockers.extend(
            {"stage": "input", "message": row["message"], "code": row["code"]}
            for row in inventory.get("inputIssues", [])
            if not (distortion_corrected_input and row["code"] in correction_only_issues)
        )
    if (
        require_distortion_correction and not distortion_corrected_input and not synb0_configured
        and not inventory["sbciReady"] and not completed["sbci"] and not completed["dwi_correction"]
    ):
        blockers.append({
            "stage": "dwi_correction",
            "message": "A Synb0-DISCO image and a readable FreeSurfer license are required for single-direction ADNI DWI.",
        })
    return {
        "readyToRun": not blockers,
        "runPsc": run_psc,
        "resume": resume,
        "requireDistortionCorrection": require_distortion_correction,
        "distortionCorrectedInput": distortion_corrected_input,
        "stages": rows,
        "blockers": blockers,
        "note": "PSC is an optional companion branch; SBCI does not consume the PSC connectome.",
    }
