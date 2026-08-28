#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd, check=True):
    return subprocess.run(
        cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_sfincs(config: Path | None) -> Path | None:
    env = os.environ.get("SFINCS_EXECUTABLE")
    if env and Path(env).expanduser().exists():
        return Path(env).expanduser().resolve()

    if config is not None and config.exists():
        try:
            sys.path.insert(0, str(config.parent.resolve()))
            from sfincs_config import read_config
            cfg = read_config(config)
            p = Path(cfg.get("runtime", {}).get("sfincs", {}).get("executable", ""))
            if p.exists():
                return p.resolve()
        except Exception:
            pass

    p = shutil.which("sfincs")
    return Path(p).resolve() if p else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=Path("environment"))
    args = ap.parse_args()

    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)

    if not os.environ.get("CONDA_PREFIX"):
        raise SystemExit(
            "ERROR: activate the tested AutoCF conda environment before freezing it."
        )
    if shutil.which("conda") is None:
        raise SystemExit("ERROR: conda executable not found.")

    # Exact single-platform conda specification with package checksums.
    explicit = run(["conda", "list", "--explicit", "--md5"]).stdout
    (out / "explicit-linux-64.txt").write_text(explicit)

    # Useful human-readable exports.
    env_export = run(["conda", "env", "export", "--no-builds"]).stdout
    (out / "environment-export.yml").write_text(env_export)

    history = run(["conda", "env", "export", "--from-history"]).stdout
    (out / "environment-history.yml").write_text(history)

    # Only packages actually recorded by conda as PyPI installs.
    conda_rows = json.loads(run(["conda", "list", "--json"]).stdout)
    pip_only = []
    for row in conda_rows:
        channel = str(row.get("channel", "")).lower()
        build = str(row.get("build_string", row.get("build", ""))).lower()
        if channel == "pypi" or build == "pypi_0":
            name = str(row["name"])
            version = str(row["version"])
            pip_only.append(f"{name}=={version}")
    pip_only = sorted(set(pip_only), key=str.lower)
    (out / "pip-only.txt").write_text(
        ("\n".join(pip_only) + "\n") if pip_only else ""
    )

    wanted = [
        "hydromt", "hydromt_sfincs", "numpy", "pandas", "xarray",
        "geopandas", "rasterio", "shapely", "pyproj", "requests",
        "PyYAML", "matplotlib", "cdsapi", "s3fs", "zarr", "overturemaps",
    ]
    versions = {}
    for name in wanted:
        try:
            versions[name] = md.version(name)
        except md.PackageNotFoundError:
            versions[name] = None

    sfincs = find_sfincs(args.config)
    sfincs_info = None
    if sfincs:
        sfincs_info = {
            "path": str(sfincs),
            "sha256": sha256_file(sfincs),
            "size_bytes": sfincs.stat().st_size,
        }
        try:
            ldd = run(["ldd", str(sfincs)], check=False)
            (out / "sfincs-ldd.txt").write_text(ldd.stdout + ldd.stderr)
        except Exception:
            pass
        (out / "sfincs-sha256.txt").write_text(
            f"{sfincs_info['sha256']}  {sfincs}\n"
        )

    manifest = {
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "conda_prefix": os.environ.get("CONDA_PREFIX"),
        "packages": versions,
        "executables": {
            name: shutil.which(name)
            for name in [
                "hydromt", "gdalwarp", "gdalbuildvrt",
                "gdal_rasterize", "overturemaps", "sfincs"
            ]
        },
        "sfincs": sfincs_info,
    }
    (out / "software_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True)
    )

    print(f"Wrote exact conda lock: {out / 'explicit-linux-64.txt'}")
    print(f"Wrote pip-only lock   : {out / 'pip-only.txt'}")
    print(f"Wrote manifest        : {out / 'software_manifest.json'}")
    if sfincs is None:
        print(
            "WARNING: SFINCS executable was not found. "
            "Set SFINCS_EXECUTABLE and rerun to record its checksum."
        )


if __name__ == "__main__":
    main()
