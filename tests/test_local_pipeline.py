from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workflow import local_pipeline

sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_fixture  # noqa: E402


def setUpModule() -> None:
    image_fixture.install()


def tearDownModule() -> None:
    image_fixture.remove()



COMMANDS_FOR_PREPROCESS_TEST = {
    "mrconvert": "/mock/bin/mrconvert",
    "scil_gradients_convert": "/mock/bin/scil_gradients_convert",
    "scil_gradients_modify_axes": "/mock/bin/scil_gradients_modify_axes",
}


class LocalPipelineTests(unittest.TestCase):
    def test_runtime_python_falls_back_to_user_sbci_conda_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            user_python = root / ".conda" / "envs" / "sbci_py3" / "bin" / "python"
            user_python.parent.mkdir(parents=True)
            user_python.write_bytes(b"python")
            successful_probe = mock.Mock(returncode=0, stdout="", stderr="")
            with (
                mock.patch.dict(local_pipeline.os.environ, {}, clear=True),
                mock.patch.object(local_pipeline, "RUNTIME_PYTHON_DEFAULT", root / "missing" / "python"),
                mock.patch.object(local_pipeline.subprocess, "run", return_value=successful_probe),
            ):
                resolved = local_pipeline.resolve_runtime_python(home=root)
            self.assertEqual(user_python, resolved["path"])
            self.assertEqual("user_conda_sbci_py3", resolved["source"])
            self.assertTrue(resolved["ready"])

    def test_finds_cleaned_final_session_and_routes_it_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "arbitrary" / "sub-001" / "ses-20200101"
            connectome = session / "psc_sbci_final_files" / "sbci_connectome"
            connectome.mkdir(parents=True)
            (connectome / "smoothed_sc_avg_0.005_ico4.mat").write_bytes(b"sc")
            (connectome / "fc_avg_ico4.mat").write_bytes(b"fc")
            self.assertEqual([session], local_pipeline.find_subjects(root / "arbitrary"))
            inventory = local_pipeline.inventory_subject(session)
            self.assertEqual("completed_connectivity", local_pipeline.resolved_input_profile("auto", session, inventory))

    def test_finds_session_below_arbitrarily_named_preprocessed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            session = data_root / "prepared-for-analysis" / "sub-001" / "ses-20200101"
            (session / "dwi_pipeline").mkdir(parents=True)
            self.assertEqual([session], local_pipeline.find_subjects(data_root / "prepared-for-analysis"))
            self.assertEqual(
                "sbci_ready",
                local_pipeline.resolved_input_profile("auto", session, {"sbciReady": True}),
            )

    def test_deprecated_py3_profile_is_a_layout_alias(self) -> None:
        self.assertEqual(
            "preprocessed",
            local_pipeline.resolved_input_profile("py3_preprocessed", None, None),
        )

    def test_confirmed_preprocess_dispatches_raw_bids_correction_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            subject = input_dir / "sub-001"
            output_dir = root / "output"
            (subject / "anat").mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"t1")
            (input_dir / "sbci_local.json").write_text(json.dumps({
                "schema_version": "1.0", "mode": "preprocess", "approved": True,
                "workflow": {"resume": True, "synb0_image": "synb0.sif", "freesurfer_license": "license.txt"},
            }), encoding="utf-8")
            inventory = {
                "raw": {"t1": "t1", "dwi": "dwi", "bold": "bold"},
                "completed": {}, "sbciReady": False, "pscReady": False,
            }
            plan = {"readyToRun": True, "stages": [
                {"id": "dwi_correction", "state": "pending", "missingCommands": []},
            ], "blockers": []}
            execution = {
                "stage": "dwi_correction", "state": "complete",
                "workSubject": str(output_dir / "files" / "preprocessing_work" / "sub-001"),
                "validation": {"ready": True},
            }
            with (
                mock.patch.object(local_pipeline, "inventory_subject", return_value=inventory),
                mock.patch.object(local_pipeline, "plan_workflow", return_value=plan),
                mock.patch.object(local_pipeline, "probe_commands", return_value={"commands": {"eddy": "/bin/eddy"}}),
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                mock.patch.object(local_pipeline, "execute_workflow_stage_safely", return_value=execution) as execute,
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(0, exit_code)
            execute.assert_called_once()
            self.assertEqual("dwi_correction", execute.call_args.args[0])
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("completed_preprocessing_stage", manifest["status"])
            self.assertTrue(manifest["workflowStageRan"])

    def test_confirmed_preprocess_consumes_validated_handoff_as_one_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            subject = input_dir / "sub-001"
            output_dir = root / "output"
            (subject / "anat").mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"raw-t1")
            producer = root / "producer"
            producer.mkdir()
            payloads = {
                "correctedDwi": b"corrected-dwi",
                "rotatedBvec": b"rotated-bvec",
                "bval": b"0 1000",
                "fieldHz": b"field-hz",
                "hifiB0": b"hifi-b0",
            }
            artifacts = []
            for role, payload in payloads.items():
                path = producer / role
                path.write_bytes(payload)
                artifacts.append({"role": role, "path": path.name})
            handoff = {
                "schema_version": "1.0", "handoff_id": "producer-run-1",
                "producer": {"skill": "FSL", "stage": "dwi_correction", "environment": "fsl-container"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": artifacts,
                "provenance": {"command": "producer-command", "container": "image", "completed_at": "now"},
            }
            handoff_path = producer / "handoff.json"
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            (input_dir / "sbci_local.json").write_text(json.dumps({
                "schema_version": "1.0", "mode": "preprocess", "approved": True,
                "workflow": {
                    "resume": True,
                    "orchestration": {
                        "mode": "manual_shared_artifacts",
                        "handoff_manifest": str(handoff_path),
                        "consume_handoff": True,
                    },
                },
            }), encoding="utf-8")
            runtime = {"commands": {}}
            with (
                mock.patch.object(local_pipeline, "probe_commands", return_value=runtime),
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                mock.patch.object(local_pipeline, "execute_workflow_stage_safely") as execute,
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])

            self.assertEqual(0, exit_code)
            execute.assert_not_called()
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("completed_preprocessing_stage", manifest["status"])
            self.assertEqual("dwi_correction", manifest["preprocessing"]["stage"])
            self.assertEqual("complete", manifest["preprocessing"]["state"])
            self.assertEqual(
                "validated_handoff_copy",
                json.loads(Path(manifest["preprocessing"]["marker"]).read_text(encoding="utf-8"))["completionKind"],
            )
            self.assertEqual("fmri_correction", manifest["workflow"]["executor"]["nextStage"]["id"])
            self.assertFalse(manifest["scientificAlgorithmRan"])

    def test_confirmed_preprocess_mode_runs_only_the_next_enabled_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            subject = input_dir / "sub-001"
            output_dir = root / "output"
            (subject / "dwi_pipeline").mkdir(parents=True)
            (input_dir / "sbci_local.json").write_text(json.dumps({
                "schema_version": "1.0",
                "mode": "preprocess",
                "approved": True,
                "workflow": {"resume": True},
            }), encoding="utf-8")
            inventory = {
                "raw": {"t1": "t1", "dwi": "dwi", "bold": "bold"},
                "completed": {},
                "sbciReady": False,
                "pscReady": False,
            }
            plan = {
                "readyToRun": True,
                "stages": [
                    {"id": "dwi_correction", "state": "complete"},
                    {"id": "fmri_correction", "state": "complete"},
                    {"id": "prepare_data", "state": "pending", "missingCommands": []},
                ],
                "blockers": [],
            }
            execution = {
                "stage": "prepare_data",
                "state": "complete",
                "workSubject": str(output_dir / "files" / "preprocessing_work" / "sub-001"),
                "validation": {"ready": True},
            }
            with (
                mock.patch.object(local_pipeline, "inventory_subject", return_value=inventory),
                mock.patch.object(local_pipeline, "plan_workflow", return_value=plan),
                mock.patch.object(local_pipeline, "probe_commands", return_value={"commands": COMMANDS_FOR_PREPROCESS_TEST}),
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                mock.patch.object(local_pipeline, "execute_prepare_data", return_value=execution) as execute,
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(0, exit_code)
            execute.assert_called_once()
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("completed_preprocessing_stage", manifest["status"])
            self.assertTrue(manifest["preprocessingStageRan"])
            self.assertFalse(manifest["scientificAlgorithmRan"])

    def test_profile_that_does_not_fit_stops_before_any_stage(self) -> None:
        """The preflight and a directly submitted job must both stop; FSFast would
        otherwise find the wrong TR only after FreeSurfer has run for hours."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            (input_dir / "sub-001" / "dwi_pipeline").mkdir(parents=True)
            inventory = {"raw": {"t1": "t1", "dwi": "dwi", "bold": "bold"}, "completed": {},
                         "sbciReady": False, "pscReady": False,
                         "rawMetadata": {"bold": {"repetitionTime": 3.0}}}
            for mode, approved in (("inspect", False), ("preprocess", True)):
                with self.subTest(mode=mode):
                    output_dir = root / mode
                    (input_dir / "sbci_local.json").write_text(json.dumps({
                        "schema_version": "1.0",
                        "mode": mode,
                        "approved": approved,
                        "workflow": {
                            "distortion_corrected_input": True,
                            "preprocessing_config": str(ROOT / "datasets" / "abcd" / "profile.conf"),
                        },
                    }), encoding="utf-8")
                    plan = {
                        "readyToRun": True,
                        "stages": [
                            {"id": "dwi_correction", "state": "pending", "missingCommands": []},
                            {"id": "fsfast", "state": "pending", "missingCommands": []},
                        ],
                        "blockers": [],
                    }
                    with (
                        mock.patch.object(local_pipeline, "inventory_subject", return_value=inventory),
                        mock.patch.object(local_pipeline, "plan_workflow", return_value=plan),
                        mock.patch.object(local_pipeline, "probe_commands", return_value={"commands": COMMANDS_FOR_PREPROCESS_TEST}),
                        mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                        mock.patch.object(local_pipeline, "execute_workflow_stage_safely") as execute,
                    ):
                        local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
                    execute.assert_not_called()
                    manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text(encoding="utf-8"))
                    self.assertEqual("documented_blocked_local_preflight", manifest["status"])
                    self.assertFalse(manifest["workflow"]["plan"]["readyToRun"])
                    self.assertEqual("profile_mismatch", manifest["workflow"]["plan"]["blockers"][0]["code"])
                    self.assertIn("RepetitionTime is 3 s", " ".join(row["message"] for row in manifest["issues"]))

    def test_multishell_registration_without_shell_policy_fails_preflight(self) -> None:
        """The silent failure this guards against exited 0 having used one shell
        of three; it must instead stop, exit non-zero, and say why in issues."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            subject = input_dir / "sub-001"
            output_dir = root / "output"
            (subject / "dwi_pipeline").mkdir(parents=True)
            (subject / "dwi_pipeline" / "flip_x.bval").write_text("0 1000 2000 3000 0 1000\n", encoding="ascii")
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "preproc_step2_t1_dwi_registration.sh").write_bytes(
                b'#!/bin/bash\ntouch "$CYBERNEURO_WORK_SUBJECT/stage_ran"\n'
            )
            shipped = (ROOT / "datasets" / "adni" / "profile.conf").read_text(encoding="utf-8")
            config = root / "sbci_config"
            config.write_text(
                "\n".join(l for l in shipped.splitlines() if not l.startswith("SHELL_POLICY=")) + "\n",
                encoding="utf-8",
            )
            (input_dir / "sbci_local.json").write_text(json.dumps({
                "schema_version": "1.0",
                "mode": "preprocess",
                "approved": True,
                "workflow": {
                    "resume": True,
                    "reference_scripts_dir": str(scripts),
                    "preprocessing_config": str(config),
                },
            }), encoding="utf-8")
            inventory = {"raw": {"t1": "t1", "dwi": "dwi", "bold": "bold"}, "completed": {},
                         "sbciReady": False, "pscReady": False}
            plan = {
                "readyToRun": True,
                "stages": [
                    {"id": "prepare_data", "state": "complete"},
                    {"id": "t1_dwi_registration", "state": "pending", "missingCommands": []},
                ],
                "blockers": [],
            }
            with (
                mock.patch.object(local_pipeline, "inventory_subject", return_value=inventory),
                mock.patch.object(local_pipeline, "plan_workflow", return_value=plan),
                mock.patch.object(local_pipeline, "probe_commands", return_value={"commands": COMMANDS_FOR_PREPROCESS_TEST}),
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(1, exit_code)
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("failed_preprocessing_stage", manifest["status"])
            self.assertFalse(manifest["workflowStageRan"])
            messages = " ".join(row["message"] for row in manifest["issues"])
            self.assertIn("b = 1000, 2000, 3000", messages)
            self.assertIn("SHELL_POLICY", messages)
            self.assertEqual([], list(output_dir.rglob("stage_ran")), "the stage script ran")

    def test_orchestration_plan_is_rewritten_after_a_stage_completes(self) -> None:
        """The written plan must not still name the stage that just finished.
        Observed live: plan written 07:17 said nextStage fsfast, fsfast finished
        07:35, and the file was never refreshed."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            subject = input_dir / "sub-001"
            output_dir = root / "output"
            (subject / "dwi_pipeline").mkdir(parents=True)
            (input_dir / "sbci_local.json").write_text(json.dumps({
                "schema_version": "1.0",
                "mode": "preprocess",
                "approved": True,
                "workflow": {"resume": True},
            }), encoding="utf-8")
            inventory = {
                "raw": {"t1": "t1", "dwi": "dwi", "bold": "bold"},
                "completed": {},
                "sbciReady": False,
                "pscReady": False,
            }
            before = {
                "readyToRun": True,
                "stages": [
                    {"id": "dwi_correction", "state": "complete"},
                    {"id": "fmri_correction", "state": "complete"},
                    {"id": "prepare_data", "state": "pending", "missingCommands": []},
                ],
                "blockers": [],
            }
            # what the planner sees once prepare_data has actually run
            after = {
                "readyToRun": True,
                "stages": [
                    {"id": "dwi_correction", "state": "complete"},
                    {"id": "fmri_correction", "state": "complete"},
                    {"id": "prepare_data", "state": "complete"},
                    {"id": "t1_dwi_registration", "state": "pending", "missingCommands": []},
                ],
                "blockers": [],
            }
            execution = {
                "stage": "prepare_data",
                "state": "complete",
                "workSubject": str(output_dir / "files" / "preprocessing_work" / "sub-001"),
                "validation": {"ready": True},
            }
            with (
                mock.patch.object(local_pipeline, "inventory_subject", return_value=inventory),
                mock.patch.object(local_pipeline, "plan_workflow", side_effect=[before, after, after]),
                mock.patch.object(local_pipeline, "probe_commands", return_value={"commands": COMMANDS_FOR_PREPROCESS_TEST}),
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                mock.patch.object(local_pipeline, "execute_prepare_data", return_value=execution),
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(0, exit_code)
            written = json.loads(
                (output_dir / "files" / "orchestration_plan.json").read_text(encoding="utf-8")
            )
            next_stage = written.get("nextStage")
            name = next_stage.get("id") if isinstance(next_stage, dict) else next_stage
            self.assertNotEqual(
                "prepare_data", name,
                "orchestration_plan.json still names the stage that just completed",
            )

    def test_missing_input_produces_friendly_manifest_and_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            with mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": False}):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(0, exit_code)
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text())
            self.assertEqual("documented_blocked_local_preflight", manifest["status"])
            self.assertTrue((output_dir / "files" / "sbci_local.template.json").is_file())
            self.assertTrue((output_dir / "reports" / "sbci_local_report.html").is_file())

    def test_quick_mode_uses_explicit_non_scientific_parameters(self) -> None:
        params, errors = local_pipeline.parameter_values({"mode": "quick_test"})
        self.assertEqual([], errors)
        self.assertEqual(1, params["n_runs"])
        self.assertEqual(1, params["n_seed"])
        self.assertEqual(5, params["steps"])

    def test_self_test_needs_no_subject_and_keeps_scientific_flags_false(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "sbci_local.json").write_text(
                json.dumps({"schema_version": "1.0", "mode": "self_test", "approved": False})
            )
            result = {
                "ready": True,
                "patientDataUsed": False,
                "scientificAlgorithmRan": False,
                "exitCode": 0,
                "log": str(output_dir / "logs" / "runtime_self_test.log"),
            }
            with (
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                mock.patch.object(local_pipeline, "run_runtime_self_test", return_value=result),
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(0, exit_code)
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text())
            self.assertEqual("completed_local_runtime_self_test", manifest["status"])
            self.assertTrue(manifest["runtimeSelfTestRan"])
            self.assertFalse(manifest["inspectionOnly"])
            self.assertFalse(manifest["scientificAlgorithmRan"])
            self.assertFalse(manifest["officialScientificPipelineRan"])
            self.assertFalse(any(row["code"] == "missing_subject" for row in manifest["issues"]))

    def test_failed_self_test_is_a_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "sbci_local.json").write_text(
                json.dumps({"schema_version": "1.0", "mode": "self_test", "approved": False})
            )
            result = {
                "ready": False,
                "message": "A runtime check failed.",
                "exitCode": 1,
                "log": str(output_dir / "logs" / "runtime_self_test.log"),
            }
            with (
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                mock.patch.object(local_pipeline, "run_runtime_self_test", return_value=result),
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(1, exit_code)
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text())
            self.assertEqual("failed_runtime_self_test", manifest["status"])
            self.assertFalse(manifest["completed"])

    def test_production_parameters_are_not_silently_reduced(self) -> None:
        params, errors = local_pipeline.parameter_values({"mode": "production", "parameters": {"n_runs": 7}})
        self.assertEqual([], errors)
        self.assertEqual(7, params["n_runs"])
        self.assertEqual(local_pipeline.DEFAULT_PARAMETERS["n_seed"], params["n_seed"])

    def test_generated_config_is_local_and_contains_reviewed_values(self) -> None:
        text = local_pipeline.generated_config(ROOT, Path("/tmp/grid"), local_pipeline.DEFAULT_PARAMETERS)
        self.assertIn("N_RUNS=4", text)
        self.assertIn("N_SEED=1000", text)
        self.assertIn("CONCON_PATH=", text)
        self.assertIn(f"CONCON_PATH='{ROOT / 'data' / 'concon'}'", text)
        self.assertIn(f"REFDIR='{ROOT / 'data' / 'fsaverage'}'", text)
        self.assertIn(f"OUTPUT_PATH='{Path('/tmp/grid')}'", text)
        self.assertNotIn("/proj/grwulab", text.lower())
        self.assertNotIn("sbatch", text.lower())

    def test_only_validated_ico4_resolution_is_accepted(self) -> None:
        _, errors = local_pipeline.parameter_values({"mode": "production", "parameters": {"resolution": "ico5"}})
        self.assertTrue(any(row["code"] == "invalid_parameter" for row in errors))

    def test_work_tree_keeps_structure_container_writable_without_linking_generated_maps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "subject"
            structure = subject / "dwi_pipeline" / "structure"
            (subject / "dwi_pipeline" / "diffusion").mkdir(parents=True)
            (subject / "fmri").mkdir()
            (structure / "t1_freesurfer").mkdir(parents=True)
            (structure / "map_wm.nii.gz").write_bytes(b"input")
            (structure / "set_map_include.nii.gz").write_bytes(b"stale-output")
            work_subject = root / "work" / "subject"
            with mock.patch.object(local_pipeline, "link_path") as link:
                local_pipeline.prepare_work_tree(subject, work_subject)
            target_structure = work_subject / "dwi_pipeline" / "structure"
            self.assertTrue(target_structure.is_dir())
            linked_sources = {call.args[0].name for call in link.call_args_list}
            self.assertIn("t1_freesurfer", linked_sources)
            self.assertIn("map_wm.nii.gz", linked_sources)
            self.assertNotIn("set_map_include.nii.gz", linked_sources)

    def test_a_retry_sets_the_previous_sbci_attempt_aside_instead_of_crashing(self) -> None:
        """A failed or interrupted SBCI run left files/work/<subject>; the retry used to raise
        FileExistsError (review 2026-10-05, finding 9)."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "subject"
            (subject / "dwi_pipeline" / "diffusion").mkdir(parents=True)
            (subject / "dwi_pipeline" / "structure").mkdir()
            (subject / "fmri").mkdir()
            work_subject = root / "work" / "subject"
            (work_subject / "dwi_pipeline").mkdir(parents=True)
            (work_subject / "dwi_pipeline" / "partial.log").write_text("first attempt", encoding="utf-8")
            with mock.patch.object(local_pipeline, "link_path"):
                retired = local_pipeline.start_work_tree(subject, work_subject)
            self.assertTrue(retired.name.startswith("subject.attempt-"))
            self.assertEqual("first attempt", (retired / "dwi_pipeline" / "partial.log").read_text(encoding="utf-8"))
            self.assertTrue((work_subject / "dwi_pipeline").is_dir())
            self.assertFalse((work_subject / "dwi_pipeline" / "partial.log").exists())

    def test_failed_subject_metadata_probe_blocks_scientific_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            subject = input_dir / "ADNI-subject"
            output_dir = root / "output"
            (subject / "dwi_pipeline").mkdir(parents=True)
            (input_dir / "sbci_local.json").write_text(
                json.dumps({"schema_version": "1.0", "mode": "quick_test", "approved": False})
            )
            metadata = {
                "ready": False,
                "message": "ADNI/SBCI metadata preflight found 1 blocker(s).",
                "exitCode": 1,
                "log": str(output_dir / "logs" / "subject_metadata_probe.log"),
            }
            inventory = {
                "raw": {"t1": None, "dwi": None, "bold": None},
                "completed": {
                    "dwi_correction": True,
                    "fmri_correction": True,
                    "prepare_data": True,
                    "t1_dwi_registration": True,
                    "freesurfer": True,
                    "fodf_tissue": True,
                    "fsfast": True,
                    "psc": False,
                    "sbci": False,
                },
                "sbciReady": True,
                "pscReady": True,
            }
            with (
                mock.patch.object(local_pipeline, "inventory_subject", return_value=inventory),
                mock.patch.object(local_pipeline, "inspect_subject", return_value=[]),
                mock.patch.object(local_pipeline, "runtime_probe", return_value={"ready": True}),
                mock.patch.object(local_pipeline, "run_subject_metadata_probe", return_value=metadata),
                mock.patch.object(local_pipeline, "execute_local") as execute,
            ):
                exit_code = local_pipeline.main(["--input", str(input_dir), "--output", str(output_dir)])
            self.assertEqual(0, exit_code)
            execute.assert_not_called()
            manifest = json.loads((output_dir / "local_adapter_manifest.json").read_text())
            self.assertEqual("documented_blocked_local_preflight", manifest["status"])
            self.assertTrue(any(row["code"] == "subject_metadata_failed" for row in manifest["issues"]))

    def test_stage_output_streams_to_log_and_missing_command_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            success = local_pipeline.run_stage(
                "stream",
                [sys.executable, "-c", "print('stage-output')"],
                root,
                dict(local_pipeline.os.environ),
                root / "logs",
            )
            self.assertEqual(0, success["exitCode"])
            self.assertIn("stage-output", Path(success["log"]).read_text())
            failure = local_pipeline.run_stage(
                "missing",
                [str(root / "does-not-exist")],
                root,
                dict(local_pipeline.os.environ),
                root / "logs",
            )
            self.assertEqual(127, failure["exitCode"])
            self.assertIn("Could not start stage", Path(failure["log"]).read_text())

    def test_output_validation_rejects_parseable_but_wrong_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            connectivity = Path(temp)
            bandwidth = local_pipeline.DEFAULT_PARAMETERS["bandwidth"]
            resolution = local_pipeline.DEFAULT_PARAMETERS["resolution"]
            for name in (
                f"smoothed_sc_avg_{bandwidth}_{resolution}.mat",
                f"smoothed_sc_avg_{bandwidth}_{resolution}.npz",
                f"sub_sc_avg_{bandwidth}_{resolution}.mat",
                f"fc_avg_{resolution}.mat",
            ):
                (connectivity / name).write_bytes(b"placeholder")
            parsed = {
                "structuralMat": {"finite": True, "valid": True},
                "structuralSparse": {"finite": True, "valid": True},
                "subcorticalStructural": {"finite": True, "valid": True},
                "functionalMat": {"finite": True, "valid": False},
            }
            completed = mock.Mock(returncode=0, stdout=json.dumps(parsed), stderr="")
            with mock.patch.object(local_pipeline.subprocess, "run", return_value=completed):
                result = local_pipeline.validate_connectivity(
                    connectivity,
                    local_pipeline.DEFAULT_PARAMETERS,
                    Path(sys.executable),
                )
            self.assertFalse(result["ready"])

    def test_cleaned_completed_connectivity_validation_does_not_require_deleted_npz(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            connectivity = Path(temp)
            bandwidth = local_pipeline.DEFAULT_PARAMETERS["bandwidth"]
            resolution = local_pipeline.DEFAULT_PARAMETERS["resolution"]
            for name in (
                f"smoothed_sc_avg_{bandwidth}_{resolution}.mat",
                f"sub_sc_avg_{bandwidth}_{resolution}.mat",
                f"fc_avg_{resolution}.mat",
            ):
                (connectivity / name).write_bytes(b"placeholder")
            parsed = {
                "structuralMat": {"finite": True, "valid": True},
                "subcorticalStructural": {"finite": True, "valid": True},
                "functionalMat": {"finite": True, "valid": True},
            }
            completed = mock.Mock(returncode=0, stdout=json.dumps(parsed), stderr="")
            with mock.patch.object(local_pipeline.subprocess, "run", return_value=completed):
                result = local_pipeline.validate_completed_connectivity(
                    connectivity,
                    local_pipeline.DEFAULT_PARAMETERS,
                    Path(sys.executable),
                )
            self.assertTrue(result["ready"])
            self.assertTrue(result["readOnly"])
            self.assertNotIn("structuralSparse", result["expected"])


if __name__ == "__main__":
    unittest.main()
