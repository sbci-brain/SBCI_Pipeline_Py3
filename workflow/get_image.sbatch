#!/bin/bash -l
#SBATCH --job-name=sbci-image
#SBATCH --cpus-per-task=4
#SBATCH --mem=16g
#SBATCH --time=04:00:00
# Fetches sbci-full, the image every job runs in, once. Needs internet: run it on a machine (or
# cluster login node) that has it, or through submit.sh where compute nodes reach the internet.
#   bash <pipeline>/workflow/get_image.sbatch [IMAGE.sif]
# With Apptainer (clusters, Linux, WSL): to sbci-full.sif in this pipeline's folder, or the path
# given. About 11 GB to download, 2-3 hours packing it on 4 cores, and room for about 3x that
# while it builds (beside the image, not /tmp).
# With Docker (no Apptainer on PATH, or SBCI_CONTAINER=docker): `docker pull`, about 11 GB to
# download and 40 GB unpacked; no file is written here.
# Pinned by digest (image_pin.sh), so every clone of this release runs on the same image.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PIPELINE=${SBCI_PIPELINE:-$here}
source "$PIPELINE/workflow/image_pin.sh"
source "$PIPELINE/workflow/runtime_env.sh"
runtime=$(container_runtime) || exit 2
# The image contract (README.md, "The image"): its variables, and the tools on PATH.
contract='for v in SBCI_IMAGE SBCI_ENV SBCI_CORE_PY SBCI_TEMPLATE_DIR FSLDIR FREESURFER_HOME; do test -n "${!v:-}" || { echo "not set: $v" >&2; exit 1; }; done
    test -x "$SBCI_CORE_PY" && test -x "$SBCI_ENV/bin/python3" && test -s "$SBCI_TEMPLATE_DIR/t1/t1_template.nii.gz" || { echo "a variable names a missing file" >&2; exit 1; }
    for c in recon-all antsRegistration dwi2fod eddy_cpu synb0; do command -v "$c" > /dev/null || { echo "not on PATH: $c" >&2; exit 1; }; done'

if [ "$runtime" = docker ]; then
    image=${SBCI_DOCKER_IMAGE:-$REPOSITORY@$DIGEST}
    docker pull --platform linux/amd64 "$image"
    docker run --rm --platform linux/amd64 "$image" bash -c "$contract" \
        || { echo "ERROR: $image lacks the pipeline's environment or tools." >&2; exit 1; }
    echo "sbci-full ready in Docker: $image"
    exit 0
fi

mkdir -p "$(dirname "${1:-$PIPELINE/sbci-full.sif}")"
DEST=$(cd "$(dirname "${1:-$PIPELINE/sbci-full.sif}")" && pwd -P)/$(basename "${1:-sbci-full.sif}")
T=$(dirname "$DEST")/.sbci-image-tmp
export APPTAINER_TMPDIR=$T APPTAINER_CACHEDIR=$T/cache
mkdir -p "$APPTAINER_CACHEDIR"
# Apptainer's Docker conversion replaces the image's two standard environment files with its
# own. An image made for this pipeline keeps its environment in a file of another name, which
# the conversion leaves alone. For an image that predates that, SBCI_IMAGE_ENV names a file of
# `export NAME=value` lines, which go in here.
{
    printf 'Bootstrap: docker\nFrom: %s@%s\n\n%%environment\n    export LANG="${LANG:-C.UTF-8}"\n' "$REPOSITORY" "$DIGEST"
    [ -z "${SBCI_IMAGE_ENV:-}" ] || sed 's/^/    /' "$SBCI_IMAGE_ENV"
} > "$T/sbci-full.def"
# mksquashfs otherwise sizes its memory from the whole machine, not the job (OOM-killed at 32 GB).
apptainer build --force --mksquashfs-args "-mem 2G -processors 4" "$DEST" "$T/sbci-full.def"
rm -rf "$T"
apptainer exec "$DEST" bash -c "$contract" \
    || { echo "ERROR: $DEST ($REPOSITORY:$TAG) lacks the pipeline's environment or tools." >&2; exit 1; }
ls -l "$DEST"
echo "sbci-full ready: $DEST"
