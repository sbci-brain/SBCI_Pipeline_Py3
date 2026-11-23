# Sourced by the job scripts (and by the image's own run script). Every tool the pipeline runs
# is in the sbci-full image; MATLAB, for FSFast, is the one thing taken from this machine.

# Prints the container runtime the jobs use: SBCI_CONTAINER (apptainer or docker) when set, else
# Apptainer where it is on PATH (clusters), else Docker.
container_runtime() {
    case ${SBCI_CONTAINER:-} in
        apptainer|docker)
            command -v "$SBCI_CONTAINER" >/dev/null 2>&1 || { echo "ERROR: SBCI_CONTAINER=$SBCI_CONTAINER, but $SBCI_CONTAINER is not on PATH." >&2; return 2; }
            echo "$SBCI_CONTAINER" ;;
        "")
            if command -v apptainer >/dev/null 2>&1; then echo apptainer
            elif command -v docker >/dev/null 2>&1; then echo docker
            else echo "ERROR: the pipeline runs in the sbci-full image, which needs Apptainer or Docker on PATH; on a cluster, load Apptainer first (e.g. module load apptainer)." >&2; return 2
            fi ;;
        *) echo "ERROR: SBCI_CONTAINER=$SBCI_CONTAINER is not apptainer or docker." >&2; return 2 ;;
    esac
}

# in_image JOB PATH... -- ARG...: outside the image, runs workflow/JOB ARG... again inside it and
# does not return. The image sees each PATH (or, for one not made yet, the folder it goes in), this folder
# and this machine's MATLAB (`matlab` on PATH), each at its own path. Inside the image it returns.
# With Apptainer the image is SBCI_FULL_IMAGE, else sbci-full.sif in this folder; with Docker it
# is SBCI_DOCKER_IMAGE, else the pinned image (image_pin.sh). workflow/get_image.sbatch fetches either.
in_image() {
    [ -n "${SBCI_IMAGE:-}" ] && return 0
    local job=$1 runtime image paths=() p matlab_root="" binds mounts=() pass=() name
    shift
    while [ $# -gt 0 ] && [ "$1" != -- ]; do paths+=("$1"); shift; done
    shift
    runtime=$(container_runtime) || exit 2
    if [ "$runtime" = docker ]; then
        source "$PIPELINE/workflow/image_pin.sh"
        image=${SBCI_DOCKER_IMAGE:-$REPOSITORY@$DIGEST}
        docker image inspect "$image" >/dev/null 2>&1 || { echo "ERROR: Docker has no image $image. Fetch it once with: bash $PIPELINE/workflow/get_image.sbatch (or name another in SBCI_DOCKER_IMAGE)." >&2; exit 2; }
    else
        image=${SBCI_FULL_IMAGE:-$PIPELINE/sbci-full.sif}
        [ -s "$image" ] || { echo "ERROR: no sbci-full image at $image. Fetch it once with: bash $PIPELINE/workflow/get_image.sbatch (or set SBCI_FULL_IMAGE to a copy)." >&2; exit 2; }
    fi
    # The image runs Linux programs, so only a Linux MATLAB can be bound into it.
    if [ "$(uname -s)" = Linux ]; then
        matlab_root=$(command -v matlab >/dev/null 2>&1 && dirname "$(dirname "$(readlink -f "$(command -v matlab)")")")
        [ -n "$matlab_root" ] || echo "WARNING: no matlab on PATH; FSFast (the fMRI stage) needs it, so the preflight will report it missing." >&2
    else
        echo "WARNING: this machine is $(uname -s): FSFast (the fMRI stage) needs a Linux MATLAB, which cannot come from here, so the preflight will report matlab missing and no subject can finish on this machine." >&2
    fi
    # ponytail: system folders are never bound (they would hide the image's own).
    binds=$(for p in "$PIPELINE" ${matlab_root:+"$matlab_root"} ${SBCI_IMAGE_ENV:+"$SBCI_IMAGE_ENV"} ${paths[@]+"${paths[@]}"}; do
        while [ ! -e "$p" ]; do p=$(dirname "$p"); done
        readlink -f "$p"
    done | grep -vxE '/|/(bin|boot|dev|etc|lib|lib64|opt|proc|sbin|sys|usr|var)(/(bin|lib|lib64|local|local/lib|sbin|share))?' | sort -u | paste -sd, -)
    echo "running in $image ($runtime)${matlab_root:+ with MATLAB from $matlab_root}"
    if [ "$runtime" = docker ]; then
        # Docker passes on no environment and runs as root unless told: the job runs as this
        # user, so its files are this user's, and gets the settings the job scripts read.
        while IFS= read -r -d , p; do mounts+=(-v "$p:$p"); done <<< "$binds,"
        for name in SBCI_READONLY_ROOTS SBCI_ANALYSIS_PACKAGE SLURM_JOB_ID FREESURFER_THREADS MLM_LICENSE_FILE LM_LICENSE_FILE; do
            [ -z "${!name:-}" ] || pass+=(-e "$name")
        done
        exec docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)" -e HOME=/tmp \
            "${mounts[@]}" ${pass[@]+"${pass[@]}"} -e "SBCI_PIPELINE=$PIPELINE" -e "SBCI_IMAGE_FILE=$image" \
            ${SBCI_IMAGE_ENV:+-e "SBCI_IMAGE_ENV=$(readlink -f "$SBCI_IMAGE_ENV")"} \
            ${matlab_root:+-e "PREPEND_PATH=$matlab_root/bin"} "$image" bash "$PIPELINE/workflow/$job" "$@"
    fi
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
