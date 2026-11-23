#!/usr/bin/env python3
"""Entry point of the SBCI pipeline: one call inspects a subject or runs its next stage.

    python3 run_pipeline.py --input INPUT_DIR --output OUTPUT_DIR

INPUT_DIR holds one subject folder and sbci_local.json; OUTPUT_DIR receives the work tree,
logs and local_adapter_manifest.json. workflow/run_subject.sbatch calls this once per stage,
inside the sbci-full image, and is the usual way to run a subject (README.md).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# The stage scripts are started with bash and the converter shims are found through PATH, so
# both must be executable; some ways of copying a folder (zip uploads, some sync tools) drop
# the bits, and a shim without them silently loses to whatever comes next on PATH.
EXECUTABLE_GLOBS = ("workflow/compat_bin/*", "pipeline/**/*.sh", "pipeline/**/*.py", "preprocessing/*.sh")


def _restore_executable_bits(root: Path) -> None:
    for pattern in EXECUTABLE_GLOBS:
        for path in root.glob(pattern):
            if path.is_file() and not os.access(path, os.X_OK):
                try:
                    path.chmod(path.stat().st_mode | 0o111)
                except OSError:
                    pass  # read-only install: the stage that needs it reports the failure


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if os.name == "nt":
        print("The SBCI pipeline runs on Linux: start it from WSL, or run the sbci-full image.", file=sys.stderr)
        return 2
    _restore_executable_bits(Path(__file__).resolve().parent)
    from workflow.local_pipeline import main as local_main

    return local_main(effective_argv)


if __name__ == "__main__":
    raise SystemExit(main())
