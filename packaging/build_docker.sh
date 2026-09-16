#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${AUTOCF_VERSION:-1.0.0}"
IMAGE="${AUTOCF_DOCKER_IMAGE:-autocf/runtime:${VERSION}}"
OUT="$ROOT/dist/AutoCF-Runtime-${VERSION}-amd64.tar.zst"

mkdir -p "$ROOT/dist"
docker build --platform linux/amd64 -f "$ROOT/host/Dockerfile.runtime" -t "$IMAGE" "$ROOT"
docker run --rm --platform linux/amd64 "$IMAGE" /usr/local/bin/python --version
# SFINCS has no --version flag. A launch in an empty directory must reach the
# native engine and report the expected missing sfincs.inp error (exit 2).
docker run --rm --platform linux/amd64 "$IMAGE" /bin/sh -lc '
  work="$(mktemp -d)"; cd "$work"
  set +e
  output="$(/usr/local/bin/sfincs-cpu 2>&1)"; status=$?
  set -e
  printf "%s\n" "$output"
  test "$status" -eq 2
  printf "%s\n" "$output" | grep -q "sfincs.inp"
'
docker run --rm --platform linux/amd64 "$IMAGE" /usr/local/bin/transformez-helper --version

# Level 9 provides a compact offline image without making CI spend tens of
# minutes on the marginal gains of ultra compression (-19).
docker save "$IMAGE" | zstd -T0 -9 -o "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
