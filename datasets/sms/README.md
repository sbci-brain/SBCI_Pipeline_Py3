# SMS protocol study (Siemens Prisma fit, 3 T)

A study that compares diffusion protocols with different simultaneous multi-slice (SMS)
factors. Route `corrected_dwi_raw_bold`: the DWI is already corrected with topup and eddy;
the BOLD is the raw rest run, corrected here with topup on its spin-echo fieldmap pair.

**This profile is untested on this release.** A run with it says so in the preflight, in the
job log and in `config/versions.json` (`"profile_status": "untested"`). The data are not
public.

## Input

```
sub-<label>/
  anat/  *_T1w.nii.gz
  dwi/   *_dwi.nii.gz  *.bval  *.bvec  *_dwi.json      corrected with topup and eddy
  func/  *_task-rest*_bold.nii.gz  *_bold.json         raw
  fmap/  *_dir-AP_epi.nii.gz  *_dir-PA_epi.nii.gz  and a JSON for each
```

| File | Fields |
|---|---|
| `*_bold.json` | `RepetitionTime` 1.09, `PhaseEncodingDirection` `j-`, `EffectiveEchoSpacing` |
| `fmap/*_epi.json` | `PhaseEncodingDirection` (`j-` and `j`) and `TotalReadoutTime` 0.0711 |

The two fieldmap images and the BOLD must share one voxel grid.

In `sbci_local.json`: `"distortion_corrected_input": true`, `"bold_topup_fieldmaps": true` and
`"preprocessing_config": "datasets/sms/profile.conf"`.

## Acquisition notes

- DWI: one of five protocols that differ in the SMS factor (1, 3 or 6) and the in-plane
  acceleration (1 or 2). Each has 141 volumes: 13 at b = 0, 26 at b = 1000 and 102 at
  b = 2000. The tensor fit uses b = 1000; both shells reach the fODF (multi-shell
  multi-tissue CSD, SH order 8).
- BOLD: TR 1.09 s, 300 frames, multiband 6. Slice-timing correction is off (FSFast's orders
  do not describe multiband acquisition), and FSFast's own motion estimates are the regressors.
- At SMS factor 6 with in-plane acceleration 2 the scans are noisy; the fODF stage widens the
  region it estimates the fibre response from when too few voxels qualify.
