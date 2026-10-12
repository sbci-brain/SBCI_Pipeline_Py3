#!/bin/bash
# Python 3 / scilpy 2.x adaptation of ADNI_example/sbci_step1_process_grid.sh
#
# One-time setup: processes the fsaverage template surfaces to produce
# downsampled meshes, mapping files, grid files, and atlas ROI mappings.
#
# Reference: https://github.com/sbci-brain/SBCI_Pipeline/blob/master/ADNI_example/sbci_step1_process_grid.sh
#
# Command mappings (Python 2 → Python 3):
#   python ${SCRIPT_PATH}/X.py      → python3 ${SBCI_SCRIPTS}/X.py
#   scil_flip_surface.py            → kept as-is (scilpy CLI; rename if needed for scilpy 2.x)
#
# NOTE on scilpy 2.x renames — verify these in your conda env before running:
#   scil_flip_surface.py  →  may be  scil_surface_flip.py  in scilpy 2.x
#
# Usage:
#   export SBCI_CONFIG=/path/to/sbci_config      # sets OUTPUT_PATH, RESOLUTION, REFDIR, etc.
#   export SBCI_SCRIPTS=/path/to/SBCI_Pipeline/scripts
#   source ~/.bashrc_sbci && conda activate sbci
#   bash /path/to/SBCI_Pipeline/scripts/ADNI_example/sbci_step1_process_grid.sh

set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${WRAPPER_DIR}/../.." && pwd)"
SBCI_SCRIPTS="${SBCI_SCRIPTS:-${REPO_ROOT}/scripts}"
export PYTHONPATH="${SBCI_SCRIPTS}:${PYTHONPATH:-}"

echo "Sourcing SBCI config file"
source "${SBCI_CONFIG}"

echo "Begin processing SBCI grid: $(date)"
mkdir -p "${OUTPUT_PATH}"

if [ -z "${REFDIR+x}" ]; then
    REFDIR="${SBCI_PATH}/data/fsaverage"
fi

# convert freesurfer surfaces to vtk format
mris_convert "${REFDIR}/surf/lh.white_avg"       "${OUTPUT_PATH}/lh_white_avg.vtk"
mris_convert "${REFDIR}/surf/rh.white_avg"       "${OUTPUT_PATH}/rh_white_avg.vtk"
mris_convert "${REFDIR}/surf/lh.sphere.reg.avg"  "${OUTPUT_PATH}/lh_sphere_avg.vtk"
mris_convert "${REFDIR}/surf/rh.sphere.reg.avg"  "${OUTPUT_PATH}/rh_sphere_avg.vtk"
mris_convert "${REFDIR}/surf/lh.inflated_avg"    "${OUTPUT_PATH}/lh_inflated_avg.vtk"
mris_convert "${REFDIR}/surf/rh.inflated_avg"    "${OUTPUT_PATH}/rh_inflated_avg.vtk"

# flip from RAS to LPS (required by SET)
# scilpy 2.x: scil_flip_surface.py → scil_surface_flip
scil_surface_flip "${OUTPUT_PATH}/lh_sphere_avg.vtk"   "${OUTPUT_PATH}/lh_sphere_avg_lps.vtk"   x y -f
scil_surface_flip "${OUTPUT_PATH}/rh_sphere_avg.vtk"   "${OUTPUT_PATH}/rh_sphere_avg_lps.vtk"   x y -f
scil_surface_flip "${OUTPUT_PATH}/lh_white_avg.vtk"    "${OUTPUT_PATH}/lh_white_avg_lps.vtk"    x y -f
scil_surface_flip "${OUTPUT_PATH}/rh_white_avg.vtk"    "${OUTPUT_PATH}/rh_white_avg_lps.vtk"    x y -f
scil_surface_flip "${OUTPUT_PATH}/lh_inflated_avg.vtk" "${OUTPUT_PATH}/lh_inflated_avg_lps.vtk" x y -f
scil_surface_flip "${OUTPUT_PATH}/rh_inflated_avg.vtk" "${OUTPUT_PATH}/rh_inflated_avg_lps.vtk" x y -f

# Step1) Downsample the sphere surfaces to the given resolution
python3 "${SBCI_SCRIPTS}/downsample_ico_mesh.py" \
        --surface "${OUTPUT_PATH}/lh_sphere_avg_lps.vtk" --res "${RESOLUTION}" \
        --output  "${OUTPUT_PATH}/lh_sphere_avg_${RESOLUTION}.vtk" -f

python3 "${SBCI_SCRIPTS}/downsample_ico_mesh.py" \
        --surface "${OUTPUT_PATH}/rh_sphere_avg_lps.vtk" --res "${RESOLUTION}" \
        --output  "${OUTPUT_PATH}/rh_sphere_avg_${RESOLUTION}.vtk" -f

# Step2) Generate a discrete mapping from full resolution mesh to downsampled mesh
python3 "${SBCI_SCRIPTS}/map_surfaces.py" \
        --lh_surface_hi "${OUTPUT_PATH}/lh_sphere_avg_lps.vtk" \
        --lh_surface_lo "${OUTPUT_PATH}/lh_sphere_avg_${RESOLUTION}.vtk" \
        --rh_surface_hi "${OUTPUT_PATH}/rh_sphere_avg_lps.vtk" \
        --rh_surface_lo "${OUTPUT_PATH}/rh_sphere_avg_${RESOLUTION}.vtk" \
        --matlab_output "${OUTPUT_PATH}/mapping_avg_${RESOLUTION}.mat" \
        --output        "${OUTPUT_PATH}/mapping_avg_${RESOLUTION}.npz" -f

# Step3) Generate meshes for visualisation
python3 "${SBCI_SCRIPTS}/downsample_ico_mesh.py" \
        --surface "${OUTPUT_PATH}/lh_white_avg_lps.vtk" --res "${RESOLUTION}" \
        --output  "${OUTPUT_PATH}/lh_white_avg_${RESOLUTION}.vtk" -f

python3 "${SBCI_SCRIPTS}/downsample_ico_mesh.py" \
        --surface "${OUTPUT_PATH}/rh_white_avg_lps.vtk" --res "${RESOLUTION}" \
        --output  "${OUTPUT_PATH}/rh_white_avg_${RESOLUTION}.vtk" -f

python3 "${SBCI_SCRIPTS}/downsample_ico_mesh.py" \
        --surface "${OUTPUT_PATH}/lh_inflated_avg_lps.vtk" --res "${RESOLUTION}" \
        --output  "${OUTPUT_PATH}/lh_inflated_avg_${RESOLUTION}.vtk" -f

python3 "${SBCI_SCRIPTS}/downsample_ico_mesh.py" \
        --surface "${OUTPUT_PATH}/rh_inflated_avg_lps.vtk" --res "${RESOLUTION}" \
        --output  "${OUTPUT_PATH}/rh_inflated_avg_${RESOLUTION}.vtk" -f

# Step4) Generate the grid to be used in concon
python3 "${SBCI_SCRIPTS}/concon/vtk_to_m.py" \
        --surface "${OUTPUT_PATH}/lh_sphere_avg_${RESOLUTION}.vtk" \
        --output  "${OUTPUT_PATH}/tmp_lh_grid.m" -f

python3 "${SBCI_SCRIPTS}/concon/vtk_to_m.py" \
        --surface "${OUTPUT_PATH}/rh_sphere_avg_${RESOLUTION}.vtk" \
        --output  "${OUTPUT_PATH}/tmp_rh_grid.m" -f

python3 "${SBCI_SCRIPTS}/concon/normalise_m.py" \
        --surface "${OUTPUT_PATH}/tmp_lh_grid.m" \
        --output  "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.m" -f

python3 "${SBCI_SCRIPTS}/concon/normalise_m.py" \
        --surface "${OUTPUT_PATH}/tmp_rh_grid.m" \
        --output  "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.m" -f

# concon requires both vertices and faces.  VTK 9 writes polygon connectivity
# differently from VTK 8, and older converters could silently create a
# vertex-only .m file that made c3_main emit an empty raw kernel.
for GRID in \
    "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.m" \
    "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.m"
do
    VERTEX_COUNT=$(grep -c '^Vertex ' "${GRID}" || true)
    FACE_COUNT=$(grep -c '^Face ' "${GRID}" || true)
    echo "Validated concon grid ${GRID}: ${VERTEX_COUNT} vertices, ${FACE_COUNT} faces"
    if [ "${VERTEX_COUNT}" -eq 0 ] || [ "${FACE_COUNT}" -eq 0 ]; then
        echo "ERROR: Invalid concon grid ${GRID}: vertices=${VERTEX_COUNT}, faces=${FACE_COUNT}" >&2
        exit 1
    fi
done

python3 "${SBCI_SCRIPTS}/normalise_vtk.py" \
        --surface "${OUTPUT_PATH}/lh_sphere_avg_${RESOLUTION}.vtk" \
        --output  "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.vtk" -f

python3 "${SBCI_SCRIPTS}/normalise_vtk.py" \
        --surface "${OUTPUT_PATH}/rh_sphere_avg_${RESOLUTION}.vtk" \
        --output  "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.vtk" -f

python3 "${SBCI_SCRIPTS}/normalise_vtk.py" \
        --surface "${OUTPUT_PATH}/lh_sphere_avg_lps.vtk" \
        --output  "${OUTPUT_PATH}/lh_sphere_avg_norm.vtk" -f

python3 "${SBCI_SCRIPTS}/normalise_vtk.py" \
        --surface "${OUTPUT_PATH}/rh_sphere_avg_lps.vtk" \
        --output  "${OUTPUT_PATH}/rh_sphere_avg_norm.vtk" -f

rm -f "${OUTPUT_PATH}/tmp_lh_grid.m" "${OUTPUT_PATH}/tmp_rh_grid.m"

# Step5) Generate coordinates and adjacency matrix files for grid
python3 "${SBCI_SCRIPTS}/get_coords.py" \
        --lh_surface "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.vtk" \
        --rh_surface "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.vtk" \
        --output     "${OUTPUT_PATH}/grid_coords_${RESOLUTION}.mat" -f

python3 "${SBCI_SCRIPTS}/get_coords.py" \
        --lh_surface "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.vtk" \
        --rh_surface "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.vtk" \
        --output     "${OUTPUT_PATH}/grid_coords_${RESOLUTION}.npz" -f

python3 "${SBCI_SCRIPTS}/get_adjacency_matrix.py" \
        --lh_surface "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.vtk" \
        --rh_surface "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.vtk" \
        --output     "${OUTPUT_PATH}/adjacency_${RESOLUTION}.mat" -f

# Step6) Generate triangulations for the meshes
python3 "${SBCI_SCRIPTS}/generate_grid_triangulation.py" \
        --lh_surface "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.vtk" \
        --rh_surface "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.vtk" \
        --output     "${OUTPUT_PATH}/template_sphere_grid_${RESOLUTION}.mat" -f

python3 "${SBCI_SCRIPTS}/generate_grid_triangulation.py" \
        --lh_surface "${OUTPUT_PATH}/lh_white_avg_${RESOLUTION}.vtk" \
        --rh_surface "${OUTPUT_PATH}/rh_white_avg_${RESOLUTION}.vtk" \
        --output     "${OUTPUT_PATH}/template_white_grid_${RESOLUTION}.mat" -f

python3 "${SBCI_SCRIPTS}/generate_grid_triangulation.py" \
        --lh_surface "${OUTPUT_PATH}/lh_inflated_avg_${RESOLUTION}.vtk" \
        --rh_surface "${OUTPUT_PATH}/rh_inflated_avg_${RESOLUTION}.vtk" \
        --output     "${OUTPUT_PATH}/template_inflated_grid_${RESOLUTION}.mat" -f

########################################
# LOOP THROUGH AVAILABLE PARCELLATIONS #
########################################

GROUP_PARCELLATIONS=(${REFDIR}/label/lh.*.annot)

for PARCELLATION_FILE in "${GROUP_PARCELLATIONS[@]}"; do

    PARCELLATION="${PARCELLATION_FILE##*lh.}"
    PARCELLATION="${PARCELLATION%*.annot}"

    if [ -f "${REFDIR}/label/rh.${PARCELLATION}.annot" ]; then

        echo "Processing Parcellation: ${PARCELLATION}"

        python3 "${SBCI_SCRIPTS}/group_roi_vertices.py" \
                --lh_annot "${REFDIR}/label/lh.${PARCELLATION}.annot" \
                --rh_annot "${REFDIR}/label/rh.${PARCELLATION}.annot" \
                --mesh     "${OUTPUT_PATH}/mapping_avg_${RESOLUTION}.npz" \
                --output   "${OUTPUT_PATH}/${PARCELLATION}_avg_roi_${RESOLUTION}.npz" \
                --matlab   "${OUTPUT_PATH}/${PARCELLATION}_avg_roi_${RESOLUTION}.mat" -f
    fi
done

echo "Finished processing SBCI grid: $(date)"
