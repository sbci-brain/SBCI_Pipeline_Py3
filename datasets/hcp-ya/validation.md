# Validation: HCP Young Adult

| Field | Value |
|---|---|
| Status | validated |
| Release | 20261122 |
| Date | 2026-11-21 |
| Subject | one HCP Young Adult participant |
| Profile | profile.conf |
| Pipeline version | `new-ef92c5c@c02bc9b90e84` |
| Image | `sbci-full:ws2-20261119@ef92c5cf00893e8d72af78a8203876dd78f14797` |
| Compared with | the same subject run with the previous code and image (November 2026) |
| Result | All 24 FreeSurfer surfaces, cortical SC, both subcortical SC matrices and FC are identical in every value. QC passed. |

`python3 tests/check_validation_record.py <run folder> <this file>` checks the record against
the run.
