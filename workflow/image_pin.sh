# The image every job runs in, pinned by digest so that every clone of this release uses the
# same one. Read by get_image.sbatch (which fetches it) and runtime_env.sh (the Docker route).
REPOSITORY=sdszhauk/sbci-image
TAG=20261123
DIGEST=sha256:d5b6f2c64867238e0704cd408ad876460aeafe8610860391cc86c9d661feeb0a
