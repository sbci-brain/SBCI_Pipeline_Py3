#!/usr/bin/env python3
"""Check a dataset's validation.md against the run it describes.

    python3 tests/check_validation_record.py RUN_DIR datasets/<name>/validation.md

RUN_DIR is the OUTPUT_DIR of a finished workflow/run_subject.sbatch job. The record must name
the pipeline version and image that job wrote to config/versions.json, the run must have used
this dataset's profile unchanged, and its connectomes must be there. Prints what disagrees and
exits 1, or prints "ok" and exits 0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FIELDS = ("Status", "Release", "Date", "Subject", "Profile", "Pipeline version", "Image", "Compared with", "Result")
STATUSES = ("untested", "validated")
EMPTY = ("", "—", "-")
CONNECTOMES = ("smoothed_sc_avg_*_ico4.mat", "sub_sc_avg_*_ico4.mat", "fc_avg_ico4.mat")


def read_record(path: Path) -> dict[str, str]:
    """The Field | Value table of a validation.md."""
    record = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] in FIELDS:
            record[cells[0]] = cells[1].strip("`")
    return record


def record_problems(record: dict[str, str]) -> list[str]:
    """What is wrong with a record on its own, before any run is looked at."""
    problems = [f"field missing: {name}" for name in FIELDS if name not in record]
    status = record.get("Status")
    if status not in STATUSES:
        problems.append(f"Status must be one of {', '.join(STATUSES)}, not {status!r}")
    if status == "validated":
        problems += [f"a validated record needs a value for: {name}" for name in FIELDS if record.get(name, "") in EMPTY]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record.get("Date", "")):
            problems.append("Date must be YYYY-MM-DD")
        if record.get("Profile") not in ("profile.conf", "profile.historical.conf"):
            problems.append("Profile must be profile.conf or profile.historical.conf")
    return problems


def run_problems(run: Path, record_path: Path) -> list[str]:
    """What in the run disagrees with the record."""
    record = read_record(record_path)
    problems = record_problems(record)
    if record.get("Status") != "validated":
        return problems + ["the record is not marked validated"]
    try:
        versions = json.loads((run / "config" / "versions.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return problems + [f"cannot read the run's config/versions.json: {exc}"]
    for field, key in (("Pipeline version", "pipeline_version"), ("Image", "container_version")):
        if record.get(field) != versions.get(key):
            problems.append(f"{field}: the record says {record.get(field)!r}, the run {versions.get(key)!r}")
    profile = record_path.parent / str(record.get("Profile"))
    used = run / "config" / profile.name
    if not profile.is_file() or not used.is_file():
        problems.append(f"the run's copy of the profile is missing: {used}")
    elif used.read_bytes() != profile.read_bytes():
        problems.append(f"the run used a profile that differs from {profile}")
    for pattern in CONNECTOMES:
        if not [path for path in run.glob(f"sbci_output/files/work/*/dwi_pipeline/sbci_connectome/{pattern}")
                if path.stat().st_size > 0]:
            problems.append(f"the run has no {pattern}")
    return problems


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    problems = run_problems(Path(arguments[0]), Path(arguments[1]))
    for problem in problems:
        print(problem)
    if not problems:
        print("ok")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
