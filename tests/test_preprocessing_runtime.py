from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow.preprocessing_runtime import (
    COMPONENTS, candidate_bin_dirs, inspect_preprocessing_runtime, resolve_command,
)


class PreprocessingRuntimeTests(unittest.TestCase):
    def test_missing_auto_components_are_installable_but_license_components_are_not(self) -> None:
        # This test models a clean host even when the developer has already
        # provisioned CyberNeuro's known ~/.cyberneuro bin directories.
        with mock.patch("workflow.preprocessing_runtime.resolve_command", return_value=None):
            result = inspect_preprocessing_runtime(path_value="")
        self.assertEqual(["fsl", "ants", "mrtrix3"], result["installableComponents"])
        blocked = {row["component"] for row in result["blockedUserInput"]}
        self.assertIn("freesurfer", blocked)
        self.assertIn("scilpy_preprocessing", blocked)
        self.assertIn("synb0", blocked)
        self.assertNotIn("psc", blocked)
        self.assertTrue(result["inspectionOnly"])

    def test_known_cyberneuro_bin_is_used_without_path_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "antsRegistration"
            binary.write_text("stub", encoding="utf-8")
            binary.chmod(0o755)
            result = inspect_preprocessing_runtime(extra_bin_dirs=(Path(temp),), path_value="")
            self.assertEqual(str(binary), result["commands"]["antsRegistration"])

    @unittest.skipUnless(os.name == "posix", "executable bits are POSIX-only")
    def test_a_non_executable_file_is_not_an_installed_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "antsRegistration").write_text("stub", encoding="utf-8")
            self.assertIsNone(resolve_command("antsRegistration", [Path(temp)], path_value=""))

    def test_discovery_honours_cyberneuro_home_like_the_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"CYBERNEURO_HOME": temp}):
            dirs = candidate_bin_dirs()
        self.assertIn(Path(temp) / "envs" / "ants" / "bin", dirs)
        self.assertNotIn(Path.home() / ".cyberneuro" / "envs" / "ants" / "bin", dirs)

    def test_prepared_input_does_not_require_raw_preprocessing_runtime(self) -> None:
        result = inspect_preprocessing_runtime(preprocessing_required=False, path_value="")
        self.assertTrue(result["ready"])
        self.assertFalse(result["preprocessingRequired"])
        self.assertEqual([], result["blockedUserInput"])
        self.assertEqual("not_required", result["synb0"]["status"])
        self.assertFalse(any(row["required"] for row in result["components"].values()))

    def test_fully_configured_core_runtime_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            required_ids = ("fsl", "ants", "mrtrix3", "freesurfer", "scilpy_preprocessing")
            for component_id in required_ids:
                for command in (*COMPONENTS[component_id]["commands"], "docker"):
                    (root / command).write_text("stub", encoding="utf-8")
                    (root / command).chmod(0o755)
            license_path = root / "license.txt"
            license_path.write_text("configured", encoding="utf-8")
            result = inspect_preprocessing_runtime(
                freesurfer_license=license_path,
                synb0_image="example/synb0:validated",
                extra_bin_dirs=(root,),
                path_value="",
            )
            self.assertTrue(result["ready"])
            self.assertEqual([], result["installableComponents"])
            self.assertEqual([], result["blockedUserInput"])
            self.assertEqual("configured", result["synb0"]["status"])

    def test_builtin_synb0_needs_no_image_or_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "synb0").write_text("stub", encoding="utf-8")
            (root / "synb0").chmod(0o755)
            result = inspect_preprocessing_runtime(extra_bin_dirs=(root,), path_value="")
            self.assertEqual("configured", result["synb0"]["status"])
            self.assertEqual(str(root / "synb0"), result["synb0"]["runner"])


if __name__ == "__main__":
    unittest.main()
