# Validation: ABCD

| Field | Value |
|---|---|
| Status | validated |
| Release | 20261122 |
| Date | 2026-11-21 |
| Subject | one ABCD participant |
| Profile | profile.conf |
| Pipeline version | `new-ef92c5c@c02bc9b90e84` |
| Image | `sbci-full:ws2-20261119@ef92c5cf00893e8d72af78a8203876dd78f14797` |
| Compared with | the same subject run with the previous code and image (November 2026) |
| Result | All 24 FreeSurfer surfaces, cortical SC, both subcortical SC matrices and FC are identical in every value; the same holds with `profile.historical.conf`. QC passed. |

`python3 tests/check_validation_record.py <run folder> <this file>` checks the record against
the run.

## Earlier releases

On the October 2026 code, three ABCD subjects matched the lab's existing database at cortical
SC r 0.976–0.978 and cortical FC r 0.912–0.991.
