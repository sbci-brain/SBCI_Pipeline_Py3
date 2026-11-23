# HCP Young Adult (WU-Minn 1200 release, 3T)

Route `corrected_dwi_raw_bold`: the DWI is HCP's minimally preprocessed output; the BOLD is
the unprocessed rest run, corrected here with topup on its spin-echo fieldmap pair.

## Input

A BIDS view of HCP's packages:

```
sub-<label>/
  anat/  *_T1w.nii.gz                      T1w/T1w_acpc_dc_restore.nii.gz (0.7 mm, bias-corrected)
  dwi/   *_dwi.nii.gz  *.bval  *.bvec      T1w/Diffusion (topup, eddy and gradient nonlinearity applied)
  func/  *_task-rest*_bold.nii.gz  *_bold.json     unprocessed rfMRI_REST1_LR
  fmap/  *_dir-LR_epi.nii.gz  *_dir-RL_epi.nii.gz  and a JSON for each     SpinEchoFieldMap_LR / _RL
```

HCP publishes no BIDS sidecars, so they are written from HCP's protocol:

| File | Fields |
|---|---|
| `*_bold.json` | `RepetitionTime` 0.72, `PhaseEncodingDirection` `i-` (LR), `EffectiveEchoSpacing` 0.00058 |
| `fmap/*_epi.json` | `PhaseEncodingDirection` (`i-` for LR, `i` for RL) and `TotalReadoutTime` |

The two fieldmap images and the BOLD must share one voxel grid.

In `sbci_local.json`: `"distortion_corrected_input": true`, `"bold_topup_fieldmaps": true` and
`"preprocessing_config": "datasets/hcp-ya/profile.conf"`.

## Acquisition notes

- DWI: 1.25 mm; b ≈ 5 volumes and three shells at b ≈ 1000, 2000 and 3000, 90 directions
  each. All shells reach the fODF (multi-shell multi-tissue CSD).
- BOLD: TR 0.72 s, 1200 frames, multiband 8. Slice-timing correction is off (FSFast's orders
  do not describe multiband acquisition), and FSFast's own motion estimates are the regressors.
- HCP's gradient-nonlinearity field (`grad_dev.nii.gz`) is not used.
