#!/bin/bash

# =============================================================================
# preproc_step5_fmri.sh — ADNI resting-state fMRI preprocessing
#
# Adapted from integrated_pipeline/preproc_step5_fmri.sh for ADNI-specific
# acquisition parameters:
#   - TR = 3 s  (not 2.5 s)                    -- FMRI_TR, see the timing block below
#   - 48 slices, Siemens interleaved ordering  -- FMRI_STC
#   - Single run per subject (no run-* in filename)
#   - Input BOLD is the distortion-corrected image from adni_fmri_correction
#     (fmri/bold_distcorr.nii.gz); the stage stops if it is absent
#   - No pre-computed motion .tsv files; FSFast handles motion correction
#     internally via -mcextreg
#
# Changes from integrated_pipeline version:
#   1. Added data-preparation block: copies BOLD into fsfast/bold/001/f.nii.gz
#   2. -TR 2.5 → -TR 3
#   3. Added -nskip 4 (skip 12 s of T1 saturation at TR=3)
#   4. -polyfit 1 → -polyfit 2 (quadratic detrending; TR=3 s has more drift)
#   5. SUBJECTS_DIR updated to match ADNI working directory structure
#
# Expected working directory structure before running this script:
#   <subject_dir>/
#   ├── dwi_pipeline/structure/t1_freesurfer/  (from preproc_step3)
#   └── fmri/bold_distcorr.nii.gz                      (from adni_fmri_correction)
#
# Outputs:
#   fmri/bold/001/fc.surface.lh.nii.gz
#   fmri/bold/001/fc.surface.rh.nii.gz
#   fmri/bold/001/fc.mni.nii.gz
# =============================================================================

# The legacy ~/.bashrc_sbci_legacy_py2 (Python 2 env) is deliberately NOT sourced:
# this package is Python 3, and FreeSurfer/FSL come from the caller's modules.

set -eo pipefail

if [ -z "${SBCI_CONFIG:-}" ] || [ ! -f "${SBCI_CONFIG}" ]; then
    echo "ERROR: SBCI_CONFIG is missing or does not name a readable file: ${SBCI_CONFIG:-<unset>}" >&2
    exit 1
fi

echo "Sourcing SBCI config file"
set +eu
source "${SBCI_CONFIG}"

echo "Sourcing FREESURFER_HOME"
# set -e/pipefail stay off here: SetUpFreeSurfer.sh runs a grep over
# ~/matlab/startup.m that exits 1 when the string is absent.
source "${FREESURFER_PATH}/SetUpFreeSurfer.sh"
set -eu

# Acquisition timing, ADNI values as defaults. The CyberNeuro executor exports
# FMRI_TR from the BIDS RepetitionTime and FMRI_NSKIP = round(FMRI_SKIP_SECONDS / TR)
# after checking them against sbci_config, so no dataset needs this file edited.
#   FMRI_TR       repetition time, s                          ADNI 3
#   FMRI_NSKIP    leading frames dropped (T1 saturation)      ADNI 4 (12 s)
#   FMRI_POLYFIT  detrending polynomial order                 ADNI 2
#   FMRI_STC      FSFast slice-timing order, or none          ADNI siemens
FMRI_TR=${FMRI_TR:-3}
FMRI_NSKIP=${FMRI_NSKIP:-4}
FMRI_POLYFIT=${FMRI_POLYFIT:-2}
FMRI_STC=${FMRI_STC:-siemens}
STC_ARGS="-stc ${FMRI_STC}"
if [ "${FMRI_STC}" = none ]; then
    STC_ARGS=""
fi
#   FMRI_MOTION_REGRESSORS  mcextreg: FSFast's own motion estimates            ADNI
#                           tsv: each run's BIDS *_motion.tsv (header and first
#                           column dropped -> 6 regressors), the lab's ABCD/HCP recipe
FMRI_MOTION_REGRESSORS=${FMRI_MOTION_REGRESSORS:-mcextreg}
case "${FMRI_MOTION_REGRESSORS}" in
    mcextreg) MOTION_ARGS="-mcextreg" ;;
    tsv)      MOTION_ARGS="-nuisreg motion.dat 6" ;;
    *)
        echo "ERROR: FMRI_MOTION_REGRESSORS=${FMRI_MOTION_REGRESSORS} is not mcextreg or tsv." >&2
        exit 1
        ;;
esac
echo "fMRI timing: TR=${FMRI_TR} nskip=${FMRI_NSKIP} polyfit=${FMRI_POLYFIT} stc=${FMRI_STC} motion=${FMRI_MOTION_REGRESSORS}"

# FreeSurfer 6's own bin/slicedelay is a Python 2 script and dies under a
# Python 3 interpreter, so the package ships a port in compat_bin and puts
# it ahead of FREESURFER_HOME/bin. SBCI_LOCAL_BIN can override.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_COMPAT_BIN="$(cd "${SCRIPT_DIR}/../../../../compat_bin" 2>/dev/null && pwd || true)"
# Expose ONLY slicedelay: compat_bin also holds mri_convert/mrconvert
# wrappers for the container, and putting those ahead of FreeSurfer's own
# binaries breaks FSFast (its mri_convert --frame 0 is rejected).
if [ -n "${PACKAGE_COMPAT_BIN}" ] && [ -x "${PACKAGE_COMPAT_BIN}/slicedelay" ]; then
    mkdir -p .sbci_bin
    ln -sf "${PACKAGE_COMPAT_BIN}/slicedelay" .sbci_bin/slicedelay
    export PATH="${PWD}/.sbci_bin:${PATH}"
fi
SBCI_LOCAL_BIN="${SBCI_LOCAL_BIN:-}"
[ -n "${SBCI_LOCAL_BIN}" ] && [ -d "${SBCI_LOCAL_BIN}" ] && export PATH="${SBCI_LOCAL_BIN}:${PATH}"
hash -r
SLICEDELAY=$(command -v slicedelay || true)
if [ -z "${SLICEDELAY}" ]; then
    echo "ERROR: Patched slicedelay is not available on PATH." >&2
    exit 1
fi
echo "Using slicedelay: ${SLICEDELAY}"
head -n 1 "${SLICEDELAY}"

echo "Begin FSFast fMRI preprocessing: $(date)"

export SUBJECTS_DIR=$(pwd)/dwi_pipeline/structure

# =============================================================================
# Data preparation: place BOLD into FSFast directory layout
#
# FSFast expects: fsfast/bold/<3-digit-run>/f.nii.gz
# ADNI has a single resting-state run → run 001
# Input: the distortion-corrected BOLD the adni_fmri_correction stage writes,
# fmri/bold_distcorr.nii.gz (fmri_distcorr/ in older trees). There is deliberately
# no fallback to uncorrected func/ BOLD: it used to be taken, with only a log
# warning, whenever the corrected file was not at the old fmri_distcorr/ path --
# a path this package never writes -- so FC could come from uncorrected BOLD and
# the stage still exited 0.
# =============================================================================

RUN_DIR="fsfast/bold/001"
mkdir -p "${RUN_DIR}"

if [ ! -f "${RUN_DIR}/f.nii.gz" ]; then
    BOLD_CORRECTED=""
    for _candidate in fmri/bold_distcorr.nii.gz fmri_distcorr/bold_distcorr.nii.gz; do
        if [ -f "$_candidate" ]; then
            BOLD_CORRECTED=$_candidate
            break
        fi
    done
    if [ -z "$BOLD_CORRECTED" ]; then
        echo "ERROR: distortion-corrected BOLD (fmri/bold_distcorr.nii.gz) not found;" >&2
        echo "       refusing to fall back to uncorrected BOLD. Run adni_fmri_correction first." >&2
        exit 1
    fi
    echo "Using B0-corrected BOLD: ${BOLD_CORRECTED}"
    cp "$BOLD_CORRECTED" "${RUN_DIR}/f.nii.gz"
    # Further rest runs (a distortion-corrected-input copy keeps them all, e.g. ABCD's
    # four): bold_distcorr_run-NN.nii.gz -> fsfast/bold/0NN. preproc-sess, mkanalysis
    # (-per-run) and the residual export below already process every run directory.
    for _extra in "$(dirname "$BOLD_CORRECTED")"/bold_distcorr_run-*.nii.gz; do
        [ -f "$_extra" ] || continue
        _run=${_extra##*_run-}
        _run=${_run%.nii.gz}
        mkdir -p "fsfast/bold/0${_run}"
        echo "Using B0-corrected BOLD run ${_run}: ${_extra}"
        cp "$_extra" "fsfast/bold/0${_run}/f.nii.gz"
    done
else
    echo "fsfast/bold/001/f.nii.gz already exists. Skipping copy."
fi

# Motion regressors from the dataset's own files, converted as the upstream ABCD/HCP
# scripts did: fmri/bold_distcorr[_run-NN]_motion.tsv -> fsfast/bold/0NN/motion.dat.
# A missing file stops the stage instead of silently falling back to -mcextreg.
if [ "${FMRI_MOTION_REGRESSORS}" = tsv ]; then
    for _dir in fsfast/bold/[0-9][0-9][0-9]; do
        _n=${_dir##*/}
        if [ "$_n" = 001 ]; then
            _tsv=fmri/bold_distcorr_motion.tsv
        else
            _tsv=fmri/bold_distcorr_run-${_n#0}_motion.tsv
        fi
        if [ ! -s "$_tsv" ]; then
            echo "ERROR: FMRI_MOTION_REGRESSORS=tsv but ${_tsv} is missing for run ${_n}." >&2
            exit 1
        fi
        tail -n +2 "$_tsv" | cut -f2- | tr '\t' ' ' > "${_dir}/motion.dat"
        echo "Motion regressors for run ${_n}: ${_tsv}"
    done
fi

# =============================================================================
# FSFast preprocessing
# preproc-sess performs:
#   - Motion correction (mcflirt-based)
#   - Spatial smoothing (5 mm FWHM)
#   - Registration to fsaverage surface and MNI305 volume
# Note: -stc siemens matches the py2 reference; it needs the compat_bin slicedelay port
#       because FreeSurfer 6.0.0 ships a Python 2 slicedelay.
# =============================================================================

cd fsfast
echo t1_freesurfer > subjectname

SUBJECT_ID=.

preproc-sess \
    -s ${SUBJECT_ID} \
    -fwhm 5 \
    ${STC_ARGS} \
    -surface fsaverage lhrh \
    -per-run \
    -fsd bold \
    -mni305 \
    -force

# =============================================================================
# Extract nuisance signals: white matter and ventricular CSF
# =============================================================================

fcseed-config -wm   -fcname wm.dat   -fsd bold -mean -cfg wm.config   -force
fcseed-config -vcsf -fcname vcsf.dat -fsd bold -mean -cfg vcsf.config -force

fcseed-sess -s ${SUBJECT_ID} -cfg wm.config   -force
fcseed-sess -s ${SUBJECT_ID} -cfg vcsf.config -force

# =============================================================================
# Nuisance regression model definition (mkanalysis-sess)
#
# Nuisance regressors:
#   vcsf.dat 1    — ventricular CSF mean signal
#   wm.dat 1      — white matter mean signal
#   global.waveform.dat 1 — whole-brain global signal
#   -mcextreg     — 6 FSFast-internal motion parameters (from preproc-sess MC)
#
# Temporal processing:
#   -TR           — FMRI_TR (ADNI 3)
#   -nskip        — FMRI_NSKIP frames (ADNI 4: 12 s T1 saturation at TR=3 s)
# Note: -stc siemens (FMRI_STC) matches the py2 reference; it needs the compat_bin
#       slicedelay port because FreeSurfer 6.0.0 ships a Python 2 slicedelay.
#   -polyfit      — FMRI_POLYFIT (ADNI 2: quadratic; longer TR → more low-freq drift)
#   -hpf 0.009    — highpass filter cutoff in Hz  (111 s period)
#   -lpf 0.08     — lowpass filter cutoff in Hz   (12.5 s period)
#   -inorm        — intensity normalisation
#   -fwhm 5       — spatial smoothing (redundant with preproc-sess but kept
#                   here so mkanalysis records it in the design matrix)
# =============================================================================

mkanalysis-sess -analysis fc.surface.lh \
    -surface fsaverage lh \
    -per-run \
    -nskip ${FMRI_NSKIP} \
    -fwhm 5 \
    -notask \
    -nuisreg vcsf.dat 1 \
    -nuisreg wm.dat 1 \
    -nuisreg global.waveform.dat 1 \
    ${MOTION_ARGS} \
    -polyfit ${FMRI_POLYFIT} \
    -fsd bold \
    -TR ${FMRI_TR} \
    ${STC_ARGS} \
    -hpf 0.009 \
    -lpf 0.08 \
    -inorm \
    -force

mkanalysis-sess -analysis fc.surface.rh \
    -surface fsaverage rh \
    -per-run \
    -nskip ${FMRI_NSKIP} \
    -fwhm 5 \
    -notask \
    -nuisreg vcsf.dat 1 \
    -nuisreg wm.dat 1 \
    -nuisreg global.waveform.dat 1 \
    ${MOTION_ARGS} \
    -polyfit ${FMRI_POLYFIT} \
    -fsd bold \
    -TR ${FMRI_TR} \
    ${STC_ARGS} \
    -hpf 0.009 \
    -lpf 0.08 \
    -inorm \
    -force

mkanalysis-sess -analysis fc.mni \
    -mni305 \
    -per-run \
    -nskip ${FMRI_NSKIP} \
    -fwhm 5 \
    -notask \
    -nuisreg vcsf.dat 1 \
    -nuisreg wm.dat 1 \
    -nuisreg global.waveform.dat 1 \
    ${MOTION_ARGS} \
    -polyfit ${FMRI_POLYFIT} \
    -fsd bold \
    -TR ${FMRI_TR} \
    ${STC_ARGS} \
    -hpf 0.009 \
    -lpf 0.08 \
    -inorm \
    -force

# =============================================================================
# Apply nuisance regression; save residuals as cleaned BOLD
# =============================================================================

# selxavg3-sess shells out to MATLAB unless told to use the compiled
# fast_selxavg3b.glnxa64, which FreeSurfer 6 ships together with an MCR
# runtime. Prefer that: no MATLAB install or licence is then required.
SXA_MCR=""
if command -v matlab >/dev/null 2>&1; then
    echo "Using MATLAB for selxavg3-sess: $(command -v matlab)"
elif [ -x "${FREESURFER_HOME}/bin/fast_selxavg3b.glnxa64" ] \
     && [ -e "${FREESURFER_HOME}/MCRv80/bin/glnxa64/libmwlaunchermain.so" ]; then
    # Only usable when the MCR is complete; FreeSurfer 6 ships a partial
    # MCRv80 on some installs and the binary then fails to load.
    SXA_MCR="-mcr ${FREESURFER_HOME}/MCRv80"
    echo "Using compiled selxavg3 with MCR: ${FREESURFER_HOME}/MCRv80"
else
    echo "ERROR: selxavg3-sess needs matlab on PATH, or a complete ${FREESURFER_HOME}/MCRv80." >&2
    exit 1
fi

selxavg3-sess -analysis fc.surface.lh -s ${SUBJECT_ID} -no-con-ok -run-wise -svres -force ${SXA_MCR}
selxavg3-sess -analysis fc.surface.rh -s ${SUBJECT_ID} -no-con-ok -run-wise -svres -force ${SXA_MCR}
selxavg3-sess -analysis fc.mni        -s ${SUBJECT_ID} -no-con-ok -run-wise -svres -force ${SXA_MCR}

# =============================================================================
# Collect outputs
# =============================================================================

cd ..

find ./fsfast/bold/ -regextype sed -regex ".*/[0-9]\+$" -type d -printf "%f\n" \
    | sort > ./fcruns

if [ ! -s ./fcruns ]; then
    echo "ERROR: No numeric FSFast BOLD runs were produced." >&2
    exit 1
fi

while IFS= read -r BOLDRUN; do
    LH_SOURCE="./fsfast/bold/fc.surface.lh/pr${BOLDRUN}/res/res-${BOLDRUN}.nii.gz"
    RH_SOURCE="./fsfast/bold/fc.surface.rh/pr${BOLDRUN}/res/res-${BOLDRUN}.nii.gz"
    MNI_SOURCE="./fsfast/bold/fc.mni/pr${BOLDRUN}/res/res-${BOLDRUN}.nii.gz"

    for REQUIRED in "${LH_SOURCE}" "${RH_SOURCE}" "${MNI_SOURCE}"; do
        if [ ! -s "${REQUIRED}" ]; then
            echo "ERROR: Missing or empty FSFast residual: ${REQUIRED}" >&2
            exit 1
        fi
    done

    mkdir -p "fmri/bold/${BOLDRUN}"
    mv "${LH_SOURCE}" "fmri/bold/${BOLDRUN}/fc.surface.lh.nii.gz"
    mv "${RH_SOURCE}" "fmri/bold/${BOLDRUN}/fc.surface.rh.nii.gz"
    mv "${MNI_SOURCE}" "fmri/bold/${BOLDRUN}/fc.mni.nii.gz"

    for OUTPUT in \
        "fmri/bold/${BOLDRUN}/fc.surface.lh.nii.gz" \
        "fmri/bold/${BOLDRUN}/fc.surface.rh.nii.gz" \
        "fmri/bold/${BOLDRUN}/fc.mni.nii.gz"
    do
        if [ ! -s "${OUTPUT}" ]; then
            echo "ERROR: Missing or empty exported FC input: ${OUTPUT}" >&2
            exit 1
        fi
    done
done < ./fcruns

echo "Finished FSFast fMRI preprocessing: $(date)"
