#!/usr/bin/env python3
"""Constrained, resumable output-side executor for SBCI preprocessing stages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from workflow.planner import inventory_subject


COPY_DIRS = ("anat", "dwi", "func", "fmap", "fmri", "dwi_pipeline")
PREPARE_COMMANDS = (
    "mrconvert",
    "scil_gradients_convert",
    "scil_gradients_modify_axes",
)
CommandRunner = Callable[[Sequence[str], Path, Path], int]


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_snapshot(subject: Path) -> dict[str, Any]:
    """Hash files that may enter the work tree; never follow directory symlinks."""
    files: list[dict[str, Any]] = []
    for directory_name in COPY_DIRS:
        directory = subject / directory_name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
            files.append({
                "path": path.relative_to(subject).as_posix(),
                "size": stat.st_size,
                "sha256": sha256_file(path),
            })
    canonical = json.dumps(files, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {
        "subject": str(subject.resolve()),
        "files": files,
        "fingerprint": hashlib.sha256(canonical).hexdigest(),
    }


def make_writable(path: Path) -> None:
    """Let the owner write PATH and everything under it (folders: also enter them).

    A copy keeps its source's permissions, and the stages rewrite files of the work tree, so a
    copy of write-protected inputs has to be opened up. The inputs themselves are not touched.
    """
    for item in (path, *path.rglob("*")) if path.is_dir() else (path,):
        if item.is_symlink():
            continue
        mode = item.stat().st_mode
        wanted = mode | (stat.S_IRWXU if item.is_dir() else stat.S_IWUSR)
        if wanted != mode:
            item.chmod(wanted)


def _copy_subject(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for directory_name in COPY_DIRS:
        source_dir = source / directory_name
        if not source_dir.is_dir() or source_dir.is_symlink():
            continue
        shutil.copytree(source_dir, destination / directory_name, copy_function=shutil.copy2)
    make_writable(destination)


def ensure_work_subject(subject: Path, output_dir: Path) -> tuple[Path, dict[str, Any], bool]:
    """Create or verify an immutable-source work tree under the run output."""
    subject = subject.resolve()
    work_root = output_dir / "files" / "preprocessing_work"
    work_subject = work_root / subject.name
    provenance_path = work_root / "source_provenance.json"
    snapshot = source_snapshot(subject)
    if work_subject.exists():
        if not provenance_path.is_file():
            raise RuntimeError("Existing preprocessing work tree has no source provenance manifest.")
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("source", {}).get("fingerprint") != snapshot["fingerprint"]:
            raise RuntimeError("Source inputs changed after this preprocessing work tree was created; use a new output directory.")
        return work_subject, provenance, False
    work_root.mkdir(parents=True, exist_ok=True)
    _copy_subject(subject, work_subject)
    provenance = {
        "schemaVersion": "1.0",
        "source": snapshot,
        "workSubject": str(work_subject),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "policy": "Source files are copied once and are never passed as writable command outputs.",
    }
    write_json_atomic(provenance_path, provenance)
    return work_subject, provenance, True


@contextmanager
def execution_lock(output_dir: Path) -> Iterator[Path]:
    lock_path = output_dir / "files" / "preprocessing_work" / ".execution.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Another preprocessing execution owns {lock_path}") from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.close(descriptor)
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _required_raw_path(inventory: dict[str, Any], group: str, key: str) -> Path:
    value = inventory[group][key]
    if not value:
        raise RuntimeError(f"Required input is missing: {group}.{key}")
    return Path(value)


def build_prepare_actions(work_subject: Path, commands: dict[str, str | None]) -> list[list[str]]:
    """Build bounded argv arrays for the published ADNI prepare-data operation."""
    inventory = inventory_subject(work_subject)
    missing_programs = [name for name in PREPARE_COMMANDS if not commands.get(name)]
    if missing_programs:
        raise RuntimeError("Missing prepare-data commands: " + ", ".join(missing_programs))
    t1 = _required_raw_path(inventory, "raw", "t1")
    dwi_bval = work_subject / "dwi" / "ap_dwi.bval"
    rotated_bvec = work_subject / "dwi" / "eddy_corrected_data.eddy_rotated_bvecs"
    corrected_dwi = work_subject / "dwi" / "eddy_corrected_data.nii.gz"
    for path in (dwi_bval, rotated_bvec, corrected_dwi):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Required corrected DWI input is missing or empty: {path}")
    pipeline = work_subject / "dwi_pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    encoding = pipeline / "encoding"
    encoding_x = pipeline / "encoding_x.b"
    flipped = pipeline / "flip_x"
    # Modern scilpy's scil_gradients_convert requires an FSL .bval/.bvec pair to
    # share one basename; the eddy outputs do not (ap_dwi.bval alongside
    # eddy_corrected_data.eddy_rotated_bvecs) and it exits with
    # "fsl gradients filenames must have the same basename". Legacy scilpy was
    # permissive. Stage a matched pair here rather than widening this stage's
    # command allowlist, mirroring how the DWI-correction builder copies its
    # gradient files into the work tree.
    staged_bval = pipeline / "grad.bval"
    staged_bvec = pipeline / "grad.bvec"
    shutil.copy2(dwi_bval, staged_bval)
    shutil.copy2(rotated_bvec, staged_bvec)
    # The gradient commands get paths relative to the work subject, which is the folder they
    # run in: scilpy removes every ".b" from an output path, so with an absolute path a run
    # folder such as study.bids/ or data.backup/ made them write to a folder that does not exist.
    def rel(path: Path) -> str:
        return path.relative_to(work_subject).as_posix()

    return [
        [str(commands["mrconvert"]), str(t1), "-stride", "1,2,3", str(pipeline / "t1.nii.gz"), "-force"],
        [str(commands["mrconvert"]), str(corrected_dwi), "-stride", "1,2,3,4", str(pipeline / "data.nii.gz"), "-force"],
        [str(commands["scil_gradients_convert"]), "--input_fsl", rel(staged_bval), rel(staged_bvec), rel(encoding), "-f"],
        [str(commands["scil_gradients_modify_axes"]), rel(encoding.with_suffix(".b")), rel(encoding_x), "-1", "2", "3", "-f"],
        [str(commands["scil_gradients_convert"]), "--input_mrtrix", rel(encoding_x), rel(flipped), "-f"],
    ]


def validate_prepare_outputs(work_subject: Path) -> dict[str, Any]:
    pipeline = work_subject / "dwi_pipeline"
    outputs = {
        "t1": pipeline / "t1.nii.gz",
        "dwi": pipeline / "data.nii.gz",
        "bval": pipeline / "flip_x.bval",
        "bvec": pipeline / "flip_x.bvec",
    }
    nonempty = {name: path.is_file() and path.stat().st_size > 0 for name, path in outputs.items()}
    counts = {"bvals": None, "bvecs": None, "consistent": False}
    if nonempty["bval"] and nonempty["bvec"]:
        bvals = outputs["bval"].read_text(encoding="utf-8-sig").split()
        rows = [line.split() for line in outputs["bvec"].read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        bvec_count = len(rows[0]) if len(rows) == 3 and len({len(row) for row in rows}) == 1 else None
        counts = {"bvals": len(bvals), "bvecs": bvec_count, "consistent": bool(bvals and len(bvals) == bvec_count)}
    ready = all(nonempty.values()) and counts["consistent"]
    return {
        "ready": ready,
        "outputs": {name: str(path) for name, path in outputs.items()},
        "nonempty": nonempty,
        "gradientCounts": counts,
        "message": "Prepare-data outputs passed validation." if ready else "Prepare-data outputs are missing, empty, or have inconsistent gradients.",
    }


def _action_fingerprint(actions: Sequence[Sequence[str]]) -> str:
    canonical = json.dumps([list(argv) for argv in actions], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validated_output_hashes(validation: dict[str, Any]) -> dict[str, str]:
    if not validation.get("ready"):
        return {}
    return {name: sha256_file(Path(path)) for name, path in validation["outputs"].items()}


def subprocess_runner(argv: Sequence[str], cwd: Path, log_path: Path) -> int:
    allowed = set(PREPARE_COMMANDS)
    program = Path(argv[0]).name
    if program not in allowed:
        raise RuntimeError(f"Refusing undeclared preprocessing command: {program}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        completed = subprocess.run(list(argv), cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=False)
    return completed.returncode


def execute_prepare_data(
    subject: Path,
    output_dir: Path,
    commands: dict[str, str | None],
    *,
    resume: bool = True,
    runner: CommandRunner = subprocess_runner,
) -> dict[str, Any]:
    """Run only the prepare-data stage and commit a marker after validation."""
    output_dir = output_dir.resolve()
    source_before = source_snapshot(subject.resolve())
    with execution_lock(output_dir):
        work_subject, provenance, created = ensure_work_subject(subject, output_dir)
        marker = output_dir / "files" / "preprocessing_work" / "state" / "prepare_data.json"
        actions = build_prepare_actions(work_subject, commands)
        action_fingerprint = _action_fingerprint(actions)
        if resume and marker.is_file():
            previous = json.loads(marker.read_text(encoding="utf-8"))
            validation = validate_prepare_outputs(work_subject)
            output_hashes = _validated_output_hashes(validation)
            if (
                previous.get("sourceFingerprint") == source_before["fingerprint"]
                and previous.get("actionFingerprint") == action_fingerprint
                and previous.get("outputHashes") == output_hashes
                and validation["ready"]
            ):
                return {
                    "stage": "prepare_data",
                    "state": "skipped_complete",
                    "workSubject": str(work_subject),
                    "workTreeCreated": created,
                    "marker": str(marker),
                    "validation": validation,
                }
        logs: list[str] = []
        for index, argv in enumerate(actions, start=1):
            log_path = output_dir / "logs" / f"preprocess_prepare_data_{index:02d}.log"
            exit_code = runner(argv, work_subject, log_path)
            logs.append(str(log_path))
            if exit_code != 0:
                return {
                    "stage": "prepare_data",
                    "state": "failed_command",
                    "exitCode": exit_code,
                    "failedAction": index,
                    "workSubject": str(work_subject),
                    "logs": logs,
                    "marker": None,
                }
        validation = validate_prepare_outputs(work_subject)
        source_after = source_snapshot(subject.resolve())
        if source_after["fingerprint"] != source_before["fingerprint"]:
            raise RuntimeError("Source subject changed during preprocessing; no completion marker was written.")
        if not validation["ready"]:
            return {
                "stage": "prepare_data",
                "state": "failed_validation",
                "workSubject": str(work_subject),
                "logs": logs,
                "validation": validation,
                "marker": None,
            }
        marker_value = {
            "schemaVersion": "1.0",
            "stage": "prepare_data",
            "state": "complete",
            "sourceFingerprint": source_before["fingerprint"],
            "actionFingerprint": action_fingerprint,
            "workSubject": str(work_subject),
            "commands": [list(argv) for argv in actions],
            "logs": logs,
            "validation": validation,
            "outputHashes": _validated_output_hashes(validation),
            "completedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "provenance": provenance["policy"],
        }
        write_json_atomic(marker, marker_value)
        return {
            "stage": "prepare_data",
            "state": "complete",
            "workSubject": str(work_subject),
            "workTreeCreated": created,
            "logs": logs,
            "marker": str(marker),
            "validation": validation,
        }
