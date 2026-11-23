#!/bin/bash
# Python 3 / scilpy 2.x adaptation of ADNI_example/sbci_step5_structural.sh
#
# Per-subject: concatenates SET runs, snaps fibers, computes structural
# connectivity matrix using kernel smoothing via concon.
#
# Reference: https://github.com/sbci-brain/SBCI_Pipeline/blob/master/ADNI_example/sbci_step5_structural.sh
#
# Command mappings (Python 2 → Python 3):
#   scil_concatenate_surfaces_intersections.py  → python3 ${SBCI_SCRIPTS}/concatenate_intersections.py
#   python ${SCRIPT_PATH}/snap_fibers.py        → python3 ${SBCI_SCRIPTS}/snap_fibers.py
#   python ${SCRIPT_PATH}/concon/intersections_to_sphere.py  → python3 ${SBCI_SCRIPTS}/concon/intersections_to_sphere.py
#   ${CONCON_PATH}/c3_main                      → ${CONCON_PATH}/c3_main  (unchanged — compiled binary)
#   python ${SCRIPT_PATH}/concon/convert_raw.py → python3 ${SBCI_SCRIPTS}/concon/convert_raw.py
#   python ${SCRIPT_PATH}/calculate_subcortical_sc.py → python3 ${SBCI_SCRIPTS}/calculate_subcortical_sc.py
#   python ${SCRIPT_PATH}/intersections_to_sphere.py  → python3 ${SBCI_SCRIPTS}/intersections_to_sphere.py
#   python ${SCRIPT_PATH}/get_fibers_barycentric.py   → python3 ${SBCI_SCRIPTS}/get_fibers_barycentric.py
#
# Usage:
#   export SBCI_CONFIG=/path/to/sbci_config
#   cd /path/to/subject_dir
#   bash <pipeline>/pipeline/sbci_step5_structural.sh

set -euo pipefail

# The Python scripts sit beside this one; the reference data in ../data.
WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${WRAPPER_DIR}/.." && pwd)"
SBCI_SCRIPTS="${SBCI_SCRIPTS:-${WRAPPER_DIR}}"
export PYTHONPATH="${SBCI_SCRIPTS}:${PYTHONPATH:-}"

echo "Sourcing SBCI config file"
source "${SBCI_CONFIG}"

OUTPUTDIR=dwi_pipeline/sbci_connectome
SETDIR=dwi_pipeline/set

if [ -z "${CONCON_PATH+x}" ]; then
    CONCON_PATH="${REPO_ROOT}/data/concon"
fi

echo "Begin processing SBCI structural: $(date)"

##############################
#    CONCATENATE SET DATA    #
##############################

INTERSECTIONS=""

for ((RUN = 1; RUN <= N_RUNS; RUN++)); do
    INTERSECTIONS="${INTERSECTIONS} ${SETDIR}/streamline/intersections_random_loop${RUN}_filtered.npz"
done

# Python 3 replacement for scil_concatenate_surfaces_intersections.py
python3 "${SBCI_SCRIPTS}/concatenate_intersections.py" \
        ${INTERSECTIONS} \
        --output_intersections "${SETDIR}/streamline/set_filtered_intersections.npz" -f

################################################################
##            GROUP SPACE STRUCTURAL CONNECTIVITY             ##
################################################################

python3 "${SBCI_SCRIPTS}/snap_fibers.py" \
        --surfaces      "${SETDIR}/out_surf/surfaces.vtk" \
        --surface_map   "${SETDIR}/preprocess/surfaces_id.npy" \
        --intersections "${SETDIR}/streamline/set_filtered_intersections.npz" \
        --output        "${OUTPUTDIR}/snapped_fibers.npz" -f

python3 "${SBCI_SCRIPTS}/concon/intersections_to_sphere.py" \
        --lh_surface    "${OUTPUTDIR}/lh_sphere_reg_lps.vtk" \
        --rh_surface    "${OUTPUTDIR}/rh_sphere_reg_lps.vtk" \
        --intersections "${OUTPUTDIR}/snapped_fibers.npz" \
        --output        "${OUTPUTDIR}/subject_xing_sphere_avg_coords.tsv" -f

"${CONCON_PATH}/c3_main" \
    Compute_Kernel \
    --subj subject \
    --sigma "${BANDWIDTH}" \
    --epsilon 0.001 \
    --final_thold 0.000000001 \
    --OPT_VAL_exp_num_kern_samps 6 \
    --OPT_VAL_exp_num_harm_samps 5 \
    --OPT_VAL_num_harm 33 \
    --LOAD_xing_path "${OUTPUTDIR}/" \
    --LOAD_xing_postfix "_xing_sphere_avg_coords.tsv" \
    --LOAD_kernel_path "" \
    --LOAD_kernel_postfix "" \
    --LOAD_mask_file MASK \
    --SAVE_Compute_Kernel_prefix "${OUTPUTDIR}/" \
    --SAVE_Compute_Kernel_postfix "_avg_${BANDWIDTH}_${RESOLUTION}.raw" \
    --LOAD_grid_file "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.m" \
    --LOAD_rh_grid_file "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.m"

RAW_KERNEL="${OUTPUTDIR}/subject_avg_${BANDWIDTH}_${RESOLUTION}.raw"
RAW_BYTES=$(stat -c '%s' "${RAW_KERNEL}" 2>/dev/null || echo 0)
if [ "${RAW_BYTES}" -le 4 ]; then
    LH_VERTICES=$(grep -c '^Vertex ' "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.m" || true)
    LH_FACES=$(grep -c '^Face ' "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.m" || true)
    RH_VERTICES=$(grep -c '^Vertex ' "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.m" || true)
    RH_FACES=$(grep -c '^Face ' "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.m" || true)
    echo "ERROR: concon wrote an empty kernel (${RAW_BYTES} bytes): ${RAW_KERNEL}" >&2
    echo "Grid counts: LH vertices=${LH_VERTICES} faces=${LH_FACES}; RH vertices=${RH_VERTICES} faces=${RH_FACES}" >&2
    echo "Regenerate the shared grid with the current vtk_to_m.py before retrying step 5." >&2
    exit 1
fi

python3 "${SBCI_SCRIPTS}/concon/convert_raw.py" \
        --input         "${RAW_KERNEL}" \
        --intersections "${OUTPUTDIR}/snapped_fibers.npz" \
        --mesh          "${OUTPUT_PATH}/mapping_avg_${RESOLUTION}.npz" \
        --output        "${OUTPUTDIR}/smoothed_sc_avg_${BANDWIDTH}_${RESOLUTION}" -f

python3 "${SBCI_SCRIPTS}/calculate_subcortical_sc.py" \
        --intersections "${OUTPUTDIR}/snapped_fibers.npz" \
        --grid          "${OUTPUT_PATH}/grid_coords_${RESOLUTION}.npz" \
        --coordinates   "${OUTPUTDIR}/subject_coords.npz" \
        --bandwidth     "${BANDWIDTH}" \
        --output        "${OUTPUTDIR}/sub_sc_avg_${BANDWIDTH}_${RESOLUTION}.mat" -f

python3 "${SBCI_SCRIPTS}/intersections_to_sphere.py" \
        --lh_reg_surface  "${OUTPUTDIR}/lh_sphere_reg_norm.vtk" \
        --rh_reg_surface  "${OUTPUTDIR}/rh_sphere_reg_norm.vtk" \
        --set_surfaces    "${SETDIR}/out_surf/surfaces.vtk" \
        --set_surface_map "${SETDIR}/preprocess/surfaces_id.npy" \
        --intersections   "${SETDIR}/streamline/set_filtered_intersections.npz" \
        --output          "${OUTPUTDIR}/sphere_intersections.npz" -f

python3 "${SBCI_SCRIPTS}/get_fibers_barycentric.py" \
        --lh_surface    "${OUTPUT_PATH}/lh_grid_avg_${RESOLUTION}.vtk" \
        --rh_surface    "${OUTPUT_PATH}/rh_grid_avg_${RESOLUTION}.vtk" \
        --intersections "${OUTPUTDIR}/sphere_intersections.npz" \
        --output        "${OUTPUTDIR}/mesh_intersections_${RESOLUTION}.mat" -f

echo "Finished processing SBCI structural: $(date)"
