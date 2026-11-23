"""DWI route `raw`: Synb0-DISCO, topup and eddy on single-direction DWI."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping

from corrections.common import available_cpus, copy_or_convert, missing_commands, nonempty, pe_vector, required_file
from workflow.planner import inventory_subject, nifti_volume_count


def build_dwi_correction_actions(
    work_subject: Path,
    commands: Mapping[str, str | None],
    workflow: Mapping[str, Any],
) -> list[list[str]]:
    """Build the CPU Synb0/topup/eddy sequence without scanner defaults.

    With workflow.distortion_corrected_input the DWI was corrected upstream: it is
    copied to the paths later stages read and no command runs.
    """
    inventory = inventory_subject(work_subject)
    raw = inventory["raw"]
    sidecars = inventory["sidecars"]
    metadata = inventory["rawMetadata"]["dwi"]
    t1 = Path(raw["t1"]) if raw.get("t1") else None
    dwi = Path(raw["dwi"]) if raw.get("dwi") else None
    bval = Path(sidecars["dwiBval"]) if sidecars.get("dwiBval") else None
    bvec = Path(sidecars["dwiBvec"]) if sidecars.get("dwiBvec") else None
    if not all(path is not None and nonempty(path) for path in (t1, dwi, bval, bvec)):
        raise RuntimeError("DWI correction requires non-empty T1w, DWI, .bval, and .bvec BIDS inputs.")
    if workflow.get("distortion_corrected_input") is True:
        out = work_subject / "dwi"
        copy_or_convert(dwi, out / "eddy_corrected_data.nii.gz", commands)
        shutil.copy2(bval, out / "ap_dwi.bval")
        shutil.copy2(bvec, out / "eddy_corrected_data.eddy_rotated_bvecs")
        return []
    phase = metadata.get("phaseEncodingDirection")
    readout = metadata.get("totalReadoutTime")
    if not isinstance(readout, (int, float)) or isinstance(readout, bool) or readout <= 0:
        raise RuntimeError("DWI TotalReadoutTime must be present or derivable from BIDS JSON; no default is used.")
    pe = pe_vector(phase)
    license_path = required_file(str(workflow.get("freesurfer_license") or ""), "workflow.freesurfer_license")
    # The image's own copy of Synb0-DISCO (the `synb0` command), which needs no image or runner.
    builtin_synb0 = commands.get("synb0")
    synb0_image = str(workflow.get("synb0_image") or "").strip()
    if not synb0_image and not builtin_synb0:
        raise RuntimeError("workflow.synb0_image must identify an approved Synb0-DISCO image.")
    container = commands.get("apptainer") or commands.get("docker")
    required = ("fslroi", "bet", "fslmerge", "topup", "fslmaths", "eddy")
    missing = missing_commands(commands, required)
    if not container and not builtin_synb0:
        missing.append("apptainer-or-docker")
    if missing:
        raise RuntimeError("Missing DWI-correction commands: " + ", ".join(missing))

    out = work_subject / "dwi"
    out.mkdir(parents=True, exist_ok=True)
    dwi_gz = out / "ap_dwi.nii.gz"
    t1_gz = out / "T1.nii.gz"
    shutil.copy2(bval, out / "ap_dwi.bval")
    shutil.copy2(bvec, out / "ap_dwi.bvec")
    actions: list[list[str]] = []
    for source, destination in ((dwi, dwi_gz), (t1, t1_gz)):
        action = copy_or_convert(source, destination, commands)
        if action:
            actions.append(action)

    synb0_out = out / "synb0_output"
    synb0_out.mkdir(exist_ok=True)
    acqparams = out / "acqparams.txt"
    vector = " ".join(pe)
    acqparams.write_text(f"{vector} {readout:.9g}\n{vector} 0.000\n", encoding="ascii")
    gradient_count = inventory["rawMetadata"]["gradientCounts"].get("bvals")
    if not isinstance(gradient_count, int) or gradient_count < 1:
        raise RuntimeError("Cannot derive a non-zero DWI volume count from the .bval sidecar.")
    volumes = nifti_volume_count(dwi)
    if volumes is not None and volumes != gradient_count:
        raise RuntimeError(f"The DWI has {volumes} volumes but {bval.name} lists {gradient_count} b-values.")
    (out / "index.txt").write_text(" ".join("1" for _ in range(gradient_count)) + "\n", encoding="ascii")
    # Synb0 needs a b=0 image, which is not always volume 0. b < 50 is b=0, as step 2
    # rounds shells to the nearest 100.
    b0_index = next((i for i, b in enumerate(bval.read_text(encoding="utf-8-sig").split()) if float(b) < 50), None)
    if b0_index is None:
        raise RuntimeError(f"{bval.name} has no b=0 volume (b < 50) for Synb0.")

    actions.extend([
        [str(commands["fslroi"]), str(dwi_gz), str(out / "b0.nii.gz"), str(b0_index), "1"],
        [str(commands["bet"]), str(t1_gz), str(out / "t1_bet.nii.gz"), "-R", "-f", "0.35", "-m"],
    ])
    # b0 is produced by the preceding action; bind the DWI working directory as
    # Synb0's input so T1.nii.gz, b0.nii.gz, and acqparams.txt share one mount.
    if builtin_synb0:
        # Same steps and files as the container's `--notopup` run, with the folders as arguments.
        actions.append([str(builtin_synb0), str(out), str(synb0_out), str(license_path)])
    elif Path(str(container)).name == "docker":
        actions.append([
            str(container), "run", "--rm",
            "-v", f"{out}:/WORK",
            "-v", f"{out}:/INPUTS",
            "-v", f"{synb0_out}:/OUTPUTS",
            "-v", f"{license_path}:/extra/freesurfer/license.txt:ro",
            synb0_image, "--notopup",
        ])
    else:
        # --cleanenv is required, not cosmetic. Synb0's pipeline.sh sets
        # FREESURFER_HOME=/extra/freesurfer and sources SetUpFreeSurfer.sh.
        # Apptainer forwards the host environment by default, so a host
        # `module load freesurfer` leaks FREESURFER_HOME/MNI_DIR/MINC_* into the
        # container, pointing FreeSurfer at paths that do not exist there;
        # mri_nu_correct.mni then fails with "nu_correct: Command not found",
        # T1_norm.mgz is never written, and every later step aborts while
        # pipeline.sh still prints FINISHED!!!.
        actions.append([
            str(container), "run", "--cleanenv",
            "-B", f"{out}:/WORK",
            "-B", f"{out}:/INPUTS",
            "-B", f"{synb0_out}:/OUTPUTS",
            "-B", f"{license_path}:/extra/freesurfer/license.txt:ro",
            synb0_image, "--notopup",
        ])
    # Note: Synb0 exits 0 even when its internal steps abort, so a missing
    # b0_u.nii.gz surfaces at the fslmerge below rather than here. A guard
    # action cannot be added without declaring another allowlisted command for
    # this stage, which execute_workflow_stage deliberately refuses.
    actions.extend([
        [str(commands["fslmerge"]), "-t", str(synb0_out / "b0_all.nii.gz"), str(synb0_out / "b0_d_smooth.nii.gz"), str(synb0_out / "b0_u.nii.gz")],
        [str(commands["topup"]), f"--imain={synb0_out / 'b0_all.nii.gz'}", f"--datain={acqparams}", "--config=b02b0.cnf", f"--out={out / 'topup_results'}", f"--iout={out / 'hifi_b0'}", f"--fout={out / 'field_hz'}", "--verbose"],
        [str(commands["fslmaths"]), str(out / "hifi_b0"), "-Tmean", str(out / "hifi_b0")],
        [str(commands["bet"]), str(out / "hifi_b0"), str(out / "hifi_b0_brain"), "-m", "-f", "0.20"],
        [str(commands["eddy"]), f"--imain={dwi_gz}", f"--mask={out / 'hifi_b0_brain_mask.nii.gz'}", f"--acqp={acqparams}", f"--index={out / 'index.txt'}", f"--bvecs={out / 'ap_dwi.bvec'}", f"--bvals={out / 'ap_dwi.bval'}", f"--topup={out / 'topup_results'}", "--niter=8", "--fwhm=10,8,4,2,0,0,0,0", "--repol", "--cnr_maps", f"--out={out / 'eddy_corrected_data'}", f"--nthr={available_cpus()}", "--verbose"],
    ])
    return actions
