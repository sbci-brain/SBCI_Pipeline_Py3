"""Helpers shared by the correction routes and the stage executor."""

from __future__ import annotations

import gzip
import os
import shutil
from pathlib import Path
from typing import Mapping, Sequence


def nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def required_file(value: str | None, label: str) -> Path:
    if not value:
        raise RuntimeError(f"Required workflow setting is missing: {label}")
    path = Path(value).expanduser().resolve()
    if not nonempty(path):
        raise RuntimeError(f"Required workflow file is missing or empty: {label}={path}")
    return path


def copy_or_convert(
    source: Path,
    destination: Path,
    commands: Mapping[str, str | None],
) -> list[str] | None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.name.endswith(".nii.gz"):
        shutil.copy2(source, destination)
        return None
    if not source.name.endswith(".nii"):
        raise RuntimeError(f"Unsupported BIDS NIfTI filename: {source}")
    with source.open("rb") as input_stream, gzip.open(destination, "wb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream)
    return None


def pe_vector(direction: str | None) -> tuple[str, str, str]:
    vectors = {
        "i": ("1", "0", "0"), "i-": ("-1", "0", "0"),
        "j": ("0", "1", "0"), "j-": ("0", "-1", "0"),
        "k": ("0", "0", "1"), "k-": ("0", "0", "-1"),
    }
    if direction not in vectors:
        raise RuntimeError(f"Unsupported or missing BIDS PhaseEncodingDirection: {direction!r}")
    return vectors[direction]


def unwarp_direction(direction: str | None) -> str:
    values = {"i": "x", "i-": "x-", "j": "y", "j-": "y-", "k": "z", "k-": "z-"}
    if direction not in values:
        raise RuntimeError(f"Unsupported or missing BIDS PhaseEncodingDirection: {direction!r}")
    return values[direction]


def available_cpus() -> int:
    """CPUs this process may use (the Slurm allocation, not the whole node)."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # not Linux
        return os.cpu_count() or 1


def missing_commands(commands: Mapping[str, str | None], names: Sequence[str]) -> list[str]:
    return [name for name in names if not commands.get(name)]
