#!/bin/bash
# Python 3 / scilpy 2.x adaptation of ADNI_example/sbci_step6_functional.sh
#
# Per-subject: converts fMRI BOLD time series to SBCI format, concatenates
# runs, and computes functional connectivity matrix.
#
# Reference: https://github.com/sbci-brain/SBCI_Pipeline/blob/master/ADNI_example/sbci_step6_functional.sh
#
# Command mappings (Python 2 → Python 3):
#   python ${SCRIPT_PATH}/convert_fsfast_timeseries.py → python3 ${SBCI_SCRIPTS}/convert_fsfast_timeseries.py
#   python ${SCRIPT_PATH}/concatenate_timeseries.py    → python3 ${SBCI_SCRIPTS}/concatenate_timeseries.py
#   python ${SCRIPT_PATH}/calculate_fc.py              → python3 ${SBCI_SCRIPTS}/calculate_fc.py
#
# Usage:
#   export SBCI_CONFIG=/path/to/sbci_config
#   export SBCI_SCRIPTS=/path/to/SBCI_Pipeline/scripts
#   source ~/.bashrc_sbci && conda activate sbci
#   cd /path/to/subject_dir
#   bash /path/to/SBCI_Pipeline/scripts/ADNI_example/sbci_step6_functional.sh

set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${WRAPPER_DIR}/../.." && pwd)"
SBCI_SCRIPTS="${SBCI_SCRIPTS:-${REPO_ROOT}/scripts}"
export PYTHONPATH="${SBCI_SCRIPTS}:${PYTHONPATH:-}"

source "${SBCI_CONFIG}"

if [ -z "${REFDIR+x}" ]; then
    REFDIR="${SBCI_PATH}/data/fsaverage"
fi

##########################################################
# CALCULATE FUNCTIONAL BOLD TIME SERIES AND CONNECTIVITY #
##########################################################

mkdir -p dwi_pipeline/sbci_connectome

# Locate all preprocessed BOLD runs.  A raw func/ directory is not sufficient:
# step 6 consumes the three FSFast residual files written by preprocessing.
if [ ! -d ./fmri/bold ]; then
    echo "ERROR: Missing preprocessed fMRI directory: $(pwd)/fmri/bold" >&2
    echo "Run and QC preproc_step5_fmri.sh, then make sure fmri is linked into this branch tree." >&2
    exit 1
fi

FCRUNS=./dwi_pipeline/sbci_connectome/fcruns
find ./fmri/bold/ -regextype sed -regex ".*/[0-9]\+$" -type d -print0 \
    | sort -z > "${FCRUNS}"

if [ ! -s "${FCRUNS}" ]; then
    echo "ERROR: No numeric BOLD run directories found below $(pwd)/fmri/bold" >&2
    echo "Expected paths such as fmri/bold/001 containing FC residual images." >&2
    exit 1
fi

TS_LIST=()
run=1

while IFS= read -r -d '' FCDIR; do

    FCOUTPUTDIR=./dwi_pipeline/sbci_connectome/RUN$(printf '%03d' "${run}")
    mkdir -p "${FCOUTPUTDIR}"

    for REQUIRED in fc.surface.lh.nii.gz fc.surface.rh.nii.gz fc.mni.nii.gz; do
        if [ ! -s "${FCDIR}/${REQUIRED}" ]; then
            echo "ERROR: Missing or empty preprocessed fMRI input: ${FCDIR}/${REQUIRED}" >&2
            exit 1
        fi
    done

    # Step1) Convert BOLD timeseries into SBCI format
    python3 "${SBCI_SCRIPTS}/convert_fsfast_timeseries.py" \
            --lh_time_series "${FCDIR}/fc.surface.lh.nii.gz" \
            --rh_time_series "${FCDIR}/fc.surface.rh.nii.gz" \
            --sub_time_series "${FCDIR}/fc.mni.nii.gz" \
            --aparc "${REFDIR}/mri.2mm/aseg.mgz" \
            --sub_rois "${ROIS[@]}" \
            --output "${FCOUTPUTDIR}/fc_ts.npz" -f

    run=$((run + 1))
    TS_LIST+=("${FCOUTPUTDIR}/fc_ts.npz")

done < "${FCRUNS}"

# Step2) Join all the runs into a single timeseries
python3 "${SBCI_SCRIPTS}/concatenate_timeseries.py" \
        --time_series "${TS_LIST[@]}" \
        --output ./dwi_pipeline/sbci_connectome/fc_ts.npz -f

# Step3) Calculate FC matrix at the given resolution in template space
python3 "${SBCI_SCRIPTS}/calculate_fc.py" \
        --time_series ./dwi_pipeline/sbci_connectome/fc_ts.npz \
        --mesh        "${OUTPUT_PATH}/mapping_avg_${RESOLUTION}.npz" \
        --output      ./dwi_pipeline/sbci_connectome/fc_avg_${RESOLUTION}.mat \
        --ts -f

echo "Finished processing SBCI functional: $(date)"
