# Generic profile: a dataset that is not listed

For data that has no folder in `datasets/`. **It is not validated on any dataset.** A run with
it says so in the preflight, in the job log and in `config/versions.json`
(`"profile_status": "untested"`), and you check the outputs yourself.

## Input

The layout in [`../README.md`](../README.md), with BIDS JSON sidecars. In `sbci_local.json`:
`"preprocessing_config": "datasets/generic/profile.conf"`, and the route of your input, which
this profile does not check:

| Your input | `distortion_corrected_input` | `bold_topup_fieldmaps` |
|---|---|---|
| Scanner output; the pipeline corrects DWI and BOLD (route `raw`) | `false` | `false` |
| DWI and BOLD both corrected upstream (route `corrected`) | `true` | `false` |
| DWI corrected upstream, BOLD with a spin-echo fieldmap pair (route `corrected_dwi_raw_bold`) | `true` | `true` |

Never declare uncorrected data as corrected: the run would finish on uncorrected data. The
JSON fields each route reads are those of the listed dataset with the same route:
[`adni/`](../adni/README.md) (`raw`), [`abcd/`](../abcd/README.md) (`corrected`),
[`hcp-ya/`](../hcp-ya/README.md) (`corrected_dwi_raw_bold`).

## What comes from the data

| Setting | How |
|---|---|
| b-value shells | b-values rounded to the nearest 100, so a shell holds the volumes within 50 of it. Every non-zero shell reaches the fODF. |
| Tensor fit | The shell nearest b = 1000 (of two equally near, the higher). The run warns when that shell is outside b = 700–1500, where the tensor model fits poorly. |
| fODF model | One shell: single-shell CSD. Several: multi-shell multi-tissue CSD. |
| SH order | 6 for one shell, 8 for several. With too few gradient directions (28 for order 6, 45 for order 8) the fODF stage stops and names the highest order the data support. That is after FreeSurfer, hours into the run, and the preflight does not check it: count your directions first, and with too few set `SH_ORDER` in a copy of the profile (below). |
| fMRI TR | The BOLD's BIDS `RepetitionTime`. |

## What does not, and is fixed in the profile

| Setting | Value | Change it when |
|---|---|---|
| `FMRI_STC` | `none` (no slice-timing correction) | single-band BOLD acquired in a known order: `siemens`, `up`, `down`, `odd` or `even` |
| `FMRI_SKIP_SECONDS` | `12` | the scanner already discarded its dummy scans, or the TR is short |
| `FMRI_MOTION_REGRESSORS` | `mcextreg` (FSFast's estimates) | the BOLD came motion-corrected with a BIDS `*_motion.tsv`: `tsv` |
| `FMRI_POLYFIT` | `2` | — |
| `N_RUNS`, `N_SEED`, `STEPS`, `RNG`, `BANDWIDTH`, `ROIS`, `RESOLUTION` | as the listed datasets | — |

To change one, copy `datasets/generic/profile.conf` to a file of your own, edit the line, and
give that file's path as `preprocessing_config`. Such a run records
`"profile_status": "modified"`. To make the dataset a listed one, see "Adding a dataset" in
[`../README.md`](../README.md).
