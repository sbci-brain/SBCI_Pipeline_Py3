# ADNI

Route `raw`: scanner output in BIDS. The pipeline corrects it.

## Input

```
sub-<label>[/ses-<label>]/
  anat/  *_T1w.nii.gz
  dwi/   *_dwi.nii.gz  *_dwi.bval  *_dwi.bvec  *_dwi.json
  func/  *_task-rest*_bold.nii.gz  *_task-rest*_bold.json
```

JSON fields that are read, with no defaults assumed:

| File | Fields |
|---|---|
| `*_dwi.json` | `PhaseEncodingDirection`; `TotalReadoutTime` (or `EffectiveEchoSpacing` with `ReconMatrixPE`) |
| `*_bold.json` | `RepetitionTime`, `EffectiveEchoSpacing`, `PhaseEncodingDirection` |

In `sbci_local.json`: `"preprocessing_config": "datasets/adni/profile.conf"` and your FreeSurfer
licence. Leave `distortion_corrected_input` and `bold_topup_fieldmaps` false.

## Acquisition notes

- DWI is acquired in one phase-encoding direction, with no reverse b0. Synb0-DISCO makes an
  undistorted b0 from the T1, then topup and eddy correct the series.
- The DWI protocols are mixed: (0, 1000), (0, 500, 1000, 2000) and (0, 2000). The profile keeps
  the single shell nearest b = 1000 for both the tensor fit and the fODF (`SHELL_POLICY=nearest_1000`).
- BOLD: TR 3 s, 48 slices, Siemens interleaved; one rest run. `FMRI_TR=3` keeps other ADNI
  protocols (for example multiband at TR 0.6 s) from being processed with these settings.
- The BOLD is unwarped with FUGUE, using the field map topup estimated from the DWI.
- Synb0's own registration seeds from the clock, so two runs of the same subject differ
  slightly from the DWI correction on (eddy-corrected data correlate at about 0.998).
