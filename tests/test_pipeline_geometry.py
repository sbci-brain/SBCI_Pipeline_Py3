"""Coordinate conventions in the SET/SBCI scripts (pipeline/) (reviews 2026-10-05 and 2026-11-10)."""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "pipeline"
HAVE_VTK = all(importlib.util.find_spec(name) for name in ("vtk", "nibabel", "scipy"))


@unittest.skipUnless(HAVE_VTK, "vtk, nibabel and scipy are needed to run the SET surface scripts")
class RoiSurfaceCoordinateTests(unittest.TestCase):
    def test_roi_surface_is_written_in_lps_like_the_cortical_surfaces(self) -> None:
        """--vox2vtk must give LPS, as legacy trimeshpy vox_to_vtk did (diag(-1,-1,1,1) @ affine);
        step 2 flips the cortical surfaces to LPS and concatenates the ROI meshes with them."""
        import nibabel as nib
        import numpy as np
        import vtk
        import vtk.util.numpy_support as ns

        with tempfile.TemporaryDirectory() as temp:
            volume = np.zeros((10, 12, 14), dtype=np.int16)
            volume[2:5, 6:9, 3:6] = 17  # an off-centre block, so a mirror cannot pass
            affine = np.diag([2.0, 3.0, 4.0, 1.0])
            affine[:3, 3] = (-10.0, 20.0, 5.0)
            labels, surface = Path(temp) / "labels.nii.gz", Path(temp) / "roi.vtk"
            nib.save(nib.Nifti1Image(volume, affine), str(labels))
            subprocess.run([sys.executable, str(SCRIPTS / "scil_surface_from_volume.py"), str(labels),
                            str(surface), "--index", "17", "--vox2vtk", "-f"], check=True)
            reader = vtk.vtkPolyDataReader()
            reader.SetFileName(str(surface))
            reader.Update()
            centroid = ns.vtk_to_numpy(reader.GetOutput().GetPoints().GetData()).mean(axis=0)
        ras = affine @ np.array([3.0, 7.0, 4.0, 1.0])  # block centre (2..4, 6..8, 3..5)
        np.testing.assert_allclose(centroid, [-ras[0], -ras[1], ras[2]], atol=0.5)

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
