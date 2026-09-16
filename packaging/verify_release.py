"""Reject missing, suspiciously small, or incorrectly named release files."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


MINIMUM_BYTES = {
    "Windows": 150_000_000,
    "Linux": 500_000_000,
    "macOS": 500_000_000,
    "Runtime": 500_000_000,
}


def main() -> int:
    artifact = Path(sys.argv[1])
    expected_name = sys.argv[2]
    if not artifact.is_file():
        raise SystemExit(f"missing artifact: {artifact}")
    if expected_name not in artifact.name:
        raise SystemExit(f"wrong artifact name: {artifact.name}; expected {expected_name}")
    family = next((key for key in MINIMUM_BYTES if key in expected_name), None)
    if family is None:
        raise SystemExit(f"unknown artifact family: {expected_name}")
    size = artifact.stat().st_size
    if size < MINIMUM_BYTES[family]:
        raise SystemExit(f"artifact is suspiciously small: {size} bytes")
    hasher = hashlib.sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    print(json.dumps({"artifact": str(artifact), "bytes": size, "sha256": digest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
