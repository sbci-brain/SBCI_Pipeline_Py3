#!/bin/bash

# this script is for two things:
# 1. prepare data for tractography
# 2. register t1 to diffusion (d0 and fa) space

# Author: Zhengwu Zhang
# Date: Oct. 23, 2018

# Updated for CSVD by Kyle Murray
# Updated for SBCI by Martin Cole

# Python 3 port of the legacy ADNI_example script. Only the scilpy commands
# changed (scilpy 1.x -> 2.x renames); FSL, ANTs and MRtrix calls are
# byte-identical to the original, as are all parameters.
#   scil_extract_b0.py              -> scil_dwi_extract_b0      (--b0_thr -> --b0_threshold)
#   scil_apply_bias_field_on_dwi.py -> scil_dwi_apply_bias_field
#   scil_crop_volume.py             -> scil_volume_crop         (bbox .pkl -> .json; scilpy 2.x
#                                      only accepts pickle via --use_deprecated_pickle, which is
#                                      mutually exclusive with --input_bbox/--output_bbox)
#   scil_resample_volume.py         -> scil_volume_resample     (--resolution -> --voxel_size)
#   scil_run_nlmeans.py             -> scil_denoising_nlmeans   (positional N + --noise_est basic
#                                      -> --basic_sigma --number_coils N)
#   scil_extract_dwi_shell.py       -> scil_dwi_extract_shell   (-t -> --tolerance)
#   scil_compute_dti_metrics.py     -> scil_dti_metrics

echo "Sourcing SBCI config file"
source "${SBCI_CONFIG}"

if [ -z ${TEMPLATEDIR+x} ]; then
    TEMPLATEDIR=${SBCI_PATH}/data/mni_152_sym_09c
fi

echo "Begin T1-DWI registration preprocessing: $(date)"

# folder for t1 preprocessing
mkdir -p structure/

#results for diffusion analysis
mkdir -p diffusion/

# antsBrainExtraction.sh writes into bet/ and does not create it
mkdir -p bet/

# guarded so the stage is safe to re-run after a partial failure
[ -f data.nii.gz ] && mv -f data.nii.gz diffusion/data.nii.gz
[ -f flip_x.bvec ] && mv -f flip_x.bvec diffusion/flip_x.bvec
[ -f flip_x.bval ] && mv -f flip_x.bval diffusion/flip_x.bval

#####################################
####### b-value shell selection #####
#####################################

# Which shells reach DTI and fODF is an explicit choice, SHELL_POLICY in sbci_config:
#   nearest_1000  DTI and fODF both use the non-zero shell nearest b=1000. The ADNI
#                 choice: its protocols are mixed, (0,1000), (0,500,1000,2000), (0,2000)...
#   explicit      DTI_BVALS / FODF_BVALS from the config, as written
#   all           fODF gets every non-zero shell (multi-shell models); DTI keeps the
#                 shell nearest b=1000, because the tensor model does not hold at high b
# Single-shell data needs no policy. Multi-shell data without one stops here: keeping
# one shell silently would discard the rest of the acquisition and still exit 0.
# Checked before any processing, so a missing policy costs seconds rather than hours.
# Shells are b-values rounded to the nearest 100.
DETECTED_SHELLS=$(cat diffusion/flip_x.bval | tr -s '[:space:]' '\n' | grep -v '^$' | \
    awk '{printf "%d\n", int($1/100+0.5)*100}' | sort -nu)
NONZERO_SHELLS=$(echo "$DETECTED_SHELLS" | awk '$1 > 0')
N_SHELLS=$(echo "$NONZERO_SHELLS" | grep -c .)

if [ "$N_SHELLS" -eq 0 ]; then
    echo "ERROR: No non-zero b-value shells detected in flip_x.bval"
    exit 1
fi

if [ -z "${SHELL_POLICY:-}" ]; then
    if [ "$N_SHELLS" -gt 1 ]; then
        echo "ERROR: multi-shell DWI (b = $(echo $NONZERO_SHELLS)) and SHELL_POLICY is not set in ${SBCI_CONFIG}."
        echo "       Set SHELL_POLICY=nearest_1000, all or explicit; refusing to discard shells silently."
        exit 1
    fi
    SHELL_POLICY=nearest_1000
fi

_BEST_SHELL=""
_BEST_DIST=999999
for _s in $NONZERO_SHELLS; do
    _d=$(( _s - 1000 ))
    _d=${_d#-}
    if [ "$_d" -lt "$_BEST_DIST" ]; then
        _BEST_DIST=$_d
        _BEST_SHELL=$_s
    fi
done

# fODF extraction tolerance: 20 as always (or FODF_TOLERANCE from sbci_config), except
# under `all`, where it is the same +-50 window that defines a shell above.
# scil_dwi_extract_shell (scilpy 2.x) keeps |b - shell| <= tolerance; the legacy py2
# scilpy behind the lab's historical outputs kept < tolerance, so at 20 it dropped HCP's
# b=2980 volumes and 2.x keeps them. The historical profiles set 19 to reproduce that.
FODF_TOLERANCE=${FODF_TOLERANCE:-20}
case "$SHELL_POLICY" in
    nearest_1000) DTI_BVALS=(0 $_BEST_SHELL); FODF_BVALS=(0 $_BEST_SHELL) ;;
    explicit)     ;;
    all)          DTI_BVALS=(0 $_BEST_SHELL); FODF_BVALS=(0 $NONZERO_SHELLS); FODF_TOLERANCE=50 ;;
    *)
        echo "ERROR: SHELL_POLICY=${SHELL_POLICY} is not one of nearest_1000, all, explicit."
        exit 1
        ;;
esac

echo "Detected shells: $(echo $DETECTED_SHELLS)"
echo "SHELL_POLICY=${SHELL_POLICY}: DTI_BVALS=${DTI_BVALS[*]}, FODF_BVALS=${FODF_BVALS[*]}"

#####################################
######## dMRI preprocessing #########
#####################################

# extract b0 from dwi
scil_dwi_extract_b0 diffusion/data.nii.gz \
	diffusion/flip_x.bval \
	diffusion/flip_x.bvec \
	diffusion/b0.nii.gz \
	--mean --b0_threshold 10

# bet dwi
bet diffusion/b0.nii.gz diffusion/b0_bet.nii.gz -m -R -f 0.10

mrcalc diffusion/data.nii.gz diffusion/b0_bet_mask.nii.gz \
	 -mult diffusion/dwi_bet.nii.gz -quiet -force

# N4_DWI
N4BiasFieldCorrection -i diffusion/b0_bet.nii.gz \
        -o [diffusion/b0_n4.nii.gz, diffusion/bias_field_b0.nii.gz] \
        -c [300x150x75x50, 1e-6] -v 1

scil_dwi_apply_bias_field diffusion/dwi_bet.nii.gz diffusion/bias_field_b0.nii.gz \
        diffusion/dwi_n4.nii.gz --mask diffusion/b0_bet_mask.nii.gz -f

# crop dwi
scil_volume_crop diffusion/dwi_n4.nii.gz diffusion/dwi_cropped.nii.gz \
        --output_bbox diffusion/dwi_boundingBox.json -f

scil_volume_crop diffusion/b0_bet.nii.gz diffusion/b0_cropped.nii.gz \
        --input_bbox diffusion/dwi_boundingBox.json -f

scil_volume_crop diffusion/b0_bet_mask.nii.gz diffusion/b0_mask_cropped.nii.gz \
        --input_bbox diffusion/dwi_boundingBox.json -f

# dwi normalization
dwinormalise individual \
        diffusion/dwi_cropped.nii.gz \
        diffusion/b0_cropped.nii.gz \
        diffusion/dwi_normalized.nii.gz \
        -fslgrad diffusion/flip_x.bvec diffusion/flip_x.bval -force

# resample the dti image into 1x1x1
scil_volume_resample diffusion/dwi_normalized.nii.gz \
	diffusion/dwi_resample.nii.gz \
	--voxel_size 1 --interp "cubic" -f

fslmaths diffusion/dwi_resample.nii.gz -thr 0 diffusion/dwi_resample_clipped.nii.gz

scil_volume_resample diffusion/b0_mask_cropped.nii.gz \
	diffusion/mask_resample.nii.gz \
        --ref diffusion/dwi_resample.nii.gz \
        --enforce_dimensions \
        --interp nn -f

mrcalc diffusion/dwi_resample_clipped.nii.gz \
	diffusion/mask_resample.nii.gz \
        -mult diffusion/dwi_resampled.nii.gz -quiet -force

# resample b0
scil_dwi_extract_b0 diffusion/dwi_resampled.nii.gz \
	diffusion/flip_x.bval \
	diffusion/flip_x.bvec \
	diffusion/b0_resampled.nii.gz \
	--mean --b0_threshold 10

mrthreshold diffusion/b0_resampled.nii.gz diffusion/b0_mask_resampled.nii.gz --abs 0.00001 -force


###################################################################
######################## t1 preprocessing #########################
###################################################################

# denoise and bias field correction for t1 image
scil_denoising_nlmeans t1.nii.gz structure/t1_denoised.nii.gz --basic_sigma --number_coils 1 -f

N4BiasFieldCorrection -i structure/t1_denoised.nii.gz \
        -o [structure/t1_n4.nii.gz, structure/bias_field_t1.nii.gz] \
        -c [300x150x75x50, 1e-6] -v 1

# bet t1
antsBrainExtraction.sh -d 3 -a structure/t1_n4.nii.gz -e ${TEMPLATEDIR}/t1/t1_template.nii.gz \
        -o bet/ -m ${TEMPLATEDIR}/t1/t1_brain_probability_map.nii.gz -u 0

mrcalc structure/t1_n4.nii.gz bet/BrainExtractionMask.nii.gz -mult structure/t1_bet.nii.gz -force

mv -f bet/BrainExtractionMask.nii.gz structure/t1_bet_mask.nii.gz

# crop t1
scil_volume_crop structure/t1_bet.nii.gz structure/t1_bet_cropped.nii.gz \
        --output_bbox structure/t1_boundingBox.json -f

scil_volume_crop structure/t1_bet_mask.nii.gz structure/t1_bet_mask_cropped.nii.gz \
        --input_bbox structure/t1_boundingBox.json -f


######################################################################
########### dwi preprocessing - get ready for tracking ###############
######################################################################

# DTI_BVALS / FODF_BVALS were fixed by SHELL_POLICY at the top of this script.

# extract data for dti
scil_dwi_extract_shell diffusion/dwi_resampled.nii.gz \
        diffusion/flip_x.bval diffusion/flip_x.bvec ${DTI_BVALS[*]} diffusion/dwi_dti.nii.gz \
        diffusion/dti.bval diffusion/dti.bvec --tolerance 20 -f

# extract data for fodf
scil_dwi_extract_shell diffusion/dwi_resampled.nii.gz \
        diffusion/flip_x.bval diffusion/flip_x.bvec ${FODF_BVALS[*]} diffusion/dwi_fodf.nii.gz \
        diffusion/fodf.bval diffusion/fodf.bvec --tolerance ${FODF_TOLERANCE} -f

mkdir -p diffusion/dti

# compute dti metrics
scil_dti_metrics diffusion/dwi_dti.nii.gz diffusion/dti.bval diffusion/dti.bvec \
	--mask diffusion/b0_mask_resampled.nii.gz \
        --not_all \
        --ad diffusion/dti/ad.nii.gz --evecs diffusion/dti/evecs.nii.gz \
        --evals diffusion/dti/evals.nii.gz --fa diffusion/dti/fa.nii.gz \
        --ga diffusion/dti/ga.nii.gz --rgb diffusion/dti/rgb.nii.gz \
        --md diffusion/dti/md.nii.gz --mode diffusion/dti/mode.nii.gz \
        --norm diffusion/dti/norm.nii.gz --rd diffusion/dti/rd.nii.gz \
        --tensor diffusion/dti/tensor.nii.gz \
        --non-physical diffusion/dti/nonphysical.nii.gz -f

################################################
####### registration between dti and t1 ########
################################################

# registration between t1 and diffusion imaging data can use multiple threads
export ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS=1

$SRUN antsRegistration --dimensionality 3 --float 0\
        --output [output,outputWarped.nii.gz,outputInverseWarped.nii.gz]\
        --interpolation Linear --use-histogram-matching 0\
        --winsorize-image-intensities [0.005,0.995]\
        --initial-moving-transform [diffusion/b0_resampled.nii.gz,structure/t1_bet_cropped.nii.gz,1]\
        --transform Rigid['0.2']\
        --metric MI[diffusion/b0_resampled.nii.gz,structure/t1_bet_cropped.nii.gz,1,32,Regular,0.25]\
        --convergence [500x250x125x50,1e-6,10] --shrink-factors 8x4x2x1\
        --smoothing-sigmas 3x2x1x0\
        --transform Affine['0.2']\
        --metric MI[diffusion/b0_resampled.nii.gz,structure/t1_bet_cropped.nii.gz,1,32,Regular,0.25]\
        --convergence [500x250x125x50,1e-6,10] --shrink-factors 8x4x2x1\
        --smoothing-sigmas 3x2x1x0\
        --transform SyN[0.1,3,0]\
        --metric MI[diffusion/b0_resampled.nii.gz,structure/t1_bet_cropped.nii.gz,1,32]\
        --metric CC[diffusion/dti/fa.nii.gz,structure/t1_bet_cropped.nii.gz,1,4]\
        --convergence [50x25x10,1e-6,10] --shrink-factors 4x2x1\
        --smoothing-sigmas 3x2x1


mv -f outputWarped.nii.gz structure/t1_warped.nii.gz
mv -f output0GenericAffine.mat structure/output0GenericAffine.mat
mv -f output1InverseWarp.nii.gz structure/output1InverseWarp.nii.gz
mv -f output1Warp.nii.gz structure/output1Warp.nii.gz

antsApplyTransforms -d 3 -i structure/t1_bet_mask_cropped.nii.gz -r structure/t1_warped.nii.gz \
        -o structure/t1_mask_warped.nii.gz -n NearestNeighbor \
        -t structure/output1Warp.nii.gz structure/output0GenericAffine.mat

# for freesurfer
antsApplyTransforms -d 3 -i structure/t1_n4.nii.gz -r structure/t1_n4.nii.gz \
        -o structure/t1_wholebrain_warped.nii.gz -n Linear \
        -t structure/output1Warp.nii.gz structure/output0GenericAffine.mat

echo "Finished T1-DWI registration preprocessing: $(date)"
