from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow.planner import inventory_subject, looks_like_subject, plan_workflow


class AdniWorkflowTests(unittest.TestCase):
    def test_cleaned_final_connectivity_layout_is_terminal_and_never_replanned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "sub-001" / "ses-20200101"
            connectome = session / "psc_sbci_final_files" / "sbci_connectome"
            connectome.mkdir(parents=True)
            (connectome / "smoothed_sc_avg_0.005_ico4.mat").write_bytes(b"sc")
            (connectome / "fc_avg_ico4.mat").write_bytes(b"fc")
            self.assertTrue(looks_like_subject(session))
            inventory = inventory_subject(session)
            self.assertTrue(inventory["completedConnectivity"])
            self.assertFalse(inventory["sbciReady"])
            self.assertEqual("completed_connectivity", inventory["inputLevel"])
            plan = plan_workflow(inventory, command_paths={})
            self.assertEqual([], plan["blockers"])
            self.assertTrue(all(row["state"] == "complete" for row in plan["stages"]))

    def test_raw_bids_sidecars_and_philips_metadata_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "sub-002"
            for folder in ("anat", "dwi", "func"):
                (subject / folder).mkdir(parents=True, exist_ok=True)
            (subject / "anat" / "sub-002_T1w.nii.gz").write_bytes(b"t1")
            (subject / "dwi" / "sub-002_dwi.nii.gz").write_bytes(b"dwi")
            (subject / "dwi" / "sub-002_dwi.bval").write_text("0 1000 1000\n", encoding="utf-8")
            (subject / "dwi" / "sub-002_dwi.bvec").write_text("1 0 0\n0 1 0\n0 0 1\n", encoding="utf-8")
            (subject / "dwi" / "sub-002_dwi.json").write_text(json.dumps({
                "Manufacturer": "Philips",
                "PhaseEncodingAxis": "j",
                "EstimatedEffectiveEchoSpacing": 0.0005,
                "ReconMatrixPE": 90,
            }), encoding="utf-8")
            (subject / "func" / "sub-002_task-rest_bold.nii.gz").write_bytes(b"bold")
            (subject / "func" / "sub-002_task-rest_bold.json").write_text(json.dumps({
                "RepetitionTime": 3.0,
                "PhaseEncodingDirection": "j",
            }), encoding="utf-8")
            inventory = inventory_subject(subject)
            self.assertEqual([], inventory["inputIssues"])
            self.assertEqual(3, inventory["rawMetadata"]["gradientCounts"]["bvals"])
            self.assertEqual(3, inventory["rawMetadata"]["gradientCounts"]["bvecs"])
            self.assertAlmostEqual(0.0445, inventory["rawMetadata"]["dwi"]["totalReadoutTime"])
            self.assertEqual(3.0, inventory["rawMetadata"]["bold"]["repetitionTime"])

    def test_malformed_gradient_sidecars_block_raw_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "sub-003"
            for folder in ("anat", "dwi", "func"):
                (subject / folder).mkdir(parents=True, exist_ok=True)
            (subject / "anat" / "sub-003_T1w.nii.gz").write_bytes(b"t1")
            (subject / "dwi" / "sub-003_dwi.nii.gz").write_bytes(b"dwi")
            (subject / "dwi" / "sub-003_dwi.bval").write_text("0 1000\n", encoding="utf-8")
            (subject / "dwi" / "sub-003_dwi.bvec").write_text("1 0 0\n0 1 0\n0 0 1\n", encoding="utf-8")
            (subject / "dwi" / "sub-003_dwi.json").write_text(json.dumps({
                "PhaseEncodingDirection": "j", "TotalReadoutTime": 0.05,
            }), encoding="utf-8")
            (subject / "func" / "sub-003_task-rest_bold.nii.gz").write_bytes(b"bold")
            (subject / "func" / "sub-003_task-rest_bold.json").write_text(json.dumps({"RepetitionTime": 3.0}), encoding="utf-8")
            inventory = inventory_subject(subject)
            self.assertIn("gradient_count_mismatch", {row["code"] for row in inventory["inputIssues"]})
            plan = plan_workflow(inventory, command_paths={})
            self.assertIn("gradient_count_mismatch", {row.get("code") for row in plan["blockers"]})

    def test_bold_tr_falls_back_to_a_unit_labelled_nifti_header(self) -> None:
        """HCP trees ship BOLD without JSON; the header's own TR is subject metadata,
        but an unlabelled time unit is not guessed at."""
        import gzip
        import struct

        def nifti(path: Path, tr: float, units: int) -> None:
            header = bytearray(352)
            struct.pack_into("<i", header, 0, 348)
            struct.pack_into("<8h", header, 40, 4, 2, 2, 2, 10, 1, 1, 1)
            struct.pack_into("<8f", header, 76, 1, 2, 2, 2, tr, 0, 0, 0)
            header[123] = units
            header[344:348] = b"n+1\0"
            with gzip.open(path, "wb") as stream:
                stream.write(bytes(header))

        from workflow.planner import _nifti_tr_seconds
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for tr, units, expected in ((0.8, 2 | 8, 0.8), (800.0, 2 | 16, 0.8), (0.8, 2, None)):
                nifti(root / "b.nii.gz", tr, units)
                got = _nifti_tr_seconds(root / "b.nii.gz")
                self.assertTrue(got == expected if expected is None else abs(got - expected) < 1e-6, (tr, units, got))
            subject = root / "sub-005"
            for folder in ("anat", "dwi", "func"):
                (subject / folder).mkdir(parents=True)
            (subject / "anat" / "sub-005_T1w.nii.gz").write_bytes(b"t1")
            nifti(subject / "func" / "sub-005_task-rest_bold.nii.gz", 0.8, 2 | 8)
            bold = inventory_subject(subject)["rawMetadata"]["bold"]
            self.assertEqual((0.8, "nifti_header"), (round(bold["repetitionTime"], 6), bold["repetitionTimeSource"]))

    def test_declared_corrected_input_needs_no_correction_runtime_or_metadata(self) -> None:
        """ABCD/HCP-style BIDS: corrected upstream, JSON without phase encoding. The
        declaration lifts exactly the correction-only requirements, nothing else."""
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "sub-004"
            for folder in ("anat", "dwi", "func"):
                (subject / folder).mkdir(parents=True, exist_ok=True)
            (subject / "anat" / "sub-004_T1w.nii.gz").write_bytes(b"t1")
            (subject / "dwi" / "sub-004_dwi.nii.gz").write_bytes(b"dwi")
            (subject / "dwi" / "sub-004_dwi.bval").write_text("0 1000\n", encoding="utf-8")
            (subject / "dwi" / "sub-004_dwi.bvec").write_text("0 1\n0 0\n0 0\n", encoding="utf-8")
            (subject / "dwi" / "sub-004_dwi.json").write_text(json.dumps({"RepetitionTime": 4.2}), encoding="utf-8")
            (subject / "func" / "sub-004_task-rest_bold.nii.gz").write_bytes(b"bold")
            (subject / "func" / "sub-004_task-rest_bold.json").write_text(json.dumps({"RepetitionTime": 0.8}), encoding="utf-8")
            inventory = inventory_subject(subject)
            ordinary = plan_workflow(inventory, command_paths={})
            declared = plan_workflow(inventory, command_paths={}, distortion_corrected_input=True)
            ordinary_codes = {row.get("code") for row in ordinary["blockers"]}
            self.assertTrue({"missing_dwi_phase_encoding", "missing_dwi_readout"} <= ordinary_codes)
            self.assertTrue(any(row["stage"] == "dwi_correction" for row in ordinary["blockers"]))
            stages = {row["id"]: row for row in declared["stages"]}
            for stage in ("dwi_correction", "fmri_correction"):
                self.assertEqual(("pending", []), (stages[stage]["state"], stages[stage]["missingCommands"]))
            self.assertEqual("blocked", stages["prepare_data"]["state"], "only correction stages are exempt")
            self.assertEqual([], [row for row in declared["blockers"] if row["stage"] in ("input", "dwi_correction")])
            # Raw ADNI with Synb0 and a license configured is not blocked on them (it always was).
            configured = plan_workflow(inventory, command_paths={}, synb0_configured=True)
            self.assertTrue(any("Synb0-DISCO" in row["message"] for row in ordinary["blockers"]))
            self.assertFalse(any("Synb0-DISCO" in row["message"] for row in configured["blockers"]))

    def test_raw_bids_subject_is_detected_and_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "sub-001"
            for folder in ("anat", "dwi", "func"):
                (subject / folder).mkdir(parents=True, exist_ok=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"t1")
            (subject / "dwi" / "sub-001_dwi.nii.gz").write_bytes(b"dwi")
            (subject / "func" / "sub-001_task-rest_bold.nii.gz").write_bytes(b"bold")
            self.assertTrue(looks_like_subject(subject))
            inventory = inventory_subject(subject)
            commands = {
                command: "/mock/bin/" + command
                for command in {
                    "apptainer", "topup", "eddy", "bet", "fslroi", "fslmerge", "fslmaths",
                    "flirt", "fugue", "mrconvert", "scil_gradients_convert",
                    "scil_gradients_modify_axes", "mrcalc", "dwinormalise", "antsRegistration",
                    "antsApplyTransforms", "scil_dti_metrics", "recon-all", "mri_convert",
                    "fast", "scil_frf_ssst", "scil_fodf_ssst", "preproc-sess",
                    "fcseed-config", "fcseed-sess", "mkanalysis-sess", "selxavg3-sess",
                    "scil_tracking_pft", "extraction_sccm_withfeatures_cortical.py",
                }
            }
            plan = plan_workflow(inventory, command_paths=commands)
            self.assertEqual("pending", plan["stages"][0]["state"])
            self.assertFalse(plan["runPsc"])
            self.assertFalse(any(row["id"] == "psc" for row in plan["stages"]))

    def test_psc_is_optional_and_not_an_sbci_prerequisite(self) -> None:
        inventory = {
            "raw": {"t1": None, "dwi": None, "bold": None},
            "completed": {
                "dwi_correction": False,
                "fmri_correction": False,
                "prepare_data": False,
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
        plan = plan_workflow(inventory, command_paths={})
        self.assertEqual([], plan["blockers"])
        self.assertTrue(all(row["state"] == "complete" for row in plan["stages"][:-1]))
        self.assertEqual("pending", plan["stages"][-1]["state"])


if __name__ == "__main__":
    unittest.main()
