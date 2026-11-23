from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workflow.orchestration as orchestration
from workflow.orchestration import build_orchestration_plan, consume_handoff, validate_handoff


class OrchestrationTests(unittest.TestCase):
    def test_ported_stage_scripts_match_declared_commands(self) -> None:
        """Every scil_* command the shipped scripts call must be declared, or the
        preflight blocks the stage (and legacy .py names must be gone)."""
        import re
        from workflow.planner import STAGE_COMMANDS
        root = Path(__file__).resolve().parents[1]
        scripts = {
            "t1_dwi_registration": "preproc_step2_t1_dwi_registration.sh",
            "fodf_tissue": "preproc_step4_fodf_estimation.sh",
        }
        for stage, name in scripts.items():
            body = (root / "preprocessing" / name).read_text(encoding="utf-8")
            called = {m for m in re.findall(r"^\s*(scil_[A-Za-z0-9_.]+)", body, re.MULTILINE)}
            declared = set(STAGE_COMMANDS[stage])
            self.assertEqual(set(), called - declared, f"{stage}: undeclared {called - declared}")
            self.assertEqual([], [c for c in declared if c.endswith(".py")], f"{stage}: legacy names remain")
            # The runtime probe only resolves commands listed in its component
            # table, so a name missing there reports as missing whatever is on PATH.
            from workflow.preprocessing_runtime import COMPONENTS
            probed = set(COMPONENTS["scilpy_preprocessing"]["commands"])
            self.assertEqual(set(), called - probed, f"{stage}: not probed {called - probed}")

    def test_shipped_reference_scripts_exist(self) -> None:
        """Every stage the package claims to run must ship its script; psc is
        deliberately excluded (optional, not an SBCI dependency, still legacy)."""
        from workflow.workflow_executor import REFERENCE_SCRIPTS
        shipped = Path(__file__).resolve().parents[1] / "preprocessing"
        for stage, name in REFERENCE_SCRIPTS.items():
            if stage == "psc":
                continue
            script = shipped / name
            self.assertTrue(script.is_file() and script.stat().st_size > 0, f"{stage}: {name} not shipped")
        # CRLF is fatal to bash ("$'\r': command not found") and easy to
        # reintroduce when authoring on Windows.
        for script in sorted((shipped.parent).rglob("*.sh")):
            self.assertNotIn(b"\r", script.read_bytes(), f"{script.name}: CRLF line endings")
            # This package is Python 3; no shipped stage may activate the legacy
            # Python 2 environment (the ADNI reference rules forbid that branch).
            body = script.read_text(encoding="utf-8")
            for banned in ("bashrc_sbci_legacy_py2", "conda activate sbci_legacy_py2"):
                self.assertNotIn(
                    banned,
                    "".join(l for l in body.splitlines() if not l.lstrip().startswith("#")),
                    f"{script.name}: activates the legacy py2 environment",
                )

    def test_no_write_text_newline_kwarg(self) -> None:
        """Path.the newline= kwarg of Path.write_text is Python 3.10+; Longleaf's default python3
        is 3.9, so the package must use write_bytes instead. Twice now this has
        only shown up on the host, after the local suite passed."""
        import re
        root = Path(__file__).resolve().parents[1]
        pattern = re.compile(r"write_text\s*\((?:[^()]|\([^()]*\))*newline\s*=", re.S)
        for source in sorted(root.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            body = "\n".join(
                line for line in source.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#")
            )
            found = pattern.search(body)
            self.assertIsNone(
                found,
                f"{source.relative_to(root)}: the newline= kwarg of Path.write_text requires Python 3.10",
            )

    def test_compat_slicedelay_is_python3_and_used_by_fsfast(self) -> None:
        """fsfast runs -stc siemens, which shells out to slicedelay; FreeSurfer
        6.0.0 ships a Python 2 one, so the stage depends on our port."""
        import ast
        root = Path(__file__).resolve().parents[1]
        port = root / "workflow" / "compat_bin" / "slicedelay"
        self.assertTrue(port.is_file(), "workflow/compat_bin/slicedelay not shipped")
        ast.parse(port.read_text(encoding="utf-8"))  # raises on py2 syntax
        step5 = (root / "preprocessing" / "preproc_step5_fmri.sh").read_text(encoding="utf-8")
        # The stage finds the port from its own place in the pipeline.
        self.assertIn('"${SCRIPT_DIR}/../workflow/compat_bin"', step5)
        code = "".join(l for l in step5.splitlines() if not l.lstrip().startswith("#"))
        # The order is FMRI_STC now; ADNI (and the script's default) is still siemens.
        self.assertIn('STC_ARGS="-stc ${FMRI_STC}"', code)
        self.assertIn("FMRI_STC=${FMRI_STC:-siemens}", code)
        config = (root / "datasets" / "adni" / "profile.conf").read_text(encoding="utf-8")
        self.assertIn("\nFMRI_STC=siemens\n", config)
        self.assertIn("slicedelay", code)

    def test_partial_prepare_data_is_not_complete(self) -> None:
        from workflow.planner import inventory_subject
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "sub-001"
            pipeline = subject / "dwi_pipeline"
            pipeline.mkdir(parents=True)
            for name in ("t1.nii.gz", "data.nii.gz"):
                (pipeline / name).write_bytes(b"x")
            self.assertFalse(inventory_subject(subject)["completed"]["prepare_data"])
            for name in ("flip_x.bval", "flip_x.bvec"):
                (pipeline / name).write_bytes(b"x")
            self.assertTrue(inventory_subject(subject)["completed"]["prepare_data"])

    def test_prepare_data_handoff_copies_complete_prepared_input_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "input" / "sub-001"
            (subject / "anat").mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"raw-t1")
            producer = root / "producer"
            producer.mkdir()
            payloads = {
                "t1": b"prepared-t1",
                "dwi": b"prepared-dwi",
                "bval": b"0 1000\n",
                "bvec": b"1 0\n0 1\n0 0\n",
            }
            artifacts = []
            for role, payload in payloads.items():
                path = producer / role
                path.write_bytes(payload)
                artifacts.append({
                    "role": role,
                    "path": path.name,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
            manifest = {
                "schema_version": "1.0", "handoff_id": "prepare-run-1",
                "producer": {"skill": "Brain MRI Pipeline", "stage": "prepare_data", "environment": "producer-env"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": artifacts,
                "provenance": {"command": "producer", "container": "image", "completed_at": "now"},
            }
            manifest_path = producer / "handoff.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with mock.patch.object(orchestration, "sha256_file", wraps=orchestration.sha256_file) as digest:
                result = consume_handoff(manifest_path, subject, root / "output")

            self.assertEqual("complete", result["state"])
            work_subject = Path(result["workSubject"])
            expected = {
                "t1": work_subject / "dwi_pipeline" / "t1.nii.gz",
                "dwi": work_subject / "dwi_pipeline" / "data.nii.gz",
                "bval": work_subject / "dwi_pipeline" / "flip_x.bval",
                "bvec": work_subject / "dwi_pipeline" / "flip_x.bvec",
            }
            for role, target in expected.items():
                self.assertEqual(payloads[role], target.read_bytes())
            hashed_paths = [Path(call.args[0]) for call in digest.call_args_list]
            for role in payloads:
                self.assertNotIn(producer / role, hashed_paths)

    def test_t1_dwi_registration_handoff_copies_canonical_registration_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "input" / "sub-001"
            (subject / "anat").mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"raw-t1")
            producer = root / "producer"
            (producer / "diffusion" / "dti").mkdir(parents=True)
            (producer / "structure").mkdir(parents=True)
            payloads = {
                "fa": b"registered-fa",
                "md": b"registered-md",
                "t1Warped": b"registered-t1-warped",
                "t1WholebrainWarped": b"registered-t1-wholebrain-warped",
                "fodfDwi": b"registered-fodf-dwi",
            }
            relative = {
                "fa": "diffusion/dti/fa.nii.gz",
                "md": "diffusion/dti/md.nii.gz",
                "t1Warped": "structure/t1_warped.nii.gz",
                "t1WholebrainWarped": "structure/t1_wholebrain_warped.nii.gz",
                "fodfDwi": "diffusion/dwi_fodf.nii.gz",
            }
            artifacts = []
            for role, payload in payloads.items():
                path = producer / relative[role]
                path.write_bytes(payload)
                artifacts.append({
                    "role": role,
                    "path": relative[role],
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
            manifest = {
                "schema_version": "1.0", "handoff_id": "registration-run-1",
                "producer": {"skill": "ANTs", "stage": "t1_dwi_registration", "environment": "producer-env"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": artifacts,
                "provenance": {"command": "producer", "container": "image", "completed_at": "now"},
            }
            manifest_path = producer / "handoff.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with mock.patch.object(orchestration, "sha256_file", wraps=orchestration.sha256_file) as digest:
                result = consume_handoff(manifest_path, subject, root / "output")

            self.assertEqual("complete", result["state"])
            self.assertEqual("t1_dwi_registration", result["stage"])
            self.assertFalse(result["producerArtifactsModified"])
            work_subject = Path(result["workSubject"])
            pipeline = work_subject / "dwi_pipeline"
            expected = {
                "fa": pipeline / "diffusion" / "dti" / "fa.nii.gz",
                "md": pipeline / "diffusion" / "dti" / "md.nii.gz",
                "t1Warped": pipeline / "structure" / "t1_warped.nii.gz",
                "t1WholebrainWarped": pipeline / "structure" / "t1_wholebrain_warped.nii.gz",
                "fodfDwi": pipeline / "diffusion" / "dwi_fodf.nii.gz",
            }
            for role, target in expected.items():
                self.assertEqual(payloads[role], target.read_bytes())
                self.assertEqual(payloads[role], (producer / relative[role]).read_bytes())
            # The digest verified during mandatory handoff validation is reused,
            # so producer artifacts are never re-read for hashing during the copy.
            hashed_paths = [Path(call.args[0]) for call in digest.call_args_list]
            for role in payloads:
                self.assertNotIn(producer / relative[role], hashed_paths)

    def test_freesurfer_handoff_copies_canonical_surface_and_label_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "input" / "sub-001"
            (subject / "anat").mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"raw-t1")
            producer = root / "producer"
            relative = {
                "lhWhite": "structure/t1_freesurfer/surf/lh.white",
                "rhWhite": "structure/t1_freesurfer/surf/rh.white",
                "wmparc": "structure/t1_freesurfer/mri/wmparc.mgz",
                "lhDestrieux": "structure/t1_freesurfer/label/lh.aparc.a2009s.annot",
                "rhDestrieux": "structure/t1_freesurfer/label/rh.aparc.a2009s.annot",
                "warpedParcellation": "structure/aparc.a2009s+aseg_warped_label.nii.gz",
                "parcellation": "structure/aparc.a2009s+aseg.nii.gz",
                "lhPial": "structure/t1_freesurfer/surf/lh.pial",
                "rhPial": "structure/t1_freesurfer/surf/rh.pial",
                "lhSphere": "structure/t1_freesurfer/surf/lh.sphere",
                "rhSphere": "structure/t1_freesurfer/surf/rh.sphere",
                "lhSphereReg": "structure/t1_freesurfer/surf/lh.sphere.reg",
                "rhSphereReg": "structure/t1_freesurfer/surf/rh.sphere.reg",
                "lhInflated": "structure/t1_freesurfer/surf/lh.inflated",
                "rhInflated": "structure/t1_freesurfer/surf/rh.inflated",
            }
            payloads = {role: f"freesurfer-{role}".encode() for role in relative}
            artifacts = []
            for role, rel in relative.items():
                path = producer / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payloads[role])
                artifacts.append({
                    "role": role,
                    "path": rel,
                    "size": len(payloads[role]),
                    "sha256": hashlib.sha256(payloads[role]).hexdigest(),
                })
            manifest = {
                "schema_version": "1.0", "handoff_id": "freesurfer-run-1",
                "producer": {"skill": "FreeSurfer", "stage": "freesurfer", "environment": "producer-env"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": artifacts,
                "provenance": {"command": "producer", "container": "image", "completed_at": "now"},
            }
            manifest_path = producer / "handoff.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = consume_handoff(manifest_path, subject, root / "output")

            self.assertEqual("complete", result["state"])
            self.assertEqual("freesurfer", result["stage"])
            self.assertFalse(result["producerArtifactsModified"])
            fs = Path(result["workSubject"]) / "dwi_pipeline" / "structure" / "t1_freesurfer"
            expected = {
                "lhWhite": fs / "surf" / "lh.white",
                "rhWhite": fs / "surf" / "rh.white",
                "wmparc": fs / "mri" / "wmparc.mgz",
                "lhDestrieux": fs / "label" / "lh.aparc.a2009s.annot",
                "rhDestrieux": fs / "label" / "rh.aparc.a2009s.annot",
                "warpedParcellation": fs.parent / "aparc.a2009s+aseg_warped_label.nii.gz",
            }
            for role, target in expected.items():
                self.assertEqual(payloads[role], target.read_bytes())
                self.assertEqual(payloads[role], (producer / relative[role]).read_bytes())

    def test_fodf_tissue_handoff_copies_canonical_fodf_and_tissue_maps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "input" / "sub-001"
            (subject / "anat").mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"raw-t1")
            producer = root / "producer"
            relative = {
                "fodf": "diffusion/fodf/fodf.nii.gz",
                "wm": "structure/map_wm.nii.gz",
                "gm": "structure/map_gm.nii.gz",
                "csf": "structure/map_csf.nii.gz",
            }
            payloads = {role: f"fodf-tissue-{role}".encode() for role in relative}
            artifacts = []
            for role, rel in relative.items():
                path = producer / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payloads[role])
                artifacts.append({
                    "role": role,
                    "path": rel,
                    "size": len(payloads[role]),
                    "sha256": hashlib.sha256(payloads[role]).hexdigest(),
                })
            manifest = {
                "schema_version": "1.0", "handoff_id": "fodf-tissue-run-1",
                "producer": {"skill": "Brain MRI Pipeline", "stage": "fodf_tissue", "environment": "producer-env"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": artifacts,
                "provenance": {"command": "producer", "container": "image", "completed_at": "now"},
            }
            manifest_path = producer / "handoff.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = consume_handoff(manifest_path, subject, root / "output")

            self.assertEqual("complete", result["state"])
            self.assertEqual("fodf_tissue", result["stage"])
            self.assertFalse(result["producerArtifactsModified"])
            pipeline = Path(result["workSubject"]) / "dwi_pipeline"
            expected = {
                "fodf": pipeline / "diffusion" / "fodf" / "fodf.nii.gz",
                "wm": pipeline / "structure" / "map_wm.nii.gz",
                "gm": pipeline / "structure" / "map_gm.nii.gz",
                "csf": pipeline / "structure" / "map_csf.nii.gz",
            }
            for role, target in expected.items():
                self.assertEqual(payloads[role], target.read_bytes())
                self.assertEqual(payloads[role], (producer / relative[role]).read_bytes())

    def test_fsfast_handoff_copies_residuals_outside_the_dwi_pipeline_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "input" / "sub-001"
            (subject / "anat").mkdir(parents=True)
            (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"raw-t1")
            producer = root / "producer"
            relative = {
                "lhResidual": "fmri/bold/001/fc.surface.lh.nii.gz",
                "rhResidual": "fmri/bold/001/fc.surface.rh.nii.gz",
                "mniResidual": "fmri/bold/001/fc.mni.nii.gz",
            }
            payloads = {role: f"fsfast-{role}".encode() for role in relative}
            artifacts = []
            for role, rel in relative.items():
                path = producer / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payloads[role])
                artifacts.append({
                    "role": role,
                    "path": rel,
                    "size": len(payloads[role]),
                    "sha256": hashlib.sha256(payloads[role]).hexdigest(),
                })
            manifest = {
                "schema_version": "1.0", "handoff_id": "fsfast-run-1",
                "producer": {"skill": "FreeSurfer", "stage": "fsfast", "environment": "producer-env"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": artifacts,
                "provenance": {"command": "producer", "container": "image", "completed_at": "now"},
            }
            manifest_path = producer / "handoff.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = consume_handoff(manifest_path, subject, root / "output")

            self.assertEqual("complete", result["state"])
            self.assertEqual("fsfast", result["stage"])
            self.assertFalse(result["producerArtifactsModified"])
            # FSFast residuals land under the work subject's own fmri run
            # directory, not under dwi_pipeline like the diffusion stages.
            run = Path(result["workSubject"]) / "fmri" / "bold" / "001"
            expected = {
                "lhResidual": run / "fc.surface.lh.nii.gz",
                "rhResidual": run / "fc.surface.rh.nii.gz",
                "mniResidual": run / "fc.mni.nii.gz",
            }
            for role, target in expected.items():
                self.assertEqual(payloads[role], target.read_bytes())
                self.assertEqual(payloads[role], (producer / relative[role]).read_bytes())
            self.assertFalse((Path(result["workSubject"]) / "dwi_pipeline" / "fmri").exists())

    def test_handoff_consumption_copies_verified_artifacts_without_mutating_producer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "input" / "sub-001"
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
                artifacts.append({"role": role, "path": path.name, "size": len(payload)})
            manifest = {
                "schema_version": "1.0",
                "handoff_id": "producer-run-1",
                "producer": {"skill": "FSL", "stage": "dwi_correction", "environment": "fsl-container"},
                "subject": "sub-001",
                "status": "complete",
                "read_only": True,
                "artifact_root": ".",
                "artifacts": artifacts,
                "provenance": {"command": "producer-command", "container": "image@sha256:x", "completed_at": "now"},
            }
            manifest_path = producer / "handoff.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = consume_handoff(manifest_path, subject, root / "output")

            self.assertEqual("complete", result["state"])
            self.assertEqual("dwi_correction", result["stage"])
            self.assertTrue(result["validation"]["ready"])
            work_subject = Path(result["workSubject"])
            targets = {
                "correctedDwi": work_subject / "dwi" / "eddy_corrected_data.nii.gz",
                "rotatedBvec": work_subject / "dwi" / "eddy_corrected_data.eddy_rotated_bvecs",
                "bval": work_subject / "dwi" / "ap_dwi.bval",
                "fieldHz": work_subject / "dwi" / "field_hz.nii.gz",
                "hifiB0": work_subject / "dwi" / "hifi_b0.nii.gz",
            }
            for role, target in targets.items():
                self.assertEqual(payloads[role], target.read_bytes())
                self.assertFalse(target.is_symlink())
                self.assertEqual(payloads[role], (producer / role).read_bytes())
            self.assertTrue(Path(result["marker"]).is_file())

    def test_handoff_consumption_refuses_conflicting_work_tree_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "input" / "sub-001"
            (subject / "dwi").mkdir(parents=True)
            conflict = subject / "dwi" / "eddy_corrected_data.nii.gz"
            conflict.write_bytes(b"existing-output")
            producer = root / "producer"
            producer.mkdir()
            roles = ("correctedDwi", "rotatedBvec", "bval", "fieldHz", "hifiB0")
            artifacts = []
            for role in roles:
                path = producer / role
                path.write_bytes(("new-" + role).encode("ascii"))
                artifacts.append({"role": role, "path": path.name})
            manifest = {
                "schema_version": "1.0", "handoff_id": "producer-run-2",
                "producer": {"skill": "FSL", "stage": "dwi_correction", "environment": "fsl-container"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": artifacts,
                "provenance": {"command": "producer-command", "container": "image", "completed_at": "now"},
            }
            manifest_path = producer / "handoff.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "conflicts with handoff role correctedDwi"):
                consume_handoff(manifest_path, subject, root / "output")

            work_conflict = root / "output" / "files" / "preprocessing_work" / "sub-001" / "dwi" / conflict.name
            self.assertEqual(b"existing-output", work_conflict.read_bytes())
            self.assertEqual(b"new-correctedDwi", (producer / "correctedDwi").read_bytes())
            self.assertFalse((work_conflict.parent / "ap_dwi.bval").exists())

    def test_blocked_stage_recommends_isolated_candidate_without_claiming_invocation(self) -> None:
        plan = build_orchestration_plan(
            Path("/shared/sub-001"),
            {"completedConnectivity": False},
            {"stages": [{"id": "freesurfer", "state": "blocked"}]},
        )
        self.assertEqual("external_skill_candidate", plan["state"])
        self.assertIn("FreeSurfer", {row["skill"] for row in plan["candidateProviders"]})
        self.assertFalse(plan["automaticSkillInvocation"])

    def test_handoff_requires_complete_nonempty_stage_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = root / "shared"
            files.mkdir()
            for role in ("lhResidual", "rhResidual", "mniResidual"):
                (files / f"{role}.nii.gz").write_bytes(role.encode("ascii"))
            manifest_path = root / "handoff.json"
            manifest = {
                "schema_version": "1.0",
                "handoff_id": "run-1",
                "producer": {"skill": "FreeSurfer", "stage": "fsfast", "environment": "container-a"},
                "subject": "sub-001",
                "status": "complete",
                "read_only": True,
                "artifact_root": "shared",
                "artifacts": [
                    {"role": role, "path": f"{role}.nii.gz"}
                    for role in ("lhResidual", "rhResidual", "mniResidual")
                ],
                "provenance": {"command": "run", "container": "image@sha256:x", "completed_at": "now"},
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = validate_handoff(manifest, manifest_path)
            self.assertTrue(result["ready"])
            self.assertFalse(result["imported"])

    def test_validated_handoff_requires_explicit_consumption_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "sub-001"
            subject.mkdir()
            roles = ("correctedBold",)
            for role in roles:
                (root / role).write_bytes(b"corrected-bold")
            manifest = {
                "schema_version": "1.0", "handoff_id": "bold-run-1",
                "producer": {"skill": "FSL", "stage": "fmri_correction", "environment": "fsl"},
                "subject": "sub-001", "status": "complete", "read_only": True,
                "artifact_root": ".", "artifacts": [{"role": role, "path": role} for role in roles],
                "provenance": {"command": "producer", "container": "image", "completed_at": "now"},
            }
            path = root / "handoff.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")

            inspect_plan = build_orchestration_plan(
                subject, {"completedConnectivity": False},
                {"stages": [{"id": "fmri_correction", "state": "blocked"}]},
                {"handoff_manifest": str(path), "consume_handoff": False},
            )
            consume_plan = build_orchestration_plan(
                subject, {"completedConnectivity": False},
                {"stages": [{"id": "fmri_correction", "state": "blocked"}]},
                {"handoff_manifest": str(path), "consume_handoff": True},
            )

            self.assertFalse(inspect_plan["handoffConsumptionRequested"])
            self.assertIn("remains read-only", inspect_plan["action"])
            self.assertTrue(consume_plan["handoffConsumptionRequested"])
            self.assertIn("approved preprocess invocation", consume_plan["action"])

    def test_handoff_rejects_path_traversal_and_missing_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "handoff.json"
            manifest = {
                "schema_version": "1.0",
                "producer": {"stage": "fsfast"},
                "status": "complete",
                "read_only": True,
                "artifact_root": ".",
                "artifacts": [{"role": "lhResidual", "path": "../outside.nii.gz"}],
            }
            result = validate_handoff(manifest, manifest_path)
            self.assertFalse(result["ready"])
            codes = {row["code"] for row in result["issues"]}
            self.assertIn("unsafe_artifact_path", codes)
            self.assertIn("missing_artifact_role", codes)

    def test_handoff_enforces_metadata_and_unique_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / "file.nii.gz"
            artifact.write_bytes(b"ok")
            manifest = {
                "schema_version": "1.0", "handoff_id": "",
                "producer": {"stage": "fmri_correction", "skill": "", "environment": ""},
                "subject": "", "status": "complete", "read_only": True, "artifact_root": ".",
                "artifacts": [
                    {"role": "correctedBold", "path": "file.nii.gz"},
                    {"role": "correctedBold", "path": "file.nii.gz"},
                ],
                "provenance": {},
            }
            result = validate_handoff(manifest, root / "handoff.json")
            codes = {row["code"] for row in result["issues"]}
            self.assertIn("missing_handoff_id", codes)
            self.assertIn("invalid_handoff_producer", codes)
            self.assertIn("missing_handoff_subject", codes)
            self.assertIn("invalid_handoff_provenance", codes)
            self.assertIn("duplicate_artifact_role", codes)

    def test_relative_shared_work_dir_is_subject_relative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "sub-001" / "ses-1"
            subject.mkdir(parents=True)
            plan = build_orchestration_plan(
                subject, {"completedConnectivity": False},
                {"stages": [{"id": "freesurfer", "state": "blocked"}]},
                {"shared_work_dir": "shared"},
            )
            self.assertEqual(str((subject / "shared").resolve()), plan["sharedWorkDir"])

    def test_completed_connectivity_is_read_only_terminal_route(self) -> None:
        plan = build_orchestration_plan(
            Path("/shared/sub-001/ses-1"),
            {"completedConnectivity": True},
            {"stages": []},
        )
        self.assertEqual("completed_connectivity_read_only", plan["state"])
        self.assertIn("do not rerun", plan["action"])

    def test_valid_but_wrong_stage_handoff_is_not_accepted_for_next_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = root / "sub-001" / "ses-1"
            subject.mkdir(parents=True)
            for role in ("lhResidual", "rhResidual", "mniResidual"):
                (root / f"{role}.nii.gz").write_bytes(b"ok")
            manifest = {
                "schema_version": "1.0", "handoff_id": "x",
                "producer": {"skill": "FreeSurfer", "stage": "fsfast", "environment": "container"},
                "subject": "sub-001", "session": "ses-1", "status": "complete", "read_only": True,
                "artifact_root": ".",
                "artifacts": [{"role": role, "path": f"{role}.nii.gz"} for role in ("lhResidual", "rhResidual", "mniResidual")],
                "provenance": {"command": "", "container": "", "completed_at": ""},
            }
            path = root / "handoff.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = build_orchestration_plan(
                subject, {"completedConnectivity": False},
                {"stages": [{"id": "freesurfer", "state": "blocked"}]},
                {"handoff_manifest": str(path)},
            )
            self.assertFalse(result["handoffValidation"]["ready"])
            self.assertIn("handoff_stage_mismatch", {row["code"] for row in result["handoffValidation"]["issues"]})


if __name__ == "__main__":
    unittest.main()
