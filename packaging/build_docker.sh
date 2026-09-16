#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${AUTOCF_VERSION:-1.0.0}"
IMAGE="${AUTOCF_DOCKER_IMAGE:-autocf/runtime:${VERSION}}"
OUT="$ROOT/dist/AutoCF-Runtime-${VERSION}-amd64.tar.zst"

mkdir -p "$ROOT/dist"
docker build --platform linux/amd64 -f "$ROOT/host/Dockerfile.runtime" -t "$IMAGE" "$ROOT"
docker run --rm --platform linux/amd64 "$IMAGE" /usr/local/bin/python --version
docker run --rm --platform linux/amd64 "$IMAGE" /usr/local/bin/sfincs-cpu --version
docker run --rm --platform linux/amd64 "$IMAGE" /usr/local/bin/transformez-helper --version

docker save "$IMAGE" | zstd -T0 -19 -o "$OUT"
sha256sum "$OUT" > "$OUT.sha256"

