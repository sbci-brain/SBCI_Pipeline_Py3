# ABCD

Route `corrected`: the DWI and BOLD were distortion and eddy corrected upstream.

## Input

```
sub-<label>/ses-<label>/
  anat/  *_T1w.nii.gz
  dwi/   *_dwi.nii.gz  *_dwi.bval  *_dwi.bvec
  func/  *_task-rest_run-<n>_bold.nii.gz  *_task-rest_run-<n>_bold.json  *_task-rest_run-<n>_motion.tsv
```

In `sbci_local.json`: `"distortion_corrected_input": true` and
`"preprocessing_config": "datasets/abcd/profile.conf"` (or `profile.historical.conf`).

- Every rest run is processed and the SBCI stage concatenates them. All runs must share one TR.
- Each run's `*_motion.tsv` supplies the motion regressors (`FMRI_MOTION_REGRESSORS=tsv`): a
  header line, a frame index column, then six motion columns.
- The BOLD TR is read from the JSON, or from the NIfTI header when it declares its time unit.

## Acquisition notes

- DWI: 7 b0, then b = 500 (6 directions), 1000 (15), 2000 (15) and 3000 (60).
- BOLD: TR 0.8 s, multiband, 383 frames per run; no slice-timing correction (`FMRI_STC=none`).

## The two profiles

| | `profile.conf` | `profile.historical.conf` |
|---|---|---|
| fODF | multi-shell multi-tissue CSD on all four shells | single-shell CSD fitted to b = 1000, 2000 and 3000 together |
| Shell tolerance | 50 | 19 |
| SET repetitions (`N_RUNS`) | 4 | 3 |
| Use for | new processing | results pooled with the lab's existing ABCD database |
