from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow.c3_asset import AssetError, inspect_c3_asset, materialize_c3_main


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SHA256 = "03109ebc87781fada22bc4cd62b52151920eafa0f753b8c2607d2363aeaf309a"


class C3AssetTests(unittest.TestCase):
    def test_source_package_contains_three_verified_upload_chunks(self) -> None:
        report = inspect_c3_asset(ROOT)
        self.assertTrue(report["ready"])
        self.assertEqual(3, len(report["parts"]))
        self.assertEqual(40006712, report["bytes"])
        self.assertEqual(EXPECTED_SHA256, report["sha256"])
        self.assertLessEqual(max(row["bytes"] for row in report["parts"]), 14000000)
        self.assertFalse((ROOT / "data" / "concon" / "c3_main").exists())

    def test_materialization_is_exact_executable_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "runtime" / "concon" / "c3_main"
            first = materialize_c3_main(ROOT, target)
            second = materialize_c3_main(ROOT, target)
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(EXPECTED_SHA256, first["sha256"])
            self.assertEqual(40006712, target.stat().st_size)
            if os.name != "nt":
                self.assertTrue(target.stat().st_mode & 0o111)

    def test_invalid_existing_destination_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "c3_main"
            target.write_bytes(b"not-the-kernel")
            with self.assertRaisesRegex(AssetError, "Refusing to replace"):
                materialize_c3_main(ROOT, target)
            self.assertEqual(b"not-the-kernel", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
