#!/usr/bin/env python3
"""Exercise real WSL symlinks and prove generated structure maps cannot reach source data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-root", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    pipeline_root = Path(args.pipeline_root).resolve()
    sys.path.insert(0, str(pipeline_root))
    from workflow.local_pipeline import prepare_work_tree

    subject = Path(args.subject).resolve()
    work_subject = Path(args.output).resolve() / subject.name
    prepare_work_tree(subject, work_subject)
    source_structure = subject / "dwi_pipeline" / "structure"
    target_dwi = work_subject / "dwi_pipeline"
    target_structure = target_dwi / "structure"
    assert target_structure.is_dir() and not target_structure.is_symlink()
    assert (target_structure / "map_wm.nii.gz").is_symlink()
    assert (target_structure / "t1_freesurfer").is_symlink()
    assert (target_dwi / "diffusion").is_symlink()
    assert (work_subject / "fmri").is_symlink()
    generated = target_structure / "set_map_include.nii.gz"
    generated.write_bytes(b"staging-only")
    assert generated.is_file()
    assert not (source_structure / generated.name).exists()
    print("staging-isolation-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
