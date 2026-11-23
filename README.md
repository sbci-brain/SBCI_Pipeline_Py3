# SBCI: surface-based connectivity integration (Python 3)

SBCI builds structural connectivity (SC) and functional connectivity (FC) on the white-matter
surface, without an atlas. This repository runs it end to end for one subject: raw BIDS (or
data already corrected by HCP- or ABCD-style pipelines) → distortion correction → FreeSurfer →
diffusion and fODF → FSFast resting-state cleaning → SC and FC on the ico4 grid (5124 × 5124
cortical vertices plus 19 subcortical regions).

Reference: Cole M, Murray K, St-Onge E, Risk B, Zhong J, Schifitto G, Descoteaux M, Zhang Z.
Surface-Based Connectivity Integration: an atlas-free approach to jointly study functional and
structural connectivity. *Human Brain Mapping* 42(11):3481–3499, 2021.

## One subject in three commands

On Linux (or WSL) with Apptainer and MATLAB on `PATH` (on a cluster, load both before
submitting, e.g. `module load apptainer matlab`):

```bash
git clone https://github.com/sbci-brain/SBCI_Pipeline_Py3.git && cd SBCI_Pipeline_Py3
bash workflow/get_image.sbatch                                   # once: fetches the image
bash workflow/submit.sh --output=OUT/slurm-%j.out workflow/run_subject.sbatch IN OUT
```

`IN` holds one subject folder and an `sbci_local.json`; `OUT` must exist. Before the long run,
the same script gives a preflight in seconds:

```bash
bash workflow/run_subject.sbatch --inspect IN OUT
```

It prints whether the run is ready, each stage's missing commands, and anything that blocks it.
The run itself takes 6–18 h on 16 CPUs, through Slurm where there is one and otherwise in the
background. A rerun on the same `OUT` resumes after the last completed stage. Results land in
`OUT/sbci_output/files/work/<subject>/dwi_pipeline/sbci_connectome/`:

| File | Content |
|---|---|
| `smoothed_sc_avg_0.005_ico4.mat` | cortical SC, 5124 × 5124 |
| `sub_sc_avg_0.005_ico4.mat` | subcortical SC: region × region and region × surface |
| `fc_avg_ico4.mat` | FC, 5124 × 5124 |

### `sbci_local.json`

Copy `datasets/sbci_local.example.json` and set two things:

- `workflow.preprocessing_config`: the dataset profile, by its place in this repository
  (`datasets/adni/profile.conf`) or as a path to your own copy;
- `workflow.freesurfer_license`: the path of your FreeSurfer `license.txt` (free from
  FreeSurfer's site; the image has none).

For data corrected upstream also set `workflow.distortion_corrected_input` (and
`workflow.bold_topup_fieldmaps` where the BOLD is raw), as the dataset's `README.md` says.
`workflow/contracts/sbci.local.schema.json` lists every setting.

## Datasets

The datasets differ in settings, not in code: every dataset runs the same stage scripts, and
`datasets/<name>/` holds its profile, the input it expects and the record of its validation.
See [`datasets/README.md`](datasets/README.md).

| Route | Input | The pipeline does |
|---|---|---|
| `raw` | scanner output in BIDS, DWI in one phase-encoding direction | Synb0-DISCO + topup + eddy on the DWI; FUGUE on the BOLD |
| `corrected` | DWI and BOLD corrected upstream | nothing; it starts at preprocessing |
| `corrected_dwi_raw_bold` | DWI corrected upstream, raw BOLD with a spin-echo fieldmap pair | topup on the BOLD |

## What it needs

- **Linux (or WSL) with Apptainer.**
- **The image `sbci-full`**, which holds every tool: FSL 6.0.7, ANTs 2.6.0, MRtrix3 3.0.8,
  FreeSurfer 6.0.0, Synb0-DISCO v3.1 and two Python environments. `workflow/get_image.sbatch`
  fetches it once per clone, pinned by digest, to `sbci-full.sif` in this folder (about 11 GB
  to download, then 2–3 hours packing on 4 cores; needs internet, so on a cluster run it
  where there is some). Set `SBCI_FULL_IMAGE` to use a copy elsewhere.
- **MATLAB** (R2018b validated, a Linux install) on `PATH`, for the FSFast stage
  (`selxavg3-sess`); the job binds it into the image. It is not optional: the SBCI stage runs
  only after every preprocessing stage, so without MATLAB no subject finishes, SC included.
  FreeSurfer 6's compiled `selxavg3` does not replace it (it needs MATLAB Runtime R2014a;
  FreeSurfer 6 ships R2012b).
- **A FreeSurfer licence file**, named in `sbci_local.json`.

### The image

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
as `SBCI_PIPELINE`). The image recipe is in the `SBCI_Images` repository.

## Reproducibility

Two runs of the same input on the same image agree in every value of the surfaces and SC, and
in FC to about 1e-9 across CPU types. The pipeline pins what would otherwise vary:

- N4 bias correction runs with 16 ITK threads on any machine, and brain extraction with one;
- `antsRegistration` has a fixed seed;
- numpy and OpenBLAS use the same kernels on every CPU type (`workflow/runtime_env.sh`).

One step cannot be pinned: Synb0-DISCO's own registration seeds from the clock, so on the
`raw` route everything after it differs slightly from run to run (eddy-corrected data
correlate at about 0.998).

## Layout

| Folder | Content |
|---|---|
| `run_pipeline.py` | the entry point: inspects a subject or runs its next stage |
| `pipeline/` | the SBCI stages: grid, SET tractography, snapping, SC, FC |
| `preprocessing/` | registration, FreeSurfer, fODF and fMRI stages, driven by the profile |
| `corrections/` | the raw-input routes: Synb0, topup, eddy, FUGUE |
| `workflow/` | the driver: planning, resume, preflight, Slurm or background submission |
| `datasets/` | one folder per dataset: profile, expected input, validation record |
| `data/` | fsaverage reference surfaces, the ico4 grid, the `concon` kernel `c3_main` |
| `tests/` | unit tests |

```bash
python3 -m unittest discover -s tests
```

Tests that need VTK, nibabel or POSIX permissions skip elsewhere.

## Optional: the `sbci` analysis package

The `.mat` files above are the pipeline's results. If the separate `sbci` analysis package is
installed in the image's Python (or its source folder is named in `SBCI_ANALYSIS_PACKAGE`), a
subject's job also writes that package's `.h5` files and QC page, and
`workflow/sbci_tool.sbatch` runs its commands as short jobs. Without it both are skipped.
