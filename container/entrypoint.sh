#!/usr/bin/env bash
set -euo pipefail

export PYTHONNOUSERSITE=1
export HDF5_USE_FILE_LOCKING="${HDF5_USE_FILE_LOCKING:-FALSE}"
export SFINCS_EXECUTABLE="${SFINCS_EXECUTABLE:-/opt/sfincs/bin/sfincs}"
export PATH="/opt/autocf-env/bin:/opt/sfincs/bin:${PATH}"

# Keep user-site packages out even if a host home directory is bind-mounted.
unset PYTHONPATH || true
unset PYTHONHOME || true

exec "$@"
