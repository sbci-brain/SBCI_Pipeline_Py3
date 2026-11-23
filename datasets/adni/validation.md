# Validation: ADNI

| Field | Value |
|---|---|
| Status | validated |
| Release | 20261122 |
| Date | 2026-11-21 |
| Subject | one ADNI participant |
| Profile | profile.conf |
| Pipeline version | `new-ef92c5c@c02bc9b90e84` |
| Image | `sbci-full:ws2-20261119@ef92c5cf00893e8d72af78a8203876dd78f14797` |
| Compared with | the same subject run with the previous code and image (November 2026) |
| Result | Not identical, and not expected to be: see below. With the Synb0 output held fixed: cortical SC r 0.995, FC r 0.994, corrected diffusion data r 0.99996, white-surface vertex counts within 0.9% per hemisphere. QC passed. |

## Run-to-run variation

This dataset takes the raw-input route (Synb0-DISCO, topup, eddy). Two of its steps give
slightly different output on every run, with the same code, image and inputs: Synb0-DISCO
seeds a registration from the clock, and FSL eddy with 16 threads is not repeatable. The
difference grows downstream because FreeSurfer runs on the T1 after its registration to the
diffusion image.

Two runs of the previous code and image, same subject:

| What varies | Cortical SC r | FC r | White-surface vertices |
|---|---|---|---|
| Synb0-DISCO and eddy | 0.986 | 0.991 | 2.6% apart |
| eddy only (Synb0 output held fixed) | 0.995 | 0.995 | 0.4% apart |

The release's difference from the previous code and image is the same size as this variation.
The two subcortical SC matrices vary more between runs (r 0.96 to 0.99 for the 19 x 19
subcortical matrix, 0.68 to 0.80 for subcortical-to-surface) and were not used as criteria.

`python3 tests/check_validation_record.py <run folder> <this file>` checks the record against
the run.
