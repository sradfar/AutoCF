#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import sys
from pathlib import Path

PACKAGE_IMPORTS = {
    "numpy": "numpy",
    "pandas": "pandas",
    "xarray": "xarray",
    "geopandas": "geopandas",
    "rasterio": "rasterio",
    "shapely": "shapely",
    "pyproj": "pyproj",
    "requests": "requests",
    "PyYAML": "yaml",
    "matplotlib": "matplotlib",
    "hydromt": "hydromt",
    "hydromt_sfincs": "hydromt_sfincs",
    "cdsapi": "cdsapi",
    "s3fs": "s3fs",
    "zarr": "zarr",
}

EXECUTABLES = [
    "hydromt", "gdalwarp", "gdalbuildvrt", "gdal_rasterize", "overturemaps"
]


def sha256(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--sfincs", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    result = {
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": {},
        "executables": {},
        "sfincs": None,
        "ok": True,
    }

    print("AutoCF environment doctor")
    print("=" * 72)

    for dist, module in PACKAGE_IMPORTS.items():
        try:
            importlib.import_module(module)
            try:
                version = md.version(dist)
            except Exception:
                version = "installed"
            result["packages"][dist] = {"ok": True, "version": version}
            print(f"[OK]   {dist:<20} {version}")
        except Exception as exc:
            result["packages"][dist] = {"ok": False, "error": repr(exc)}
            result["ok"] = False
            print(f"[FAIL] {dist:<20} {exc}")

    for exe in EXECUTABLES:
        p = shutil.which(exe)
        result["executables"][exe] = p
        if p:
            print(f"[OK]   executable {exe:<14} {p}")
        else:
            # overturemaps is optional for core hydraulics.
            required = exe != "overturemaps"
            if required:
                result["ok"] = False
            print(f"[{'FAIL' if required else 'WARN'}] executable {exe:<14} not found")

    sfincs = args.sfincs
    if sfincs is None:
        env = os.environ.get("SFINCS_EXECUTABLE")
        if env:
            sfincs = Path(env)
    if sfincs is None and args.config is not None:
        try:
            sys.path.insert(0, str(args.config.parent.resolve()))
            from sfincs_config import read_config
            cfg = read_config(args.config)
            raw = cfg.get("runtime", {}).get("sfincs", {}).get("executable")
            if raw:
                sfincs = Path(raw)
        except Exception as exc:
            print(f"[WARN] could not resolve SFINCS from config: {exc}")

    if sfincs is None:
        p = shutil.which("sfincs")
        sfincs = Path(p) if p else None

    if sfincs is not None and sfincs.expanduser().exists():
        sfincs = sfincs.expanduser().resolve()
        result["sfincs"] = {
            "path": str(sfincs),
            "sha256": sha256(sfincs),
            "size_bytes": sfincs.stat().st_size,
        }
        print(f"[OK]   SFINCS executable      {sfincs}")
        print(f"[OK]   SFINCS sha256          {result['sfincs']['sha256']}")
    else:
        result["ok"] = False
        print("[FAIL] SFINCS executable not found")

    try:
        from hydromt_sfincs import SfincsModel
        has_plot = hasattr(SfincsModel, "plot_basemap")
        print(f"[OK]   HydroMT SfincsModel    plot_basemap={has_plot}")
        if not has_plot:
            result["ok"] = False
    except Exception as exc:
        result["ok"] = False
        print(f"[FAIL] HydroMT SfincsModel import: {exc}")

    print("=" * 72)
    print("PASS" if result["ok"] else "FAIL")

    if args.json:
        args.json.write_text(json.dumps(result, indent=2, sort_keys=True))

    raise SystemExit(0 if result["ok"] else 2)


if __name__ == "__main__":
    main()
