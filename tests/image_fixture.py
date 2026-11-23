"""A stand-in for what the sbci-full image provides through its variables: here only the MNI
template folder (SBCI_TEMPLATE_DIR), which the preflight checks before registration."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_state: dict[str, str | None] = {}


def install() -> Path:
    """Create an empty-but-present template and point SBCI_TEMPLATE_DIR at it (setUpModule)."""
    folder = Path(tempfile.mkdtemp(prefix="sbci-template-"))
    (folder / "t1").mkdir()
    for name in ("t1_template.nii.gz", "t1_brain_probability_map.nii.gz"):
        (folder / "t1" / name).write_bytes(b"template")
    _state.update(folder=str(folder), previous=os.environ.get("SBCI_TEMPLATE_DIR"), templatedir=os.environ.pop("TEMPLATEDIR", None))
    os.environ["SBCI_TEMPLATE_DIR"] = str(folder)
    return folder


def remove() -> None:
    """Undo install() (tearDownModule)."""
    shutil.rmtree(str(_state.get("folder")), ignore_errors=True)
    for name, key in (("SBCI_TEMPLATE_DIR", "previous"), ("TEMPLATEDIR", "templatedir")):
        if _state.get(key) is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(_state[key])
