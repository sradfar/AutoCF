#!/usr/bin/env python3
"""
sfincs_agent.py

Standalone SFINCS regular/subgrid-mesh agent.

User inputs:
  1. sfincs_agent_config.yml (regular) or sfincs_agent_subgrid_config.yml (subgrid)
  2. one GeoJSON containing:
       - Polygon or MultiPolygon: SFINCS domain
       - LineString or MultiLineString: ocean/open-boundary mask
       - Point(s): water-level forcing anchor points

The default workflow downloads public datasets online, while each major forcing can also be replaced by a user-supplied file. In subgrid mode the same provider pipeline is reused. Public V1 providers include:
  - topobathy: NOAA/NCEI CUDEM + GEBCO, or user raster(s)
  - roughness: ESA WorldCover, or user land-cover/Manning inputs
  - precipitation: ERA5, automatic NOAA AORC, MRMS input, user NetCDF, or none
  - wind: ERA5, automatic NOAA AORC, user NetCDF, or none
  - atmospheric pressure: ERA5, automatic NOAA AORC, user NetCDF, or none
  - ocean boundary: NOAA CO-OPS, user time series, or a constant level
  - river discharge: USGS auto-selection, explicit USGS sites, user time series, or none
  - infiltration: USGS Annual NLCD + USDA SSURGO CN-with-recovery, global GCN250, or user CN raster
  - evaluation observations: NOAA CO-OPS or user observations
  - production external validation: USGS high-water marks

Remote-sensing flood-extent products and Google Flood Hub are not acquired in
the production AutoCF workflow. Global gauge discovery/evaluation is handled
by sfincs_global_validation.py before/after this hydraulic core.

AORC is downloaded/subset automatically from NOAA Public Data on AWS when source=aorc and no path is supplied. A user-supplied AORC NetCDF path remains supported. MRMS is file-backed in this first public version.

Resume behavior:
  - The script writes <output_dir>/agent_state.json.
  - Each completed stage is marked done.
  - Restart with the same command: python -u sfincs_agent.py
  - Existing downloaded/preprocessed files are reused.
  - Existing SFINCS outputs are not overwritten unless project.overwrite=true.

Important Della note:
  - Run this script on a login node for downloading/building because compute nodes may not have internet.
  - No Slurm or sbatch is used. Use workflow.mode=build_only on a login node, then workflow.mode=run_direct_only on an interactive GPU node.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
import yaml

try:
    import geopandas as gpd
    from shapely.geometry import Point, shape, mapping
    from shapely.ops import unary_union
    from pyproj import CRS, Transformer
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Missing geospatial Python packages. Install geopandas, shapely, pyproj.\n"
        f"Original import error: {exc}"
    )


DEFAULT_CONFIG = "sfincs_agent_config.yml"

NOAA_MDAPI_STATIONS = (
    "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
)
NOAA_DATA_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
USGS_SITE_API = "https://waterservices.usgs.gov/nwis/site/"
USGS_IV_API = "https://waterservices.usgs.gov/nwis/iv/"

USDA_SDA_TABULAR_API = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
USDA_SDA_WFS = "https://sdmdataaccess.sc.egov.usda.gov/Spatial/SDMWGS84Geographic.wfs"
USDA_SDA_WFS_VERSION = "1.0.0"
USGS_ANNUAL_NLCD_WCS_BASE = "https://dmsdata.cr.usgs.gov/geoserver"
USGS_ANNUAL_NLCD_LNDCOV_WORKSPACE = "mrlc_Land-Cover-Native_conus_year_data"

# GCN250 is published on Figshare (Jaafar & Ahmad, 2019). HydroMT's legacy
# artifact_data v0.0.8 catalog points to a retired GitHub release archive,
# which now returns HTTP 404. Public V1 therefore resolves the requested
# GCN250 antecedent condition directly from the authoritative Figshare record
# and caches only that GeoTIFF.
GCN250_FIGSHARE_ARTICLE_ID = 7756202
GCN250_FIGSHARE_API = f"https://api.figshare.com/v2/articles/{GCN250_FIGSHARE_ARTICLE_ID}"
GCN250_FILENAMES = {
    "dry": "GCN250_ARCI.tif",
    "avg": "GCN250_ARCII.tif",
    "wet": "GCN250_ARCIII.tif",
}

# CN-II / average-condition lookup used in the USACE HEC-HMS NLCD + HSG
# tutorial. Annual NLCD retains the same standard Level-II class codes. Class
# 12 (perennial ice/snow) is not listed in the cited HEC tutorial; this agent
# uses an explicit conservative fallback of CN=98 (same as open water). Supply
# infiltration.cn_lookup.path to replace any bundled lookup values.
DEFAULT_NLCD_CN_II = {
    11: (98, 98, 98, 98),
    12: (98, 98, 98, 98),
    21: (49, 69, 79, 84),
    22: (57, 72, 81, 86),
    23: (61, 75, 83, 87),
    24: (81, 88, 91, 93),
    31: (78, 86, 91, 93),
    41: (45, 66, 77, 83),
    42: (25, 55, 70, 77),
    43: (36, 60, 73, 79),
    52: (55, 72, 81, 86),
    71: (50, 69, 79, 84),
    81: (49, 69, 79, 84),
    82: (67, 78, 85, 89),
    90: (30, 58, 71, 78),
    95: (30, 58, 71, 78),
}

DEFAULT_ESA_MANNING = {
    10: ("Tree cover", 0.120),
    20: ("Shrubland", 0.050),
    30: ("Grassland", 0.034),
    40: ("Cropland", 0.037),
    50: ("Built-up", 0.100),
    60: ("Bare sparse vegetation", 0.023),
    70: ("Snow and ice", 0.010),
    80: ("Permanent water bodies", 0.020),
    90: ("Herbaceous wetland", 0.035),
    95: ("Mangroves", 0.070),
    100: ("Moss and lichen", 0.025),
}


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge config dictionaries without mutating the inputs."""
    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def read_yaml(path: Path, _stack: Optional[List[Path]] = None) -> Dict[str, Any]:
    """Read YAML with optional ``extends: other.yml`` inheritance.

    This lets the dedicated subgrid config contain only subgrid-specific changes
    while inheriting the already-tested forcing, units, URLs, executable path,
    simulation dates, and GeoJSON settings from the regular config.
    """
    path = path.expanduser().resolve()
    stack = list(_stack or [])
    if path in stack:
        chain = " -> ".join(str(p) for p in stack + [path])
        raise RuntimeError(f"Circular YAML inheritance detected: {chain}")
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Top-level YAML object must be a mapping: {path}")

    parent = raw.pop("extends", None)
    if parent:
        parent_path = Path(str(parent)).expanduser()
        if not parent_path.is_absolute():
            parent_path = path.parent / parent_path
        base = read_yaml(parent_path, stack + [path])
        cfg = _deep_merge_dict(base, raw)
    else:
        cfg = raw

    # Optional collision-safe output directory derivation for overlay configs.
    proj = cfg.get("project", {})
    suffix = proj.get("output_dir_suffix")
    if suffix:
        base_out = Path(str(proj["output_dir"])).expanduser()
        suffix = str(suffix)
        if not base_out.name.endswith(suffix):
            proj["output_dir"] = str(base_out.with_name(base_out.name + suffix))
        cfg["project"] = proj

    return cfg


def get_model_mode(cfg: Dict[str, Any]) -> str:
    mode = str(cfg.get("model", {}).get("mode", "regular")).strip().lower()
    if mode not in {"regular", "subgrid"}:
        raise RuntimeError(f"Unsupported model.mode={mode!r}; use 'regular' or 'subgrid'.")
    return mode


def subgrid_sampling_resolution_m(cfg: Dict[str, Any]) -> float:
    """Nominal SFINCS subgrid pixel size implied by dx / nr_subgrid_pixels."""
    dx = float(cfg["mesh"]["dx_m"])
    n = int(cfg.get("subgrid", {}).get("nr_subgrid_pixels", 20))
    if n <= 0:
        raise RuntimeError("subgrid.nr_subgrid_pixels must be > 0")
    return dx / float(n)


def infiltration_enabled(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("infiltration", {}).get("enabled", False))


def infiltration_signature(cfg: Dict[str, Any]) -> str:
    """Stable hash used to prevent running a stale HydroMT build after edits."""
    obj = cfg.get("infiltration", {}) if infiltration_enabled(cfg) else {"enabled": False}
    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]

def forcing_signature(cfg: Dict[str, Any]) -> str:
    """Hash forcing/evaluation provider choices to prevent stale resumed builds."""
    obj = {
        "forcing": forcing_cfg(cfg),
        "aorc": cfg.get("aorc", {}),
        "evaluation": cfg.get("evaluation", {}),
        "simulation": cfg.get("simulation", {}),
    }
    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _bbox_signature(bbox: Iterable[float], *parts: Any) -> str:
    vals = [f"{float(v):.6f}" for v in bbox] + [str(v) for v in parts]
    return hashlib.sha1("|".join(vals).encode("utf-8")).hexdigest()[:16]


def _snap_bounds(bounds: Iterable[float], res: float) -> Tuple[float, float, float, float]:
    minx, miny, maxx, maxy = [float(v) for v in bounds]
    return (
        math.floor(minx / res) * res,
        math.floor(miny / res) * res,
        math.ceil(maxx / res) * res,
        math.ceil(maxy / res) * res,
    )


def write_default_nlcd_cn_table(path: Path) -> Path:
    """Write the bundled NLCD x HSG Curve Number II lookup matrix."""
    ensure_dir(path.parent)
    rows = []
    for nlcd, vals in DEFAULT_NLCD_CN_II.items():
        rows.append({"nlcd": nlcd, "1": vals[0], "2": vals[1], "3": vals[2], "4": vals[3]})
    pd.DataFrame(rows).set_index("nlcd").to_csv(path)
    return path



def get_hydromt_subgrid_api() -> Tuple[str, set[str]]:
    """Return installed HydroMT-SFINCS version and setup_subgrid keyword names.

    The user's established environment uses hydromt_sfincs 1.0.3, while newer
    releases added parameters such as q_table_option / weight_option. Filtering
    against the live signature prevents a newer configuration from breaking an
    older, otherwise-valid environment.
    """
    try:
        import inspect
        from importlib import metadata
        from hydromt_sfincs import SfincsModel  # type: ignore

        try:
            version = metadata.version("hydromt_sfincs")
        except Exception:
            try:
                version = metadata.version("hydromt-sfincs")
            except Exception:
                version = "unknown"
        sig = inspect.signature(SfincsModel.setup_subgrid)
        params = {name for name in sig.parameters if name != "self"}
        return version, params
    except Exception as exc:
        raise RuntimeError(
            "Cannot inspect hydromt_sfincs.SfincsModel.setup_subgrid. "
            "Activate the same HydroMT-SFINCS environment used for building."
        ) from exc


def write_yaml(obj: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False, default_flow_style=False)


def write_json(obj: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    tmp.replace(path)


def read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_cmd(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> None:
    log("RUN: " + " ".join(str(c) for c in cmd))
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed with status {p.returncode}: {' '.join(cmd)}")


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required executable not found in PATH: {name}")
    return path


def parse_time(s: str) -> pd.Timestamp:
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def sfincs_time(ts: pd.Timestamp) -> str:
    ts = ts.tz_convert("UTC")
    return ts.strftime("%Y%m%d %H%M%S")


def era5_forcing_end(cfg: Dict[str, Any]) -> pd.Timestamp:
    """Return the first complete UTC hour at or after simulation tstop.

    HydroMT-SFINCS 1.0.3 clips gridded atmospheric forcing to the model time
    window. For a non-hourly SFINCS tstop (for example 00:12), the HydroMT
    forcing build therefore needs to extend temporarily to 01:00 so that the
    first ERA5 hourly field after tstop is retained. The exact SFINCS tstop is
    restored in sfincs.inp immediately after the HydroMT build.
    """
    end = parse_time(cfg["simulation"]["end"])
    return end.ceil("h")



def forcing_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return the public V1 provider configuration with legacy compatibility.

    New configurations should use ``forcing``. Older study configs containing
    atmosphere/noaa_boundary/usgs_rivers continue to work unchanged.
    """
    if isinstance(cfg.get("forcing"), dict):
        return cfg["forcing"]
    acfg = cfg.get("atmosphere", {})
    legacy_atm_source = str(acfg.get("source", "ERA5")).lower()
    return {
        "precipitation": {"source": legacy_atm_source},
        "wind": {"source": legacy_atm_source},
        "pressure": {"source": legacy_atm_source},
        "ocean_boundary": {"source": "noaa_coops", **cfg.get("noaa_boundary", {})},
        "river": {
            "source": "usgs_auto" if cfg.get("usgs_rivers", {}).get("enabled", True) else "none",
            **cfg.get("usgs_rivers", {}),
        },
    }


def _provider_source(cfg: Dict[str, Any], key: str, default: str = "none") -> str:
    return str(forcing_cfg(cfg).get(key, {}).get("source", default)).strip().lower()


def atmospheric_forcing_end(cfg: Dict[str, Any]) -> pd.Timestamp:
    """Hourly bracket only when at least one gridded atmospheric forcing is used."""
    active = any(_provider_source(cfg, k) != "none" for k in ("precipitation", "wind", "pressure"))
    end = parse_time(cfg["simulation"]["end"])
    return end.ceil("h") if active else end


def resolve_user_path(value: Any, label: str) -> Path:
    if value in (None, ""):
        raise RuntimeError(f"Missing required path for {label}")
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise RuntimeError(f"Configured {label} does not exist: {path}")
    return path.resolve()


def copy_user_file(value: Any, dst: Path, label: str) -> Path:
    src = resolve_user_path(value, label)
    ensure_dir(dst.parent)
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return dst


def validate_pressure_grid_pa(path: Optional[Path]) -> None:
    """Validate that gridded atmospheric pressure handed to HydroMT is in Pa."""
    if path is None:
        return
    import xarray as xr  # type: ignore

    ds = xr.open_dataset(path)
    try:
        candidates = [v for v in ("press", "press_msl", "PRES_surface", "msl") if v in ds.data_vars]
        if not candidates:
            if len(ds.data_vars) == 1:
                candidates = [next(iter(ds.data_vars))]
            else:
                raise RuntimeError(
                    f"Cannot identify pressure variable in {path}; variables={list(ds.data_vars)}"
                )
        da = ds[candidates[0]]
        mn = float(da.min(skipna=True))
        mx = float(da.max(skipna=True))
    finally:
        ds.close()
    if mn < 70000.0 or mx > 120000.0:
        raise RuntimeError(
            f"Pressure forcing {path} is not plausible in Pa: min={mn}, max={mx}. "
            "HydroMT-SFINCS setup_pressure_forcing_from_grid requires Pa. "
            "ERA5/AORC should use factor 1.0; hPa/mbar user files should use factor 100.0."
        )
    log(f"Validated atmospheric pressure units: {path.name} | range={mn:.1f}..{mx:.1f} Pa")



def _aorc_ordered_slice(coord: Any, lo: float, hi: float) -> slice:
    """Return a coordinate slice that respects increasing/decreasing AORC axes."""
    first = float(coord.values[0])
    last = float(coord.values[-1])
    return slice(lo, hi) if first <= last else slice(hi, lo)


def _slug(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_")
    return text or "event"


def download_aorc_subset(
    cfg: Dict[str, Any],
    geo_meta: Dict[str, Any],
    paths: Paths,
    requested_kinds: Iterable[str],
) -> Path:
    """Download/cache the requested AORC subset directly from NOAA public S3.

    The source is NOAA AORC v1.1's cloud-optimized yearly Zarr archive. The
    subset is clipped to the model domain plus a configurable geographic
    buffer and to the model atmospheric-forcing time window. One cached
    NetCDF is shared by precipitation, wind and pressure providers.

    If a study already has a local AORC NetCDF, users may still set ``path``
    under the corresponding forcing provider and this downloader is bypassed.
    """
    import xarray as xr  # type: ignore

    try:
        import s3fs  # type: ignore
        import zarr  # noqa: F401  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Automatic AORC download requires the Python packages 's3fs' and 'zarr' "
            "inside the SFINCS-agent environment. The AORC downloader is integrated "
            "into sfincs_agent.py; no separate downloader script is needed. Check with: "
            "python -c \"import s3fs, zarr; print('AORC dependencies OK')\""
        ) from exc

    acfg = cfg.get("aorc", {})
    bucket = str(acfg.get("bucket", "noaa-nws-aorc-v1-1-1km")).strip().strip("/")
    cache_subdir = str(acfg.get("cache_subdir", "aorc"))
    buffer_deg = float(acfg.get("spatial_buffer_degrees", 0.5))

    kinds = {str(k).strip().lower() for k in requested_kinds}
    variable_map = {
        "precipitation": ["APCP_surface"],
        "wind": ["UGRD_10maboveground", "VGRD_10maboveground"],
        "pressure": ["PRES_surface"],
    }
    unknown = sorted(kinds.difference(variable_map))
    if unknown:
        raise RuntimeError(f"Unsupported automatic AORC component(s): {unknown}")
    vars_needed: List[str] = []
    for kind in ("precipitation", "wind", "pressure"):
        if kind in kinds:
            vars_needed.extend(variable_map[kind])
    if not vars_needed:
        raise RuntimeError("download_aorc_subset called without requested AORC variables")

    start = parse_time(cfg["simulation"]["start"]).tz_localize(None)
    end = atmospheric_forcing_end(cfg).tz_localize(None)
    west, south, east, north = bbox_buffer_degrees(geo_meta["bbox"], buffer_deg)

    # AORC CONUS v1.1 longitudes are negative-west. Keep a clear failure for
    # domains outside the published archive instead of silently returning an
    # empty grid.
    if east < -180 or west > 180 or north < -90 or south > 90:
        raise RuntimeError(f"Invalid AORC request bbox: {[west, south, east, north]}")

    sig = _bbox_signature(
        [west, south, east, north],
        start.isoformat(), end.isoformat(), bucket, ",".join(vars_needed),
    )
    event = _slug(cfg.get("simulation", {}).get("event_name", "event"))
    aorc_dir = paths.cache / cache_subdir
    ensure_dir(aorc_dir)
    out = aorc_dir / f"aorc_{event}_{start:%Y%m%d%H}_{end:%Y%m%d%H}_{sig}.nc"
    if file_ready(out, 1024):
        log(f"Reusing automatic AORC subset: {out}")
        return out

    log(
        "Automatic AORC download: NOAA public S3 | "
        f"components={','.join(sorted(kinds))} | "
        f"time={start}..{end} | bbox=W{west:.4f},S{south:.4f},E{east:.4f},N{north:.4f}"
    )

    fs = s3fs.S3FileSystem(anon=True)
    pieces = []
    try:
        for year in range(start.year, end.year + 1):
            y0 = max(start, pd.Timestamp(year=year, month=1, day=1))
            y1 = min(end, pd.Timestamp(year=year, month=12, day=31, hour=23, minute=59, second=59))
            if y1 < y0:
                continue
            root = f"{bucket}/{year}.zarr"
            log(f"Opening AORC yearly Zarr: s3://{root}")
            mapper = s3fs.S3Map(root=root, s3=fs, check=False)
            try:
                ds = xr.open_zarr(mapper, consolidated=True, chunks=None)
            except Exception:
                ds = xr.open_zarr(mapper, consolidated=False, chunks=None)

            try:
                missing = [v for v in vars_needed if v not in ds.data_vars]
                if missing:
                    raise RuntimeError(
                        f"AORC {year} is missing variables {missing}; available={list(ds.data_vars)}"
                    )
                lat_name = "latitude" if "latitude" in ds.coords else "lat"
                lon_name = "longitude" if "longitude" in ds.coords else "lon"
                if lat_name not in ds.coords or lon_name not in ds.coords or "time" not in ds.coords:
                    raise RuntimeError(f"Unexpected AORC coordinates for {year}: {list(ds.coords)}")

                sub = ds[vars_needed].sel(
                    time=slice(y0, y1),
                    **{
                        lat_name: _aorc_ordered_slice(ds[lat_name], south, north),
                        lon_name: _aorc_ordered_slice(ds[lon_name], west, east),
                    },
                )
                if sub.sizes.get("time", 0) == 0:
                    raise RuntimeError(f"AORC {year} subset has no time steps in {y0}..{y1}")
                if sub.sizes.get(lat_name, 0) == 0 or sub.sizes.get(lon_name, 0) == 0:
                    raise RuntimeError(
                        "AORC subset has no spatial cells. The study area may lie outside "
                        "the AORC v1.1 spatial domain."
                    )
                log(f"Loading AORC {year} subset: dims={dict(sub.sizes)}")
                pieces.append(sub.load())
            finally:
                ds.close()
    except Exception as exc:
        raise RuntimeError(
            "Automatic NOAA AORC download/subsetting failed. Confirm login-node internet "
            "access and that the study dates/domain are covered by AORC. "
            f"Original error: {exc}"
        ) from exc

    if not pieces:
        raise RuntimeError("Automatic AORC download returned no data")
    merged = pieces[0] if len(pieces) == 1 else xr.concat(pieces, dim="time")
    merged = merged.sortby("time")
    # Remove duplicate boundary timestamps when concatenating calendar years.
    tvals = pd.DatetimeIndex(pd.to_datetime(merged["time"].values))
    keep = ~tvals.duplicated(keep="first")
    if not bool(np.all(keep)):
        merged = merged.isel(time=np.flatnonzero(np.asarray(keep)))

    log("AORC subset ranges:")
    for v in vars_needed:
        da = merged[v]
        mn = float(da.min(skipna=True))
        mx = float(da.max(skipna=True))
        log(f"  {v}: {mn:.6g} .. {mx:.6g} {da.attrs.get('units', '')}")

    tmp = out.with_suffix(out.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    merged.to_netcdf(tmp)
    if not file_ready(tmp, 1024):
        raise RuntimeError(f"AORC subset write failed or produced an empty file: {tmp}")
    tmp.replace(out)
    log(f"Automatic AORC subset ready: {out}")
    return out


def standardize_atmospheric_file(
    cfg: Dict[str, Any], provider: Dict[str, Any], kind: str, paths: Paths
) -> Path:
    """Normalize AORC/MRMS/user gridded forcing to HydroMT variable names.

    Automatic AORC acquisition is handled upstream and arrives here as the same
    NetCDF interface used by an explicitly supplied AORC file. ``factor`` and
    ``offset`` provide explicit unit conversion.
    """
    import xarray as xr  # type: ignore

    source = str(provider.get("source", "file")).lower()
    src = resolve_user_path(provider.get("path"), f"forcing.{kind}.path")
    if src.suffix.lower() not in {".nc", ".nc4", ".cdf"}:
        raise RuntimeError(
            f"{source}/{kind} V1 expects a NetCDF file after download/subsetting; got {src}. "
            "Convert GRIB/Zarr to NetCDF or use the provider's preprocessing workflow first."
        )
    ds = xr.open_dataset(src)
    defaults = {
        "wind": {
            "aorc": ("UGRD_10maboveground", "VGRD_10maboveground"),
            "file": ("wind_u", "wind_v"),
        },
        "pressure": {"aorc": ("PRES_surface",), "file": ("press",)},
        "precipitation": {
            "aorc": ("APCP_surface",), "mrms": ("precip",), "file": ("precip",)
        },
    }
    dflt = defaults[kind].get(source, defaults[kind].get("file"))
    start = parse_time(cfg["simulation"]["start"]).tz_localize(None)
    end = atmospheric_forcing_end(cfg).tz_localize(None)

    # Normalize common coordinate names.
    ren_coord = {}
    for a, b in (("valid_time", "time"), ("latitude", "lat"), ("longitude", "lon")):
        if a in ds.coords and b not in ds.coords:
            ren_coord[a] = b
    if ren_coord:
        ds = ds.rename(ren_coord)
    if "time" not in ds.coords:
        raise RuntimeError(f"{src} does not contain a time coordinate")
    try:
        ds = ds.sel(time=slice(start, end))
    except Exception:
        pass
    if ds.sizes.get("time", 0) == 0:
        raise RuntimeError(f"{src} has no data overlapping the simulation period")

    def one(raw: str, final: str, factor: float = 1.0, offset: float = 0.0):
        if raw not in ds:
            raise RuntimeError(f"Variable {raw!r} not found in {src}; available={list(ds.data_vars)}")
        return ds[raw] * factor + offset

    if kind == "wind":
        uv = provider.get("variables", {})
        u = str(uv.get("u", dflt[0])); v = str(uv.get("v", dflt[1]))
        fac = float(provider.get("factor", 1.0)); off = float(provider.get("offset", 0.0))
        out = xr.Dataset({"wind_u": one(u, "wind_u", fac, off), "wind_v": one(v, "wind_v", fac, off)})
        dst = paths.data / f"{source}_wind_standardized.nc"
    elif kind == "pressure":
        raw = str(provider.get("variable", dflt[0]))
        # HydroMT-SFINCS setup_pressure_forcing_from_grid requires pressure in Pa.
        # ERA5 and NOAA AORC pressure products are already in Pa. For a user
        # file stored in hPa/mbar, set factor: 100.0 explicitly in the YAML.
        fac = float(provider.get("factor", 1.0))
        off = float(provider.get("offset", 0.0))
        press = one(raw, "press", fac, off)
        mn = float(press.min(skipna=True))
        mx = float(press.max(skipna=True))
        if mn < 70000.0 or mx > 120000.0:
            raise RuntimeError(
                f"Pressure range check failed after conversion: min={mn}, max={mx} Pa. "
                "HydroMT-SFINCS requires pressure in Pa. If the source is in hPa/mbar, "
                "set forcing.pressure.factor: 100.0."
            )
        press.attrs["units"] = "Pa"
        out = xr.Dataset({"press": press})
        dst = paths.data / f"{source}_pressure_standardized_pa.nc"
    else:
        raw = str(provider.get("variable", dflt[0]))
        da = one(raw, "precip", float(provider.get("factor", 1.0)), float(provider.get("offset", 0.0)))
        # AORC and MRMS precipitation products are interval accumulations.
        # hydromt_sfincs 1.0.3 writes the supplied precipitation values directly
        # as SFINCS precipitation intensity, so convert an interval depth [mm]
        # to the equivalent mean rate [mm/hr]. Do NOT cumulatively sum the
        # successive hourly amounts. For native hourly AORC, the numerical
        # values are unchanged because 1 mm over 1 hour = 1 mm/hr.
        if bool(provider.get("interval_accumulation", source in {"aorc", "mrms"})):
            if ds.sizes.get("time", 0) < 2:
                raise RuntimeError(
                    f"{source}/{kind} interval accumulation requires at least two time steps"
                )
            tvals = pd.DatetimeIndex(pd.to_datetime(ds["time"].values))
            dt_hours = float(np.median(np.diff(tvals.values).astype("timedelta64[s]").astype(float))) / 3600.0
            if not np.isfinite(dt_hours) or dt_hours <= 0:
                raise RuntimeError(f"Could not infer a valid precipitation interval from {src}")
            da = da.clip(min=0) / dt_hours
            # AORC APCP is a one-hour accumulation ENDING at the timestamp.
            # SFINCS expects the rate at the START of the upcoming interval, so
            # shift a right-labeled accumulation back by exactly one interval.
            time_label = str(
                provider.get(
                    "accumulation_time_label",
                    "right" if source in {"aorc", "mrms"} else "left",
                )
            ).strip().lower()
            if time_label not in {"left", "right"}:
                raise RuntimeError(
                    f"forcing.{kind}.accumulation_time_label must be left or right; got {time_label!r}"
                )
            if time_label == "right":
                shift = pd.to_timedelta(dt_hours, unit="h")
                da = da.assign_coords(time=pd.DatetimeIndex(pd.to_datetime(da["time"].values)) - shift)
            da.attrs["units"] = "mm/hr"
            log(
                f"Standardized {source.upper()} precipitation interval depth to rate: "
                f"dt={dt_hours:g} h; time_label={time_label}; no cumulative sum applied."
            )
        out = xr.Dataset({"precip": da})
        dst = paths.data / f"{source}_precip_standardized.nc"
    # Only materialize the selected variable(s), not every field in a shared
    # automatic AORC subset. This avoids loading the same four-variable file
    # three times when rain, wind and pressure all use AORC.
    out = out.load()
    ds.close()
    out.to_netcdf(dst)
    return dst


def write_forcing_provenance(cfg: Dict[str, Any], paths: Paths, resolved: Dict[str, Any]) -> None:
    report = {
        "simulation": cfg.get("simulation", {}),
        "providers": {k: forcing_cfg(cfg).get(k, {}) for k in ("precipitation", "wind", "pressure", "ocean_boundary", "river")},
        "resolved_files": {k: (str(v) if v is not None else None) for k, v in resolved.items()},
        "notes": {
            "aorc": "source=aorc auto-downloads/subsets NOAA AORC unless a provider path is supplied.",
            "mrms": "MRMS remains file-backed in public V1.",
            "pressure": "May be ERA5, automatic/file AORC, generic file, or none.",
        },
    }
    write_json(report, paths.reports / "forcing_report.json")

def noaa_time(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y%m%d %H:%M")


def hydromt_naive_utc_index(index: Any) -> pd.DatetimeIndex:
    """Return a timezone-naive UTC DatetimeIndex for HydroMT 0.9.x.

    HydroMT 0.9.4 calls ``np.dtype(df.index)`` while slicing DataFrame
    forcing. That fails for timezone-aware pandas indices such as
    ``datetime64[us, UTC]``. SFINCS times are UTC, so convert to UTC first
    and then remove only the timezone metadata.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx


def write_hydromt_timeseries_csv(df: pd.DataFrame, path: Path) -> None:
    """Write forcing CSV with a HydroMT-0.9-compatible UTC time index."""
    out = df.copy()
    out.index = hydromt_naive_utc_index(out.index)
    out.index.name = "time"
    out.to_csv(path)


def sanitize_hydromt_timeseries_csv(path: Optional[Path]) -> None:
    """Repair an existing forcing CSV in place if its time index has a timezone.

    This is deliberately called even for already-completed download stages so a
    failed HydroMT build can resume without redownloading NOAA/USGS data.
    """
    if path is None or not path.exists():
        return
    df = pd.read_csv(path, index_col=0)
    idx = pd.DatetimeIndex(pd.to_datetime(df.index))
    had_tz = idx.tz is not None
    df.index = hydromt_naive_utc_index(idx)
    df.index.name = "time"
    df.to_csv(path)
    if had_tz:
        log(f"HydroMT compatibility: converted forcing timestamps to timezone-naive UTC: {path}")



def normalize_hydromt_point_forcing_ids(
    timeseries_path: Optional[Path],
    locations_path: Optional[Path],
    source_id_field: str,
) -> None:
    """Make tabular point-forcing IDs compatible with HydroMT-SFINCS 1.0.x.

    hydromt_sfincs 1.0.x converts tabular forcing column names to ``int``.
    Therefore labels such as ``bnd001`` or arbitrary string station IDs cannot
    be used as the forcing-key columns. This helper rewrites both sides of the
    forcing pair consistently to sequential integer IDs 1..N while preserving
    the original identifiers in ``source_id_field``.

    It is safe to call on every invocation and repairs existing/resumed files
    in place without redownloading the underlying NOAA/USGS data.
    """
    if timeseries_path is None or locations_path is None:
        return
    if not timeseries_path.exists() or not locations_path.exists():
        return

    df = pd.read_csv(timeseries_path, index_col=0)
    gdf = gpd.read_file(locations_path)

    if len(df.columns) != len(gdf):
        raise RuntimeError(
            "Point-forcing mismatch before HydroMT build: "
            f"{timeseries_path} has {len(df.columns)} series but "
            f"{locations_path} has {len(gdf)} locations."
        )
    if len(gdf) == 0:
        return

    original_cols = [str(c) for c in df.columns]
    numeric_ids = list(range(1, len(original_cols) + 1))

    # Preserve the original source/station identifiers. If this file has
    # already been normalized, retain the existing source-id metadata.
    if source_id_field not in gdf.columns:
        if "ID" in gdf.columns:
            gdf[source_id_field] = gdf["ID"].astype(str)
        else:
            gdf[source_id_field] = original_cols

    # Always enforce exact 1..N ordering on both the CSV columns and locations.
    # This removes ambiguity and matches the int conversion performed by
    # hydromt_sfincs 1.0.x.
    changed = original_cols != [str(i) for i in numeric_ids]
    df.columns = numeric_ids

    gdf = gdf.reset_index(drop=True)
    old_ids = gdf["ID"].astype(str).tolist() if "ID" in gdf.columns else []
    if old_ids != [str(i) for i in numeric_ids]:
        changed = True
    gdf["ID"] = numeric_ids

    # Keep a human-readable name, but do not use it as the forcing key.
    if "name" not in gdf.columns:
        gdf["name"] = gdf[source_id_field].astype(str)

    # Re-apply the HydroMT 0.9.x timezone compatibility while writing.
    df.index = hydromt_naive_utc_index(df.index)
    df.index.name = "time"
    df.to_csv(timeseries_path)
    gdf.to_file(locations_path, driver="GeoJSON")

    if changed:
        log(
            "HydroMT-SFINCS 1.0.x compatibility: normalized point-forcing IDs "
            f"to sequential integers 1..{len(numeric_ids)} in "
            f"{timeseries_path.name} and {locations_path.name}; "
            f"original IDs preserved in {source_id_field!r}."
        )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def file_ready(path: Path, min_bytes: int = 1) -> bool:
    return path.exists() and path.stat().st_size >= min_bytes


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def retry_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 120, tries: int = 4) -> requests.Response:
    last_exc = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            last_exc = RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        except Exception as exc:
            last_exc = exc
        sleep_s = 3 * (i + 1)
        log(f"HTTP request failed; retrying in {sleep_s}s: {last_exc}")
        time.sleep(sleep_s)
    raise RuntimeError(f"HTTP request failed after retries: {url}\n{last_exc}")


@dataclass
class Paths:
    out: Path
    cache: Path
    data: Path
    work: Path
    gis: Path
    reports: Path
    validation: Path
    state: Path


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = read_json(path, {"done": {}, "files": {}, "created": datetime.now(timezone.utc).isoformat()})

    def is_done(self, key: str) -> bool:
        return bool(self.data.get("done", {}).get(key, False))

    def mark(self, key: str, **extra: Any) -> None:
        self.data.setdefault("done", {})[key] = True
        self.data.setdefault("meta", {})[key] = {"time_utc": datetime.now(timezone.utc).isoformat(), **extra}
        write_json(self.data, self.path)

    def set_file(self, key: str, path: Path) -> None:
        self.data.setdefault("files", {})[key] = str(path)
        write_json(self.data, self.path)


# -----------------------------------------------------------------------------
# GeoJSON parsing
# -----------------------------------------------------------------------------


def extract_geojson_parts(geojson_path: Path, paths: Paths) -> Dict[str, Any]:
    log(f"Reading GeoJSON: {geojson_path}")
    gdf = gpd.read_file(geojson_path)
    if gdf.empty:
        raise RuntimeError("GeoJSON has no features.")
    gdf = gdf.set_crs(4326) if gdf.crs is None else gdf.to_crs(4326)

    role_col = "role" if "role" in gdf.columns else None
    roles = gdf[role_col].fillna("").astype(str).str.strip().str.lower() if role_col else pd.Series("", index=gdf.index)
    has_roles = bool((roles != "").any())

    if has_roles:
        polys = gdf[(roles == "domain") & gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        lines = gdf[(roles == "ocean_boundary") & gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
        points = gdf[(roles == "ocean_forcing_point") & (gdf.geometry.geom_type == "Point")].copy()
        river_points = gdf[(roles == "river_source") & (gdf.geometry.geom_type == "Point")].copy()
        obs_points = gdf[(roles == "observation_point") & (gdf.geometry.geom_type == "Point")].copy()
    else:
        # Fully backward compatible geometry-type convention.
        polys = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        lines = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
        points = gdf[gdf.geometry.geom_type == "Point"].copy()
        river_points = gdf.iloc[0:0].copy(); obs_points = gdf.iloc[0:0].copy()

    if len(polys) != 1:
        raise RuntimeError(f"Expected exactly one domain Polygon/MultiPolygon; found {len(polys)}.")
    if len(lines) < 1:
        raise RuntimeError("Expected at least one ocean_boundary LineString/MultiLineString.")
    if len(points) < 1:
        raise RuntimeError("Expected at least one ocean_forcing_point Point.")

    domain_geom = polys.geometry.iloc[0]
    line_geom = unary_union(lines.geometry.values)
    domain = gpd.GeoDataFrame({"name": ["domain"]}, geometry=[domain_geom], crs=4326)
    ocean = gpd.GeoDataFrame({"name": ["ocean_bc"]}, geometry=[line_geom], crs=4326)
    pts = points.reset_index(drop=True).copy()
    if "ID" not in pts.columns:
        pts["ID"] = [f"bnd{idx+1:03d}" for idx in range(len(pts))]
    pts["ID"] = pts["ID"].astype(str)
    if "name" not in pts.columns:
        pts["name"] = pts["ID"]

    domain_path = paths.gis / "domain.geojson"
    ocean_path = paths.gis / "ocean_bc_line.geojson"
    bnd_points_path = paths.gis / "boundary_forcing_points.geojson"
    domain.to_file(domain_path, driver="GeoJSON")
    ocean.to_file(ocean_path, driver="GeoJSON")
    pts[["ID", "name", "geometry"]].to_file(bnd_points_path, driver="GeoJSON")

    river_path = None
    if len(river_points):
        rp = river_points.reset_index(drop=True).copy()
        if "ID" not in rp.columns: rp["ID"] = [f"riv{i+1:03d}" for i in range(len(rp))]
        river_path = paths.gis / "river_source_points.geojson"; rp.to_file(river_path, driver="GeoJSON")
    obs_path = None
    if len(obs_points):
        op = obs_points.reset_index(drop=True).copy()
        if "ID" not in op.columns: op["ID"] = [f"obs{i+1:03d}" for i in range(len(op))]
        obs_path = paths.gis / "user_observation_points.geojson"; op.to_file(obs_path, driver="GeoJSON")

    minx, miny, maxx, maxy = domain.total_bounds
    centroid = domain.geometry.iloc[0].centroid
    zone = int((centroid.x + 180) // 6) + 1
    epsg = 32600 + zone if centroid.y >= 0 else 32700 + zone
    meta = {
        "domain_path": str(domain_path), "ocean_path": str(ocean_path), "bnd_points_path": str(bnd_points_path),
        "river_points_path": str(river_path) if river_path else "", "observation_points_path": str(obs_path) if obs_path else "",
        "bbox": [float(minx), float(miny), float(maxx), float(maxy)],
        "centroid_lonlat": [float(centroid.x), float(centroid.y)], "auto_utm_epsg": epsg,
        "n_boundary_points": int(len(pts)), "n_river_points": int(len(river_points)), "n_observation_points": int(len(obs_points)),
        "role_schema_used": has_roles,
    }
    write_json(meta, paths.reports / "geojson_report.json")
    log(f"GeoJSON parsed: domain=1, ocean lines={len(lines)}, boundary points={len(points)}, river points={len(river_points)}, obs points={len(obs_points)}, UTM EPSG:{epsg}")
    return meta

def bbox_buffer_degrees(bbox: List[float], buffer_deg: float) -> List[float]:
    minx, miny, maxx, maxy = bbox
    return [minx - buffer_deg, miny - buffer_deg, maxx + buffer_deg, maxy + buffer_deg]


def bbox_buffer_km(bbox: List[float], buffer_km: float) -> List[float]:
    minx, miny, maxx, maxy = bbox
    mid_lat = 0.5 * (miny + maxy)
    dlat = buffer_km / 111.0
    dlon = buffer_km / max(20.0, 111.0 * math.cos(math.radians(mid_lat)))
    return [minx - dlon, miny - dlat, maxx + dlon, maxy + dlat]


# -----------------------------------------------------------------------------
# Online raster downloads
# -----------------------------------------------------------------------------


def gdalwarp_remote(src: str, dst: Path, bbox4326: List[float], t_srs: str, tr_m: float, resampling: str = "bilinear") -> None:
    ensure_dir(dst.parent)
    if file_ready(dst, 1024):
        log(f"Reusing existing raster: {dst}")
        return
    require_executable("gdalwarp")
    te = [str(x) for x in bbox4326]
    cmd = [
        "gdalwarp",
        "-overwrite",
        "-of", "GTiff",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-multi",
        "-wo", "NUM_THREADS=ALL_CPUS",
        "-te", *te,
        "-te_srs", "EPSG:4326",
        "-t_srs", t_srs,
        "-tr", str(tr_m), str(tr_m),
        "-r", resampling,
        src,
        str(dst),
    ]
    run_cmd(cmd)


def esa_tile_name(lat_sw: int, lon_sw: int) -> str:
    ns = "N" if lat_sw >= 0 else "S"
    ew = "E" if lon_sw >= 0 else "W"
    return f"{ns}{abs(lat_sw):02d}{ew}{abs(lon_sw):03d}"


def esa_tiles_for_bbox(bbox: List[float]) -> List[str]:
    minx, miny, maxx, maxy = bbox
    lon0 = int(math.floor(minx / 3.0) * 3)
    lon1 = int(math.floor(maxx / 3.0) * 3)
    lat0 = int(math.floor(miny / 3.0) * 3)
    lat1 = int(math.floor(maxy / 3.0) * 3)
    tiles = []
    for lat in range(lat0, lat1 + 1, 3):
        for lon in range(lon0, lon1 + 1, 3):
            tiles.append(esa_tile_name(lat, lon))
    return sorted(set(tiles))


def download_file(url: str, dst: Path, min_bytes: int = 1024) -> None:
    ensure_dir(dst.parent)
    if file_ready(dst, min_bytes):
        log(f"Reusing existing download: {dst}")
        return
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    log(f"Downloading: {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    if tmp.stat().st_size < min_bytes:
        raise RuntimeError(f"Downloaded file is too small: {dst}")
    tmp.replace(dst)


def _normalize_gcn250_condition(value: Any) -> str:
    v = str(value if value is not None else "avg").strip().lower()
    aliases = {
        "i": "dry", "arc_i": "dry", "arci": "dry",
        "ii": "avg", "average": "avg", "arc_ii": "avg", "arcii": "avg",
        "iii": "wet", "arc_iii": "wet", "arciii": "wet",
    }
    v = aliases.get(v, v)
    if v not in GCN250_FILENAMES:
        raise RuntimeError(
            "infiltration.antecedent_moisture must be dry, avg, or wet for source=gcn250"
        )
    return v


def download_gcn250(cfg: Dict[str, Any], paths: Paths) -> Path:
    """Download and cache the requested GCN250 antecedent-condition raster.

    We intentionally do not use HydroMT 0.9.x's predefined ``artifact_data``
    catalog because its v0.0.8 archive URL is retired and returns HTTP 404.
    The authoritative GCN250 Figshare record is queried for the current file
    URL, so we do not hard-code an ephemeral Figshare file ID.
    """
    icfg = cfg.get("infiltration", {})
    cond = _normalize_gcn250_condition(icfg.get("antecedent_moisture", "avg"))
    filename = GCN250_FILENAMES[cond]
    dst = paths.cache / "gcn250" / filename
    if file_ready(dst, 10 * 1024 * 1024):
        log(f"Reusing GCN250 {cond} raster: {dst}")
        return dst

    log(
        f"Resolving GCN250 {cond} raster from Figshare article "
        f"{GCN250_FIGSHARE_ARTICLE_ID}: {filename}"
    )
    try:
        r = requests.get(GCN250_FIGSHARE_API, timeout=60)
        r.raise_for_status()
        meta = r.json()
    except Exception as exc:
        raise RuntimeError(
            "Could not query the public GCN250 Figshare metadata API. "
            f"URL={GCN250_FIGSHARE_API} | {exc}"
        ) from exc

    files = meta.get("files", []) if isinstance(meta, dict) else []
    match = next(
        (f for f in files if str(f.get("name", "")).lower() == filename.lower()),
        None,
    )
    if not match or not match.get("download_url"):
        available = ", ".join(str(f.get("name")) for f in files)
        raise RuntimeError(
            f"GCN250 file {filename!r} was not found in Figshare article "
            f"{GCN250_FIGSHARE_ARTICLE_ID}. Available files: {available}"
        )

    download_file(str(match["download_url"]), dst, min_bytes=10 * 1024 * 1024)
    log(f"GCN250 {cond} raster ready: {dst}")
    return dst


def download_esa_worldcover(cfg: Dict[str, Any], bbox: List[float], paths: Paths, model_crs: str) -> Path:
    """Download/reproject ESA WorldCover at an appropriate resolution.

    Regular mode keeps the original behavior: categorical land cover is placed
    on the computational mesh resolution before creating sfincs.man.

    Subgrid mode MUST retain high-resolution roughness information. Therefore
    WorldCover is reprojected at ``subgrid.landcover_output_resolution_m``
    (default 10 m, approximately the native WorldCover resolution) rather than
    at the coarse SFINCS computational-cell size. HydroMT then samples this
    raster while constructing the subgrid roughness/conveyance tables.
    """
    lc = cfg["landcover"]
    mode = get_model_mode(cfg)
    bbox_lc = bbox_buffer_km(bbox, float(lc.get("buffer_km", 10)))
    tiles = esa_tiles_for_bbox(bbox_lc)
    tile_paths = []
    for tile in tiles:
        url = lc["tile_url_template"].format(tile=tile)
        dst = paths.cache / "esa_worldcover" / f"ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
        download_file(url, dst, min_bytes=10000)
        tile_paths.append(dst)

    vrt = paths.data / "esa_worldcover_mosaic.vrt"
    if mode == "subgrid":
        sg = cfg.get("subgrid", {})
        target_res = float(sg.get("landcover_output_resolution_m", 10.0))
        if target_res <= 0:
            raise RuntimeError("subgrid.landcover_output_resolution_m must be > 0")
        out = paths.data / f"esa_worldcover_model_crs_subgrid_{target_res:g}m.tif"
        log(
            f"Subgrid roughness preprocessing: WorldCover target={target_res:g} m; "
            f"nominal SFINCS subgrid pixel={subgrid_sampling_resolution_m(cfg):g} m"
        )
    else:
        target_res = float(cfg["mesh"]["dx_m"])
        out = paths.data / "esa_worldcover_model_crs.tif"

    if not file_ready(vrt, 1024):
        require_executable("gdalbuildvrt")
        run_cmd(["gdalbuildvrt", str(vrt), *[str(p) for p in tile_paths]])
    if not file_ready(out, 1024):
        require_executable("gdalwarp")
        run_cmd([
            "gdalwarp", "-overwrite", "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
            "-te", *[str(x) for x in bbox_lc], "-te_srs", "EPSG:4326",
            "-t_srs", model_crs, "-tr", str(target_res), str(target_res),
            "-r", "mode", str(vrt), str(out)
        ])
    return out

def write_esa_manning_table(path: Path) -> None:
    ensure_dir(path.parent)
    rows = []
    for code, (desc, n) in DEFAULT_ESA_MANNING.items():
        rows.append({"esa_worldcover": code, "description": desc, "landuse": desc, "N": n})
    rows.append({"esa_worldcover": 0, "description": "No data", "landuse": "No data", "N": -999.0})
    pd.DataFrame(rows).to_csv(path, index=False)


# -----------------------------------------------------------------------------
# Infiltration: Annual NLCD + USDA SSURGO
# -----------------------------------------------------------------------------


def _stream_get_to_file(url: str, params: Dict[str, Any], dst: Path, timeout: int = 300, tries: int = 4) -> None:
    """Resumable-at-stage HTTP download with retry and atomic replacement."""
    ensure_dir(dst.parent)
    if file_ready(dst, 1024):
        log(f"Reusing existing download: {dst}")
        return
    last_exc: Optional[Exception] = None
    for attempt in range(tries):
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        try:
            log(f"Downloading: {url}")
            with requests.get(url, params=params, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                ctype = str(r.headers.get("content-type", "")).lower()
                if "xml" in ctype or "html" in ctype:
                    body = r.content[:2000].decode("utf-8", errors="ignore")
                    raise RuntimeError(f"Server returned {ctype} instead of a raster: {body[:1000]}")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            if not file_ready(tmp, 1024):
                raise RuntimeError(f"Downloaded file is empty or too small: {tmp}")
            tmp.replace(dst)
            return
        except Exception as exc:
            last_exc = exc
            if tmp.exists():
                tmp.unlink()
            if attempt + 1 < tries:
                wait = 5 * (attempt + 1)
                log(f"Download failed; retrying in {wait}s: {exc}")
                time.sleep(wait)
    raise RuntimeError(f"Download failed after {tries} attempts: {url}\n{last_exc}")


def download_annual_nlcd_landcover(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths
) -> Path:
    """Download an Annual NLCD land-cover subset from the official USGS WCS.

    Annual NLCD is requested on its native EPSG:5070, 30 m grid. Keeping the
    categorical source at native resolution avoids unnecessary resampling before
    HydroMT combines it with SSURGO HSG data.
    """
    icfg = cfg["infiltration"]
    lcfg = icfg.get("landcover", {})
    year_cfg = lcfg.get("year", "simulation")
    if year_cfg is None or str(year_cfg).strip().lower() in {"simulation", "event", "auto"}:
        year = int(parse_time(cfg["simulation"]["start"]).year)
    else:
        year = int(year_cfg)
    native_res = float(lcfg.get("native_resolution_m", 30.0))
    native_crs = str(lcfg.get("native_crs", "EPSG:5070"))
    buffer_km = float(lcfg.get("buffer_km", 5.0))
    workspace = str(lcfg.get("workspace", USGS_ANNUAL_NLCD_LNDCOV_WORKSPACE))
    base = str(lcfg.get("wcs_base_url", USGS_ANNUAL_NLCD_WCS_BASE)).rstrip("/")

    domain = gpd.read_file(geo_meta["domain_path"])
    if domain.crs is None:
        domain = domain.set_crs(4326)
    domain = domain.to_crs(native_crs)
    if buffer_km > 0:
        geom = domain.geometry.union_all().buffer(buffer_km * 1000.0)
        bounds = geom.bounds
    else:
        bounds = domain.total_bounds
    minx, miny, maxx, maxy = _snap_bounds(bounds, native_res)

    sig = _bbox_signature([minx, miny, maxx, maxy], year, workspace, native_res)
    cache_file = paths.cache / "annual_nlcd" / f"Annual_NLCD_LndCov_{year}_{sig}.tif"
    crs_tag = native_crs.upper().replace(":", "")
    out = paths.data / f"annual_nlcd_lndcov_{year}_{crs_tag.lower()}_{native_res:g}m.tif"

    if not file_ready(cache_file, 1024):
        coverage = f"{workspace}:{workspace.removeprefix('mrlc_')}"
        wcs_url = f"{base}/{workspace}/wcs"
        params = {
            "service": "WCS",
            "version": "1.0.0",
            "request": "GetCoverage",
            "coverage": coverage,
            "CRS": native_crs,
            "BBOX": f"{minx},{miny},{maxx},{maxy}",
            "time": f"{year}-01-01T00:00:00.000Z",
            "format": "image/geotiff",
            "resx": native_res,
            "resy": native_res,
        }
        _stream_get_to_file(wcs_url, params, cache_file, timeout=int(lcfg.get("timeout_seconds", 600)))

    if not file_ready(out, 1024):
        shutil.copy2(cache_file, out)
    write_json(
        {
            "source": "USGS Annual NLCD",
            "year": year,
            "native_crs": native_crs,
            "native_resolution_m": native_res,
            "bbox_native": [minx, miny, maxx, maxy],
            "path": str(out),
        },
        paths.reports / "annual_nlcd_infiltration_report.json",
    )
    log(f"Annual NLCD land cover ready: {out}")
    return out


def _wfs_tile_bounds(bbox: List[float], tile_deg: float) -> Iterable[Tuple[float, float, float, float]]:
    minx, miny, maxx, maxy = [float(v) for v in bbox]
    x = minx
    while x < maxx - 1e-12:
        x1 = min(x + tile_deg, maxx)
        y = miny
        while y < maxy - 1e-12:
            y1 = min(y + tile_deg, maxy)
            yield (x, y, x1, y1)
            y = y1
        x = x1


def _download_ssurgo_mapunit_polygons(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> gpd.GeoDataFrame:
    scfg = cfg["infiltration"].get("soils", {})
    bbox = bbox_buffer_km(geo_meta["bbox"], float(scfg.get("buffer_km", 5.0)))
    tile_deg = float(scfg.get("wfs_tile_degrees", 0.20))
    if tile_deg <= 0:
        raise RuntimeError("infiltration.soils.wfs_tile_degrees must be > 0")
    wfs_url = str(scfg.get("wfs_url", USDA_SDA_WFS))
    wfs_version = str(scfg.get("wfs_version", USDA_SDA_WFS_VERSION))
    cache_dir = (
        paths.cache
        / "ssurgo"
        / f"{_bbox_signature(bbox, tile_deg)}_wfs{wfs_version.replace('.', '')}"
    )
    ensure_dir(cache_dir)
    merged_file = cache_dir / "ssurgo_mapunit_polygons.gpkg"

    if file_ready(merged_file, 1024):
        log(f"Reusing cached SSURGO mapunit polygons: {merged_file}")
        return gpd.read_file(merged_file)

    log(
        f"USDA SSURGO spatial source: WFS {wfs_version} "
        f"(cache={cache_dir})"
    )

    pieces: List[gpd.GeoDataFrame] = []
    for idx, (x0, y0, x1, y1) in enumerate(_wfs_tile_bounds(bbox, tile_deg), start=1):
        tile = cache_dir / f"mapunit_{idx:03d}.gml"
        if not file_ready(tile, 256):
            # Use USDA Soil Data Access WFS 1.0.0. For EPSG:4326/GML2 this
            # preserves the requested x=longitude, y=latitude coordinate order.
            # WFS 1.1.0 was verified independently to return the same Tampa
            # geometries with the axes interpreted as latitude/longitude by
            # the installed GeoPandas/GDAL stack.
            spatial_filter = (
                "<Filter><BBOX><PropertyName>Geometry</PropertyName>"
                "<Box srsName='EPSG:4326'><coordinates>"
                f"{x0:.8f},{y0:.8f} {x1:.8f},{y1:.8f}"
                "</coordinates></Box></BBOX></Filter>"
            )
            params = {
                "SERVICE": "WFS",
                "VERSION": wfs_version,
                "REQUEST": "GetFeature",
                "TYPENAME": "MapunitPoly",
                "SRSNAME": "EPSG:4326",
                "FILTER": spatial_filter,
                "OUTPUTFORMAT": "GML2",
                "MAXFEATURES": "250000",
            }
            r = retry_get(wfs_url, params=params, timeout=int(scfg.get("timeout_seconds", 180)), tries=4)
            ctype = str(r.headers.get("content-type", "")).lower()
            if "exception" in r.text[:500].lower():
                raise RuntimeError(f"USDA SSURGO WFS exception for tile {(x0,y0,x1,y1)}: {r.text[:1500]}")
            tile.write_bytes(r.content)
        try:
            g = gpd.read_file(tile)
        except Exception as exc:
            raise RuntimeError(f"Could not read SSURGO WFS tile {tile}: {exc}") from exc
        if not g.empty:
            if g.crs is None:
                g = g.set_crs(4326)
            else:
                g = g.to_crs(4326)
            pieces.append(g)

    if not pieces:
        raise RuntimeError("USDA SSURGO WFS returned no MapunitPoly features for the model domain.")
    gdf = gpd.GeoDataFrame(pd.concat(pieces, ignore_index=True), crs=4326)
    cols_lower = {str(c).lower(): c for c in gdf.columns}
    if "mukey" not in cols_lower:
        raise RuntimeError(f"SSURGO MapunitPoly response lacks mukey. Columns: {list(gdf.columns)}")
    if cols_lower["mukey"] != "mukey":
        gdf = gdf.rename(columns={cols_lower["mukey"]: "mukey"})
    gdf["mukey"] = gdf["mukey"].astype(str)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    # Tile edges can return duplicate polygon features. Deduplicate exactly.
    gdf["_geom_wkb"] = gdf.geometry.apply(lambda geom: geom.wkb_hex)
    gdf = gdf.drop_duplicates(subset=["mukey", "_geom_wkb"]).drop(columns="_geom_wkb")

    # Keep the WFS buffer in the downloaded dataset; HydroMT will clip later.
    gdf.to_file(merged_file, driver="GPKG")
    log(f"Cached SSURGO mapunit polygons: {merged_file} ({len(gdf)} features)")
    return gdf


def _sda_query_dataframe(sql: str, url: str = USDA_SDA_TABULAR_API, timeout: int = 180) -> pd.DataFrame:
    last_exc: Optional[Exception] = None
    for attempt in range(4):
        try:
            r = requests.post(url, data={"query": sql, "format": "JSON+COLUMNNAME"}, timeout=timeout)
            r.raise_for_status()
            js = r.json()
            table = js.get("Table", [])
            if not table:
                return pd.DataFrame()
            header = [str(v) for v in table[0]]
            return pd.DataFrame(table[1:], columns=header)
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                wait = 4 * (attempt + 1)
                log(f"USDA SDA tabular query failed; retrying in {wait}s: {exc}")
                time.sleep(wait)
    raise RuntimeError(f"USDA SDA tabular query failed after retries: {last_exc}\nSQL:\n{sql[:3000]}")


def _normalize_hsg(value: Any, dual_policy: str, missing_group: str) -> int:
    s = str(value or "").strip().upper().replace(" ", "")
    valid = [c for c in s.split("/") if c in {"A", "B", "C", "D"}]
    if not valid:
        valid = [missing_group]
    if len(valid) > 1:
        if dual_policy == "drained":
            letter = valid[0]
        elif dual_policy == "undrained":
            letter = valid[-1]
        else:
            raise RuntimeError("hsg_dual_policy must be 'drained' or 'undrained'")
    else:
        letter = valid[0]
    return {"A": 1, "B": 2, "C": 3, "D": 4}.get(letter, 4)


def _component_topsoil_ksat(rows: pd.DataFrame, depth_cm: float, method: str) -> float:
    if rows.empty:
        return float("nan")
    zt = pd.to_numeric(rows.get("hzdept_r"), errors="coerce")
    zb = pd.to_numeric(rows.get("hzdepb_r"), errors="coerce")
    k = pd.to_numeric(rows.get("ksat_r"), errors="coerce")
    top = np.maximum(zt.to_numpy(dtype=float), 0.0)
    bot = np.minimum(zb.to_numpy(dtype=float), depth_cm)
    thick = np.maximum(bot - top, 0.0)
    kval = k.to_numpy(dtype=float)
    good = np.isfinite(kval) & np.isfinite(thick) & (thick > 0)
    if not np.any(good):
        # Fall back to the shallowest horizon with Ksat when the top-depth
        # window is unavailable for a component.
        good_any = np.isfinite(kval) & np.isfinite(zt.to_numpy(dtype=float))
        if not np.any(good_any):
            return float("nan")
        ii = np.where(good_any)[0][np.argmin(zt.to_numpy(dtype=float)[good_any])]
        return float(max(kval[ii], 0.0))
    kval = np.maximum(kval[good], 0.0)
    thick = thick[good]
    if method == "arithmetic":
        return float(np.sum(kval * thick) / np.sum(thick))
    if method == "geometric":
        if np.any(kval <= 0):
            return 0.0
        return float(np.exp(np.sum(np.log(kval) * thick) / np.sum(thick)))
    if method == "harmonic":
        if np.any(kval <= 0):
            return 0.0
        return float(np.sum(thick) / np.sum(thick / kval))
    raise RuntimeError("ksat_horizon_aggregation must be harmonic, geometric, or arithmetic")


def _aggregate_ssurgo_properties(raw: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    scfg = cfg["infiltration"].get("soils", {})
    dual_policy = str(scfg.get("hsg_dual_policy", "undrained")).lower()
    missing_group = str(scfg.get("hsg_missing_group", "D")).upper()
    depth_cm = float(scfg.get("ksat_topsoil_depth_cm", 30.0))
    k_method = str(scfg.get("ksat_horizon_aggregation", "harmonic")).lower()
    missing_k = float(scfg.get("ksat_missing_um_s", 0.0))

    if raw.empty:
        raise RuntimeError("No SSURGO component/horizon records were returned.")
    for col in ["mukey", "cokey", "comppct_r", "hydgrp", "hzdept_r", "hzdepb_r", "ksat_r"]:
        if col not in raw.columns:
            raise RuntimeError(f"SSURGO query lacks required field {col!r}. Columns: {list(raw.columns)}")

    rows = []
    for mukey, mu in raw.groupby(raw["mukey"].astype(str), sort=False):
        comps = []
        for cokey, comp_rows in mu.groupby(mu["cokey"].astype(str), sort=False):
            first = comp_rows.iloc[0]
            pct = pd.to_numeric(pd.Series([first.get("comppct_r")]), errors="coerce").iloc[0]
            pct = float(pct) if np.isfinite(pct) and pct > 0 else 0.0
            hsg = _normalize_hsg(first.get("hydgrp"), dual_policy, missing_group)
            ksat = _component_topsoil_ksat(comp_rows, depth_cm, k_method)
            comps.append((pct, hsg, ksat))
        if not comps:
            rows.append({"mukey": str(mukey), "hsg": 4, "ksat_um_s": missing_k})
            continue
        pcts = np.array([c[0] for c in comps], dtype=float)
        if np.sum(pcts) <= 0:
            pcts[:] = 1.0
        # Dominant-component-percentage HSG vote.
        hsg_votes = {h: float(np.sum([w for w, hh, _ in comps if hh == h])) for h in (1, 2, 3, 4)}
        hsg = max(hsg_votes, key=lambda h: (hsg_votes[h], h))
        kval = np.array([c[2] for c in comps], dtype=float)
        good = np.isfinite(kval)
        if np.any(good):
            wg = pcts[good]
            if np.sum(wg) <= 0:
                wg = np.ones_like(wg)
            ksat = float(np.sum(kval[good] * wg) / np.sum(wg))
        else:
            ksat = missing_k
        rows.append({"mukey": str(mukey), "hsg": int(hsg), "ksat_um_s": max(float(ksat), 0.0)})
    return pd.DataFrame(rows)


def prepare_ssurgo_infiltration_rasters(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths, model_crs: str
) -> Tuple[Path, Path, Path]:
    """Create HSG and Ksat rasters from official USDA SSURGO web services.

    HSG is encoded A/B/C/D -> 1/2/3/4. Dual groups default to the undrained
    member (e.g. A/D -> D), consistent with the conservative HEC-HMS example.
    Ksat is aggregated over the configured top-soil depth and is intentionally
    left in SSURGO's native micrometers/second: hydromt_sfincs 1.0.3 itself
    multiplies Ksat by 3.6 to obtain mm/hr in its CN-recovery workflow.

    Resume behavior
    ---------------
    Final rasters and tabular Soil Data Access responses use signatures derived
    from the domain and preprocessing options. Restarting the same command
    therefore reuses completed work, while changing HSG/Ksat options creates a
    new preprocessing signature instead of silently reusing stale rasters.
    """
    scfg = cfg["infiltration"].get("soils", {})
    res = float(scfg.get("raster_resolution_m", 30.0))
    if res <= 0:
        raise RuntimeError("infiltration.soils.raster_resolution_m must be > 0")
    depth_cm = float(scfg.get("ksat_topsoil_depth_cm", 30.0))
    if depth_cm <= 0:
        raise RuntimeError("infiltration.soils.ksat_topsoil_depth_cm must be > 0")

    soil_sig = _bbox_signature(
        geo_meta["bbox"],
        model_crs,
        res,
        scfg.get("raster_buffer_km", 3.0),
        scfg.get("buffer_km", 5.0),
        scfg.get("wfs_tile_degrees", 0.20),
        # The derived HSG/Ksat rasters depend on how USDA WFS geometries are
        # decoded. WFS 1.0.0 and 1.1.0 have different EPSG:4326 axis behavior
        # in the installed GDAL/GeoPandas stack, so the WFS version must be
        # part of the preprocessing cache signature.
        scfg.get("wfs_version", USDA_SDA_WFS_VERSION),
        scfg.get("hsg_dual_policy", "undrained"),
        scfg.get("hsg_missing_group", "D"),
        depth_cm,
        scfg.get("ksat_horizon_aggregation", "harmonic"),
        scfg.get("ksat_missing_um_s", 0.0),
    )

    hsg_tif = paths.data / f"ssurgo_hsg_{res:g}m_{soil_sig}.tif"
    ksat_tif = paths.data / f"ssurgo_ksat_um_s_{res:g}m_{soil_sig}.tif"
    prop_path = paths.gis / f"ssurgo_mapunit_infiltration_{soil_sig}.gpkg"
    layer_name = "ssurgo_mapunit_infiltration"

    if file_ready(hsg_tif, 1024) and file_ready(ksat_tif, 1024):
        log(f"Reusing SSURGO infiltration rasters: HSG={hsg_tif}, Ksat={ksat_tif}")
        return hsg_tif, ksat_tif, prop_path

    gdf = _download_ssurgo_mapunit_polygons(cfg, geo_meta, paths)
    mukeys = sorted(set(gdf["mukey"].astype(str)))
    if not mukeys:
        raise RuntimeError("No SSURGO mukeys found in downloaded polygons.")

    # The component/horizon response depends only on the set of mapunit keys,
    # so cache it independently of the later aggregation choices.
    mukey_hash = hashlib.sha1("|".join(mukeys).encode("utf-8")).hexdigest()[:16]
    raw_csv = paths.cache / "ssurgo" / "tabular" / f"component_horizon_{mukey_hash}.csv"
    ensure_dir(raw_csv.parent)
    if file_ready(raw_csv, 256):
        log(f"Reusing cached SSURGO component/horizon attributes: {raw_csv}")
        raw = pd.read_csv(raw_csv, dtype=str)
    else:
        batch_size = int(scfg.get("tabular_batch_mukeys", 250))
        if batch_size <= 0 or batch_size > 1000:
            raise RuntimeError("infiltration.soils.tabular_batch_mukeys must be between 1 and 1000")
        tab_url = str(scfg.get("tabular_url", USDA_SDA_TABULAR_API))
        raw_parts = []
        for i in range(0, len(mukeys), batch_size):
            batch = mukeys[i : i + batch_size]
            in_clause = ",".join("'" + k.replace("'", "''") + "'" for k in batch)
            sql = f"""
SELECT
    c.mukey,
    c.cokey,
    c.comppct_r,
    c.hydgrp,
    ch.chkey,
    ch.hzdept_r,
    ch.hzdepb_r,
    ch.ksat_r
FROM component AS c
LEFT OUTER JOIN chorizon AS ch ON c.cokey = ch.cokey
WHERE c.mukey IN ({in_clause})
""".strip()
            df = _sda_query_dataframe(
                sql,
                url=tab_url,
                timeout=int(scfg.get("timeout_seconds", 180)),
            )
            if not df.empty:
                raw_parts.append(df)
            log(
                f"SSURGO tabular attributes: batch "
                f"{i // batch_size + 1}/{math.ceil(len(mukeys) / batch_size)}"
            )
        if not raw_parts:
            raise RuntimeError("SSURGO SDA returned no component/horizon attributes.")
        raw = pd.concat(raw_parts, ignore_index=True)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw.to_csv(raw_csv, index=False)
        log(f"Cached SSURGO component/horizon attributes: {raw_csv}")

    raw.columns = [str(c).lower() for c in raw.columns]
    raw.to_csv(paths.data / f"ssurgo_component_horizon_raw_{soil_sig}.csv", index=False)

    props = _aggregate_ssurgo_properties(raw, cfg)
    props.to_csv(paths.data / f"ssurgo_mapunit_properties_{soil_sig}.csv", index=False)
    prop = gdf.merge(props, on="mukey", how="left")
    miss_hsg = float(prop["hsg"].isna().mean())
    miss_ksat = float(pd.to_numeric(prop["ksat_um_s"], errors="coerce").isna().mean())
    prop["hsg"] = prop["hsg"].fillna(
        _normalize_hsg(
            scfg.get("hsg_missing_group", "D"),
            str(scfg.get("hsg_dual_policy", "undrained")).lower(),
            str(scfg.get("hsg_missing_group", "D")).upper(),
        )
    ).astype(int)
    prop["ksat_um_s"] = (
        pd.to_numeric(prop["ksat_um_s"], errors="coerce")
        .fillna(float(scfg.get("ksat_missing_um_s", 0.0)))
        .clip(lower=0.0)
    )
    prop = prop.to_crs(model_crs)
    if prop_path.exists():
        prop_path.unlink()
    prop[["mukey", "hsg", "ksat_um_s", "geometry"]].to_file(
        prop_path, layer=layer_name, driver="GPKG"
    )

    domain = gpd.read_file(geo_meta["domain_path"])
    if domain.crs is None:
        domain = domain.set_crs(4326)
    domain = domain.to_crs(model_crs)
    geom = domain.geometry.union_all().buffer(
        float(scfg.get("raster_buffer_km", 3.0)) * 1000.0
    )
    xmin, ymin, xmax, ymax = _snap_bounds(geom.bounds, res)

    require_executable("gdal_rasterize")

    # GDAL versions available on some HPC systems (including the Della build
    # used by this workflow) do not support `gdal_rasterize -overwrite`.
    # Make rasterization idempotent by removing a stale/incomplete target first,
    # then invoke gdal_rasterize using only broadly supported options.
    if not file_ready(hsg_tif, 1024):
        if hsg_tif.exists():
            hsg_tif.unlink()
        run_cmd([
            "gdal_rasterize", "-l", layer_name,
            "-a", "hsg", "-init", "0", "-a_nodata", "0",
            "-ot", "Byte", "-tr", str(res), str(res),
            "-te", str(xmin), str(ymin), str(xmax), str(ymax),
            "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
            str(prop_path), str(hsg_tif),
        ])

    if not file_ready(ksat_tif, 1024):
        if ksat_tif.exists():
            ksat_tif.unlink()
        run_cmd([
            "gdal_rasterize", "-l", layer_name,
            "-a", "ksat_um_s", "-init", "0", "-a_nodata", "0",
            "-ot", "Float32", "-tr", str(res), str(res),
            "-te", str(xmin), str(ymin), str(xmax), str(ymax),
            "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES",
            str(prop_path), str(ksat_tif),
        ])

    report = {
        "source": "USDA SSURGO via Soil Data Access",
        "soil_preprocessing_signature": soil_sig,
        "n_mapunit_polygons": int(len(prop)),
        "n_unique_mukey": int(len(mukeys)),
        "missing_hsg_join_fraction": miss_hsg,
        "missing_ksat_join_fraction": miss_ksat,
        "hsg_dual_policy": str(scfg.get("hsg_dual_policy", "undrained")),
        "hsg_missing_group": str(scfg.get("hsg_missing_group", "D")),
        "ksat_topsoil_depth_cm": depth_cm,
        "ksat_horizon_aggregation": str(
            scfg.get("ksat_horizon_aggregation", "harmonic")
        ),
        "ksat_units_input_to_hydromt": "micrometers per second",
        "note_ksat_v103": (
            "hydromt_sfincs 1.0.3 multiplies input Ksat by 3.6 internally "
            "to convert micrometers/second to mm/hour"
        ),
        "raster_resolution_m": res,
        "hsg_path": str(hsg_tif),
        "ksat_path": str(ksat_tif),
    }
    write_json(report, paths.reports / "ssurgo_infiltration_report.json")
    log(
        "SSURGO infiltration rasters ready: "
        f"HSG={hsg_tif}, Ksat={ksat_tif} | "
        f"missing HSG={miss_hsg:.3%}, missing Ksat={miss_ksat:.3%}"
    )
    return hsg_tif, ksat_tif, prop_path


def prepare_infiltration_inputs(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths, model_crs: str
) -> Optional[Dict[str, Path]]:
    if not infiltration_enabled(cfg): return None
    icfg = cfg.get("infiltration", {})
    method = str(icfg.get("method", "cn_with_recovery")).lower()
    source = str(icfg.get("source", "nlcd_ssurgo" if method == "cn_with_recovery" else "gcn250")).lower()
    if method == "cn":
        if source == "gcn250":
            cond = _normalize_gcn250_condition(icfg.get("antecedent_moisture", "avg"))
            gcn = download_gcn250(cfg, paths)
            log(
                "Infiltration specification: ordinary SCS CN using "
                f"GCN250 ({cond}) from authoritative Figshare source"
            )
            return {"gcn": gcn}
        if source == "file":
            cn = copy_user_file(icfg.get("path"), paths.data / "user_curve_number.tif", "infiltration.path")
            log(f"Infiltration specification: ordinary SCS CN using user raster {cn}")
            return {"gcn": cn}
        raise RuntimeError("infiltration.method=cn supports source: gcn250 or file")
    if method != "cn_with_recovery":
        raise RuntimeError("infiltration.method must be cn_with_recovery or cn")
    if source not in {"nlcd_ssurgo", "annual_nlcd_ssurgo"}:
        raise RuntimeError("cn_with_recovery currently supports source: nlcd_ssurgo")
    condition = str(icfg.get("cn_condition", "average")).lower(); effective = float(icfg.get("effective", 0.50))
    if not (0.0 <= effective <= 1.0): raise RuntimeError("infiltration.effective must be between 0 and 1")
    if condition not in {"average", "avg", "ii", "cn2"}:
        raise RuntimeError("Bundled NLCD/HSG lookup is CN-II; use average or provide a custom lookup")
    lc = download_annual_nlcd_landcover(cfg, geo_meta, paths)
    hsg, ksat, _ = prepare_ssurgo_infiltration_rasters(cfg, geo_meta, paths, model_crs)
    lookup_cfg = icfg.get("cn_lookup", {}); custom = lookup_cfg.get("path")
    if custom:
        table = resolve_user_path(custom, "infiltration.cn_lookup.path")
    else:
        table = write_default_nlcd_cn_table(paths.data / "nlcd_hsg_cn_ii_lookup.csv")
    log(f"Infiltration specification: CN with recovery | Annual NLCD + SSURGO | effective={effective:.3f}")
    return {"lulc": lc, "hsg": hsg, "ksat": ksat, "cn_table": table}


# -----------------------------------------------------------------------------
# NOAA CO-OPS
# -----------------------------------------------------------------------------


def fetch_noaa_stations() -> pd.DataFrame:
    params = {"type": "waterlevels", "format": "json"}
    r = retry_get(NOAA_MDAPI_STATIONS, params=params, timeout=120)
    js = r.json()
    stations = js.get("stations", js.get("Stations", []))
    rows = []
    for st in stations:
        sid = str(st.get("id") or st.get("stationId") or st.get("station_id") or st.get("station"))
        name = st.get("name") or st.get("stationName") or ""
        lat = st.get("lat") or st.get("latitude")
        lon = st.get("lng") or st.get("lon") or st.get("longitude")
        try:
            rows.append({"station": sid, "name": name, "lat": float(lat), "lon": float(lon)})
        except Exception:
            continue
    if not rows:
        raise RuntimeError("No NOAA CO-OPS water-level stations returned by MDAPI.")
    return pd.DataFrame(rows).drop_duplicates("station")


def download_noaa_water_level(
    station: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    datum: str,
    units: str,
    product: str = "water_level",
    time_zone: str = "gmt",
) -> pd.Series:
    params = {
        "begin_date": noaa_time(start),
        "end_date": noaa_time(end),
        "station": station,
        "product": product,
        "datum": datum,
        "time_zone": time_zone,
        "units": units,
        "format": "json",
        "application": "sfincs_agent",
    }
    r = retry_get(NOAA_DATA_API, params=params, timeout=120)
    js = r.json()
    if "error" in js:
        raise RuntimeError(f"NOAA API error for station {station}: {js['error']}")
    data = js.get("data", [])
    if not data:
        raise RuntimeError(f"NOAA API returned no water-level data for station {station}")
    idx = []
    vals = []
    for row in data:
        try:
            t = pd.Timestamp(row["t"], tz="UTC")
            v = float(row["v"])
            idx.append(t)
            vals.append(v)
        except Exception:
            continue
    if not vals:
        raise RuntimeError(f"No parsable NOAA values for station {station}")
    s = pd.Series(vals, index=pd.DatetimeIndex(idx), name=str(station)).sort_index()
    return s[~s.index.duplicated(keep="first")]


def select_boundary_noaa(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Path, Path, pd.DataFrame]:
    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])
    nc = cfg["noaa_boundary"]
    stations = fetch_noaa_stations()
    pts = gpd.read_file(geo_meta["bnd_points_path"]).to_crs(4326)
    ts_list = []
    loc_rows = []
    report_rows = []
    expected_index = pd.date_range(start, end, freq=f"{int(nc['interval_minutes'])}min")

    for forcing_zero, (_, row) in enumerate(pts.iterrows()):
        p: Point = row.geometry
        point_label = str(row["ID"])
        forcing_id = forcing_zero + 1
        cand = stations.copy()
        cand["distance_km"] = cand.apply(lambda r: haversine_km(p.x, p.y, r["lon"], r["lat"]), axis=1)
        cand = cand.sort_values("distance_km")
        cand = cand[cand["distance_km"] <= float(nc["max_station_distance_km"])]
        selected = None
        last_err = None
        for _, st in cand.iterrows():
            try:
                s = download_noaa_water_level(
                    st["station"], start, end, datum=nc["datum"], units=nc["units"],
                    product=nc.get("product", "water_level"), time_zone=nc.get("time_zone", "gmt")
                )
                s = s.reindex(expected_index).interpolate(limit_direction="both")
                missing_frac = float(s.isna().mean())
                if missing_frac <= float(nc["max_missing_fraction"]):
                    selected = (st, s)
                    break
            except Exception as exc:
                last_err = exc
                continue
        if selected is None:
            raise RuntimeError(f"No valid NOAA station found for boundary point {point_label}. Last error: {last_err}")
        st, s = selected
        s.name = forcing_id
        ts_list.append(s)
        loc_rows.append({
            "ID": forcing_id,
            "name": point_label,
            "boundary_label": point_label,
            "noaa_station": st["station"],
            "geometry": p,
        })
        report_rows.append({
            "forcing_id": forcing_id,
            "point_id": point_label,
            "point_lon": p.x,
            "point_lat": p.y,
            "station": st["station"],
            "station_name": st["name"],
            "station_lon": st["lon"],
            "station_lat": st["lat"],
            "distance_km": st["distance_km"],
            "missing_fraction_after_interp": float(s.isna().mean()),
        })

    ts = pd.concat(ts_list, axis=1)
    ts.index.name = "time"
    ts_path = paths.data / "noaa_boundary_timeseries.csv"
    loc_path = paths.gis / "noaa_boundary_locations.geojson"
    write_hydromt_timeseries_csv(ts, ts_path)
    gpd.GeoDataFrame(loc_rows, crs=4326).to_file(loc_path, driver="GeoJSON")
    pd.DataFrame(report_rows).to_csv(paths.reports / "noaa_boundary_station_selection.csv", index=False)
    log(f"NOAA boundary forcing: wrote {ts_path} and {loc_path}")
    return ts_path, loc_path, pd.DataFrame(report_rows)


def select_validation_noaa(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Optional[Path]:
    if not cfg.get("noaa_validation", {}).get("enabled", True):
        return None
    domain = gpd.read_file(geo_meta["domain_path"]).to_crs(4326)
    stations = fetch_noaa_stations()
    gdf = gpd.GeoDataFrame(
        stations,
        geometry=[Point(xy) for xy in zip(stations["lon"], stations["lat"])],
        crs=4326,
    )
    inside = gpd.sjoin(gdf, domain[["geometry"]], predicate="within", how="inner")
    if inside.empty:
        log("No NOAA validation stations inside polygon. Observation file will not be created.")
        return None
    inside = inside.drop(columns=[c for c in inside.columns if c.startswith("index_")], errors="ignore")
    inside["ID"] = inside["station"].astype(str)
    inside["name"] = inside["station"].astype(str) + "_" + inside["name"].astype(str).str.replace(" ", "_", regex=False)
    out = paths.gis / "noaa_validation_obs.geojson"
    inside[["ID", "name", "station", "geometry"]].to_file(out, driver="GeoJSON")
    inside.drop(columns="geometry").to_csv(paths.reports / "noaa_validation_stations_inside_domain.csv", index=False)
    log(f"NOAA validation stations inside domain: {len(inside)}")
    return out


# -----------------------------------------------------------------------------
# USGS discharge
# -----------------------------------------------------------------------------


def fetch_usgs_sites(bbox: List[float], parameter_cd: str) -> pd.DataFrame:
    """
    Query USGS NWIS site service for stream gages with instantaneous discharge.

    Important:
    USGS bBox accepts west,south,east,north with up to six decimal places.
    Eight decimal places can trigger HTTP 400 on the site service.
    """
    bbox6 = [round(float(x), 6) for x in bbox]

    params = {
        "format": "rdb",
        "bBox": ",".join(f"{x:.6f}" for x in bbox6),
        "parameterCd": str(parameter_cd),
        "hasDataTypeCd": "iv",
        "siteStatus": "all",
        "siteType": "ST",
    }

    log("USGS site query parameters:")
    log(str(params))

    r = retry_get(USGS_SITE_API, params=params, timeout=120)

    lines = [ln for ln in r.text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) < 3:
        return pd.DataFrame()

    from io import StringIO

    # RDB has a header row, then a type-width row, then data rows.
    txt = "\n".join([lines[0]] + lines[2:])
    df = pd.read_csv(StringIO(txt), sep="\t", dtype=str)

    lat_col = "dec_lat_va"
    lon_col = "dec_long_va"

    if lat_col not in df.columns or lon_col not in df.columns or "site_no" not in df.columns:
        log(f"USGS site response did not include expected columns. Columns: {list(df.columns)}")
        return pd.DataFrame()

    out = pd.DataFrame({
        "site_no": df["site_no"].astype(str),
        "station_nm": df.get("station_nm", "").astype(str),
        "lat": pd.to_numeric(df[lat_col], errors="coerce"),
        "lon": pd.to_numeric(df[lon_col], errors="coerce"),
    }).dropna(subset=["lat", "lon"])

    return out.drop_duplicates("site_no")

def download_usgs_discharge(site_no: str, start: pd.Timestamp, end: pd.Timestamp, parameter_cd: str, unit_mult: float) -> pd.Series:
    params = {
        "format": "json",
        "sites": site_no,
        "parameterCd": parameter_cd,
        "startDT": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDT": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "siteStatus": "all",
    }
    r = retry_get(USGS_IV_API, params=params, timeout=120)
    js = r.json()
    tss = js.get("value", {}).get("timeSeries", [])
    if not tss:
        raise RuntimeError(f"No USGS timeSeries for site {site_no}")
    vals = []
    idx = []
    for ts in tss:
        for vv in ts.get("values", []):
            for v in vv.get("value", []):
                try:
                    val = float(v["value"]) * unit_mult
                    t = pd.Timestamp(v["dateTime"]).tz_convert("UTC")
                    vals.append(val)
                    idx.append(t)
                except Exception:
                    continue
    if not vals:
        raise RuntimeError(f"No parsable USGS discharge for site {site_no}")
    s = pd.Series(vals, index=pd.DatetimeIndex(idx), name=str(site_no)).sort_index()
    return s[~s.index.duplicated(keep="first")]


def select_usgs_discharge(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Optional[Path], Optional[Path]]:
    rcfg = cfg.get("usgs_rivers", {})
    if not rcfg.get("enabled", True):
        return None, None
    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])
    bbox = bbox_buffer_km(geo_meta["bbox"], float(rcfg.get("max_gage_distance_km", 50)))
    try:
        sites = fetch_usgs_sites(bbox, str(rcfg.get("parameter_cd", "00060")))
    except Exception as exc:
        log(f"USGS site query failed. Skipping river discharge forcing for this run: {exc}")
        return None, None

    if sites.empty:
        log("No USGS discharge gages found near domain. Skipping river discharge forcing.")
        return None, None
    max_gages = int(rcfg.get("max_gages", 12))
    domain_cent = geo_meta["centroid_lonlat"]
    sites["distance_to_domain_centroid_km"] = sites.apply(
        lambda r: haversine_km(domain_cent[0], domain_cent[1], r["lon"], r["lat"]), axis=1
    )
    sites = sites.sort_values("distance_to_domain_centroid_km").head(max_gages)
    expected_index = pd.date_range(start, end, freq=f"{int(rcfg.get('interval_minutes', 15))}min")
    series = []
    loc_rows = []
    rep_rows = []
    for _, st in sites.iterrows():
        try:
            s = download_usgs_discharge(
                st["site_no"], start, end, str(rcfg.get("parameter_cd", "00060")), float(rcfg.get("unit_mult", 0.028316846592))
            )
            s = s.reindex(expected_index).interpolate(limit_direction="both")
            miss = float(s.isna().mean())
            if miss <= float(rcfg.get("max_missing_fraction", 0.25)):
                sid = str(st["site_no"])
                forcing_id = len(series) + 1
                s.name = forcing_id
                series.append(s)
                loc_rows.append({
                    "ID": forcing_id,
                    "name": sid,
                    "usgs_site_no": sid,
                    "geometry": Point(float(st["lon"]), float(st["lat"])),
                })
                rep_rows.append({
                    **st.to_dict(),
                    "forcing_id": forcing_id,
                    "missing_fraction_after_interp": miss,
                })
        except Exception as exc:
            log(f"Skipping USGS site {st['site_no']}: {exc}")
    if not series:
        log("No USGS gages passed missing-data filter. Skipping river discharge forcing.")
        return None, None
    ts = pd.concat(series, axis=1)
    ts.index.name = "time"
    ts_path = paths.data / "usgs_discharge_m3s.csv"
    loc_path = paths.gis / "usgs_discharge_locations.geojson"
    write_hydromt_timeseries_csv(ts, ts_path)
    gpd.GeoDataFrame(loc_rows, crs=4326).to_file(loc_path, driver="GeoJSON")
    pd.DataFrame(rep_rows).to_csv(paths.reports / "usgs_discharge_station_selection.csv", index=False)
    log(f"USGS discharge forcing: wrote {ts_path} and {loc_path}")
    return ts_path, loc_path


# -----------------------------------------------------------------------------
# Flexible point forcing providers
# -----------------------------------------------------------------------------


def _copy_point_forcing_pair(ts_value: Any, loc_value: Any, paths: Paths, stem: str) -> Tuple[Path, Path]:
    ts = copy_user_file(ts_value, paths.data / f"{stem}_timeseries.csv", f"{stem}.timeseries")
    loc = copy_user_file(loc_value, paths.gis / f"{stem}_locations.geojson", f"{stem}.locations")
    sanitize_hydromt_timeseries_csv(ts)
    return ts, loc


def prepare_ocean_boundary(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Path, Path]:
    ocfg = forcing_cfg(cfg).get("ocean_boundary", {})
    source = str(ocfg.get("source", "noaa_coops")).lower()
    if source in {"noaa", "noaa_coops", "coops"}:
        # Merge new provider options over legacy NOAA defaults.
        cfg2 = dict(cfg); cfg2["noaa_boundary"] = {**cfg.get("noaa_boundary", {}), **ocfg}
        ts, loc, _ = select_boundary_noaa(cfg2, geo_meta, paths)
        return ts, loc
    if source == "file":
        return _copy_point_forcing_pair(ocfg.get("timeseries"), ocfg.get("locations", geo_meta.get("bnd_points_path")), paths, "ocean_boundary_user")
    if source == "constant":
        value = float(ocfg.get("value_m", 0.0))
        loc_src = Path(geo_meta["bnd_points_path"])
        gdf = gpd.read_file(loc_src).reset_index(drop=True)
        ids = list(range(1, len(gdf)+1)); gdf["ID"] = ids
        start = parse_time(cfg["simulation"]["start"]); end = parse_time(cfg["simulation"]["end"])
        idx = pd.DatetimeIndex([start, end])
        df = pd.DataFrame({i: [value, value] for i in ids}, index=idx)
        ts = paths.data / "ocean_boundary_constant.csv"; loc = paths.gis / "ocean_boundary_constant.geojson"
        write_hydromt_timeseries_csv(df, ts); gdf.to_file(loc, driver="GeoJSON")
        return ts, loc
    raise RuntimeError(f"Unsupported forcing.ocean_boundary.source={source!r}; use noaa_coops, file, or constant")


def prepare_river_forcing(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Optional[Path], Optional[Path]]:
    rcfg = forcing_cfg(cfg).get("river", {})
    source = str(rcfg.get("source", "usgs_auto")).lower()
    if source == "none": return None, None
    if source in {"usgs_auto", "usgs"}:
        cfg2 = dict(cfg); cfg2["usgs_rivers"] = {**cfg.get("usgs_rivers", {}), **rcfg, "enabled": True}
        return select_usgs_discharge(cfg2, geo_meta, paths)
    if source == "file":
        loc = rcfg.get("locations") or geo_meta.get("river_points_path")
        return _copy_point_forcing_pair(rcfg.get("timeseries"), loc, paths, "river_user")
    if source == "usgs_sites":
        sites_cfg = rcfg.get("sites", [])
        if not sites_cfg: raise RuntimeError("forcing.river.source=usgs_sites requires a non-empty sites list")
        start=parse_time(cfg["simulation"]["start"]); end=parse_time(cfg["simulation"]["end"])
        expected = pd.date_range(start, end, freq=f"{int(rcfg.get('interval_minutes',15))}min")
        series=[]; rows=[]
        for item in sites_cfg:
            sid = str(item["site_no"] if isinstance(item, dict) else item)
            srs = download_usgs_discharge(sid, start, end, str(rcfg.get("parameter_cd","00060")), float(rcfg.get("unit_mult",0.028316846592)))
            srs = srs.reindex(expected).interpolate(limit_direction="both")
            if float(srs.isna().mean()) > float(rcfg.get("max_missing_fraction",0.25)):
                raise RuntimeError(f"Explicit USGS site {sid} exceeds max_missing_fraction")
            if not isinstance(item, dict) or "lon" not in item or "lat" not in item:
                raise RuntimeError(f"Explicit USGS site {sid} requires lon and lat in V1")
            fid=len(series)+1; srs.name=fid; series.append(srs)
            rows.append({"ID":fid,"usgs_site_no":sid,"name":sid,"geometry":Point(float(item["lon"]),float(item["lat"]))})
        ts=paths.data/"usgs_explicit_discharge_m3s.csv"; loc=paths.gis/"usgs_explicit_discharge_locations.geojson"
        write_hydromt_timeseries_csv(pd.concat(series,axis=1),ts); gpd.GeoDataFrame(rows,crs=4326).to_file(loc,driver="GeoJSON")
        return ts,loc
    raise RuntimeError(f"Unsupported forcing.river.source={source!r}; use usgs_auto, usgs_sites, file, or none")


def prepare_validation_observations(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Optional[Path]:
    vcfg = cfg.get("evaluation", {}).get("observations", {})
    source = str(vcfg.get("source", "noaa_coops" if cfg.get("noaa_validation", {}).get("enabled", True) else "none")).lower()
    if source == "none": return None
    if source in {"noaa", "noaa_coops", "coops"}:
        return select_validation_noaa(cfg, geo_meta, paths)
    if source == "file":
        loc_value = vcfg.get("locations") or geo_meta.get("observation_points_path")
        # Keep the established filename so snapping/evaluation code remains backward compatible.
        loc = copy_user_file(loc_value, paths.gis / "noaa_validation_obs.geojson", "evaluation.observations.locations")
        if vcfg.get("timeseries"):
            copy_user_file(vcfg.get("timeseries"), paths.data / "validation_observations.csv", "evaluation.observations.timeseries")
        return loc
    raise RuntimeError(f"Unsupported evaluation.observations.source={source!r}; use noaa_coops, file, or none")

# -----------------------------------------------------------------------------
# External validation / benchmarking datasets
# -----------------------------------------------------------------------------

USGS_STN_EVENTS_API = "https://stn.wim.usgs.gov/STNServices/Events.json"
USGS_STN_ALL_HWMS_API = "https://stn.wim.usgs.gov/STNServices/HWMs.json"
NASA_CMR_GRANULES_UMM = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
GFM_STAC_SEARCH_API = "https://stac.eodc.eu/api/v1/search"
GOOGLE_FLOOD_API_BASE = "https://floodforecasting.googleapis.com/v1"
GOOGLE_FLOOD_MIN_CUTOFF = pd.Timestamp("2025-08-01T00:00:00Z")


def external_validation_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    obj = cfg.get("external_validation", {})
    return obj if isinstance(obj, dict) else {}


def external_validation_signature(cfg: Dict[str, Any]) -> str:
    raw = json.dumps(external_validation_cfg(cfg), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _validation_status(paths: Paths, provider: str, status: str, **extra: Any) -> None:
    report = {
        "provider": provider,
        "status": status,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    write_json(report, paths.reports / f"external_validation_{provider}.json")


def _optional_validation_failure(
    cfg: Dict[str, Any], paths: Paths, provider: str, exc: Exception
) -> None:
    strict = bool(external_validation_cfg(cfg).get("strict", False))
    _validation_status(paths, provider, "failed", error=str(exc))
    if strict:
        raise
    log(f"External validation {provider}: unavailable/failed; continuing because strict=false: {exc}")


def _point_in_domain_buffer(
    gdf: gpd.GeoDataFrame, domain_path: str, buffer_km: float
) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    domain = gpd.read_file(domain_path)
    if domain.crs is None:
        domain = domain.set_crs(4326)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    centroid = domain.to_crs(4326).geometry.union_all().centroid
    zone = int((centroid.x + 180) // 6) + 1
    epsg = 32600 + zone if centroid.y >= 0 else 32700 + zone
    d = domain.to_crs(epsg).geometry.union_all().buffer(float(buffer_km) * 1000.0)
    gg = gdf.to_crs(epsg)
    keep = gg.geometry.apply(lambda geom: bool(geom is not None and not geom.is_empty and geom.intersects(d)))
    return gdf.loc[np.asarray(keep)].copy()


def _event_field(row: Dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lower and lower[name.lower()] not in (None, ""):
            return lower[name.lower()]
    return default


def _select_usgs_stn_event(cfg: Dict[str, Any], hcfg: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    explicit = hcfg.get("event_id")
    if explicit not in (None, ""):
        eid = int(explicit)
        return eid, {"event_id": eid, "selection": "explicit"}

    events = retry_get(USGS_STN_EVENTS_API, timeout=120).json()
    if isinstance(events, dict):
        for key in ("events", "Events", "items", "data"):
            if isinstance(events.get(key), list):
                events = events[key]
                break
    if not isinstance(events, list):
        raise RuntimeError("Unexpected USGS STN Events.json response")

    sim0 = parse_time(cfg["simulation"]["start"])
    sim1 = parse_time(cfg["simulation"]["end"])
    target = str(hcfg.get("event_name") or cfg.get("simulation", {}).get("event_name", "")).lower()
    target_tokens = {t for t in re.findall(r"[a-z]+", target) if len(t) >= 4}

    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid_raw = _event_field(ev, ["event_id", "eventid", "id"])
        try:
            eid = int(eid_raw)
        except Exception:
            continue
        name = str(_event_field(ev, ["event_name", "name", "eventname"], ""))
        desc = str(_event_field(ev, ["event_description", "description"], ""))
        text = (name + " " + desc).lower()
        score = 0.0
        if target and target in text:
            score += 100.0
        score += 25.0 * sum(1 for t in target_tokens if t in text)
        if str(sim0.year) in text:
            score += 15.0
        s0 = _event_field(ev, ["event_start_date", "start_date", "eventstartdate"])
        s1 = _event_field(ev, ["event_end_date", "end_date", "eventenddate"])
        try:
            e0 = parse_time(str(s0)) if s0 else None
            e1 = parse_time(str(s1)) if s1 else e0
            if e0 is not None and e1 is not None and e0 <= sim1 and e1 >= sim0:
                score += 50.0
        except Exception:
            pass
        scored.append((score, eid, ev))

    if not scored:
        raise RuntimeError("USGS STN returned no usable events")
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    score, eid, ev = scored[0]
    min_score = float(hcfg.get("minimum_event_match_score", 20.0))
    if score < min_score:
        raise RuntimeError(
            f"Could not confidently match USGS STN event for {target!r}; best score={score:g}. "
            "Set external_validation.high_water_marks.event_id explicitly."
        )
    return eid, ev


def _records_to_point_gdf(records: Any) -> gpd.GeoDataFrame:
    if not isinstance(records, list):
        raise RuntimeError("Expected a list of USGS HWM records")
    rows = []
    geoms = []
    for item in records:
        if not isinstance(item, dict):
            continue
        lon = _event_field(item, ["longitude_dd", "longitude", "lon", "x"])
        lat = _event_field(item, ["latitude_dd", "latitude", "lat", "y"])
        try:
            lonf, latf = float(lon), float(lat)
        except Exception:
            continue
        rows.append(dict(item))
        geoms.append(Point(lonf, latf))
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=4326)


def prepare_usgs_high_water_marks(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths
) -> Optional[Path]:
    hcfg = external_validation_cfg(cfg).get("high_water_marks", {}) or {}
    if not bool(hcfg.get("enabled", False)):
        _validation_status(paths, "high_water_marks", "disabled")
        return None
    source = str(hcfg.get("source", "usgs_stn")).lower()
    out = paths.validation / "usgs_high_water_marks.geojson"

    if source == "file":
        src = resolve_user_path(hcfg.get("path"), "external_validation.high_water_marks.path")
        gdf = gpd.read_file(src)
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        gdf = gdf.to_crs(4326)
        gdf = _point_in_domain_buffer(gdf, geo_meta["domain_path"], float(hcfg.get("domain_buffer_km", 5.0)))
        gdf.to_file(out, driver="GeoJSON")
        _validation_status(paths, "high_water_marks", "ready", source="file", path=str(out), n=int(len(gdf)))
        return out
    if source not in {"usgs", "usgs_stn", "stn"}:
        raise RuntimeError("high_water_marks.source must be usgs_stn, file, or none")

    event_id, event_meta = _select_usgs_stn_event(cfg, hcfg)
    url = f"https://stn.wim.usgs.gov/STNServices/Events/{event_id}/HWMs.json"
    records = retry_get(url, timeout=180).json()
    if isinstance(records, dict):
        for key in ("hwms", "HWMs", "items", "data"):
            if isinstance(records.get(key), list):
                records = records[key]
                break
    gdf = _records_to_point_gdf(records)
    gdf = _point_in_domain_buffer(gdf, geo_meta["domain_path"], float(hcfg.get("domain_buffer_km", 5.0)))

    qualities = [str(v).strip().lower() for v in hcfg.get("accepted_qualities", []) if str(v).strip()]
    if qualities and not gdf.empty:
        qcol = next((c for c in gdf.columns if str(c).lower() in {"hwm_quality_name", "hwmqualityname", "quality", "quality_name"}), None)
        if qcol is not None:
            gdf = gdf[gdf[qcol].astype(str).str.strip().str.lower().isin(qualities)].copy()

    if gdf.empty:
        _validation_status(paths, "high_water_marks", "no_coverage", event_id=event_id, event=event_meta)
        log(f"USGS HWM: no marks inside/near the model domain for STN event {event_id}.")
        return None
    gdf.to_file(out, driver="GeoJSON")

    datum_col = next((c for c in gdf.columns if str(c).lower() in {"vdatum", "vertical_datum", "verticaldatum", "vdatum_name", "verticaldatumname"}), None)
    datum_counts = gdf[datum_col].astype(str).value_counts(dropna=False).to_dict() if datum_col else {}
    _validation_status(
        paths,
        "high_water_marks",
        "ready",
        source="USGS STN",
        event_id=event_id,
        event_name=_event_field(event_meta, ["event_name", "name", "eventname"], ""),
        n=int(len(gdf)),
        vertical_datums={str(k): int(v) for k, v in datum_counts.items()},
        path=str(out),
    )
    log(f"USGS HWM validation data ready: {len(gdf)} marks | event_id={event_id} | {out}")
    return out


def _cmr_search_dswx(
    bbox: List[float], start: pd.Timestamp, end: pd.Timestamp, page_size: int
) -> List[Dict[str, Any]]:
    params = {
        "short_name": "OPERA_L3_DSWX-S1_V1",
        "bounding_box": ",".join(f"{float(v):.6f}" for v in bbox),
        "temporal": f"{start.tz_convert('UTC').isoformat().replace('+00:00','Z')},{end.tz_convert('UTC').isoformat().replace('+00:00','Z')}",
        "page_size": int(page_size),
    }
    r = retry_get(NASA_CMR_GRANULES_UMM, params=params, timeout=180)
    js = r.json()
    items = js.get("items", []) if isinstance(js, dict) else []
    return items if isinstance(items, list) else []


def _cmr_item_wtr_link(item: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
    umm = item.get("umm", {}) if isinstance(item, dict) else {}
    granule = str(umm.get("GranuleUR") or item.get("meta", {}).get("native-id") or "granule")
    temporal = umm.get("TemporalExtent", {}) if isinstance(umm, dict) else {}
    rng = temporal.get("RangeDateTime", {}) if isinstance(temporal, dict) else {}
    t0 = rng.get("BeginningDateTime") or temporal.get("SingleDateTime")
    urls = umm.get("RelatedUrls", []) if isinstance(umm, dict) else []
    candidates: List[str] = []
    for obj in urls if isinstance(urls, list) else []:
        if not isinstance(obj, dict):
            continue
        u = str(obj.get("URL", ""))
        ul = u.split("?", 1)[0].lower()
        if u.startswith("http") and ul.endswith(".tif") and ("b01_wtr" in ul or "_wtr.tif" in ul):
            candidates.append(u)
    if not candidates:
        for obj in urls if isinstance(urls, list) else []:
            if isinstance(obj, dict):
                u = str(obj.get("URL", ""))
                if u.startswith("http") and u.split("?", 1)[0].lower().endswith(".tif"):
                    candidates.append(u)
    return granule, t0, (candidates[0] if candidates else None)


def _download_authenticated_https(url: str, dst: Path, token: str, min_bytes: int = 1024) -> None:
    ensure_dir(dst.parent)
    if file_ready(dst, min_bytes):
        return
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    headers = {"Authorization": f"Bearer {token}"}
    log(f"Downloading authenticated validation asset: {url}")
    with requests.get(url, headers=headers, stream=True, timeout=300, allow_redirects=True) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    if not file_ready(tmp, min_bytes):
        raise RuntimeError(f"Downloaded OPERA asset is empty: {url}")
    tmp.replace(dst)


def _prepare_opera_window(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths, label: str,
    start: pd.Timestamp, end: pd.Timestamp, ocfg: Dict[str, Any]
) -> List[Dict[str, Any]]:
    items = _cmr_search_dswx(
        geo_meta["bbox"], start, end, int(ocfg.get("max_granules_per_window", 12))
    )
    token_env = str(ocfg.get("earthdata_token_env", "EARTHDATA_TOKEN"))
    token = os.environ.get(token_env, "").strip()
    rows = []
    for i, item in enumerate(items):
        granule, t0, url = _cmr_item_wtr_link(item)
        row: Dict[str, Any] = {"granule": granule, "time": t0, "url": url, "path": None}
        if url and token:
            dst = paths.validation / "opera_dswx_s1" / label / f"{_slug(granule)}_{i:03d}_B01_WTR.tif"
            try:
                _download_authenticated_https(url, dst, token)
                row["path"] = str(dst)
            except Exception as exc:
                row["download_error"] = str(exc)
        rows.append(row)
    return rows


def prepare_opera_dswx_validation(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths
) -> Optional[Path]:
    ocfg = external_validation_cfg(cfg).get("opera_dswx_s1", {}) or {}
    if not bool(ocfg.get("enabled", False)):
        _validation_status(paths, "opera_dswx_s1", "disabled")
        return None
    source = str(ocfg.get("source", "cmr")).lower()
    manifest = paths.validation / "opera_dswx_s1_manifest.json"
    if source == "file":
        obj = {
            "provider": "OPERA DSWx-S1",
            "mode": "file",
            "event": [{"path": str(resolve_user_path(p, "opera event raster"))} for p in ocfg.get("event_paths", [])],
            "baseline": [{"path": str(resolve_user_path(p, "opera baseline raster"))} for p in ocfg.get("baseline_paths", [])],
        }
        write_json(obj, manifest)
        _validation_status(paths, "opera_dswx_s1", "ready", source="file", manifest=str(manifest))
        return manifest
    if source not in {"cmr", "nasa_cmr"}:
        raise RuntimeError("opera_dswx_s1.source must be cmr or file")

    sim0 = parse_time(cfg["simulation"]["start"])
    sim1 = parse_time(cfg["simulation"]["end"])
    baseline_days = float(ocfg.get("baseline_days", 30.0))
    baseline_gap_days = float(ocfg.get("baseline_gap_days", 1.0))
    b1 = sim0 - pd.Timedelta(days=baseline_gap_days)
    b0 = b1 - pd.Timedelta(days=baseline_days)
    event = _prepare_opera_window(cfg, geo_meta, paths, "event", sim0, sim1, ocfg)
    baseline = _prepare_opera_window(cfg, geo_meta, paths, "baseline", b0, b1, ocfg)
    obj = {
        "provider": "NASA OPERA DSWx-S1",
        "collection": "OPERA_L3_DSWX-S1_V1",
        "event_window": [sim0.isoformat(), sim1.isoformat()],
        "baseline_window": [b0.isoformat(), b1.isoformat()],
        "event": event,
        "baseline": baseline,
        "water_classes": ocfg.get("water_classes", [1, 3]),
        "invalid_classes": ocfg.get("invalid_classes", [250, 251, 255]),
        "note": "DSWx-S1 is a surface-water observation product. Flood water is derived during evaluation as event water minus pre-event baseline water.",
    }
    write_json(obj, manifest)
    n_event = sum(1 for r in event if r.get("path"))
    n_base = sum(1 for r in baseline if r.get("path"))
    token_env = str(ocfg.get("earthdata_token_env", "EARTHDATA_TOKEN"))
    status = "ready" if n_event else ("metadata_only" if event else "no_coverage")
    _validation_status(
        paths, "opera_dswx_s1", status,
        manifest=str(manifest), event_hits=len(event), baseline_hits=len(baseline),
        event_downloaded=n_event, baseline_downloaded=n_base,
        earthdata_token_env=token_env,
        message=(None if n_event else f"Set {token_env} to an Earthdata bearer token to download protected OPERA GeoTIFF assets."),
    )
    log(f"OPERA DSWx-S1: event hits={len(event)}, downloaded={n_event}; baseline hits={len(baseline)}, downloaded={n_base}")
    return manifest


def _match_asset(assets: Dict[str, Any], needles: Iterable[str]) -> Optional[Tuple[str, str]]:
    wants = [str(n).lower() for n in needles]
    for key, obj in assets.items():
        href = str(obj.get("href", "")) if isinstance(obj, dict) else ""
        hay = (str(key) + " " + href).lower()
        if href and any(n in hay for n in wants):
            return str(key), href
    return None


def prepare_gfm_validation(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths
) -> Optional[Path]:
    gcfg = external_validation_cfg(cfg).get("gfm", {}) or {}
    if not bool(gcfg.get("enabled", False)):
        _validation_status(paths, "gfm", "disabled")
        return None
    source = str(gcfg.get("source", "stac")).lower()
    manifest = paths.validation / "gfm_manifest.json"
    if source == "file":
        obj = {
            "provider": "Copernicus GFM",
            "mode": "file",
            "items": [{"flood_path": str(resolve_user_path(p, "GFM flood raster"))} for p in gcfg.get("flood_paths", [])],
            "reference_water_paths": [str(resolve_user_path(p, "GFM reference water raster")) for p in gcfg.get("reference_water_paths", [])],
        }
        write_json(obj, manifest)
        _validation_status(paths, "gfm", "ready", source="file", manifest=str(manifest))
        return manifest
    if source != "stac":
        raise RuntimeError("gfm.source must be stac or file")

    sim0 = parse_time(cfg["simulation"]["start"])
    sim1 = parse_time(cfg["simulation"]["end"])
    params = {
        "collections": str(gcfg.get("collection", "GFM")),
        "bbox": ",".join(f"{float(v):.6f}" for v in geo_meta["bbox"]),
        "datetime": f"{sim0.isoformat().replace('+00:00','Z')}/{sim1.isoformat().replace('+00:00','Z')}",
        "limit": int(gcfg.get("max_items", 50)),
    }
    js = retry_get(str(gcfg.get("stac_search_url", GFM_STAC_SEARCH_API)), params=params, timeout=180).json()
    features = js.get("features", []) if isinstance(js, dict) else []
    rows = []
    for feat in features if isinstance(features, list) else []:
        assets = feat.get("assets", {}) if isinstance(feat, dict) else {}
        flood = _match_asset(assets, ["ensemble_flood_extent", "ensemble_flood", "flood_extent"])
        ref = _match_asset(assets, ["reference_water_mask", "reference_water"])
        excl = _match_asset(assets, ["exclusion_mask"])
        like = _match_asset(assets, ["ensemble_likelihood", "likelihood"])
        if flood is None:
            continue
        row: Dict[str, Any] = {
            "id": feat.get("id"),
            "datetime": (feat.get("properties", {}) or {}).get("datetime"),
            "flood_url": flood[1],
        }
        for name, pair in (("reference_water_url", ref), ("exclusion_mask_url", excl), ("likelihood_url", like)):
            if pair:
                row[name] = pair[1]
        for key in ["flood_url", "reference_water_url", "exclusion_mask_url", "likelihood_url"]:
            u = row.get(key)
            if not u:
                continue
            dst = paths.validation / "gfm" / f"{_slug(feat.get('id','item'))}_{key.replace('_url','')}.tif"
            try:
                download_file(str(u), dst, min_bytes=1024)
                row[key.replace("_url", "_path")] = str(dst)
            except Exception as exc:
                row[key.replace("_url", "_download_error")] = str(exc)
        rows.append(row)
    write_json({"provider": "Copernicus GFM", "collection": params["collections"], "items": rows}, manifest)
    ready = sum(1 for r in rows if r.get("flood_path"))
    _validation_status(paths, "gfm", "ready" if ready else "no_coverage", manifest=str(manifest), hits=len(rows), downloaded=ready)
    log(f"Copernicus GFM: matching STAC items={len(rows)}, downloaded flood rasters={ready}")
    return manifest


def _kml_polygon_geometries(kml: str) -> List[Any]:
    import xml.etree.ElementTree as ET
    from shapely.geometry import Polygon

    try:
        root = ET.fromstring(kml)
    except Exception as exc:
        raise RuntimeError(f"Could not parse Google Flood Hub KML polygon: {exc}") from exc
    polygons = []
    for poly in root.findall(".//{*}Polygon"):
        coords_el = poly.find(".//{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
        if coords_el is None or not coords_el.text:
            continue
        pts = []
        for token in coords_el.text.replace("\n", " ").split():
            parts = token.split(",")
            if len(parts) >= 2:
                try:
                    pts.append((float(parts[0]), float(parts[1])))
                except Exception:
                    pass
        if len(pts) >= 3:
            p = Polygon(pts)
            if p.is_valid and not p.is_empty:
                polygons.append(p)
    return polygons


def prepare_google_flood_hub_validation(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths
) -> Optional[Path]:
    gcfg = external_validation_cfg(cfg).get("google_flood_hub", {}) or {}
    if not bool(gcfg.get("enabled", False)):
        _validation_status(paths, "google_flood_hub", "disabled")
        return None
    source = str(gcfg.get("source", "api")).lower()
    out = paths.validation / "google_flood_hub_inundation.geojson"
    if source == "file":
        src = resolve_user_path(gcfg.get("path"), "external_validation.google_flood_hub.path")
        gg = gpd.read_file(src)
        if gg.crs is None:
            gg = gg.set_crs(4326)
        gg.to_crs(4326).to_file(out, driver="GeoJSON")
        _validation_status(paths, "google_flood_hub", "ready", source="file", path=str(out), n=int(len(gg)))
        return out
    if source != "api":
        raise RuntimeError("google_flood_hub.source must be api or file")

    cutoff = parse_time(str(gcfg.get("cutoff_time") or cfg["simulation"]["end"]))
    if cutoff < GOOGLE_FLOOD_MIN_CUTOFF:
        _validation_status(
            paths, "google_flood_hub", "unsupported_historical_date",
            cutoff_time=cutoff.isoformat(), minimum_cutoff=GOOGLE_FLOOD_MIN_CUTOFF.isoformat(),
            note="Google Flood Forecasting API flood-status history cannot be queried before 2025-08-01. This provider is an inter-model forecast benchmark, not an observation source.",
        )
        log("Google Flood Hub skipped: API flood-status cutoff cannot be earlier than 2025-08-01; this event predates the archive.")
        return None

    key_env = str(gcfg.get("api_key_env", "GOOGLE_FLOOD_API_KEY"))
    api_key = os.environ.get(key_env, "").strip()
    if not api_key:
        _validation_status(paths, "google_flood_hub", "credentials_required", api_key_env=key_env)
        log(f"Google Flood Hub skipped: set {key_env} after approval for the Flood Forecasting API.")
        return None

    query_bbox = bbox_buffer_km(
        [float(v) for v in geo_meta["bbox"]],
        float(gcfg.get("query_buffer_km", 50.0)),
    )
    west, south, east, north = query_bbox
    loop = {
        "vertices": [
            {"latitude": south, "longitude": west},
            {"latitude": south, "longitude": east},
            {"latitude": north, "longitude": east},
            {"latitude": north, "longitude": west},
        ]
    }
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": api_key}
    base_payload = {
        "cutoffTime": cutoff.isoformat().replace("+00:00", "Z"),
        "pageSize": min(int(gcfg.get("page_size", 20000)), 20000),
        "loop": loop,
        "includeNonQualityVerified": bool(gcfg.get("include_non_quality_verified", False)),
    }
    url = GOOGLE_FLOOD_API_BASE + "/floodStatus:searchLatestFloodStatusByArea"
    statuses: List[Dict[str, Any]] = []
    page_token = ""
    for _ in range(int(gcfg.get("max_pages", 10))):
        payload = dict(base_payload)
        if page_token:
            payload["pageToken"] = page_token
        r = requests.post(url, headers=headers, json=payload, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"Google Flood Forecasting API HTTP {r.status_code}: {r.text[:1000]}")
        js = r.json()
        page = js.get("floodStatuses", [])
        if isinstance(page, list):
            statuses.extend(v for v in page if isinstance(v, dict))
        page_token = str(js.get("nextPageToken", "") or "")
        if not page_token:
            break
    desired_levels = {str(v).upper() for v in gcfg.get("levels", ["LOW", "MEDIUM", "HIGH"])}
    features = []
    seen = set()
    for st in statuses if isinstance(statuses, list) else []:
        ms = st.get("inundationMapSet") or {}
        mtype = str(ms.get("inundationMapType", ""))
        for mp in ms.get("inundationMaps", []) or []:
            level = str(mp.get("level", "")).replace("INUNDATION_LEVEL_", "").upper()
            pid = str(mp.get("serializedPolygonId", ""))
            if not pid or level not in desired_levels or pid in seen:
                continue
            seen.add(pid)
            name = pid if pid.startswith("serializedPolygons/") else f"serializedPolygons/{pid}"
            rr = requests.get(
                GOOGLE_FLOOD_API_BASE + "/" + name,
                headers={"X-Goog-Api-Key": api_key}, timeout=120,
            )
            if rr.status_code != 200:
                continue
            pobj = rr.json()
            for geom in _kml_polygon_geometries(str(pobj.get("kml", ""))):
                features.append({
                    "gauge_id": st.get("gaugeId"),
                    "quality_verified": st.get("qualityVerified"),
                    "issued_time": st.get("issuedTime"),
                    "severity": st.get("severity"),
                    "map_inference_type": st.get("mapInferenceType"),
                    "inundation_map_type": mtype,
                    "level": level,
                    "polygon_id": pid,
                    "geometry": geom,
                })
    if not features:
        _validation_status(paths, "google_flood_hub", "no_coverage", cutoff_time=cutoff.isoformat(), statuses=len(statuses))
        return None
    gg = gpd.GeoDataFrame(features, crs=4326)
    gg.to_file(out, driver="GeoJSON")
    _validation_status(
        paths, "google_flood_hub", "ready", path=str(out), n=int(len(gg)),
        statuses=len(statuses), cutoff_time=cutoff.isoformat(), query_bbox=query_bbox,
        benchmark_type="model_to_model_forecast_benchmark",
    )
    log(f"Google Flood Hub benchmark polygons ready: {len(gg)} polygons | {out}")
    return out


def prepare_external_validation(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths
) -> Dict[str, Optional[str]]:
    """Acquire optional validation/benchmark products on the internet-enabled build node.

    These products never modify HydroMT/SFINCS inputs. Failures are non-fatal by
    default and are reported provider-by-provider unless external_validation.strict=true.
    """
    outputs: Dict[str, Optional[str]] = {}
    # Production AutoCF deliberately limits external validation acquisition to
    # high-water marks. SAR/flood-extent and model-to-model inundation products
    # are retained only as dead legacy helper functions below for provenance;
    # they are never called by this production provider list.
    providers = [
        ("high_water_marks", prepare_usgs_high_water_marks),
    ]
    for name, func in providers:
        try:
            p = func(cfg, geo_meta, paths)
            outputs[name] = str(p) if p is not None else None
        except Exception as exc:
            _optional_validation_failure(cfg, paths, name, exc)
            outputs[name] = None
    write_json(outputs, paths.reports / "external_validation_files.json")
    return outputs


# -----------------------------------------------------------------------------
# ERA5
# -----------------------------------------------------------------------------


def download_era5(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Path, Path]:
    try:
        import cdsapi  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "cdsapi is required for ERA5 download. Install it and configure ~/.cdsapirc."
        ) from exc
    import xarray as xr  # type: ignore

    acfg = cfg["atmosphere"]
    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])
    forcing_end = atmospheric_forcing_end(cfg)
    bbox = bbox_buffer_degrees(geo_meta["bbox"], float(acfg.get("spatial_buffer_degrees", 0.5)))
    west, south, east, north = bbox
    era_dir = paths.cache / acfg.get("cache_subdir", "era5")
    ensure_dir(era_dir)
    raw_ws = era_dir / "era5_helene_wind_pressure_raw.nc"
    raw_tp = era_dir / "era5_helene_precip_raw.nc"
    proc_ws = paths.data / "era5_wind_pressure_pa.nc"
    proc_tp = paths.data / "era5_precip_mm_cumulative.nc"

    dates = pd.date_range(start.normalize(), forcing_end.normalize(), freq="D")
    if forcing_end > end:
        log(
            "ERA5 forcing coverage: extending through the next complete UTC hour "
            f"({forcing_end.isoformat()}) to bracket simulation tstop {end.isoformat()}."
        )
    years = sorted(set(str(d.year) for d in dates))
    months = sorted(set(f"{d.month:02d}" for d in dates))
    days = sorted(set(f"{d.day:02d}" for d in dates))
    hours = [f"{h:02d}:00" for h in range(24)]

    c = cdsapi.Client()
    common = {
        "product_type": ["reanalysis"],
        "year": years,
        "month": months,
        "day": days,
        "time": hours,
        "area": [north, west, south, east],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    if not file_ready(raw_ws, 1024):
        log("Downloading ERA5 wind and pressure from CDS")
        c.retrieve(acfg["cds_dataset"], {**common, "variable": [
            acfg["variables"]["wind_u"]["era5_name"],
            acfg["variables"]["wind_v"]["era5_name"],
            acfg["variables"]["pressure"]["era5_name"],
        ]}, str(raw_ws))
    else:
        log(f"Reusing ERA5 wind/pressure: {raw_ws}")
    if not file_ready(raw_tp, 1024):
        log("Downloading ERA5 precipitation from CDS")
        c.retrieve(acfg["cds_dataset"], {**common, "variable": [
            acfg["variables"]["precipitation"]["era5_name"],
        ]}, str(raw_tp))
    else:
        log(f"Reusing ERA5 precipitation: {raw_tp}")

    def process(infile: Path, outfile: Path, keys: List[str]) -> None:
        if file_ready(outfile, 1024):
            log(f"Reusing processed ERA5 file: {outfile}")
            return
        ds = xr.open_dataset(infile).load()
        rename = {}
        for key in keys:
            vcfg = acfg["variables"][key]
            raw = vcfg["raw_short_name"]
            final = vcfg["final_name"]
            if raw not in ds:
                matches = [v for v in ds.data_vars if v.lower() == raw.lower()]
                if matches:
                    raw = matches[0]
                else:
                    raise RuntimeError(f"ERA5 variable {raw} not found in {infile}. Variables: {list(ds.data_vars)}")
            da = ds[raw] * float(vcfg.get("unit_mult", 1.0)) + float(vcfg.get("unit_add", 0.0))
            mn = float(da.min(skipna=True))
            mx = float(da.max(skipna=True))
            if mn < float(vcfg["valid_min"]) or mx > float(vcfg["valid_max"]):
                raise RuntimeError(
                    f"ERA5 range check failed for {final}: min={mn}, max={mx}. "
                    "Check unit conversion."
                )
            ds[final] = da
            rename[raw] = final
        keep = [acfg["variables"][k]["final_name"] for k in keys]
        out = ds[keep]
        if "valid_time" in out.coords:
            out = out.rename({"valid_time": "time"})
        out.to_netcdf(outfile)

    process(raw_ws, proc_ws, ["wind_u", "wind_v", "pressure"])
    process(raw_tp, proc_tp, ["precipitation"])
    return proc_ws, proc_tp


# -----------------------------------------------------------------------------
# HydroMT build files
# -----------------------------------------------------------------------------


def make_hydromt_catalog(
    cfg: Dict[str, Any],
    paths: Paths,
    cudem_tif: Path,
    gebco_tif: Path,
    esa_tif: Path,
    manning_csv: Path,
    wind_grid: Optional[Path],
    pressure_grid: Optional[Path],
    precip_grid: Optional[Path],
    infiltration_inputs: Optional[Dict[str, Path]] = None,
    topography_sources: Optional[List[Tuple[str, Path, str]]] = None,
    roughness_source: Optional[Tuple[Path, Path]] = None,
) -> Path:
    catalog = {
        "meta": {"version": "1", "root": str(paths.out)},
        "cudem_3m": {
            "data_type": "RasterDataset",
            "path": str(cudem_tif),
            "driver": "raster",
            "filesystem": "local",
            "crs": None,
            "meta": {"category": "topography", "unit": "m+MSL", "source": "NOAA CUDEM 1/9 arc-second"},
        },
        "gebco_500m": {
            "data_type": "RasterDataset",
            "path": str(gebco_tif),
            "driver": "raster",
            "filesystem": "local",
            "crs": None,
            "meta": {"category": "topography", "unit": "m+MSL", "source": "GEBCO 2024 15 arc-second"},
        },
        "esa_worldcover": {
            "data_type": "RasterDataset",
            "path": str(esa_tif),
            "driver": "raster",
            "filesystem": "local",
            "meta": {"category": "landuse", "source": "ESA WorldCover 2021 v200"},
        },
        "esa_worldcover_mapping": {
            "data_type": "DataFrame",
            "path": str(manning_csv),
            "driver": "csv",
            "meta": {"category": "landuse", "notes": "Manning n by ESA WorldCover class"},
        },
    }
    if wind_grid is not None:
        catalog["forcing_wind"] = {"data_type":"RasterDataset","path":str(wind_grid),"driver":"netcdf","kwargs":{"decode_times":True},"crs":4326,"meta":{"category":"meteo","unit":"m/s"}}
    if pressure_grid is not None:
        catalog["forcing_pressure"] = {"data_type":"RasterDataset","path":str(pressure_grid),"driver":"netcdf","kwargs":{"decode_times":True},"crs":4326,"meta":{"category":"meteo","unit":"Pa"}}
    if precip_grid is not None:
        catalog["forcing_precip"] = {"data_type":"RasterDataset","path":str(precip_grid),"driver":"netcdf","kwargs":{"decode_times":True},"crs":4326,"meta":{"category":"meteo","unit":"precipitation rate mm/hr"}}

    if infiltration_inputs is not None and {"lulc","hsg","ksat","cn_table"}.issubset(infiltration_inputs):
        catalog.update({
            "annual_nlcd_landcover": {
                "data_type": "RasterDataset",
                "path": str(infiltration_inputs["lulc"]),
                "driver": "raster",
                "filesystem": "local",
                "meta": {"category": "landuse", "source": "USGS Annual NLCD", "unit": "class"},
            },
            "ssurgo_hsg": {
                "data_type": "RasterDataset",
                "path": str(infiltration_inputs["hsg"]),
                "driver": "raster",
                "filesystem": "local",
                "meta": {"category": "soil", "source": "USDA SSURGO", "unit": "HSG 1=A,2=B,3=C,4=D"},
            },
            "ssurgo_ksat": {
                "data_type": "RasterDataset",
                "path": str(infiltration_inputs["ksat"]),
                "driver": "raster",
                "filesystem": "local",
                "meta": {"category": "soil", "source": "USDA SSURGO", "unit": "micrometers per second"},
            },
            "nlcd_cn_lookup": {
                "data_type": "DataFrame",
                "path": str(infiltration_inputs["cn_table"]),
                "driver": "csv",
                # Required by hydromt_sfincs 1.0.3:
                # rows = NLCD classes (DataFrame index)
                # columns = integer HSG classes 1,2,3,4.
                # Without index_col=0 HydroMT reads "nlcd" as a normal
                # column and scs_recovery_determination() later attempts
                # int("nlcd"), which fails.
                "driver_kwargs": {"index_col": 0},
                "meta": {"category": "infiltration", "notes": "NLCD x HSG CN-II lookup matrix"},
            },
        })

    if infiltration_inputs is not None and "gcn" in infiltration_inputs:
        icfg = cfg.get("infiltration", {})
        source = str(icfg.get("source", "file")).lower()
        cn_key = "gcn250" if source == "gcn250" else "user_curve_number"
        cn_meta = {"category": "infiltration", "unit": "curve number"}
        entry = {
            "data_type": "RasterDataset",
            "path": str(infiltration_inputs["gcn"]),
            "driver": "raster",
            "filesystem": "local",
            "meta": cn_meta,
        }
        if source == "gcn250":
            entry["nodata"] = 255
            entry["meta"].update({
                "source": "GCN250 / Jaafar & Ahmad (2019)",
                "paper_doi": "10.1038/s41597-019-0155-x",
                "dataset_doi": "10.6084/m9.figshare.7756202.v1",
            })
        catalog[cn_key] = entry

    out = paths.work / "_internal_hydromt_data_catalog.yml"
    write_yaml(catalog, out)
    return out


def make_hydromt_build_config(
    cfg: Dict[str, Any],
    paths: Paths,
    geo_meta: Dict[str, Any],
    wl_ts: Path,
    wl_locs: Path,
    obs_locs: Optional[Path],
    dis_ts: Optional[Path],
    dis_locs: Optional[Path],
    wind_grid: Optional[Path],
    pressure_grid: Optional[Path],
    precip_grid: Optional[Path],
) -> Path:
    sim = cfg["simulation"]
    start = parse_time(sim["start"])
    end = parse_time(sim["end"])
    forcing_end = atmospheric_forcing_end(cfg)
    hc = cfg["hydromt"]
    sf = cfg["sfincs"]
    mesh = cfg["mesh"]
    model_mode = get_model_mode(cfg)

    setup_config = {
        "tref": sfincs_time(start),
        "tstart": sfincs_time(start),
        # Temporary HydroMT build tstop used only to keep the first complete
        # ERA5 hour at/after the true simulation end in the gridded forcing.
        "tstop": sfincs_time(forcing_end),
        "dtout": int(sf.get("dtout_seconds", 3600)),
        "dthisout": int(sf.get("dthisout_seconds", 360)),
        "dtmaxout": int(sf.get("dtmaxout_seconds", 3600)),
        "inputformat": sf.get("inputformat", "bin"),
        "outputformat": sf.get("outputformat", "net"),
        "obsfile": "sfincs.obs" if obs_locs else None,
        **sf.get("additional_parameters", {}),
    }
    setup_config = {k: v for k, v in setup_config.items() if v is not None}
    # Atmospheric pressure only affects SFINCS when the barometric term is enabled.
    setup_config["baro"] = 1 if pressure_grid is not None else 0

    dep_sources = [
        {"elevtn": "cudem_3m", "merge_method": "first", "reproj_method": "bilinear"},
    ]
    if str(cfg.get("topobathy", {}).get("gapfill", {}).get("source", "GEBCO_2024_15_ARCSEC")).lower() != "none":
        dep_sources.append({"elevtn": "gebco_500m", "merge_method": "first", "reproj_method": "bilinear"})

    # Keep setup_dep in BOTH modes. In subgrid mode it is used only to derive
    # the computational mask/boundary; setup_subgrid subsequently removes the
    # depfile from sfincs.inp and SFINCS uses the subgrid table hydraulically.
    build: Dict[str, Any] = {
        "global": {"data_libs": []},
        "setup_config": setup_config,
        "setup_grid_from_region": {
            "res": float(mesh["dx_m"]),
            "crs": mesh.get("crs", "utm"),
            "rotated": bool(mesh.get("rotated", False)),
        },
        "setup_dep": {
            "datasets_dep": [dict(d) for d in dep_sources],
            "interp_method": "linear",
            "buffer_cells": 0,
        },
        "setup_mask_active": {
            "mask": geo_meta["domain_path"],
            "zmin": float(hc.get("active_zmin_m", -50.0)),
            "zmax": float(hc.get("active_zmax_m", 20.0)),
            "fill_area": 10.0,
            "drop_area": 0.0,
            "connectivity": 8,
            "all_touched": True,
            "reset_mask": True,
        },
        "setup_mask_bounds": {
            "btype": "waterlevel",
            "include_mask": geo_meta["ocean_path"],
            "include_mask_buffer": float(hc.get("boundary_include_buffer_m", 600.0)),
            "zmax": float(hc.get("boundary_zmax_m", 5.0)),
            "connectivity": 8,
            "all_touched": True,
            "reset_bounds": True,
        },
    }

    if model_mode == "regular":
        build["setup_manning_roughness"] = {
            "datasets_rgh": [
                {"lulc": "esa_worldcover", "reclass_table": "esa_worldcover_mapping"}
            ],
            "manning_land": 0.04,
            "manning_sea": 0.02,
        }
    else:
        sg = cfg.get("subgrid", {})
        hm_version, accepted = get_hydromt_subgrid_api()
        log(f"Detected hydromt_sfincs {hm_version}; setup_subgrid keywords: {sorted(accepted)}")

        candidate: Dict[str, Any] = {
            "datasets_dep": [dict(d) for d in dep_sources],
            "datasets_rgh": [
                {"lulc": "esa_worldcover", "reclass_table": "esa_worldcover_mapping"}
            ],
            "buffer_cells": int(sg.get("buffer_cells", 0)),
            "nlevels": int(sg.get("nlevels", 10)),
            "nr_subgrid_pixels": int(sg.get("nr_subgrid_pixels", 20)),
            "nrmax": int(sg.get("nrmax", 2000)),
            "huthresh": float(sg.get("huthresh", 0.01)),
            "manning_land": float(sg.get("manning_land", 0.04)),
            "manning_sea": float(sg.get("manning_sea", 0.02)),
            "rgh_lev_land": float(sg.get("rgh_lev_land", 0.0)),
            "write_dep_tif": bool(sg.get("write_dep_tif", True)),
            "write_man_tif": bool(sg.get("write_man_tif", True)),
        }
        # Newer HydroMT-SFINCS releases support these. v1.0.3 does not use
        # the revised wet-fraction weighting formulation, so they are filtered
        # out automatically when absent from the installed method signature.
        if "q_table_option" in sg:
            candidate["q_table_option"] = int(sg["q_table_option"])
        if "weight_option" in sg:
            candidate["weight_option"] = str(sg["weight_option"])
        for key in ("max_gradient", "z_minimum"):
            if key in sg:
                candidate[key] = float(sg[key])

        # Compatibility fallback for older APIs that used nbins instead of nlevels.
        if "nlevels" not in accepted and "nbins" in accepted:
            candidate["nbins"] = candidate.pop("nlevels")

        required = {"datasets_dep", "datasets_rgh", "nr_subgrid_pixels"}
        missing_required = sorted(required - accepted)
        if missing_required:
            raise RuntimeError(
                f"Installed hydromt_sfincs {hm_version} setup_subgrid API is missing required "
                f"keywords: {missing_required}"
            )

        unsupported = [k for k in candidate if k not in accepted]
        for key in unsupported:
            log(f"Subgrid option {key!r} is not supported by hydromt_sfincs {hm_version}; not forwarding it.")
        setup_subgrid = {k: v for k, v in candidate.items() if k in accepted}
        build["setup_subgrid"] = setup_subgrid

    if infiltration_enabled(cfg):
        icfg = cfg.get("infiltration", {})
        method = str(icfg.get("method", "cn_with_recovery")).lower()
        source = str(icfg.get("source", "nlcd_ssurgo" if method == "cn_with_recovery" else "gcn250")).lower()
        if method == "cn_with_recovery":
            build["setup_cn_infiltration_with_ks"] = {
                "lulc": "annual_nlcd_landcover", "hsg": "ssurgo_hsg", "ksat": "ssurgo_ksat",
                "reclass_table": "nlcd_cn_lookup", "effective": float(icfg.get("effective", 0.50)),
                "block_size": int(icfg.get("block_size", 2000)),
            }
        elif method == "cn":
            cn_name = "gcn250" if source == "gcn250" else "user_curve_number"
            build["setup_cn_infiltration"] = {
                "cn": cn_name,
                "antecedent_moisture": str(icfg.get("antecedent_moisture", "avg")),
            }
            # GCN250 is registered in the agent-generated local catalog.
            # Do not request HydroMT 0.9.x's retired artifact_data archive.
        else:
            raise RuntimeError(f"Unsupported infiltration.method={method!r}; use cn_with_recovery or cn")

    build["setup_waterlevel_forcing"] = {
        "timeseries": str(wl_ts),
        "locations": str(wl_locs),
        "buffer": float(hc.get("boundary_point_spacing_m", 5000.0)),
        "merge": True,
    }
    if wind_grid is not None:
        build["setup_wind_forcing_from_grid"] = {"wind": "forcing_wind"}
    if pressure_grid is not None:
        build["setup_pressure_forcing_from_grid"] = {"press": "forcing_pressure"}
    if precip_grid is not None:
        build["setup_precip_forcing_from_grid"] = {"precip": "forcing_precip"}

    if obs_locs is not None:
        build["setup_observation_points"] = {"locations": str(obs_locs), "merge": True}
    if dis_ts is not None and dis_locs is not None:
        build["setup_discharge_forcing"] = {
            "timeseries": str(dis_ts), "locations": str(dis_locs), "merge": True
        }

    out = paths.work / "_internal_hydromt_build.yml"
    write_yaml(build, out)
    log(f"HydroMT build mode: {model_mode}")
    if forcing_end > end:
        log(
            "HydroMT atmospheric-forcing window temporarily ends at "
            f"{sfincs_time(forcing_end)}; exact SFINCS tstop is {sfincs_time(end)}."
        )
    if model_mode == "subgrid":
        log(
            "Subgrid specification: "
            f"dx={float(mesh['dx_m']):g} m, "
            f"nr_subgrid_pixels={int(cfg.get('subgrid', {}).get('nr_subgrid_pixels', 20))}, "
            f"nominal subgrid pixel={subgrid_sampling_resolution_m(cfg):g} m"
        )
    return out

def _set_sfincs_inp_value(path: Path, key: str, value: str) -> None:
    """Set one scalar SFINCS input value while leaving all other lines intact."""
    if not path.exists():
        raise RuntimeError(f"Cannot update {key}; SFINCS input file does not exist: {path}")

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*(?:=|\s)\s*.*$", re.IGNORECASE)
    replaced = False
    out_lines: List[str] = []

    for line in lines:
        m = pattern.match(line)
        if m and not replaced:
            out_lines.append(f"{m.group(1)}{key:<22s} = {value}")
            replaced = True
        else:
            out_lines.append(line)

    if not replaced:
        out_lines.append(f"{key:<22s} = {value}")

    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def run_hydromt_build(cfg: Dict[str, Any], paths: Paths, geo_meta: Dict[str, Any], catalog: Path, build_cfg: Path) -> None:
    """Execute the HydroMT build when the state file says it is unfinished.

    Do not use sfincs.inp existence as a completion test: HydroMT may leave a
    partial sfincs.inp after an exception. The caller's hydromt_build state flag
    is the authoritative completion marker.
    """
    sfincs_inp = paths.out / cfg["sfincs"].get("input_file", "sfincs.inp")
    require_executable("hydromt")

    # hydromt_sfincs 1.0.3 still references the legacy NumPy alias np.NaN
    # inside workflows.curvenumber.scs_recovery_determination(). NumPy >= 2.0
    # removed that alias in favor of np.nan. Because HydroMT is launched as a
    # separate process, patch the alias inside that child process rather than
    # modifying the user's Conda environment or site-packages.
    if not hasattr(np, "NaN"):
        log(
            "NumPy >=2 compatibility: launching HydroMT with a process-local "
            "np.NaN -> np.nan shim required by hydromt_sfincs 1.0.3."
        )
        hydromt_compat_code = (
            "import sys, numpy as np; "
            "np.NaN = np.nan; "
            "from hydromt.cli.main import main; "
            "main(args=sys.argv[1:], prog_name='hydromt')"
        )
        cmd = [
            sys.executable, "-c", hydromt_compat_code,
            "build", "sfincs", str(paths.out),
            "-r", "{'geom': '" + geo_meta["domain_path"] + "'}",
            "-i", str(build_cfg),
            "-d", str(catalog),
            cfg["hydromt"].get("build_verbosity", "-vv"),
        ]
    else:
        cmd = [
            "hydromt", "build", "sfincs", str(paths.out),
            "-r", "{'geom': '" + geo_meta["domain_path"] + "'}",
            "-i", str(build_cfg),
            "-d", str(catalog),
            cfg["hydromt"].get("build_verbosity", "-vv"),
        ]
    # A failed HydroMT build can leave partial model files. Since this function
    # is only called while state['hydromt_build'] is false, overwrite those
    # partial HydroMT model files and rebuild the stage deterministically.
    if file_ready(sfincs_inp, 100):
        log(f"Incomplete previous HydroMT build detected ({sfincs_inp} exists but hydromt_build is not complete).")
        log("Rebuilding the HydroMT stage with --force-overwrite; downloaded/cache data are retained.")
        cmd.append("--force-overwrite")
    elif bool(cfg["project"].get("overwrite", False)):
        cmd.append("--force-overwrite")
    run_cmd(cmd)

    # HydroMT needed the hourly-bracketed tstop only while selecting and
    # writing gridded ERA5 forcing. Restore the exact hydraulic end time now.
    true_end = parse_time(cfg["simulation"]["end"])
    forcing_end = atmospheric_forcing_end(cfg)
    _set_sfincs_inp_value(sfincs_inp, "tstop", sfincs_time(true_end))
    if forcing_end > true_end:
        log(
            "Restored exact SFINCS tstop after atmospheric forcing build: "
            f"{sfincs_time(true_end)} "
            f"(ERA5 forcing retained through {sfincs_time(forcing_end)})."
        )



def _read_sfincs_inp_keys(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key, value = parts
        out[key.strip().lower()] = value.strip().split()[0]
    return out


def validate_built_model(cfg: Dict[str, Any], paths: Paths) -> None:
    """Fail early when a nominal subgrid build did not actually produce subgrid input."""
    mode = get_model_mode(cfg)
    inp = paths.out / str(cfg["sfincs"].get("input_file", "sfincs.inp"))
    keys = _read_sfincs_inp_keys(inp)

    if infiltration_enabled(cfg):
        method = str(cfg.get("infiltration", {}).get("method", "cn_with_recovery")).lower()
        if method == "cn_with_recovery":
            inf_files = {}
            for key in ("smaxfile", "sefffile", "ksfile"):
                fn = keys.get(key)
                if not fn:
                    raise RuntimeError(f"Infiltration is enabled but sfincs.inp has no {key} for cn_with_recovery")
                fp = paths.out / fn
                if not file_ready(fp, 100): raise RuntimeError(f"Infiltration file referenced by {key} is missing/empty: {fp}")
                inf_files[key] = fp
            log("Validated infiltration: CN with recovery | " + " | ".join(f"{k}={v.name}" for k,v in inf_files.items()))
        elif method == "cn":
            fn = keys.get("scsfile")
            if not fn: raise RuntimeError("Ordinary CN infiltration is enabled but sfincs.inp has no scsfile")
            fp = paths.out / fn
            if not file_ready(fp,100): raise RuntimeError(f"CN infiltration file is missing/empty: {fp}")
            log(f"Validated infiltration: ordinary CN | scsfile={fp.name}")
        else:
            raise RuntimeError(f"Unsupported infiltration method during validation: {method}")

    if mode == "regular":
        log("Validated model mode: regular")
        return

    sbg = keys.get("sbgfile")
    if not sbg:
        raise RuntimeError(
            "model.mode=subgrid but sfincs.inp has no sbgfile. HydroMT did not finish setup_subgrid correctly."
        )
    sbg_path = paths.out / sbg
    if not file_ready(sbg_path, 1024):
        raise RuntimeError(f"Subgrid file referenced by sfincs.inp is missing/empty: {sbg_path}")
    if "manningfile" in keys:
        log("WARNING: sfincs.inp still contains manningfile, but SFINCS ignores regular-grid roughness when sbgfile is active.")
    if "depfile" in keys:
        log("WARNING: sfincs.inp still contains depfile, but SFINCS uses the subgrid table hydraulically when sbgfile is active.")
    log(f"Validated model mode: subgrid | sbgfile={sbg_path.name} ({sbg_path.stat().st_size / 1e6:.1f} MB)")


# -----------------------------------------------------------------------------
# Observation-point snapping
# -----------------------------------------------------------------------------


def _station_id_from_row(row: pd.Series) -> str:
    """Extract a NOAA-like seven-digit station id from a GeoJSON row."""
    for col in row.index:
        if col == "geometry":
            continue
        m = re.search(r"\b(\d{7})\b", str(row[col]))
        if m:
            return m.group(1)
    return str(row.get("ID", row.name))


def snap_noaa_observation_points_to_wet_cells(paths: Paths, wet_zb_max_m: float = 0.0) -> None:
    """Snap NOAA validation observation points to nearest active wet grid cell.

    This is done after HydroMT builds the SFINCS case and before SFINCS is run,
    so point_zs in sfincs_his.nc is extracted from hydraulically meaningful wet
    cells instead of raw gauge coordinates that may fall on piers, seawalls,
    shoreline pixels, or hydroflattened positive-elevation cells.

    The function is intentionally safe and idempotent. It writes a report to
    agent_reports/noaa_obs_auto_snap_report.csv and only rewrites sfincs.obs when
    the snapped coordinates differ from the current file.
    """
    obs_geo = paths.gis / "noaa_validation_obs.geojson"
    obs_file = paths.out / "sfincs.obs"
    if not obs_geo.exists() or not obs_file.exists():
        log("NOAA observation snapping skipped; no validation GeoJSON or sfincs.obs file found.")
        return

    try:
        from hydromt_sfincs import SfincsModel  # type: ignore
    except Exception as exc:
        log(f"NOAA observation snapping skipped; hydromt_sfincs is unavailable: {exc}")
        return

    sf = SfincsModel(root=str(paths.out), mode="r")
    sf.read()

    if "dep" not in sf.grid or "msk" not in sf.grid:
        log("NOAA observation snapping skipped; HydroMT grid lacks dep/msk.")
        return

    dep_da = sf.grid["dep"]
    msk_da = sf.grid["msk"]

    dep = dep_da.values
    msk = msk_da.values
    if dep.ndim == 3:
        dep = dep[0]
    if msk.ndim == 3:
        msk = msk[0]

    if "x" in dep_da.coords and "y" in dep_da.coords:
        x = dep_da["x"].values
        y = dep_da["y"].values
    elif "x" in sf.grid and "y" in sf.grid:
        x = sf.grid["x"].values
        y = sf.grid["y"].values
    else:
        log("NOAA observation snapping skipped; HydroMT grid lacks x/y coordinates.")
        return

    # x/y can be 1D or 2D depending on HydroMT/SFINCS version.
    #
    # HydroMT regular grids commonly store dep/msk as (y, x). In that case
    # np.meshgrid(x, y, indexing="ij") incorrectly creates arrays with shape
    # (x, y), i.e. transposed relative to dep/msk. Resolve the orientation
    # explicitly from the hydraulic-grid shape so resumed regular/subgrid
    # workflows are robust across HydroMT versions.
    target_shape = dep.shape

    if x.ndim == 1 and y.ndim == 1:
        if len(y) == target_shape[0] and len(x) == target_shape[1]:
            # Standard raster convention: dep/msk dimensions are (y, x).
            x, y = np.meshgrid(x, y, indexing="xy")
        elif len(x) == target_shape[0] and len(y) == target_shape[1]:
            # Less common convention: dimensions are (x, y).
            x, y = np.meshgrid(x, y, indexing="ij")
        else:
            raise RuntimeError(
                "Cannot align HydroMT x/y coordinates with dep/msk grid: "
                f"dep shape={target_shape}, len(x)={len(x)}, len(y)={len(y)}"
            )

    elif x.ndim == 2 and y.ndim == 2:
        if x.shape == target_shape and y.shape == target_shape:
            pass
        elif x.T.shape == target_shape and y.T.shape == target_shape:
            log(
                "HydroMT coordinate arrays are transposed relative to dep/msk; "
                "transposing x/y for NOAA observation snapping."
            )
            x = x.T
            y = y.T
        else:
            raise RuntimeError(
                "Cannot align 2-D HydroMT x/y coordinates with dep/msk grid: "
                f"dep shape={target_shape}, x shape={x.shape}, y shape={y.shape}"
            )
    else:
        raise RuntimeError(
            "Unexpected HydroMT coordinate dimensionality for observation snapping: "
            f"x.ndim={x.ndim}, y.ndim={y.ndim}, dep shape={target_shape}"
        )

    if x.shape != target_shape or y.shape != target_shape:
        raise RuntimeError(
            "Internal coordinate alignment failure before NOAA observation snapping: "
            f"dep shape={target_shape}, x shape={x.shape}, y shape={y.shape}"
        )

    log(
        "NOAA observation snapping grid alignment: "
        f"dep/msk={target_shape}, x/y={x.shape}"
    )

    epsg = None
    try:
        epsg = int(sf.crs.to_epsg()) if sf.crs is not None else None
    except Exception:
        epsg = None
    if epsg is None:
        epsg = 4326

    gdf = gpd.read_file(obs_geo)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(f"EPSG:{epsg}")

    wet = (msk > 0) & np.isfinite(dep) & (dep <= wet_zb_max_m)
    if not np.any(wet):
        log("NOAA observation snapping skipped; no active wet cells found.")
        return

    rows: List[Dict[str, Any]] = []
    new_lines: List[str] = []
    dx_guess = None
    try:
        xs = np.unique(np.asarray(x)[np.isfinite(x)])
        if len(xs) > 1:
            dx_guess = float(np.nanmedian(np.diff(np.sort(xs))))
    except Exception:
        dx_guess = None

    for i, row in gdf.reset_index(drop=True).iterrows():
        sid = _station_id_from_row(row)
        gx = float(row.geometry.x)
        gy = float(row.geometry.y)

        d2 = (x - gx) ** 2 + (y - gy) ** 2
        raw_idx = np.unravel_index(np.nanargmin(d2), d2.shape)
        d2_wet = np.where(wet, d2, np.nan)
        wet_idx = np.unravel_index(np.nanargmin(d2_wet), d2_wet.shape)
        dist_m = float(np.sqrt(d2_wet[wet_idx]))
        snapped_x = float(x[wet_idx])
        snapped_y = float(y[wet_idx])

        rows.append({
            "obs_zero_based": i,
            "sfincs_station_one_based": i + 1,
            "noaa_station": sid,
            "raw_x": gx,
            "raw_y": gy,
            "raw_cell_x": float(x[raw_idx]),
            "raw_cell_y": float(y[raw_idx]),
            "raw_cell_zb_m": float(dep[raw_idx]),
            "raw_cell_msk": float(msk[raw_idx]),
            "snapped_x": snapped_x,
            "snapped_y": snapped_y,
            "snapped_cell_zb_m": float(dep[wet_idx]),
            "snapped_cell_msk": float(msk[wet_idx]),
            "snap_distance_m": dist_m,
            "snap_distance_grid_cells": float(dist_m / dx_guess) if dx_guess and dx_guess > 0 else np.nan,
        })
        new_lines.append(f"{snapped_x:.3f} {snapped_y:.3f} {sid}\n")

    report = pd.DataFrame(rows)
    out = paths.reports / "noaa_obs_auto_snap_report.csv"
    report.to_csv(out, index=False)
    log(f"Wrote NOAA observation snapping report: {out}")

    old_text = obs_file.read_text(encoding="utf-8") if obs_file.exists() else ""
    new_text = "".join(new_lines)
    if old_text.strip() == new_text.strip():
        log("sfincs.obs already uses snapped wet-cell observation coordinates.")
        return

    backup = paths.out / "sfincs.obs.raw_hydromt_points.bak"
    if not backup.exists() and obs_file.exists():
        shutil.copy2(obs_file, backup)
        log(f"Backed up raw HydroMT observation file: {backup}")
    obs_file.write_text(new_text, encoding="utf-8")
    log(f"Rewrote sfincs.obs with nearest active wet-cell coordinates: {obs_file}")

# -----------------------------------------------------------------------------
# Direct SFINCS run
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Direct SFINCS run
# -----------------------------------------------------------------------------


def run_sfincs_direct(cfg: Dict[str, Any], paths: Paths) -> None:
    """Run the SFINCS executable directly in the case folder.

    This is intentionally not a Slurm/sbatch workflow. It is meant for an
    interactive GPU node where the NVIDIA/HDF5/NetCDF modules are already loaded.
    The run is resumable: existing sfincs_map.nc/sfincs_his.nc are reused only
    when both are newer than the current built model inputs.
    """
    exe = Path(str(cfg["sfincs"]["executable"])).expanduser()
    if not exe.exists():
        raise RuntimeError(f"SFINCS executable not found: {exe}")

    inp = paths.out / str(cfg["sfincs"].get("input_file", "sfincs.inp"))
    if not inp.exists():
        raise RuntimeError(f"SFINCS input file not found: {inp}")

    map_nc = paths.out / "sfincs_map.nc"
    his_nc = paths.out / "sfincs_his.nc"

    # Resume correctly against the CURRENT built model. Existing hydraulic
    # outputs are reusable only when both output files exist and are at least as
    # new as the model inputs that determine the run. A HydroMT rebuild updates
    # sfincs.inp and, for this workflow, the subgrid/infiltration files.
    inp_keys = _read_sfincs_inp_keys(inp)
    model_inputs = [inp]
    for key in ("sbgfile", "smaxfile", "sefffile", "ksfile"):
        fn = inp_keys.get(key)
        if fn:
            fp = paths.out / fn
            if fp.exists():
                model_inputs.append(fp)
    newest_input_mtime = max(fp.stat().st_mtime_ns for fp in model_inputs)

    if map_nc.exists() and his_nc.exists():
        oldest_output_mtime = min(
            map_nc.stat().st_mtime_ns,
            his_nc.stat().st_mtime_ns,
        )
        if oldest_output_mtime >= newest_input_mtime:
            log("SFINCS outputs already exist for the current built inputs. Skipping direct run.")
            log(f"Existing map file: {map_nc}")
            log(f"Existing his file: {his_nc}")
            return

        log("Existing SFINCS outputs are older than the current built model inputs; rerunning SFINCS.")
        map_nc.unlink()
        his_nc.unlink()

    elif map_nc.exists() or his_nc.exists():
        log("Partial SFINCS output detected; removing it and rerunning SFINCS.")
        if map_nc.exists():
            map_nc.unlink()
        if his_nc.exists():
            his_nc.unlink()

    log("Running SFINCS directly from Python. No Slurm/sbatch is used.")
    log(f"Case directory: {paths.out}")
    log(f"Executable    : {exe}")

    log_file = paths.out / "run_gpu.log"
    with log_file.open("w", encoding="utf-8") as f:
        f.write("Running SFINCS directly from Python\n")
        f.write(f"Executable: {exe}\n")
        f.write(f"Working directory: {paths.out}\n")
        f.flush()

        proc = subprocess.run(
            [str(exe)],
            cwd=str(paths.out),
            stdout=f,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )

    if proc.returncode != 0:
        raise RuntimeError(f"SFINCS direct run failed with status {proc.returncode}. See {log_file}")

    log(f"SFINCS direct run finished successfully. Log: {log_file}")


# -----------------------------------------------------------------------------
# Main workflow
# -----------------------------------------------------------------------------


def main() -> int:
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_CONFIG)
    if not cfg_path.exists():
        raise SystemExit(f"Config file not found: {cfg_path}")
    cfg = read_yaml(cfg_path)
    model_mode = get_model_mode(cfg)

    out = Path(cfg["project"]["output_dir"]).expanduser()
    cache = Path(cfg["project"]["cache_dir"]).expanduser()
    paths = Paths(
        out=out,
        cache=cache,
        data=out / "agent_data",
        work=out / "agent_work",
        gis=out / "agent_gis",
        reports=out / "agent_reports",
        validation=out / "agent_validation",
        state=out / "agent_state.json",
    )
    for p in [paths.out, paths.cache, paths.data, paths.work, paths.gis, paths.reports, paths.validation]:
        ensure_dir(p)
    state = State(paths.state)
    log(f"Configuration: {cfg_path.resolve()}")
    log(f"SFINCS hydraulic mode: {model_mode}")
    log(f"Output directory: {paths.out}")

    inf_sig = infiltration_signature(cfg)
    if infiltration_enabled(cfg):
        icfg0=cfg.get("infiltration",{}); log(f"Infiltration: ENABLED | method={icfg0.get('method','cn_with_recovery')} | source={icfg0.get('source','nlcd_ssurgo')}")
        old_meta = state.data.get("meta", {}).get("hydromt_build", {})
        old_sig = old_meta.get("infiltration_signature")
        if state.is_done("hydromt_build") and old_sig != inf_sig:
            log(
                "Infiltration configuration differs from the completed HydroMT build; "
                "invalidating only the HydroMT build stage. Downloaded/preprocessed data are retained."
            )
            state.data.setdefault("done", {})["hydromt_build"] = False
            write_json(state.data, state.path)
    else:
        log("Infiltration: disabled")

    forc_sig = forcing_signature(cfg)
    old_forc_sig = state.data.get("meta", {}).get("hydromt_build", {}).get("forcing_signature")
    if state.is_done("hydromt_build") and old_forc_sig != forc_sig:
        log("Forcing/evaluation configuration changed; invalidating forcing-dependent build stages while retaining cached downloads.")
        for key in ("ocean_boundary", "validation_observations", "river_forcing", "era5", "aorc", "hydromt_build"):
            state.data.setdefault("done", {})[key] = False
        write_json(state.data, state.path)

    mode = cfg.get("workflow", {}).get("mode", "build_only")
    allowed_modes = {"build_only", "run_direct_only", "build_run_direct", "validation_only"}
    if mode not in allowed_modes:
        raise RuntimeError(
            f"Unsupported workflow.mode={mode!r}. Use one of: "
            "build_only, run_direct_only, build_run_direct, validation_only."
        )

    if mode == "validation_only":
        # Internet-enabled evaluation-data acquisition only. Do not require
        # GDAL/HydroMT and do not touch SFINCS model inputs or outputs.
        geo_meta = read_json(paths.reports / "geojson_report.json", {})
        if not geo_meta:
            geo_meta = extract_geojson_parts(Path(cfg["geojson"]["path"]), paths)
            state.mark("geojson", **geo_meta)
        ext_sig = external_validation_signature(cfg)
        ext_files = prepare_external_validation(cfg, geo_meta, paths)
        state.mark("external_validation", signature=ext_sig, files=ext_files)
        log("Validation-only acquisition complete. Hydraulic model files were not modified.")
        return 0

    if mode == "run_direct_only":
        if infiltration_enabled(cfg):
            built_sig = state.data.get("meta", {}).get("hydromt_build", {}).get("infiltration_signature")
            if built_sig != inf_sig or not state.is_done("hydromt_build"):
                raise RuntimeError("run_direct_only requested, but the current infiltration configuration has not been built. Set workflow.mode: build_only first.")
        built_forc_sig = state.data.get("meta", {}).get("hydromt_build", {}).get("forcing_signature")
        if built_forc_sig != forc_sig or not state.is_done("hydromt_build"):
            raise RuntimeError("run_direct_only requested, but the current forcing configuration has not been built. Set workflow.mode: build_only first.")
        geo_meta = read_json(paths.reports / "geojson_report.json", {})
        if not geo_meta:
            geo_meta = extract_geojson_parts(Path(cfg["geojson"]["path"]), paths)
        validate_built_model(cfg, paths)
        snap_noaa_observation_points_to_wet_cells(paths)
        run_sfincs_direct(cfg, paths)
        return 0

    # Required command-line tools for online raster extraction and HydroMT.
    require_executable("gdalwarp")
    require_executable("gdalbuildvrt")
    require_executable("hydromt")
    if infiltration_enabled(cfg) and str(cfg.get("infiltration",{}).get("method","cn_with_recovery")).lower()=="cn_with_recovery":
        require_executable("gdal_rasterize")

    if not state.is_done("geojson"):
        geo_meta = extract_geojson_parts(Path(cfg["geojson"]["path"]), paths)
        state.mark("geojson", **geo_meta)
    else:
        geo_meta = read_json(paths.reports / "geojson_report.json", {})
        log("Skipping GeoJSON parsing; already complete.")

    # Optional external validation/benchmark products are acquired on the
    # internet-enabled build node and never alter the hydraulic model. Re-run
    # discovery each time so newly supplied API credentials can complete a
    # previous metadata-only provider; downloaded assets are still cached.
    ext_sig = external_validation_signature(cfg)
    ext_files = prepare_external_validation(cfg, geo_meta, paths)
    state.mark("external_validation", signature=ext_sig, files=ext_files)

    model_crs = f"EPSG:{geo_meta.get('auto_utm_epsg')}" if cfg["mesh"].get("crs") == "utm" else str(cfg["mesh"].get("crs"))
    bbox_topo = bbox_buffer_km(geo_meta["bbox"], float(cfg["topobathy"].get("buffer_km", 10)))

    # Topobathy provider: default CUDEM + GEBCO, or user rasters.
    tcfg = cfg.get("topobathy", {})
    psrc = str(tcfg.get("primary", {}).get("source", "NOAA_CUDEM_1_9_ARCSEC")).lower()
    if psrc == "file":
        cudem = copy_user_file(tcfg["primary"].get("path"), paths.data / "user_topobathy_primary.tif", "topobathy.primary.path")
    else:
        cudem = paths.data / "cudem_1_9arcsec_model_crs.tif"
        if not state.is_done("cudem"):
            src = "/vsicurl/" + tcfg["primary"]["vrt_url"]
            gdalwarp_remote(src, cudem, bbox_topo, model_crs, float(tcfg["primary"].get("output_resolution_m", 3)))
            state.mark("cudem", path=str(cudem))
        else: log("Skipping CUDEM; already complete.")
    gcfg = tcfg.get("gapfill", {}); gsrc = str(gcfg.get("source", "GEBCO_2024_15_ARCSEC")).lower()
    gebco = None
    if gsrc == "none":
        log("Topobathy gapfill: none")
    elif gsrc == "file":
        gebco = copy_user_file(gcfg.get("path"), paths.data / "user_topobathy_gapfill.tif", "topobathy.gapfill.path")
    else:
        gebco = paths.data / "gebco_2024_model_crs.tif"
        if not state.is_done("gebco"):
            src = "/vsicurl/" + gcfg["cog_url"]
            gdalwarp_remote(src, gebco, bbox_topo, model_crs, float(gcfg.get("output_resolution_m", 500)))
            state.mark("gebco", path=str(gebco))
        else: log("Skipping GEBCO; already complete.")

    lcfg = cfg.get("landcover", {})
    lsource = str(lcfg.get("source", "ESA_WorldCover_2021_v200")).lower()
    if lsource == "file":
        esa = copy_user_file(lcfg.get("path"), paths.data / "user_landcover.tif", "landcover.path")
        if lcfg.get("manning_table"):
            manning_csv = copy_user_file(lcfg.get("manning_table"), paths.data / "user_manning_table.csv", "landcover.manning_table")
        else:
            manning_csv = paths.data / "esa_worldcover_manning_default.csv"; write_esa_manning_table(manning_csv)
    else:
        if model_mode == "subgrid":
            lc_res = float(cfg.get("subgrid", {}).get("landcover_output_resolution_m", 10.0)); esa = paths.data / f"esa_worldcover_model_crs_subgrid_{lc_res:g}m.tif"
        else: esa = paths.data / "esa_worldcover_model_crs.tif"
        if (not state.is_done("esa_worldcover")) or (not file_ready(esa, 1024)):
            esa = download_esa_worldcover(cfg, geo_meta["bbox"], paths, model_crs); state.mark("esa_worldcover", path=str(esa))
        else: log(f"Skipping ESA WorldCover; already complete: {esa}")
        manning_csv = paths.data / "esa_worldcover_manning_default.csv"; write_esa_manning_table(manning_csv)

    # Ocean boundary provider.
    if not state.is_done("ocean_boundary"):
        wl_ts, wl_locs = prepare_ocean_boundary(cfg, geo_meta, paths); state.mark("ocean_boundary", timeseries=str(wl_ts), locations=str(wl_locs))
    else:
        meta=state.data.get("meta",{}).get("ocean_boundary",{}); wl_ts=Path(meta.get("timeseries","")); wl_locs=Path(meta.get("locations",""))
        if not wl_ts.exists() or not wl_locs.exists(): wl_ts,wl_locs=prepare_ocean_boundary(cfg,geo_meta,paths)

    if not state.is_done("validation_observations"):
        obs_locs = prepare_validation_observations(cfg, geo_meta, paths); state.mark("validation_observations", locations=str(obs_locs) if obs_locs else "")
    else:
        val=state.data.get("meta",{}).get("validation_observations",{}).get("locations",""); obs_locs=Path(val) if val and Path(val).exists() else None

    if not state.is_done("river_forcing"):
        dis_ts,dis_locs=prepare_river_forcing(cfg,geo_meta,paths); state.mark("river_forcing",timeseries=str(dis_ts) if dis_ts else "",locations=str(dis_locs) if dis_locs else "")
    else:
        meta=state.data.get("meta",{}).get("river_forcing",{}); a=meta.get("timeseries",""); b=meta.get("locations",""); dis_ts=Path(a) if a and Path(a).exists() else None; dis_locs=Path(b) if b and Path(b).exists() else None

    # Atmospheric providers are independent. ERA5 and NOAA AORC are each
    # downloaded once when requested. AORC may still point to a user-supplied
    # local NetCDF; omitting path activates the integrated NOAA S3 subsetter.
    fcfg = forcing_cfg(cfg); wind_grid=pressure_grid=precip_grid=None
    atm_sources={k:_provider_source(cfg,k) for k in ("wind","pressure","precipitation")}
    era_ws=era_tp=None
    if "era5" in atm_sources.values():
        if not state.is_done("era5"):
            era_ws,era_tp=download_era5(cfg,geo_meta,paths); state.mark("era5",wind_pressure=str(era_ws),precip=str(era_tp))
        else:
            era_ws=paths.data/"era5_wind_pressure_pa.nc"; era_tp=paths.data/"era5_precip_mm_cumulative.nc"

    auto_aorc_kinds = [
        kind for kind in ("wind", "pressure", "precipitation")
        if atm_sources[kind] == "aorc" and not fcfg.get(kind, {}).get("path")
    ]
    aorc_auto = None
    if auto_aorc_kinds:
        aorc_auto = download_aorc_subset(cfg, geo_meta, paths, auto_aorc_kinds)
        state.mark("aorc", path=str(aorc_auto), components=auto_aorc_kinds)

    for kind in ("wind","pressure","precipitation"):
        src=atm_sources[kind]; pcfg=dict(fcfg.get(kind,{}) or {})
        if src=="none": outp=None
        elif src=="era5": outp=era_tp if kind=="precipitation" else era_ws
        elif src in ({"aorc","file"} if kind in {"wind","pressure"} else {"aorc","mrms","file"}):
            if src == "aorc" and not pcfg.get("path"):
                if aorc_auto is None:
                    raise RuntimeError(f"Automatic AORC subset was not resolved for forcing.{kind}")
                pcfg["path"] = str(aorc_auto)
            outp=standardize_atmospheric_file(cfg,pcfg,kind,paths)
        else: raise RuntimeError(f"Unsupported forcing.{kind}.source={src!r}")
        if kind=="wind": wind_grid=outp
        elif kind=="pressure": pressure_grid=outp
        else: precip_grid=outp

    # Fail before HydroMT build if a legacy/custom pressure conversion produced
    # hPa/mbar instead of the Pa required by SFINCS.
    validate_pressure_grid_pa(pressure_grid)

    infiltration_inputs = prepare_infiltration_inputs(cfg, geo_meta, paths, model_crs)

    # Compatibility repairs are deliberately applied on every invocation so
    # partially completed builds can resume without redownloading forcing data.
    #
    # HydroMT 0.9.x cannot slice timezone-aware DataFrame indices, and
    # hydromt_sfincs 1.0.x expects tabular point-forcing column names that can
    # be converted to integer IDs.
    sanitize_hydromt_timeseries_csv(wl_ts)
    sanitize_hydromt_timeseries_csv(dis_ts)
    normalize_hydromt_point_forcing_ids(
        wl_ts, wl_locs, source_id_field="boundary_source_id"
    )
    normalize_hydromt_point_forcing_ids(
        dis_ts, dis_locs, source_id_field="river_source_id"
    )

    # Catalog/build now consume canonical provider outputs rather than source-specific names.
    if gebco is None:
        # Reuse primary raster as a harmless second source only for the legacy function signature;
        # dep source assembly below filters duplicates.
        gebco = cudem
    catalog = make_hydromt_catalog(
        cfg, paths, cudem, gebco, esa, manning_csv, wind_grid, pressure_grid, precip_grid,
        infiltration_inputs=infiltration_inputs,
    )
    build_cfg = make_hydromt_build_config(cfg, paths, geo_meta, wl_ts, wl_locs, obs_locs, dis_ts, dis_locs, wind_grid, pressure_grid, precip_grid)
    write_forcing_provenance(cfg, paths, {"ocean_timeseries":wl_ts,"ocean_locations":wl_locs,"river_timeseries":dis_ts,"river_locations":dis_locs,"wind":wind_grid,"pressure":pressure_grid,"precipitation":precip_grid})

    if not state.is_done("hydromt_build"):
        run_hydromt_build(cfg, paths, geo_meta, catalog, build_cfg)
        validate_built_model(cfg, paths)
        state.mark(
            "hydromt_build",
            catalog=str(catalog),
            build_config=str(build_cfg),
            model_mode=model_mode,
            infiltration_signature=inf_sig,
            infiltration_enabled=infiltration_enabled(cfg),
            forcing_signature=forc_sig,
        )
    else:
        log("Skipping HydroMT build; already complete.")
        validate_built_model(cfg, paths)

    snap_noaa_observation_points_to_wet_cells(paths)

    if mode == "build_only":
        log("Build-only mode complete. SFINCS was not run.")
        log(f"Next step on an interactive GPU node: set workflow.mode: run_direct_only and run python -u sfincs_agent.py {cfg_path}")
        return 0

    if mode == "build_run_direct":
        run_sfincs_direct(cfg, paths)
        return 0

    raise RuntimeError(f"Unexpected workflow.mode at end of build: {mode}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("FAILED")
        log(str(exc))
        raise
