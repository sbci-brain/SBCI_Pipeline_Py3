"""BOLD routes: FUGUE with the DWI field map (`raw`), topup on a spin-echo fieldmap pair
(`corrected_dwi_raw_bold`), or a copy of BOLD corrected upstream (`corrected`)."""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from corrections.common import copy_or_convert, missing_commands, nonempty, pe_vector, unwarp_direction
from workflow.planner import bold_run_tr, inventory_subject, nifti_shape, nifti_volume_count, rest_bold_runs


def bold_epi_fieldmaps(subject: Path) -> list[tuple[Path, Any, Any]]:
    """BIDS fmap/*_epi images with their JSON PhaseEncodingDirection and TotalReadoutTime."""
    maps = []
    for image in sorted(subject.glob("fmap/*_epi.nii*")):
        sidecar = image.with_name(re.sub(r"\.nii(\.gz)?$", ".json", image.name))
        meta = json.loads(sidecar.read_text(encoding="utf-8-sig")) if sidecar.is_file() else {}
        maps.append((image, meta.get("PhaseEncodingDirection"), meta.get("TotalReadoutTime")))
    return maps


def _bold_topup_actions(
    work_subject: Path, raw_bold: Path, metadata: Mapping[str, Any], commands: Mapping[str, str | None],
) -> list[list[str]]:
    """Raw BOLD with a spin-echo EPI pair in fmap/ (e.g. the SMS protocol): topup on the pair,
    applytopup on the BOLD. Only the first rest run is corrected, as on the FUGUE route."""
    fieldmaps = bold_epi_fieldmaps(work_subject)
    directions = {pe for _, pe, _ in fieldmaps}
    bold_pe = metadata.get("phaseEncodingDirection")
    if (len(fieldmaps) != 2 or len(directions) != 2 or bold_pe not in directions or any(
            isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0 for _, _, t in fieldmaps)):
        raise RuntimeError(
            "workflow.bold_topup_fieldmaps needs exactly two fmap/*_epi images with opposite "
            "PhaseEncodingDirection (one matching the BOLD's) and a TotalReadoutTime in each JSON."
        )
    shapes = {nifti_shape(path) for path in (raw_bold, *(image for image, _, _ in fieldmaps))}
    if None not in shapes and len(shapes) > 1:
        raise RuntimeError("The fmap/ EPI pair and the BOLD must share one voxel grid for applytopup.")
    missing = missing_commands(commands, ("fslmerge", "topup", "applytopup"))
    if missing:
        raise RuntimeError("Missing fMRI-correction commands: " + ", ".join(missing))
    out = work_subject / "fmri"
    out.mkdir(parents=True, exist_ok=True)
    bold = out / "bold.nii.gz"
    copy_or_convert(raw_bold, bold, commands)
    pair = out / "se_epi_pair.nii.gz"
    acqparams = out / "se_epi_acqparams.txt"
    # topup needs one acqparams row per volume, and an image may hold several (HCP-YA's hold 3).
    volumes = [nifti_volume_count(image) or 1 for image, _, _ in fieldmaps]
    acqparams.write_text("".join(f"{' '.join(pe_vector(pe))} {readout:.9g}\n" * n for (_, pe, readout), n in zip(fieldmaps, volumes)), encoding="ascii")
    # applytopup reads the row of the first volume acquired with the BOLD's phase encoding.
    index = 1 + sum(volumes[:next(i for i, (_, pe, _) in enumerate(fieldmaps) if pe == bold_pe)])
    # b02b0.cnf subsamples by 2, which needs an even grid; b02b0_1.cnf does not subsample.
    shape = next(iter(shapes))
    config = "b02b0.cnf" if shape is not None and all(n % 2 == 0 for n in shape) else "b02b0_1.cnf"
    return [
        [str(commands["fslmerge"]), "-t", str(pair), *(str(image) for image, _, _ in fieldmaps)],
        [str(commands["topup"]), f"--imain={pair}", f"--datain={acqparams}", f"--config={config}", f"--out={out / 'topup_bold'}", f"--iout={out / 'se_epi_unwarped'}", f"--fout={out / 'field_hz_bold'}", "--verbose"],
        [str(commands["applytopup"]), f"--imain={bold}", f"--inindex={index}", f"--datain={acqparams}", f"--topup={out / 'topup_bold'}", "--method=jac", f"--out={out / 'bold_distcorr'}"],
    ]


def build_fmri_correction_actions(
    work_subject: Path,
    commands: Mapping[str, str | None],
    workflow: Mapping[str, Any] | None = None,
) -> list[list[str]]:
    inventory = inventory_subject(work_subject)
    raw_bold = inventory["raw"].get("bold")
    metadata = inventory["rawMetadata"]["bold"]
    if not raw_bold or not nonempty(Path(raw_bold)):
        raise RuntimeError("fMRI correction requires a non-empty resting-state BOLD BIDS input.")
    # Checked before distortion_corrected_input: SMS-style data has upstream-corrected DWI
    # but raw BOLD with its own spin-echo fieldmap pair.
    if (workflow or {}).get("bold_topup_fieldmaps") is True:
        return _bold_topup_actions(work_subject, Path(raw_bold), metadata, commands)
    if (workflow or {}).get("distortion_corrected_input") is True:
        # Every rest run, as the lab's historical ABCD processing used all four:
        # run 1 -> fmri/bold_distcorr.nii.gz, run N -> fmri/bold_distcorr_run-NN.nii.gz.
        # FSFast applies one TR to all runs, so runs with a different TR are refused.
        runs = rest_bold_runs(work_subject)
        trs = [bold_run_tr(run) for run in runs]
        if len({round(tr, 6) for tr in trs if tr is not None}) > 1:
            raise RuntimeError(
                "Resting-state runs have different TRs ("
                + ", ".join(f"{run.name}: {tr}" for run, tr in zip(runs, trs)) + "); FSFast needs one TR."
            )
        for index, run in enumerate(runs, start=1):
            stem = "bold_distcorr" if index == 1 else f"bold_distcorr_run-{index:02d}"
            copy_or_convert(run, work_subject / "fmri" / f"{stem}.nii.gz", commands)
            # The run's own motion estimates, for FMRI_MOTION_REGRESSORS=tsv (step 5).
            motion = run.with_name(re.sub(r"_bold\.nii(\.gz)?$", "_motion.tsv", run.name))
            if nonempty(motion):
                shutil.copy2(motion, work_subject / "fmri" / f"{stem}_motion.tsv")
        return []
    spacing = metadata.get("effectiveEchoSpacing")
    if not isinstance(spacing, (int, float)) or isinstance(spacing, bool) or spacing <= 0:
        raise RuntimeError("BOLD EffectiveEchoSpacing must be present in BIDS JSON; no default is used.")
    unwarp = unwarp_direction(metadata.get("phaseEncodingDirection"))
    required = ("fslmaths", "bet", "flirt", "fugue")
    missing = missing_commands(commands, required)
    if missing:
        raise RuntimeError("Missing fMRI-correction commands: " + ", ".join(missing))
    dwi = work_subject / "dwi"
    field = dwi / "field_hz.nii.gz"
    hifi = dwi / "hifi_b0.nii.gz"
    if not nonempty(field) or not nonempty(hifi):
        raise RuntimeError("Validated DWI field_hz.nii.gz and hifi_b0.nii.gz are required; uncorrected fallback is disabled.")
    out = work_subject / "fmri"
    out.mkdir(parents=True, exist_ok=True)
    bold = out / "bold.nii.gz"
    actions: list[list[str]] = []
    copy_action = copy_or_convert(Path(raw_bold), bold, commands)
    if copy_action:
        actions.append(copy_action)
    actions.extend([
        [str(commands["fslmaths"]), str(bold), "-Tmean", str(out / "mean_bold.nii.gz")],
        [str(commands["bet"]), str(out / "mean_bold.nii.gz"), str(out / "mean_bold_brain.nii.gz"), "-m", "-f", "0.30"],
        [str(commands["flirt"]), "-in", str(hifi), "-ref", str(out / "mean_bold.nii.gz"), "-out", str(out / "b0_in_fmri_space.nii.gz"), "-omat", str(out / "b0_to_fmri.mat"), "-dof", "6", "-cost", "normcorr", "-searchrx", "-10", "10", "-searchry", "-10", "10", "-searchrz", "-10", "10"],
        [str(commands["flirt"]), "-in", str(field), "-ref", str(out / "mean_bold.nii.gz"), "-out", str(out / "field_hz_fmri.nii.gz"), "-init", str(out / "b0_to_fmri.mat"), "-applyxfm", "-interp", "trilinear"],
        # topup --fout writes Hz; fugue --loadfmap reads rad/s, hence x 2*pi.
        [str(commands["fslmaths"]), str(out / "field_hz_fmri.nii.gz"), "-s", "1.5", "-mul", repr(2 * math.pi), str(out / "field_rads_fmri_smooth.nii.gz")],
        [str(commands["fugue"]), f"--loadfmap={out / 'field_rads_fmri_smooth.nii.gz'}", f"--dwell={spacing:.9g}", "-i", str(bold), f"--mask={out / 'mean_bold_brain_mask.nii.gz'}", f"--unwarpdir={unwarp}", "-u", str(out / "bold_distcorr.nii.gz"), "--verbose"],
    ])
    return actions
