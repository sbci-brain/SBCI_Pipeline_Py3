"""Raw-input routes: what turns scanner output into the distortion-corrected DWI and BOLD
that preprocessing/ starts from.

  dwi.py   Synb0-DISCO + topup + eddy for DWI acquired in one phase-encoding direction
  fmri.py  BOLD unwarped with FUGUE from the DWI field map, or with topup from a
           spin-echo fieldmap pair

Each builds the list of commands for its stage; workflow/ runs and records them. Data that
was corrected upstream (the `corrected` route) is copied to the same file names instead.
"""
