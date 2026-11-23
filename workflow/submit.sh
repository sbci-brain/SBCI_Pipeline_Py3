#!/bin/bash
# Start one SBCI job script on this machine. With Slurm this is sbatch with the options given.
# Without it (a server, or a desktop under Linux or WSL) the job runs in the background on this
# machine, its log at --output; the other sbatch options (CPUs, memory, time) do not apply
# there. Either way the job scripts get this pipeline's folder as SBCI_PIPELINE, so they work
# wherever it was cloned (sbatch runs a copy of the script from Slurm's spool folder).
#   bash <pipeline>/workflow/submit.sh [--output=LOG] [other sbatch options] <job script> ARGS...
# A job script given by name only (sbci_tool.sbatch) is taken from this folder.
SBCI_PIPELINE=${SBCI_PIPELINE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
export SBCI_PIPELINE
opts=()
while [ $# -gt 0 ] && [ "${1#-}" != "$1" ]; do opts+=("$1"); shift; done
[ $# -gt 0 ] || { echo "usage: submit.sh [sbatch options] <job script> ARGS..." >&2; exit 2; }
script=$1; shift
[ -f "$script" ] || script=$SBCI_PIPELINE/workflow/$script
[ -f "$script" ] || { echo "ERROR: no job script $script" >&2; exit 2; }
if command -v sbatch >/dev/null 2>&1; then
    exec sbatch ${opts[@]+"${opts[@]}"} "$script" "$@"
fi
id=local-$(date +%Y%m%d%H%M%S)-$$
log=$PWD/sbci-job-%j.out
for o in ${opts[@]+"${opts[@]}"}; do case $o in --output=*) log=${o#--output=} ;; esac; done
log=${log//%j/$id}
mkdir -p "$(dirname "$log")" || exit 2
# Detached (setsid where there is one, as on Linux), so the job outlives the shell that started it.
detach=(); command -v setsid >/dev/null 2>&1 && detach=(setsid)
nohup ${detach[@]+"${detach[@]}"} bash "$script" "$@" > "$log" 2>&1 < /dev/null &
echo "Started job $id (process $!) on $(hostname), without Slurm; log: $log"
