"""The real files, not mocks: every path the driver builds from the pipeline's layout exists.

Moving files breaks paths silently. A release once shipped a preflight that raised on every
subject because the reference surfaces had moved and each test replaced the probe with a mock.
These tests run the probe and the entry point on this checkout as it is.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workflow import local_pipeline  # noqa: E402
from workflow.c3_asset import inspect_c3_asset  # noqa: E402
from workflow.workflow_executor import PREPROCESSING_SCRIPTS, REFERENCE_SCRIPTS, template_problem  # noqa: E402

BASH = shutil.which("bash")


class RuntimeProbeTests(unittest.TestCase):
    def test_runtime_probe_runs_on_this_checkout(self) -> None:
        probe = local_pipeline.runtime_probe(ROOT)
        for key in ("pipelineScriptsExist", "conconExists", "referenceExists", "ico4GridCacheExists"):
            self.assertTrue(probe[key], key)
        for group, count in (("adapterScripts", 2), ("compatCommands", 3), ("stageWrappers", 6),
                             ("referenceFiles", 9), ("ico4GridFiles", 6)):
            self.assertEqual(count, len(probe[group]), group)
            self.assertEqual([], [name for name, found in probe[group].items() if not found], group)
        for name in (*probe["referenceFiles"], *probe["ico4GridFiles"]):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertTrue(probe["conconAsset"]["ready"], probe["conconAsset"])

    def test_concon_kernel_parts_are_where_the_manifest_says(self) -> None:
        report = inspect_c3_asset(ROOT)
        self.assertEqual("data/concon/c3_main", report["asset"])
        self.assertEqual(3, len(report["parts"]))

    def test_runtime_python_is_the_images_or_this_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            named = Path(temp) / "python"
            for environment, expected, source in (({"SBCI_CORE_PY": str(named)}, named, "environment"),
                                                  ({}, Path(sys.executable), "current_python")):
                cleared = {k: v for k, v in os.environ.items() if k != "SBCI_CORE_PY"}
                with unittest.mock.patch.dict(os.environ, {**cleared, **environment}, clear=True):
                    resolved = local_pipeline.resolve_runtime_python(home=Path(temp))
                self.assertEqual((expected, source), (resolved["path"], resolved["candidates"][0]["source"]))


class EntryPointTests(unittest.TestCase):
    def run_entry(self, input_dir: Path, output_dir: Path, **environment: str) -> tuple[int, dict]:
        env = {k: v for k, v in os.environ.items() if k not in ("SBCI_TEMPLATE_DIR", "TEMPLATEDIR", "SBCI_CORE_PY")}
        completed = subprocess.run(
            [sys.executable, str(ROOT / "run_pipeline.py"), "--input", str(input_dir), "--output", str(output_dir)],
            capture_output=True, text=True, env={**env, **environment},
        )
        self.assertNotIn("Traceback", completed.stderr)
        return completed.returncode, json.loads((output_dir / "local_adapter_manifest.json").read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "the pipeline runs on Linux")
    def test_inspect_of_a_raw_subject_runs_end_to_end_without_mocks(self) -> None:
        """What `run_subject.sbatch --inspect` runs. Outside the image the tools are missing, so
        the plan is blocked, but every stage is planned and nothing raises."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "in" / "sub-001"
            for folder in ("anat", "dwi", "func"):
                (subject / folder).mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"t1")
            (subject / "dwi" / "sub-001_dwi.nii.gz").write_bytes(b"dwi")
            (subject / "dwi" / "sub-001_dwi.bval").write_text("0 1000\n", encoding="ascii")
            (subject / "dwi" / "sub-001_dwi.bvec").write_text("0 1\n0 0\n0 0\n", encoding="ascii")
            (subject / "dwi" / "sub-001_dwi.json").write_text(
                json.dumps({"PhaseEncodingDirection": "j-", "TotalReadoutTime": 0.05}), encoding="utf-8")
            (subject / "func" / "sub-001_task-rest_bold.nii.gz").write_bytes(b"bold")
            (subject / "func" / "sub-001_task-rest_bold.json").write_text(json.dumps(
                {"RepetitionTime": 3.0, "EffectiveEchoSpacing": 0.0005, "PhaseEncodingDirection": "j-"}), encoding="utf-8")
            (root / "in" / "sbci_local.json").write_text(json.dumps({
                "schema_version": "1.0", "mode": "inspect", "approved": False,
                "workflow": {"preprocessing_config": "datasets/adni/profile.conf"},
            }), encoding="utf-8")
            code, manifest = self.run_entry(root / "in", root / "out")
            self.assertEqual(0, code)
            plan = manifest["workflow"]["plan"]
            self.assertEqual(["dwi_correction", "fmri_correction", "prepare_data", "t1_dwi_registration",
                              "freesurfer", "fodf_tissue", "fsfast", "sbci"], [row["id"] for row in plan["stages"]])
            # The profile was found by its relative name and fits this raw subject.
            self.assertEqual([], [row for row in plan["blockers"] if row.get("code") == "profile_mismatch"])
            # No image here: the template is reported missing before any stage, not minutes into one.
            self.assertIn("missing_template", [row.get("code") for row in plan["blockers"]])
            self.assertTrue(manifest["runtime"]["referenceExists"] and manifest["runtime"]["ico4GridCacheExists"])

            template = root / "template" / "t1"
            template.mkdir(parents=True)
            for name in ("t1_template.nii.gz", "t1_brain_probability_map.nii.gz"):
                (template / name).write_bytes(b"x")
            _, manifest = self.run_entry(root / "in", root / "out2", SBCI_TEMPLATE_DIR=str(template.parent))
            self.assertNotIn("missing_template", [row.get("code") for row in manifest["workflow"]["plan"]["blockers"]])

    def test_template_problem_names_what_is_missing(self) -> None:
        self.assertIn("SBCI_TEMPLATE_DIR is not set", template_problem({}))
        with tempfile.TemporaryDirectory() as temp:
            self.assertIn("lacks t1/t1_template.nii.gz", template_problem({"SBCI_TEMPLATE_DIR": temp}))
            (Path(temp) / "t1").mkdir()
            for name in ("t1_template.nii.gz", "t1_brain_probability_map.nii.gz"):
                (Path(temp) / "t1" / name).write_bytes(b"x")
            self.assertIsNone(template_problem({"SBCI_TEMPLATE_DIR": temp}))
            self.assertIsNone(template_problem({"TEMPLATEDIR": temp}))


class StageScriptTests(unittest.TestCase):
    def test_every_stage_script_and_every_script_it_calls_is_shipped(self) -> None:
        for stage, name in REFERENCE_SCRIPTS.items():
            if stage != "psc":  # optional branch, not shipped
                self.assertTrue((PREPROCESSING_SCRIPTS / name).is_file(), name)
        wrappers = sorted((ROOT / "pipeline").glob("sbci_step*.sh"))
        self.assertEqual(6, len(wrappers))
        called = set()
        for wrapper in wrappers:
            code = "\n".join(line for line in wrapper.read_text(encoding="utf-8").splitlines()
                             if not line.lstrip().startswith("#"))
            called.update(re.findall(r'\$\{SBCI_SCRIPTS\}/([A-Za-z0-9_/.]+\.py)', code))
        self.assertGreater(len(called), 20)
        self.assertEqual([], sorted(name for name in called if not (ROOT / "pipeline" / name).is_file()))

    def test_no_tool_or_install_path_is_written_in_the_code(self) -> None:
        """Tools come from PATH and the image's variables (README.md, "The image")."""
        literal = re.compile(r"""(?<![\w$}.<>])/(?:opt|nas|usr/local|apps)/[\w.-]+""")
        found = []
        for folder in ("pipeline", "preprocessing", "corrections", "workflow"):
            for path in sorted((ROOT / folder).rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".json":
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                    if literal.search(line) and not line.lstrip().startswith("#"):
                        found.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()[:120]}")
        self.assertEqual([], found)

    @unittest.skipUnless(BASH, "bash is required")
    def test_sbci_steps_find_their_scripts_and_reference_data_from_their_own_place(self) -> None:
        """Run outside the driver, a step defaults to the scripts beside it and to ../data."""
        probe = ('SBCI_CONFIG=/dev/null; set -- 1; mris_convert() { :; }; python3() { :; }; scil_surface_flip() { :; };'
                 ' eval "$(sed -n "1,/^source /p" "$1" | grep -E "^(WRAPPER_DIR|REPO_ROOT|SBCI_SCRIPTS)=")";'
                 ' echo "$SBCI_SCRIPTS|$REPO_ROOT"')
        for name in ("sbci_step1_process_grid.sh", "sbci_step6_functional.sh"):
            script = ROOT / "pipeline" / name
            text = script.read_text(encoding="utf-8")
            self.assertIn('SBCI_SCRIPTS="${SBCI_SCRIPTS:-${WRAPPER_DIR}}"', text)
            self.assertIn('REFDIR="${REPO_ROOT}/data/fsaverage"', text)
            done = subprocess.run([BASH, "-c", probe.replace('"$1"', f'"{script.as_posix()}"')],
                                  capture_output=True, text=True, cwd=script.parent,
                                  env={k: v for k, v in os.environ.items() if k != "SBCI_SCRIPTS"})
            self.assertEqual(0, done.returncode, done.stderr)
            scripts, repo = done.stdout.strip().split("|")
            self.assertTrue(Path(scripts, "calculate_fc.py").is_file(), scripts)
            self.assertTrue(Path(repo, "data", "fsaverage", "surf", "lh.white_avg").is_file(), repo)


if __name__ == "__main__":
    unittest.main()
