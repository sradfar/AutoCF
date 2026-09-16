"""Create, extract, and validate the large AutoCF runtime release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path


REQUIRED = {
    "autocf-wsl-rootfs.tar.gz": 500_000_000,
    "sfincs-cpu-rootfs.tar.gz": 150_000_000,
    "sfincs-cpu-launcher.sh": 100,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(runtime: Path) -> dict[str, object]:
    errors: list[str] = []
    files: dict[str, dict[str, object]] = {}
    for name, minimum in REQUIRED.items():
        path = runtime / name
        if not path.is_file():
            errors.append(f"missing {name}")
            continue
        size = path.stat().st_size
        if size < minimum:
            errors.append(f"truncated {name}: {size} bytes")
        files[name] = {"bytes": size, "sha256": sha256(path)}
    wheels = runtime / "wheels"
    wheel_count = len(list(wheels.glob("*.whl"))) if wheels.is_dir() else 0
    if wheel_count == 0:
        errors.append("missing offline Python wheels")
    result = {"valid": not errors, "errors": errors, "wheel_count": wheel_count, "files": files}
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)
    return result


def create(runtime: Path, destination: Path) -> None:
    verify(runtime)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in sorted(runtime.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(runtime).as_posix())
    print(f"created {destination} ({destination.stat().st_size} bytes)")


def extract(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            target = (destination / item.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError(f"unsafe archive member: {item.filename}")
        archive.extractall(destination)
    verify(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("runtime", type=Path)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("runtime", type=Path)
    create_parser.add_argument("destination", type=Path)
    extract_parser = sub.add_parser("extract")
    extract_parser.add_argument("archive", type=Path)
    extract_parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        verify(args.runtime)
    elif args.command == "create":
        create(args.runtime, args.destination)
    else:
        extract(args.archive, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

