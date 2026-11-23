"""Nothing that identifies a subject, a person or a site's folders is in the published files.

Published means every tracked file that .gitattributes does not mark `export-ignore`
(`git archive` leaves those out). Two lists are checked: the shapes below, which say nothing
about anyone, and, in a checkout that has it, the lab's own list of terms.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB_TERMS = ROOT / "lab" / "public_scan_terms.txt"

# A subject label that is not an obvious placeholder (sub-001, sub-01, sub-XXXX, sub-*, sub-<id>).
SUBJECT = re.compile(r"sub-(?!(?:\d{1,3}|X+|sub)(?![A-Za-z0-9]))[A-Za-z0-9]+")
SHAPES = {
    "a subject label": SUBJECT,
    "an NDAR identifier": re.compile(r"NDAR[A-Z0-9_]{4,}"),
    "an ADNI subject identifier": re.compile(r"(?<![A-Za-z0-9])\d{3}_?S_?\d{4}(?![A-Za-z0-9])"),
    "a home or scratch folder": re.compile(r"/(?:work/)?users/[a-z]/[a-z]/|/home/[a-z][a-z0-9_]+/|[A-Z]:[\\/]Users[\\/]"),
}


def export_ignored() -> tuple[str, ...]:
    prefixes = []
    for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
        words = line.split()
        if len(words) >= 2 and "export-ignore" in words[1:] and not line.lstrip().startswith("#"):
            prefixes.append(words[0])
    return tuple(prefixes)


def published_files() -> list[Path]:
    git = shutil.which("git")
    listed = subprocess.run([git, "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=False) if git else None
    if listed is not None and listed.returncode == 0 and listed.stdout:
        names = [name for name in listed.stdout.decode("utf-8").split("\0") if name]
    else:  # not a git checkout (an unpacked archive): every file there is
        names = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".sif"]
    ignored = export_ignored()
    return [ROOT / name for name in names if not name.startswith(ignored) and (ROOT / name).is_file()]


def text_of(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data:
        return None  # binary: reference surfaces, the concon kernel parts
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class PublicTreeTests(unittest.TestCase):
    def scan(self, patterns: dict[str, re.Pattern[str]]) -> list[str]:
        found = []
        for path in published_files():
            text = text_of(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for label, pattern in patterns.items():
                    match = pattern.search(line)
                    if match:
                        found.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {label}: {match.group(0)}")
        return found

    def test_lab_only_folders_are_left_out_of_an_export(self) -> None:
        self.assertEqual({"container/", "lab/"}, set(export_ignored()))
        files = {path.relative_to(ROOT).as_posix() for path in published_files()}
        self.assertIn("run_pipeline.py", files)
        self.assertFalse([name for name in files if name.startswith(("container/", "lab/", "vendor/"))])

    def test_no_subject_or_personal_identifier_shapes(self) -> None:
        self.assertEqual([], self.scan(SHAPES))

    def test_subject_shape_accepts_placeholders_only(self) -> None:
        for label in ("sub-001", "sub-01", "sub-XXXX", "sub-*", "sub-<id>", "sub-$ID", "sub-sub"):
            self.assertIsNone(SUBJECT.search(label), label)
        # Made-up labels in the shapes real ones have, put together here so that this file passes its own scan.
        for tail in ("1234567", "123" + "S4567", "NDAR" + "INV0AB1CD2E", "ABC1234567", "0001x"):
            self.assertIsNotNone(SUBJECT.search("sub-" + tail), tail)

    @unittest.skipUnless(LAB_TERMS.is_file(), "the lab's term list is not part of a public checkout")
    def test_no_lab_specific_terms(self) -> None:
        terms = [line.strip() for line in LAB_TERMS.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertGreater(len(terms), 5)
        self.assertEqual([], self.scan({f"lab term {term}": re.compile(term) for term in terms}))


if __name__ == "__main__":
    unittest.main()
