from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# nibabel is a dependency of the image's Python environments (requirements.txt);
# a bare host test interpreter may lack it, so skip rather than error there.
HAS_NIBABEL = importlib.util.find_spec("nibabel") is not None

if HAS_NIBABEL:
    from workflow.subject_metadata_probe import reference_surface_vertex_counts


@unittest.skipUnless(HAS_NIBABEL, "nibabel is not installed in this host interpreter; it is provided by the SBCI container")
class SubjectMetadataProbeTests(unittest.TestCase):
    def test_fsfast_vertex_expectation_comes_from_fsaverage_reference(self) -> None:
        errors: list[dict[str, str]] = []

        counts = reference_surface_vertex_counts(ROOT, errors)

        self.assertEqual([], errors)
        self.assertEqual({"lh": 163842, "rh": 163842}, counts)


if __name__ == "__main__":
    unittest.main()
