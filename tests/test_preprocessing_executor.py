from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow.preprocessing_executor import execute_prepare_data, source_snapshot


COMMANDS = {
    "mrconvert": "/mock/bin/mrconvert",
    "scil_gradients_convert": "/mock/bin/scil_gradients_convert",
    "scil_gradients_modify_axes": "/mock/bin/scil_gradients_modify_axes",
}


def create_corrected_subject(root: Path) -> Path:
    subject = root / "sub-001"
    (subject / "anat").mkdir(parents=True)
    (subject / "dwi").mkdir()
    (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"t1-source")
    (subject / "dwi" / "eddy_corrected_data.nii.gz").write_bytes(b"dwi-source")
    (subject / "dwi" / "ap_dwi.bval").write_text("0 1000 1000\n", encoding="utf-8")
    (subject / "dwi" / "eddy_corrected_data.eddy_rotated_bvecs").write_text(
        "1 0 0\n0 1 0\n0 0 1\n", encoding="utf-8"
    )
    return subject


class FakePrepareRunner:
    def __init__(self, *, create_outputs: bool = True) -> None:
        self.create_outputs = create_outputs
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, log_path: Path) -> int:
        values = list(argv)
        self.calls.append(values)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("synthetic command\n", encoding="utf-8")
        if not self.create_outputs:
            return 0
        program = Path(values[0]).name
        if program == "mrconvert":
            (cwd / values[-2]).write_bytes(b"synthetic-nifti")
        elif program == "scil_gradients_modify_axes":
            (cwd / values[2]).write_text("0 0 0 0\n", encoding="utf-8")
        elif program == "scil_gradients_convert" and "--input_fsl" in values:
            (cwd / (values[-2] + ".b")).write_text("0 0 0 0\n", encoding="utf-8")
        elif program == "scil_gradients_convert" and "--input_mrtrix" in values:
            (cwd / (values[-2] + ".bval")).write_text("0 1000 1000\n", encoding="utf-8")
            (cwd / (values[-2] + ".bvec")).write_text("1 0 0\n0 1 0\n0 0 1\n", encoding="utf-8")
        return 0


class PreprocessingExecutorTests(unittest.TestCase):
    def test_prepare_data_uses_output_worktree_and_writes_validated_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = create_corrected_subject(root / "input")
            output = root / "output"
            before = source_snapshot(subject)
            runner = FakePrepareRunner()
            result = execute_prepare_data(subject, output, COMMANDS, runner=runner)
            self.assertEqual("complete", result["state"])
            self.assertEqual(5, len(runner.calls))
            self.assertEqual(before["fingerprint"], source_snapshot(subject)["fingerprint"])
            marker = Path(result["marker"])
            self.assertTrue(marker.is_file())
            marker_value = json.loads(marker.read_text(encoding="utf-8"))
            self.assertTrue(marker_value["validation"]["ready"])
            self.assertNotEqual(subject, Path(result["workSubject"]))

    def test_gradient_commands_never_see_the_run_folder_path(self) -> None:
        """scilpy removes every ".b" from an output path, so a run folder named study.bids broke
        the gradient commands while they were given absolute paths."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "study.bids"
            subject = create_corrected_subject(root / "input")
            runner = FakePrepareRunner()
            result = execute_prepare_data(subject, root / "output", COMMANDS, runner=runner)
        self.assertEqual("complete", result["state"])
        gradient_calls = [call for call in runner.calls if Path(call[0]).name.startswith("scil_gradients")]
        self.assertEqual(3, len(gradient_calls))
        for call in gradient_calls:
            paths = [value for value in call[1:] if "/" in value]
            self.assertTrue(paths)
            self.assertTrue(all(value.startswith("dwi_pipeline/") for value in paths), call)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_write_protected_inputs_give_a_writable_work_tree(self) -> None:
        """The stages rewrite files of the work tree; the inputs keep their protection."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subject = create_corrected_subject(root / "input")
            protected = [subject / "anat", subject / "dwi", *subject.rglob("*.*")]
            for path in sorted(protected, key=lambda item: len(item.parts), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            try:
                result = execute_prepare_data(subject, root / "output", COMMANDS, runner=FakePrepareRunner())
                work = Path(result["workSubject"])
                copies = [work, *work.rglob("*")]
                self.assertEqual("complete", result["state"])
                self.assertTrue(all(path.stat().st_mode & stat.S_IWUSR for path in copies))
                self.assertFalse((subject / "dwi" / "ap_dwi.bval").stat().st_mode & stat.S_IWUSR)
            finally:
                for path in protected:
                    path.chmod(0o755 if path.is_dir() else 0o644)

    def test_resume_revalidates_outputs_and_skips_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = create_corrected_subject(root / "input")
            output = root / "output"
            first_runner = FakePrepareRunner()
            execute_prepare_data(subject, output, COMMANDS, runner=first_runner)
            resume_runner = FakePrepareRunner()
            result = execute_prepare_data(subject, output, COMMANDS, runner=resume_runner)
            self.assertEqual("skipped_complete", result["state"])
            self.assertEqual([], resume_runner.calls)

    def test_resume_reruns_when_a_valid_looking_output_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = create_corrected_subject(root / "input")
            output = root / "output"
            first = execute_prepare_data(subject, output, COMMANDS, runner=FakePrepareRunner())
            work_subject = Path(first["workSubject"])
            (work_subject / "dwi_pipeline" / "data.nii.gz").write_bytes(b"changed-but-nonempty")
            resume_runner = FakePrepareRunner()
            result = execute_prepare_data(subject, output, COMMANDS, runner=resume_runner)
            self.assertEqual("complete", result["state"])
            self.assertEqual(5, len(resume_runner.calls))

    def test_validation_failure_never_commits_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = create_corrected_subject(root / "input")
            output = root / "output"
            result = execute_prepare_data(subject, output, COMMANDS, runner=FakePrepareRunner(create_outputs=False))
            self.assertEqual("failed_validation", result["state"])
            self.assertIsNone(result["marker"])
            self.assertFalse((output / "files" / "preprocessing_work" / "state" / "prepare_data.json").exists())


if __name__ == "__main__":
    unittest.main()
