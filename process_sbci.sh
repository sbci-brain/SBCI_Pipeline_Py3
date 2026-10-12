#!/bin/bash

set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "Usage: $0 SUBJECT_LIST SUBJECTS_DIR WRAPPER_DIR" >&2
    exit 2
fi

IN=$1
OUT=$2
SCRIPTS=$3

function fail() {
    echo "ERROR: $*" >&2
    exit 1
}

# CHANGE LOCATION TO YOUR SOURCE FILE
ENV_FILE="${SBCI_ENV_FILE:-${HOME}/.bashrc_sbci_py3}"
if [[ ! -f "${ENV_FILE}" ]]; then
    fail "Environment file not found: ${ENV_FILE}. Set SBCI_ENV_FILE to the Python 3 SBCI environment loader."
fi
echo "Sourcing ${ENV_FILE}"
set +euo pipefail
source "${ENV_FILE}"
set -euo pipefail

# CHANGE LOCATION TO THE CONFIGURATION FILE FOR SBCI
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR}"
export SBCI_CONFIG="${SBCI_CONFIG:-${SCRIPTS}/sbci_config}"
export SBCI_SCRIPTS="${SBCI_SCRIPTS:-${REPO_ROOT}/scripts}"
export PYTHONPATH="${SBCI_SCRIPTS}:${PYTHONPATH:-}"

# CHANGE FOR SPECIFIC SBATCH OPTIONS
OPTIONS=()

[[ -f "${IN}" ]] || fail "Subject list not found: ${IN}"
[[ -d "${OUT}" ]] || fail "Subject tree not found: ${OUT}"
[[ -d "${SCRIPTS}" ]] || fail "Wrapper directory not found: ${SCRIPTS}"
[[ -f "${SBCI_CONFIG}" ]] || fail "SBCI config not found: ${SBCI_CONFIG}"

command -v python3 >/dev/null 2>&1 || fail "python3 is not available after sourcing ${ENV_FILE}"
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
[[ "${PYTHON_MAJOR}" == "3" ]] || fail "The Py3 branch requires Python 3, but $(python3 --version 2>&1) is active"
echo "Using $(command -v python3): $(python3 --version 2>&1)"

echo "Sourcing SBCI config file"
source "${SBCI_CONFIG}"

EXPECTED_BRANCH="${SBCI_BRANCH:-py3}"
OUT=$(readlink -f "${OUT}")
IN=$(readlink -f "${IN}")
SCRIPTS=$(readlink -f "${SCRIPTS}")
OUTPUT_PATH="${OUTPUT_PATH%/}"

case "${EXPECTED_BRANCH}" in
    py2|py3) ;;
    *) fail "SBCI_BRANCH must be py2 or py3, not: ${EXPECTED_BRANCH}" ;;
esac

case "${OUT}" in
    */${EXPECTED_BRANCH}/preprocessed) ;;
    *) fail "Subject tree must end in /${EXPECTED_BRANCH}/preprocessed, not: ${OUT}" ;;
esac

case "${OUTPUT_PATH}" in
    */${EXPECTED_BRANCH}/outputs) ;;
    *) fail "OUTPUT_PATH must end in /${EXPECTED_BRANCH}/outputs, not: ${OUTPUT_PATH}" ;;
esac

BASE="${OUT%/${EXPECTED_BRANCH}/preprocessed}"
SHARED_ROOT="${SBCI_SHARED_PREPROCESSED:-${BASE}/preprocessed}"
SHARED_ROOT="${SHARED_ROOT%/}"
REQUIRE_PSC="${SBCI_REQUIRE_PSC:-1}"

[[ -d "${SHARED_ROOT}" ]] || fail "Shared preprocessing tree not found: ${SHARED_ROOT}"
[[ "${OUT}" != "$(readlink -f "${SHARED_ROOT}")" ]] || fail "Branch tree resolves to the shared preprocessing tree"

command -v sbatch >/dev/null 2>&1 || fail "sbatch is not available"
command -v uuidgen >/dev/null 2>&1 || fail "uuidgen is not available"

# helper function to return job id
function sb() {
   local result
   if ! result="$(sbatch "$@")"; then
     echo "ERROR: sbatch failed: $*" >&2
     return 1
   fi

   if [[ "$result" =~ Submitted\ batch\ job\ ([0-9]+) ]]; then
     echo "${BASH_REMATCH[1]}"
   else
     echo "ERROR: Could not parse job ID from sbatch output: ${result}" >&2
     return 1
   fi
}

# create a unique job name prefix
JID=$(uuidgen | cut -d- -f1)

# get all subject names
subjects=()
while IFS= read -r subject || [[ -n "${subject}" ]]; do
    subject="${subject%$'\r'}"
    [[ -z "${subject}" || "${subject}" == \#* ]] && continue
    subjects+=("${subject}")
done < "${IN}"

# make sure there are subjects
if [[ ${#subjects[@]} -eq 0 ]]; then
    echo "no subjects found in ${IN}"
    exit 1
fi

echo "Processing ${#subjects[@]} subject(s)"

for subject in "${subjects[@]}"; do
    case "${subject}" in
        /*|*..*) fail "Invalid session path in ${IN}: ${subject}" ;;
    esac

    SUBJECT_DIR="${OUT}/${subject}"
    SHARED_SESSION="${SHARED_ROOT}/${subject}"
    [[ -d "${SUBJECT_DIR}" ]] || fail "Branch session not found: ${SUBJECT_DIR}"
    [[ -d "${SHARED_SESSION}" ]] || fail "Shared session not found: ${SHARED_SESSION}"

    for item in diffusion structure; do
        BRANCH_INPUT="${SUBJECT_DIR}/dwi_pipeline/${item}"
        SHARED_INPUT="${SHARED_SESSION}/dwi_pipeline/${item}"
        [[ -e "${SHARED_INPUT}" ]] || fail "Missing shared input: ${SHARED_INPUT}"
        [[ -e "${BRANCH_INPUT}" ]] || fail "Missing or broken branch input: ${BRANCH_INPUT}"
        BRANCH_INPUT_REAL=$(readlink -f "${BRANCH_INPUT}")
        SHARED_INPUT_REAL=$(readlink -f "${SHARED_INPUT}")
        [[ "${BRANCH_INPUT_REAL}" == "${SHARED_INPUT_REAL}" ]] || \
            fail "${BRANCH_INPUT} resolves to ${BRANCH_INPUT_REAL}, not shared input ${SHARED_INPUT_REAL}"
    done

    BRANCH_FMRI="${SUBJECT_DIR}/fmri"
    SHARED_FMRI="${SHARED_SESSION}/fmri"
    [[ -d "${SHARED_FMRI}/bold" ]] || fail "Missing shared fMRI input: ${SHARED_FMRI}/bold"
    [[ -d "${BRANCH_FMRI}/bold" ]] || fail "Missing preprocessed fMRI input: ${BRANCH_FMRI}/bold"
    BRANCH_FMRI_REAL=$(readlink -f "${BRANCH_FMRI}")
    SHARED_FMRI_REAL=$(readlink -f "${SHARED_FMRI}")
    [[ "${BRANCH_FMRI_REAL}" == "${SHARED_FMRI_REAL}" ]] || \
        fail "${BRANCH_FMRI} resolves to ${BRANCH_FMRI_REAL}, not shared input ${SHARED_FMRI_REAL}"

    if [[ "${REQUIRE_PSC}" == "1" ]]; then
        BRANCH_PSC="${SUBJECT_DIR}/dwi_pipeline/psc_connectome"
        SHARED_PSC="${SHARED_SESSION}/dwi_pipeline/psc_connectome"
        [[ -e "${SHARED_PSC}" ]] || fail "Missing shared PSC input: ${SHARED_PSC}"
        [[ -e "${BRANCH_PSC}" ]] || fail "Missing or broken shared PSC input: ${BRANCH_PSC}"
        BRANCH_PSC_REAL=$(readlink -f "${BRANCH_PSC}")
        SHARED_PSC_REAL=$(readlink -f "${SHARED_PSC}")
        [[ "${BRANCH_PSC_REAL}" == "${SHARED_PSC_REAL}" ]] || \
            fail "${BRANCH_PSC} resolves to ${BRANCH_PSC_REAL}, not shared input ${SHARED_PSC_REAL}"
    fi

    for branch_output in set sbci_connectome; do
        OUTPUT_DIR="${SUBJECT_DIR}/dwi_pipeline/${branch_output}"
        [[ ! -L "${OUTPUT_DIR}" ]] || fail "Branch-specific output must not be a symlink: ${OUTPUT_DIR}"
    done
done

echo "Parallel-branch preflight passed for ${EXPECTED_BRANCH}:"
echo "  subject tree: ${OUT}"
echo "  shared inputs: ${SHARED_ROOT}"
echo "  branch grids: ${OUTPUT_PATH}"
echo "  PSC required: ${REQUIRE_PSC}"

echo "Beginning processing of SBCI grid: $(date)"
mkdir -p "${OUTPUT_PATH}"

STEP1=$(sb "${OPTIONS[@]}" --time=4:00:00 --mem=4g --job-name="${JID}.step1" \
    --export=ALL,SBCI_CONFIG,SBCI_SCRIPTS \
    --output="${OUTPUT_PATH}/sbci_step1_process_grid.log" "${SCRIPTS}/sbci_step1_process_grid.sh")

sleep 0.01

ROOT=$(pwd)
SUBMITTED_JOBS=("${STEP1}")

for subject in "${subjects[@]}"; do
    cd "${OUT}/${subject}"

    echo "Placing subject ${subject} in queue"
    STEP2=$(sb "${OPTIONS[@]}" --time=20:00:00 --mem=20g --job-name="${JID}.step2.${subject}" \
        --export=ALL,SBCI_CONFIG,SBCI_SCRIPTS \
        --output=sbci_step2_prepare_set.log \
        --dependency="afterok:${STEP1}" "${SCRIPTS}/sbci_step2_prepare_set.sh")
    SUBMITTED_JOBS+=("${STEP2}")

    STEP3=()
    for ((RUN = 1; RUN <= N_RUNS; RUN++)); do
        STEP3_JOB=$(sb "${OPTIONS[@]}" --time=40:00:00 --mem=20g --job-name="${JID}.step3.${subject}" \
            --export=ALL,SBCI_CONFIG,SBCI_SCRIPTS \
            --output="sbci_step3_set_${RUN}.log" \
            --dependency="afterok:${STEP2}" "${SCRIPTS}/sbci_step3_run_set.sh" "${RUN}")
        STEP3+=("${STEP3_JOB}")
        SUBMITTED_JOBS+=("${STEP3_JOB}")
    done

    STEP3_DEPENDENCY=$(IFS=:; echo "${STEP3[*]}")
    STEP4=$(sb "${OPTIONS[@]}" --time=4:00:00 --mem=4g --job-name="${JID}.step4.${subject}" \
        --export=ALL,SBCI_CONFIG,SBCI_SCRIPTS \
        --output=sbci_step4_process_surfaces.log \
        --dependency="afterok:${STEP3_DEPENDENCY}" "${SCRIPTS}/sbci_step4_process_surfaces.sh")
    SUBMITTED_JOBS+=("${STEP4}")

    STEP5=$(sb "${OPTIONS[@]}" --time=20:00:00 --mem=20g --job-name="${JID}.step5.${subject}" \
        --export=ALL,SBCI_CONFIG,SBCI_SCRIPTS \
        --output=sbci_step5_structural.log \
        --dependency="afterok:${STEP4}" "${SCRIPTS}/sbci_step5_structural.sh")
    SUBMITTED_JOBS+=("${STEP5}")

    STEP6=$(sb "${OPTIONS[@]}" --time=10:00:00 --mem=20g --job-name="${JID}.step6.${subject}" \
        --export=ALL,SBCI_CONFIG,SBCI_SCRIPTS \
        --output=sbci_step6_functional.log \
        --dependency="afterok:${STEP1}" "${SCRIPTS}/sbci_step6_functional.sh")
    SUBMITTED_JOBS+=("${STEP6}")

    cd "${ROOT}"
done

echo "Submitted SBCI ${EXPECTED_BRANCH} batch ${JID}"
echo "Job IDs: ${SUBMITTED_JOBS[*]}"
