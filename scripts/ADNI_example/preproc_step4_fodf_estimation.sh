#!/bin/bash

# This script is for two things:
# 1. segment t1 image
# 2. reconstruct fodf for tracking

# author: Zhengwu Zhang
# date: Mar. 23, 2017

# Updated  for SBCI by Martin Cole

# Python 3 port of the legacy ADNI_example script. Only the scilpy commands
# changed (scilpy 1.x -> 2.x); the FSL call and all parameters are unchanged.
#   scil_compute_ssst_frf.py              -> scil_frf_ssst
#       --fa -> --fa_thresh, --min_fa -> --min_fa_thresh, --roi_radius -> --roi_radii
#   scil_compute_fodf.py                  -> scil_fodf_ssst
#       out fODF is now a positional; --force_b0_threshold -> --skip_b0_check; and
#       scilpy 2.x no longer emits peaks here, so --peaks/--peak_indices moved to
#       scil_fodf_metrics below (same files, same names).
#   scil_compute_fodf_max_in_ventricles.py -> scil_fodf_max_in_ventricles
#   scil_compute_fodf_metrics.py          -> scil_fodf_metrics
#       threshold positional -> --at, --afd -> --afd_max
# bc is not installed on all Longleaf nodes; awk computes the same 2x threshold.
# SH_ORDER comes from sbci_config or the environment (the CyberNeuro executor exports
# the value it checked against the direction count); unset means 6 for a single shell
# (the original) and 8 for multi-shell data.

[ -n "${SBCI_CONFIG:-}" ] && source "${SBCI_CONFIG}"

echo "Begin FODF preprocessing: $(date)"

#########################################################
####### t1 preprocessing - get ready for tracking #######
#########################################################

cd structure

# segment
fast -t 1 -n 3 -H 0.1 -I 4 -l 20.0 -g -o t1_brain.nii.gz t1_warped.nii.gz
mv -f t1_brain_seg_2.nii.gz mask_wm.nii.gz
mv -f t1_brain_seg_1.nii.gz mask_gm.nii.gz
mv -f t1_brain_seg_0.nii.gz mask_csf.nii.gz
mv -f t1_brain_pve_2.nii.gz map_wm.nii.gz
mv -f t1_brain_pve_1.nii.gz map_gm.nii.gz
mv -f t1_brain_pve_0.nii.gz map_csf.nii.gz

cd ..

##########################################################
####### dwi preprocessing - get ready for tracking #######
##########################################################

# processe dMRI image
cd diffusion

# The fODF model follows the shells SHELL_POLICY left in fodf.bval (step 2):
#   one non-zero shell -> single-shell single-tissue CSD, unchanged; SH order default 6
#   several            -> multi-shell multi-tissue CSD using the FAST masks above;
#                         SH order default 8. Only the WM fODF is written, to the same
#                         fodf/fodf.nii.gz that tracking reads.
# FODF_MODEL=ssst (sbci_config) instead fits single-shell CSD to however many shells
# are given: the lab's historical ABCD/HCP recipe, for results poolable with those
# databases. The default, auto, is the routing above.
N_FODF_SHELLS=$(tr -s '[:space:]' '\n' < fodf.bval | grep -v '^$' | \
    awk '{s = int($1/100+0.5)*100; if (s > 0) print s}' | sort -nu | grep -c .)

case "${FODF_MODEL:-auto}" in
    auto|ssst) ;;
    *) echo "ERROR: FODF_MODEL=${FODF_MODEL} is not auto or ssst."; exit 1 ;;
esac

mkdir -p fodf

if [ "$N_FODF_SHELLS" -gt 1 ] && [ "${FODF_MODEL:-auto}" != ssst ]; then
    # scilpy groups b-values into shells within --tolerance (default 20). Real shells
    # spread wider (HCP: b=1485-1510, 2980-3010) and would split, so use the same
    # +-50 window that defines a shell in step 2. The FRF's tissue-selecting DTI uses
    # only shells <= --dti_bval_limit (default 1200); with no shell that low
    # (HCP-Lifespan: 1500/3000) it would see b0s alone, so lift it to the lowest shell.
    LOWEST_SHELL=$(tr -s '[:space:]' '\n' < fodf.bval | grep -v '^$' | \
        awk '{s = int($1/100+0.5)*100; if (s > 0) print s}' | sort -n | head -1)
    DTI_BVAL_LIMIT=1200
    if [ "$LOWEST_SHELL" -gt 1150 ]; then
        DTI_BVAL_LIMIT=$(( LOWEST_SHELL + 50 ))
    fi

    scil_frf_msmt dwi_fodf.nii.gz fodf.bval fodf.bvec frf_wm.txt frf_gm.txt frf_csf.txt \
        --mask b0_mask_resampled.nii.gz --mask_wm ../structure/mask_wm.nii.gz \
        --mask_gm ../structure/mask_gm.nii.gz --mask_csf ../structure/mask_csf.nii.gz \
        --roi_radii 20 --tolerance 50 --dti_bval_limit ${DTI_BVAL_LIMIT} -f

    # MSMT solves a constrained QP per voxel: on one core an HCP subject takes hours.
    # Voxels are fitted independently, so the process count does not change the result.
    # Count the cores this job may use (CPU affinity = the Slurm allocation). Plain nproc
    # would honour OMP_NUM_THREADS, which Longleaf sets to 1 in every batch job.
    FODF_PROCESSES=$(env -u OMP_NUM_THREADS -u OMP_THREAD_LIMIT nproc)
    scil_fodf_msmt dwi_fodf.nii.gz fodf.bval fodf.bvec frf_wm.txt frf_gm.txt frf_csf.txt \
        --sh_order ${SH_ORDER:-8} --skip_b0_check --mask b0_mask_resampled.nii.gz \
        --tolerance 50 --processes ${FODF_PROCESSES} --not_all --wm_out_fODF fodf/fodf.nii.gz -f
else
# compute FRF
scil_frf_ssst dwi_fodf.nii.gz fodf.bval fodf.bvec frf.txt \
	--mask b0_mask_resampled.nii.gz \
        --fa_thresh 0.7 --min_fa_thresh 0.5 --min_nvox 300 --roi_radii 20 -f

# compute fodf and fodf metrics
scil_fodf_ssst dwi_fodf.nii.gz fodf.bval fodf.bvec frf.txt fodf/fodf.nii.gz \
 --sh_order ${SH_ORDER:-6} --skip_b0_check --mask b0_mask_resampled.nii.gz \
 --processes 1 -f
fi

scil_fodf_max_in_ventricles fodf/fodf.nii.gz dti/fa.nii.gz dti/md.nii.gz \
        --max_value_output fodf/ventricles_fodf_max_value.txt -f

# Clamped at 0: a multi-shell multi-tissue WM fODF is ~0 in the ventricles (the CSF
# compartment takes them), and noise such as -1.8e-22 printed as "-3.6e-22" is read
# by argparse as an option, failing scil_fodf_metrics. Positive values are unchanged.
a_threshold=$(awk '{v = 2*$1; if (v < 0) v = 0; printf "%.10g", v}' fodf/ventricles_fodf_max_value.txt)

# Its outputs (peaks, NuFO, AFD) are not in the stage's output contract, so a failure
# here must stop the stage rather than leave it "complete" without them.
scil_fodf_metrics fodf/fodf.nii.gz --at ${a_threshold} \
        --mask b0_mask_resampled.nii.gz --not_all \
        --afd_max fodf/afd_max.nii.gz \
        --afd_total fodf/afd_total.nii.gz --afd_sum fodf/afd_sum.nii.gz \
        --nufo fodf/nufo.nii.gz \
        --peaks fodf/peaks.nii.gz --peak_indices fodf/peak_indices.nii.gz -f \
    || { echo "ERROR: scil_fodf_metrics failed (--at ${a_threshold})" >&2; exit 1; }

# get out the folder
cd ..

echo "Finished FODF preprocessing: $(date)"
