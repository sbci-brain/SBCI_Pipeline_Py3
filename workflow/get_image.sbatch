#!/bin/bash -l
#SBATCH --job-name=sbci-image
#SBATCH --cpus-per-task=4
#SBATCH --mem=16g
#SBATCH --time=04:00:00
# Fetches sbci-full, the image every job runs in, once per clone: to sbci-full.sif in this
# pipeline's folder, or the path given. Needs Apptainer and internet: run it on a machine (or
# cluster login node) that has both, or through submit.sh where compute nodes reach the
# internet. About 11 GB to download, 2-3 hours packing it on 4 cores, and room for about 3x
# that while it builds (beside the image, not /tmp).
#   bash <pipeline>/workflow/get_image.sbatch [IMAGE.sif]
# Pinned by digest, so every clone of this release runs on the same image.
set -euo pipefail
# Every job runs in a Linux container: on macOS or plain Windows there is no Apptainer (use a
# Linux machine, a cluster, or WSL).
[ "$(uname -s)" = Linux ] || { echo "ERROR: the SBCI pipeline runs on Linux (or WSL) with Apptainer; this machine is $(uname -s)." >&2; exit 2; }
command -v apptainer > /dev/null || { echo "ERROR: apptainer is not on PATH (on a cluster: module load apptainer)." >&2; exit 2; }
REPOSITORY=sdszhauk/sbci-image
TAG=20261123
DIGEST=sha256:d5b6f2c64867238e0704cd408ad876460aeafe8610860391cc86c9d661feeb0a
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PIPELINE=${SBCI_PIPELINE:-$here}
DEST=$(readlink -m "${1:-$PIPELINE/sbci-full.sif}")
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
# The image contract (README.md, "The image"): its variables, and the tools on PATH.
apptainer exec "$DEST" bash -c 'for v in SBCI_IMAGE SBCI_ENV SBCI_CORE_PY SBCI_TEMPLATE_DIR FSLDIR FREESURFER_HOME; do test -n "${!v:-}" || { echo "not set: $v" >&2; exit 1; }; done
    test -x "$SBCI_CORE_PY" && test -x "$SBCI_ENV/bin/python3" && test -s "$SBCI_TEMPLATE_DIR/t1/t1_template.nii.gz" || { echo "a variable names a missing file" >&2; exit 1; }
    for c in recon-all antsRegistration dwi2fod eddy_cpu synb0; do command -v "$c" > /dev/null || { echo "not on PATH: $c" >&2; exit 1; }; done' \
    || { echo "ERROR: $DEST ($REPOSITORY:$TAG) lacks the pipeline's environment or tools." >&2; exit 1; }
ls -l "$DEST"
echo "sbci-full ready: $DEST"
