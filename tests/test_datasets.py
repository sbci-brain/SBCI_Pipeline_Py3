"""Each dataset folder: its profile, the description of its input, and its validation record."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_validation_record as check  # noqa: E402
from workflow.workflow_executor import (  # noqa: E402
    config_setting,
    config_shells,
    profile_path,
    profile_record,
    profile_warnings,
)

DATASETS = ROOT / "datasets"
FOLDERS = sorted(path for path in DATASETS.iterdir() if path.is_dir())


class DatasetFolderTests(unittest.TestCase):
    def test_every_dataset_ships_profile_description_and_record(self) -> None:
        self.assertEqual(["abcd", "adni", "generic", "hcp-lifespan", "hcp-ya", "sms"], [path.name for path in FOLDERS])
        for folder in FOLDERS:
            with self.subTest(dataset=folder.name):
                for name in ("profile.conf", "README.md", "validation.md"):
                    self.assertTrue((folder / name).is_file(), name)
                # The description names the profile the way sbci_local.json does, and that name resolves.
                relative = f"datasets/{folder.name}/profile.conf"
                self.assertIn(relative, (folder / "README.md").read_text(encoding="utf-8"))
                self.assertEqual(str(folder / "profile.conf"), profile_path(relative))

    def test_dataset_table_agrees_with_profiles_and_records(self) -> None:
        rows = {}
        for line in (DATASETS / "README.md").read_text(encoding="utf-8").splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 6 and cells[1].startswith("[`"):
                rows[cells[1].split("`")[1].rstrip("/")] = cells
        self.assertEqual({path.name for path in FOLDERS}, set(rows))
        for folder in FOLDERS:
            with self.subTest(dataset=folder.name):
                _, _, route, profiles, validated_on, status = rows[folder.name]
                # The generic profile names no route, and its row says "any".
                self.assertEqual(config_setting(folder / "profile.conf", "INPUT_ROUTE") or "any", route.strip("`"))
                self.assertEqual(sorted(path.name for path in folder.glob("profile*.conf")),
                                 sorted(name.strip(" `") for name in profiles.split(",")))
                record = check.read_record(folder / "validation.md")
                self.assertEqual(record["Status"], status)
                self.assertEqual(record["Release"], validated_on)

    def test_historical_profile_differs_from_recommended_only_in_documented_settings(self) -> None:
        keys = ("INPUT_ROUTE", "FMRI_TR", "FMRI_SKIP_SECONDS", "FMRI_POLYFIT", "FMRI_STC", "FMRI_MOTION_REGRESSORS",
                "N_SEED", "STEPS", "RNG", "BANDWIDTH", "RESOLUTION", "SH_ORDER")
        for folder in FOLDERS:
            historical = folder / "profile.historical.conf"
            if historical.is_file():
                with self.subTest(dataset=folder.name):
                    for key in keys:
                        self.assertEqual(config_setting(folder / "profile.conf", key), config_setting(historical, key), key)
                    self.assertEqual("ssst", config_setting(historical, "FODF_MODEL"))
                    self.assertEqual("explicit", config_setting(historical, "SHELL_POLICY"))

    def test_generic_profile_takes_shells_and_tr_from_the_data_and_is_untested(self) -> None:
        """For a dataset with no folder of its own: nothing in it may tie a run to one
        acquisition, and no record may call it validated."""
        generic = DATASETS / "generic" / "profile.conf"
        self.assertEqual("all", config_setting(generic, "SHELL_POLICY"))
        self.assertEqual("auto", config_setting(generic, "FODF_MODEL"))
        for key in ("INPUT_ROUTE", "FMRI_TR", "SH_ORDER", "FODF_TOLERANCE"):
            self.assertIsNone(config_setting(generic, key), key)
        for key in ("DTI_BVALS", "FODF_BVALS"):
            self.assertIsNone(config_shells(generic, key), key)
        self.assertEqual("untested", check.read_record(DATASETS / "generic" / "validation.md")["Status"])

    def test_a_run_records_whether_a_validation_record_covers_its_profile(self) -> None:
        for folder in FOLDERS:
            for profile in sorted(folder.glob("profile*.conf")):
                with self.subTest(profile=f"{folder.name}/{profile.name}"):
                    relative = f"datasets/{folder.name}/{profile.name}"
                    record = profile_record(relative)
                    self.assertEqual(relative, record["profile"])
                    self.assertEqual(check.read_record(folder / "validation.md")["Status"], record["profile_status"])
                    self.assertEqual(config_setting(profile, "DATASET_NAME"), record["dataset_name"])
                    self.assertEqual(record, profile_record(str(profile)))
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "profile.conf"
            shipped = (DATASETS / "adni" / "profile.conf").read_bytes()
            copy.write_bytes(shipped)  # an unchanged copy is still the shipped profile
            self.assertEqual("validated", profile_record(str(copy))["profile_status"])
            self.assertEqual("datasets/adni/profile.conf", profile_record(str(copy))["profile"])
            copy.write_bytes(shipped.replace(b"SH_ORDER=6", b"SH_ORDER=8"))
            changed = profile_record(str(copy))
            self.assertEqual(("modified", str(copy), "ADNI"),
                             (changed["profile_status"], changed["profile"], changed["dataset_name"]))
            self.assertIn("not one of the profiles shipped",
                          " ".join(profile_warnings({"preprocessing_config": str(copy)}, {})))
        self.assertEqual([], profile_warnings({"preprocessing_config": "datasets/adni/profile.conf"}, {}))
        self.assertIn("untested", " ".join(profile_warnings({"preprocessing_config": "datasets/generic/profile.conf"}, {})))
        self.assertEqual({"profile": None, "dataset_name": None, "profile_status": None}, profile_record(None))
        self.assertEqual([], profile_warnings({}, {}))

    def test_example_config_names_a_shipped_profile(self) -> None:
        example = json.loads((DATASETS / "sbci_local.example.json").read_text(encoding="utf-8"))
        self.assertTrue(Path(profile_path(example["workflow"]["preprocessing_config"])).is_file())
        self.assertNotIn("runtime", example)


class ValidationRecordTests(unittest.TestCase):
    def test_shipped_records_are_well_formed(self) -> None:
        for folder in FOLDERS:
            with self.subTest(dataset=folder.name):
                self.assertEqual([], check.record_problems(check.read_record(folder / "validation.md")))

    def make_run(self, root: Path, profile: Path, version: str = "pipeline@0123456789ab") -> Path:
        run = root / "run"
        connectome = run / "sbci_output" / "files" / "work" / "sub-001" / "dwi_pipeline" / "sbci_connectome"
        connectome.mkdir(parents=True)
        for name in ("smoothed_sc_avg_0.005_ico4.mat", "sub_sc_avg_0.005_ico4.mat", "fc_avg_ico4.mat"):
            (connectome / name).write_bytes(b"matrix")
        (run / "config").mkdir()
        (run / "config" / "versions.json").write_text(json.dumps(
            {"pipeline_version": version, "container_version": "sbci-full:2026.01.01"}), encoding="utf-8")
        (run / "config" / profile.name).write_bytes(profile.read_bytes())
        return run

    def write_record(self, folder: Path, **changes: str) -> Path:
        values = {"Status": "validated", "Release": "2026.01.01", "Date": "2026-01-02", "Subject": "one subject",
                  "Profile": "profile.conf", "Pipeline version": "pipeline@0123456789ab",
                  "Image": "sbci-full:2026.01.01", "Compared with": "an earlier run", "Result": "identical", **changes}
        record = folder / "validation.md"
        record.write_text("| Field | Value |\n|---|---|\n" + "".join(f"| {k} | {v} |\n" for k, v in values.items()),
                          encoding="utf-8")
        return record

    def test_record_is_checked_against_a_real_run(self) -> None:
        """A validated record must describe the run: same code, same image, the dataset's own
        profile, and connectomes that exist."""
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "datasets" / "example"
            folder.mkdir(parents=True)
            (folder / "profile.conf").write_bytes((DATASETS / "adni" / "profile.conf").read_bytes())
            run = self.make_run(Path(temp), folder / "profile.conf")
            self.assertEqual([], check.run_problems(run, self.write_record(folder)))
            self.assertEqual(0, check.main([str(run), str(folder / "validation.md")]))

            stale = check.run_problems(run, self.write_record(folder, **{"Pipeline version": "pipeline@ffffffffffff"}))
            self.assertEqual(1, len(stale))
            self.assertIn("Pipeline version", stale[0])

            (run / "config" / "profile.conf").write_text("SH_ORDER=10\n", encoding="utf-8")
            self.assertIn("differs", " ".join(check.run_problems(run, self.write_record(folder))))

            (run / "config" / "profile.conf").write_bytes((folder / "profile.conf").read_bytes())
            next(run.rglob("fc_avg_ico4.mat")).unlink()
            self.assertIn("no fc_avg_ico4.mat", " ".join(check.run_problems(run, self.write_record(folder))))

    def test_validated_record_needs_every_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            record = check.read_record(self.write_record(Path(temp), Subject="—", Date="January"))
        problems = " ".join(check.record_problems(record))
        self.assertIn("Subject", problems)
        self.assertIn("YYYY-MM-DD", problems)


if __name__ == "__main__":
    unittest.main()
