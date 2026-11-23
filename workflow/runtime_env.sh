# Sourced by the job scripts (and by the image's own run script). Every tool the pipeline runs
# is in the sbci-full image; MATLAB, for FSFast, is the one thing taken from this machine.
#
# in_image JOB PATH... -- ARG...: outside the image, runs workflow/JOB ARG... again inside it and
# does not return. The image sees each PATH (or, for one not made yet, the folder it goes in), this folder
# and this machine's MATLAB (`matlab` on PATH), each at its own path. Inside the image it returns.
# The image is SBCI_FULL_IMAGE, else sbci-full.sif in this folder (workflow/get_image.sbatch fetches it).
in_image() {
    [ -n "${SBCI_IMAGE:-}" ] && return 0
    local job=$1 image=${SBCI_FULL_IMAGE:-$PIPELINE/sbci-full.sif} paths=() p matlab_root binds
    shift
    while [ $# -gt 0 ] && [ "$1" != -- ]; do paths+=("$1"); shift; done
    shift
    command -v apptainer >/dev/null 2>&1 || { echo "ERROR: the pipeline runs in the sbci-full image, which needs Apptainer on Linux (or WSL); on a cluster, load it first (e.g. module load apptainer)." >&2; exit 2; }
    [ -s "$image" ] || { echo "ERROR: no sbci-full image at $image. Fetch it once with: bash $PIPELINE/workflow/get_image.sbatch (or set SBCI_FULL_IMAGE to a copy)." >&2; exit 2; }
    matlab_root=$(command -v matlab >/dev/null 2>&1 && dirname "$(dirname "$(readlink -f "$(command -v matlab)")")")
    [ -n "$matlab_root" ] || echo "WARNING: no matlab on PATH; FSFast (the fMRI stage) needs it, so the preflight will report it missing." >&2
    # ponytail: system folders are never bound (they would hide the image's own).
    binds=$(for p in "$PIPELINE" ${matlab_root:+"$matlab_root"} ${SBCI_IMAGE_ENV:+"$SBCI_IMAGE_ENV"} ${paths[@]+"${paths[@]}"}; do
        while [ ! -e "$p" ]; do p=$(dirname "$p"); done
        readlink -f "$p"
    done | grep -vxE '/|/(bin|boot|dev|etc|lib|lib64|opt|proc|sbin|sys|usr|var)(/(bin|lib|lib64|local|local/lib|sbin|share))?' | sort -u | paste -sd, -)
    echo "running in $image${matlab_root:+ with MATLAB from $matlab_root}"
    exec apptainer exec --bind "$binds" --env "SBCI_PIPELINE=$PIPELINE" --env "SBCI_IMAGE_FILE=$image" \
        ${SBCI_IMAGE_ENV:+--env "SBCI_IMAGE_ENV=$(readlink -f "$SBCI_IMAGE_ENV")"} \
        ${matlab_root:+--env "PREPEND_PATH=$matlab_root/bin"} "$image" bash "$PIPELINE/workflow/$job" "$@"
}

# Inside the image. The image names its parts through variables (README.md, "The image"), so
# no tool path is written in this repository:
#   SBCI_ENV           the preprocessing Python environment
#   SBCI_CORE_PY       the Python that runs the SBCI stage
#   SBCI_TEMPLATE_DIR  the MNI 152 template (t1/t1_template.nii.gz)
# FSL, ANTs, MRtrix3, FreeSurfer and `synb0` are on its PATH (FSLDIR, ANTSPATH, FREESURFER_HOME).
# An image built before these variables existed gets them from the file SBCI_IMAGE_ENV names;
# PATH stays the image's own (it holds this machine's MATLAB).
image_environment() {
    local path=$PATH name missing=""
    if [ -n "${SBCI_IMAGE_ENV:-}" ]; then
        . "$SBCI_IMAGE_ENV" || { echo "ERROR: cannot read SBCI_IMAGE_ENV=$SBCI_IMAGE_ENV." >&2; exit 2; }
        PATH=$path
    fi
    for name in SBCI_ENV SBCI_CORE_PY SBCI_TEMPLATE_DIR; do
        [ -n "${!name:-}" ] || missing="$missing $name"
    done
    [ -z "$missing" ] || { echo "ERROR: the image ${SBCI_IMAGE:-} does not set$missing. Use the image workflow/get_image.sbatch fetches, or name a file that sets them in SBCI_IMAGE_ENV." >&2; exit 2; }
    export SBCI_ENV SBCI_CORE_PY SBCI_TEMPLATE_DIR
    # The preprocessing environment first on PATH.
    export PATH="$SBCI_ENV/bin:$PATH"
    # Same numerical kernels on every node type: on AVX-512 nodes numpy and OpenBLAS otherwise take
    # AVX-512 code paths, and the tensor and fODF fits differ from other nodes in the last digits
    # (measured 2026-10-31 on four CPU types; no slower). The Haswell kernel needs AVX2,
    # so a CPU without it keeps the defaults.
    if grep -qw avx2 /proc/cpuinfo 2>/dev/null; then
        export OPENBLAS_CORETYPE=Haswell
        export NPY_DISABLE_CPU_FEATURES="AVX512F AVX512CD AVX512_SKX AVX512_CLX AVX512_CNL AVX512_ICL AVX512_SPR"
    fi
}
