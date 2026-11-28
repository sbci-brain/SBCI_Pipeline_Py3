"""workflow.stages (which part of a run is asked for) and the job scripts."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workflow import local_pipeline  # noqa: E402
from workflow.workflow_executor import sbci_stage_config  # noqa: E402

SKILLS = ("sbci-preprocess", "sbci-tractography", "sbci-fc", "sbci-qc", "sbci-explore", "sbci-coupling",
          "sbci-fastpath", "sbci-cohort")


class StageSetTests(unittest.TestCase):
    def test_requested_sbci_steps(self) -> None:
        self.assertEqual((True, True), local_pipeline.requested_sbci_steps({}))
        self.assertEqual((True, False), local_pipeline.requested_sbci_steps({"stages": "tractography"}))
        self.assertEqual((False, True), local_pipeline.requested_sbci_steps({"stages": "fc"}))
        self.assertEqual((False, False), local_pipeline.requested_sbci_steps({"stages": "preprocess"}))

    def test_unknown_stage_set_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "sbci_local.json").write_text(json.dumps(
                {"schema_version": local_pipeline.SCHEMA_VERSION, "mode": "inspect", "workflow": {"stages": "sc"}}))
            _, _, errors = local_pipeline.load_config(Path(temp))
        self.assertEqual(["invalid_stages"], [e["code"] for e in errors])

    def test_sbci_stage_config_forwards_the_sbci_half(self) -> None:
        for stages, forwarded in (("tractography", {"stages": "tractography"}), ("fc", {"stages": "fc"}),
                                  ("all", None), ("preprocess", None)):
            self.assertEqual(forwarded, sbci_stage_config({"workflow": {"stages": stages}}).get("workflow"), stages)

    def run_steps(self, **kwargs):
        names = []

        def fake_stage(name, command, cwd, environment, logs):
            names.append(name)
            return {"name": name, "exitCode": 0, "elapsedSeconds": 0.0, "log": ""}

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(local_pipeline, "materialize_c3_main"), \
                mock.patch.object(local_pipeline, "start_work_tree") as start, \
                mock.patch.object(local_pipeline, "run_stage", side_effect=fake_stage):
            local_pipeline.execute_local(ROOT, Path(temp) / "subject", Path(temp) / "out",
                                         dict(local_pipeline.DEFAULT_PARAMETERS), Path(sys.executable), **kwargs)
        return names, start.call_args.kwargs

    def test_fc_only_runs_step6_on_the_existing_work_tree(self) -> None:
        names, start = self.run_steps(structural=False, functional=True)
        self.assertEqual(["step6_functional"], [n for n in names if not n.startswith("step1")])
        self.assertTrue(start["keep_existing"])

    def test_tractography_runs_steps_2_to_5_on_a_fresh_tree(self) -> None:
        names, start = self.run_steps(structural=True, functional=False)
        self.assertIn("step3_set_4", names)
        self.assertIn("step5_structural", names)
        self.assertNotIn("step6_functional", names)
        self.assertFalse(start["keep_existing"])

    def test_set_repetitions_run_side_by_side(self) -> None:
        import threading
        import time

        for cap, expect_overlap in ((None, True), ("1", False)):
            running, peak, lock = [0], [0], threading.Lock()

            def fake_stage(name, command, cwd, environment, logs):
                if name.startswith("step3_set_"):
                    with lock:
                        running[0] += 1
                        peak[0] = max(peak[0], running[0])
                    time.sleep(0.2)
                    with lock:
                        running[0] -= 1
                return {"name": name, "exitCode": 0, "elapsedSeconds": 0.0, "log": ""}

            env = {} if cap is None else {"SBCI_SET_PROCESSES": cap}
            with tempfile.TemporaryDirectory() as temp, \
                    mock.patch.dict(local_pipeline.os.environ, env), \
                    mock.patch.object(local_pipeline, "available_cpus", return_value=16), \
                    mock.patch.object(local_pipeline, "materialize_c3_main"), \
                    mock.patch.object(local_pipeline, "start_work_tree"), \
                    mock.patch.object(local_pipeline, "run_stage", side_effect=fake_stage):
                results, _ = local_pipeline.execute_local(ROOT, Path(temp) / "s", Path(temp) / "o",
                                                          dict(local_pipeline.DEFAULT_PARAMETERS), Path(sys.executable))
            names = [r["name"] for r in results]
            self.assertEqual(names, sorted(names, key=lambda n: (n[:5], n)), "stage order kept")
            self.assertEqual(expect_overlap, peak[0] > 1, f"cap={cap} peak={peak[0]}")

    def test_validation_asks_only_for_the_requested_half(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            connectivity = Path(temp)
            (connectivity / f"fc_avg_{local_pipeline.DEFAULT_PARAMETERS['resolution']}.mat").write_bytes(b"x")
            completed = mock.Mock(returncode=0, stdout=json.dumps({"functionalMat": {"finite": True, "valid": True}}), stderr="")
            with mock.patch.object(local_pipeline.subprocess, "run", return_value=completed):
                fc_only = local_pipeline.validate_connectivity(connectivity, local_pipeline.DEFAULT_PARAMETERS,
                                                               Path(sys.executable), structural=False)
                both = local_pipeline.validate_connectivity(connectivity, local_pipeline.DEFAULT_PARAMETERS,
                                                            Path(sys.executable))
        self.assertTrue(fc_only["ready"])
        self.assertEqual(["functionalMat"], list(fc_only["expected"]))
        self.assertFalse(both["ready"])


class ShippedSkillTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "bash is required")
    def test_slurm_scripts_parse(self) -> None:
        for script in ("run_subject.sbatch", "sbci_tool.sbatch", "runtime_env.sh", "submit.sh", "get_image.sbatch"):
            subprocess.run([shutil.which("bash"), "-n", (ROOT / "workflow" / script).as_posix()], check=True)

    @unittest.skipUnless(shutil.which("bash") and not shutil.which("sbatch"), "needs bash and no Slurm")
    def test_submit_runs_the_job_here_without_slurm(self) -> None:
        """No sbatch (a lab server or desktop): the job runs in the background, logged at --output."""
        import time
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            job = Path(temp) / "job.sh"
            job.write_text('printf "%s|" "$SBCI_PIPELINE" "$@"; echo finished\n', encoding="utf-8")
            started = subprocess.run(
                [shutil.which("bash"), (ROOT / "workflow" / "submit.sh").as_posix(),
                 f"--output={Path(temp).as_posix()}/logs/slurm-%j.out", "--mem=1g", job.as_posix(), "a", "b c"],
                capture_output=True, text=True, check=True,
                # Inside the image SBCI_PIPELINE names the image's copy; this run is of this folder.
                env={name: value for name, value in os.environ.items() if name != "SBCI_PIPELINE"})
            for _ in range(100):
                logs = list((Path(temp) / "logs").glob("slurm-local-*.out"))
                if logs and "finished" in logs[0].read_text(encoding="utf-8"):
                    break
                time.sleep(0.1)
            text = logs[0].read_text(encoding="utf-8")
        self.assertIn("without Slurm", started.stdout)
        self.assertRegex(text, re.escape(ROOT.name) + r"\|a\|b c\|finished")

    @unittest.skipUnless(shutil.which("bash") and sys.platform.startswith("linux"), "needs bash on Linux")
    def test_jobs_rerun_inside_the_image(self) -> None:
        """Outside sbci-full a job runs itself again in the image, seeing its folders and the host's MATLAB."""
        with tempfile.TemporaryDirectory() as name:
            temp = Path(name).resolve()
            fake = temp / "bin" / "apptainer"
            matlab = temp / "MATLAB" / "R2018b" / "bin" / "matlab"
            for tool, text in ((fake, 'printf "%s\\n" "$@"'), (matlab, "")):
                tool.parent.mkdir(parents=True)
                tool.write_text("#!/bin/sh\n" + text + "\n", encoding="utf-8")
                tool.chmod(0o755)
            image, licence, inp, out = temp / "sbci-full.sif", temp / "license.txt", temp / "in", temp / "out"
            for path in (image, licence):
                path.write_text("x", encoding="utf-8")
            (inp / "sub-01").mkdir(parents=True)
            out.mkdir()
            (inp / "sbci_local.json").write_text(json.dumps({"workflow": {"freesurfer_license": str(licence)}}))
            env = {k: v for k, v in os.environ.items() if k not in ("SBCI_IMAGE", "SBCI_PIPELINE", "SBCI_IMAGE_ENV")}
            env.update(PATH=f"{fake.parent}:{matlab.parent}:{env['PATH']}", SBCI_FULL_IMAGE=str(image))

            def run(script: str, *args: str) -> list[str]:
                done = subprocess.run([shutil.which("bash"), (ROOT / "workflow" / script).as_posix(), *args],
                                      env=env, capture_output=True, text=True)
                self.assertEqual(0, done.returncode, done.stderr)
                lines = done.stdout.splitlines()
                return lines[lines.index("exec") + 1:]

            subject = run("run_subject.sbatch", "--inspect", str(inp), str(out))
            self.assertEqual([str(image), "bash", f"{ROOT.as_posix()}/workflow/run_subject.sbatch", "--inspect", str(inp), str(out)],
                             subject[-6:])
            self.assertIn(f"PREPEND_PATH={matlab.parent}", subject)
            self.assertLessEqual({str(p) for p in (ROOT.resolve(), matlab.parent.parent, inp, inp / "sub-01", out, licence)},
                                 set(subject[subject.index("--bind") + 1].split(",")))
            new = temp / "explore" / "new"
            tool = run("sbci_tool.sbatch", "explore", str(licence), "--out", str(new))
            self.assertEqual([str(image), "bash", f"{ROOT.as_posix()}/workflow/sbci_tool.sbatch", "explore", str(licence), "--out", str(new)],
                             tool[-7:])
            self.assertLessEqual({str(licence), str(temp)}, set(tool[tool.index("--bind") + 1].split(",")))
            env["SBCI_FULL_IMAGE"] = str(temp / "missing.sif")
            missing = subprocess.run([shutil.which("bash"), (ROOT / "workflow" / "sbci_tool.sbatch").as_posix(), "explore"],
                                     env=env, capture_output=True, text=True)
            self.assertEqual(2, missing.returncode)
            self.assertIn("get_image.sbatch", missing.stderr)

    @unittest.skipUnless(shutil.which("bash") and sys.platform.startswith("linux"), "needs bash on Linux")
    def test_jobs_rerun_in_docker_without_apptainer(self) -> None:
        """SBCI_CONTAINER=docker: the job runs again in the pinned image, as this user, with the same folders."""
        with tempfile.TemporaryDirectory() as name:
            temp = Path(name).resolve()
            fake = temp / "bin" / "docker"
            fake.parent.mkdir(parents=True)
            # `docker image inspect` succeeds; `docker run` prints its arguments.
            fake.write_text('#!/bin/sh\n[ "$1" = image ] && exit 0\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            fake.chmod(0o755)
            licence, inp, out = temp / "license.txt", temp / "in", temp / "out"
            licence.write_text("x", encoding="utf-8")
            (inp / "sub-01").mkdir(parents=True)
            out.mkdir()
            (inp / "sbci_local.json").write_text(json.dumps({"workflow": {"freesurfer_license": str(licence)}}))
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SBCI_IMAGE", "SBCI_PIPELINE", "SBCI_IMAGE_ENV", "SBCI_DOCKER_IMAGE", "SBCI_FULL_IMAGE")}
            env.update(PATH=f"{fake.parent}:{env['PATH']}", SBCI_CONTAINER="docker", SBCI_READONLY_ROOTS="/nowhere")
            done = subprocess.run([shutil.which("bash"), (ROOT / "workflow" / "run_subject.sbatch").as_posix(),
                                   "--inspect", str(inp), str(out)], env=env, capture_output=True, text=True)
            self.assertEqual(0, done.returncode, done.stderr)
            args = done.stdout.splitlines()
            pin = dict(line.split("=", 1) for line in (ROOT / "workflow" / "image_pin.sh").read_text().splitlines()
                       if "=" in line and not line.startswith("#"))
            image = f"{pin['REPOSITORY']}@{pin['DIGEST']}"
            self.assertEqual([image, "bash", f"{ROOT.as_posix()}/workflow/run_subject.sbatch", "--inspect", str(inp), str(out)],
                             args[-6:])
            mounts = {args[i + 1] for i, arg in enumerate(args) if arg == "-v"}
            self.assertLessEqual({f"{p}:{p}" for p in (ROOT.resolve(), inp, inp / "sub-01", out, licence)}, mounts)
            self.assertIn(f"{os.getuid()}:{os.getgid()}", args)
            self.assertIn(f"SBCI_PIPELINE={ROOT.as_posix()}", args)
            self.assertIn("SBCI_READONLY_ROOTS", args)

    def test_one_pin_serves_both_routes(self) -> None:
        """get_image.sbatch and the Docker route read the image's digest from image_pin.sh only."""
        pin = (ROOT / "workflow" / "image_pin.sh").read_text(encoding="utf-8")
        self.assertRegex(pin, r"(?m)^DIGEST=sha256:[0-9a-f]{64}$")
        for script in ("get_image.sbatch", "runtime_env.sh"):
            text = (ROOT / "workflow" / script).read_text(encoding="utf-8")
            self.assertIn("workflow/image_pin.sh", text)
            self.assertNotRegex(text, r"sha256:[0-9a-f]{64}")

    def test_runtime_probe_finds_the_assets(self) -> None:
        """The reference surfaces and ico4 grid sit in data/ (a preflight once raised after they moved)."""
        probe = local_pipeline.runtime_probe(ROOT)
        self.assertTrue(probe["referenceExists"] and probe["ico4GridCacheExists"])
        self.assertIn(str(Path("data", "fsaverage", "surf", "lh.white_avg")), probe["referenceFiles"])

    def test_runtime_pins_numerical_kernels(self) -> None:
        """AVX-512 nodes gave other tensor/fODF bytes than the rest until both pins (2026-10-31)."""
        text = (ROOT / "workflow" / "runtime_env.sh").read_text(encoding="utf-8")
        self.assertIn("if grep -qw avx2 /proc/cpuinfo", text)
        self.assertIn("export OPENBLAS_CORETYPE=Haswell\n", text)
        self.assertRegex(text, r'export NPY_DISABLE_CPU_FEATURES="AVX512F [^"]*AVX512_SKX')

    def test_analysis_package_is_optional(self) -> None:
        """The `sbci` analysis package is separate: nothing of it ships here, a subject's job
        skips the .h5 and QC step without it, and the tool job says what is missing."""
        self.assertFalse((ROOT / "vendor").exists())
        self.assertFalse((ROOT / ".gitmodules").exists())
        subject = (ROOT / "workflow" / "run_subject.sbatch").read_text(encoding="utf-8")
        self.assertIn("if [ \"$rc\" -eq 0 ] && ! python3 -c 'import sbci' 2>/dev/null; then", subject)
        self.assertIn("SBCI_ANALYSIS_PACKAGE", subject)
        tool = (ROOT / "workflow" / "sbci_tool.sbatch").read_text(encoding="utf-8")
        self.assertIn("the sbci analysis package is not installed", tool)

    def test_self_test_is_one_call_before_any_subject_work(self) -> None:
        """Mode self_test has no stages: the job makes one call and ends with its result,
        before the SBCI config, the preflight and the stage loop (which would repeat the call
        ten times and end as a failure)."""
        subject = (ROOT / "workflow" / "run_subject.sbatch").read_text(encoding="utf-8")
        start = subject.index('if [ "$MODE" = self_test ]; then')
        end = subject.index("\nfi\n", start)
        block = subject[start:end]
        self.assertEqual(1, block.count("python3 run_pipeline.py "))
        self.assertIn('exit "$rc"', block)
        for later in ("sbci_stage_config", 'if [ -n "$INSPECT" ]; then', "for i in $(seq 1 10); do"):
            self.assertLess(end, subject.index(later), later)
        example = json.loads((ROOT / "workflow" / "sbci_self_test.example.json").read_text(encoding="utf-8"))
        self.assertEqual("self_test", example["mode"])


if __name__ == "__main__":
    unittest.main()
