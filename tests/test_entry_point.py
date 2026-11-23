import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_pipeline


class EntryPointTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "executable bits are POSIX-only")
    def test_entry_point_restores_executable_bits(self) -> None:
        """A copy of the folder made without executable bits (seen 2026-10-10) must still run:
        the compat_bin shims are found through PATH, so they must be +x again."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = [
                root / "workflow" / "compat_bin" / "mrconvert",
                root / "preprocessing" / "preproc_step4_fodf_estimation.sh",
                root / "pipeline" / "sbci_step2_prepare_set.sh",
                root / "pipeline" / "get_coords.py",
            ]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o666)
            with mock.patch("workflow.local_pipeline.main", return_value=0), \
                    mock.patch.object(run_pipeline, "__file__", str(root / "run_pipeline.py")):
                self.assertEqual(0, run_pipeline.main(["--input", temp, "--output", temp]))
            for path in files:
                self.assertTrue(os.access(path, os.X_OK), path)


if __name__ == "__main__":
    unittest.main()
