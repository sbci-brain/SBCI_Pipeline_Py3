#!/bin/bash -l
#SBATCH --job-name=sbci-subject
#SBATCH --nodes=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64g
#SBATCH --time=48:00:00
# Run one subject end to end: every preprocessing stage, one run_pipeline.py call per stage,
# then the SBCI stage, all inside the sbci-full image (FSL, ANTs, MRtrix3, FreeSurfer, Synb0,
# both Python environments). The one thing taken from this machine is MATLAB (R2018b
# validated), which FSFast, the fMRI stage, runs: have `matlab` on PATH (on a cluster, e.g.
# `module load apptainer matlab` before submitting; Slurm passes PATH on to the job).
# Needs Apptainer (clusters, Linux, WSL) or Docker, and the image (workflow/get_image.sbatch,
# once). Apptainer is used where it is on PATH; SBCI_CONTAINER=docker or =apptainer chooses.
# Start it with submit.sh (sbatch where there is Slurm, a background job elsewhere):
#
#   bash <pipeline>/workflow/submit.sh --output=<OUTPUT_DIR>/slurm-%j.out \
#     <pipeline>/workflow/run_subject.sbatch <INPUT_DIR> <OUTPUT_DIR>
#
# INPUT_DIR holds one subject folder and sbci_local.json (mode "preprocess", approved
# true; see datasets/README.md). OUTPUT_DIR must exist; a rerun on the same OUTPUT_DIR resumes
# after the last completed stage. Neither may be under a folder listed in SBCI_READONLY_ROOTS
# (colon-separated; a site sets it so shared data is never used as a run folder).
# Connectivity ends up in OUTPUT_DIR/sbci_output/files/work/<subject>/dwi_pipeline/sbci_connectome.
#
# Preflight first, in seconds (same environment and input, mode "inspect"; prints readiness,
# each stage's missing commands and any blockers; writes OUTPUT_DIR/inspect):
#
#   bash <pipeline>/workflow/run_subject.sbatch --inspect <INPUT_DIR> <OUTPUT_DIR>
set -uo pipefail
# `readlink -m` for any machine (macOS has no -m): the real path of the nearest folder that
# exists, plus the rest of the path.
real_path() {
    local p=$1 rest="" d
    case $p in /*) ;; *) p=$PWD/$p ;; esac
    while [ ! -d "$p" ]; do rest=/$(basename "$p")$rest; p=$(dirname "$p"); done
    d=$(cd "$p" && pwd -P)
    echo "${d%/}$rest"
}

INSPECT=""
[ "${1:-}" = "--inspect" ] && { INSPECT=1; shift; }
usage="usage: run_subject.sbatch [--inspect] INPUT_DIR OUTPUT_DIR"
for path in "${1:?$usage}" "${2:?$usage}"; do
    # -m, not -f: -f prints nothing for a path with missing parents, skipping the check.
    real=$(real_path "$path")
    IFS=: read -ra protected <<< "${SBCI_READONLY_ROOTS:-}"
    for root in ${protected[@]+"${protected[@]}"}; do
        [ -n "$root" ] || continue
        case "$real/" in
            "${root%/}"/*) echo "ERROR: refusing a path under $root ($path): SBCI_READONLY_ROOTS marks it read-only." >&2; exit 2 ;;
        esac
    done
done
IN=$(real_path "$1")
OUT=$(real_path "$2")
[ -s "$IN/sbci_local.json" ] || { echo "ERROR: $IN/sbci_local.json is missing." >&2; exit 2; }
[ -d "$OUT" ] || { echo "ERROR: output folder $OUT does not exist." >&2; exit 2; }

# This folder: from submit.sh, else the one this script is in (sbatch runs a copy of the
# script from Slurm's spool folder, so there it must come from submit.sh).
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PIPELINE=${SBCI_PIPELINE:-$here}
[ -f "$PIPELINE/workflow/submit.sh" ] || { echo "ERROR: cannot find the pipeline folder; start this job with workflow/submit.sh, which sets SBCI_PIPELINE." >&2; exit 2; }
source "$PIPELINE/workflow/runtime_env.sh"
# Outside the image this script runs again inside it, seeing the run's folders, the subject
# data and every file or folder sbci_local.json names (the licence, the profile).
# ponytail: paths split on spaces.
in_image run_subject.sbatch "$IN" "$OUT" "$IN"/*/ $(grep -o '"/[^"]*"' "$IN/sbci_local.json" | tr -d '"') \
    -- ${INSPECT:+--inspect} "$IN" "$OUT"
image_environment
SIF=${SBCI_IMAGE_FILE:-$SBCI_IMAGE} CONTAINER_VERSION=$SBCI_IMAGE
# FSFast's MATLAB code, without relying on the account's ~/matlab/startup.m.
[ -n "${FREESURFER_HOME:-}" ] && export MATLABPATH="$FREESURFER_HOME/fsfast/toolbox:$FREESURFER_HOME/matlab${MATLABPATH:+:$MATLABPATH}"
mkdir -p "$OUT/logs" "$OUT/sbci_input"
cd "$PIPELINE" || exit 2
# workflow.stages: all | preprocess | tractography | fc.
STAGES=$(python3 -c 'import json, sys; print((json.load(open(sys.argv[1])).get("workflow") or {}).get("stages", "all"))' "$IN/sbci_local.json")
# The SBCI stage's own config, written now so a contradiction stops the job before hours
# of preprocessing rather than after.
python3 - "$IN/sbci_local.json" "$OUT/sbci_input/sbci_local.json" <<'PY' || exit 2
import json, sys
from workflow.workflow_executor import sbci_stage_config
try:
    sbci = sbci_stage_config(json.load(open(sys.argv[1])))
except RuntimeError as exc:
    sys.exit(f"ERROR: {exc}")
json.dump(sbci, open(sys.argv[2], "w"), indent=2)
PY

if [ -n "$INSPECT" ]; then
    SCRATCH="$OUT/inspect_input"
    mkdir -p "$SCRATCH"
    for subject in "$IN"/*/; do ln -sfn "${subject%/}" "$SCRATCH/$(basename "$subject")"; done
    python3 - "$IN/sbci_local.json" "$SCRATCH/sbci_local.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
config.update(mode="inspect", approved=False)
json.dump(config, open(sys.argv[2], "w"), indent=2)
PY
    python3 run_pipeline.py --input "$SCRATCH" --output "$OUT/inspect" > "$OUT/logs/inspect.json" 2> "$OUT/logs/inspect.err"
    rc=$?
    python3 - "$OUT/inspect/local_adapter_manifest.json" <<'PY' || exit 1
import json, sys
m = json.load(open(sys.argv[1]))
plan = (m.get("workflow") or {}).get("plan") or {}
print("status", m.get("status"), "readyToRun", plan.get("readyToRun"))
for s in plan.get("stages", []):
    missing = s.get("missingCommands") or []
    print(f"  {s['id']:<22} {s.get('state')}", "MISSING: " + " ".join(missing) if missing else "")
for b in plan.get("blockers") or []:
    print("  blocker:", b)
for i in m.get("issues", []):
    print("  issue:", i.get("code"), i.get("message", "")[:300])
sys.exit(0 if plan.get("readyToRun") else 1)
PY
    exit "$rc"
fi

echo "host=$(hostname) job=${SLURM_JOB_ID:-none} pipeline=$PIPELINE stages=$STAGES started=$(date -Is)"
# The saved configuration, next to the outputs, so the run can be repeated.
mkdir -p "$OUT/config"
cp "$IN/sbci_local.json" "$OUT/config/sbci_local.json"
# The profile: a path, or one in this folder by its relative name (datasets/adni/profile.conf).
PROFILE=$(python3 -c 'import json, sys
from workflow.workflow_executor import profile_path
print(profile_path((json.load(open(sys.argv[1])).get("workflow") or {}).get("preprocessing_config")) or "")' "$IN/sbci_local.json")
[ -s "$PROFILE" ] && cp "$PROFILE" "$OUT/config/"
# The code this job runs: one hash over every file of the pipeline (not the image, not caches).
PIPELINE_VERSION="$(basename "$PIPELINE")@$(find run_pipeline.py pipeline preprocessing corrections workflow datasets data -type f ! -path '*/__pycache__/*' | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -c1-12)"
printf '{"pipeline": "%s", "pipeline_version": "%s", "container": "%s", "container_version": "%s", "stages": "%s"}\n' \
    "$PIPELINE" "$PIPELINE_VERSION" "$SIF" "$CONTAINER_VERSION" "$STAGES" > "$OUT/config/versions.json"

# Prints "<status> <stage> <state> <next pending stage>" from a run's manifest.
summary() {
    python3 - "$1" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
w = m.get("workflow") or {}
e = w.get("execution") or {}
nxt = next((s["id"] for s in (w.get("plan") or {}).get("stages", []) if s.get("state") != "complete"), "")
print(m.get("status"), e.get("stage"), e.get("state"), nxt or "-")
for i in m.get("issues", []):
    print("  issue:", i.get("code"), i.get("message", "")[:300])
PY
}

# Preprocessing stages, one per call, until the next stage is SBCI.
next=""
[ -s "$OUT/local_adapter_manifest.json" ] && next=$(summary "$OUT/local_adapter_manifest.json" | head -1 | awk '{print $4}')
for i in $(seq 1 10); do
    [ "$next" = sbci ] && break
    echo "=== preprocessing call $i $(date -Is) ==="
    python3 run_pipeline.py --input "$IN" --output "$OUT" > "$OUT/logs/call_$i.json" 2> "$OUT/logs/call_$i.err"
    rc=$?
    summary "$OUT/local_adapter_manifest.json"
    if [ "$rc" -ne 0 ]; then
        echo "STOPPED: preprocessing call $i failed (rc=$rc); see the issue above and $OUT/logs/." >&2
        exit 1
    fi
    next=$(summary "$OUT/local_adapter_manifest.json" | head -1 | awk '{print $4}')
done
[ "$next" = sbci ] || { echo "STOPPED: preprocessing did not reach the SBCI stage (next: $next)." >&2; exit 1; }
if [ "$STAGES" = preprocess ]; then
    echo "preprocessing complete; SBCI not requested (workflow.stages=preprocess). finished=$(date -Is) rc=0"
    exit 0
fi

# SBCI stage, on the completed work tree.
WORK="$OUT/files/preprocessing_work"
SUBJ=$(find "$WORK" -mindepth 1 -maxdepth 1 -type d ! -name state -printf "%f\n" | head -1)
SBCI_IN="$OUT/sbci_input"  # its sbci_local.json was written at the start
ln -sfn "$WORK/$SUBJ" "$SBCI_IN/$SUBJ"
echo "=== SBCI $(date -Is) ==="
# The SBCI stage runs on the image's SBCI Python, with that environment's commands first on PATH.
PATH="$(dirname "$SBCI_CORE_PY"):$PATH" "$SBCI_CORE_PY" \
    run_pipeline.py --input "$SBCI_IN" --output "$OUT/sbci_output" > "$OUT/logs/sbci.json" 2> "$OUT/logs/sbci.err"
rc=$?
summary "$OUT/sbci_output/local_adapter_manifest.json"
ls -la "$OUT/sbci_output/files/work/$SUBJ/dwi_pipeline/sbci_connectome/" 2>/dev/null | grep -E "\.mat$"
# Optional: with the `sbci` analysis package installed (or its source folder named in
# SBCI_ANALYSIS_PACKAGE), also write its .h5 files and QC page. The pipeline's own results are
# the .mat files listed above and need no such package.
[ -n "${SBCI_ANALYSIS_PACKAGE:-}" ] && export PYTHONPATH="$SBCI_ANALYSIS_PACKAGE${PYTHONPATH:+:$PYTHONPATH}"
if [ "$rc" -eq 0 ] && ! python3 -c 'import sbci' 2>/dev/null; then
    echo "The sbci analysis package is not installed: no .h5 files or QC page (the .mat results are complete)."
elif [ "$rc" -eq 0 ]; then
    # The sbci package's files (<subject>_sc.h5 / _fc.h5) and QC page, as the notebooks write them.
    export MPLBACKEND=Agg
    python3 -m sbci convert "$OUT/sbci_output/files/work/$SUBJ/dwi_pipeline/sbci_connectome" --subject "$SUBJ" \
        --out "$OUT/derivatives" --pipeline-version "$PIPELINE_VERSION" --container-version "$CONTAINER_VERSION" \
        ${PROFILE:+--config "$PROFILE"} > "$OUT/logs/convert.json" 2> "$OUT/logs/convert.err" || rc=$?
    H5=()
    [ -s "$OUT/derivatives/${SUBJ}_sc.h5" ] && H5+=(--sc "$OUT/derivatives/${SUBJ}_sc.h5")
    [ -s "$OUT/derivatives/${SUBJ}_fc.h5" ] && H5+=(--fc "$OUT/derivatives/${SUBJ}_fc.h5")
    python3 -m sbci qc "${H5[@]}" --run-dir "$OUT" --out "$OUT/qc" > "$OUT/logs/qc.json" 2> "$OUT/logs/qc.err" \
        || echo "QC flagged a check: see $OUT/qc/index.html"
    ls "$OUT/derivatives" "$OUT/qc/index.html" 2>/dev/null
fi
echo "finished=$(date -Is) rc=$rc"
exit "$rc"
