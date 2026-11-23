"""LPS/RAS conventions in the SET/SBCI scripts: the .fib streamlines and the surfaces are LPS,
the FreeSurfer label and FA images have RAS affines."""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "pipeline"
HAVE_VTK = all(importlib.util.find_spec(name) for name in ("vtk", "nibabel", "scipy"))


@unittest.skipUnless(HAVE_VTK, "vtk, nibabel and scipy are needed to run the SET surface scripts")
class LpsCoordinateTests(unittest.TestCase):
    def test_seed_mask_samples_the_volume_under_an_lps_surface(self) -> None:
        """scil_surface_map_from_volume.py must flip the LPS ROI mesh to RAS before applying the
        volume affine; unflipped it sampled FA at (-x, -y, z) (review 2026-11-10)."""
        import nibabel as nib
        import numpy as np
        import vtk
        import vtk.util.numpy_support as ns

        with tempfile.TemporaryDirectory() as temp:
            volume = np.zeros((10, 12, 14))
            volume[2:5, 6:9, 3:6] = 1.0  # off-centre, so the mirrored point lands outside it
            affine = np.diag([2.0, 3.0, 4.0, 1.0])
            affine[:3, 3] = (-10.0, 20.0, 5.0)
            ras = affine @ np.array([3.0, 7.0, 4.0, 1.0])
            image, surface, output = Path(temp) / "fa.nii.gz", Path(temp) / "roi.vtk", Path(temp) / "mask.npy"
            nib.save(nib.Nifti1Image(volume, affine), str(image))
            points = vtk.vtkPoints()
            points.SetData(ns.numpy_to_vtk(np.array([[-ras[0], -ras[1], ras[2]]])))
            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            writer = vtk.vtkPolyDataWriter()
            writer.SetFileName(str(surface))
            writer.SetInputData(polydata)
            writer.Write()
            subprocess.run([sys.executable, str(SCRIPTS / "scil_surface_map_from_volume.py"), str(surface),
                            str(image), str(output), "-f"], check=True)
            self.assertEqual(np.load(output).tolist(), [1.0])

    @unittest.skipUnless(importlib.util.find_spec("dipy"), "trim_cortical_fibers.py imports dipy")
    def test_trim_reads_subcortical_labels_at_the_lps_point(self) -> None:
        """The .fib streamlines are LPS and the aparc affine is RAS (review 2026-11-10)."""
        import numpy as np

        sys.path.insert(0, str(SCRIPTS))
        try:
            spec = importlib.util.spec_from_file_location("trim_cortical_fibers", SCRIPTS / "trim_cortical_fibers.py")
            trim = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(trim)
        finally:
            sys.path.remove(str(SCRIPTS))
        affine = np.diag([2.0, 3.0, 4.0, 1.0])
        affine[:3, 3] = (-10.0, 20.0, 5.0)
        ras = affine @ np.array([3.0, 7.0, 4.0, 1.0])
        transform, offset = trim.lps_to_voxel(affine)
        voxel = (np.array([[-ras[0], -ras[1], ras[2]]]) @ transform + offset).astype(int)
        self.assertEqual(voxel.tolist(), [[3, 7, 4]])


if __name__ == "__main__":
    unittest.main()
