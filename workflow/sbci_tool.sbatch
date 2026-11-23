#!/bin/bash -l
#SBATCH --job-name=sbci-tool
#SBATCH --nodes=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=16g
#SBATCH --time=01:00:00
# Run one command of the `sbci` analysis package (explore, coupling, qc, convert, validate,
# cifti, fastpath, cohort) in the sbci-full image, as a short job. The package is separate from
# this pipeline: install it in the image's Python, or name its source folder in
# SBCI_ANALYSIS_PACKAGE. The defaults below suit the short commands; fastpath and cohort pass
# more on sbatch.
#
#   bash <pipeline>/workflow/submit.sh --output=<OUT_DIR>/sbci-tool-%j.out \
#     <pipeline>/workflow/sbci_tool.sbatch explore <files.h5> --out <OUT_DIR>
set -uo pipefail
# This folder: from submit.sh, else the one this script is in (sbatch runs a copy of the
# script from Slurm's spool folder, so there it must come from submit.sh).
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PIPELINE=${SBCI_PIPELINE:-$here}
[ -f "$PIPELINE/workflow/submit.sh" ] || { echo "ERROR: cannot find the pipeline folder; start this job with workflow/submit.sh, which sets SBCI_PIPELINE." >&2; exit 2; }
source "$PIPELINE/workflow/runtime_env.sh"
# Outside the image this script runs again inside it, seeing every file among the arguments
# and, for an output not made yet, its folder.
in_image sbci_tool.sbatch ${SBCI_ANALYSIS_PACKAGE:+"$SBCI_ANALYSIS_PACKAGE"} "$@" -- "$@"
image_environment
[ -n "${SBCI_ANALYSIS_PACKAGE:-}" ] && export PYTHONPATH="$SBCI_ANALYSIS_PACKAGE${PYTHONPATH:+:$PYTHONPATH}"
export MPLBACKEND=Agg
python3 -c 'import sbci' 2>/dev/null || { echo "ERROR: the sbci analysis package is not installed in the image's Python; install it, or name its source folder in SBCI_ANALYSIS_PACKAGE." >&2; exit 2; }
now() { date +%Y-%m-%dT%H:%M:%S%z; }
echo "host=$(hostname) job=${SLURM_JOB_ID:-none} pipeline=$PIPELINE started=$(now)"
echo "command: sbci $*"
python3 -m sbci "$@"
rc=$?
echo "finished=$(now) rc=$rc"
exit "$rc"
