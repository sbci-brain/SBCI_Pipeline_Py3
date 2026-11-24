from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workflow.planner import STAGE_COMMANDS
from workflow.workflow_executor import (
    build_dwi_correction_actions,
    build_fmri_correction_actions,
    execute_workflow_stage,
    profile_mismatches,
    unique_directions,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import image_fixture  # noqa: E402


def setUpModule() -> None:
    image_fixture.install()


def tearDownModule() -> None:
    image_fixture.remove()


SHIPPED = ROOT / "preprocessing"
DATASETS = ROOT / "datasets"
ADNI_PROFILE = DATASETS / "adni" / "profile.conf"
BASH = shutil.which("bash")


def write_gradients(directory: Path, name: str, directions: int, shells=(1000,), b0s: int = 2) -> None:
    """FSL bval/bvec with `directions` well-spread hemisphere directions repeated on each shell."""
    golden = math.pi * (3 - math.sqrt(5))
    points = []
    for i in range(directions):
        z = 1 - 0.9 * (i + 0.5) / directions
        r = math.sqrt(1 - z * z)
        points.append((r * math.cos(i * golden), r * math.sin(i * golden), z))
    bvals = [0] * b0s + [shell for shell in shells for _ in points]
    vectors = [(0.0, 0.0, 0.0)] * b0s + [point for _ in shells for point in points]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.bval").write_text(" ".join(map(str, bvals)) + "\n", encoding="ascii")
    (directory / f"{name}.bvec").write_text(
        "\n".join(" ".join(f"{v[axis]:.6f}" for v in vectors) for axis in range(3)) + "\n", encoding="ascii"
    )


def run_shipped_stage(script: str, stage: str, cwd: Path, config_text: str, extra_env=None, drop=(), failing=()):
    """Run a shipped stage script with every declared command stubbed to log its argv.

    Commands named in `drop` get no stub, and PATH entries mentioning them are
    removed, so a test can check what the script does when one is absent.
    Stubs named in `failing` log, then exit 1.
    """
    config = cwd.parent / "sbci_config"
    config.write_text(config_text, encoding="utf-8")
    stubs = cwd.parent / "stubs"
    stubs.mkdir(exist_ok=True)
    log = cwd.parent / "commands.log"
    for name in STAGE_COMMANDS[stage]:
        if name in drop:
            continue
        exit_line = b"exit 1\n" if name in failing else b""
        (stubs / name).write_bytes(b'#!/bin/bash\necho "$(basename "$0") $*" >> "$STUB_LOG"\n' + exit_line)
        (stubs / name).chmod(0o755)
    path = [p for p in os.environ["PATH"].split(os.pathsep) if not any(d in p.lower() for d in drop)]
    env = {k: v for k, v in os.environ.items()
           if k not in ("SH_ORDER", "SHELL_POLICY", "FMRI_TR", "FMRI_NSKIP", "FREESURFER_HOME", "TEMPLATEDIR")}
    env.update(PATH=os.pathsep.join([str(stubs), *path]), STUB_LOG=log.as_posix(),
               SBCI_CONFIG=config.as_posix(), **(extra_env or {}))
    completed = subprocess.run([BASH, (SHIPPED / script).as_posix()], cwd=cwd, env=env,
                               capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.is_file() else []
    return completed, calls


def config_without(*keys: str) -> str:
    """The shipped ADNI config with the named assignments removed."""
    lines = (ADNI_PROFILE).read_text(encoding="utf-8").splitlines()
    return "\n".join(l for l in lines if not any(l.startswith(f"{k}=") for k in keys)) + "\n"


def make_raw_bids_subject(root: Path) -> Path:
    subject = root / "sub-001"
    (subject / "anat").mkdir(parents=True)
    (subject / "dwi").mkdir()
    (subject / "func").mkdir()
    (subject / "anat" / "sub-001_T1w.nii.gz").write_bytes(b"t1")
    (subject / "dwi" / "sub-001_dir-AP_dwi.nii.gz").write_bytes(b"dwi")
    (subject / "dwi" / "sub-001_dir-AP_dwi.bval").write_text("0 1000\n", encoding="ascii")
    (subject / "dwi" / "sub-001_dir-AP_dwi.bvec").write_text("0 1\n0 0\n0 0\n", encoding="ascii")
    (subject / "dwi" / "sub-001_dir-AP_dwi.json").write_text(json.dumps({
        "PhaseEncodingDirection": "j-", "TotalReadoutTime": 0.0449,
    }), encoding="utf-8")
    (subject / "func" / "sub-001_task-rest_bold.nii.gz").write_bytes(b"bold")
    (subject / "func" / "sub-001_task-rest_bold.json").write_text(json.dumps({
        "PhaseEncodingDirection": "j", "EffectiveEchoSpacing": 0.00036,
        "RepetitionTime": 3.0,
    }), encoding="utf-8")
    return subject


def command_map(*names: str) -> dict[str, str]:
    return {name: f"/mock/bin/{name}" for name in names}


class OutputCreatingRunner:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd: Path, log: Path, environment) -> int:
        self.calls.append(list(argv))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fake stage\n", encoding="utf-8")
        if self.stage == "dwi_correction":
            dwi = cwd / "dwi"
            for name in (
                "eddy_corrected_data.nii.gz", "eddy_corrected_data.eddy_rotated_bvecs",
                "ap_dwi.bval", "field_hz.nii.gz", "hifi_b0.nii.gz",
            ):
                path = dwi / name
                if not path.exists():
                    path.write_bytes(b"validated")
        elif self.stage == "t1_dwi_registration":
            pipeline = cwd if cwd.name == "dwi_pipeline" else cwd / "dwi_pipeline"
            structure = pipeline / "structure"
            diffusion = pipeline / "diffusion"
            for path in (
                diffusion / "dti" / "fa.nii.gz", diffusion / "dti" / "md.nii.gz",
                structure / "t1_warped.nii.gz", structure / "t1_wholebrain_warped.nii.gz",
                diffusion / "dwi_fodf.nii.gz",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"validated")
        return 0


class WorkflowExecutorTests(unittest.TestCase):
    def test_dwi_builder_uses_bids_metadata_and_cpu_eddy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root)
            license_path = root / "license.txt"
            license_path.write_text("license", encoding="utf-8")
            commands = command_map("fslroi", "bet", "fslmerge", "topup", "fslmaths", "eddy", "apptainer")
            actions = build_dwi_correction_actions(subject, commands, {
                "synb0_image": "/images/synb0.sif",
                "freesurfer_license": str(license_path),
            })
            flattened = "\n".join(" ".join(action) for action in actions)
            self.assertIn("0 -1 0 0.0449", (subject / "dwi" / "acqparams.txt").read_text())
            self.assertIn("/mock/bin/eddy", flattened)
            self.assertIn(" --nthr=", flattened, "eddy_cpu defaults to one thread")
            self.assertNotIn("eddy_cuda", flattened)
            self.assertIn("/images/synb0.sif", flattened)

    def test_dwi_builder_runs_the_images_builtin_synb0(self) -> None:
        """In sbci-full, Synb0 runs directly: no synb0_image, no apptainer or docker."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root)
            license_path = root / "license.txt"
            license_path.write_text("license", encoding="utf-8")
            commands = command_map("fslroi", "bet", "fslmerge", "topup", "fslmaths", "eddy", "synb0")
            actions = build_dwi_correction_actions(subject, commands, {"freesurfer_license": str(license_path)})
            out = subject / "dwi"
            self.assertIn(["/mock/bin/synb0", str(out), str(out / "synb0_output"), str(license_path)], actions)

    def test_declared_corrected_input_copies_without_running_anything(self) -> None:
        """No Synb0/topup/eddy/fugue, no phase-encoding metadata: the images are copied
        to the paths prepare_data and fsfast read, and the marker records the declaration."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root / "input")
            (subject / "dwi" / "sub-001_dir-AP_dwi.json").write_text(json.dumps({"RepetitionTime": 4.2}), encoding="utf-8")
            runner = OutputCreatingRunner("none")
            workflow = {"distortion_corrected_input": True}
            for stage, outputs in (
                ("dwi_correction", {"dwi/eddy_corrected_data.nii.gz": "dwi/sub-001_dir-AP_dwi.nii.gz",
                                         "dwi/eddy_corrected_data.eddy_rotated_bvecs": "dwi/sub-001_dir-AP_dwi.bvec",
                                         "dwi/ap_dwi.bval": "dwi/sub-001_dir-AP_dwi.bval"}),
                ("fmri_correction", {"fmri/bold_distcorr.nii.gz": "func/sub-001_task-rest_bold.nii.gz"}),
            ):
                result = execute_workflow_stage(stage, subject, root / "output", {}, workflow, runner=runner)
                self.assertEqual("complete", result["state"], result)
                work = Path(result["workSubject"])
                for copy, source in outputs.items():
                    self.assertEqual((subject / source).read_bytes(), (work / copy).read_bytes(), copy)
                marker = json.loads(Path(result["marker"]).read_text(encoding="utf-8"))
                self.assertEqual((True, []), (marker["distortionCorrectedInput"], marker["commands"]))
            self.assertEqual([], runner.calls)
            with self.assertRaisesRegex(RuntimeError, "TotalReadoutTime"):
                build_dwi_correction_actions(Path(result["workSubject"]), {}, {})

    def test_declared_corrected_input_keeps_every_rest_run_and_refuses_mixed_tr(self) -> None:
        """ABCD has four rest runs and its historical database used them all."""
        for trs, error in (((0.8, 0.8, 0.8), None), ((0.8, 0.8, 2.0), "different TRs")):
            with self.subTest(trs=trs), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                subject = make_raw_bids_subject(root / "input")
                for old in (subject / "func").iterdir():
                    old.unlink()
                for run, tr in enumerate(trs, start=1):
                    stem = subject / "func" / f"sub-001_task-rest_run-{run:02d}_bold"
                    Path(f"{stem}.nii.gz").write_bytes(f"bold-{run}".encode())
                    Path(f"{stem}.json").write_text(json.dumps({"RepetitionTime": tr}), encoding="utf-8")
                    Path(str(stem).replace("_bold", "_motion") + ".tsv").write_bytes(f"motion-{run}".encode())
                call = lambda: execute_workflow_stage(  # noqa: E731
                    "fmri_correction", subject, root / "output", {}, {"distortion_corrected_input": True},
                    runner=OutputCreatingRunner("none"),
                )
                if error:
                    with self.assertRaisesRegex(RuntimeError, error):
                        call()
                    continue
                fmri = Path(call()["workSubject"]) / "fmri"
                self.assertEqual(
                    [b"bold-1", b"bold-2", b"bold-3"],
                    [(fmri / n).read_bytes() for n in (
                        "bold_distcorr.nii.gz", "bold_distcorr_run-02.nii.gz", "bold_distcorr_run-03.nii.gz")],
                )
                self.assertEqual(
                    [b"motion-1", b"motion-2", b"motion-3"],
                    [(fmri / n).read_bytes() for n in (
                        "bold_distcorr_motion.tsv", "bold_distcorr_run-02_motion.tsv", "bold_distcorr_run-03_motion.tsv")],
                )

    def test_dwi_builder_rejects_missing_readout_instead_of_defaulting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root)
            (subject / "dwi" / "sub-001_dir-AP_dwi.json").write_text(
                json.dumps({"PhaseEncodingDirection": "j"}), encoding="utf-8"
            )
            license_path = root / "license.txt"
            license_path.write_text("license", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "TotalReadoutTime"):
                build_dwi_correction_actions(subject, command_map(
                    "fslroi", "bet", "fslmerge", "topup", "fslmaths", "eddy", "apptainer"
                ), {"synb0_image": "synb0.sif", "freesurfer_license": str(license_path)})

    def test_fmri_builder_requires_validated_dwi_fieldmap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject = make_raw_bids_subject(Path(temp))
            with self.assertRaisesRegex(RuntimeError, "field_hz"):
                build_fmri_correction_actions(subject, command_map("fslmaths", "bet", "flirt", "fugue"))

    def test_fmri_builder_gives_fugue_the_topup_field_in_rad_per_s(self) -> None:
        """topup --fout writes Hz; fugue --loadfmap reads rad/s (review 2026-10-05, finding 5)."""
        with tempfile.TemporaryDirectory() as temp:
            subject = make_raw_bids_subject(Path(temp))
            (subject / "dwi" / "field_hz.nii.gz").write_bytes(b"field")
            (subject / "dwi" / "hifi_b0.nii.gz").write_bytes(b"b0")
            actions = build_fmri_correction_actions(subject, command_map("fslmaths", "bet", "flirt", "fugue"))
        fugue = next(a for a in actions if a[0].endswith("fugue"))
        fieldmap = next(arg.split("=", 1)[1] for arg in fugue if arg.startswith("--loadfmap="))
        producer = next(a for a in actions if a[-1] == fieldmap)
        self.assertIn("-mul", producer)
        self.assertAlmostEqual(2 * math.pi, float(producer[producer.index("-mul") + 1]), places=9)

    def test_eddy_uses_the_allocation_and_the_thread_count_does_not_change_the_fingerprint(self) -> None:
        """eddy_cpu defaults to one thread; a resumed stage on another node must not rerun."""
        from workflow.workflow_executor import _stage_fingerprint, available_cpus
        eddy = ["/mock/bin/eddy", "--imain=x", f"--nthr={available_cpus()}", "--verbose"]
        self.assertTrue(available_cpus() >= 1)
        self.assertEqual(_stage_fingerprint([eddy]), _stage_fingerprint([["/mock/bin/eddy", "--imain=x", "--nthr=64", "--verbose"]]))
        self.assertEqual(_stage_fingerprint([eddy]), _stage_fingerprint([["/mock/bin/eddy", "--imain=x", "--verbose"]]))
        self.assertNotEqual(_stage_fingerprint([eddy]), _stage_fingerprint([["/mock/bin/eddy", "--imain=y", "--verbose"]]))

    def test_fmri_builder_topup_route_corrects_raw_bold_with_its_epi_pair(self) -> None:
        """SMS-style data: corrected DWI, raw BOLD plus an AP/PA spin-echo pair in fmap/."""
        with tempfile.TemporaryDirectory() as temp:
            subject = make_raw_bids_subject(Path(temp))
            (subject / "fmap").mkdir()
            for direction, pe in (("AP", "j-"), ("PA", "j")):
                (subject / "fmap" / f"sub-001_dir-{direction}_epi.nii.gz").write_bytes(b"epi")
                (subject / "fmap" / f"sub-001_dir-{direction}_epi.json").write_text(json.dumps({
                    "PhaseEncodingDirection": pe, "TotalReadoutTime": 0.0711195,
                }), encoding="utf-8")
            workflow = {"bold_topup_fieldmaps": True, "distortion_corrected_input": True}
            actions = build_fmri_correction_actions(subject, command_map("fslmerge", "topup", "applytopup"), workflow)
            acqparams = (subject / "fmri" / "se_epi_acqparams.txt").read_text()
        self.assertEqual(["fslmerge", "topup", "applytopup"], [Path(a[0]).name for a in actions])
        self.assertEqual("0 -1 0 0.0711195\n0 1 0 0.0711195\n", acqparams)
        applytopup = actions[2]
        self.assertIn("--inindex=2", applytopup)  # the BOLD is PE j, the PA image's direction
        self.assertTrue(applytopup[-1].endswith("bold_distcorr"))
        self.assertIn("--config=b02b0_1.cnf", actions[1])  # unreadable grid: no subsampling

    def test_fmri_builder_topup_route_writes_one_acqparams_row_per_fieldmap_volume(self) -> None:
        """HCP-YA's SpinEchoFieldMap images hold 3 volumes each; topup refuses 2 rows for 6."""
        import gzip
        import struct
        with tempfile.TemporaryDirectory() as temp:
            subject = make_raw_bids_subject(Path(temp))
            (subject / "fmap").mkdir()
            for direction, pe in (("AP", "j-"), ("PA", "j")):
                header = bytearray(352)
                struct.pack_into("<i", header, 0, 348)
                struct.pack_into("<8h", header, 40, 4, 2, 2, 2, 3, 1, 1, 1)
                header[344:348] = b"n+1\0"
                with gzip.open(subject / "fmap" / f"sub-001_dir-{direction}_epi.nii.gz", "wb") as stream:
                    stream.write(bytes(header))
                (subject / "fmap" / f"sub-001_dir-{direction}_epi.json").write_text(json.dumps({
                    "PhaseEncodingDirection": pe, "TotalReadoutTime": 0.05,
                }), encoding="utf-8")
            actions = build_fmri_correction_actions(
                subject, command_map("fslmerge", "topup", "applytopup"), {"bold_topup_fieldmaps": True},
            )
            acqparams = (subject / "fmri" / "se_epi_acqparams.txt").read_text()
        self.assertEqual("0 -1 0 0.05\n" * 3 + "0 1 0 0.05\n" * 3, acqparams)
        self.assertIn("--inindex=4", actions[2])  # first PA volume, the BOLD's direction

    def test_fmri_builder_topup_route_refuses_a_pair_without_the_bold_direction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject = make_raw_bids_subject(Path(temp))
            (subject / "fmap").mkdir()
            for direction in ("AP", "PA"):
                (subject / "fmap" / f"sub-001_dir-{direction}_epi.nii.gz").write_bytes(b"epi")
                (subject / "fmap" / f"sub-001_dir-{direction}_epi.json").write_text(json.dumps({
                    "PhaseEncodingDirection": "j-", "TotalReadoutTime": 0.07,
                }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "opposite"):
                build_fmri_correction_actions(
                    subject, command_map("fslmerge", "topup", "applytopup"), {"bold_topup_fieldmaps": True},
                )

    def test_dwi_builder_takes_the_synb0_b0_from_a_b0_volume(self) -> None:
        """Volume 0 is not always b=0 (review 2026-10-05, finding 6)."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root)
            (subject / "dwi" / "sub-001_dir-AP_dwi.bval").write_text("1000 5 1000\n", encoding="ascii")
            (subject / "dwi" / "sub-001_dir-AP_dwi.bvec").write_text("1 0 0\n0 0 1\n0 0 0\n", encoding="ascii")
            (root / "license.txt").write_text("license", encoding="utf-8")
            workflow = {"synb0_image": "/images/synb0.sif", "freesurfer_license": str(root / "license.txt")}
            commands = command_map("fslroi", "bet", "fslmerge", "topup", "fslmaths", "eddy", "apptainer")
            actions = build_dwi_correction_actions(subject, commands, workflow)
            fslroi = next(a for a in actions if a[0].endswith("fslroi"))
            self.assertEqual(["1", "1"], fslroi[-2:])
            (subject / "dwi" / "sub-001_dir-AP_dwi.bval").write_text("1000 1000 1000\n", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "no b=0 volume"):
                build_dwi_correction_actions(subject, commands, workflow)

    def test_dwi_execution_writes_marker_and_resume_revalidates_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root / "input")
            output = root / "output"
            license_path = root / "license.txt"
            license_path.write_text("license", encoding="utf-8")
            commands = command_map("fslroi", "bet", "fslmerge", "topup", "fslmaths", "eddy", "apptainer")
            workflow = {"synb0_image": "synb0.sif", "freesurfer_license": str(license_path)}
            first_runner = OutputCreatingRunner("dwi_correction")
            first = execute_workflow_stage(
                "dwi_correction", subject, output, commands, workflow, runner=first_runner
            )
            self.assertEqual("complete", first["state"])
            marker = Path(first["marker"])
            self.assertTrue(marker.is_file())
            second_runner = OutputCreatingRunner("dwi_correction")
            second = execute_workflow_stage(
                "dwi_correction", subject, output, commands, workflow, runner=second_runner
            )
            self.assertEqual("skipped_complete", second["state"])
            self.assertEqual([], second_runner.calls)

    def test_reference_stage_is_allowlisted_and_uses_copied_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root / "input")
            pipeline = subject / "dwi_pipeline"
            pipeline.mkdir()
            scripts = root / "approved-scripts"
            scripts.mkdir()
            script = scripts / "preproc_step2_t1_dwi_registration.sh"
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            config = root / "sbci_config"
            config.write_text("DATASET_NAME=ADNI\n", encoding="utf-8")
            runner = OutputCreatingRunner("t1_dwi_registration")
            result = execute_workflow_stage(
                "t1_dwi_registration", subject, root / "output", {"bash": "/bin/bash"},
                {"reference_scripts_dir": str(scripts), "preprocessing_config": str(config)},
                runner=runner,
            )
            self.assertEqual("complete", result["state"])
            self.assertEqual(script.resolve(), Path(runner.calls[0][1]))
            self.assertNotIn(str(subject.resolve()), " ".join(runner.calls[0]))
            self.assertTrue(Path(result["workSubject"]).is_dir())

    def test_reference_stage_can_reach_commands_found_outside_path(self) -> None:
        """Inspection may find a command in an isolated prefix; the stage script calls it by
        bare name, so its folder must be on the stage's PATH -- after the existing entries,
        so tools already on PATH win (review 2026-10-05, finding 10)."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root / "input")
            (subject / "dwi_pipeline").mkdir()
            scripts = root / "approved-scripts"
            scripts.mkdir()
            (scripts / "preproc_step2_t1_dwi_registration.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (root / "sbci_config").write_text("DATASET_NAME=ADNI\n", encoding="utf-8")
            seen = {}

            class Runner(OutputCreatingRunner):
                def __call__(self, argv, cwd, log, environment):
                    seen.update(environment)
                    return super().__call__(argv, cwd, log, environment)

            isolated = root / "isolated" / "ants" / "bin"
            on_path = Path(os.environ["PATH"].split(os.pathsep)[0])
            commands = {"bash": str(on_path / "bash"), "N4BiasFieldCorrection": str(isolated / "N4BiasFieldCorrection")}
            execute_workflow_stage(
                "t1_dwi_registration", subject, root / "output", commands,
                {"reference_scripts_dir": str(scripts), "preprocessing_config": str(root / "sbci_config")},
                runner=Runner("t1_dwi_registration"),
            )
        entries = seen["PATH"].split(os.pathsep)
        self.assertEqual(str(isolated), entries[-1])
        self.assertEqual(os.environ["PATH"].split(os.pathsep), entries[: len(entries) - 1])

    @unittest.skipUnless(os.name == "posix", "symlinks need extra privileges on Windows")
    def test_reference_stage_gives_freesurfer_6_the_configured_license(self) -> None:
        """FreeSurfer 6.0.0 ignores FS_LICENSE and reads only $FREESURFER_HOME/license.txt, which
        sbci-full.sif leaves out (its run 8242252 failed so): the stage gets a FREESURFER_HOME of
        links that adds the configured licence. An install with its own licence is left alone."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root / "input")
            (subject / "dwi_pipeline").mkdir()
            scripts = root / "approved-scripts"
            scripts.mkdir()
            (scripts / "preproc_step2_t1_dwi_registration.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (root / "sbci_config").write_text("DATASET_NAME=ADNI\n", encoding="utf-8")
            (root / "my-license.txt").write_text("my licence", encoding="utf-8")
            home = root / "freesurfer"
            (home / "bin").mkdir(parents=True)
            seen = []

            class Runner(OutputCreatingRunner):
                def __call__(self, argv, cwd, log, environment):
                    seen.append(environment["FREESURFER_HOME"])
                    return super().__call__(argv, cwd, log, environment)

            workflow = {"reference_scripts_dir": str(scripts), "preprocessing_config": str(root / "sbci_config"),
                        "freesurfer_license": str(root / "my-license.txt")}
            run = lambda output: execute_workflow_stage(  # noqa: E731
                "t1_dwi_registration", subject, root / output, {"bash": "/bin/bash"}, workflow,
                runner=Runner("t1_dwi_registration"),
            )
            with mock.patch.dict(os.environ, {"FREESURFER_HOME": str(home)}):
                run("without-license")
                (home / "license.txt").write_text("site licence", encoding="utf-8")
                run("with-license")
            linked = Path(seen[0])
            self.assertEqual("my licence", (linked / "license.txt").read_text(encoding="utf-8"))
            self.assertEqual((home / "bin").resolve(), (linked / "bin").resolve())
            self.assertEqual(str(home), seen[1])
            # run_subject.sbatch picks the subject as preprocessing_work's only folder besides state/.
            work = root / "without-license" / "files" / "preprocessing_work"
            self.assertEqual({subject.name, "state"}, {p.name for p in work.iterdir() if p.is_dir()})

    def test_fsfast_reference_adapter_rejects_non_adni_tr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject = make_raw_bids_subject(root / "input")
            bold_json = subject / "func" / "sub-001_task-rest_bold.json"
            bold_json.write_text(json.dumps({
                "PhaseEncodingDirection": "j", "EffectiveEchoSpacing": 0.00036,
                "RepetitionTime": 2.0,
            }), encoding="utf-8")
            scripts = root / "approved-scripts"
            scripts.mkdir()
            (scripts / "preproc_step5_fmri.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            config = root / "sbci_config"
            config.write_text("DATASET_NAME=ADNI\n", encoding="utf-8")
            config.write_text((ADNI_PROFILE).read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, r"RepetitionTime is 2 s, but .* declares FMRI_TR=3"):
                execute_workflow_stage(
                    "fsfast", subject, root / "output", {"bash": "/bin/bash"},
                    {"reference_scripts_dir": str(scripts), "preprocessing_config": str(config)},
                    runner=OutputCreatingRunner("fsfast"),
                )

    def test_preflight_refuses_a_profile_that_does_not_fit(self) -> None:
        adni = ADNI_PROFILE
        abcd = DATASETS / "abcd" / "profile.conf"
        hcp_ya = DATASETS / "hcp-ya" / "profile.conf"
        corrected = {"distortion_corrected_input": True}
        fieldmaps = {"distortion_corrected_input": True, "bold_topup_fieldmaps": True}
        pending = {"stages": [{"id": "dwi_correction", "state": "pending"}, {"id": "fsfast", "state": "pending"}]}

        def problems(config, settings, tr, plan=pending):
            return " ".join(profile_mismatches(
                {"preprocessing_config": str(config), **settings},
                {"raw": {"bold": "bold"}, "rawMetadata": {"bold": {"repetitionTime": tr}}},
                plan,
            ))

        for config, settings, tr, error in (
            (adni, {}, 3.0, ""),
            (abcd, corrected, 0.8, ""),
            (DATASETS / "abcd" / "profile.historical.conf", corrected, 0.8, ""),
            (DATASETS / "hcp-lifespan" / "profile.conf", corrected, 0.8, ""),
            (DATASETS / "hcp-lifespan" / "profile.historical.conf", corrected, 0.8, ""),
            (hcp_ya, fieldmaps, 0.72, ""),
            # A profile named by its place in the pipeline, as README.md shows it.
            (Path("datasets/abcd/profile.conf"), corrected, 0.8, ""),
            (Path("datasets/abcd/profile.conf"), {}, 0.8, "INPUT_ROUTE=corrected"),
            # Raw ADNI declared corrected would otherwise finish on uncorrected data.
            (adni, corrected, 3.0, "INPUT_ROUTE=raw"),
            (abcd, {}, 0.8, "INPUT_ROUTE=corrected"),
            # HCP-YA without its fieldmap step would leave the BOLD uncorrected.
            (hcp_ya, corrected, 0.72, "INPUT_ROUTE=corrected_dwi_raw_bold"),
            (abcd, corrected, 3.0, "RepetitionTime is 3 s"),
            (adni, {}, None, "RepetitionTime is missing"),
        ):
            with self.subTest(config=str(config), settings=settings, tr=tr):
                found = problems(config, settings, tr)
                self.assertIn(error, found) if error else self.assertEqual("", found)
        # A finished stage is not judged again on resume, and a profile without the setting is left alone.
        finished = {"stages": [{"id": "dwi_correction", "state": "complete"}, {"id": "fsfast", "state": "complete"}]}
        self.assertEqual("", problems(adni, corrected, 0.8, finished))
        with tempfile.TemporaryDirectory() as temp:
            custom = Path(temp) / "sbci_config"
            custom.write_text("DATASET_NAME=Mine\n", encoding="utf-8")
            self.assertEqual("", problems(custom, corrected, 2.0))

    def test_fsfast_preflight_takes_tr_from_bids_and_requires_corrected_bold(self) -> None:
        def recording_runner(argv, cwd, log, environment):
            seen.append((environment.get("FMRI_TR"), environment.get("FMRI_NSKIP")))
            return 0

        adni = (ADNI_PROFILE).read_text(encoding="utf-8")
        for config_text, tr, corrected, expected, error in (
            (adni, 3.0, True, ("3", "4"), None),
            (config_without("FMRI_TR"), 0.8, True, ("0.8", "15"), None),
            (config_without("FMRI_TR"), 0.72, True, ("0.72", "17"), None),
            (config_without("FMRI_TR") + "FMRI_NSKIP=0\n", 0.8, True, ("0.8", "0"), None),
            (config_without("FMRI_TR", "FMRI_SKIP_SECONDS") + "FMRI_SKIP_SECONDS=6.4\n", 0.8, True, ("0.8", "8"), None),
            (adni, 3.0, False, None, "distortion-corrected BOLD"),
            (config_without("FMRI_MOTION_REGRESSORS") + "FMRI_MOTION_REGRESSORS=tsv\n", 3.0, True, None, "bold_distcorr_motion.tsv is missing"),
            (config_without("FMRI_MOTION_REGRESSORS") + "FMRI_MOTION_REGRESSORS=fsl\n", 3.0, True, None, "not one of mcextreg, tsv"),
        ):
            with self.subTest(tr=tr, corrected=corrected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                subject, workflow = self._reference_subject(root, "preproc_step5_fmri.sh", config_text)
                (subject / "func" / "sub-001_task-rest_bold.json").write_text(json.dumps({
                    "PhaseEncodingDirection": "j", "EffectiveEchoSpacing": 0.00036, "RepetitionTime": tr,
                }), encoding="utf-8")
                if corrected:
                    (subject / "fmri").mkdir()
                    (subject / "fmri" / "bold_distcorr.nii.gz").write_bytes(b"corrected")
                seen: list[tuple] = []
                call = lambda: execute_workflow_stage(  # noqa: E731
                    "fsfast", subject, root / "output", {"bash": "/bin/bash"}, workflow, runner=recording_runner,
                )
                if error:
                    with self.assertRaisesRegex(RuntimeError, error):
                        call()
                    self.assertEqual([], seen)
                else:
                    call()
                    self.assertEqual([expected], seen)

    def _fsfast_tree(self, root: Path, *, corrected: bool = True, complete_mcr: bool = False) -> tuple[Path, str]:
        """A work subject for preproc_step5 plus a fake FreeSurfer install."""
        fs = root / "freesurfer"
        fs.mkdir(parents=True)
        (fs / "SetUpFreeSurfer.sh").write_bytes(f"export FREESURFER_HOME={fs.as_posix()}\n".encode())
        if complete_mcr:
            (fs / "bin").mkdir()
            (fs / "bin" / "fast_selxavg3b.glnxa64").write_bytes(b"#!/bin/bash\n")
            (fs / "bin" / "fast_selxavg3b.glnxa64").chmod(0o755)
            (fs / "MCRv80" / "bin" / "glnxa64").mkdir(parents=True)
            (fs / "MCRv80" / "bin" / "glnxa64" / "libmwlaunchermain.so").write_bytes(b"")
        subject = root / "work" / "sub-001"
        (subject / "dwi_pipeline" / "structure").mkdir(parents=True)
        (subject / "func").mkdir()
        (subject / "func" / "sub-001_task-rest_bold.nii.gz").write_bytes(b"raw")
        if corrected:
            (subject / "fmri").mkdir()
            (subject / "fmri" / "bold_distcorr.nii.gz").write_bytes(b"corrected")
        # The image sets FREESURFER_HOME; here the config stands in for it.
        return subject, f"export FREESURFER_HOME={fs.as_posix()}\n"

    @staticmethod
    def _fsfast_args(calls: list[str], command: str) -> list[str]:
        return [c for c in calls if c.startswith(command + " ")]

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fsfast_keeps_adni_timing_and_reads_it_from_config(self) -> None:
        for config_keys, env, expect in (
            ((), {}, ("-TR 3", "-nskip 4", "-polyfit 2", "-stc siemens")),
            (("FMRI_TR", "FMRI_STC"), {"FMRI_TR": "0.8", "FMRI_NSKIP": "15"},
             ("-TR 0.8", "-nskip 15", "-polyfit 2", None)),
        ):
            with self.subTest(env=env), tempfile.TemporaryDirectory() as temp:
                subject, fs_line = self._fsfast_tree(Path(temp))
                extra = "FMRI_STC=none\n" if "FMRI_STC" in config_keys else ""
                completed, calls = run_shipped_stage(
                    "preproc_step5_fmri.sh", "fsfast", subject,
                    config_without(*config_keys) + fs_line + extra, env,
                )
                analyses = self._fsfast_args(calls, "mkanalysis-sess")
                self.assertEqual(3, len(analyses), completed.stdout + completed.stderr)
                for call in analyses:
                    for flag in expect[:3]:
                        self.assertIn(f" {flag} ", call)
                stc_calls = analyses + self._fsfast_args(calls, "preproc-sess")
                if expect[3]:
                    self.assertTrue(all(f" {expect[3]} " in c for c in stc_calls))
                else:
                    self.assertFalse(any("-stc" in c for c in stc_calls))
                self.assertEqual(b"corrected", (subject / "fsfast" / "bold" / "001" / "f.nii.gz").read_bytes())

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fsfast_uses_bids_motion_regressors_when_asked(self) -> None:
        """The lab's ABCD/HCP recipe: -nuisreg motion.dat 6 from each run's BIDS
        *_motion.tsv (header and t_indx column dropped), not FSFast's -mcextreg."""
        header = "t_indx\trot_z\trot_x\trot_y\ttrans_z\ttrans_x\ttrans_y\n"
        rows = "0\t0.0439\t-0.3464\t-0.2979\t-0.3281\t0.0251\t0.0836\n1\t0.1\t0.2\t0.3\t0.4\t0.5\t0.6\n"
        for present, ok in (((1, 2), True), ((1,), False)):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as temp:
                subject, fs_line = self._fsfast_tree(Path(temp))
                (subject / "fmri" / "bold_distcorr_run-02.nii.gz").write_bytes(b"run-2")
                for run in present:
                    name = "bold_distcorr_motion.tsv" if run == 1 else f"bold_distcorr_run-{run:02d}_motion.tsv"
                    (subject / "fmri" / name).write_bytes((header + rows).encode())
                completed, calls = run_shipped_stage(
                    "preproc_step5_fmri.sh", "fsfast", subject,
                    config_without("FMRI_MOTION_REGRESSORS") + fs_line + "FMRI_MOTION_REGRESSORS=tsv\n",
                )
                analyses = self._fsfast_args(calls, "mkanalysis-sess")
                if not ok:
                    self.assertNotEqual(0, completed.returncode)
                    self.assertEqual([], self._fsfast_args(calls, "preproc-sess"), "FSFast ran without run 2's motion")
                    continue
                self.assertEqual(3, len(analyses), completed.stdout + completed.stderr)
                self.assertTrue(all(" -nuisreg motion.dat 6 " in c and "-mcextreg" not in c for c in analyses))
                self.assertEqual(
                    b"0.0439 -0.3464 -0.2979 -0.3281 0.0251 0.0836\n0.1 0.2 0.3 0.4 0.5 0.6\n",
                    (subject / "fsfast" / "bold" / "002" / "motion.dat").read_bytes(),
                )

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fsfast_places_every_corrected_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject, fs_line = self._fsfast_tree(Path(temp))
            for run in (2, 3, 4):
                (subject / "fmri" / f"bold_distcorr_run-{run:02d}.nii.gz").write_bytes(f"run-{run}".encode())
            completed, calls = run_shipped_stage(
                "preproc_step5_fmri.sh", "fsfast", subject, config_without() + fs_line,
            )
            bold = subject / "fsfast" / "bold"
            self.assertEqual(
                [b"corrected", b"run-2", b"run-3", b"run-4"],
                [(bold / f"{run:03d}" / "f.nii.gz").read_bytes() for run in (1, 2, 3, 4)],
                completed.stdout + completed.stderr,
            )
            self.assertEqual(1, len(self._fsfast_args(calls, "preproc-sess")), "preproc-sess handles all runs")

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fodf_metrics_threshold_clamped_and_failure_stops_stage(self) -> None:
        """Real values: ABCD MSMT gave -1.79e-22 (argparse read "--at -3.6e-22" as an
        option and the stage still reported complete without peaks); the historical
        ABCD recipe gave 0.1232490273, whose threshold must print exactly as before."""
        for ventricle_max, at, failing, ok in (
            ("-1.792861422645006e-22", "0", (), True),
            ("0.12324902725045207", "0.2464980545", (), True),
            ("0.12324902725045207", "0.2464980545", ("scil_fodf_metrics",), False),
        ):
            with self.subTest(ventricle_max=ventricle_max, failing=failing), tempfile.TemporaryDirectory() as temp:
                pipeline = Path(temp) / "work" / "dwi_pipeline"
                (pipeline / "structure").mkdir(parents=True)
                write_gradients(pipeline / "diffusion", "fodf", 60, shells=(1000, 2000, 3000))
                (pipeline / "diffusion" / "fodf").mkdir()
                (pipeline / "diffusion" / "fodf" / "ventricles_fodf_max_value.txt").write_bytes(ventricle_max.encode())
                completed, calls = run_shipped_stage(
                    "preproc_step4_fodf_estimation.sh", "fodf_tissue", pipeline, config_without("SH_ORDER"),
                    failing=failing,
                )
                metrics = [c for c in calls if c.startswith("scil_fodf_metrics ")]
                self.assertEqual(1, len(metrics), completed.stdout + completed.stderr)
                self.assertIn(f" --at {at} --mask ", metrics[0])
                self.assertEqual(ok, completed.returncode == 0, completed.stderr)

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fodf_stage_ssst_model_reproduces_historical_multishell_fit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pipeline = Path(temp) / "work" / "dwi_pipeline"
            (pipeline / "structure").mkdir(parents=True)
            write_gradients(pipeline / "diffusion", "fodf", 60, shells=(1000, 2000, 3000))
            completed, calls = run_shipped_stage(
                "preproc_step4_fodf_estimation.sh", "fodf_tissue", pipeline,
                config_without("FODF_MODEL", "SH_ORDER") + "FODF_MODEL=ssst\nSH_ORDER=8\n",
            )
            fodf = [c for c in calls if c.startswith("scil_fodf_ssst ")]
            self.assertEqual(1, len(fodf), completed.stdout + completed.stderr)
            self.assertIn(" --sh_order 8 ", fodf[0])
            self.assertFalse(any("msmt" in c for c in calls))

    def test_fodf_preflight_rejects_unknown_fodf_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject, workflow = self._reference_subject(
                root, "preproc_step4_fodf_estimation.sh", config_without("FODF_MODEL") + "FODF_MODEL=SSST\n",
            )
            write_gradients(subject / "dwi_pipeline" / "diffusion", "fodf", 60, shells=(1000, 2000))
            runner = OutputCreatingRunner("fodf_tissue")
            with self.assertRaisesRegex(RuntimeError, "FODF_MODEL=SSST .* not one of auto, ssst"):
                execute_workflow_stage(
                    "fodf_tissue", subject, root / "output", {"bash": "/bin/bash"}, workflow, runner=runner,
                )
            self.assertEqual([], runner.calls)

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fsfast_refuses_uncorrected_bold(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject, fs_line = self._fsfast_tree(Path(temp), corrected=False)
            completed, calls = run_shipped_stage(
                "preproc_step5_fmri.sh", "fsfast", subject, config_without() + fs_line,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertEqual([], self._fsfast_args(calls, "preproc-sess"), "FSFast ran on uncorrected BOLD")
            self.assertFalse((subject / "fsfast" / "bold" / "001" / "f.nii.gz").exists())

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_freesurfer_uses_the_allocated_cores_unless_told_otherwise(self) -> None:
        for extra, env, threads in (("", {"OMP_NUM_THREADS": "1"}, None), ("FREESURFER_THREADS=1\n", {}, "1")):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                pipeline = Path(temp) / "work" / "dwi_pipeline"
                (pipeline / "structure").mkdir(parents=True)
                completed, calls = run_shipped_stage(
                    "preproc_step3_t1_freesurfer.sh", "freesurfer", pipeline, config_without() + extra, env,
                )
                recon = [c for c in calls if c.startswith("recon-all ")]
                self.assertEqual(1, len(recon), completed.stdout + completed.stderr)
                cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
                self.assertTrue(recon[0].endswith(f" -all -openmp {threads or cores}"), recon[0])

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fsfast_uses_complete_mcr_when_matlab_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            subject, fs_line = self._fsfast_tree(Path(temp), complete_mcr=True)
            completed, calls = run_shipped_stage(
                "preproc_step5_fmri.sh", "fsfast", subject, config_without() + fs_line,
                drop=("matlab",),
            )
            selxavg = self._fsfast_args(calls, "selxavg3-sess")
            self.assertEqual(3, len(selxavg), completed.stdout + completed.stderr)
            self.assertTrue(all(" -mcr " in c for c in selxavg))

    # Shell selection and SH order. The shipped-script tests run the real stage
    # scripts under bash with every declared command stubbed, so they check what
    # the scripts actually pass to scilpy rather than what a comment claims.

    @staticmethod
    def _registration_tree(root: Path, bvals: str) -> Path:
        pipeline = root / "work" / "dwi_pipeline"
        pipeline.mkdir(parents=True)
        (pipeline / "flip_x.bval").write_text(bvals + "\n", encoding="ascii")
        return pipeline

    @staticmethod
    def _extract_shell_args(calls: list[str]) -> dict[str, str]:
        """Map dti/fodf to the b-values each scil_dwi_extract_shell call received."""
        result = {}
        for call in calls:
            if call.startswith("scil_dwi_extract_shell"):
                words = call.split()
                end = next(i for i, w in enumerate(words) if w.endswith(".nii.gz") and i > 1)
                result["dti" if "dwi_dti" in words[end] else "fodf"] = " ".join(words[4:end])
        return result

    def test_shipped_brain_extraction_is_single_threaded(self) -> None:
        """Multithreaded antsBrainExtraction gave a different mask on every run (2026-10-25)."""
        script = (SHIPPED / "preproc_step2_t1_dwi_registration.sh").read_text(encoding="utf-8")
        calls = [line for line in script.splitlines() if "antsBrainExtraction.sh" in line and not line.lstrip().startswith("#")]
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0].startswith("ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS=1 antsBrainExtraction.sh "), calls[0])

    def test_shipped_registration_has_fixed_seed(self) -> None:
        """antsRegistration seeds its sampling from the clock by default: the T1-DWI
        transform differed on every run, even single-threaded on one node (2026-10-27)."""
        script = (SHIPPED / "preproc_step2_t1_dwi_registration.sh").read_text(encoding="utf-8")
        calls = [line for line in script.splitlines() if "antsRegistration " in line and not line.lstrip().startswith("#")]
        self.assertEqual(1, len(calls))
        self.assertIn("--random-seed ", calls[0])

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_registration_keeps_adni_shell_selection(self) -> None:
        """ADNI's declared nearest_1000 picks what the old auto-detection picked,
        for each ADNI protocol shape: single shell, multi-shell, and b=2000 only."""
        for bvals, kept in (
            ("0 1000 1000 1000", "0 1000"),
            ("0 500 1000 2000 1000 500", "0 1000"),
            ("5 2000 1995 2005", "0 2000"),
        ):
            with self.subTest(bvals=bvals), tempfile.TemporaryDirectory() as temp:
                pipeline = self._registration_tree(Path(temp), bvals)
                completed, calls = run_shipped_stage(
                    "preproc_step2_t1_dwi_registration.sh", "t1_dwi_registration", pipeline,
                    (ADNI_PROFILE).read_text(encoding="utf-8"),
                )
                self.assertEqual({"dti": kept, "fodf": kept}, self._extract_shell_args(calls),
                                 completed.stdout + completed.stderr)

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_tensor_shell_is_nearest_1000_and_warns_outside_700_to_1500(self) -> None:
        """The script and the preflight pick the same tensor shell (of two equally near 1000,
        the higher), for each listed dataset as before, and warn only outside b = 700-1500."""
        from workflow.workflow_executor import dwi_shells, nearest_1000, shell_warnings
        for bvals, tensor, warns in (
            ("0 1000 1000", 1000, False),                 # ADNI single shell
            ("0 500 1000 2000", 1000, False),             # ADNI multi-shell
            ("0 500 1000 2000 3000", 1000, False),        # ABCD
            ("5 1485 1510 2980 3010", 1500, False),       # HCP Aging / Development
            ("5 990 1005 1995 2005 2990 3010", 1000, False),  # HCP-YA
            ("0 2000 1995", 2000, True),                  # ADNI b=2000 only
            ("0 500 1500", 1500, False),                  # equally near: the higher
            ("0 600 1500", 600, True),
            ("0 700 2000", 700, False),
            ("0 300 3000", 300, True),
        ):
            with self.subTest(bvals=bvals), tempfile.TemporaryDirectory() as temp:
                pipeline = self._registration_tree(Path(temp), bvals)
                completed, calls = run_shipped_stage(
                    "preproc_step2_t1_dwi_registration.sh", "t1_dwi_registration", pipeline,
                    config_without("SHELL_POLICY", "DTI_BVALS", "FODF_BVALS") + "SHELL_POLICY=all\n",
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(f"0 {tensor}", self._extract_shell_args(calls)["dti"], output)
                self.assertEqual(warns, f"WARNING: the tensor fit uses b = {tensor}," in completed.stdout, output)
                bval = pipeline / "diffusion" / "flip_x.bval"
                self.assertEqual(tensor, nearest_1000(dwi_shells(bval)))
                start = f"The tensor fit uses b = {tensor}, outside"
                found = shell_warnings(bval, pipeline.parent / "sbci_config")
                self.assertEqual([start] if warns else [], [message[:len(start)] for message in found])

    def test_preflight_warns_when_the_data_are_not_the_shells_a_profile_lists(self) -> None:
        """DTI_BVALS / FODF_BVALS select nothing under nearest_1000 and all; they say what the
        profile expects. Each listed dataset's own shells give no warning."""
        from workflow.workflow_executor import shell_warnings
        own = {
            "adni/profile.conf": ("0 1000 1000", "0 500 1000 2000"),
            "abcd/profile.conf": ("0 500 1000 2000 3000",),
            "abcd/profile.historical.conf": ("0 500 1000 2000 3000",),
            "hcp-lifespan/profile.conf": ("5 1485 1510 2980 3010",),
            "hcp-lifespan/profile.historical.conf": ("5 1485 1510 2980 3010",),
            "hcp-ya/profile.conf": ("5 990 1005 1995 2005 2990 3010",),
            "generic/profile.conf": ("0 1000", "0 700 1400 2100", "5 1485 1510 2980 3010"),
            "sms/profile.conf": ("0 1000 2000",),
        }
        self.assertEqual(set(own), {f"{p.parent.name}/{p.name}" for p in DATASETS.glob("*/profile*.conf")})
        with tempfile.TemporaryDirectory() as temp:
            bval = Path(temp) / "dwi.bval"

            def warnings(profile: str, bvals: str) -> str:
                bval.write_text(bvals + "\n", encoding="ascii")
                return " ".join(shell_warnings(bval, DATASETS / profile))

            for profile, protocols in own.items():
                for bvals in protocols:
                    with self.subTest(profile=profile, bvals=bvals):
                        self.assertEqual("", warnings(profile, bvals))
            # HCP-YA data under the ABCD profile: the fODF shells are not the ones it lists.
            self.assertRegex(
                warnings("abcd/profile.conf", "5 990 1005 1995 2005 2990 3010"),
                r"give FODF_BVALS=\(0 1000 2000 3000\) .* written for FODF_BVALS=\(0 500 1000 2000 3000\)")
            # HCP Aging data under the HCP-YA profile: the tensor shell differs too.
            self.assertRegex(warnings("hcp-ya/profile.conf", "5 1485 1510 2980 3010"),
                             r"DTI_BVALS=\(0 1500\); FODF_BVALS=\(0 1500 3000\)")
            # ADNI's b=2000-only protocol: not what the profile lists, and a poor tensor shell.
            self.assertRegex(warnings("adni/profile.conf", "0 2000 2000"),
                             r"written for DTI_BVALS=\(0 1000\).*outside b = 700-1500")
            # explicit: the listed shells are used as written, so only a missing one is reported.
            self.assertRegex(warnings("abcd/profile.historical.conf", "0 1000 2000"),
                             r"lists b = 3000 .* shells are b = 1000, 2000")

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_registration_stops_multishell_without_policy_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pipeline = self._registration_tree(Path(temp), "0 1000 2000 3000")
            completed, calls = run_shipped_stage(
                "preproc_step2_t1_dwi_registration.sh", "t1_dwi_registration", pipeline,
                config_without("SHELL_POLICY"),
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertEqual([], calls, "commands ran although no shell policy was chosen")
            self.assertIn("1000 2000 3000", completed.stdout)

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_registration_honours_all_and_explicit_policies(self) -> None:
        for extra, expected, fodf_tolerance in (
            ("SHELL_POLICY=all\n", {"dti": "0 1000", "fodf": "0 1000 2000 3000"}, "50"),
            ("SHELL_POLICY=explicit\nFODF_BVALS=(0 2000)\n", {"dti": "0 1000", "fodf": "0 2000"}, "20"),
            # Historical profiles: scilpy 2.x keeps |b - shell| <= tol, legacy kept < tol.
            ("SHELL_POLICY=explicit\nFODF_BVALS=(0 2000)\nFODF_TOLERANCE=19\n",
             {"dti": "0 1000", "fodf": "0 2000"}, "19"),
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                pipeline = self._registration_tree(Path(temp), "0 1000 2000 3000")
                completed, calls = run_shipped_stage(
                    "preproc_step2_t1_dwi_registration.sh", "t1_dwi_registration", pipeline,
                    config_without("SHELL_POLICY", "FODF_BVALS") + extra,
                )
                self.assertEqual(expected, self._extract_shell_args(calls), completed.stdout + completed.stderr)
                shells = {"dwi_dti" in c: c for c in calls if c.startswith("scil_dwi_extract_shell")}
                self.assertIn(f"--tolerance {fodf_tolerance} ", shells[False])  # fODF extraction
                # The tensor extraction: the shell's own +-50 window under `all`, else 20.
                self.assertIn(f"--tolerance {50 if 'all' in extra else 20} ", shells[True])

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fodf_stage_keeps_adni_ssst_order_6_and_reads_sh_order(self) -> None:
        for config_text, env, order in (
            ((ADNI_PROFILE).read_text(encoding="utf-8"), {}, "6"),
            (config_without("SH_ORDER"), {}, "6"),
            (config_without("SH_ORDER"), {"SH_ORDER": "8"}, "8"),
            (config_without("SH_ORDER") + "SH_ORDER=10\n", {}, "10"),
        ):
            with self.subTest(env=env, order=order), tempfile.TemporaryDirectory() as temp:
                pipeline = Path(temp) / "work" / "dwi_pipeline"
                (pipeline / "structure").mkdir(parents=True)
                write_gradients(pipeline / "diffusion", "fodf", 30)
                completed, calls = run_shipped_stage(
                    "preproc_step4_fodf_estimation.sh", "fodf_tissue", pipeline, config_text, env,
                )
                fodf = [c for c in calls if c.startswith("scil_fodf_ssst ")]
                self.assertEqual(1, len(fodf), completed.stdout + completed.stderr)
                self.assertIn(f" --sh_order {order} ", fodf[0])
                self.assertTrue(any(c.startswith("scil_frf_ssst ") for c in calls))
                self.assertFalse(any("msmt" in c for c in calls))

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_fodf_stage_fits_msmt_on_multishell_input(self) -> None:
        """Several shells route to multi-shell multi-tissue CSD with the FAST
        masks, writing only the WM fODF to the path tracking reads."""
        for env, order, shells, dti_limit in (
            ({}, "8", (1000, 2000, 3000), "1200"),
            ({"SH_ORDER": "6"}, "6", (500, 1000, 2000, 3000), "1200"),
            ({}, "8", (1485, 1510, 2980, 3010), "1550"),  # HCP-Lifespan: no shell <= 1200
        ):
            with self.subTest(env=env, shells=shells), tempfile.TemporaryDirectory() as temp:
                pipeline = Path(temp) / "work" / "dwi_pipeline"
                (pipeline / "structure").mkdir(parents=True)
                write_gradients(pipeline / "diffusion", "fodf", 60, shells=shells)
                completed, calls = run_shipped_stage(  # Longleaf sets OMP_NUM_THREADS=1 in batch jobs
                    "preproc_step4_fodf_estimation.sh", "fodf_tissue", pipeline, config_without("SH_ORDER"),
                    {**env, "OMP_NUM_THREADS": "1"},
                )
                frf = [c for c in calls if c.startswith("scil_frf_msmt ")]
                fodf = [c for c in calls if c.startswith("scil_fodf_msmt ")]
                self.assertEqual((1, 1), (len(frf), len(fodf)), completed.stdout + completed.stderr)
                for tissue in ("wm", "gm", "csf"):
                    self.assertIn(f"--mask_{tissue} ../structure/mask_{tissue}.nii.gz", frf[0])
                self.assertIn(f" --sh_order {order} ", fodf[0])
                self.assertIn("--not_all --wm_out_fODF fodf/fodf.nii.gz", fodf[0])
                self.assertIn(f"--tolerance 50 --dti_bval_limit {dti_limit} ", frf[0])
                self.assertIn(" --tolerance 50 ", fodf[0])
                cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
                self.assertIn(f" --processes {cores} ", fodf[0], "MSMT must use the allocated cores, not OMP_NUM_THREADS")
                self.assertFalse(any("ssst" in c for c in calls))
                self.assertTrue(any(c.startswith("scil_fodf_metrics fodf/fodf.nii.gz") for c in calls))

    @unittest.skipUnless(BASH, "bash is required to run the shipped stage scripts")
    def test_shipped_msmt_frf_widens_roi_then_stops_before_the_fodf_fit(self) -> None:
        """Too few GM voxels (noisy SMS-6 x 2 scans) widens the FRF cube 20 -> 30 -> 40;
        if all fail the stage stops there instead of failing later on missing files."""
        with tempfile.TemporaryDirectory() as temp:
            pipeline = Path(temp) / "work" / "dwi_pipeline"
            (pipeline / "structure").mkdir(parents=True)
            write_gradients(pipeline / "diffusion", "fodf", 60, shells=(1000, 2000))
            completed, calls = run_shipped_stage(
                "preproc_step4_fodf_estimation.sh", "fodf_tissue", pipeline, config_without("SH_ORDER"),
                failing=("scil_frf_msmt",),
            )
            radii = [c.split("--roi_radii ")[1].split()[0] for c in calls if c.startswith("scil_frf_msmt ")]
            self.assertEqual(["20", "30", "40"], radii, completed.stdout + completed.stderr)
            self.assertNotEqual(0, completed.returncode)
            self.assertEqual(["scil_frf_msmt"], sorted({c.split()[0] for c in calls if c.startswith("scil_")}))

    def _reference_subject(self, root: Path, script: str, config_text: str) -> tuple[Path, dict[str, str]]:
        subject = make_raw_bids_subject(root / "input")
        (subject / "dwi_pipeline" / "diffusion").mkdir(parents=True)
        scripts = root / "approved-scripts"
        scripts.mkdir()
        (scripts / script).write_text("#!/bin/bash\n", encoding="utf-8")
        config = root / "sbci_config"
        config.write_text(config_text, encoding="utf-8")
        return subject, {"reference_scripts_dir": str(scripts), "preprocessing_config": str(config)}

    def test_registration_preflight_blocks_multishell_without_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject, workflow = self._reference_subject(
                root, "preproc_step2_t1_dwi_registration.sh", config_without("SHELL_POLICY"),
            )
            (subject / "dwi_pipeline" / "flip_x.bval").write_text("0 1000 2000 3000 5\n", encoding="ascii")
            runner = OutputCreatingRunner("t1_dwi_registration")
            with self.assertRaisesRegex(RuntimeError, r"3 non-zero shells \(b = 1000, 2000, 3000\).*SHELL_POLICY"):
                execute_workflow_stage(
                    "t1_dwi_registration", subject, root / "output", {"bash": "/bin/bash"}, workflow, runner=runner,
                )
            self.assertEqual([], runner.calls)
            Path(workflow["preprocessing_config"]).write_text(
                config_without("SHELL_POLICY") + "SHELL_POLICY=all\n", encoding="utf-8",
            )
            result = execute_workflow_stage(
                "t1_dwi_registration", subject, root / "output", {"bash": "/bin/bash"}, workflow, runner=runner,
            )
            self.assertEqual("complete", result["state"])

    def test_registration_preflight_rejects_unknown_policy_even_on_single_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subject, workflow = self._reference_subject(
                root, "preproc_step2_t1_dwi_registration.sh",
                config_without("SHELL_POLICY") + "SHELL_POLICY=nearest1000\n",
            )
            (subject / "dwi_pipeline" / "flip_x.bval").write_text("0 1000\n", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "not one of"):
                execute_workflow_stage(
                    "t1_dwi_registration", subject, root / "output", {"bash": "/bin/bash"}, workflow,
                    runner=OutputCreatingRunner("t1_dwi_registration"),
                )

    def test_fodf_preflight_checks_sh_order_against_unique_directions(self) -> None:
        def recording_runner(argv, cwd, log, environment):
            seen.append(environment.get("SH_ORDER"))
            return 0

        cases = (
            ((ADNI_PROFILE).read_text(encoding="utf-8"), {}, "6", None),
            (config_without("SH_ORDER"), {"sh_order": 8}, None, r"needs at least 45 unique .* has 30; the highest order it supports is 6"),
            ((ADNI_PROFILE).read_text(encoding="utf-8"), {"sh_order": 4}, None, "disagrees"),
            (config_without("SH_ORDER") + "SH_ORDER=$((8))\n", {}, None, "plain assignment"),
        )
        for config_text, parameters, exported, error in cases:
            with self.subTest(parameters=parameters, error=error), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                subject, workflow = self._reference_subject(root, "preproc_step4_fodf_estimation.sh", config_text)
                write_gradients(subject / "dwi_pipeline" / "diffusion", "fodf", 30)
                seen: list[str | None] = []
                if error:
                    with self.assertRaisesRegex(RuntimeError, error):
                        execute_workflow_stage(
                            "fodf_tissue", subject, root / "output", {"bash": "/bin/bash"}, workflow,
                            runner=recording_runner, parameters=parameters,
                        )
                    self.assertEqual([], seen)
                else:
                    execute_workflow_stage(
                        "fodf_tissue", subject, root / "output", {"bash": "/bin/bash"}, workflow,
                        runner=recording_runner, parameters=parameters,
                    )
                    self.assertEqual([exported], seen)

    def test_fodf_preflight_defaults_multishell_to_order_8_and_still_checks_directions(self) -> None:
        def recording_runner(argv, cwd, log, environment):
            seen.append(environment.get("SH_ORDER"))
            return 0

        for directions, parameters, exported, error in (
            (60, {}, "8", None),
            (60, {"sh_order": 6}, "6", None),
            (40, {}, None, r"SH order 8 needs at least 45 unique .* has 40"),
        ):
            with self.subTest(directions=directions, parameters=parameters), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                subject, workflow = self._reference_subject(
                    root, "preproc_step4_fodf_estimation.sh", config_without("SH_ORDER"),
                )
                write_gradients(subject / "dwi_pipeline" / "diffusion", "fodf", directions, shells=(1000, 2000))
                seen: list[str | None] = []
                call = lambda: execute_workflow_stage(  # noqa: E731
                    "fodf_tissue", subject, root / "output", {"bash": "/bin/bash"}, workflow,
                    runner=recording_runner, parameters=parameters,
                )
                if error:
                    with self.assertRaisesRegex(RuntimeError, error):
                        call()
                    self.assertEqual([], seen)
                else:
                    call()
                    self.assertEqual([exported], seen)

    def test_shipped_dataset_profiles_parse_and_declare_their_choices(self) -> None:
        """Profiles are sourced by bash and read by the pre-flight parser; both must agree."""
        from workflow.workflow_executor import FODF_MODELS, SHELL_POLICIES, config_setting
        profiles = sorted(DATASETS.glob("*/profile*.conf"))
        self.assertGreater(len(profiles), 1, "no dataset profiles shipped")
        for profile in profiles:
            with self.subTest(profile=f"{profile.parent.name}/{profile.name}"):
                self.assertNotIn(b"\r", profile.read_bytes())
                self.assertIn(config_setting(profile, "SHELL_POLICY"), SHELL_POLICIES)
                self.assertIn(config_setting(profile, "FODF_MODEL"), FODF_MODELS)
                self.assertIn(config_setting(profile, "FMRI_MOTION_REGRESSORS"), ("mcextreg", "tsv"))
                # The generic profile takes the SH order, the TR and the route from each run.
                generic = profile.parent.name == "generic"
                for key in ("SH_ORDER", "FMRI_TR", "FMRI_SKIP_SECONDS", "FMRI_POLYFIT", "FMRI_STC"):
                    if not (generic and key in ("SH_ORDER", "FMRI_TR")):
                        self.assertIsNotNone(config_setting(profile, key), key)
                # Legacy scilpy's strict < 20 is scilpy 2.x's inclusive 19 (lab DB drops HCP b=2980).
                historical = profile.name == "profile.historical.conf"
                self.assertEqual("19" if historical else None, config_setting(profile, "FODF_TOLERANCE"))
                # A profile holds settings, never where things are installed (README.md, "Datasets").
                text = profile.read_text(encoding="utf-8")
                for key in ("OUTPUT_PATH", "SBCI_PATH", "PSC_PATH", "FREESURFER_PATH", "TEMPLATEDIR", "REFDIR"):
                    self.assertNotRegex(text, rf"(?m)^\s*(export\s+)?{key}=", key)
                self.assertIn(config_setting(profile, "INPUT_ROUTE"),
                              (None,) if generic else ("raw", "corrected", "corrected_dwi_raw_bold"))

    def test_sbci_stage_config_takes_n_runs_from_the_profile(self) -> None:
        """The lab's ABCD database ran 3 SET sets (40 of 40 subjects sampled), HCP 4. The SBCI
        stage reads parameters.n_runs, so the profile's N_RUNS must reach it, uncontradicted."""
        from workflow.workflow_executor import sbci_stage_config
        workflow = {"workflow": {"preprocessing_config": "datasets/abcd/profile.historical.conf"}}
        self.assertEqual({"n_runs": 3}, sbci_stage_config(workflow)["parameters"])
        self.assertEqual({"n_runs": 3, "n_seed": 1000},
                         sbci_stage_config({**workflow, "parameters": {"n_runs": 3, "n_seed": 1000}})["parameters"])
        with self.assertRaisesRegex(RuntimeError, "n_runs=4 disagrees with N_RUNS=3"):
            sbci_stage_config({**workflow, "parameters": {"n_runs": 4}})
        self.assertEqual({"schema_version": "1.0", "mode": "preprocess", "approved": True}, sbci_stage_config({}))

    @unittest.skipUnless(BASH, "bash is required to check the submission script")
    def test_run_subject_script_parses_and_refuses_read_only_roots(self) -> None:
        """The one-command recipe: valid bash, LF only, and refuses a folder a site marked
        read-only (SBCI_READONLY_ROOTS) before doing anything."""
        script = ROOT / "workflow" / "run_subject.sbatch"
        self.assertNotIn(b"\r", script.read_bytes())
        self.assertEqual(0, subprocess.run([BASH, "-n", script.as_posix()]).returncode)
        environment = {**os.environ, "SBCI_READONLY_ROOTS": "/shared/archive:/shared/raw/"}
        for args in (["/shared/archive/study/x", "/work/out"], ["/work/in", "/shared/raw/out"],
                     ["--inspect", "/work/in", "/shared/archive"]):
            completed = subprocess.run([BASH, script.as_posix(), *args], capture_output=True, text=True, env=environment)
            self.assertEqual(2, completed.returncode, completed.stderr)
            self.assertIn("SBCI_READONLY_ROOTS marks it read-only", completed.stderr)
        # A folder that only starts with the same letters is not under the root.
        completed = subprocess.run([BASH, script.as_posix(), "/shared/archive2/in", "/work/out"],
                                   capture_output=True, text=True, env=environment)
        self.assertNotIn("read-only", completed.stderr)

    def test_unique_directions_merges_antipodes_and_rotated_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            write_gradients(directory, "multi", 30, shells=(1000, 2000, 3000))
            self.assertEqual(30, unique_directions(directory / "multi.bval", directory / "multi.bvec"))
            one_degree = math.radians(1)
            vectors = [(0, 0, 0), (1, 0, 0), (-1, 0, 0), (math.cos(one_degree), math.sin(one_degree), 0), (0, 1, 0), (0, 0, 1)]
            (directory / "g.bval").write_text("0 1000 1000 1000 1000 1000\n", encoding="ascii")
            (directory / "g.bvec").write_text(
                "\n".join(" ".join(str(v[axis]) for v in vectors) for axis in range(3)) + "\n", encoding="ascii",
            )
            self.assertEqual(3, unique_directions(directory / "g.bval", directory / "g.bvec"))


if __name__ == "__main__":
    unittest.main()
