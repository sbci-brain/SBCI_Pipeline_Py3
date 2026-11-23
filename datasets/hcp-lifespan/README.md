# HCP Aging and HCP Development (HCP-Lifespan)

Route `corrected`: the DWI and BOLD come from the HCP pipelines.

## Input

```
sub-<label>/ses-<label>/
  anat/  *_T1w.nii.gz
  dwi/   *_dwi.nii.gz  *_dwi.bval  *_dwi.bvec
  func/  *_task-rest*_bold.nii.gz  *_task-rest*_motion.tsv
```

In `sbci_local.json`: `"distortion_corrected_input": true` and
`"preprocessing_config": "datasets/hcp-lifespan/profile.conf"` (or `profile.historical.conf`).

- JSON sidecars are not needed: the BOLD TR is read from the NIfTI header, which must declare
  seconds.
- Each run's `*_motion.tsv` supplies the motion regressors (`FMRI_MOTION_REGRESSORS=tsv`).

## Acquisition notes

- DWI: preprocessed, 1.5 mm; 14 b0, about 93 volumes at b ≈ 1500 (1485–1510) and about 92 at
  b ≈ 3000 (2980–3010).
- BOLD: TR 0.8 s, multiband, 478 frames, one rest run; no slice-timing correction.

## The two profiles

| | `profile.conf` | `profile.historical.conf` |
|---|---|---|
| fODF | multi-shell multi-tissue CSD on every volume of both shells | single-shell CSD fitted to b = 1500 and 3000 together |
| Shell tolerance | 50 | 19, which leaves out the b = 2980 volumes as the historical outputs did |
| Use for | new processing | results pooled with the lab's existing HCP outputs |
