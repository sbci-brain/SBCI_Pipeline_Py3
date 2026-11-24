#!/usr/bin/env python3
"""Execute one validated raw-BIDS-to-SBCI workflow stage in a copied worktree."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from corrections.common import (
    available_cpus,
    missing_commands as _missing,
    nonempty as _nonempty,
    required_file as _required,
)
from corrections.dwi import build_dwi_correction_actions
from corrections.fmri import bold_epi_fieldmaps, build_fmri_correction_actions
from workflow.planner import inventory_subject
from workflow.preprocessing_executor import (
    CommandRunner,
    _action_fingerprint,
    ensure_work_subject,
    execution_lock,
    sha256_file,
    source_snapshot,
    validate_prepare_outputs,
    write_json_atomic,
)


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
# The stage scripts every dataset runs; workflow.reference_scripts_dir names another copy.
PREPROCESSING_SCRIPTS = PIPELINE_ROOT / "preprocessing"

REFERENCE_SCRIPTS = {
    "t1_dwi_registration": "preproc_step2_t1_dwi_registration.sh",
    "freesurfer": "preproc_step3_t1_freesurfer.sh",
    "fodf_tissue": "preproc_step4_fodf_estimation.sh",
    "fsfast": "preproc_step5_fmri.sh",
    "psc": "psc_step1_tractography.sh",
}

REFERENCE_CWD = {
    "t1_dwi_registration": "dwi_pipeline",
    "freesurfer": "dwi_pipeline",
    "fodf_tissue": "dwi_pipeline",
    "fsfast": ".",
    "psc": ".",
}

NATIVE_COMMANDS = {
    "dwi_correction": {
        "fslchfiletype", "fslroi", "bet", "fslmerge", "topup", "fslmaths",
        "eddy", "apptainer", "docker", "synb0",
    },
    "fmri_correction": {"fslchfiletype", "fslmaths", "bet", "flirt", "fugue", "fslmerge", "topup", "applytopup"},
}

SHELL_POLICIES = ("nearest_1000", "all", "explicit")
# b-values where the tensor model fits well; a tensor shell outside it gets a warning.
TENSOR_SHELL_RANGE = (700, 1500)
FODF_MODELS = ("auto", "ssst")
MOTION_REGRESSORS = ("mcextreg", "tsv")

StageRunner = Callable[[Sequence[str], Path, Path, Mapping[str, str]], int]


def profile_path(value: Any) -> str | None:
    """workflow.preprocessing_config as a path. A relative name is looked up in this pipeline
    (datasets/adni/profile.conf), so a run names its profile without knowing where the
    pipeline is installed."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() and (PIPELINE_ROOT / path).is_file():
        path = PIPELINE_ROOT / path
    return str(path)


def template_problem(environment: Mapping[str, str] | None = None) -> str | None:
    """Why registration cannot find the MNI 152 template, or None when it can.

    preprocessing/preproc_step2 reads it from TEMPLATEDIR, else SBCI_TEMPLATE_DIR, which the
    image sets. Checked in the preflight: the stage would otherwise fail minutes in."""
    environment = os.environ if environment is None else environment
    value = environment.get("TEMPLATEDIR") or environment.get("SBCI_TEMPLATE_DIR")
    if not value:
        return ("SBCI_TEMPLATE_DIR is not set: registration needs the mni_152_sym_09c template folder "
                "(the sbci-full image sets it; outside the image, set it yourself).")
    missing = [name for name in ("t1_template.nii.gz", "t1_brain_probability_map.nii.gz")
               if not _nonempty(Path(value) / "t1" / name)]
    if missing:
        return f"The MNI template folder {value} lacks t1/{' and t1/'.join(missing)}."
    return None


def config_setting(config: Path, key: str) -> str | None:
    """The value of `key` a stage script sees after sourcing `config`.

    Only plain `KEY=value` lines are understood. Any other assignment of `key`
    is refused rather than guessed at, because the script would still act on it.
    """
    value = None
    for line in config.read_text(encoding="utf-8-sig").splitlines():
        if not re.match(rf"\s*(?:export\s+)?{key}=", line):
            continue
        match = re.match(rf"\s*(?:export\s+)?{key}=(['\"]?)([A-Za-z0-9_.]*)\1\s*(?:#.*)?$", line)
        if not match:
            raise RuntimeError(f"{config}: write {key} as a plain assignment ({key}=value), not {line.strip()!r}.")
        value = match.group(2) or None
    return value


def dwi_shells(bval: Path) -> list[int]:
    """Non-zero shells, rounded to the nearest 100 exactly as preproc_step2 does."""
    rounded = {int(float(value) / 100 + 0.5) * 100 for value in bval.read_text(encoding="utf-8-sig").split()}
    return sorted(rounded - {0})


def nearest_1000(shells: Sequence[int]) -> int:
    """The shell nearest b = 1000, as preproc_step2 picks it; of two equally near, the higher."""
    return min(shells, key=lambda shell: (abs(shell - 1000), -shell))


def config_shells(config: Path, key: str) -> list[int] | None:
    """The non-zero shells of a `KEY=(0 1000 2000)` line, rounded as dwi_shells rounds them.
    None when the profile has no such line."""
    shells = None
    for line in config.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(rf"\s*(?:export\s+)?{key}=\(([^()$`]*)\)\s*(?:#.*)?$", line)
        if match:
            try:
                values = [float(word.strip("'\"")) for word in match.group(1).split()]
            except ValueError:
                return None
            shells = sorted({int(value / 100 + 0.5) * 100 for value in values} - {0})
    return shells


def shell_warnings(bval: Path, config: Path) -> list[str]:
    """What a user should know about the shells this profile takes from this DWI. None of it
    stops a run; what does stop one is check_shell_policy's.

    - The tensor fit on a shell outside TENSOR_SHELL_RANGE: its FA drives the T1-to-DWI
      registration, and the tensor model fits poorly there.
    - Shells that are not the ones the profile lists in DTI_BVALS / FODF_BVALS. Under
      nearest_1000 and all those lines select nothing, so they say what the profile expects;
      a difference is the one sign in the data that it is not the profile's dataset.
    """
    policy = config_setting(config, "SHELL_POLICY") or "nearest_1000"
    shells = dwi_shells(bval)
    if not shells or policy not in SHELL_POLICIES:
        return []
    listed = {key: config_shells(config, key) for key in ("DTI_BVALS", "FODF_BVALS")}
    found = ", ".join(map(str, shells))
    warnings: list[str] = []
    if policy == "explicit":
        tensor = listed["DTI_BVALS"] or []
        absent = sorted({shell for value in listed.values() for shell in value or []} - set(shells))
        if absent:
            warnings.append(
                f"{config} lists b = {', '.join(map(str, absent))} in DTI_BVALS/FODF_BVALS, but this DWI's shells "
                f"are b = {found}: the data may not be the acquisition this profile was written for."
            )
    else:
        tensor = [nearest_1000(shells)]
        selected = {"DTI_BVALS": tensor, "FODF_BVALS": shells if policy == "all" else tensor}
        differing = [key for key in selected if listed[key] is not None and listed[key] != selected[key]]
        if differing:
            expected = "; ".join(f"{key}=(0 {' '.join(map(str, listed[key]))})" for key in differing)
            used = "; ".join(f"{key}=(0 {' '.join(map(str, selected[key]))})" for key in differing)
            warnings.append(
                f"This DWI's shells (b = {found}) give {used} under SHELL_POLICY={policy}, but {config} was "
                f"written for {expected}: the data may not be the acquisition this profile was written for. "
                "The run uses the shells of the data."
            )
    low, high = TENSOR_SHELL_RANGE
    outside = [shell for shell in tensor if not low <= shell <= high]
    if outside:
        warnings.append(
            f"The tensor fit uses b = {', '.join(map(str, outside))}, outside b = {low}-{high} where the tensor "
            "model fits well. Its FA drives the T1-to-DWI registration: check that registration."
        )
    return warnings


def validation_status(record: Path) -> str:
    """`validated` when a dataset's validation.md says so; anything else is `untested`."""
    try:
        text = record.read_text(encoding="utf-8")
    except OSError:
        return "untested"
    match = re.search(r"(?m)^\|\s*Status\s*\|\s*`?(\w+)`?\s*\|", text)
    return "validated" if match and match.group(1) == "validated" else "untested"


def profile_record(value: Any) -> dict[str, str | None]:
    """What a run records about its profile (config/versions.json).

    profile_status is `validated` or `untested` when the file equals a profile shipped in
    datasets/, by that dataset's validation.md, and `modified` for any other file: changed, or
    written by the user. `validated` says the profile was validated on its own dataset;
    nothing can show that the data of a run are that dataset.
    """
    path = profile_path(value)
    if path is None or not Path(path).is_file():
        return {"profile": None, "dataset_name": None, "profile_status": None}
    config = Path(path)
    content = config.read_bytes().replace(b"\r\n", b"\n")
    name, status = str(config), "modified"
    for shipped in sorted((PIPELINE_ROOT / "datasets").glob("*/profile*.conf")):
        if shipped.read_bytes().replace(b"\r\n", b"\n") == content:
            name = shipped.relative_to(PIPELINE_ROOT).as_posix()
            status = validation_status(shipped.parent / "validation.md")
            break
    try:
        dataset = config_setting(config, "DATASET_NAME")
    except RuntimeError:
        dataset = None
    return {"profile": name, "dataset_name": dataset, "profile_status": status}


def profile_warnings(workflow: Mapping[str, Any], inventory: Mapping[str, Any]) -> list[str]:
    """Warnings for the preflight and the job log: a profile no validation record covers, and
    shell_warnings. They never stop a run."""
    value = workflow.get("preprocessing_config")
    record = profile_record(value)
    if record["profile"] is None:
        return []
    warnings: list[str] = []
    if record["profile_status"] == "untested":
        warnings.append(
            f"{record['profile']} is marked untested: no dataset has been validated with it. "
            "Check the outputs of this run yourself (config/versions.json records this)."
        )
    elif record["profile_status"] == "modified":
        warnings.append(
            f"{record['profile']} is not one of the profiles shipped in datasets/ (it was changed, or written "
            "for this data), so no validation record covers this run (config/versions.json records this)."
        )
    bval = (inventory.get("sidecars") or {}).get("dwiBval")
    if bval and Path(bval).is_file():
        try:
            warnings += shell_warnings(Path(bval), Path(str(profile_path(value))))
        except (RuntimeError, ValueError, OSError):
            pass  # an unreadable profile or b-value file is reported by the checks that stop a run
    return warnings


def check_shell_policy(bval: Path, config: Path) -> None:
    """Refuse multi-shell data that has no explicit SHELL_POLICY (see preproc_step2)."""
    policy = config_setting(config, "SHELL_POLICY")
    if policy is not None and policy not in SHELL_POLICIES:
        raise RuntimeError(f"SHELL_POLICY={policy} in {config} is not one of {', '.join(SHELL_POLICIES)}.")
    shells = dwi_shells(bval)
    if not shells:
        raise RuntimeError(f"No non-zero b-value shell in {bval}.")
    if len(shells) > 1 and policy is None:
        kept = nearest_1000(shells)
        raise RuntimeError(
            f"DWI has {len(shells)} non-zero shells (b = {', '.join(map(str, shells))}) and {config} sets no "
            f"SHELL_POLICY, so only b = {kept} would be used and the other shells discarded. Set "
            "SHELL_POLICY=nearest_1000 (one shell, the ADNI choice), all (every shell, for a multi-shell "
            "fODF) or explicit (DTI_BVALS/FODF_BVALS as written)."
        )


def resolve_sh_order(config: Path, parameters: Mapping[str, Any], default: int = 6) -> int:
    """SH order from parameters.sh_order or SH_ORDER in the config; they may not disagree."""
    configured = config_setting(config, "SH_ORDER")
    requested = parameters.get("sh_order")
    if configured is not None and not configured.isdigit():
        raise RuntimeError(f"SH_ORDER={configured} in {config} is not an integer.")
    if configured is not None and requested is not None and requested != int(configured):
        raise RuntimeError(
            f"parameters.sh_order={requested} disagrees with SH_ORDER={configured} in {config}; set it in one place."
        )
    order = requested if requested is not None else int(configured or default)
    if isinstance(order, bool) or not isinstance(order, int) or order % 2 or not 2 <= order <= 16:
        raise RuntimeError(f"SH order must be an even integer from 2 through 16, not {order!r}.")
    return order


def sbci_stage_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """sbci_local.json for the SBCI stage that follows a preprocessing run.

    The SBCI stage counts SET runs from parameters.n_runs; N_RUNS in the preprocessing
    profile fills it, and the two may not disagree (the lab's ABCD database used 3).
    """
    parameters = dict(config.get("parameters") or {})
    profile = profile_path((config.get("workflow") or {}).get("preprocessing_config"))
    configured = config_setting(Path(profile), "N_RUNS") if profile else None
    if configured is not None:
        if "n_runs" in parameters and parameters["n_runs"] != int(configured):
            raise RuntimeError(
                f"parameters.n_runs={parameters['n_runs']} disagrees with N_RUNS={configured} in {profile}; "
                "set it in one place."
            )
        parameters["n_runs"] = int(configured)
    sbci: dict[str, Any] = {"schema_version": "1.0", "mode": "preprocess", "approved": True}
    if parameters:
        sbci["parameters"] = parameters
    stages = (config.get("workflow") or {}).get("stages")
    if stages in ("tractography", "fc"):  # the SBCI half a split skill asked for
        sbci["workflow"] = {"stages": stages}
    return sbci


def resolve_fmri_timing(config: Path, repetition_time: Any) -> dict[str, str]:
    """FMRI_TR from the BIDS RepetitionTime and FMRI_NSKIP from FMRI_SKIP_SECONDS.

    A config that declares FMRI_TR describes one acquisition; a subject with any
    other TR is refused rather than processed with timing meant for something else.
    """
    tr = repetition_time
    if isinstance(tr, bool) or not isinstance(tr, (int, float)) or tr <= 0:
        raise RuntimeError("BOLD RepetitionTime is missing from the BIDS JSON; FSFast needs the real TR and none is assumed.")
    declared = config_setting(config, "FMRI_TR")
    if declared is not None and abs(float(declared) - tr) > 1e-6:
        raise RuntimeError(
            f"This subject's BOLD RepetitionTime is {tr:g} s, but {config} declares FMRI_TR={declared}; "
            "its FSFast settings describe a different acquisition. Use a config for this protocol, "
            "or remove FMRI_TR to take the TR from BIDS."
        )
    nskip = config_setting(config, "FMRI_NSKIP")
    if nskip is None:
        nskip = str(round(float(config_setting(config, "FMRI_SKIP_SECONDS") or 12) / tr))
    return {"FMRI_TR": f"{tr:g}", "FMRI_NSKIP": nskip}


def declared_input_route(workflow: Mapping[str, Any]) -> str:
    """The INPUT_ROUTE that sbci_local.json's correction settings describe."""
    if workflow.get("distortion_corrected_input") is not True:
        return "raw"
    return "corrected_dwi_raw_bold" if workflow.get("bold_topup_fieldmaps") is True else "corrected"


def profile_mismatches(workflow: Mapping[str, Any], inventory: Mapping[str, Any], plan: Mapping[str, Any]) -> list[str]:
    """Why the profile does not fit this subject and these settings, before any stage runs.

    The images and sidecars can't show whether a scan was distortion corrected (the
    lab's corrected SMS DWI has the same sidecar fields as raw ADNI), so a profile names
    the input it was written for and the declared settings must agree with it. A wrong
    TR is the check fsfast makes, found here instead of hours into the job.
    """
    value = profile_path(workflow.get("preprocessing_config"))
    if value is None or not Path(value).is_file():
        return []
    config = Path(value)
    pending = {row.get("id") for row in plan.get("stages", []) if row.get("state") != "complete"}
    problems: list[str] = []
    try:
        route = config_setting(config, "INPUT_ROUTE")
        declared = declared_input_route(workflow)
        if route is not None and route != declared and pending & {"dwi_correction", "fmri_correction"}:
            problems.append(
                f"{config} is written for INPUT_ROUTE={route} input, but workflow.distortion_corrected_input and "
                f"workflow.bold_topup_fieldmaps describe {declared} input. Use the profile for this data or correct "
                "those settings; never declare uncorrected data as corrected."
            )
        if "fsfast" in pending and (inventory.get("raw") or {}).get("bold"):
            resolve_fmri_timing(config, ((inventory.get("rawMetadata") or {}).get("bold") or {}).get("repetitionTime"))
    except RuntimeError as exc:
        problems.append(str(exc))
    return problems


def unique_directions(bval: Path, bvec: Path, angle_degrees: float = 5.0) -> int:
    """Distinct non-b0 gradient directions; g and -g count once.

    Directions closer than `angle_degrees` merge, so eddy-rotated repeats of one
    design direction are not counted twice.
    """
    # ponytail: greedy O(n^2) clustering, fine for a few hundred volumes; it can
    # merge genuinely distinct directions in very dense schemes, which only undercounts.
    bvals = [float(value) for value in bval.read_text(encoding="utf-8-sig").split()]
    rows = [[float(v) for v in line.split()] for line in bvec.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    vectors = [list(column) for column in zip(*rows)] if len(rows) == 3 else rows
    if len(vectors) != len(bvals) or any(len(vector) != 3 for vector in vectors):
        raise RuntimeError(f"{bvec} does not hold one 3-vector per b-value in {bval}.")
    limit = math.cos(math.radians(angle_degrees))
    kept: list[list[float]] = []
    for value, vector in zip(bvals, vectors):
        norm = math.sqrt(sum(c * c for c in vector))
        if value < 50 or norm == 0:  # b < 50 rounds to shell 0 in preproc_step2
            continue
        unit = [c / norm for c in vector]
        if all(abs(sum(a * b for a, b in zip(unit, other))) < limit for other in kept):
            kept.append(unit)
    return len(kept)


def check_sh_order(bval: Path, bvec: Path, order: int) -> None:
    """Refuse an SH order the gradient table cannot determine: (L+1)(L+2)/2 directions."""
    needed = (order + 1) * (order + 2) // 2
    available = unique_directions(bval, bvec)
    if available < needed:
        supported = [L for L in range(2, 17, 2) if (L + 1) * (L + 2) // 2 <= available]
        advice = f"the highest order it supports is {supported[-1]}" if supported else "it supports no SH order"
        raise RuntimeError(
            f"SH order {order} needs at least {needed} unique gradient directions, but {bvec} has "
            f"{available}; {advice}. Lower SH_ORDER rather than fit an underdetermined model."
        )


def freesurfer_home_with_license(home: Path, license_path: Path, link_home: Path) -> Path:
    """FreeSurfer 6.0.0's programs ignore FS_LICENSE and read only $FREESURFER_HOME/license.txt.
    For an install without one, such as sbci-full.sif's, return a folder of links to the
    install's entries plus license.txt linked to the configured licence."""
    links = {entry.name: entry for entry in home.iterdir()}
    links["license.txt"] = license_path
    link_home.mkdir(parents=True, exist_ok=True)
    for name, target in links.items():
        link = link_home / name
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
    return link_home


# A thread count describes the machine, not the computation: a resumed stage on a node
# with another CPU count must not look like a different command and rerun.
RUNTIME_ONLY_FLAGS = ("--nthr=",)


def _stage_fingerprint(actions: Sequence[Sequence[str]]) -> str:
    return _action_fingerprint([[arg for arg in argv if not arg.startswith(RUNTIME_ONLY_FLAGS)] for argv in actions])


def stage_output_paths(stage: str, work_subject: Path, corrected_input: bool = False) -> dict[str, Path]:
    dwi = work_subject / "dwi"
    pipeline = work_subject / "dwi_pipeline"
    structure = pipeline / "structure"
    diffusion = pipeline / "diffusion"
    if stage == "dwi_correction":
        outputs = {
            "correctedDwi": dwi / "eddy_corrected_data.nii.gz",
            "rotatedBvec": dwi / "eddy_corrected_data.eddy_rotated_bvecs",
            "bval": dwi / "ap_dwi.bval",
        }
        if not corrected_input:  # a passthrough of upstream-corrected DWI has no field map
            outputs["fieldHz"] = dwi / "field_hz.nii.gz"
            outputs["hifiB0"] = dwi / "hifi_b0.nii.gz"
        return outputs
    if stage == "fmri_correction":
        return {"correctedBold": work_subject / "fmri" / "bold_distcorr.nii.gz"}
    if stage == "prepare_data":
        return {
            "t1": pipeline / "t1.nii.gz",
            "dwi": pipeline / "data.nii.gz",
            "bval": pipeline / "flip_x.bval",
            "bvec": pipeline / "flip_x.bvec",
        }
    if stage == "t1_dwi_registration":
        return {
            "fa": diffusion / "dti" / "fa.nii.gz",
            "md": diffusion / "dti" / "md.nii.gz",
            "t1Warped": structure / "t1_warped.nii.gz",
            "t1WholebrainWarped": structure / "t1_wholebrain_warped.nii.gz",
            "fodfDwi": diffusion / "dwi_fodf.nii.gz",
        }
    if stage == "freesurfer":
        fs = structure / "t1_freesurfer"
        return {
            "lhWhite": fs / "surf" / "lh.white",
            "rhWhite": fs / "surf" / "rh.white",
            "wmparc": fs / "mri" / "wmparc.mgz",
            "lhDestrieux": fs / "label" / "lh.aparc.a2009s.annot",
            "rhDestrieux": fs / "label" / "rh.aparc.a2009s.annot",
            "warpedParcellation": structure / "aparc.a2009s+aseg_warped_label.nii.gz",
            "parcellation": structure / "aparc.a2009s+aseg.nii.gz",
            "lhPial": fs / "surf" / "lh.pial",
            "rhPial": fs / "surf" / "rh.pial",
            "lhSphere": fs / "surf" / "lh.sphere",
            "rhSphere": fs / "surf" / "rh.sphere",
            "lhSphereReg": fs / "surf" / "lh.sphere.reg",
            "rhSphereReg": fs / "surf" / "rh.sphere.reg",
            "lhInflated": fs / "surf" / "lh.inflated",
            "rhInflated": fs / "surf" / "rh.inflated",
        }
    if stage == "fodf_tissue":
        return {
            "fodf": diffusion / "fodf" / "fodf.nii.gz",
            "wm": structure / "map_wm.nii.gz",
            "gm": structure / "map_gm.nii.gz",
            "csf": structure / "map_csf.nii.gz",
        }
    if stage == "fsfast":
        run = work_subject / "fmri" / "bold" / "001"
        return {
            "lhResidual": run / "fc.surface.lh.nii.gz",
            "rhResidual": run / "fc.surface.rh.nii.gz",
            "mniResidual": run / "fc.mni.nii.gz",
        }
    if stage == "psc":
        matches = sorted((pipeline / "psc_connectome").glob("*_desikan_cm_count_processed.mat"))
        return {"pscConnectome": matches[0] if matches else pipeline / "psc_connectome" / "<missing>"}
    raise ValueError(f"No output contract for stage {stage}")


def validate_stage_outputs(stage: str, work_subject: Path, corrected_input: bool = False) -> dict[str, Any]:
    if stage == "prepare_data":
        return validate_prepare_outputs(work_subject)
    outputs = stage_output_paths(stage, work_subject, corrected_input)
    nonempty = {name: _nonempty(path) for name, path in outputs.items()}
    ready = bool(outputs) and all(nonempty.values())
    return {
        "ready": ready,
        "outputs": {name: str(path) for name, path in outputs.items()},
        "nonempty": nonempty,
        "message": f"{stage} outputs passed validation." if ready else f"{stage} outputs are missing or empty.",
    }


def _output_hashes(validation: Mapping[str, Any]) -> dict[str, str]:
    if not validation.get("ready"):
        return {}
    return {name: sha256_file(Path(path)) for name, path in validation["outputs"].items()}


def stage_subprocess_runner(
    argv: Sequence[str], cwd: Path, log_path: Path, environment: Mapping[str, str]
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        completed = subprocess.run(
            list(argv), cwd=cwd, env=dict(environment), stdout=log,
            stderr=subprocess.STDOUT, check=False,
        )
    return completed.returncode


def _reference_action(
    stage: str,
    work_subject: Path,
    commands: Mapping[str, str | None],
    workflow: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> tuple[list[str], Path, dict[str, str]]:
    scripts_value = str(workflow.get("reference_scripts_dir") or "").strip()
    config_value = profile_path(workflow.get("preprocessing_config")) or ""
    scripts = (Path(scripts_value).expanduser() if scripts_value else PREPROCESSING_SCRIPTS).resolve()
    script = (scripts / REFERENCE_SCRIPTS[stage]).resolve()
    if script.parent != scripts or not _nonempty(script):
        raise RuntimeError(f"Approved stage script is missing or empty: {script}")
    config = _required(config_value, "workflow.preprocessing_config")
    pipeline = work_subject / "dwi_pipeline"
    sh_order = None
    fmri_timing: dict[str, str] = {}
    if stage == "fsfast":
        fmri_timing = resolve_fmri_timing(
            config, inventory_subject(work_subject)["rawMetadata"]["bold"].get("repetitionTime"),
        )
        motion = config_setting(config, "FMRI_MOTION_REGRESSORS") or "mcextreg"
        if motion not in MOTION_REGRESSORS:
            raise RuntimeError(f"FMRI_MOTION_REGRESSORS={motion} in {config} is not one of {', '.join(MOTION_REGRESSORS)}.")
        if motion == "tsv" and not _nonempty(work_subject / "fmri" / "bold_distcorr_motion.tsv"):
            raise RuntimeError(
                f"{config} sets FMRI_MOTION_REGRESSORS=tsv, but fmri/bold_distcorr_motion.tsv is missing: "
                "the BIDS func/*_motion.tsv files are copied by the distortion_corrected_input route."
            )
        # Same inputs, same order as preproc_step5; it never falls back to uncorrected BOLD.
        if not any(_nonempty(work_subject / relative) for relative in (
            "fsfast/bold/001/f.nii.gz", "fmri/bold_distcorr.nii.gz", "fmri_distcorr/bold_distcorr.nii.gz",
        )):
            raise RuntimeError(
                "fsfast needs the distortion-corrected BOLD fmri/bold_distcorr.nii.gz from "
                "fmri_correction; uncorrected BOLD is never used."
            )
    if stage == "t1_dwi_registration":
        # prepare_data leaves flip_x.bval here; a re-run after a partial failure
        # finds it already moved into diffusion/. If neither exists the script's
        # own shell check fails the stage.
        for bval in (pipeline / "flip_x.bval", pipeline / "diffusion" / "flip_x.bval"):
            if bval.is_file():
                check_shell_policy(bval, config)
                break
        problem = template_problem()
        if problem:
            raise RuntimeError(problem)
    if stage == "fodf_tissue":
        model = config_setting(config, "FODF_MODEL") or "auto"
        if model not in FODF_MODELS:
            raise RuntimeError(f"FODF_MODEL={model} in {config} is not one of {', '.join(FODF_MODELS)}.")
        fodf_bval = _required(str(pipeline / "diffusion" / "fodf.bval"), "fODF b-values")
        # preproc_step4 fits multi-shell multi-tissue CSD when fodf.bval holds
        # several shells; its default order there is 8, not the single-shell 6.
        multishell = len(dwi_shells(fodf_bval)) > 1
        sh_order = resolve_sh_order(config, parameters, default=8 if multishell else 6)
        check_sh_order(fodf_bval, _required(str(pipeline / "diffusion" / "fodf.bvec"), "fODF b-vectors"), sh_order)
    bash = commands.get("bash") or shutil.which("bash")
    if not bash:
        raise RuntimeError("bash is required to run the approved lab stage adapter.")
    cwd = (work_subject / REFERENCE_CWD[stage]).resolve()
    work_root = work_subject.resolve()
    if cwd != work_root and work_root not in cwd.parents:
        raise RuntimeError("Refusing a stage working directory outside the copied subject tree.")
    cwd.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    # The script calls tools by bare name; folders where inspection found them outside
    # PATH go last, so whatever PATH already resolves keeps winning.
    path = environment.get("PATH", "").split(os.pathsep)
    for value in commands.values():
        folder = str(Path(value).parent) if value else ""
        if folder and folder not in path:
            path.append(folder)
    environment["PATH"] = os.pathsep.join(path)
    environment["SBCI_CONFIG"] = str(config)
    license_value = str(workflow.get("freesurfer_license") or "").strip()
    if license_value:
        license_path = _required(license_value, "workflow.freesurfer_license")
        environment["FS_LICENSE"] = str(license_path)
        home = environment.get("FREESURFER_HOME")
        if home and not any(_nonempty(Path(home) / name) for name in ("license.txt", ".license")):
            # Outside preprocessing_work: the runner takes its only folder besides state/ as the subject.
            environment["FREESURFER_HOME"] = str(
                freesurfer_home_with_license(Path(home), license_path, work_subject.parent.parent / "freesurfer_home")
            )
    environment["CYBERNEURO_WORK_SUBJECT"] = str(work_subject)
    if sh_order is not None:
        environment["SH_ORDER"] = str(sh_order)
    environment.update(fmri_timing)
    return [str(bash), str(script)], cwd, environment


def execute_workflow_stage(
    stage: str,
    subject: Path,
    output_dir: Path,
    commands: Mapping[str, str | None],
    workflow: Mapping[str, Any],
    *,
    resume: bool = True,
    runner: StageRunner = stage_subprocess_runner,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one non-SBCI workflow stage and commit a validated marker."""
    if stage not in {"dwi_correction", "fmri_correction", *REFERENCE_SCRIPTS}:
        raise ValueError(f"Unsupported workflow stage executor: {stage}")
    source_before = source_snapshot(subject.resolve())
    output_dir = output_dir.resolve()
    corrected_input = workflow.get("distortion_corrected_input") is True and stage in (
        "dwi_correction", "fmri_correction",
    ) and not (stage == "fmri_correction" and workflow.get("bold_topup_fieldmaps") is True)
    with execution_lock(output_dir):
        work_subject, provenance, created = ensure_work_subject(subject, output_dir)
        if stage == "dwi_correction":
            actions = build_dwi_correction_actions(work_subject, commands, workflow)
            cwd = work_subject
            environment = dict(os.environ)
        elif stage == "fmri_correction":
            actions = build_fmri_correction_actions(work_subject, commands, workflow)
            cwd = work_subject
            environment = dict(os.environ)
        else:
            action, cwd, environment = _reference_action(stage, work_subject, commands, workflow, parameters or {})
            actions = [action]
        fingerprint = _stage_fingerprint(actions)
        marker = output_dir / "files" / "preprocessing_work" / "state" / f"{stage}.json"
        if resume and marker.is_file():
            previous = json.loads(marker.read_text(encoding="utf-8"))
            validation = validate_stage_outputs(stage, work_subject, corrected_input)
            hashes = _output_hashes(validation)
            if (
                previous.get("sourceFingerprint") == source_before["fingerprint"]
                and previous.get("actionFingerprint") == fingerprint
                and previous.get("outputHashes") == hashes
                and validation["ready"]
            ):
                return {
                    "stage": stage, "state": "skipped_complete", "workSubject": str(work_subject),
                    "workTreeCreated": created, "marker": str(marker), "validation": validation,
                }
        logs: list[str] = []
        for index, argv in enumerate(actions, start=1):
            program = Path(argv[0]).name
            if stage in NATIVE_COMMANDS and program not in NATIVE_COMMANDS[stage]:
                raise RuntimeError(f"Refusing undeclared {stage} command: {program}")
            if stage in REFERENCE_SCRIPTS and (program != "bash" or Path(argv[1]).name != REFERENCE_SCRIPTS[stage]):
                raise RuntimeError(f"Refusing undeclared stage adapter: {argv}")
            log = output_dir / "logs" / f"preprocess_{stage}_{index:02d}.log"
            code = runner(argv, cwd, log, environment)
            logs.append(str(log))
            if code != 0:
                return {
                    "stage": stage, "state": "failed_command", "exitCode": code,
                    "failedAction": index, "workSubject": str(work_subject), "logs": logs, "marker": None,
                }
        validation = validate_stage_outputs(stage, work_subject, corrected_input)
        if source_snapshot(subject.resolve())["fingerprint"] != source_before["fingerprint"]:
            raise RuntimeError("Source subject changed during preprocessing; no completion marker was written.")
        if not validation["ready"]:
            return {
                "stage": stage, "state": "failed_validation", "workSubject": str(work_subject),
                "logs": logs, "validation": validation, "marker": None,
            }
        marker_value = {
            "schemaVersion": "1.0", "stage": stage, "state": "complete",
            "sourceFingerprint": source_before["fingerprint"], "actionFingerprint": fingerprint,
            "workSubject": str(work_subject), "commands": [list(item) for item in actions],
            "logs": logs, "validation": validation, "outputHashes": _output_hashes(validation),
            "completedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "provenance": provenance["policy"],
            "distortionCorrectedInput": corrected_input,
        }
        write_json_atomic(marker, marker_value)
        return {
            "stage": stage, "state": "complete", "workSubject": str(work_subject),
            "workTreeCreated": created, "logs": logs, "marker": str(marker), "validation": validation,
        }


def execute_workflow_stage_safely(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Convert expected configuration/input failures into a GUI-readable state."""
    stage = str(args[0]) if args else str(kwargs.get("stage") or "unknown")
    subject = Path(args[1]) if len(args) > 1 else Path(str(kwargs.get("subject") or "."))
    output_dir = Path(args[2]) if len(args) > 2 else Path(str(kwargs.get("output_dir") or "."))
    try:
        return execute_workflow_stage(*args, **kwargs)
    except (RuntimeError, ValueError, OSError) as exc:
        work_subject = output_dir / "files" / "preprocessing_work" / subject.name
        return {
            "stage": stage,
            "state": "blocked",
            "message": str(exc),
            "workSubject": str(work_subject if work_subject.is_dir() else subject),
            "marker": None,
        }
