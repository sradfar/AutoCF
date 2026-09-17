"""Assemble the source-free payload embedded in a macOS AutoCF app."""

from __future__ import annotations

import argparse
import hashlib
import json
import py_compile
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


EXPECTED_CACHE_TAG = "cpython-311"
PYTHON_TREES = ("app", "vendor")
PLAIN_TREES = ("data", "examples", "logo")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_plain_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyw", "*.pyc"),
    )


def compile_module(source: Path, destination: Path, display_name: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(
        str(source), cfile=str(destination), dfile=f"autocf/{display_name}",
        doraise=True, optimize=0,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )


def compile_tree(source: Path, destination: Path, display_root: str) -> list[str]:
    compiled: list[str] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts:
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.suffix.lower() in {".py", ".pyw"}:
            target = target.with_suffix(".pyc")
            compile_module(path, target, f"{display_root}/{relative.as_posix()}")
            compiled.append(f"{display_root}/{target.relative_to(destination).as_posix()}")
        elif path.suffix.lower() != ".pyc":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return compiled


def write_manifest(destination: Path, version: str, architecture: str, compiled: list[str]) -> None:
    critical_names = (
        "host/Dockerfile.runtime",
        "host/transformez-helper.sh",
        "windows/autocf_runtime.pyc",
        "windows/transformez_helper.pyc",
        "windows/runtime/autocf-wsl-rootfs.tar.gz",
        "windows/runtime/sfincs-cpu-rootfs.tar.gz",
        "windows/runtime/sfincs-cpu-launcher.sh",
    )
    critical = {name: sha256(destination / name) for name in critical_names}
    manifest = {
        "product": "AutoCF Desktop",
        "version": version,
        "release_channel": "release-candidate",
        "platform": "macOS",
        "architecture": architecture,
        "assembled_utc": datetime.now(timezone.utc).isoformat(),
        "python_payload": {
            "format": "sourceless bytecode",
            "cache_tag": sys.implementation.cache_tag,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "compiled_module_count": len(compiled),
        },
        "readable_autocf_python_sources_included": False,
        "critical_sha256": critical,
    }
    (destination / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def assemble(root: Path, destination: Path, version: str, architecture: str) -> None:
    root = root.resolve()
    destination = destination.resolve()
    if sys.implementation.cache_tag != EXPECTED_CACHE_TAG:
        raise RuntimeError(
            f"macOS payload must be compiled by {EXPECTED_CACHE_TAG}; "
            f"got {sys.implementation.cache_tag}"
        )
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite payload: {destination}")
    destination.mkdir(parents=True)

    compiled: list[str] = []
    for name in PYTHON_TREES:
        compiled.extend(compile_tree(root / name, destination / name, name))
    for name in PLAIN_TREES:
        copy_plain_tree(root / name, destination / name)

    windows = destination / "windows"
    windows.mkdir()
    shutil.copytree(root / "windows" / "runtime", windows / "runtime")
    shutil.copy2(root / "windows" / "riva_qa_database.json", windows / "riva_qa_database.json")
    for module in ("autocf_runtime", "transformez_helper"):
        compile_module(
            root / "windows" / f"{module}.py",
            windows / f"{module}.pyc",
            f"windows/{module}.py",
        )
        compiled.append(f"windows/{module}.pyc")

    host = destination / "host"
    host.mkdir()
    dockerfile = """FROM python:3.11-slim-bookworm AS datum-helper-builder

ARG TRANSFORMEZ_VERSION=0.2.3
ARG HTDP_COMMIT=59fdbe6b7cf5fabf79f901801ecdc33e7ca2095c

RUN apt-get update && apt-get install -y --no-install-recommends \\
      ca-certificates git gfortran && \\
    rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --target /opt/transformez-site \\
      "transformez==${TRANSFORMEZ_VERSION}"
RUN git clone https://github.com/noaa-ngs/HTDP.git /tmp/htdp && \\
    cd /tmp/htdp && git checkout "${HTDP_COMMIT}" && \\
    mkdir -p /opt/transformez-bin && \\
    gfortran -O2 htdp.f initvl.f initeq.f initbd.f initps.f \\
      -o /opt/transformez-bin/htdp

FROM --platform=$BUILDPLATFORM alpine:3.22 AS rootfs-unpacker

COPY windows/runtime/autocf-wsl-rootfs.tar.gz /tmp/autocf-rootfs.tar.gz
COPY windows/runtime/sfincs-cpu-rootfs.tar.gz /tmp/sfincs-rootfs.tar.gz
RUN mkdir -p /rootfs /sfincs-rootfs && \\
    tar -xzf /tmp/autocf-rootfs.tar.gz -C /rootfs && \\
    tar -xzf /tmp/sfincs-rootfs.tar.gz -C /sfincs-rootfs

FROM scratch

COPY --from=rootfs-unpacker /rootfs/ /
COPY --from=rootfs-unpacker /sfincs-rootfs/ /opt/sfincs-cpu/rootfs/
COPY windows/runtime/sfincs-cpu-launcher.sh /usr/local/bin/sfincs-cpu
COPY windows/runtime/wheels /opt/autocf-wheels
COPY --from=datum-helper-builder /opt/transformez-site /opt/transformez-site
COPY --from=datum-helper-builder /opt/transformez-bin /opt/transformez-bin
COPY windows/transformez_helper.pyc /opt/transformez-helper/transformez_helper.pyc
COPY host/transformez-helper.sh /usr/local/bin/transformez-helper

RUN chmod 0755 /usr/local/bin/sfincs-cpu /usr/local/bin/transformez-helper \\
      /opt/transformez-bin/htdp && \\
    /usr/local/bin/python -m pip install --no-index --find-links /opt/autocf-wheels \\
      cartopy==0.25.0 pyshp==3.1.6 overturemaps==1.0.2 cdsapi==0.7.7 && \\
    /usr/local/bin/transformez-helper --version && \\
    printf 'AutoCF Managed Runtime 2.0\\n' > /etc/autocf-release

ENV PATH="/opt/autocf-gdal-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ENV AUTOCF_TRANSFORMEZ_EXECUTABLE="/usr/local/bin/transformez-helper"
ENTRYPOINT []
"""
    (host / "Dockerfile.runtime").write_text(dockerfile, encoding="utf-8")
    (host / "transformez-helper.sh").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "export PATH=\"/opt/transformez-bin:$PATH\"\n"
        "export PYTHONPATH=/opt/transformez-site\n"
        "exec /usr/local/bin/python -B "
        "/opt/transformez-helper/transformez_helper.pyc \"$@\"\n",
        encoding="utf-8",
    )
    for name in ("README.md", "RELEASE-NOTES.txt", "START-HERE.txt"):
        shutil.copy2(root / name, destination / name)
    write_manifest(destination, version, architecture, compiled)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", required=True)
    args = parser.parse_args()
    assemble(args.root, args.destination, args.version, args.architecture)
    print(json.dumps({
        "payload": str(args.destination.resolve()),
        "cache_tag": sys.implementation.cache_tag,
        "readable_python_sources": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
