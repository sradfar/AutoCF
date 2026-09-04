#!/usr/bin/env bash
set -euo pipefail

echo "=== NVIDIA ==="
nvidia-smi

echo "=== SFINCS binary ==="
command -v sfincs
ldd "$(command -v sfincs)" | grep -E 'not found' && {
  echo "FAIL: missing SFINCS shared libraries"
  exit 1
} || true

echo "=== SFINCS startup / GPU acceptance ==="
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

# SFINCS prints its release banner before stopping because no sfincs.inp exists.
set +e
timeout 30 sfincs >sfincs_startup.log 2>&1
rc=$?
set -e
cat sfincs_startup.log

grep -q "v2.4.0" sfincs_startup.log
grep -qi "Galibier" sfincs_startup.log

# rc is expected non-zero because this intentionally has no model input.
echo "SFINCS GPU runtime startup: PASS (expected no-input exit rc=$rc)"
