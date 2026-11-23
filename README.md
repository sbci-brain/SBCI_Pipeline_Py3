# SBCI Pipeline (Python 3)

From one subject's MRI to continuous structural and functional connectomes on the cortical
surface: distortion correction, FreeSurfer, diffusion modelling, surface-enhanced
tractography, resting-state cleaning, and connectivity on the ico4 grid, in one job, inside
one container image.

SBCI, Surface-Based Connectivity Integration (Cole et al., *Human Brain Mapping*, 2021),
represents structural connectivity (SC) and functional connectivity (FC) as functions on the
white-matter surface instead of between atlas regions. The ico4 grid is the icosahedron
subdivided four times: 2,562 vertices per hemisphere, 5,124 in all, left then right. A
connectome is a 5,124 × 5,124 matrix over that grid; the pipeline also writes the
connectivity of 19 subcortical regions with each other and with the surface.

This repository is the Python 3 version of
[SBCI_Pipeline](https://github.com/sbci-brain/SBCI_Pipeline). The stage scripts are that
pipeline's, moved to Python 3 and scilpy 2.x; around them is a workflow that plans the
stages, checks each one's outputs, resumes after an interruption and runs everything in the
`sbci-full` image.

**Status: release 20261122.** One subject each of ADNI, ABCD, HCP Aging and HCP Young
Adult has run end to end on this layout; the numbers are in each dataset's `validation.md`
([Datasets](#datasets)).

## One subject in three commands

On Linux (or WSL) with Apptainer and MATLAB on `PATH` (on a cluster, load both before
submitting, e.g. `module load apptainer matlab`):

```bash
git clone https://github.com/sbci-brain/SBCI_Pipeline_Py3.git && cd SBCI_Pipeline_Py3
bash workflow/get_image.sbatch                                   # once: fetches the image, about 11 GB
bash workflow/submit.sh --output=OUT/slurm-%j.out workflow/run_subject.sbatch IN OUT
```

`IN` holds one subject folder and an `sbci_local.json` ([below](#sbci_localjson)); `OUT`
must exist. `submit.sh` is `sbatch` where there is Slurm and a background job elsewhere.
Before the long run, the same script gives a preflight in seconds:

```bash
bash workflow/run_subject.sbatch --inspect IN OUT
```

It prints whether the run is ready, each stage's state and missing commands, and anything
that blocks it. The run itself takes 6–18 h on 16 CPUs (the job asks for 16 CPUs, 64 GB and
48 h) and writes about 30 GB. A rerun on the same `OUT` resumes after the last completed
stage.

## What it needs

| | |
|---|---|
| **Linux (or WSL) with Apptainer** | Every stage runs inside the image. |
| **The image `sbci-full`** | Holds every tool: FSL 6.0.7.18, ANTs 2.6.0, MRtrix3 3.0.8, FreeSurfer 6.0.0, Synb0-DISCO v3.1 and two Python environments. `workflow/get_image.sbatch` fetches `sdszhauk/sbci-image:20261122` from Docker Hub, pinned by digest, to `sbci-full.sif` in this folder: about 11 GB to download, then 2–3 hours packing on 4 cores, and room for about three times that while it builds. It needs internet, so on a cluster run it where there is some. Set `SBCI_FULL_IMAGE` to use a copy elsewhere. The recipe is in [SBCI_Images](https://github.com/sbci-brain/SBCI_Images). |
| **MATLAB** (R2018b validated, a Linux install) on `PATH` | For the FSFast stage (`selxavg3-sess`); the job binds it into the image. It is not optional: the SBCI stage runs only after every preprocessing stage, so without MATLAB no subject finishes, SC included. FreeSurfer 6's compiled `selxavg3` does not replace it (it needs MATLAB Runtime R2014a; FreeSurfer 6 ships R2012b). |
| **A FreeSurfer licence file** | Free from FreeSurfer's site; the image has none. Its path goes in `sbci_local.json`. |
| **16 CPUs, 64 GB of memory, about 30 GB of disk per subject** | What `run_subject.sbatch` asks Slurm for, and what the validation runs used. |

## Input

`IN` holds `sbci_local.json` and one subject folder in BIDS (`sub-<label>`, or
`sub-<label>/ses-<label>`):

```
IN/
  sbci_local.json
  sub-<label>/
    anat/  *_T1w.nii.gz
    dwi/   *_dwi.nii.gz  *_dwi.bval  *_dwi.bvec  *_dwi.json
    func/  *_task-rest*_bold.nii.gz  *_task-rest*_bold.json  [*_task-rest*_motion.tsv]
    fmap/  *_epi.nii.gz  *_epi.json          (route corrected_dwi_raw_bold only)
```

There are three input routes. A dataset's profile names the route it is written for, and the
preflight stops when `sbci_local.json` describes another.

| Route | Input | The pipeline does |
|---|---|---|
| `raw` | scanner output in BIDS, DWI in one phase-encoding direction | Synb0-DISCO + topup + eddy on the DWI; FUGUE on the BOLD, with the DWI's field map |
| `corrected` | DWI and BOLD corrected upstream | nothing; the two correction stages copy the images on |
| `corrected_dwi_raw_bold` | DWI corrected upstream, raw BOLD with a spin-echo fieldmap pair | topup on the pair, applytopup on the BOLD |

Each dataset's `README.md` lists the files it needs and the JSON fields that are read. The
phase-encoding direction, readout time and repetition time come from those fields; none has
a default.

### `sbci_local.json`

Copy `datasets/sbci_local.example.json`:

```json
{
  "schema_version": "1.0",
  "mode": "preprocess",
  "approved": true,
  "workflow": {
    "preprocessing_config": "datasets/adni/profile.conf",
    "freesurfer_license": "/path/to/your/freesurfer/license.txt",
    "distortion_corrected_input": false,
    "bold_topup_fieldmaps": false,
    "stages": "all"
  }
}
```

| Setting | Meaning |
|---|---|
| `workflow.preprocessing_config` | The dataset profile: its place in this repository (`datasets/adni/profile.conf`) or a path to your own copy. |
| `workflow.freesurfer_license` | The path of your FreeSurfer `license.txt`. |
| `workflow.distortion_corrected_input` | `true` when DWI and BOLD were corrected upstream (ABCD, HCP). The correction stages then run no command and need no phase-encoding metadata or Synb0. |
| `workflow.bold_topup_fieldmaps` | `true` to correct raw BOLD with the spin-echo pair in `fmap/` (two `*_epi` images with opposite `PhaseEncodingDirection` and a `TotalReadoutTime` each). It combines with an upstream-corrected DWI. Never set it for BOLD that was already corrected. |
| `workflow.stages` | `all` (default); `preprocess` stops before the SBCI stage; `tractography` runs tractography and SC; `fc` runs FC. Earlier stages that are missing always run first. |
| `parameters.*` | Optional settings of the SBCI stage for one run: `n_runs`, `n_seed`, `steps`, `rng`, `bandwidth`, `sh_order`, `rois`. The profile normally sets these; `n_runs` may not disagree with the profile's `N_RUNS`, and the job stops at its start if it does. |

`workflow/contracts/sbci.local.schema.json` lists every setting.

## What it does

`run_subject.sbatch` calls `run_pipeline.py` once per stage. Each stage has a list of
commands it needs and a list of files it must leave; a stage counts as complete only when
those files exist, and a rerun starts at the first stage that is not.

| Stage | What happens | Tools | Leaves (under the subject's work folder) |
|---|---|---|---|
| `dwi_correction` | Route `raw`: Synb0-DISCO makes an undistorted b0 from the T1, topup estimates the field, eddy corrects distortion, eddy currents and motion. Other routes: a copy. (`corrections/dwi.py`) | Synb0-DISCO, FSL topup and eddy | `dwi/eddy_corrected_data.nii.gz`, the rotated b-vectors; on route `raw` also `dwi/field_hz.nii.gz` |
| `fmri_correction` | Unwarps the BOLD: FUGUE with the DWI's field map, or topup on the fieldmap pair, or a copy. (`corrections/fmri.py`) | FSL fugue, flirt, topup, applytopup | `fmri/bold_distcorr.nii.gz` |
| `prepare_data` | Reorients the images and the gradient table. | MRtrix3 `mrconvert`, scilpy | `dwi_pipeline/t1.nii.gz`, `data.nii.gz`, `flip_x.bval`, `flip_x.bvec` |
| `t1_dwi_registration` | Brain extraction, N4 bias correction, denoising, shell selection, the tensor fit, and registration of the T1 to diffusion space. (`preprocessing/preproc_step2_t1_dwi_registration.sh`) | FSL, ANTs, MRtrix3, scilpy | `diffusion/dti/fa.nii.gz`, `md.nii.gz`, `structure/t1_warped.nii.gz`, `diffusion/dwi_fodf.nii.gz` |
| `freesurfer` | `recon-all` on the registered T1; the Destrieux parcellation carried to diffusion space. (`preproc_step3_t1_freesurfer.sh`) | FreeSurfer 6.0.0, ANTs | `structure/t1_freesurfer/` (white, pial, sphere, inflated surfaces; `wmparc`), `structure/aparc.a2009s+aseg.nii.gz` |
| `fodf_tissue` | Tissue maps, the fibre response and the fODF: single-shell CSD for one shell, multi-shell multi-tissue CSD for several. (`preproc_step4_fodf_estimation.sh`) | FSL fast, scilpy | `diffusion/fodf/fodf.nii.gz`, `structure/map_wm.nii.gz`, `map_gm.nii.gz`, `map_csf.nii.gz` |
| `fsfast` | Resting-state cleaning in FSFast: motion correction, slice-timing correction where the profile names an order, 5 mm smoothing, regression of the white-matter and ventricular-CSF signals and the motion regressors, and the residuals on fsaverage and in MNI305 space. (`preproc_step5_fmri.sh`) | FreeSurfer FSFast, MATLAB | `fmri/bold/<run>/fc.surface.lh.nii.gz`, `fc.surface.rh.nii.gz`, `fc.mni.nii.gz` |
| `sbci` | Surface-enhanced tractography (SET) repeated `N_RUNS` times, streamline endpoints snapped to the white surface and carried to the grid, SC smoothed on the sphere by the `concon` kernel, subcortical SC, and FC from the surface time series. (`pipeline/sbci_step2_prepare_set.sh` to `sbci_step6_functional.sh`) | scilpy, dipy, `concon` (`data/concon`) | the connectomes below |

The stage's own Python (scilpy 2.3.0, Python 3.12 for the SBCI stage and 3.11 for
preprocessing) is in the image. An optional atlas-connectome stage (`workflow.run_psc`) is
off by default and is not covered by the validation records.

## Output

```
OUT/
  config/                      sbci_local.json and the profile as they were used; versions.json
                               (the hash of the pipeline's files, and the image)
  logs/                        one .json and .err per stage call; inspect.json for the preflight
  local_adapter_manifest.json  the state of the preprocessing stages
  files/preprocessing_work/<subject>/     the work tree of the stages above
  sbci_output/
    local_adapter_manifest.json           the state of the SBCI stage
    files/work/<subject>/dwi_pipeline/sbci_connectome/     the results
```

The results, in `sbci_connectome/`:

| File | Variables | Content |
|---|---|---|
| `smoothed_sc_avg_0.005_ico4.mat` | `sc` | cortical SC, 5,124 × 5,124, upper triangle; 0.005 is the smoothing bandwidth |
| `sub_sc_avg_0.005_ico4.mat` | `sub_sub_sc`, `sub_surf_sc` | subcortical SC: region × region (19 × 19) and region × surface |
| `fc_avg_ico4.mat` | `fc` | FC, 5,124 × 5,124: the correlation of the cleaned time series |
| `fc_ts.npz` | | the surface time series FC was computed from, all runs joined |
| `mesh_intersections_ico4.mat` | | the streamline endpoints, located on the grid |

To check a finished run against a dataset's record:

```bash
python3 tests/check_validation_record.py OUT datasets/adni/validation.md
```

## Datasets

The datasets differ in settings, not in code: every dataset runs the same stage scripts, and
`datasets/<name>/` holds its profile, the input it expects and the record of its validation.

| Dataset | Folder | Route | Profiles | Validated on |
|---|---|---|---|---|
| ADNI | [`datasets/adni/`](datasets/adni/) | `raw` | `profile.conf` | 20261122 |
| ABCD | [`datasets/abcd/`](datasets/abcd/) | `corrected` | `profile.conf`, `profile.historical.conf` | 20261122 |
| HCP Aging / Development | [`datasets/hcp-lifespan/`](datasets/hcp-lifespan/) | `corrected` | `profile.conf`, `profile.historical.conf` | 20261122 |
| HCP Young Adult | [`datasets/hcp-ya/`](datasets/hcp-ya/) | `corrected_dwi_raw_bold` | `profile.conf` | 20261122 |

A profile is a file of `NAME=value` lines: which b-value shells reach the tensor fit and the
fODF, the fODF model and order, the fMRI timing, and the tractography settings (in the
recommended profiles, 4 SET repetitions of 1,000,000 seeds per hemisphere and a bandwidth of
0.005). [`datasets/README.md`](datasets/README.md) explains every setting, the difference
between the recommended and the historical profiles, and how to add a dataset.

## Reproducibility

Two runs of the same input on the same image agree in every value of the surfaces and SC, and
in FC to about 1e-9 across CPU types. The pipeline pins what would otherwise vary:

- N4 bias correction runs with 16 ITK threads on any machine, and brain extraction with one;
- `antsRegistration` has a fixed seed;
- numpy and OpenBLAS use the same kernels on every CPU type (`workflow/runtime_env.sh`).

One step cannot be pinned: Synb0-DISCO's own registration seeds from the clock, so on the
`raw` route everything after it differs slightly from run to run (eddy-corrected data
correlate at about 0.998). [`datasets/adni/validation.md`](datasets/adni/validation.md) gives
the size of that variation for every result.

Every run records what it ran with: `OUT/config/versions.json` holds one hash over the
pipeline's files and the image's name, beside the configuration and the profile as used.

## The image

The pipeline holds no tool paths. It finds the tools on the image's `PATH` and through these
variables, which the image sets:

| Variable | Content |
|---|---|
| `FSLDIR`, `ANTSPATH`, `FREESURFER_HOME` | FSL, ANTs and FreeSurfer (with `SUBJECTS_DIR`, `FSFAST_HOME`, `MNI_*`, `PERL5LIB`); MRtrix3 and the `synb0` command are on `PATH` |
| `SBCI_ENV` | the preprocessing Python environment (scilpy 2.3.0) |
| `SBCI_CORE_PY` | the Python that runs the SBCI stage |
| `SBCI_TEMPLATE_DIR` | the MNI 152 symmetric 2009c template used for brain extraction |
| `SBCI_PIPELINE` | a copy of this repository at the release the image was built for |
| `SBCI_IMAGE` | the release tag and pipeline commit |

The image carries a copy of the pipeline so that it runs on its own; a clone of this
repository runs its own code with the image's tools (the job scripts pass the clone's folder
as `SBCI_PIPELINE`). `get_image.sbatch` ends by checking that the image sets these variables
and has the tools on `PATH`.

## When something stops

| Message | What to do |
|---|---|
| `the SBCI pipeline runs on Linux (or WSL) …`, or `readlink: illegal option -- m` from an earlier copy | The machine is not Linux (macOS, for example). Run the three commands on a Linux machine, a cluster login node, or in WSL. |
| `apptainer is not on PATH` | Load or install Apptainer (`module load apptainer` on a cluster). |
| `no sbci-full image at …` | Run `bash workflow/get_image.sbatch` once, or set `SBCI_FULL_IMAGE` to a copy. |
| `WARNING: no matlab on PATH` | Put MATLAB on `PATH` before submitting (`module load matlab`); the preflight lists `matlab` as missing for `fsfast`. |
| `cannot find the pipeline folder` | Start the job with `workflow/submit.sh`, not with `sbatch` directly: Slurm runs a copy of the script from its spool folder. |
| `…/sbci_local.json is missing`, `output folder … does not exist` | `IN` needs the configuration file; `OUT` is never created for you. |
| `refusing a path under …` | `IN` or `OUT` is under a folder a site listed in `SBCI_READONLY_ROOTS`; use a run folder elsewhere. |
| `STOPPED: preprocessing call N failed` | The line above it names the issue; `OUT/logs/call_N.err` has the stage's own output. Fix the cause and submit again: the run resumes at that stage. |
| `the image … does not set …` | The `.sif` is not the one `get_image.sbatch` fetches. |

## Layout

| Folder | Content |
|---|---|
| `run_pipeline.py` | the entry point: inspects a subject or runs its next stage |
| `workflow/` | the driver: planning, resume, preflight, the job scripts (`run_subject.sbatch`, `submit.sh`, `get_image.sbatch`, `sbci_tool.sbatch`), the configuration schema |
| `corrections/` | the raw-input routes: Synb0, topup, eddy, FUGUE |
| `preprocessing/` | registration, FreeSurfer, fODF and fMRI stages, driven by the profile |
| `pipeline/` | the SBCI stage: grid, SET tractography, snapping, SC, FC |
| `datasets/` | one folder per dataset: profile, expected input, validation record |
| `data/` | fsaverage reference surfaces, the ico4 grid, the `concon` kernel `c3_main` |
| `tests/` | unit tests, and `check_validation_record.py` |

## Documentation

| Read | For |
|---|---|
| [`datasets/README.md`](datasets/README.md) | every profile setting, the input layout, recommended against historical profiles, adding a dataset |
| `datasets/<name>/README.md` | what that dataset's input must hold and how its acquisition is handled |
| `datasets/<name>/validation.md` | the subject, date, image and numbers of that dataset's validation |
| `workflow/contracts/sbci.local.schema.json` | every setting of `sbci_local.json` |
| The comment at the top of each job script in `workflow/` | its usage, resources and environment variables |
| [SBCI_Images](https://github.com/sbci-brain/SBCI_Images) | how the image is built and what is in it |

## Development

```bash
python3 -m unittest discover -s tests      # about 150 tests, under a minute
```

The tests need no image and no subject data: they run the planner, the stage scripts and the
job scripts against fixtures and stand-in tools. Tests that need VTK, nibabel or POSIX
permissions skip where those are absent.

## Optional: the `sbci` analysis package

The `.mat` files above are the pipeline's results. If the separate `sbci` analysis package is
installed in the image's Python (or its source folder is named in `SBCI_ANALYSIS_PACKAGE`), a
subject's job also writes that package's `.h5` files to `OUT/derivatives` and a QC page to
`OUT/qc`, and `workflow/sbci_tool.sbatch` runs its commands as short jobs. Without it both
are skipped.

## References

- **SBCI** — Cole, M., Murray, K., St-Onge, E., Risk, B., Zhong, J., Schifitto, G.,
  Descoteaux, M., & Zhang, Z. (2021). Surface-Based Connectivity Integration: An atlas-free
  approach to jointly study functional and structural connectivity. *Human Brain Mapping*,
  42(11), 3481–3499. <https://doi.org/10.1002/hbm.25447>
- **The original pipeline** — <https://github.com/sbci-brain/SBCI_Pipeline>

The tools the image holds are their authors': FSL, FreeSurfer, ANTs, MRtrix3, Synb0-DISCO,
scilpy and dipy. Cite them as their own documentation asks.

## License

The code: MIT, see [LICENSE](LICENSE). The tools in the image come under their own licences;
FSL is free for non-commercial use only, and FreeSurfer needs your own licence file.
