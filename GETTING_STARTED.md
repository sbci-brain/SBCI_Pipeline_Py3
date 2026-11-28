# Getting started: your first subject, step by step

This guide takes you from nothing installed to one subject running through the SBCI
pipeline. It assumes you have never used containers, Slurm or this pipeline. Each step gives
the command to type, what you should see, and what to do when you see something else.

## Choose your route

| | **Route A: your own Linux workstation** | **Route B: a computing centre (cluster)** |
|---|---|---|
| For | a Linux workstation or server you control; a Windows PC through WSL 2 | a university or institute cluster that you reach with `ssh` and that runs jobs through Slurm (`sbatch`) |
| Container program | Docker | Apptainer (often called Singularity), already installed by the centre |
| MATLAB | you install a Linux MATLAB | the centre's MATLAB module |
| You need | 16 cores, 64 GB of memory, 80 GB of free disk | an account, and scratch space of 60 GB plus 30 GB per subject |
| The job | runs in the background on your machine | waits in the queue, then runs on a compute node |
| Best for | one or a few subjects | many subjects |

Both routes use the same pipeline, the same image and the same commands. Setting up differs
(Part A or Part B); from the first check onwards the steps are shared (Part C).

- [Part A: set up a Linux workstation](#part-a-set-up-a-linux-workstation) (steps A1 to A5)
- [Part B: set up on a computing centre](#part-b-set-up-on-a-computing-centre) (steps B1 to B5)
- [Part C: run a subject](#part-c-run-a-subject) (steps C1 to C8, both routes)

**macOS cannot finish a subject.** One stage needs a Linux MATLAB, which a Mac does not
have. Use a Linux or Windows machine, or a cluster.

### What has been run, and what has not

| | Route A, Docker | Route B, Apptainer and Slurm |
|---|---|---|
| Fetching the image (A5, B5) | run, on WSL 2 | run |
| The self-test and the practice preflight (C1) | run, on WSL 2 | run |
| A whole subject (C6) | not finished yet | run, for four datasets |

Route B is the one the validation records come from. On route A the image, the self-test
and the preflight have been run with Docker exactly as written; a whole subject through
Docker starts the same way but has not finished yet, and this table will change when one
has.

---

## Part A: set up a Linux workstation

### A1. Check that the machine can do it

| You need | How much | Why |
|---|---|---|
| Processor | x86-64 (Intel or AMD), 16 cores | The validation runs used 16. Fewer cores work more slowly, and results can differ in the last digits. |
| Memory | 64 GB | Less can stop the run part-way. |
| Free disk | about 80 GB for the first subject | The image is 11 GB to download and 40 GB unpacked; one subject writes about 30 GB. |
| Time | 6 to 18 hours for one subject | The machine must stay on and awake. |
| Internet | for the set-up | The run itself needs none. |

A machine with 16 GB of memory can do everything up to step C1, to see how it works, but
cannot be expected to finish a subject.

### A2. Install Docker

**On Linux.** Install Docker Engine for your distribution
(<https://docs.docker.com/engine/install/>), then let your user run it without `sudo`:

```bash
sudo usermod -aG docker $USER      # then log out and in again
```

If the machine has Apptainer instead, skip Docker: the pipeline uses Apptainer when it finds
it.

**On Windows.** The pipeline runs in WSL 2, a Linux system inside Windows.

1. Open PowerShell as administrator (right-click the Start button, then "Terminal (Admin)")
   and type:

   ```powershell
   wsl --install
   ```

   Restart when it asks. After the restart a window asks you to choose a Linux user name and
   password. This is your Ubuntu system; open it later from the Start menu ("Ubuntu").

2. Give WSL enough memory and cores; by default it takes only half of the computer's memory.
   In Notepad, create a file named `.wslconfig` in your Windows user folder (type
   `%UserProfile%` in File Explorer's address bar to open it) with:

   ```ini
   [wsl2]
   memory=64GB
   processors=16
   ```

   Use numbers your computer has. Then type `wsl --shutdown` in PowerShell and open Ubuntu
   again.

3. Install Docker Desktop from <https://www.docker.com/products/docker-desktop/>, keeping
   "Use WSL 2" ticked. Start it, open *Settings → Resources → WSL integration*, and switch on
   your Ubuntu.

From here on, on Windows, **type every command in the Ubuntu window**, not in PowerShell,
and keep your work in Ubuntu's own folders (under `~`). Folders under `/mnt/c/` are your
Windows disk; they work, but much more slowly.

**Check**, on either system:

```bash
docker run --rm hello-world
```

You should see `Hello from Docker!`. `permission denied` on Linux: log out and in again.
`command not found` on Windows: Docker Desktop is not running, or its WSL integration is
off. Install `git` too if it is missing: `sudo apt update && sudo apt install -y git`.

### A3. Install MATLAB for Linux

The resting-state stage (FSFast) runs MATLAB code, and the image holds no MATLAB. It uses
yours, which must be a **Linux** install (on Windows: installed *inside Ubuntu*; the MATLAB
you have in Windows cannot be used) and must be on `PATH`. Install it from MathWorks with
your licence. R2018b is the version the pipeline was validated with.

```bash
which matlab
```

This must print a path, such as `/usr/local/MATLAB/R2018b/bin/matlab`. If it prints nothing,
add MATLAB's `bin` folder to `PATH` (use your own install folder):

```bash
echo 'export PATH=/usr/local/MATLAB/R2018b/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

- MATLAB is not optional. Without it no subject finishes, the structural connectome
  included, because the connectivity stage starts only after every earlier stage.
- MATLAB runs inside the container, as your user. With a network licence, set
  `MLM_LICENSE_FILE` (for example `export MLM_LICENSE_FILE=27000@your.licence.server`)
  before the run; the job passes it on. Whether a licence tied to one computer is accepted
  inside the container has not been tested.

### A4. Get a FreeSurfer licence and the pipeline

FreeSurfer is free but wants a licence file from each user. Register at
<https://surfer.nmr.mgh.harvard.edu/registration.html>; a file `license.txt` arrives by
e-mail. Save it as `~/freesurfer/license.txt`. You do not install FreeSurfer; it is in the
image. (On Windows, copy it from Downloads with
`mkdir -p ~/freesurfer && cp /mnt/c/Users/<your Windows name>/Downloads/license.txt ~/freesurfer/`.)

Then get the pipeline:

```bash
cd ~
git clone https://github.com/sbci-brain/SBCI_Pipeline_Py3.git
cd SBCI_Pipeline_Py3
```

Every later command is typed from this folder.

### A5. Get the image

The image is one large package with every tool the pipeline runs: FSL, ANTs, MRtrix3,
FreeSurfer, Synb0-DISCO and Python. You fetch it once.

```bash
bash workflow/get_image.sbatch
```

With Docker this downloads 11 GB and unpacks it to about 40 GB, then checks that the tools
are there. The last line should be:

```
sbci-full ready in Docker: sdszhauk/sbci-image@sha256:d5b6f2c6…
```

`no space left on device`: free disk space and run it again. On Windows, Docker's disk is
on the `C:` drive unless you moved it in Docker Desktop's settings.

Now go to [Part C](#part-c-run-a-subject).

---

## Part B: set up on a computing centre

### B1. What to find out first

Every centre is set up a little differently. Find these four things in your centre's
documentation, or ask its help desk:

| Question | Used in | Example |
|---|---|---|
| How do I load Apptainer (or Singularity)? | B2 | `module load apptainer` |
| How do I load MATLAB, and which versions are there? | B2 | `module load matlab/2018b` |
| Where is my large, fast storage ("scratch" or "work")? | B3 | `/work/<you>` or `/scratch/<you>` |
| Do I need to name a partition or an account when I submit a job? | C6 | `--partition=general` |

Home folders on clusters are small. The image alone is 11 GB and each subject writes about
30 GB, so everything in this guide goes in your scratch storage.

### B2. Log in and load the two programs

```bash
ssh <you>@<your.cluster.address>
module load apptainer matlab        # your centre's names, from B1
which apptainer matlab
```

The last command must print two paths. **Load the modules again each time you log in**,
before any pipeline command: the jobs take both programs from the session that submits
them.

R2018b is the MATLAB version the pipeline was validated with. MATLAB is not optional:
without it no subject finishes, the structural connectome included.

### B3. Get a FreeSurfer licence and the pipeline

Register at <https://surfer.nmr.mgh.harvard.edu/registration.html>; a file `license.txt`
arrives by e-mail. Copy it to the cluster from your own computer (this command is typed on
your computer, not on the cluster):

```bash
scp license.txt <you>@<your.cluster.address>:<your scratch>/freesurfer-license.txt
```

Do not rely on a FreeSurfer the centre installed: the pipeline uses the one in the image,
and needs only your licence file.

Then, on the cluster:

```bash
cd <your scratch>
git clone https://github.com/sbci-brain/SBCI_Pipeline_Py3.git
cd SBCI_Pipeline_Py3
```

Every later command is typed from this folder.

### B4. Know where commands run

A cluster has two kinds of machine. You type on a **login node**, which is shared by
everyone and is for light work only. Heavy work is sent as a **job** to a **compute node**
with `sbatch`; Slurm puts it in a queue and runs it when a node is free.

The pipeline hides most of this. `workflow/submit.sh` sends a job, and each job script
already asks Slurm for what it needs (a subject: 16 CPUs, 64 GB, 48 hours). Three Slurm
commands are worth knowing:

```bash
squeue -u $USER          # your jobs: PD = waiting, R = running
scancel <job number>     # stop a job
sacct -j <job number>    # what became of a finished job
```

### B5. Get the image

With Apptainer the image becomes one file, `sbci-full.sif` (about 11 GB), in the pipeline
folder. Building it needs internet, 2–3 hours on 4 cores, and about 35 GB of room beside the
file while it builds.

Where compute nodes can reach the internet, send it as a job:

```bash
bash workflow/submit.sh --output=get_image-%j.out workflow/get_image.sbatch
```

Where they cannot, run it on the login node, in a session that survives a dropped
connection (`tmux` or `screen`, if your centre allows work of this size there):

```bash
bash workflow/get_image.sbatch
```

Either way the output ends with:

```
sbci-full ready: …/SBCI_Pipeline_Py3/sbci-full.sif
```

A colleague who already has `sbci-full.sif` of the same release can spare you the build:
`export SBCI_FULL_IMAGE=/path/to/their/sbci-full.sif` before each pipeline command.

---

## Part C: run a subject

From here on the commands are the same on both routes. Where something differs, the two are
shown side by side. On a cluster, remember `module load apptainer matlab` first.

The guide uses a work folder called `$WORK`. Set it once per session:

```bash
export WORK=~/sbci-work                 # route A
export WORK=<your scratch>/sbci-work    # route B
mkdir -p $WORK
```

### C1. Check everything, without any data

Two short checks show that the image and the pipeline work together. They need no subject,
no FreeSurfer licence and no MATLAB.

**The self-test** runs the pipeline's own tools on small made-up files and checks the data
files that came with the clone, in under a minute:

```bash
mkdir -p $WORK/selftest/in $WORK/selftest/out
cp workflow/sbci_self_test.example.json $WORK/selftest/in/sbci_local.json

# route A
bash workflow/run_subject.sbatch $WORK/selftest/in $WORK/selftest/out

# route B: as a small job; then read its log when squeue no longer lists it
bash workflow/submit.sh --output=$WORK/selftest/out/slurm-%j.out --time=00:10:00 --cpus-per-task=2 --mem=8g \
    workflow/run_subject.sbatch $WORK/selftest/in $WORK/selftest/out
cat $WORK/selftest/out/slurm-*.out
```

You should see one `ok` per check and then `self-test passed`:

```
running in sdszhauk/sbci-image@sha256:d5b6f2c6… (docker)
  conconKernel                 ok
  mapping_avg_ico4.npz         ok
  …
  stageEntrypoints             ok
self-test passed (status completed_local_runtime_self_test)
```

(On route B the first line names your `sbci-full.sif` and `(apptainer)`.) It computes
nothing scientific. If a line reads `FAILED`, the lines above it name what is missing; most
often the clone is incomplete (clone it again) or the image is not the one this release
fetches.

**A practice preflight.** The preflight is a check that takes seconds: it reads a subject
folder and prints which stages are done, which are still to run, and anything that would
stop the run. Always run it before the long run. You can try it on a made-up subject: a
tiny folder of made-up images, no brain at all, in the shape the pipeline leaves after
preprocessing. Make it with the image's Python:

```bash
mkdir -p $WORK/practice/out

# route A
source workflow/image_pin.sh
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$HOME:$HOME" "$REPOSITORY@$DIGEST" \
    bash -c "\$SBCI_ENV/bin/python3 $PWD/tests/create_subject_fixture.py --output $WORK/practice/in --pipeline-root $PWD"

# route B
apptainer exec --bind $WORK sbci-full.sif \
    bash -c "\$SBCI_ENV/bin/python3 $PWD/tests/create_subject_fixture.py --output $WORK/practice/in --pipeline-root $PWD"
```

Then the preflight, on either route:

```bash
bash workflow/run_subject.sbatch --inspect $WORK/practice/in $WORK/practice/out
```

```
running in … (docker) with MATLAB from /usr/local/MATLAB/R2018b
status completed_local_surrogate readyToRun True
  dwi_correction         complete
  fmri_correction        complete
  prepare_data           complete
  t1_dwi_registration    complete
  freesurfer             complete
  fodf_tissue            complete
  fsfast                 complete
  sbci                   pending
```

One line per stage, in the order they run. The made-up subject already has the files of the
first seven stages, so they read `complete`, and the last is `pending`. `readyToRun True`
means nothing blocks a run. If the first line is `WARNING: no matlab on PATH`, go back to
A3 or B2.

Do not start a real run on the practice subject. Delete both folders when you like:
`rm -r $WORK/practice $WORK/selftest`.

### C2. Put your subject in place

One input folder and one output folder per subject:

```bash
mkdir -p $WORK/sub-01/in $WORK/sub-01/out
```

Copy the subject into the input folder, in BIDS layout (the standard way of naming MRI
files):

```
$WORK/sub-01/in/
  sbci_local.json                     (step C3)
  sub-01/
    anat/  sub-01_T1w.nii.gz
    dwi/   sub-01_dwi.nii.gz  sub-01_dwi.bval  sub-01_dwi.bvec  sub-01_dwi.json
    func/  sub-01_task-rest_bold.nii.gz  sub-01_task-rest_bold.json
    fmap/  (only for some datasets)
```

One T1-weighted image, one diffusion scan with its b-values, b-vectors and JSON, and one
resting-state scan with its JSON. The JSON files come from the converter that made the
NIfTI files (for example `dcm2niix`); the pipeline reads the timing and the phase-encoding
direction from them and has no defaults for either.

There is no public example subject to download yet: you need a subject of your own, or one
from a dataset you have access to.

### C3. Choose the profile and write `sbci_local.json`

A **profile** is a small settings file for one kind of acquisition. Pick the row of your
data; its page says exactly which files and JSON fields it needs.

| Your data | Page | Profile | Status |
|---|---|---|---|
| ADNI | [`datasets/adni/`](datasets/adni/README.md) | `datasets/adni/profile.conf` | validated |
| ABCD | [`datasets/abcd/`](datasets/abcd/README.md) | `datasets/abcd/profile.conf` | validated |
| HCP Aging or Development | [`datasets/hcp-lifespan/`](datasets/hcp-lifespan/README.md) | `datasets/hcp-lifespan/profile.conf` | validated |
| HCP Young Adult | [`datasets/hcp-ya/`](datasets/hcp-ya/README.md) | `datasets/hcp-ya/profile.conf` | validated |
| Anything else | [`datasets/generic/`](datasets/generic/README.md) | `datasets/generic/profile.conf` | untested |

"Validated" means a subject of that dataset has run end to end and its numbers are on
record. The pipeline cannot tell which dataset your files are from: it uses the profile you
name. With the generic profile, or one you changed, the run still works but tells you, in
the preflight and in the log, that nobody has validated it for your data.

Now the file that names the profile and your licence. Start from the example:

```bash
cp datasets/sbci_local.example.json $WORK/sub-01/in/sbci_local.json
nano $WORK/sub-01/in/sbci_local.json        # Ctrl+O then Enter saves, Ctrl+X leaves
```

| Line | Put |
|---|---|
| `"preprocessing_config"` | the profile from the table above |
| `"freesurfer_license"` | the full path of your `license.txt` (`realpath <file>` prints it). A path starting with `~` does not work here. |
| `"distortion_corrected_input"`, `"bold_topup_fieldmaps"` | the two values your dataset's page gives. They say whether your images were already corrected for distortion. For ADNI, both `false`. |

Leave the other lines as they are.

### C4. Check your diffusion scan against the settings

Skip this for the four validated datasets. For anything else, two diffusion settings decide
whether the run can finish, and one of them is only checked hours into the run. Look at
your b-values:

```bash
tr -s ' \t' '\n' < $WORK/sub-01/in/sub-01/dwi/sub-01_dwi.bval | sort -n | uniq -c
```

This prints how many volumes there are at each b-value, for example `7 at 0`, `60 at 1000`.

**Shells.** A *shell* is a group of volumes with (nearly) the same b-value. Data with one
shell besides b = 0 is *single-shell*; with several, *multi-shell*. The generic profile
uses every shell for the fibre model (the fODF) and the shell nearest b = 1000 for the
tensor fit. It warns you when that shell is outside b = 700–1500, where the tensor model
fits poorly; the run continues.

**SH order.** The fibre model is described with spherical harmonics of an *order*: 6 by
default for single-shell data, 8 for multi-shell. A higher order needs more gradient
directions:

| SH order | Distinct directions needed (among the volumes with b above 0) |
|---|---|
| 4 | 15 |
| 6 | 28 |
| 8 | 45 |

If your scan has fewer directions than its default order needs (a 32-direction single-shell
scan is fine for order 6; a 30-direction two-shell scan is too few for order 8), the run
stops at the fODF stage, **after FreeSurfer, hours in**, with a message naming the highest
order your data support. The preflight does not check this. So count now, and if the
directions are too few, lower the order in your own copy of the profile:

```bash
cp datasets/generic/profile.conf $WORK/sub-01/in/my_profile.conf
echo 'SH_ORDER=6' >> $WORK/sub-01/in/my_profile.conf
```

and name that copy (its full path) as `"preprocessing_config"` in `sbci_local.json`. A
changed profile is recorded as `modified`. The validated profiles set the order themselves
(6 for ADNI, 8 for ABCD and the HCP datasets); leave those as they are. Every setting, and how the shells are chosen,
is explained in [`datasets/README.md`](datasets/README.md).

### C5. Preflight your subject

```bash
bash workflow/run_subject.sbatch --inspect $WORK/sub-01/in $WORK/sub-01/out
```

On a cluster this is light enough for the login node. For a new subject every stage should
read `pending`, with no `MISSING:` after it, and the second line should end with
`readyToRun True`. Otherwise:

| You see | Meaning |
|---|---|
| `MISSING: matlab` after `fsfast` | MATLAB is not on `PATH` (A3), or its module is not loaded (B2). |
| a line starting with `blocker:` or `issue:` | It names what is wrong: a file the subject lacks, a JSON field, a profile that does not fit the two correction settings or the scan's TR. Fix it and run the preflight again. |
| a line starting with `warning:` | Something to know that does not stop the run: a profile not validated for this data, shells other than the profile expects, a tensor shell outside b = 700–1500. |
| `…/sbci_local.json is missing` | The file is not in the input folder, or the folder's path is mistyped. |

More messages are explained in the README, under
[When something stops](README.md#when-something-stops).

### C6. Run

The command is the same on both routes:

```bash
bash workflow/submit.sh --output=$WORK/sub-01/out/slurm-%j.out workflow/run_subject.sbatch $WORK/sub-01/in $WORK/sub-01/out
```

**Route A** starts the job in the background on your machine and prints one line:

```
Started job local-20261123101500-4242 (process 4243) on mypc, without Slurm; log: …/sub-01/out/slurm-local-20261123101500-4242.out
```

- Keep the machine awake: switch off sleep and hibernation, and keep a laptop plugged in.
- On Windows, keep Docker Desktop running and leave an Ubuntu window open. Windows shuts
  WSL down, and the job with it, when nothing is using it.

**Route B** hands the job to Slurm and prints `Submitted batch job <number>`. The job asks
for 16 CPUs, 64 GB and 48 hours. You can log out; the job goes on.

- Options your centre needs go before the script name, as with `sbatch`:
  `bash workflow/submit.sh --partition=general --output=… workflow/run_subject.sbatch …`
- Always start jobs with `workflow/submit.sh`, not with `sbatch` directly.
- `squeue -u $USER` shows the job waiting (`PD`) or running (`R`).

**On both**, follow the log:

```bash
tail -f $WORK/sub-01/out/slurm-*.out        # Ctrl+C stops watching; the job goes on
```

A line `=== preprocessing call N … ===` appears as each stage starts, and a line naming the
stage and its state when it ends. FreeSurfer alone takes several hours, so a long silence
is normal. The run is finished when the log ends with:

```
finished=… rc=0
```

`rc=0` means success. Any other number, or a line starting with `STOPPED:`, means a stage
failed: the line above it says which, and `$WORK/sub-01/out/logs/` holds that stage's own
output.

**If the job stops** for any reason (a power cut, a full disk, the cluster's time limit, an
error), fix the cause and type the same `submit.sh` command again. It resumes after the
last completed stage.

### C7. Find the results

```bash
ls $WORK/sub-01/out/sbci_output/files/work/*/dwi_pipeline/sbci_connectome/
```

| File | Content |
|---|---|
| `smoothed_sc_avg_0.005_ico4.mat` | the structural connectome on the cortical surface, 5,124 × 5,124 |
| `fc_avg_ico4.mat` | the functional connectome, 5,124 × 5,124 |
| `sub_sc_avg_0.005_ico4.mat` | the connectivity of 19 subcortical regions |

The README's [Output](README.md#output) section describes every file and folder.
`$WORK/sub-01/out/config/` records what the run used: your settings, the profile and
whether it was validated (`profile_status` in `versions.json`), the image and the version
of the pipeline.

### C8. More subjects

Each subject gets its own input and output folder and its own job. With the subjects laid
out as `$WORK/<subject>/in` and `$WORK/<subject>/out`, each with its `sbci_local.json`:

```bash
for s in sub-01 sub-02 sub-03; do
    bash workflow/submit.sh --output=$WORK/$s/out/slurm-%j.out workflow/run_subject.sbatch $WORK/$s/in $WORK/$s/out
done
```

On a cluster the jobs queue and run side by side. On a workstation this would start them
all at once on one machine, so there, run one subject at a time.

## Where to go next

| To | Read |
|---|---|
| understand what each stage does | [README, "What it does"](README.md#what-it-does) |
| change a setting, or understand shells and SH order | [`datasets/README.md`](datasets/README.md) |
| run data from a dataset that is not listed | [`datasets/generic/README.md`](datasets/generic/README.md) |
| know every option of a job script | the comment at the top of each script in `workflow/` |
| know what is in the image | [SBCI_Images](https://github.com/sbci-brain/SBCI_Images) |
