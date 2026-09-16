#!/usr/bin/env python3
"""
sfincs_attribution.py

Factorial compound-flood attribution and exposure engine for AutoCF.

For three drivers:
    C = coastal forcing (ocean boundary + wind + pressure)
    P = precipitation
    R = river discharge

the engine creates the exact 2^3 factorial:
    NONE, C, P, R, CP, CR, PR, CPR

The already-completed parent SFINCS run is reused as CPR; only seven additional
scenario runs are required. Static model inputs are hard-linked where possible,
while forcing files that must change are detached before editing.

Outputs:
  - exact Shapley attribution of maximum water depth to C/P/R
  - absolute flooded-area/building/population impacts and Shapley attribution
  - percentage of the analysis-land population affected
  - CDC/ATSDR Social Vulnerability Index (SVI) burden by national quartile
  - flood duration, area-hours, building-hours, and duration attribution
  - Driver Impact Shift (DIS): consequence share minus footprint share
  - compound amplification: H_CPR - max(H_C, H_P, H_R), with meaningful thresholds
  - Overture building exposure-depth curves
  - WorldPop population exposure-depth curves
  - a separately labelled NONE-background excess-depth diagnostic
  - static-land masking using ESA WorldCover

Remote sensing is not used.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import xarray as xr
from pyproj import CRS, Transformer
from shapely.geometry import box, shape, mapping
from shapely.ops import transform as shp_transform, unary_union

from sfincs_config import read_config
from run_layout import refresh_artifact_catalog, run_layout

DRIVERS = ("coastal", "precipitation", "river")
SHORT = {"coastal": "c", "precipitation": "p", "river": "r"}


def log(msg: str) -> None:
    print(f"[attribution {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_sfincs_inp(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line:
            k, v = line.split("=", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            k, v = parts
        out[k.strip().lower()] = v.strip()
    return out


def parse_sfincs_datetime(text: str) -> datetime:
    cleaned = text.strip().replace("T", " ")
    for fmt in (
        "%Y%m%d %H%M%S",
        "%Y%m%d%H%M%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H%M%S",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            pass
    raise ValueError(f"Cannot parse SFINCS datetime {text!r}")


def _scenario_code(active: FrozenSet[str]) -> str:
    bits = "".join("1" if d in active else "0" for d in DRIVERS)
    label = "".join(SHORT[d] for d in DRIVERS if d in active) or "none"
    return f"{bits}_{label}"


def all_subsets() -> List[FrozenSet[str]]:
    out = []
    for r in range(4):
        for comb in itertools.combinations(DRIVERS, r):
            out.append(frozenset(comb))
    return out


def analysis_root(cfg: Dict[str, Any]) -> Path:
    acfg = cfg.get("analysis", {}) or {}
    custom = acfg.get("root")
    if custom:
        return Path(str(custom)).expanduser().resolve()
    return run_layout(cfg).analysis


def analysis_exposure_root(cfg: Dict[str, Any]) -> Path:
    layout = run_layout(cfg)
    return layout.analysis / ("exposure" if layout.organized else "exposure_data")


def _input_files(run: Path) -> List[Path]:
    inp = run / "sfincs.inp"
    if not inp.exists():
        raise FileNotFoundError(inp)
    cfg = parse_sfincs_inp(inp)
    files = [inp]
    for key, value in cfg.items():
        if not (key.endswith("file") or key in {"indexfile"}):
            continue
        p = Path(value)
        if p.is_absolute():
            # Absolute files remain shared and are not copied into scenarios.
            continue
        src = run / p
        if src.exists() and src.is_file():
            files.append(src)
    # Preserve deduplicated order.
    seen = set()
    out = []
    for p in files:
        rp = p.resolve()
        if rp not in seen:
            out.append(p)
            seen.add(rp)
    return out


def _hardlink_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _detach(path: Path) -> None:
    """Break a hardlink before modifying a scenario forcing file."""
    if not path.exists():
        raise FileNotFoundError(path)
    tmp = path.with_name(path.name + ".detach")
    shutil.copy2(path, tmp)
    tmp.replace(path)


def _quiet_seconds(inp: Dict[str, str], hours: Sequence[float]) -> Tuple[float, float]:
    h0, h1 = float(hours[0]), float(hours[1])
    if "tref" in inp and "tstart" in inp:
        tref = parse_sfincs_datetime(inp["tref"])
        tstart = parse_sfincs_datetime(inp["tstart"])
        t0 = (tstart - tref).total_seconds()
    else:
        t0 = 0.0
    return t0 + h0 * 3600.0, t0 + h1 * 3600.0


def _neutralize_table(path: Path, inp: Dict[str, str], quiet_hours: Sequence[float], mode: str) -> Dict[str, Any]:
    _detach(path)
    arr = np.loadtxt(path, dtype=float)
    one = arr.ndim == 1
    if one:
        arr = arr[None, :]
    if arr.shape[1] < 2:
        raise RuntimeError(f"Expected time + value columns in {path}; shape={arr.shape}")
    t0, t1 = _quiet_seconds(inp, quiet_hours)
    mask = (arr[:, 0] >= t0) & (arr[:, 0] <= t1)
    if not np.any(mask):
        # Fall back to first 10% rather than silently using event peak.
        n = max(1, int(math.ceil(0.1 * len(arr))))
        mask = np.zeros(len(arr), dtype=bool)
        mask[:n] = True
    if mode == "zero":
        base = np.zeros(arr.shape[1] - 1, dtype=float)
    elif mode == "quiet_mean":
        base = np.nanmean(arr[mask, 1:], axis=0)
    else:
        base = np.nanmedian(arr[mask, 1:], axis=0)
    arr[:, 1:] = base[None, :]
    np.savetxt(path, arr, fmt="%.8f")
    return {
        "path": str(path),
        "mode": mode,
        "quiet_rows": int(mask.sum()),
        "baseline_min": float(np.nanmin(base)),
        "baseline_max": float(np.nanmax(base)),
    }


def _time_mask_from_xarray(ds: xr.Dataset, quiet_hours: Sequence[float]) -> Optional[np.ndarray]:
    if "time" not in ds.coords:
        return None
    t = pd.DatetimeIndex(pd.to_datetime(ds["time"].values))
    if len(t) == 0:
        return None
    h0, h1 = float(quiet_hours[0]), float(quiet_hours[1])
    t0 = t[0] + pd.Timedelta(hours=h0)
    t1 = t[0] + pd.Timedelta(hours=h1)
    mask = (t >= t0) & (t <= t1)
    if not mask.any():
        n = max(1, int(math.ceil(0.1 * len(t))))
        mask = np.zeros(len(t), dtype=bool)
        mask[:n] = True
    return np.asarray(mask)


def _write_modified_dataset(path: Path, modifier) -> Dict[str, Any]:
    _detach(path)
    ds = xr.open_dataset(path)
    try:
        out = ds.load()
    finally:
        ds.close()
    report = modifier(out)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    out.to_netcdf(tmp)
    tmp.replace(path)
    report["path"] = str(path)
    return report


def _zero_gridded(path: Path) -> Dict[str, Any]:
    def mod(ds: xr.Dataset):
        changed = []
        for name, da in ds.data_vars.items():
            if "time" in da.dims and np.issubdtype(da.dtype, np.number):
                ds[name] = xr.zeros_like(da)
                changed.append(name)
        if not changed:
            raise RuntimeError(f"No numeric time-dependent variable found in {path}")
        return {"mode": "zero", "variables": changed}
    return _write_modified_dataset(path, mod)


def _quiet_gridded(path: Path, quiet_hours: Sequence[float]) -> Dict[str, Any]:
    def mod(ds: xr.Dataset):
        mask = _time_mask_from_xarray(ds, quiet_hours)
        if mask is None:
            raise RuntimeError(f"No time coordinate in {path}")
        changed = []
        for name, da in list(ds.data_vars.items()):
            if "time" not in da.dims or not np.issubdtype(da.dtype, np.number):
                continue
            q = da.isel(time=np.flatnonzero(mask)).median("time", skipna=True)
            # Broadcast constant field across original time axis.
            ds[name] = xr.concat(
                [q] * da.sizes["time"], dim=da["time"]
            ).transpose(*da.dims)
            ds[name].attrs.update(da.attrs)
            changed.append(name)
        if not changed:
            raise RuntimeError(f"No numeric time-dependent variable found in {path}")
        return {"mode": "quiet_median", "variables": changed, "quiet_steps": int(mask.sum())}
    return _write_modified_dataset(path, mod)


def _resolve_input(run: Path, inp: Dict[str, str], key: str) -> Optional[Path]:
    value = inp.get(key)
    if not value:
        return None
    p = Path(value)
    if p.is_absolute():
        # Never modify an absolute shared file in place. Copy it into scenario
        # root using the original basename, then rewrite sfincs.inp reference.
        return p
    return run / p


def _set_inp_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(errors="ignore").splitlines()
    pat = re.compile(rf"^(\s*){re.escape(key)}\s*(?:=|\s)\s*.*$", re.I)
    out, done = [], False
    for line in lines:
        if not done and pat.match(line):
            out.append(f"{key:<22s} = {value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key:<22s} = {value}")
    path.write_text("\n".join(out) + "\n")


def _localize_absolute_forcing(scenario: Path, inp: Dict[str, str], key: str) -> Optional[Path]:
    p = _resolve_input(scenario, inp, key)
    if p is None:
        return None
    if p.is_absolute() and not str(p).startswith(str(scenario.resolve())):
        if not p.exists():
            raise FileNotFoundError(p)
        target = scenario / p.name
        shutil.copy2(p, target)
        _set_inp_value(scenario / "sfincs.inp", key, target.name)
        inp[key] = target.name
        return target
    return p


def neutralize_scenario(
    scenario: Path,
    active: FrozenSet[str],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    acfg = cfg.get("analysis", {}).get("attribution", {}) or {}
    quiet = acfg.get("quiet_window_hours", [0, 12])
    off = acfg.get("off_baseline", {}) or {}
    inp_path = scenario / "sfincs.inp"
    inp = parse_sfincs_inp(inp_path)
    report: Dict[str, Any] = {"active_drivers": sorted(active), "changes": []}

    if "coastal" not in active:
        # Coastal forcing is treated as the coupled coastal meteorological
        # driver: open-boundary water level + wind + pressure.
        bzs = _localize_absolute_forcing(scenario, inp, "bzsfile")
        if bzs is not None and bzs.exists():
            report["changes"].append(
                {"component": "ocean_boundary", **_neutralize_table(
                    bzs, inp, quiet, str(off.get("coastal_boundary", "quiet_median"))
                )}
            )
        wind = _localize_absolute_forcing(scenario, inp, "netamuamvfile")
        if wind is not None and wind.exists():
            report["changes"].append({"component": "wind", **_zero_gridded(wind)})
        press = _localize_absolute_forcing(scenario, inp, "netampfile")
        if press is not None and press.exists():
            report["changes"].append({"component": "pressure", **_quiet_gridded(press, quiet)})

    if "precipitation" not in active:
        rain = _localize_absolute_forcing(scenario, inp, "netamprfile")
        if rain is not None and rain.exists():
            report["changes"].append({"component": "precipitation", **_zero_gridded(rain)})

    if "river" not in active:
        dis = _localize_absolute_forcing(scenario, inp, "disfile")
        if dis is not None and dis.exists():
            mode = str(off.get("river", "quiet_median"))
            report["changes"].append(
                {"component": "river", **_neutralize_table(dis, inp, quiet, mode)}
            )

    return report


def prepare_scenarios(cfg: Dict[str, Any], force: bool = False) -> Path:
    run = run_layout(cfg).model
    if not (run / "sfincs.inp").exists():
        raise RuntimeError(f"Base SFINCS run is not built: {run}")
    root = analysis_root(cfg)
    scen_root = root / "scenarios"
    scen_root.mkdir(parents=True, exist_ok=True)

    base_files = _input_files(run)
    manifest: Dict[str, Any] = {
        "base_run": str(run),
        "drivers": list(DRIVERS),
        "note": (
            "CPR reuses the already-completed base run. The seven other factorial "
            "scenarios use identical static inputs and only neutralize excluded drivers."
        ),
        "scenarios": {},
    }

    full = frozenset(DRIVERS)
    for active in all_subsets():
        code = _scenario_code(active)
        if active == full:
            manifest["scenarios"][code] = {
                "active": sorted(active),
                "path": str(run),
                "reuses_base_run": True,
                "changes": [],
            }
            continue

        dst = scen_root / code
        if force and dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)

        # A completed counterfactual is an immutable reusable result unless the
        # caller explicitly requested recreation.  Preparing the workspace is
        # intentionally safe to repeat: it must not detach inputs, neutralize
        # forcing, or remove outputs from a scenario that already completed.
        # This also keeps the manifest current when an earlier AutoCF session
        # prepared and ran the scenario.
        completed_map = dst / "sfincs_map.nc"
        if completed_map.is_file() and not force:
            definition_path = dst / "scenario_definition.json"
            changes: List[Dict[str, Any]] = []
            if definition_path.is_file():
                try:
                    previous = json.loads(definition_path.read_text(encoding="utf-8"))
                    prior_changes = previous.get("changes", [])
                    if isinstance(prior_changes, list):
                        changes = prior_changes
                except (OSError, ValueError, TypeError):
                    pass
            manifest["scenarios"][code] = {
                "active": sorted(active),
                "path": str(dst),
                "reuses_base_run": False,
                "changes": changes,
            }
            log(f"preserved {code}: completed sfincs_map.nc found")
            continue

        for src in base_files:
            rel = src.relative_to(run)
            target = dst / rel
            if target.exists() and not force:
                continue
            _hardlink_or_copy(src, target)

        # sfincs.inp is always detached because absolute forcing references may
        # need to be localized for a scenario.
        _detach(dst / "sfincs.inp")
        changes = neutralize_scenario(dst, active, cfg)

        manifest["scenarios"][code] = {
            "active": sorted(active),
            "path": str(dst),
            "reuses_base_run": False,
            "changes": changes["changes"],
        }
        (dst / "scenario_definition.json").write_text(
            json.dumps(manifest["scenarios"][code], indent=2), encoding="utf-8"
        )
        log(f"prepared {code}: active={sorted(active)}")

    mpath = root / "scenario_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return mpath


def _sfincs_executable(cfg: Dict[str, Any]) -> str:
    runtime = cfg.get("runtime", {}) or {}
    sf = runtime.get("sfincs", {}) or {}
    exe = str(sf.get("executable", os.environ.get("SFINCS_EXECUTABLE", "sfincs")))
    if "/" in exe:
        p = Path(os.path.expandvars(os.path.expanduser(exe)))
        if not p.exists():
            raise FileNotFoundError(f"SFINCS executable not found: {p}")
        return str(p)
    found = shutil.which(exe)
    if not found:
        raise RuntimeError(
            f"SFINCS executable {exe!r} not found. Set runtime.sfincs.executable "
            "or SFINCS_EXECUTABLE."
        )
    return found


def run_scenarios(cfg: Dict[str, Any], force: bool = False) -> None:
    root = analysis_root(cfg)
    manifest_path = root / "scenario_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("Run analysis-prepare first.")
    manifest = json.loads(manifest_path.read_text())
    exe = _sfincs_executable(cfg)

    for code, rec in manifest["scenarios"].items():
        if rec.get("reuses_base_run"):
            log(f"{code}: base full scenario already complete; not rerunning")
            continue
        run = Path(rec["path"])
        map_nc = run / "sfincs_map.nc"
        if map_nc.exists() and not force:
            log(f"{code}: existing sfincs_map.nc found; skipping")
            continue
        if force:
            for p in (run / "sfincs_map.nc", run / "sfincs_his.nc"):
                if p.exists():
                    p.unlink()
        log(f"{code}: running SFINCS offline in {run}")
        log_path = run / "run_attribution.log"
        with log_path.open("w", encoding="utf-8") as f:
            proc = subprocess.run([exe], cwd=str(run), stdout=f, stderr=subprocess.STDOUT)
        if proc.returncode != 0 or not map_nc.exists():
            raise RuntimeError(
                f"Scenario {code} failed (status={proc.returncode}). See {log_path}"
            )
        log(f"{code}: complete")


def _epsg_from_map(ds: xr.Dataset, run: Path) -> int:
    if "crs" in ds:
        try:
            return int(np.asarray(ds["crs"].values).reshape(-1)[0])
        except Exception:
            pass
    for key in ("epsg", "crs"):
        if key in ds.attrs:
            try:
                return int(ds.attrs[key])
            except Exception:
                pass
    inp = parse_sfincs_inp(run / "sfincs.inp")
    if "epsg" in inp:
        return int(float(inp["epsg"]))
    raise RuntimeError(f"Cannot determine EPSG for {run}")


def extract_max_depth(run: Path) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    ds = xr.open_dataset(run / "sfincs_map.nc")
    try:
        if "hmax" in ds:
            da = ds["hmax"]
            if "timemax" in da.dims:
                da = da.max("timemax")
            depth = np.asarray(da.values, dtype=np.float32)
        else:
            if "zsmax" in ds:
                zs = ds["zsmax"]
                if "timemax" in zs.dims:
                    zs = zs.max("timemax")
            elif "zs" in ds:
                zs = ds["zs"]
                if "time" in zs.dims:
                    zs = zs.max("time")
            else:
                raise RuntimeError(f"No hmax/zsmax/zs in {run/'sfincs_map.nc'}")
            if "zb" not in ds:
                raise RuntimeError("zs-based depth extraction needs zb")
            depth = np.asarray(zs.values - ds["zb"].values, dtype=np.float32)
        depth = np.squeeze(depth)
        x = np.asarray(ds["x"].values, dtype=float)
        y = np.asarray(ds["y"].values, dtype=float)
        if x.ndim == 1 and y.ndim == 1:
            x, y = np.meshgrid(x, y)
        if x.shape != depth.shape or y.shape != depth.shape:
            if x.T.shape == depth.shape and y.T.shape == depth.shape:
                x, y = x.T, y.T
            else:
                raise RuntimeError(f"Grid/depth shape mismatch: depth={depth.shape}, x={x.shape}, y={y.shape}")
        if "msk" in ds:
            msk = np.squeeze(np.asarray(ds["msk"].values))
            depth = np.where(msk > 0, depth, np.nan)
        depth = np.where(np.isfinite(depth), np.maximum(depth, 0.0), np.nan).astype(np.float32)
        return depth, x, y, _epsg_from_map(ds, run)
    finally:
        ds.close()


def _raster_transform(x: np.ndarray, y: np.ndarray):
    import rasterio.transform
    # Attribution is computed on the SFINCS computational grid. The current
    # agent builds non-rotated grids by default. Detect rotation rather than
    # silently writing a wrong GeoTIFF.
    if x.ndim != 2 or y.ndim != 2:
        raise RuntimeError("Expected 2D x/y center arrays")
    row_x_var = np.nanmax(np.abs(np.diff(x, axis=0))) if x.shape[0] > 1 else 0
    col_y_var = np.nanmax(np.abs(np.diff(y, axis=1))) if y.shape[1] > 1 else 0
    dx = float(np.nanmedian(np.diff(x, axis=1))) if x.shape[1] > 1 else np.nan
    dy = float(np.nanmedian(np.diff(y, axis=0))) if y.shape[0] > 1 else np.nan
    if not np.isfinite(dx) or not np.isfinite(dy) or dx == 0 or dy == 0:
        raise RuntimeError("Cannot infer computational-grid spacing")
    if row_x_var > 1e-3 * abs(dx) or col_y_var > 1e-3 * abs(dy):
        raise RuntimeError(
            "Rotated computational grid detected. Scalar attribution still works, "
            "but GeoTIFF export currently requires model.rotated=false."
        )
    left = float(x[0, 0] - dx / 2)
    top = float(y[0, 0] - dy / 2)
    return rasterio.transform.Affine(dx, 0, left, 0, dy, top), abs(dx * dy)


def _write_tif(path: Path, arr: np.ndarray, x: np.ndarray, y: np.ndarray, epsg: int) -> Path:
    import rasterio
    transform, _ = _raster_transform(x, y)
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": arr.shape[0],
        "width": arr.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": f"EPSG:{epsg}",
        "transform": transform,
        "nodata": np.nan,
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.asarray(arr, dtype=np.float32), 1)
    return path


def _key(active: FrozenSet[str]) -> str:
    return _scenario_code(active)


def shapley_arrays(values: Dict[FrozenSet[str], np.ndarray]) -> Dict[str, np.ndarray]:
    n = len(DRIVERS)
    out = {}
    for player in DRIVERS:
        acc = None
        others = [d for d in DRIVERS if d != player]
        for r in range(len(others) + 1):
            for comb in itertools.combinations(others, r):
                s = frozenset(comb)
                w = math.factorial(len(s)) * math.factorial(n - len(s) - 1) / math.factorial(n)
                term = (values[s | {player}] - values[s]) * w
                acc = term if acc is None else acc + term
        out[player] = np.asarray(acc, dtype=np.float32)
    return out


def shapley_scalars(values: Dict[FrozenSet[str], float]) -> Dict[str, float]:
    n = len(DRIVERS)
    out = {}
    for player in DRIVERS:
        phi = 0.0
        others = [d for d in DRIVERS if d != player]
        for r in range(len(others) + 1):
            for comb in itertools.combinations(others, r):
                s = frozenset(comb)
                w = math.factorial(len(s)) * math.factorial(n - len(s) - 1) / math.factorial(n)
                phi += w * (float(values[s | {player}]) - float(values[s]))
        out[player] = float(phi)
    return out


def factorial_terms_arrays(values: Dict[FrozenSet[str], np.ndarray]) -> Dict[str, np.ndarray]:
    """Exact 2^3 inclusion-exclusion decomposition.

    Unlike Shapley values, which allocate interaction effects back to drivers,
    these terms retain pairwise and three-way compound interactions explicitly.
    Their sum equals H_CPR - H_NONE exactly (up to floating-point roundoff).
    """
    z = values[frozenset()]
    c = values[frozenset({"coastal"})]
    p = values[frozenset({"precipitation"})]
    r = values[frozenset({"river"})]
    cp = values[frozenset({"coastal", "precipitation"})]
    cr = values[frozenset({"coastal", "river"})]
    pr = values[frozenset({"precipitation", "river"})]
    cpr = values[frozenset(DRIVERS)]
    return {
        "main_coastal": np.asarray(c - z, dtype=np.float32),
        "main_precipitation": np.asarray(p - z, dtype=np.float32),
        "main_river": np.asarray(r - z, dtype=np.float32),
        "interaction_coastal_precipitation": np.asarray(cp - c - p + z, dtype=np.float32),
        "interaction_coastal_river": np.asarray(cr - c - r + z, dtype=np.float32),
        "interaction_precipitation_river": np.asarray(pr - p - r + z, dtype=np.float32),
        "interaction_coastal_precipitation_river": np.asarray(
            cpr - cp - cr - pr + c + p + r - z, dtype=np.float32
        ),
    }


def factorial_terms_scalars(values: Dict[FrozenSet[str], float]) -> Dict[str, float]:
    z = float(values[frozenset()])
    c = float(values[frozenset({"coastal"})])
    p = float(values[frozenset({"precipitation"})])
    r = float(values[frozenset({"river"})])
    cp = float(values[frozenset({"coastal", "precipitation"})])
    cr = float(values[frozenset({"coastal", "river"})])
    pr = float(values[frozenset({"precipitation", "river"})])
    cpr = float(values[frozenset(DRIVERS)])
    return {
        "main_coastal": c - z,
        "main_precipitation": p - z,
        "main_river": r - z,
        "interaction_coastal_precipitation": cp - c - p + z,
        "interaction_coastal_river": cr - c - r + z,
        "interaction_precipitation_river": pr - p - r + z,
        "interaction_coastal_precipitation_river": cpr - cp - cr - pr + c + p + r - z,
    }


def _domain_bbox(cfg: Dict[str, Any]) -> Tuple[float, float, float, float]:
    g = gpd.read_file(cfg["geometry"]["path"])
    if g.crs is None:
        g = g.set_crs(4326)
    g = g.to_crs(4326)
    if "role" in g.columns:
        dom = g[g["role"].astype(str).str.lower().eq("domain")]
        if not dom.empty:
            return tuple(float(v) for v in dom.total_bounds)
    p = g[g.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    return tuple(float(v) for v in p.total_bounds)


def prepare_overture_buildings(cfg: Dict[str, Any], force: bool = False) -> Optional[Path]:
    ecfg = cfg.get("analysis", {}).get("exposure", {}) or {}
    bcfg = ecfg.get("buildings", {}) or {}
    if not bool(ecfg.get("enabled", False)) or not bool(bcfg.get("enabled", True)):
        return None
    source = str(bcfg.get("source", "overture")).lower()
    out = analysis_exposure_root(cfg) / "overture_buildings.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not force:
        return out
    if source == "file":
        src = Path(str(bcfg["path"])).expanduser().resolve()
        g = gpd.read_file(src)
        g.to_parquet(out)
        return out
    if source != "overture":
        raise RuntimeError(f"Unsupported building source {source!r}")
    exe = shutil.which("overturemaps")
    if not exe:
        if bool(bcfg.get("required", False)):
            raise RuntimeError(
                "Overture Python client is missing. Install on the internet-enabled "
                "login environment: pip install overturemaps"
            )
        log("Overture buildings skipped: `overturemaps` command not installed")
        return None
    bbox = _domain_bbox(cfg)
    bbox_arg = ",".join(f"{v:.8f}" for v in bbox)
    cmd = [
        exe, "download", f"--bbox={bbox_arg}",
        "-f", "geoparquet", "--type=building", "-o", str(out),
    ]
    log("Downloading Overture buildings for model AOI...")
    subprocess.run(cmd, check=True)
    return out


def _prepare_building_grid_indices(
    buildings: gpd.GeoDataFrame,
    x: np.ndarray,
    y: np.ndarray,
    epsg: int,
    shape_hw: Tuple[int, int],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project building representative points once and map them to model cells.

    This replaces the old Python loop over every building for every scenario.
    Geometry work is done once; all eight scenarios then use NumPy indexing.
    """
    t0 = time.time()
    bg = buildings
    if bg.crs is None:
        bg = bg.set_crs(4326)
    target_crs = CRS.from_epsg(int(epsg))
    if CRS.from_user_input(bg.crs) != target_crs:
        bg = bg.to_crs(target_crs)

    pts = bg.geometry.representative_point()
    px = pts.x.to_numpy(dtype=np.float64, copy=False)
    py = pts.y.to_numpy(dtype=np.float64, copy=False)

    transform, _ = _raster_transform(x, y)
    inv = ~transform
    col_f = inv.a * px + inv.b * py + inv.c
    row_f = inv.d * px + inv.e * py + inv.f

    finite = np.isfinite(col_f) & np.isfinite(row_f)
    rows = np.zeros(len(pts), dtype=np.int32)
    cols = np.zeros(len(pts), dtype=np.int32)
    rows[finite] = np.floor(row_f[finite]).astype(np.int32)
    cols[finite] = np.floor(col_f[finite]).astype(np.int32)

    h, w = int(shape_hw[0]), int(shape_hw[1])
    valid = finite & (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
    log(
        f"Prepared vectorized building lookup: total={len(pts)} "
        f"inside_model_grid={int(valid.sum())} elapsed={time.time()-t0:.1f}s"
    )
    return rows, cols, valid


def _sample_buildings_from_indices(
    arr: np.ndarray,
    rows: np.ndarray,
    cols: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    vals = np.full(rows.shape[0], np.nan, dtype=np.float32)
    if np.any(valid):
        vals[valid] = arr[rows[valid], cols[valid]]
    return vals


def _find_worldcover_raster(cfg: Dict[str, Any]) -> Optional[Path]:
    """Find the cached ESA WorldCover raster already prepared by AutoCF."""
    layout = run_layout(cfg)
    run = layout.model
    candidates = [
        layout.data_path('surface', 'esa_worldcover_model_crs_subgrid_10m.tif'),
        layout.data_path('surface', 'esa_worldcover_model_crs.tif'),
        run / 'agent_data' / 'esa_worldcover_model_crs_subgrid_10m.tif',
        run / 'agent_data' / 'esa_worldcover_model_crs.tif',
        run / 'esa_worldcover_model_crs_subgrid_10m.tif',
        run / 'esa_worldcover_model_crs.tif',
    ]
    for c in candidates:
        if c.exists():
            return c
    search_roots = [layout.root / 'data' / 'surface', run]
    for search_root in search_roots:
        hits = sorted(search_root.rglob('*worldcover*.tif')) if search_root.exists() else []
        if hits:
            return hits[0]
    return None


def _worldcover_land_fraction(
    wc_path: Path,
    x: np.ndarray,
    y: np.ndarray,
    epsg: int,
    water_classes: Sequence[int],
) -> np.ndarray:
    """Aggregate high-resolution WorldCover to model-grid land fraction.

    WorldCover class 80 is permanent water. Resampling a binary land indicator
    with AREA/average avoids classifying a 200-m coastal cell from only its
    center point.
    """
    import rasterio
    from rasterio.warp import reproject, Resampling

    transform, _ = _raster_transform(x, y)
    dst = np.full(x.shape, np.nan, dtype=np.float32)
    water = np.asarray([int(v) for v in water_classes], dtype=np.int16)

    with rasterio.open(wc_path) as src:
        a = src.read(1)
        valid = np.isfinite(a)
        if src.nodata is not None:
            valid &= a != src.nodata
        # WorldCover uses 0 as nodata in common distributions.
        valid &= a != 0
        land = np.full(a.shape, np.nan, dtype=np.float32)
        land[valid] = (~np.isin(a[valid].astype(np.int16), water)).astype(np.float32)
        reproject(
            source=land,
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=np.nan,
            dst_transform=transform,
            dst_crs=f'EPSG:{epsg}',
            dst_nodata=np.nan,
            resampling=Resampling.average,
            num_threads=2,
        )
    return dst


def _build_static_land_mask(
    cfg: Dict[str, Any],
    x: np.ndarray,
    y: np.ndarray,
    epsg: int,
    active_mask: np.ndarray,
    results_dir: Path,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    """Create one shared land mask for hazard and exposure calculations."""
    lcfg = cfg.get('analysis', {}).get('land_mask', {}) or {}
    enabled = bool(lcfg.get('enabled', True))
    min_land_fraction = float(lcfg.get('min_land_fraction', 0.50))
    water_classes = [int(v) for v in lcfg.get('worldcover_water_classes', [80])]

    if not enabled:
        frac = np.where(active_mask, 1.0, np.nan).astype(np.float32)
        mask = active_mask.copy()
        source = None
    else:
        wc = _find_worldcover_raster(cfg)
        if wc is None:
            if bool(lcfg.get('required', False)):
                raise RuntimeError(
                    'analysis.land_mask.required=true but no cached ESA WorldCover raster was found.'
                )
            log('WARNING: WorldCover not found; static land mask falls back to active model cells. ' 
                'The NONE/background threshold gate still excludes pre-existing water.')
            frac = np.where(active_mask, 1.0, np.nan).astype(np.float32)
            mask = active_mask.copy()
            source = None
        else:
            log(f'Building static land mask from ESA WorldCover: {wc}')
            frac = _worldcover_land_fraction(wc, x, y, epsg, water_classes)
            mask = active_mask & np.isfinite(frac) & (frac >= min_land_fraction)
            source = str(wc)

    _write_tif(results_dir / 'analysis_land_fraction.tif', frac, x, y, epsg)
    mask_tif = np.where(active_mask, mask.astype(np.float32), np.nan)
    _write_tif(results_dir / 'analysis_land_mask.tif', mask_tif, x, y, epsg)

    meta = {
        'enabled': enabled,
        'source': source,
        'worldcover_water_classes': water_classes,
        'min_land_fraction': min_land_fraction,
        'active_cells': int(active_mask.sum()),
        'land_cells': int(mask.sum()),
    }
    return mask, frac, meta


def _safe_max_single(single_arrays: Sequence[np.ndarray]) -> np.ndarray:
    stack = np.stack(single_arrays, axis=0)
    finite = np.isfinite(stack)
    safe = np.where(finite, stack, -np.inf)
    out = np.max(safe, axis=0).astype(np.float32)
    out[~finite.any(axis=0)] = np.nan
    return out


def _safe_ratio(num: np.ndarray, den: np.ndarray, valid: np.ndarray, eps: float) -> np.ndarray:
    out = np.full(num.shape, np.nan, dtype=np.float32)
    ok = valid & np.isfinite(num) & np.isfinite(den) & (np.abs(den) > eps)
    if np.any(ok):
        out[ok] = (num[ok] / den[ok]).astype(np.float32)
    return out


def _slug_num(v: float) -> str:
    return f'{float(v):g}'.replace('-', 'm').replace('.', 'p')



def _mask_geometry_wgs84(
    mask: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    epsg: int,
    simplify_m: float = 50.0,
):
    import rasterio.features
    transform, _ = _raster_transform(x, y)
    geoms = []
    for geom, value in rasterio.features.shapes(mask.astype(np.uint8), mask=mask, transform=transform):
        if int(value) == 1:
            geoms.append(shape(geom))
    if not geoms:
        return None
    geom = unary_union(geoms)
    if simplify_m > 0:
        geom = geom.simplify(float(simplify_m), preserve_topology=True)
    tr = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    geom = shp_transform(tr.transform, geom)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def _round_geojson_numbers(obj: Any, ndigits: int = 6) -> Any:
    """Compact GeoJSON coordinates without changing topology meaningfully.

    Six decimal places in lon/lat is ~0.1 m, far finer than WorldPop's ~100 m
    population grid, while substantially reducing POST-body size.
    """
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, list):
        return [_round_geojson_numbers(v, ndigits) for v in obj]
    if isinstance(obj, tuple):
        return [_round_geojson_numbers(v, ndigits) for v in obj]
    if isinstance(obj, dict):
        return {k: _round_geojson_numbers(v, ndigits) for k, v in obj.items()}
    return obj


def _polygonal_geometry(geom):
    """Return only valid, positive-area polygon components from a geometry.

    Polygon intersections can contain zero-area lines or points, especially
    where flood and census-tract boundaries merely touch. WorldPop accepts
    Polygon/MultiPolygon input only, so those components must not enter its
    GeoJSON request.
    """
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom if geom.area > 0 else None
    parts = []
    for part in getattr(geom, "geoms", ()):
        polygonal = _polygonal_geometry(part)
        if polygonal is not None and not polygonal.is_empty:
            parts.append(polygonal)
    if not parts:
        return None
    merged = unary_union(parts)
    if not merged.is_valid:
        merged = merged.buffer(0)
    return merged if not merged.is_empty and merged.area > 0 else None


def _worldpop_geojson(geom, ndigits: int = 6) -> Dict[str, Any]:
    rounded = _round_geojson_numbers(mapping(geom), ndigits=ndigits)
    compact = _polygonal_geometry(shape(rounded))
    if compact is None:
        # Preserve extremely small valid polygons at higher precision rather
        # than submitting an empty or non-polygonal geometry.
        compact = _polygonal_geometry(geom)
    if compact is None:
        return {"type": "Polygon", "coordinates": []}
    return _round_geojson_numbers(mapping(compact), ndigits=max(ndigits, 7))


def _worldpop_payload_nbytes(
    geom,
    year: int,
    resolution: str,
    ndigits: int = 6,
) -> int:
    payload = {
        "geojson": _worldpop_geojson(geom, ndigits=ndigits),
        "year": int(year),
        "resolution": str(resolution),
    }
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _worldpop_year_resolution(cfg: Dict[str, Any]) -> Tuple[int, str]:
    """Resolve the population vintage once for every scenario query."""
    pcfg = cfg.get("analysis", {}).get("exposure", {}).get("population", {}) or {}
    year = pcfg.get("year", "auto")
    if str(year).lower() == "auto":
        year = pd.Timestamp(cfg["simulation"]["start"]).year
    year = int(year)
    if not 2015 <= year <= 2030:
        raise RuntimeError(
            f"WorldPop Global2 v2 supports 2015-2030, not {year}."
        )
    resolution = str(pcfg.get("resolution", "100m"))
    if resolution not in {"100m", "1km"}:
        raise RuntimeError(
            f"WorldPop v2 resolution must be 100m or 1km, not {resolution}."
        )
    return year, resolution


def _split_geometry_disjoint(geom) -> Tuple[Any, Any]:
    """Split geometry into two disjoint pieces along its longer bbox axis."""
    minx, miny, maxx, maxy = geom.bounds
    dx = maxx - minx
    dy = maxy - miny
    if dx <= 0 and dy <= 0:
        return geom, None

    # Slightly extend boxes outside the source bounds. The two boxes meet only
    # on a zero-area line, so their polygon intersections are area-disjoint.
    pad = max(dx, dy, 1.0e-9) * 1.0e-9
    if dx >= dy:
        mid = 0.5 * (minx + maxx)
        b1 = box(minx - pad, miny - pad, mid, maxy + pad)
        b2 = box(mid, miny - pad, maxx + pad, maxy + pad)
    else:
        mid = 0.5 * (miny + maxy)
        b1 = box(minx - pad, miny - pad, maxx + pad, mid)
        b2 = box(minx - pad, mid, maxx + pad, maxy + pad)

    g1 = _polygonal_geometry(geom.intersection(b1))
    g2 = _polygonal_geometry(geom.intersection(b2))
    return g1, g2


def _worldpop_request_one_v2(
    geom,
    *,
    year: int,
    resolution: str,
    headers: Dict[str, str],
    cache: Path,
    pcfg: Dict[str, Any],
    coordinate_decimals: int,
) -> float:
    """Submit one WorldPop polygon task and cache the successful result."""
    geojson = _worldpop_geojson(geom, ndigits=coordinate_decimals)
    key_raw = json.dumps(
        {"geojson": geojson, "year": year, "resolution": resolution},
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = hashlib.sha256(key_raw.encode()).hexdigest()[:24]
    cache.mkdir(parents=True, exist_ok=True)
    cp = cache / f"worldpop_{sig}.json"
    if cp.exists():
        js = json.loads(cp.read_text())
        return float(js["total_population"])

    payload = {"geojson": geojson, "year": year, "resolution": resolution}
    max_retries = max(1, int(pcfg.get("max_retries", 5)))
    backoff = max(0.5, float(pcfg.get("retry_backoff_seconds", 2.0)))

    r = None
    for attempt in range(max_retries):
        r = requests.post(
            "https://api.worldpop.org/v2/population",
            headers=headers,
            json=payload,
            timeout=120,
        )

        # 413 is structural, not transient. Caller will split the polygon.
        if r.status_code == 413:
            raise requests.HTTPError(
                "413 Request Entity Too Large",
                response=r,
            )

        # Respect rate limiting and retry transient server failures.
        if r.status_code == 429 or 500 <= r.status_code < 600:
            wait = backoff * (2 ** attempt)
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except Exception:
                    pass
            time.sleep(wait)
            continue

        r.raise_for_status()
        break
    else:
        assert r is not None
        r.raise_for_status()

    task_id = r.json().get("task_id")
    if not task_id:
        raise RuntimeError(f"WorldPop did not return task_id: {r.text[:500]}")

    timeout_s = int(pcfg.get("task_timeout_seconds", 600))
    interval = max(0.5, float(pcfg.get("poll_seconds", 2)))
    t0 = time.time()

    while True:
        rr = requests.get(
            f"https://api.worldpop.org/v2/tasks/{task_id}",
            headers=headers,
            timeout=120,
        )

        if rr.status_code == 429 or 500 <= rr.status_code < 600:
            time.sleep(interval)
            continue

        rr.raise_for_status()
        js = rr.json()
        status = str(js.get("status", "")).lower()

        if status == "success":
            pop = float(js["result"]["total_population"])
            cp.write_text(
                json.dumps(
                    {
                        "total_population": pop,
                        "task": js,
                        "year": year,
                        "resolution": resolution,
                        "geojson_bytes": len(
                            json.dumps(
                                payload,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ),
                    },
                    indent=2,
                    default=str,
                )
            )
            return pop

        if status == "failure":
            raise RuntimeError(f"WorldPop task failed: {js.get('error', js)}")

        if time.time() - t0 > timeout_s:
            raise TimeoutError(
                f"WorldPop task {task_id} exceeded {timeout_s}s"
            )
        time.sleep(interval)


@lru_cache(maxsize=1)
def _worldpop_country_boundaries() -> gpd.GeoDataFrame:
    """Load the packaged global boundaries once per report process."""
    boundary_path = (
        Path(__file__).resolve().parents[1]
        / "data" / "reference" / "ne_50m_admin_0_countries.zip"
    )
    if not boundary_path.is_file():
        raise RuntimeError(f"Bundled country boundaries missing: {boundary_path}")
    countries = gpd.read_file(f"zip://{boundary_path}")
    if countries.crs is None:
        countries = countries.set_crs(4326)
    else:
        countries = countries.to_crs(4326)
    return countries


def _worldpop_country_parts(geom, cfg: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """Intersect an AOI with bundled global boundaries and return ISO3 parts."""
    pcfg = cfg.get("analysis", {}).get("exposure", {}).get("population", {}) or {}
    explicit = pcfg.get("iso3") or pcfg.get("country_iso3")
    if explicit:
        iso3 = str(explicit).strip().upper()
        if len(iso3) != 3 or not iso3.isalpha():
            raise RuntimeError(f"Invalid WorldPop ISO3 country code: {explicit}")
        return [(iso3, geom)]

    countries = _worldpop_country_boundaries()
    parts: List[Tuple[str, Any]] = []
    for _, row in countries[countries.geometry.intersects(geom)].iterrows():
        iso3 = ""
        for field in ("ADM0_A3", "ISO_A3_EH", "ISO_A3", "SOV_A3"):
            value = str(row.get(field, "")).strip().upper()
            if len(value) == 3 and value.isalpha():
                iso3 = value
                break
        if not iso3:
            continue
        intersection = _polygonal_geometry(row.geometry.intersection(geom))
        if intersection is not None and not intersection.is_empty:
            parts.append((iso3, intersection))
    if not parts:
        raise RuntimeError(
            "The model AOI does not intersect a recognized country in the "
            "bundled global boundary layer."
        )
    return parts


@lru_cache(maxsize=64)
def _worldpop_v2_stac_asset(iso3: str, year: int, resolution: str) -> str:
    """Resolve one official Global2 v2 raster asset once per process."""
    token = "_100m_" if resolution == "100m" else "_1km_"
    url = f"https://api.stac.worldpop.org/collections/{iso3}/items"
    response = requests.get(url, params={"limit": 1000}, timeout=120)
    response.raise_for_status()

    candidates = []
    for feature in response.json().get("features", []):
        item_id = str(feature.get("id", ""))
        props = feature.get("properties") or {}
        asset = (feature.get("assets") or {}).get("data") or {}
        if (
            int(props.get("year", -1)) == year
            and str(props.get("project", "")).lower() == "population"
            and "_CN_" in item_id
            and token in item_id
            and asset.get("href")
        ):
            candidates.append(feature)
    if not candidates:
        raise RuntimeError(
            f"Official WorldPop Global2 v2 raster not found for "
            f"{iso3}, {year}, {resolution}."
        )

    # Item identifiers include the release and version; lexical ordering picks
    # the newest official release without embedding a release name in AutoCF.
    item = sorted(candidates, key=lambda f: str(f.get("id", "")))[-1]
    return str(item["assets"]["data"]["href"])


def _worldpop_v2_stac_raster(
    cfg: Dict[str, Any], cache: Path, iso3: str
) -> Path:
    """Locate and cache the official Global2 v2 population raster."""
    year, resolution = _worldpop_year_resolution(cfg)
    asset_url = _worldpop_v2_stac_asset(iso3, year, resolution)
    filename = Path(asset_url.split("?", 1)[0]).name
    raster_dir = cache / "global2_v2_rasters"
    raster_dir.mkdir(parents=True, exist_ok=True)
    target = raster_dir / filename
    if target.is_file() and target.stat().st_size > 0:
        return target

    partial = target.with_suffix(target.suffix + ".part")
    log(
        f"WorldPop v2 API zero rejected; downloading official {iso3} "
        f"{year} {resolution} raster: {filename}"
    )
    with requests.get(asset_url, stream=True, timeout=(30, 300)) as download:
        download.raise_for_status()
        expected = int(download.headers.get("Content-Length", 0) or 0)
        received = 0
        next_progress = 10
        with partial.open("wb") as stream:
            for chunk in download.iter_content(chunk_size=8 * 1024 * 1024):
                if not chunk:
                    continue
                stream.write(chunk)
                received += len(chunk)
                if expected > 0:
                    percent = int(100 * received / expected)
                    if percent >= next_progress:
                        log(f"WorldPop raster download: {min(percent, 100)}%")
                        next_progress += 10
    if expected > 0 and received != expected:
        raise RuntimeError(
            f"Incomplete WorldPop raster download: {received}/{expected} bytes"
        )
    partial.replace(target)
    log(f"WorldPop v2 raster cached: {target}")
    return target


def _worldpop_v2_raster_population(
    geom,
    cfg: Dict[str, Any],
    cache: Path,
) -> float:
    """Sum official Global2 v2 raster cells inside one WGS84 geometry."""
    geom = _polygonal_geometry(geom)
    if geom is None or geom.is_empty:
        return 0.0
    year, resolution = _worldpop_year_resolution(cfg)
    country_parts = _worldpop_country_parts(geom, cfg)
    geojson = _worldpop_geojson(geom)
    key = json.dumps(
        {
            "provider": "global2_v2_stac_raster",
            "countries": sorted({iso3 for iso3, _part in country_parts}),
            "year": year,
            "resolution": resolution,
            "geojson": geojson,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = hashlib.sha256(key.encode()).hexdigest()[:24]
    result_cache = cache / f"worldpop_stac_v2_{sig}.json"
    if result_cache.is_file():
        return float(json.loads(result_cache.read_text(encoding="utf-8"))["total_population"])

    import rasterio
    from rasterio.mask import mask as raster_mask

    total = 0.0
    raster_names = []
    for iso3, country_geom in country_parts:
        raster = _worldpop_v2_stac_raster(cfg, cache, iso3)
        raster_names.append(raster.name)
        try:
            with rasterio.open(raster) as src:
                values, _masked_transform = raster_mask(
                    src,
                    [_worldpop_geojson(country_geom)],
                    indexes=1,
                    crop=True,
                    all_touched=False,
                    filled=False,
                )
        except ValueError:
            continue
        values = np.ma.asarray(values, dtype=np.float64)
        values = np.ma.masked_where(values < 0, values)
        if values.count():
            total += float(values.sum())

    result_cache.write_text(
        json.dumps(
            {
                "total_population": total,
                "provider": "WorldPop Global2 v2 official STAC raster",
                "countries": sorted({iso3 for iso3, _part in country_parts}),
                "year": year,
                "resolution": resolution,
                "rasters": raster_names,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return total


def _worldpop_population(
    geom,
    cfg: Dict[str, Any],
    cache: Path,
    provider: str = "api_v2",
) -> float:
    """Population total with automatic 413-safe polygon decomposition.

    WorldPop's polygon endpoint can reject highly fragmented flood masks with
    HTTP 413. Rather than changing the science or simplifying the mask until it
    loses shape, AutoCF recursively partitions the same polygon into disjoint
    pieces, queries each piece, and sums the cached totals.
    """
    geom = _polygonal_geometry(geom)
    if geom is None or geom.is_empty:
        return 0.0

    pcfg = cfg.get("analysis", {}).get("exposure", {}).get("population", {}) or {}
    year, resolution = _worldpop_year_resolution(cfg)
    provider = str(provider).lower()
    if provider == "stac_v2":
        return _worldpop_v2_raster_population(geom, cfg, cache)
    if provider != "api_v2":
        raise ValueError(f"Unsupported WorldPop provider: {provider}")

    coordinate_decimals = max(
        4, min(8, int(pcfg.get("coordinate_decimals", 6)))
    )
    # Proactive split threshold. The server's actual HTTP-body limit is not
    # part of the public API contract, so 250 kB is a conservative transport
    # threshold; any unexpected 413 triggers additional adaptive splitting.
    max_geojson_bytes = max(
        20_000,
        int(pcfg.get(
            "max_geojson_bytes",
            250_000,
        )),
    )
    max_split_depth = max(
        1, int(pcfg.get("max_split_depth", 12))
    )

    # Backward-compatible cache lookup from V2.3/V2.4. This prevents already
    # successful WorldPop jobs from being submitted again after upgrading the
    # HTTP-413 transport logic.
    if provider == "api_v2" and geom.geom_type == "Polygon":
        legacy_geojson = mapping(geom)
        legacy_key_raw = json.dumps(
            {"geojson": legacy_geojson, "year": year, "resolution": resolution},
            sort_keys=True,
        )
        legacy_sig = hashlib.sha256(legacy_key_raw.encode()).hexdigest()[:24]
        cache.mkdir(parents=True, exist_ok=True)
        legacy_cp = cache / f"worldpop_{legacy_sig}.json"
        if legacy_cp.exists():
            legacy_js = json.loads(legacy_cp.read_text())
            return float(legacy_js["total_population"])

    headers: Dict[str, str] = {}
    env_name = str(pcfg.get("api_key_env", "WORLDPOP_API_KEY"))
    api_key = os.environ.get(env_name)
    if api_key:
        headers["X-API-Key"] = api_key

    # Cache the aggregate query itself, independent of how many transport
    # chunks were needed to obtain it.
    aggregate_geojson = _worldpop_geojson(
        geom, ndigits=coordinate_decimals
    )
    aggregate_key = json.dumps(
        {
            "geojson": aggregate_geojson,
            "year": year,
            "resolution": resolution,
            "method": "api_v2_then_stac_raster_v1",
            "provider": provider,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    aggregate_sig = hashlib.sha256(
        aggregate_key.encode()
    ).hexdigest()[:24]
    cache.mkdir(parents=True, exist_ok=True)
    aggregate_cp = cache / f"worldpop_{provider}_total_{aggregate_sig}.json"
    if aggregate_cp.exists():
        js = json.loads(aggregate_cp.read_text())
        return float(js["total_population"])

    total = 0.0
    n_chunks = 0
    max_observed_bytes = 0

    # LIFO stack avoids recursion depth issues.
    stack: List[Tuple[Any, int]] = [(geom, 0)]

    while stack:
        piece, depth = stack.pop()
        if piece is None or piece.is_empty:
            continue

        nbytes = _worldpop_payload_nbytes(
            piece,
            year=year,
            resolution=resolution,
            ndigits=coordinate_decimals,
        )
        max_observed_bytes = max(max_observed_bytes, nbytes)

        # Proactively split very large request bodies.
        if nbytes > max_geojson_bytes:
            if depth >= max_split_depth:
                raise RuntimeError(
                    "WorldPop geometry remains too large after "
                    f"{max_split_depth} splits "
                    f"({nbytes} bytes > {max_geojson_bytes})."
                )
            p1, p2 = _split_geometry_disjoint(piece)
            children = [p for p in (p1, p2) if p is not None and not p.is_empty]
            if len(children) < 2:
                raise RuntimeError(
                    "WorldPop geometry could not be partitioned further "
                    f"at split depth {depth}."
                )
            stack.extend((p, depth + 1) for p in children)
            continue

        try:
            subtotal = _worldpop_request_one_v2(
                piece,
                year=year,
                resolution=resolution,
                headers=headers,
                cache=cache,
                pcfg=pcfg,
                coordinate_decimals=coordinate_decimals,
            )
            total += float(subtotal)
            n_chunks += 1

        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status != 413:
                raise

            # Server limit can change, so 413 always triggers a further split
            # even when our proactive byte threshold thought the piece was safe.
            if depth >= max_split_depth:
                raise RuntimeError(
                    "WorldPop returned HTTP 413 after maximum adaptive "
                    f"split depth {max_split_depth}; piece size={nbytes} bytes."
                ) from exc

            p1, p2 = _split_geometry_disjoint(piece)
            children = [p for p in (p1, p2) if p is not None and not p.is_empty]
            if len(children) < 2:
                raise RuntimeError(
                    "WorldPop returned HTTP 413 but the geometry could "
                    "not be partitioned further."
                ) from exc

            log(
                f"WorldPop HTTP 413: splitting polygon at depth {depth} "
                f"(payload={nbytes/1024:.1f} KiB)"
            )
            stack.extend((p, depth + 1) for p in children)

    aggregate_cp.write_text(
        json.dumps(
            {
                "total_population": float(total),
                "year": year,
                "resolution": resolution,
                "n_disjoint_chunks": int(n_chunks),
                "coordinate_decimals": int(coordinate_decimals),
                "max_geojson_bytes": int(max_geojson_bytes),
                "largest_encountered_payload_bytes": int(max_observed_bytes),
                "method": "api_v2_then_stac_raster_v1",
                "provider": provider,
            },
            indent=2,
        )
    )

    if n_chunks > 1:
        log(
            f"WorldPop polygon resolved with {n_chunks} disjoint chunks "
            f"-> population={total:.0f}"
        )
    return float(total)


def _validated_worldpop_provider(
    analysis_geom,
    cfg: Dict[str, Any],
    cache: Path,
    *,
    expected_populated_assets: int = 0,
) -> Tuple[str, float]:
    """Validate API v2 and select its official STAC raster when necessary.

    A provider is selected before concurrent scenario requests so a report can
    never mix population sources. Both routes use the same official WorldPop
    Global2 v2 dataset; the raster route bypasses faulty API aggregation only.
    """
    year, resolution = _worldpop_year_resolution(cfg)
    v2_total = float(
        _worldpop_population(
            analysis_geom, cfg, cache, provider="api_v2"
        )
    )
    if np.isfinite(v2_total) and v2_total > 0:
        log(
            f"WorldPop source validated: API v2 {year} {resolution}; "
            f"analysis-land population={v2_total:.0f}"
        )
        return "api_v2", v2_total

    log(
        f"WorldPop API v2 returned an invalid AOI population of {v2_total:g}; "
        "validating against the official Global2 v2 raster."
    )
    raster_total = float(
        _worldpop_population(
            analysis_geom, cfg, cache, provider="stac_v2"
        )
    )
    if np.isfinite(raster_total) and raster_total > 0:
        log(
            f"WorldPop source validated: official Global2 v2 raster "
            f"{year} {resolution}; analysis-land population={raster_total:.0f}"
        )
        return "stac_v2", raster_total

    evidence = (
        f" Overture identified {int(expected_populated_assets)} eligible buildings "
        "inside the analysis land."
        if expected_populated_assets > 0 else ""
    )
    raise RuntimeError(
        "WorldPop Global2 v2 validation failed: both API aggregation and the "
        f"official raster returned zero for {year} at {resolution}.{evidence} "
        "AutoCF refuses to cache/report an all-zero population result."
    )


def _delta_hours(a: Any, b: Any) -> float:
    """Return a positive time increment in hours for decoded xarray times."""
    delta = b - a
    if isinstance(delta, np.timedelta64):
        hours = float(delta / np.timedelta64(1, 'h'))
    elif hasattr(delta, 'total_seconds'):
        hours = float(delta.total_seconds()) / 3600.0
    else:
        # Numeric SFINCS time is conventionally seconds when decode_times could
        # not interpret it. Reject implausible/ambiguous values later.
        hours = float(delta) / 3600.0
    if not np.isfinite(hours) or hours <= 0:
        raise RuntimeError(f'Invalid/non-increasing SFINCS output time step: {hours!r} h')
    return hours


def _linear_exceedance_fraction(
    depth0: np.ndarray, depth1: np.ndarray, threshold: float
) -> np.ndarray:
    """Fraction of an interval above threshold under linear depth change."""
    d0 = np.asarray(depth0, dtype=np.float32)
    d1 = np.asarray(depth1, dtype=np.float32)
    above0 = d0 >= float(threshold)
    above1 = d1 >= float(threshold)
    frac = np.zeros(d0.shape, dtype=np.float32)
    frac[above0 & above1] = 1.0
    crossing = above0 ^ above1
    denom = np.abs(d1 - d0)
    valid = crossing & np.isfinite(denom) & (denom > 0)
    if np.any(valid):
        high = np.maximum(d0[valid], d1[valid])
        frac[valid] = np.clip(
            (high - float(threshold)) / denom[valid], 0.0, 1.0
        ).astype(np.float32)
    return frac


def extract_duration_hours(
    run: Path,
    thresholds: Sequence[float],
) -> Tuple[Dict[float, np.ndarray], np.ndarray, Dict[str, Any]]:
    """Integrate absolute-depth exceedance duration from SFINCS time outputs.

    NaN depth on an active SFINCS cell is treated as dry (0 m), matching the
    maximum-depth analysis. Between saved outputs, depth is assumed linear;
    this estimates the crossing time instead of rounding duration to whole
    output intervals.
    """
    map_path = run / 'sfincs_map.nc'
    ds = xr.open_dataset(map_path)
    try:
        if 'time' not in ds:
            raise RuntimeError(f'No time coordinate for duration analysis: {map_path}')
        if 'h' in ds and 'time' in ds['h'].dims:
            depth_da = ds['h']
            source = 'h(time)'
        elif 'zs' in ds and 'time' in ds['zs'].dims and 'zb' in ds:
            depth_da = ds['zs'] - ds['zb']
            source = 'max(zs(time)-zb,0)'
        else:
            raise RuntimeError(
                f'Duration analysis needs h(time) or zs(time)+zb in {map_path}'
            )

        other_dims = [d for d in depth_da.dims if d != 'time']
        if len(other_dims) != 2:
            raise RuntimeError(
                f'Expected two spatial dimensions in {map_path}; got {depth_da.dims}'
            )
        depth_da = depth_da.transpose('time', *other_dims)
        times = np.asarray(ds['time'].values)
        if times.size < 2:
            raise RuntimeError(f'Duration analysis needs at least two outputs: {map_path}')

        shape2d = tuple(int(v) for v in depth_da.shape[1:])
        if 'msk' in ds:
            valid = np.squeeze(np.asarray(ds['msk'].values)) > 0
            if valid.shape != shape2d and valid.T.shape == shape2d:
                valid = valid.T
        else:
            valid = np.ones(shape2d, dtype=bool)
        if valid.shape != shape2d:
            raise RuntimeError(
                f'Duration mask shape mismatch in {map_path}: {valid.shape} vs {shape2d}'
            )

        def _slice(i: int) -> np.ndarray:
            value = np.squeeze(np.asarray(depth_da.isel(time=i).values, dtype=np.float32))
            if value.shape != shape2d:
                raise RuntimeError(
                    f'Duration depth shape mismatch in {map_path}: {value.shape} vs {shape2d}'
                )
            out = np.zeros(shape2d, dtype=np.float32)
            out[valid] = np.where(
                np.isfinite(value[valid]), np.maximum(value[valid], 0.0), 0.0
            )
            out[~valid] = np.nan
            return out

        durations = {
            float(thr): np.where(valid, 0.0, np.nan).astype(np.float32)
            for thr in thresholds
        }
        prev = _slice(0)
        steps: List[float] = []
        for i in range(1, int(times.size)):
            current = _slice(i)
            dt_h = _delta_hours(times[i - 1], times[i])
            steps.append(dt_h)
            for thr in durations:
                frac = _linear_exceedance_fraction(prev, current, thr)
                durations[thr][valid] += frac[valid] * dt_h
            prev = current

        step_arr = np.asarray(steps, dtype=float)
        time_signature = np.asarray([str(v) for v in times])
        meta = {
            'source_variable': source,
            'n_saved_outputs': int(times.size),
            'simulation_output_span_hours': float(np.sum(step_arr)),
            'median_output_interval_hours': float(np.median(step_arr)),
            'min_output_interval_hours': float(np.min(step_arr)),
            'max_output_interval_hours': float(np.max(step_arr)),
            'integration': 'linear interpolation between saved depth outputs',
        }
        return durations, time_signature, meta
    finally:
        ds.close()


def _download_cdc_svi(
    cfg: Dict[str, Any],
    analysis_geom_wgs84: Any,
    cache_path: Path,
) -> gpd.GeoDataFrame:
    """Download/cache 2022 CDC/ATSDR tract SVI features intersecting the AOI."""
    scfg = cfg.get('analysis', {}).get('social_vulnerability', {}) or {}
    local_path = scfg.get('path')
    if local_path:
        gdf = gpd.read_file(Path(str(local_path)).expanduser())
        return gdf.to_crs(4326) if gdf.crs else gdf.set_crs(4326)
    empty_marker = cache_path.with_suffix('.empty.json')
    if cache_path.exists():
        gdf = gpd.read_file(cache_path)
        return gdf.to_crs(4326) if gdf.crs else gdf.set_crs(4326)
    if empty_marker.exists():
        return gpd.GeoDataFrame(
            columns=['RPL_THEMES', 'geometry'], geometry='geometry', crs=4326
        )

    service = str(scfg.get(
        'feature_service_url',
        'https://services3.arcgis.com/ZvidGQkLaDJxRSJ2/ArcGIS/rest/services/'
        'CDC_ATSDR_Social_Vulnerability_Index_2022_USA/FeatureServer/2',
    )).rstrip('/')
    query_url = service + '/query'
    minx, miny, maxx, maxy = analysis_geom_wgs84.bounds
    fields = [
        'FIPS', 'ST_ABBR', 'COUNTY', 'E_TOTPOP', 'RPL_THEMES',
        'RPL_THEME1', 'RPL_THEME2', 'RPL_THEME3', 'RPL_THEME4',
    ]
    features: List[Dict[str, Any]] = []
    offset = 0
    page_size = max(100, int(scfg.get('page_size', 2000)))
    timeout_s = float(scfg.get('request_timeout_seconds', 120))
    while True:
        params = {
            'where': '1=1',
            'geometry': json.dumps({
                'xmin': minx, 'ymin': miny, 'xmax': maxx, 'ymax': maxy,
                'spatialReference': {'wkid': 4326},
            }),
            'geometryType': 'esriGeometryEnvelope',
            'inSR': 4326,
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': ','.join(fields),
            'returnGeometry': 'true',
            'outSR': 4326,
            'resultOffset': offset,
            'resultRecordCount': page_size,
            'f': 'geojson',
        }
        rr = requests.get(query_url, params=params, timeout=timeout_s)
        rr.raise_for_status()
        payload = rr.json()
        if 'error' in payload:
            raise RuntimeError(f'CDC SVI ArcGIS query failed: {payload["error"]}')
        batch = payload.get('features', []) or []
        features.extend(batch)
        exceeded = bool(
            payload.get('exceededTransferLimit', False)
            or payload.get('properties', {}).get('exceededTransferLimit', False)
        )
        if not exceeded:
            break
        if not batch:
            break
        offset += len(batch)

    if features:
        gdf = gpd.GeoDataFrame.from_features(features, crs='EPSG:4326')
    else:
        gdf = gpd.GeoDataFrame(columns=fields + ['geometry'], geometry='geometry', crs=4326)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if gdf.empty:
        empty_marker.write_text(json.dumps({
            'status': 'no_features',
            'analysis_bounds_wgs84': [minx, miny, maxx, maxy],
            'source': service,
        }, indent=2))
    else:
        gdf.to_file(cache_path, driver='GeoJSON')
    return gdf


def _svi_quartile(value: Any) -> Optional[str]:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(x) or x < 0 or x > 1:
        return None
    if x <= 0.25:
        return 'Q1'
    if x <= 0.50:
        return 'Q2'
    if x <= 0.75:
        return 'Q3'
    return 'Q4'


def _driver_impact_shift_rows(
    scalar_values: Dict[str, Dict[float, Dict[FrozenSet[str], float]]]
) -> List[Dict[str, Any]]:
    """Compute DIS = 100*(impact Shapley share - area Shapley share)."""
    rows: List[Dict[str, Any]] = []
    area_by_thr = scalar_values.get('flooded_area_km2', {})
    for metric in ('buildings_exposed', 'population_exposed'):
        for threshold, impact_values in scalar_values.get(metric, {}).items():
            area_values = area_by_thr.get(threshold)
            if area_values is None:
                continue
            if not all(np.isfinite(v) for v in area_values.values()):
                continue
            if not all(np.isfinite(v) for v in impact_values.values()):
                continue
            area_phi = shapley_scalars(area_values)
            impact_phi = shapley_scalars(impact_values)
            area_delta = float(
                area_values[frozenset(DRIVERS)] - area_values[frozenset()]
            )
            impact_delta = float(
                impact_values[frozenset(DRIVERS)] - impact_values[frozenset()]
            )
            valid = abs(area_delta) > 1e-12 and abs(impact_delta) > 1e-12
            for driver in DRIVERS:
                area_share = area_phi[driver] / area_delta if valid else np.nan
                impact_share = impact_phi[driver] / impact_delta if valid else np.nan
                dis = 100.0 * (impact_share - area_share) if valid else np.nan
                rows.append({
                    'impact_metric': metric,
                    'area_reference_metric': 'flooded_area_km2',
                    'depth_threshold_m': float(threshold),
                    'driver': driver,
                    'reference_area_shapley_value': area_phi[driver],
                    'impact_shapley_value': impact_phi[driver],
                    'reference_area_increment_full_minus_none': area_delta,
                    'impact_increment_full_minus_none': impact_delta,
                    'area_shapley_share': area_share,
                    'impact_shapley_share': impact_share,
                    'dis_percentage_points': dis,
                    'status': 'ok' if valid else 'undefined_zero_increment',
                })
    return rows


def analyze(cfg: Dict[str, Any], skip_population: bool = False) -> Path:
    layout = run_layout(cfg)
    root = analysis_root(cfg)
    manifest_path = root / 'scenario_manifest.json'
    if not manifest_path.exists():
        raise RuntimeError('Run analysis-prepare first.')
    manifest = json.loads(manifest_path.read_text())

    arrays: Dict[FrozenSet[str], np.ndarray] = {}
    ref_x = ref_y = None
    ref_epsg = None
    for active in all_subsets():
        rec = manifest['scenarios'][_scenario_code(active)]
        run = Path(rec['path'])
        if not (run / 'sfincs_map.nc').exists():
            raise RuntimeError(
                f"Scenario output missing: {_scenario_code(active)} -> {run/'sfincs_map.nc'}"
            )
        arr, x, y, epsg = extract_max_depth(run)
        if ref_x is None:
            ref_x, ref_y, ref_epsg = x, y, epsg
        else:
            if arr.shape != arrays[next(iter(arrays))].shape:
                raise RuntimeError('Scenario grids have different shapes')
            if (
                epsg != ref_epsg
                or not np.allclose(x, ref_x, equal_nan=True)
                or not np.allclose(y, ref_y, equal_nan=True)
            ):
                raise RuntimeError(
                    'Scenario grids are not identical; factorial attribution is invalid'
                )
        arrays[active] = arr

    assert ref_x is not None and ref_y is not None and ref_epsg is not None
    results_dir = layout.result_section('attribution')
    results_dir.mkdir(parents=True, exist_ok=True)

    def artifact(name: str) -> Path:
        path = layout.result_path('attribution', name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    acfg = cfg.get('analysis', {}).get('attribution', {}) or {}
    thresholds = [
        float(v) for v in acfg.get(
            'depth_thresholds_m', [0.05, 0.10, 0.25, 0.50, 1.00, 2.00]
        )
    ]
    # Canonical threshold configuration is intentionally non-redundant:
    # define the reporting thresholds once and one cutoff separating primary
    # publication thresholds from thin-water sensitivity thresholds.
    if 'primary_depth_thresholds_m' in acfg:
        # Backward-compatible read of older configs.
        primary_thresholds = [float(v) for v in acfg['primary_depth_thresholds_m']]
        primary_min_depth_m = min(primary_thresholds) if primary_thresholds else 0.10
    else:
        primary_min_depth_m = float(acfg.get('primary_min_depth_m', 0.10))
        primary_thresholds = [v for v in thresholds if v >= primary_min_depth_m]

    sensitivity_thresholds = [
        v for v in thresholds if v not in primary_thresholds
    ]

    raw_full = arrays[frozenset(DRIVERS)]
    raw_none = arrays[frozenset()]

    # SFINCS/HydroMT maximum-depth outputs encode dry cells as NaN. For
    # factorial hazard/exposure analysis, NaN on an otherwise active model cell
    # is therefore a physically meaningful 0 m depth, not missing data.
    #
    # The union of finite FULL/NONE cells is retained as the model-active
    # footprint available from the scenario outputs. Every scenario is then
    # filled with zero depth on those active cells where its own output is NaN.
    active_mask = np.isfinite(raw_full) | np.isfinite(raw_none)

    analysis_arrays: Dict[FrozenSet[str], np.ndarray] = {}
    for active in all_subsets():
        raw = arrays[active]
        filled = np.full(raw.shape, np.nan, dtype=np.float32)
        filled[active_mask] = np.where(
            np.isfinite(raw[active_mask]),
            raw[active_mask],
            0.0,
        ).astype(np.float32)
        analysis_arrays[active] = filled

    full = analysis_arrays[frozenset(DRIVERS)]
    none = analysis_arrays[frozenset()]
    transform, cell_area_m2 = _raster_transform(ref_x, ref_y)

    # ------------------------------------------------------------------
    # Shared normally-dry-land analysis mask.
    # ------------------------------------------------------------------
    static_land, land_fraction, land_meta = _build_static_land_mask(
        cfg, ref_x, ref_y, ref_epsg, active_mask,
        artifact('analysis_land_fraction.tif').parent,
    )
    exp_analysis_cfg = cfg.get('analysis', {}).get('exposure', {}) or {}

    # Canonical event-depth definition.
    #
    # V2.2 attempted to define "normally dry" land with H_NONE < 0.05 m.
    # That is not robust for SFINCS because the NONE scenario can contain
    # background/numerical water depth even over land cells. In the Tampa test
    # this eliminated every land cell.
    #
    # V2.3 therefore keeps ONE static-land universe and removes the NONE
    # background cell-by-cell:
    #
    #   delta_h_S = max(H_S - H_NONE, 0)
    #
    # This is an event-induced excess-depth diagnostic. NONE is exactly zero,
    # the spatial universe never changes with threshold, and exceedance curves
    # are mathematically monotonic.
    depth_definition = str(
        exp_analysis_cfg.get('depth_definition', 'excess_over_none')
    ).strip().lower()
    if depth_definition not in ('excess_over_none', 'absolute_on_land'):
        raise ValueError(
            'analysis.exposure.depth_definition must be '
            "'excess_over_none' or 'absolute_on_land'"
        )

    # Every static-land cell is part of the exposure universe. Dry NONE cells
    # that were NaN in the raw SFINCS depth field have already been converted
    # to 0 m in analysis_arrays.
    exposure_universe = static_land.copy()

    # Record both raw storage semantics and physically filled NONE depths so
    # diagnostics can distinguish "dry NaN" from genuinely wet cells.
    raw_none_land_mask = static_land & np.isfinite(raw_none)
    n_raw_none_finite = int(raw_none_land_mask.sum())
    n_raw_none_dry_nan = int(static_land.sum()) - n_raw_none_finite
    none_land = none[static_land]
    if none_land.size:
        q = np.nanpercentile(
            none_land, [0, 1, 5, 25, 50, 75, 95, 99, 100]
        )
        none_depth_stats = {
            'n_land_cells': int(none_land.size),
            'n_raw_none_finite': n_raw_none_finite,
            'n_raw_none_dry_nan_filled_as_zero': n_raw_none_dry_nan,
            'min_m': float(q[0]),
            'p01_m': float(q[1]),
            'p05_m': float(q[2]),
            'p25_m': float(q[3]),
            'median_m': float(q[4]),
            'p75_m': float(q[5]),
            'p95_m': float(q[6]),
            'p99_m': float(q[7]),
            'max_m': float(q[8]),
            'fraction_zero': float(np.mean(none_land == 0.0)),
            'fraction_lt_0p05': float(np.mean(none_land < 0.05)),
            'fraction_lt_0p10': float(np.mean(none_land < 0.10)),
        }
    else:
        none_depth_stats = {'n_land_cells': 0}

    log(
        f'Exposure universe: all static-land cells={int(exposure_universe.sum())} | '
        f'depth_definition={depth_definition}'
    )
    if int(exposure_universe.sum()) != int(static_land.sum()):
        raise RuntimeError(
            'Exposure universe unexpectedly differs from static-land mask: '
            f'exposure={int(exposure_universe.sum())}, land={int(static_land.sum())}'
        )
    if none_land.size:
        log(
            'NONE-on-land depth diagnostic: '
            f"min={none_depth_stats['min_m']:.4f} m | "
            f"median={none_depth_stats['median_m']:.4f} m | "
            f"p95={none_depth_stats['p95_m']:.4f} m | "
            f"fraction_zero={none_depth_stats['fraction_zero']:.3%} | "
            f"raw_dry_NaN_filled={none_depth_stats['n_raw_none_dry_nan_filled_as_zero']}"
        )

    # Preserve the legacy event-induced excess-depth surface as a separately
    # labelled diagnostic. Headline impact functionals below always use each
    # scenario's absolute depth, so Shapley explains f(FULL)-f(NONE).
    excess_depth_arrays: Dict[FrozenSet[str], np.ndarray] = {}
    exposure_arrays: Dict[FrozenSet[str], np.ndarray] = {}
    for active in all_subsets():
        arr = analysis_arrays[active]
        valid = exposure_universe
        excess = np.full(arr.shape, np.nan, dtype=np.float32)
        excess[valid] = np.maximum(
            arr[valid] - none[valid], 0.0
        ).astype(np.float32)
        excess_depth_arrays[active] = excess
        ed = excess if depth_definition == 'excess_over_none' else np.where(
            valid, np.maximum(arr, 0.0), np.nan
        ).astype(np.float32)

        exposure_arrays[active] = ed
        _write_tif(
            artifact(f'event_depth_{_scenario_code(active)}.tif'),
            ed, ref_x, ref_y, ref_epsg,
        )
        _write_tif(
            artifact(f'event_excess_depth_{_scenario_code(active)}.tif'),
            excess, ref_x, ref_y, ref_epsg,
        )

    if depth_definition == 'excess_over_none':
        none_excess = excess_depth_arrays[frozenset()]
        finite_none_excess = none_excess[np.isfinite(none_excess)]
        if finite_none_excess.size and float(np.nanmax(np.abs(finite_none_excess))) > 1e-7:
            raise RuntimeError(
                'NONE event-induced excess depth is not zero; implementation error.'
            )

    full_event_depth = excess_depth_arrays[frozenset(DRIVERS)]
    positive_full = (
        exposure_universe
        & np.isfinite(full_event_depth)
        & (full_event_depth > 0)
    )
    if not np.any(positive_full):
        raise RuntimeError(
            'Full-event exposure depth is zero everywhere on static land. '
            'This indicates an invalid depth extraction or scenario set; '
            'report generation stopped instead of writing empty figures.'
        )

    # ------------------------------------------------------------------
    # Spatial Shapley + exact factorial interaction decomposition.
    # ------------------------------------------------------------------
    phi = shapley_arrays(analysis_arrays)
    for d, arr in phi.items():
        _write_tif(artifact(f'shapley_max_depth_{d}.tif'), arr, ref_x, ref_y, ref_epsg)
        land_arr = np.where(static_land, arr, np.nan).astype(np.float32)
        _write_tif(
            artifact(f'shapley_max_depth_{d}_land.tif'),
            land_arr, ref_x, ref_y, ref_epsg,
        )

    factorial_terms = factorial_terms_arrays(analysis_arrays)
    for name, arr in factorial_terms.items():
        if name.startswith('interaction_'):
            _write_tif(
                artifact(f'factorial_{name}_max_depth.tif'),
                arr, ref_x, ref_y, ref_epsg,
            )
            _write_tif(
                artifact(f'factorial_{name}_max_depth_land.tif'),
                np.where(static_land, arr, np.nan).astype(np.float32),
                ref_x, ref_y, ref_epsg,
            )

    # ------------------------------------------------------------------
    # Compound amplification, restricted to land for publication metrics.
    # ------------------------------------------------------------------
    singles = [analysis_arrays[frozenset([d])] for d in DRIVERS]
    max_single = _safe_max_single(singles)
    amp_full_domain = (full - max_single).astype(np.float32)
    _write_tif(
        artifact('compound_amplification_max_depth_full_domain.tif'),
        amp_full_domain, ref_x, ref_y, ref_epsg,
    )
    amp = np.where(static_land, amp_full_domain, np.nan).astype(np.float32)
    _write_tif(
        artifact('compound_amplification_max_depth.tif'),
        amp, ref_x, ref_y, ref_epsg,
    )

    amp_cfg = acfg.get('compound_amplification', {}) or {}
    rel_eps = float(amp_cfg.get('relative_epsilon_m', 1e-6))
    amp_rel = _safe_ratio(
        amp, full, static_land & np.isfinite(full), rel_eps
    )
    _write_tif(
        artifact('compound_amplification_relative.tif'),
        amp_rel, ref_x, ref_y, ref_epsg,
    )

    abs_amp_thresholds = [
        float(v) for v in amp_cfg.get(
            'absolute_thresholds_m', [0.01, 0.05, 0.10, 0.25]
        )
    ]
    rel_amp_thresholds = [
        float(v) for v in amp_cfg.get('relative_thresholds', [0.05, 0.10, 0.25])
    ]
    rel_min_full_depth = float(amp_cfg.get('relative_min_full_depth_m', 0.10))
    amp_spatial_rows: List[Dict[str, Any]] = []

    for thr in abs_amp_thresholds:
        m = static_land & np.isfinite(amp) & (amp >= thr)
        mask_arr = np.where(static_land, m.astype(np.float32), np.nan)
        _write_tif(
            artifact(f'compound_amplification_ge_{_slug_num(thr)}m.tif'),
            mask_arr, ref_x, ref_y, ref_epsg,
        )
        amp_spatial_rows.append({
            'criterion': 'absolute',
            'threshold': thr,
            'threshold_units': 'm',
            'minimum_full_depth_m': None,
            'area_km2': float(m.sum() * cell_area_m2 / 1e6),
            'mean_amplification_m': float(np.nanmean(amp[m])) if m.any() else 0.0,
            'max_amplification_m': float(np.nanmax(amp[m])) if m.any() else 0.0,
            'mean_relative_amplification': float(np.nanmean(amp_rel[m])) if m.any() else 0.0,
        })

    for thr in rel_amp_thresholds:
        m = (
            static_land
            & np.isfinite(amp_rel)
            & np.isfinite(full)
            & (full >= rel_min_full_depth)
            & (amp > 0)
            & (amp_rel >= thr)
        )
        mask_arr = np.where(static_land, m.astype(np.float32), np.nan)
        _write_tif(
            artifact(f'compound_amplification_relative_ge_{_slug_num(thr)}.tif'),
            mask_arr, ref_x, ref_y, ref_epsg,
        )
        amp_spatial_rows.append({
            'criterion': 'relative',
            'threshold': thr,
            'threshold_units': 'fraction',
            'minimum_full_depth_m': rel_min_full_depth,
            'area_km2': float(m.sum() * cell_area_m2 / 1e6),
            'mean_amplification_m': float(np.nanmean(amp[m])) if m.any() else 0.0,
            'max_amplification_m': float(np.nanmax(amp[m])) if m.any() else 0.0,
            'mean_relative_amplification': float(np.nanmean(amp_rel[m])) if m.any() else 0.0,
        })

    amp_spatial_df = pd.DataFrame(amp_spatial_rows)
    amp_spatial_df.to_csv(
        artifact('compound_amplification_spatial_thresholds.csv'), index=False
    )

    # ------------------------------------------------------------------
    # Mathematical closure audits.
    # ------------------------------------------------------------------
    closure = phi['coastal'] + phi['precipitation'] + phi['river']
    target = full - none

    frac_eps = float(acfg.get('shapley_fraction_epsilon_m', 1e-6))
    shapley_frac: Dict[str, np.ndarray] = {}
    frac_valid = np.isfinite(target) & (np.abs(target) > frac_eps)
    for d in DRIVERS:
        frac = np.full(target.shape, np.nan, dtype=np.float32)
        ok = frac_valid & np.isfinite(phi[d])
        if np.any(ok):
            frac[ok] = (phi[d][ok] / target[ok]).astype(np.float32)
        shapley_frac[d] = frac
        _write_tif(
            artifact(f'shapley_fraction_max_depth_{d}.tif'),
            frac, ref_x, ref_y, ref_epsg,
        )

    frac_stack = np.stack([shapley_frac[d] for d in DRIVERS], axis=0)
    finite_any = np.isfinite(frac_stack).any(axis=0)
    safe_stack = np.where(np.isfinite(frac_stack), frac_stack, -np.inf)
    dom_idx = np.argmax(safe_stack, axis=0)
    dom_val = np.max(safe_stack, axis=0)
    dominant = np.where(finite_any & np.isfinite(dom_val), dom_idx + 1, 0).astype(np.float32)
    dominant_fraction = np.where(
        finite_any & np.isfinite(dom_val), dom_val, np.nan
    ).astype(np.float32)
    _write_tif(
        artifact('dominant_shapley_driver.tif'),
        dominant, ref_x, ref_y, ref_epsg,
    )
    _write_tif(
        artifact('dominant_shapley_fraction.tif'),
        dominant_fraction, ref_x, ref_y, ref_epsg,
    )
    _write_tif(
        artifact('dominant_shapley_driver_land.tif'),
        np.where(static_land, dominant, np.nan).astype(np.float32),
        ref_x, ref_y, ref_epsg,
    )

    valid = np.isfinite(closure) & np.isfinite(target)
    closure_rmse = (
        float(np.sqrt(np.mean((closure[valid] - target[valid]) ** 2)))
        if valid.any() else np.nan
    )
    closure_max = (
        float(np.max(np.abs(closure[valid] - target[valid])))
        if valid.any() else np.nan
    )

    factorial_closure = np.zeros_like(target, dtype=np.float32)
    for term in factorial_terms.values():
        factorial_closure = factorial_closure + term
    fvalid = np.isfinite(factorial_closure) & np.isfinite(target)
    factorial_closure_rmse = (
        float(np.sqrt(np.mean((factorial_closure[fvalid] - target[fvalid]) ** 2)))
        if fvalid.any() else np.nan
    )
    factorial_closure_max = (
        float(np.max(np.abs(factorial_closure[fvalid] - target[fvalid])))
        if fvalid.any() else np.nan
    )

    # ------------------------------------------------------------------
    # Overture buildings: project ONCE and reuse model-grid indices.
    # ------------------------------------------------------------------
    expcfg = cfg.get('analysis', {}).get('exposure', {}) or {}
    buildings_path = analysis_exposure_root(cfg) / 'overture_buildings.parquet'
    buildings = None
    if (
        bool(expcfg.get('enabled', False))
        and bool((expcfg.get('buildings', {}) or {}).get('enabled', True))
        and buildings_path.exists()
    ):
        buildings = gpd.read_parquet(buildings_path)
        log(f'Loaded Overture buildings: {len(buildings)}')

    building_depth: Dict[FrozenSet[str], np.ndarray] = {}
    building_rows = building_cols = building_grid_valid = None
    building_land = None
    if buildings is not None:
        building_rows, building_cols, building_grid_valid = _prepare_building_grid_indices(
            buildings, ref_x, ref_y, ref_epsg, full.shape
        )
        building_land = np.zeros(len(building_rows), dtype=bool)
        if np.any(building_grid_valid):
            building_land[building_grid_valid] = static_land[
                building_rows[building_grid_valid], building_cols[building_grid_valid]
            ]
        for active in all_subsets():
            building_depth[active] = _sample_buildings_from_indices(
                analysis_arrays[active],
                building_rows,
                building_cols,
                building_grid_valid,
            )
        log(
            f'Buildings retained on analysis land mask: '
            f'{int(building_land.sum())}/{len(building_land)}'
        )

    # ------------------------------------------------------------------
    # Absolute scenario impacts on ONE static-land universe.
    #
    #   f_d(S) = impact computed where H_S(x) >= d
    #
    # The headline values are f_d(FULL), and Shapley allocates the incremental
    # consequence f_d(FULL)-f_d(NONE). This matters for nonlinear exposure
    # functionals: V(H_S-H_NONE) is generally not V(H_S)-V(H_NONE).
    # ------------------------------------------------------------------
    pcfg = expcfg.get('population', {}) or {}
    use_population = (
        bool(expcfg.get('enabled', False))
        and bool(pcfg.get('enabled', True))
        and str(pcfg.get('source', 'worldpop')).lower() == 'worldpop'
        and not skip_population
    )

    rows: List[Dict[str, Any]] = []
    row_lookup: Dict[Tuple[float, FrozenSet[str]], int] = {}
    population_jobs: List[Tuple[float, FrozenSet[str], Any]] = []
    pop_cache = (
        Path(str(cfg.get('project', {}).get('cache_dir'))).expanduser().resolve() / 'worldpop'
        if layout.organized else root / 'exposure_data' / 'worldpop_cache'
    )
    population_provider = 'api_v2'
    total_analysis_population = np.nan
    analysis_land_geom_wgs84 = None
    if use_population:
        analysis_land_geom_wgs84 = _mask_geometry_wgs84(
            static_land, ref_x, ref_y, ref_epsg,
            simplify_m=float(pcfg.get('polygon_simplify_m', 50.0)),
        )
    scalar_values: Dict[str, Dict[float, Dict[FrozenSet[str], float]]] = {
        'flooded_area_km2': {},
        'buildings_exposed': {},
        'population_exposed': {},
    }

    none_building = building_depth.get(frozenset())

    # Building footprints themselves are exposure assets. Retain buildings on
    # the shared land/grid universe. Absolute depths are canonical; a legacy
    # excess-over-NONE table is written separately below.
    building_eligible = None
    building_event_depth: Dict[FrozenSet[str], np.ndarray] = {}
    if building_land is not None:
        building_eligible = building_land.copy()

        for active in all_subsets():
            bd = building_depth.get(active)
            if bd is None:
                continue
            out_bd = np.full(bd.shape, np.nan, dtype=np.float32)

            if none_building is not None:
                ok = building_eligible & np.isfinite(bd) & np.isfinite(none_building)
                out_bd[ok] = np.maximum(
                    bd[ok] - none_building[ok], 0.0
                ).astype(np.float32)
            else:
                ok = building_eligible & np.isfinite(bd)
                out_bd[ok] = np.maximum(bd[ok], 0.0).astype(np.float32)

            building_event_depth[active] = out_bd

        log(
            f'Building exposure universe: '
            f'eligible={int(np.sum(building_eligible))}'
        )

    # Validate one AOI-wide denominator before dispatching scenario queries.
    # A false API zero switches the complete report to the matching official
    # Global2 v2 raster; population datasets are never mixed within a report.
    if use_population:
        if analysis_land_geom_wgs84 is None or analysis_land_geom_wgs84.is_empty:
            raise RuntimeError(
                'WorldPop population exposure requested but the analysis-land '
                'geometry is empty.'
            )
        eligible_buildings = (
            int(np.sum(building_eligible))
            if building_eligible is not None else 0
        )
        population_provider, total_analysis_population = (
            _validated_worldpop_provider(
                analysis_land_geom_wgs84,
                cfg,
                pop_cache,
                expected_populated_assets=eligible_buildings,
            )
        )

    # Low-cost legacy diagnostic: area/buildings exposed to the event-induced
    # excess depth max(H_S-H_NONE,0). No extra WorldPop calls are made here.
    excess_rows: List[Dict[str, Any]] = []
    for threshold in thresholds:
        for active in all_subsets():
            arr = excess_depth_arrays[active]
            wet = exposure_universe & np.isfinite(arr) & (arr >= threshold)
            wet_land_fraction = np.where(
                wet & np.isfinite(land_fraction),
                np.clip(land_fraction, 0.0, 1.0),
                0.0,
            )
            excess_area = float(
                np.sum(wet_land_fraction, dtype=np.float64)
                * cell_area_m2 / 1e6
            )
            excess_buildings = np.nan
            if active in building_event_depth and building_eligible is not None:
                bd = building_event_depth[active]
                excess_buildings = float(np.sum(
                    building_eligible & np.isfinite(bd) & (bd >= threshold)
                ))
            excess_rows.append({
                'scenario': _scenario_code(active),
                'active_drivers': '+'.join(sorted(active)) or 'none',
                'depth_threshold_m': threshold,
                'metric_definition': 'max(H_scenario-H_NONE,0) on static land',
                'flooded_area_km2': excess_area,
                'buildings_exposed': excess_buildings,
            })
    pd.DataFrame(excess_rows).to_csv(
        artifact('scenario_event_induced_excess_depth.csv'), index=False
    )

    for threshold in thresholds:
        scalar_values['flooded_area_km2'][threshold] = {}
        scalar_values['buildings_exposed'][threshold] = {}
        scalar_values['population_exposed'][threshold] = {}

        for active in all_subsets():
            arr = analysis_arrays[active]
            wet = exposure_universe & np.isfinite(arr) & (arr >= threshold)

            # Land-fraction-weighted area: a mixed 200-m cell contributes only
            # its estimated land portion instead of a full computational-cell area.
            wet_land_fraction = np.where(
                wet & np.isfinite(land_fraction),
                np.clip(land_fraction, 0.0, 1.0),
                0.0,
            )
            area = float(
                np.sum(wet_land_fraction, dtype=np.float64)
                * cell_area_m2 / 1e6
            )

            bcount = np.nan
            if active in building_depth and building_eligible is not None:
                bd = building_depth[active]
                bcount = float(
                    np.sum(
                        building_eligible
                        & np.isfinite(bd)
                        & (bd >= threshold)
                    )
                )

            pop = np.nan
            if use_population:
                geom = _mask_geometry_wgs84(
                    wet, ref_x, ref_y, ref_epsg,
                    simplify_m=float(pcfg.get('polygon_simplify_m', 50.0)),
                )
                if geom is None or geom.is_empty:
                    pop = 0.0
                else:
                    population_jobs.append((threshold, active, geom))

            scalar_values['flooded_area_km2'][threshold][active] = area
            scalar_values['buildings_exposed'][threshold][active] = bcount
            scalar_values['population_exposed'][threshold][active] = pop
            row_lookup[(threshold, active)] = len(rows)
            rows.append({
                'scenario': _scenario_code(active),
                'active_drivers': '+'.join(sorted(active)) or 'none',
                'depth_threshold_m': threshold,
                'threshold_role': (
                    'primary' if threshold in primary_thresholds else 'sensitivity'
                ),
                'metric_definition': 'absolute maximum depth on static land',
                'flooded_area_km2': area,
                'buildings_exposed': bcount,
                'population_exposed': pop,
            })
        log(f'Local land/exposure metrics complete for {threshold:g} m')

    # WorldPop requests are independent. Use bounded concurrency and preserve
    # the existing per-polygon disk cache.
    if use_population and population_jobs:
        provider_worker_cap = 2 if population_provider == 'stac_v2' else 8
        max_workers = max(
            1,
            min(provider_worker_cap, int(pcfg.get('max_parallel_tasks', 4))),
        )
        log(
            f'WorldPop exposure: {len(population_jobs)} jobs, '
            f'provider={population_provider}, max_parallel_tasks={max_workers}; '
            f'cached jobs return immediately'
        )

        def _pop_worker(job):
            threshold, active, geom = job
            return threshold, active, float(
                _worldpop_population(
                    geom, cfg, pop_cache, provider=population_provider
                )
            )

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_pop_worker, job) for job in population_jobs]
            for fut in as_completed(futures):
                threshold, active, pop = fut.result()
                scalar_values['population_exposed'][threshold][active] = pop
                rows[row_lookup[(threshold, active)]]['population_exposed'] = pop
                completed += 1
                log(
                    f'WorldPop {completed}/{len(population_jobs)}: '
                    f'{_scenario_code(active)} @ {threshold:g} m -> {pop:.0f}'
                )

    if use_population:
        population_values = np.asarray(
            [float(row['population_exposed']) for row in rows],
            dtype=float,
        )
        building_values = np.asarray(
            [float(row['buildings_exposed']) for row in rows],
            dtype=float,
        )
        if (
            np.isfinite(building_values).any()
            and np.nanmax(building_values) > 0
            and not np.any(np.isfinite(population_values) & (population_values > 0))
        ):
            raise RuntimeError(
                'WorldPop scenario validation failed: exposed buildings are '
                'nonzero but every population-exposure result is zero. AutoCF '
                'refuses to generate empty population/SVI figures.'
            )
    for row in rows:
        pop = float(row['population_exposed'])
        row['analysis_land_population'] = total_analysis_population
        row['population_affected_pct'] = (
            100.0 * pop / total_analysis_population
            if np.isfinite(pop)
            and np.isfinite(total_analysis_population)
            and total_analysis_population > 0
            else np.nan
        )

    scenario_df = pd.DataFrame(rows)

    # A compact headline table keeps absolute FULL totals, the NONE baseline,
    # and their incremental difference side by side.
    absolute_rows: List[Dict[str, Any]] = []
    full_active = frozenset(DRIVERS)
    none_active = frozenset()
    for threshold in thresholds:
        rec: Dict[str, Any] = {
            'depth_threshold_m': threshold,
            'threshold_role': (
                'primary' if threshold in primary_thresholds else 'sensitivity'
            ),
            'impact_basis': 'absolute maximum depth on static land',
            'analysis_land_population': total_analysis_population,
        }
        for metric in ('flooded_area_km2', 'buildings_exposed', 'population_exposed'):
            full_value = float(scalar_values[metric][threshold][full_active])
            none_value = float(scalar_values[metric][threshold][none_active])
            rec[f'full_{metric}'] = full_value
            rec[f'none_{metric}'] = none_value
            rec[f'increment_{metric}'] = full_value - none_value
        rec['full_population_affected_pct'] = (
            100.0 * rec['full_population_exposed'] / total_analysis_population
            if np.isfinite(rec['full_population_exposed'])
            and np.isfinite(total_analysis_population)
            and total_analysis_population > 0
            else np.nan
        )
        rec['none_population_affected_pct'] = (
            100.0 * rec['none_population_exposed'] / total_analysis_population
            if np.isfinite(rec['none_population_exposed'])
            and np.isfinite(total_analysis_population)
            and total_analysis_population > 0
            else np.nan
        )
        absolute_rows.append(rec)
    absolute_df = pd.DataFrame(absolute_rows)
    absolute_df.to_csv(artifact('absolute_impact_summary.csv'), index=False)
    artifact('absolute_impact_report.json').write_text(json.dumps({
        'headline_scenario': '111_cpr',
        'baseline_scenario': '000_none',
        'impact_basis': 'absolute maximum depth on the fixed static-land universe',
        'attribution_target': 'f(FULL)-f(NONE)',
        'population_denominator': 'WorldPop population within analysis_land_mask',
        'population_provider': (
            population_provider if use_population else None
        ),
        'analysis_land_population': (
            float(total_analysis_population)
            if np.isfinite(total_analysis_population) else None
        ),
        'rows': absolute_rows,
    }, indent=2, default=str))

    # ------------------------------------------------------------------
    # CDC/ATSDR SVI burden. This is a post-processing overlay on the FULL
    # absolute flood footprint and therefore requires no new SFINCS runs.
    # ------------------------------------------------------------------
    svi_cfg = cfg.get('analysis', {}).get('social_vulnerability', {}) or {}
    svi_enabled = bool(svi_cfg.get('enabled', True))
    svi_thresholds = sorted(set(
        float(v) for v in svi_cfg.get('depth_thresholds_m', primary_thresholds)
    ))
    svi_summary: Dict[str, Any] = {
        'enabled': svi_enabled,
        'dataset': 'CDC/ATSDR Social Vulnerability Index 2022, census tracts',
        'svi_measure': 'RPL_THEMES national percentile ranking',
        'quartiles': {
            'Q1': '0.00-0.25 (lower vulnerability)',
            'Q2': '>0.25-0.50',
            'Q3': '>0.50-0.75',
            'Q4': '>0.75-1.00 (higher vulnerability)',
        },
        'n_new_sfincs_runs': 0,
    }
    svi_rows: List[Dict[str, Any]] = []
    if svi_enabled and not use_population:
        svi_summary.update({
            'status': 'skipped_population_disabled',
            'message': 'SVI population burden needs WorldPop exposure enabled.',
        })
    elif svi_enabled and analysis_land_geom_wgs84 is not None:
        try:
            svi_cache = analysis_exposure_root(cfg) / 'cdc_svi_2022.geojson'
            svi = _download_cdc_svi(
                cfg, analysis_land_geom_wgs84, svi_cache
            )
            svi.columns = [str(c).upper() if c != 'geometry' else c for c in svi.columns]
            if 'RPL_THEMES' not in svi.columns:
                raise RuntimeError('CDC SVI data does not contain RPL_THEMES')
            svi['RPL_THEMES'] = pd.to_numeric(svi['RPL_THEMES'], errors='coerce')
            svi['SVI_QUARTILE'] = svi['RPL_THEMES'].map(_svi_quartile)
            svi = svi[
                svi['SVI_QUARTILE'].notna() & svi.geometry.notna()
            ].copy()
            if svi.empty:
                svi_summary.update({
                    'status': 'not_applicable_or_no_features',
                    'message': (
                        'No valid 2022 CDC SVI census tracts intersect the analysis area; '
                        'the model may be outside the United States.'
                    ),
                })
            else:
                svi['geometry'] = svi.geometry.intersection(analysis_land_geom_wgs84)
                svi = svi[~svi.geometry.is_empty].copy()
                tract_out = artifact('cdc_svi_tracts_in_analysis_area.geojson')
                svi.to_file(tract_out, driver='GeoJSON')

                quartile_geoms: Dict[str, Any] = {}
                for quartile in ('Q1', 'Q2', 'Q3', 'Q4'):
                    pieces = [
                        geom for geom in svi.loc[
                            svi['SVI_QUARTILE'] == quartile, 'geometry'
                        ].tolist() if geom is not None and not geom.is_empty
                    ]
                    quartile_geoms[quartile] = unary_union(pieces) if pieces else None

                svi_jobs: List[Tuple[str, Optional[float], str, Any]] = []
                svi_values: Dict[Tuple[str, Optional[float], str], float] = {}
                for quartile, geom in quartile_geoms.items():
                    key = ('baseline', None, quartile)
                    if geom is None or geom.is_empty:
                        svi_values[key] = 0.0
                    else:
                        svi_jobs.append((*key, geom))
                for threshold in svi_thresholds:
                    full_wet = (
                        static_land & np.isfinite(full) & (full >= threshold)
                    )
                    flood_geom = _mask_geometry_wgs84(
                        full_wet, ref_x, ref_y, ref_epsg,
                        simplify_m=float(pcfg.get('polygon_simplify_m', 50.0)),
                    )
                    for quartile, quartile_geom in quartile_geoms.items():
                        key = ('affected', threshold, quartile)
                        if (
                            flood_geom is None or flood_geom.is_empty
                            or quartile_geom is None or quartile_geom.is_empty
                        ):
                            svi_values[key] = 0.0
                        else:
                            geom = flood_geom.intersection(quartile_geom)
                            if geom.is_empty:
                                svi_values[key] = 0.0
                            else:
                                svi_jobs.append((*key, geom))

                max_workers = max(
                    1,
                    min(
                        2 if population_provider == 'stac_v2' else 8,
                        int(pcfg.get('max_parallel_tasks', 4)),
                    ),
                )
                if svi_jobs:
                    log(
                        f'CDC SVI burden: {len(svi_jobs)} cached/parallel '
                        f'WorldPop polygon jobs'
                    )

                    def _svi_pop_worker(job):
                        kind, threshold, quartile, geom = job
                        value = float(_worldpop_population(
                            geom,
                            cfg,
                            pop_cache,
                            provider=population_provider,
                        ))
                        return kind, threshold, quartile, value

                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        futures = [
                            pool.submit(_svi_pop_worker, job) for job in svi_jobs
                        ]
                        for fut in as_completed(futures):
                            kind, threshold, quartile, value = fut.result()
                            svi_values[(kind, threshold, quartile)] = value

                total_svi_baseline = sum(
                    svi_values.get(('baseline', None, q), 0.0)
                    for q in ('Q1', 'Q2', 'Q3', 'Q4')
                )
                for threshold in svi_thresholds:
                    total_svi_affected = sum(
                        svi_values.get(('affected', threshold, q), 0.0)
                        for q in ('Q1', 'Q2', 'Q3', 'Q4')
                    )
                    full_pop = float(
                        scalar_values['population_exposed'][threshold][full_active]
                    ) if threshold in scalar_values['population_exposed'] else np.nan
                    for quartile in ('Q1', 'Q2', 'Q3', 'Q4'):
                        baseline_pop = svi_values.get(
                            ('baseline', None, quartile), 0.0
                        )
                        affected_pop = svi_values.get(
                            ('affected', threshold, quartile), 0.0
                        )
                        affected_share = (
                            affected_pop / total_svi_affected
                            if total_svi_affected > 0 else np.nan
                        )
                        baseline_share = (
                            baseline_pop / total_svi_baseline
                            if total_svi_baseline > 0 else np.nan
                        )
                        svi_rows.append({
                            'depth_threshold_m': threshold,
                            'svi_quartile': quartile,
                            'baseline_population_in_analysis_land': baseline_pop,
                            'affected_population': affected_pop,
                            'quartile_population_affected_pct': (
                                100.0 * affected_pop / baseline_pop
                                if baseline_pop > 0 else np.nan
                            ),
                            'share_of_svi_covered_affected_population_pct': (
                                100.0 * affected_share
                                if np.isfinite(affected_share) else np.nan
                            ),
                            'baseline_population_share_pct': (
                                100.0 * baseline_share
                                if np.isfinite(baseline_share) else np.nan
                            ),
                            'burden_concentration_ratio': (
                                affected_share / baseline_share
                                if np.isfinite(affected_share)
                                and np.isfinite(baseline_share)
                                and baseline_share > 0 else np.nan
                            ),
                            'full_affected_population_all_svi_coverage': full_pop,
                            'svi_population_coverage_pct': (
                                100.0 * total_svi_affected / full_pop
                                if np.isfinite(full_pop) and full_pop > 0 else np.nan
                            ),
                        })
                svi_summary.update({
                    'status': 'complete',
                    'tracts_intersecting_analysis_land': int(len(svi)),
                    'thresholds_m': svi_thresholds,
                    'worldpop_queries': (
                        'Four baseline quartiles plus four affected quartiles per threshold; '
                        'all queries are disk-cached.'
                    ),
                    'burden_concentration_ratio_definition': (
                        'affected-population share in a quartile divided by that quartile\'s '
                        'baseline population share; >1 indicates disproportionate burden'
                    ),
                })
        except Exception as exc:
            svi_summary.update({
                'status': 'unavailable',
                'message': str(exc),
            })
            log(f'CDC SVI burden unavailable; core analysis continues: {exc}')
    elif not svi_enabled:
        svi_summary['status'] = 'disabled'
    else:
        svi_summary['status'] = 'unavailable_no_analysis_geometry'

    svi_columns = [
        'depth_threshold_m', 'svi_quartile',
        'baseline_population_in_analysis_land', 'affected_population',
        'quartile_population_affected_pct',
        'share_of_svi_covered_affected_population_pct',
        'baseline_population_share_pct', 'burden_concentration_ratio',
        'full_affected_population_all_svi_coverage',
        'svi_population_coverage_pct',
    ]
    pd.DataFrame(svi_rows, columns=svi_columns).to_csv(
        artifact('cdc_svi_burden_summary.csv'), index=False
    )
    artifact('cdc_svi_burden_summary.json').write_text(
        json.dumps({**svi_summary, 'rows': svi_rows}, indent=2, default=str)
    )

    # ------------------------------------------------------------------
    # Exposure-depth monotonicity QC.
    # A valid exceedance curve over a fixed mask cannot increase as the
    # threshold increases. Area/building failures are hard errors because they
    # indicate an implementation/mask bug. Population is reported with a small
    # tolerance because it is returned by an external polygon-statistics API.
    # ------------------------------------------------------------------
    monotonicity_qc: Dict[str, Any] = {
        'fixed_static_land_universe_expected': True,
        'depth_definition': 'absolute_maximum_depth_on_static_land',
        'metrics': {},
        'passes_local_metrics': True,
    }

    ordered_thresholds = sorted(thresholds)

    def _curve_for(metric: str, active: FrozenSet[str]) -> np.ndarray:
        vals = [
            float(scalar_values[metric][thr][active])
            for thr in ordered_thresholds
        ]
        return np.asarray(vals, dtype=float)

    for metric in ('flooded_area_km2', 'buildings_exposed', 'population_exposed'):
        metric_rows = {}
        for active in all_subsets():
            curve = _curve_for(metric, active)
            finite = np.isfinite(curve)
            diffs = np.diff(curve)
            # Allow only tiny numerical/API jitter. Local raster/building metrics
            # should be exact; population permits 0.5% relative or 1 person.
            if metric == 'population_exposed':
                scale = max(1.0, float(np.nanmax(np.abs(curve))) if finite.any() else 1.0)
                tol = max(1.0, 0.005 * scale)
            else:
                tol = 1e-9

            violating = [
                {
                    'from_threshold_m': ordered_thresholds[i],
                    'to_threshold_m': ordered_thresholds[i + 1],
                    'increase': float(diffs[i]),
                }
                for i in range(len(diffs))
                if np.isfinite(diffs[i]) and diffs[i] > tol
            ]
            passed = len(violating) == 0
            metric_rows[_scenario_code(active)] = {
                'passes': passed,
                'tolerance': float(tol),
                'violations': violating,
            }

            if metric in ('flooded_area_km2', 'buildings_exposed') and not passed:
                monotonicity_qc['passes_local_metrics'] = False

        monotonicity_qc['metrics'][metric] = metric_rows

    if not monotonicity_qc['passes_local_metrics']:
        bad = []
        for metric in ('flooded_area_km2', 'buildings_exposed'):
            for code, rec in monotonicity_qc['metrics'][metric].items():
                if not rec['passes']:
                    bad.append(f'{metric}:{code}:{rec["violations"]}')
        raise RuntimeError(
            'Exposure-depth monotonicity QC FAILED. '
            'This should be mathematically impossible and indicates a bug: '
            + ' | '.join(bad)
        )

    log('Exposure-depth monotonicity QC: PASS for flooded area and buildings')

    scenario_df.to_csv(artifact('scenario_hazard_exposure.csv'), index=False)

    # ------------------------------------------------------------------
    # Exposure-depth curves. Publication figures use primary thresholds only;
    # all-threshold diagnostic plots retain the 0.05-m sensitivity case.
    # ------------------------------------------------------------------
    import matplotlib.pyplot as plt

    # Publication color contract: use these exact semantic colors in every plot.
    PLOT_COLORS = {
        'compound': 'tab:red',
        'coastal': 'tab:blue',
        'precipitation': 'tab:orange',
        'river': 'tab:green',
    }
    curve_scenarios = {
        _scenario_code(frozenset(DRIVERS)): ('Full C+P+R', PLOT_COLORS['compound']),
        _scenario_code(frozenset({'coastal'})): ('Coastal only', PLOT_COLORS['coastal']),
        _scenario_code(frozenset({'precipitation'})): (
            'Precipitation only', PLOT_COLORS['precipitation']
        ),
        _scenario_code(frozenset({'river'})): ('River only', PLOT_COLORS['river']),
    }

    def _plot_curves(metric: str, df: pd.DataFrame, out_name: str, suffix: str = '') -> None:
        fig, ax = plt.subplots(figsize=(7.2, 4.5))
        plotted = 0
        for code, (label, color) in curve_scenarios.items():
            sub = df[df['scenario'] == code].sort_values('depth_threshold_m')
            vals = pd.to_numeric(sub[metric], errors='coerce')
            if sub.empty or not np.isfinite(vals).any():
                continue
            ax.plot(
                sub['depth_threshold_m'],
                vals,
                marker='o',
                label=label,
                color=color,
            )
            plotted += 1
        if plotted:
            ax.set_xlabel('Flood-depth threshold [m]')
            ax.set_ylabel(metric.replace('_', ' ').title())
            ax.set_title(f"Exposure-Depth Curve: {metric.replace('_', ' ').title()}{suffix}")
            ax.grid(alpha=0.2)
            ax.legend()
            fig.tight_layout()
            fig.savefig(artifact(out_name), dpi=300, bbox_inches='tight')
        plt.close(fig)

    primary_df = scenario_df[scenario_df['depth_threshold_m'].isin(primary_thresholds)]
    for metric in ('flooded_area_km2', 'buildings_exposed', 'population_exposed'):
        if metric not in scenario_df.columns:
            continue
        _plot_curves(metric, primary_df, f'exposure_depth_curve_{metric}.png')
        _plot_curves(
            metric, scenario_df,
            f'exposure_depth_curve_{metric}_all_thresholds.png',
            suffix=' (all thresholds)',
        )

    # ------------------------------------------------------------------
    # Flood duration and duration attribution from existing time outputs.
    # No SFINCS scenarios are added or rerun.
    # ------------------------------------------------------------------
    duration_cfg = cfg.get('analysis', {}).get('duration', {}) or {}
    duration_enabled = bool(duration_cfg.get('enabled', True))
    duration_thresholds = [
        float(v) for v in duration_cfg.get(
            'depth_thresholds_m', primary_thresholds
        )
    ]
    duration_thresholds = sorted(set(duration_thresholds))
    persistence_thresholds_h = sorted(set(
        float(v) for v in duration_cfg.get(
            'persistence_thresholds_hours', [6, 12, 24, 48]
        )
    ))
    duration_scenario_rows: List[Dict[str, Any]] = []
    duration_shapley_rows: List[Dict[str, Any]] = []
    duration_summary_rows: List[Dict[str, Any]] = []
    duration_scalar_values: Dict[
        str, Dict[float, Dict[FrozenSet[str], float]]
    ] = {
        'flooded_area_hours_km2_h': {},
        'building_hours': {},
    }
    duration_meta: Dict[str, Any] = {
        'enabled': duration_enabled,
        'depth_basis': 'absolute time-varying depth on static land',
        'depth_thresholds_m': duration_thresholds,
        'persistence_thresholds_hours': persistence_thresholds_h,
        'n_new_sfincs_runs': 0,
    }

    if duration_enabled and duration_thresholds:
        duration_arrays: Dict[
            float, Dict[FrozenSet[str], np.ndarray]
        ] = {thr: {} for thr in duration_thresholds}
        reference_time_signature = None
        scenario_time_meta: Dict[str, Any] = {}
        for active in all_subsets():
            run = Path(manifest['scenarios'][_scenario_code(active)]['path'])
            by_thr, time_signature, time_meta = extract_duration_hours(
                run, duration_thresholds
            )
            if reference_time_signature is None:
                reference_time_signature = time_signature
            elif not np.array_equal(time_signature, reference_time_signature):
                raise RuntimeError(
                    'Scenario saved-output times differ; duration attribution is invalid: '
                    + _scenario_code(active)
                )
            scenario_time_meta[_scenario_code(active)] = time_meta
            for threshold, arr in by_thr.items():
                if arr.shape != full.shape:
                    if arr.T.shape == full.shape:
                        arr = arr.T
                    else:
                        raise RuntimeError(
                            f'Duration grid mismatch for {_scenario_code(active)}: '
                            f'{arr.shape} vs {full.shape}'
                        )
                duration_arrays[threshold][active] = np.where(
                    static_land, arr, np.nan
                ).astype(np.float32)
            log(f'Duration integrated: {_scenario_code(active)}')

        duration_closure_rows: List[Dict[str, Any]] = []
        for threshold in duration_thresholds:
            values = duration_arrays[threshold]
            full_duration = values[frozenset(DRIVERS)]
            _write_tif(
                artifact(
                    f'flood_duration_full_ge_{_slug_num(threshold)}m_hours.tif'
                ),
                full_duration, ref_x, ref_y, ref_epsg,
            )
            phi_duration = shapley_arrays(values)
            closure_duration = np.zeros_like(full_duration, dtype=np.float32)
            for driver, arr in phi_duration.items():
                closure_duration += arr
                _write_tif(
                    artifact(
                        f'shapley_flood_duration_{driver}_ge_'
                        f'{_slug_num(threshold)}m_hours.tif'
                    ),
                    arr, ref_x, ref_y, ref_epsg,
                )
            target_duration = (
                values[frozenset(DRIVERS)] - values[frozenset()]
            )
            valid_duration = (
                np.isfinite(closure_duration) & np.isfinite(target_duration)
            )
            duration_error = (
                float(np.max(np.abs(
                    closure_duration[valid_duration] - target_duration[valid_duration]
                ))) if np.any(valid_duration) else np.nan
            )
            duration_closure_rows.append({
                'depth_threshold_m': threshold,
                'max_abs_error_hours': duration_error,
                'passes_1e-4_hours': bool(
                    np.isfinite(duration_error) and duration_error < 1e-4
                ),
            })

            for metric in duration_scalar_values:
                duration_scalar_values[metric][threshold] = {}
            for active in all_subsets():
                dur = values[active]
                area_hours = float(
                    np.nansum(
                        np.where(
                            static_land,
                            dur * np.clip(land_fraction, 0.0, 1.0),
                            np.nan,
                        ),
                        dtype=np.float64,
                    ) * cell_area_m2 / 1e6
                )
                building_hours = np.nan
                if (
                    building_eligible is not None
                    and building_rows is not None
                    and building_cols is not None
                    and building_grid_valid is not None
                ):
                    sampled = _sample_buildings_from_indices(
                        dur, building_rows, building_cols, building_grid_valid
                    )
                    building_hours = float(np.nansum(
                        sampled[building_eligible], dtype=np.float64
                    ))
                duration_scalar_values[
                    'flooded_area_hours_km2_h'
                ][threshold][active] = area_hours
                duration_scalar_values[
                    'building_hours'
                ][threshold][active] = building_hours
                wet_duration = dur[static_land & np.isfinite(dur) & (dur > 0)]
                duration_scenario_rows.append({
                    'scenario': _scenario_code(active),
                    'active_drivers': '+'.join(sorted(active)) or 'none',
                    'depth_threshold_m': threshold,
                    'duration_basis': 'absolute depth with linear crossing interpolation',
                    'flooded_area_hours_km2_h': area_hours,
                    'building_hours': building_hours,
                    'mean_duration_hours_ever_exceeded': (
                        float(np.mean(wet_duration)) if wet_duration.size else 0.0
                    ),
                    'p95_duration_hours_ever_exceeded': (
                        float(np.percentile(wet_duration, 95)) if wet_duration.size else 0.0
                    ),
                    'max_duration_hours': (
                        float(np.max(wet_duration)) if wet_duration.size else 0.0
                    ),
                })

            for metric, by_threshold in duration_scalar_values.items():
                values_scalar = by_threshold[threshold]
                if not all(np.isfinite(v) for v in values_scalar.values()):
                    continue
                shp_duration = shapley_scalars(values_scalar)
                none_value = float(values_scalar[frozenset()])
                full_value = float(values_scalar[frozenset(DRIVERS)])
                for driver, value in shp_duration.items():
                    duration_shapley_rows.append({
                        'metric': metric,
                        'depth_threshold_m': threshold,
                        'driver': driver,
                        'shapley_value': value,
                        'none_baseline': none_value,
                        'full_value': full_value,
                        'fraction_of_full_minus_none': (
                            value / (full_value - none_value)
                            if abs(full_value - none_value) > 1e-12 else np.nan
                        ),
                    })

            full_building_duration = None
            if (
                building_eligible is not None
                and building_rows is not None
                and building_cols is not None
                and building_grid_valid is not None
            ):
                full_building_duration = _sample_buildings_from_indices(
                    full_duration, building_rows, building_cols, building_grid_valid
                )
            for persistence_h in persistence_thresholds_h:
                persistent = (
                    static_land & np.isfinite(full_duration)
                    & (full_duration >= persistence_h)
                )
                persistent_area = float(
                    np.sum(np.where(
                        persistent,
                        np.clip(land_fraction, 0.0, 1.0),
                        0.0,
                    ), dtype=np.float64) * cell_area_m2 / 1e6
                )
                persistent_buildings = np.nan
                if full_building_duration is not None:
                    persistent_buildings = float(np.sum(
                        building_eligible
                        & np.isfinite(full_building_duration)
                        & (full_building_duration >= persistence_h)
                    ))
                duration_summary_rows.append({
                    'scenario': '111_cpr',
                    'depth_threshold_m': threshold,
                    'persistence_threshold_hours': persistence_h,
                    'persistent_flood_area_km2': persistent_area,
                    'persistent_buildings': persistent_buildings,
                })

        pd.DataFrame(duration_scenario_rows).to_csv(
            artifact('scenario_duration_metrics.csv'), index=False
        )
        pd.DataFrame(duration_shapley_rows).to_csv(
            artifact('shapley_duration_metrics.csv'), index=False
        )
        pd.DataFrame(duration_summary_rows).to_csv(
            artifact('flood_duration_summary.csv'), index=False
        )
        duration_meta.update({
            'scenario_time_outputs': scenario_time_meta,
            'shapley_closure': duration_closure_rows,
            'outputs': {
                'scenario_metrics': 'scenario_duration_metrics.csv',
                'shapley_metrics': 'shapley_duration_metrics.csv',
                'persistent_full_summary': 'flood_duration_summary.csv',
            },
        })
        artifact('duration_analysis_summary.json').write_text(
            json.dumps(duration_meta, indent=2, default=str)
        )

    # ------------------------------------------------------------------
    # Scalar Shapley/factorial/amplification tables.
    # ------------------------------------------------------------------
    shapley_rows: List[Dict[str, Any]] = []
    for metric, by_thr in scalar_values.items():
        for threshold, values in by_thr.items():
            if not all(np.isfinite(v) for v in values.values()):
                continue
            shp = shapley_scalars(values)
            nonev = float(values[frozenset()])
            fullv = float(values[frozenset(DRIVERS)])
            for driver, val in shp.items():
                shapley_rows.append({
                    'metric': metric,
                    'depth_threshold_m': threshold,
                    'threshold_role': 'primary' if threshold in primary_thresholds else 'sensitivity',
                    'driver': driver,
                    'shapley_value': val,
                    'none_baseline': nonev,
                    'full_value': fullv,
                    'fraction_of_full_minus_none': (
                        val / (fullv - nonev) if abs(fullv - nonev) > 1e-12 else np.nan
                    ),
                })
    shp_df = pd.DataFrame(shapley_rows)
    shp_df.to_csv(artifact('shapley_hazard_exposure.csv'), index=False)

    # Driver Impact Shift (DIS) compares each driver's consequence share with
    # its flooded-area share. Positive values identify consequence-heavy
    # drivers; negative values identify footprint-heavy drivers. Because both
    # share vectors close to one, DIS closes to zero percentage points.
    dis_rows = _driver_impact_shift_rows(scalar_values)
    if duration_enabled:
        for threshold, area_values in duration_scalar_values[
            'flooded_area_hours_km2_h'
        ].items():
            impact_values = duration_scalar_values['building_hours'].get(threshold, {})
            if (
                len(area_values) != 8 or len(impact_values) != 8
                or not all(np.isfinite(v) for v in area_values.values())
                or not all(np.isfinite(v) for v in impact_values.values())
            ):
                continue
            area_phi = shapley_scalars(area_values)
            impact_phi = shapley_scalars(impact_values)
            area_delta = float(
                area_values[frozenset(DRIVERS)] - area_values[frozenset()]
            )
            impact_delta = float(
                impact_values[frozenset(DRIVERS)] - impact_values[frozenset()]
            )
            valid_dis = abs(area_delta) > 1e-12 and abs(impact_delta) > 1e-12
            for driver in DRIVERS:
                area_share = area_phi[driver] / area_delta if valid_dis else np.nan
                impact_share = (
                    impact_phi[driver] / impact_delta if valid_dis else np.nan
                )
                dis_rows.append({
                    'impact_metric': 'building_hours',
                    'area_reference_metric': 'flooded_area_hours_km2_h',
                    'depth_threshold_m': float(threshold),
                    'driver': driver,
                    'reference_area_shapley_value': area_phi[driver],
                    'impact_shapley_value': impact_phi[driver],
                    'reference_area_increment_full_minus_none': area_delta,
                    'impact_increment_full_minus_none': impact_delta,
                    'area_shapley_share': area_share,
                    'impact_shapley_share': impact_share,
                    'dis_percentage_points': (
                        100.0 * (impact_share - area_share)
                        if valid_dis else np.nan
                    ),
                    'status': 'ok' if valid_dis else 'undefined_zero_increment',
                })
    dis_columns = [
        'impact_metric', 'area_reference_metric', 'depth_threshold_m', 'driver',
        'reference_area_shapley_value', 'impact_shapley_value',
        'reference_area_increment_full_minus_none',
        'impact_increment_full_minus_none', 'area_shapley_share',
        'impact_shapley_share', 'dis_percentage_points', 'status',
    ]
    dis_df = pd.DataFrame(dis_rows, columns=dis_columns)
    dis_df.to_csv(artifact('driver_impact_shift.csv'), index=False)
    dis_closure: List[Dict[str, Any]] = []
    if not dis_df.empty:
        for (metric, threshold), subdf in dis_df.groupby(
            ['impact_metric', 'depth_threshold_m']
        ):
            vals = pd.to_numeric(subdf['dis_percentage_points'], errors='coerce')
            closure_value = float(vals.sum()) if np.isfinite(vals).all() else np.nan
            dis_closure.append({
                'impact_metric': metric,
                'depth_threshold_m': float(threshold),
                'sum_dis_percentage_points': closure_value,
                'passes_1e-8_pp': bool(
                    np.isfinite(closure_value) and abs(closure_value) < 1e-8
                ),
            })

    factorial_scalar_rows: List[Dict[str, Any]] = []
    for metric, by_thr in scalar_values.items():
        for threshold, values in by_thr.items():
            if not all(np.isfinite(v) for v in values.values()):
                continue
            terms = factorial_terms_scalars(values)
            base = float(values[frozenset()])
            fullv = float(values[frozenset(DRIVERS)])
            for term_name, val in terms.items():
                factorial_scalar_rows.append({
                    'metric': metric,
                    'depth_threshold_m': threshold,
                    'threshold_role': 'primary' if threshold in primary_thresholds else 'sensitivity',
                    'term': term_name,
                    'value': float(val),
                    'none_baseline': base,
                    'full_value': fullv,
                    'fraction_of_full_minus_none': (
                        float(val) / (fullv - base) if abs(fullv - base) > 1e-12 else np.nan
                    ),
                })
    factorial_scalar_df = pd.DataFrame(factorial_scalar_rows)
    factorial_scalar_df.to_csv(
        artifact('factorial_interactions_hazard_exposure.csv'), index=False
    )

    amplification_rows: List[Dict[str, Any]] = []
    for metric, by_thr in scalar_values.items():
        for threshold, values in by_thr.items():
            if not all(np.isfinite(v) for v in values.values()):
                continue
            fullv = float(values[frozenset(DRIVERS)])
            single_max = max(float(values[frozenset({d})]) for d in DRIVERS)
            val = fullv - single_max
            amplification_rows.append({
                'metric': metric,
                'depth_threshold_m': threshold,
                'threshold_role': 'primary' if threshold in primary_thresholds else 'sensitivity',
                'full_value': fullv,
                'maximum_single_driver_value': single_max,
                'compound_amplification': val,
                'relative_to_full': val / fullv if abs(fullv) > 1e-12 else np.nan,
            })
    amplification_df = pd.DataFrame(amplification_rows)
    amplification_df.to_csv(
        artifact('compound_amplification_hazard_exposure.csv'), index=False
    )

    # ------------------------------------------------------------------
    # Publication-oriented summary.
    # ------------------------------------------------------------------
    amp_pos = static_land & np.isfinite(amp) & (amp > 0)
    summary = {
        'drivers': list(DRIVERS),
        'n_factorial_scenarios': 8,
        'n_new_runs_required': 7,
        'shapley_closure': {
            'rmse_m': closure_rmse,
            'max_abs_m': closure_max,
            'passes_1e-5_m': bool(np.isfinite(closure_max) and closure_max < 1e-5),
        },
        'factorial_interaction_closure': {
            'rmse_m': factorial_closure_rmse,
            'max_abs_m': factorial_closure_max,
            'passes_1e-5_m': bool(
                np.isfinite(factorial_closure_max) and factorial_closure_max < 1e-5
            ),
            'terms': [
                'main_coastal', 'main_precipitation', 'main_river',
                'interaction_coastal_precipitation',
                'interaction_coastal_river',
                'interaction_precipitation_river',
                'interaction_coastal_precipitation_river',
            ],
            'interpretation': (
                'Positive interaction terms are synergistic relative to additive single-driver '
                'effects; negative terms are antagonistic. Shapley values separately allocate '
                'all interaction effects back to the three drivers.'
            ),
        },
        'land_and_event_exposure_mask': {
            **land_meta,
            'background_scenario': '000_none',
            'exposure_universe_cells': int(exposure_universe.sum()),
            'depth_definition': depth_definition,
            'none_background_depth_on_land': none_depth_stats,
            'definition': (
                'The spatial universe is every static-land model cell and never changes with '
                'reporting threshold. SFINCS dry-depth NaNs are interpreted as 0 m on active '
                'cells. Headline impacts use absolute scenario depth. The separately labelled '
                'event-induced diagnostic uses delta_h=max(H_scenario-H_NONE,0).'
            ),
        },
        'absolute_impact_reporting': {
            'headline_scenario': '111_cpr',
            'baseline_scenario': '000_none',
            'headline_basis': 'absolute maximum scenario depth on static land',
            'shapley_target': 'f(111_cpr)-f(000_none)',
            'headline_table': 'absolute_impact_summary.csv',
            'scenario_table': 'scenario_hazard_exposure.csv',
            'event_excess_diagnostic_table': (
                'scenario_event_induced_excess_depth.csv'
            ),
            'analysis_land_population': (
                float(total_analysis_population)
                if np.isfinite(total_analysis_population) else None
            ),
            'population_percentage_denominator': (
                'WorldPop population inside the fixed analysis-land mask'
            ),
        },
        'cdc_svi_burden': svi_summary,
        'flood_duration': duration_meta,
        'driver_impact_shift': {
            'output': 'driver_impact_shift.csv',
            'definition': (
                'DIS_d,M = 100 * [phi_d,M/(M_FULL-M_NONE) - '
                'phi_d,area/(Area_FULL-Area_NONE)] percentage points'
            ),
            'interpretation': (
                'Positive is consequence-heavy relative to flooded footprint; '
                'negative is footprint-heavy. Negative Shapley shares are retained.'
            ),
            'closure': dis_closure,
        },
        'exposure_depth_monotonicity_qc': monotonicity_qc,
        'compound_amplification': {
            'raw_positive_area_km2_diagnostic': float(amp_pos.sum() * cell_area_m2 / 1e6),
            'mean_positive_m_diagnostic': float(np.nanmean(amp[amp_pos])) if amp_pos.any() else 0.0,
            'max_m': float(np.nanmax(amp)) if np.isfinite(amp).any() else None,
            'threshold_table': amp_spatial_rows,
            'absolute_thresholds_m': abs_amp_thresholds,
            'relative_thresholds': rel_amp_thresholds,
            'relative_min_full_depth_m': rel_min_full_depth,
            'definition': 'H_CPR - max(H_C, H_P, H_R), evaluated on static land',
            'publication_guidance': (
                'Use thresholded amplification areas, not the raw A>0 area, because '
                'millimetric positive differences can cover large areas.'
            ),
        },
        'depth_thresholds_m': thresholds,
        'primary_min_depth_m': primary_min_depth_m,
        'primary_depth_thresholds_m': primary_thresholds,
        'sensitivity_depth_thresholds_m': sensitivity_thresholds,
        'dominant_driver_classes': {
            '0': 'undefined', '1': 'coastal', '2': 'precipitation', '3': 'river'
        },
        'building_exposure_source': 'Overture Maps' if buildings is not None else None,
        'building_exposure_operator': (
            'building representative point -> SFINCS computational cell; '
            'dry-cell NaN is 0 m; headline counts use absolute scenario depth'
        ),
        'population_exposure_source': (
            (
                'WorldPop Global2 v2 official STAC raster'
                if population_provider == 'stac_v2'
                else 'WorldPop API v2'
            ) if use_population else None
        ),
        'population_resolution': pcfg.get('resolution', '100m') if use_population else None,
        'dry_depth_semantics': (
            'NaN depth on an active SFINCS cell is interpreted as 0 m (dry), not missing data'
        ),
        'attribution_grid': 'SFINCS computational grid',
        'plot_color_contract': {
            'compound_C+P+R': 'red',
            'coastal': 'blue',
            'precipitation': 'orange',
            'river': 'green',
        },
        'remote_sensing_used': False,
    }
    artifact('attribution_summary.json').write_text(
        json.dumps(summary, indent=2, default=str)
    )

    # Shapley bar plots: primary thresholds are the publication default; retain
    # all-threshold versions for sensitivity diagnosis.
    if not shp_df.empty:
        for metric in shp_df['metric'].unique():
            for label, dff, suffix in (
                ('primary', shp_df[shp_df['threshold_role'] == 'primary'], ''),
                ('all', shp_df, '_all_thresholds'),
            ):
                sub = dff[dff['metric'] == metric]
                if sub.empty:
                    continue
                piv = sub.pivot(
                    index='depth_threshold_m', columns='driver', values='shapley_value'
                )
                driver_order = [
                    d for d in ('coastal', 'precipitation', 'river')
                    if d in piv.columns
                ]
                piv = piv.reindex(columns=driver_order)
                ax = piv.plot(
                    kind='bar',
                    figsize=(8, 4.5),
                    color=[PLOT_COLORS[d] for d in driver_order],
                )
                title_suffix = '' if label == 'primary' else ' (all thresholds)'
                ax.set_title(
                    f"Shapley Attribution: {metric.replace('_', ' ').title()}{title_suffix}"
                )
                ax.set_xlabel('Depth threshold [m]')
                ax.set_ylabel(metric.replace('_', ' ').title())
                ax.grid(axis='y', alpha=0.2)
                fig = ax.get_figure()
                fig.tight_layout()
                fig.savefig(
                    artifact(f'shapley_{metric}{suffix}.png'),
                    dpi=300, bbox_inches='tight',
                )
                plt.close(fig)

    # Clear headline impact graphic: absolute FULL and NONE totals are never
    # mixed with event-excess diagnostics.
    if not absolute_df.empty:
        fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.2))
        for ax, metric, label in zip(
            axes,
            ('flooded_area_km2', 'buildings_exposed', 'population_exposed'),
            ('Flooded area [km²]', 'Buildings exposed', 'Population exposed'),
        ):
            ax.plot(
                absolute_df['depth_threshold_m'], absolute_df[f'full_{metric}'],
                marker='o', color=PLOT_COLORS['compound'], label='FULL C+P+R',
            )
            ax.plot(
                absolute_df['depth_threshold_m'], absolute_df[f'none_{metric}'],
                marker='o', color='0.45', linestyle='--', label='NONE baseline',
            )
            ax.set_xlabel('Depth threshold [m]')
            ax.set_ylabel(label)
            ax.grid(alpha=0.2)
        axes[0].legend()
        fig.suptitle('Absolute Impact Summary')
        fig.tight_layout()
        fig.savefig(
            artifact('absolute_impact_summary.png'),
            dpi=300, bbox_inches='tight',
        )
        plt.close(fig)

    if not dis_df.empty:
        for metric in dis_df['impact_metric'].dropna().unique():
            sub = dis_df[
                (dis_df['impact_metric'] == metric)
                & np.isfinite(pd.to_numeric(
                    dis_df['dis_percentage_points'], errors='coerce'
                ))
            ]
            if sub.empty:
                continue
            piv = sub.pivot(
                index='depth_threshold_m', columns='driver',
                values='dis_percentage_points',
            ).reindex(columns=list(DRIVERS))
            ax = piv.plot(
                kind='bar', figsize=(8.2, 4.6),
                color=[PLOT_COLORS[d] for d in DRIVERS],
            )
            ax.axhline(0, color='black', linewidth=0.8)
            ax.set_title(f'Driver Impact Shift: {str(metric).replace("_", " ").title()}')
            ax.set_xlabel('Depth threshold [m]')
            ax.set_ylabel('DIS [percentage points]')
            ax.grid(axis='y', alpha=0.2)
            ax.text(
                0.01, 0.01,
                'Positive = consequence-heavy; negative = footprint-heavy',
                transform=ax.transAxes, fontsize=8,
            )
            fig = ax.get_figure()
            fig.tight_layout()
            fig.savefig(
                artifact(f'driver_impact_shift_{metric}.png'),
                dpi=300, bbox_inches='tight',
            )
            plt.close(fig)

    if svi_rows:
        svi_df = pd.DataFrame(svi_rows)
        piv = svi_df.pivot(
            index='depth_threshold_m', columns='svi_quartile',
            values='share_of_svi_covered_affected_population_pct',
        ).reindex(columns=['Q1', 'Q2', 'Q3', 'Q4'])
        ax = piv.plot(
            kind='bar', stacked=True, figsize=(8.2, 4.8),
            color=['#d9f0d3', '#addd8e', '#78c679', '#238443'],
        )
        ax.set_title('Affected Population by CDC SVI Quartile')
        ax.set_xlabel('Absolute flood-depth threshold [m]')
        ax.set_ylabel('Share of SVI-covered affected population [%]')
        ax.set_ylim(0, 100)
        ax.grid(axis='y', alpha=0.2)
        fig = ax.get_figure()
        fig.tight_layout()
        fig.savefig(
            artifact('cdc_svi_burden_summary.png'),
            dpi=300, bbox_inches='tight',
        )
        plt.close(fig)

    if duration_summary_rows:
        duration_summary_df = pd.DataFrame(duration_summary_rows)
        fig, ax = plt.subplots(figsize=(7.8, 4.6))
        for threshold in duration_thresholds:
            sub = duration_summary_df[
                duration_summary_df['depth_threshold_m'] == threshold
            ].sort_values('persistence_threshold_hours')
            ax.plot(
                sub['persistence_threshold_hours'],
                sub['persistent_flood_area_km2'],
                marker='o', label=f'depth >= {threshold:g} m',
            )
        ax.set_title('Persistent Flood Area in FULL Scenario')
        ax.set_xlabel('Minimum duration [hours]')
        ax.set_ylabel('Persistent flooded area [km²]')
        ax.grid(alpha=0.2)
        ax.legend()
        fig.tight_layout()
        fig.savefig(
            artifact('flood_duration_summary.png'),
            dpi=300, bbox_inches='tight',
        )
        plt.close(fig)

    refresh_artifact_catalog(layout)
    log(f'Analysis complete: {results_dir}')
    return results_dir

def self_test() -> None:
    # Exact additive synthetic game: v(S)=sum coefficients -> Shapley must
    # recover coefficients exactly for arrays and scalars.
    a = np.ones((3, 4), dtype=np.float32)
    coeff = {"coastal": 2.0, "precipitation": 3.0, "river": 5.0}
    values = {
        s: a * (7.0 + sum(coeff[d] for d in s))
        for s in all_subsets()
    }
    shp = shapley_arrays(values)
    for d in DRIVERS:
        assert np.allclose(shp[d], coeff[d])
    scal = {s: float(11 + sum(coeff[d] for d in s)) for s in all_subsets()}
    ss = shapley_scalars(scal)
    for d in DRIVERS:
        assert abs(ss[d] - coeff[d]) < 1e-12
    closure = sum(shp.values())
    assert np.allclose(closure, values[frozenset(DRIVERS)] - values[frozenset()])

    # Synthetic game with known interactions.
    def game(s):
        v = 1.0
        if "coastal" in s: v += 2.0
        if "precipitation" in s: v += 3.0
        if "river" in s: v += 5.0
        if {"coastal", "precipitation"}.issubset(s): v += 7.0
        if {"coastal", "river"}.issubset(s): v -= 1.5
        if {"precipitation", "river"}.issubset(s): v += 0.5
        if set(DRIVERS).issubset(s): v += 4.0
        return v
    ivals = {ss: float(game(set(ss))) for ss in all_subsets()}
    terms = factorial_terms_scalars(ivals)
    assert abs(terms["main_coastal"] - 2.0) < 1e-12
    assert abs(terms["main_precipitation"] - 3.0) < 1e-12
    assert abs(terms["main_river"] - 5.0) < 1e-12
    assert abs(terms["interaction_coastal_precipitation"] - 7.0) < 1e-12
    assert abs(terms["interaction_coastal_river"] + 1.5) < 1e-12
    assert abs(terms["interaction_precipitation_river"] - 0.5) < 1e-12
    assert abs(terms["interaction_coastal_precipitation_river"] - 4.0) < 1e-12
    assert abs(sum(terms.values()) - (ivals[frozenset(DRIVERS)] - ivals[frozenset()])) < 1e-12

    # Linear threshold-crossing duration is exact for a linearly varying pair.
    d0 = np.asarray([[0.0, 2.0, 2.0, 0.0]], dtype=np.float32)
    d1 = np.asarray([[2.0, 0.0, 3.0, 0.5]], dtype=np.float32)
    frac = _linear_exceedance_fraction(d0, d1, 1.0)
    assert np.allclose(frac, [[0.5, 0.5, 1.0, 0.0]])

    # CDC SVI boundary behavior follows the four national percentile bins.
    assert [_svi_quartile(v) for v in (0, 0.25, 0.5, 0.75, 1.0)] == [
        'Q1', 'Q1', 'Q2', 'Q3', 'Q4'
    ]
    assert _svi_quartile(-999) is None

    # DIS known game: footprint shares 20/30/50%, consequence shares
    # 60/30/10%, hence +40/0/-40 percentage points.
    area_game = {
        s: float(sum({'coastal': 2, 'precipitation': 3, 'river': 5}[d] for d in s))
        for s in all_subsets()
    }
    impact_game = {
        s: float(sum({'coastal': 6, 'precipitation': 3, 'river': 1}[d] for d in s))
        for s in all_subsets()
    }
    dis_test = _driver_impact_shift_rows({
        'flooded_area_km2': {0.1: area_game},
        'buildings_exposed': {0.1: impact_game},
        'population_exposed': {},
    })
    got_dis = {r['driver']: r['dis_percentage_points'] for r in dis_test}
    assert abs(got_dis['coastal'] - 40.0) < 1e-10
    assert abs(got_dis['precipitation']) < 1e-10
    assert abs(got_dis['river'] + 40.0) < 1e-10
    assert abs(sum(got_dis.values())) < 1e-10

    # Population country selection comes from the AOI, not model names,
    # directories, or forcing providers.
    country_parts = _worldpop_country_parts(
        box(-95.45, 29.70, -95.25, 29.90),
        {'analysis': {'exposure': {'population': {}}}},
    )
    assert {iso3 for iso3, _part in country_parts} == {'USA'}
    try:
        _worldpop_year_resolution({
            'simulation': {'start': '2014-01-01T00:00:00'},
            'analysis': {'exposure': {'population': {}}},
        })
    except RuntimeError as exc:
        assert '2015-2030' in str(exc)
    else:
        raise AssertionError('pre-2015 WorldPop v2 year was not rejected')
    print("SELF_TEST_PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sfincs_agent_config.yml")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="Create seven counterfactual scenario directories.")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("prepare-exposure", help="Download/cache Overture buildings on login node.")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("run", help="Run seven counterfactual scenarios offline.")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("report", help="Compute Shapley, amplification, and exposure.")
    p.add_argument("--skip-population", action="store_true")

    sub.add_parser("self-test")

    args = ap.parse_args()
    if args.command == "self-test":
        self_test()
        return 0
    cfg = read_config(Path(args.config))
    if args.command == "prepare":
        print(prepare_scenarios(cfg, force=args.force))
    elif args.command == "prepare-exposure":
        print(prepare_overture_buildings(cfg, force=args.force))
    elif args.command == "run":
        run_scenarios(cfg, force=args.force)
    elif args.command == "report":
        print(analyze(cfg, skip_population=args.skip_population))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
