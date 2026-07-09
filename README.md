# SBCI

Surface-Based Connectivity Integration

This is an advanced MRI post-processing pipeline that builds structural
connectivity (SC) and functional connectivity (FC) on the white matter surface
of the brain and returns three imaging biomarkers representative of the
relationships between SC and FC.

This repository contains the Python 3 version of the SBCI scripts. It follows
the same general protocol as the current SBCI pipeline, with the Python
environment and script names updated for Python 3. The old `-py3` filename
suffix has been removed because this is a standalone Python 3 repository.

What you will need:

1. T1w anatomical data
2. Multi-shell dwi data
3. Resting-state fMRI data

The data are assumed to be minimaly preprocessed - i.e., using FSL's pipeline
to perforrm eddy correction and susceptibility-induced distortion correction.

**Reference**:

Martin Cole, Kyle Murray, Etienne St-Onge, Benjamin Risk, Jianhui Zhong,
Giovanni Schifitto, Maxime Descoteaux, and Zhengwu Zhang.
"Surface-Based Connectivity Integration: An atlas-free approach to jointly
study functional and structural connectivity." Human Brain Mapping 42, no. 11
(2021): 3481-3499.

## Installing Prerequisites

If using a managed system such as Bluehive (UofR), Longleaf (UNC), or Sherlock
(Stanford) with Slurm and module management included, prerequisites should be
able to be installed using commands similar to these:

```bash
module purge
module load qt gcc mrtrix3 freesurfer ants fsl
module rm python
module load java matlab dcm2niix pigz anaconda
module load git
```

Otherwise, steps for installing some of the prerequisite software are included
later in this document.

It's recommended that you create a `.bashrc_sbci` file in your home folder and
add these lines (along with others mentioned below) to a `.bashrc_sbci` file so
that you can source it everytime the pipeline is sent to a cluster. See the
section **Using a Bashrc File** for more information.

### Setting up the Python Environment

If you have Anaconda installed (either through a module or manually, see
below), the following commands set up a clean Python 3 environment with the
neccesary packages installed. Python 3.11 is recommended for the tested package
set.

```bash
conda create -n sbci python=3.11 pip -y
conda activate sbci

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

Append the following line to `.bashrc_sbci` (or the script file that's executed
when a user logs in), so that the correct Python environment is running each
time the pipeline is used.

```bash
conda activate sbci
```

### Installing the SBCI Pipeline

Clone this Python 3 SBCI pipeline repository and install the Python
requirements:

```bash
git clone https://github.com/<OWNER>/<REPO>.git
cd <REPO>
conda activate sbci
python -m pip install -r requirements.txt
```

The Python 3 scripts are located directly in:

```bash
scripts/
```

The ADNI example wrapper scripts are located in:

```bash
scripts/ADNI_example/
```

The `sbci_config` file needs to be updated according to the local computing
environment. For the ADNI example, it is located at:

```bash
scripts/ADNI_example/sbci_config
```

This repository keeps the original SBCI workflow style. The main Python 3 batch
submission command is:

```bash
bash process_sbci.sh SUBJECT_LIST SUBJECTS_DIR scripts/ADNI_example
```

where `SUBJECT_LIST` is a text file with one subject ID per line,
`SUBJECTS_DIR` is the directory containing the subject folders, and
`scripts/ADNI_example` is the directory containing the six SBCI step scripts and
`sbci_config`.

The submitted ADNI SBCI steps are:

1. `sbci_step1_process_grid.sh`
2. `sbci_step2_prepare_set.sh`
3. `sbci_step3_run_set.sh`
4. `sbci_step4_process_surfaces.sh`
5. `sbci_step5_structural.sh`
6. `sbci_step6_functional.sh`

Python 3 notes:

- The old `-py3` script suffix has been removed.
- The wrapper scripts use `python3`.
- `SBCI_SCRIPTS` points to this repository's `scripts/` directory.
- scilpy command names follow the scilpy 2.x command line tools, including
  `scil_surface_flip`, `scil_surface_smooth`, `scil_tracking_pft_maps`, and
  `scil_tractogram_convert`.

SBCI should now be installed. Check scripts in `scripts/ADNI_example` for an
example of how to use the SBCI pipeline on ADNI-style data.

### Installing the PSC Pipeline

Clone the PSC pipeline:

```bash
git clone https://github.com/zhengwu/PSC_Pipeline.git
```

See the README.md file on the github website for PSC. The PSC pipeline remains
an external dependency, as in the original SBCI workflow. Update `PSC_PATH` in
`sbci_config` and add the PSC scripts to your path in `.bashrc_sbci`.

### Alternative Installation of Prerequisites

If using a system without module management, prerequisites can be installed
manually:

- **Anaconda**: Follow the insctructions
  [here](https://docs.anaconda.com/anaconda/install/linux/). Test the
  installatioin by running the command `which python`. If there are issues, try
  adding the anaconda path to `.bashrc_sbci` (or the script file that's executed
  when a user logs in), like so.

  ```bash
  export PATH="/PATH/TO/ANACONDA/bin:$PATH"
  ```

  Replacing `/PATH/TO/ANACONDA/` with the location of the anaconda
  installation.

- **Freesurfer**: Follow the instructions
  [here](https://surfer.nmr.mgh.harvard.edu/fswiki/rel6downloads); if using
  Ubuntu, libpng12 might be an issue.

- **mrtrix**: `conda install -c mrtrix3 mrtrix3`

- **ANTs**: Download and unzip a copy of ANTs
  [here](http://stnava.github.io/ANTs/) and place in a suitable folder
  (`/home/yourname/Software/ANTs`, for example). After the installation is
  done, append the following lines to `.bashrc_sbci` (or the script file that's
  executed when a user logs in).

  ```bash
  export ANTSPATH=/home/yourname/Software/ANTs/install/bin
  export PATH=${ANTSPATH}:$PATH
  export PATH="$HOME/Software/ANTs/Scripts:$PATH"
  ```

  Run the following commands to validate the installation:

  ```bash
  which antsRegistration
  antsRegistrationSyN.sh
  ```

- **FSL**: Follow the instructions
  [here](https://fsl.fmrib.ox.ac.uk/fsl/fslwiki/FslInstallation/).

## Using a Bashrc File

So that the working environment is the same each time the pipeline is run, it
is recommended to create a `.bashrc_sbci` to be sourced each time the pipeline
is run. The following is an example of a .bashrc file that works for a managed
cluster environment. Just change `/PATH/TO/PSC_PIPELINE/` and
`/PATH/TO/SBCI_PY3/` to the location of the PSC installation and this Python 3
SBCI repository.

```bash
# .bashrc_sbci

module purge
module load qt gcc mrtrix3 freesurfer ants fsl
module load java matlab dcm2niix pigz anaconda
module load git
module unload python

# Needed to make conda play nice on managed clusters.
source /PATH/TO/ANACONDA/etc/profile.d/conda.sh

# Direct to Freesurfer installation.
export PATH="/PATH/TO/FREESURFER/fsfast/bin:$PATH"
export PATH="/PATH/TO/FREESURFER/fsfast/toolbox:$PATH"
source /PATH/TO/FREESURFER/SetUpFreeSurfer.sh

# Direct to the PSC installation.
export PATH="/PATH/TO/PSC_PIPELINE/scripts:$PATH"
export PYTHONPATH="/PATH/TO/PSC_PIPELINE:$PYTHONPATH"

# Direct to the ANTs installation.
export ANTSPATH="/PATH/TO/ANTS/bin/"
export PATH="${ANTSPATH}:$PATH"

# Direct to this Python 3 SBCI repository.
export SBCI_ROOT="/PATH/TO/SBCI_PY3"
export SBCI_SCRIPTS="${SBCI_ROOT}/scripts"
export PYTHONPATH="${SBCI_SCRIPTS}:${PYTHONPATH:-}"

# Load the Python environment.
conda activate sbci
```

For more Python 3 environment details, see `env_setup/README.md`.
