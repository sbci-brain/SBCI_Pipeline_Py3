# Datasets

Every dataset runs the same code. A dataset's folder holds what differs: its profile (the
settings below), the input layout it expects, and the record of its validation.

| Dataset | Folder | Input route | Profiles | Validated on | Status |
|---|---|---|---|---|---|
| ADNI | [`adni/`](adni/) | `raw` | `profile.conf` | 2026.11.22 | validated |
| ABCD | [`abcd/`](abcd/) | `corrected` | `profile.conf`, `profile.historical.conf` | 2026.11.22 | validated |
| HCP Aging / Development | [`hcp-lifespan/`](hcp-lifespan/) | `corrected` | `profile.conf`, `profile.historical.conf` | 2026.11.22 | validated |
| HCP Young Adult | [`hcp-ya/`](hcp-ya/) | `corrected_dwi_raw_bold` | `profile.conf` | 2026.11.22 | validated |

"Validated on" names the release a subject of that dataset last ran on end to end, with the
numbers in the folder's `validation.md`. A dataset marked untested has no such run on this
release.

## What a profile sets

A profile is a file of `NAME=value` lines that the stage scripts read. It holds settings only:
tool paths come from the image, and the paths of a run from that run's `sbci_local.json`.

| Setting | Meaning |
|---|---|
| `INPUT_ROUTE` | `raw`, `corrected` or `corrected_dwi_raw_bold`: the input the profile is written for. The preflight stops when `sbci_local.json` describes another. |
| `SHELL_POLICY`, `DTI_BVALS`, `FODF_BVALS`, `FODF_TOLERANCE` | which b-value shells reach the tensor fit and the fODF |
| `FODF_MODEL`, `SH_ORDER` | `auto` (single-shell CSD for one shell, multi-shell multi-tissue CSD for several) or `ssst`; the spherical-harmonic order |
| `FMRI_TR` | the repetition time the profile is written for; a subject with another TR is refused |
| `FMRI_SKIP_SECONDS`, `FMRI_POLYFIT`, `FMRI_STC` | leading time discarded, detrending order, slice-timing order (or `none`) |
| `FMRI_MOTION_REGRESSORS` | `mcextreg` (FSFast's own estimates) or `tsv` (each run's BIDS `*_motion.tsv`) |
| `N_RUNS`, `N_SEED`, `STEPS`, `RNG`, `BANDWIDTH`, `ROIS`, `RESOLUTION` | SET tractography repetitions and seeds, the SC smoothing bandwidth, the subcortical regions, the grid |

## Recommended and historical profiles

`profile.conf` is the recommended profile for new processing. Where a dataset also has
`profile.historical.conf`, that one reproduces how an existing lab database was built
(single-shell fODF fitted to several shells, tolerance 19, explicit shells), for results that
are to be pooled with that database. The two give structural results that are not
interchangeable.

## Input layout

`IN` holds `sbci_local.json` and one subject folder (`sub-<label>`, or `sub-<label>/ses-<label>`):

```
IN/
  sbci_local.json
  sub-<label>/
    anat/  *_T1w.nii.gz
    dwi/   *_dwi.nii.gz  *_dwi.bval  *_dwi.bvec  *_dwi.json
    func/  *_task-rest*_bold.nii.gz  *_task-rest*_bold.json  [*_task-rest*_motion.tsv]
    fmap/  *_epi.nii.gz  *_epi.json          (route corrected_dwi_raw_bold only)
```

Each dataset's `README.md` says which of these it needs and which JSON fields are read.

## Adding a dataset

Copy the closest profile into a new folder, set the values above for the acquisition, describe
the input in a `README.md`, and add a `validation.md` (status `untested` until a subject has
run end to end). `tests/test_datasets.py` checks the folder.
