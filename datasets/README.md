# Datasets

Every dataset runs the same code. A dataset's folder holds what differs: its profile (the
settings below), the input layout it expects, and the record of its validation.

| Dataset | Folder | Input route | Profiles | Validated on | Status |
|---|---|---|---|---|---|
| ADNI | [`adni/`](adni/) | `raw` | `profile.conf` | 20261122 | validated |
| ABCD | [`abcd/`](abcd/) | `corrected` | `profile.conf`, `profile.historical.conf` | 20261122 | validated |
| HCP Aging / Development | [`hcp-lifespan/`](hcp-lifespan/) | `corrected` | `profile.conf`, `profile.historical.conf` | 20261122 | validated |
| HCP Young Adult | [`hcp-ya/`](hcp-ya/) | `corrected_dwi_raw_bold` | `profile.conf` | 20261122 | validated |
| SMS protocol study | [`sms/`](sms/) | `corrected_dwi_raw_bold` | `profile.conf` | — | untested |
| Any other dataset | [`generic/`](generic/) | any | `profile.conf` | — | untested |

"Validated on" names the release a subject of that dataset last ran on end to end, with the
numbers in the folder's `validation.md`. A dataset marked untested has no such run on this
release.

## What a run says about its profile

The pipeline does not detect the dataset: a run uses the profile that `sbci_local.json` names.
Each run writes into `OUT/config/versions.json` the profile, its `DATASET_NAME` and a
`profile_status`:

| `profile_status` | Meaning |
|---|---|
| `validated` | the file equals a shipped profile whose dataset is marked validated above |
| `untested` | the file equals a shipped profile marked untested above |
| `modified` | any other file: a shipped profile that was changed, or one you wrote |

`validated` says the profile was validated on its own dataset. Nothing can show that your
data are that dataset, so these are compared with the data, in the preflight
(`--inspect`) and again in the job log:

| Check | When it does not hold |
|---|---|
| `INPUT_ROUTE` against the correction settings in `sbci_local.json` | the run stops |
| `FMRI_TR` against the subject's TR | the run stops |
| the shells taken from the data against the profile's `DTI_BVALS` / `FODF_BVALS` | warning |
| the tensor shell within b = 700–1500 | warning |
| `profile_status` is `untested` or `modified` | warning |

A warning does not stop a run.

## What a profile sets

A profile is a file of `NAME=value` lines that the stage scripts read. It holds settings only:
tool paths come from the image, and the paths of a run from that run's `sbci_local.json`.

| Setting | Meaning |
|---|---|
| `DATASET_NAME` | a label for the dataset. Nothing is checked against it; it is written into `config/versions.json`. |
| `INPUT_ROUTE` | `raw`, `corrected` or `corrected_dwi_raw_bold`: the input the profile is written for. The preflight stops when `sbci_local.json` describes another. Without it the route is not checked. |
| `SHELL_POLICY` | which b-value shells reach the tensor fit and the fODF: see below |
| `DTI_BVALS`, `FODF_BVALS` | under `explicit`, the shells to use. Under the other two policies, the shells the profile expects: the run warns when the data give others. |
| `FODF_TOLERANCE` | under `explicit` and `nearest_1000`, how far a b-value may be from its shell to reach the fODF (default 20). Under `all` it is 50. |
| `FODF_MODEL`, `SH_ORDER` | `auto` (single-shell CSD for one shell, multi-shell multi-tissue CSD for several) or `ssst`; the spherical-harmonic order (default 6 for one shell, 8 for several; the fODF stage stops at an order the gradient directions cannot support) |
| `FMRI_TR` | the repetition time the profile is written for; a subject with another TR is refused. Without it the TR is the subject's BIDS `RepetitionTime`. |
| `FMRI_SKIP_SECONDS`, `FMRI_POLYFIT`, `FMRI_STC` | leading time discarded, detrending order, slice-timing order (or `none`) |
| `FMRI_MOTION_REGRESSORS` | `mcextreg` (FSFast's own estimates) or `tsv` (each run's BIDS `*_motion.tsv`) |
| `N_RUNS`, `N_SEED`, `STEPS`, `RNG`, `BANDWIDTH`, `ROIS`, `RESOLUTION` | SET tractography repetitions and seeds, the SC smoothing bandwidth, the subcortical regions, the grid |

## b-value shells

A shell is the b-values that round to the same 100 (1485 to 1510 are the shell 1500; below
50 is b = 0).

| `SHELL_POLICY` | Tensor fit | fODF |
|---|---|---|
| `nearest_1000` | the shell nearest b = 1000 | the same shell |
| `all` | the shell nearest b = 1000 | every non-zero shell |
| `explicit` | `DTI_BVALS` as written | `FODF_BVALS` as written |

- The tensor fit stays on one shell also when the fODF uses all of them: the tensor model
  holds up to about b = 1500, and its FA drives the T1-to-DWI registration. Of two shells
  equally near 1000 (500 and 1500), the higher is used. Where the nearest shell is outside
  b = 700–1500 (data with b = 2000 only, for example) the run continues with a warning.
- Under `all`, a volume belongs to a shell when its b-value is within 50 of it, for the
  tensor fit and the fODF alike, so every volume of a shell is used. Under the other two
  policies the distance is 20 (`FODF_TOLERANCE` changes it for the fODF), and a volume
  further from its shell is left out.
- Single-shell data needs no `SHELL_POLICY`. Multi-shell data with none stops the
  registration stage before it starts.

### SH order and the number of directions

An SH order L needs (L+1)(L+2)/2 distinct gradient directions among the fODF volumes: 28 for
order 6, 45 for order 8. The order is `SH_ORDER`, or without it 6 for one shell and 8 for
several. This is checked when the fODF stage starts, which is after FreeSurfer: data with too
few directions stop there, hours into the run, with a message that names the highest order
they support. The preflight does not check it. So count the directions of a new dataset
before the first run, and set `SH_ORDER` in a copy of the profile if they are too few.

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

## A dataset that is not listed

Use [`generic/`](generic/): `"preprocessing_config": "datasets/generic/profile.conf"`. It takes
the shells and the TR from the data, and its [`README.md`](generic/README.md) lists what it
cannot take from the data and how to change that. It is not validated on any dataset, and
every run with it says so.

## Adding a dataset

Copy the closest profile into a new folder, set the values above for the acquisition, describe
the input in a `README.md`, and add a `validation.md` (status `untested` until a subject has
run end to end). Add the folder to the table at the top. `tests/test_datasets.py` checks the
folder.
