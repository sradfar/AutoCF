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
  - flooded-area Shapley attribution at configured depth thresholds
  - compound amplification: H_CPR - max(H_C, H_P, H_R), with meaningful thresholds
  - Overture building exposure-depth curves
  - WorldPop population exposure-depth curves
  - Shapley attribution of event-induced flooded land/buildings/population
  - static-land masking using ESA WorldCover + NONE-background excess-depth exposure

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
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import xarray as xr
from pyproj import CRS, Transformer
from shapely.geometry import Point, box, shape, mapping
from shapely.ops import transform as shp_transform, unary_union

from sfincs_config import read_config, resolved_output_dir

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
    run = resolved_output_dir(cfg)
    return run / "agent_analysis"


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
    run = resolved_output_dir(cfg)
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
        # Ensure old outputs cannot be mistaken for scenario results.
        for name in ("sfincs_map.nc", "sfincs_his.nc", "sfincs_log.txt", "sfincs.log", "run_gpu.log"):
            p = dst / name
            if p.exists():
                p.unlink()

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
    out = analysis_root(cfg) / "exposure_data" / "overture_buildings.parquet"
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
    run = Path(resolved_output_dir(cfg))
    candidates = [
        run / 'agent_data' / 'esa_worldcover_model_crs_subgrid_10m.tif',
        run / 'agent_data' / 'esa_worldcover_model_crs.tif',
        run / 'esa_worldcover_model_crs_subgrid_10m.tif',
        run / 'esa_worldcover_model_crs.tif',
    ]
    for c in candidates:
        if c.exists():
            return c
    for pattern in ('agent_data/*worldcover*.tif', '**/*worldcover*.tif'):
        hits = sorted(run.glob(pattern))
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


def _worldpop_geojson(geom, ndigits: int = 6) -> Dict[str, Any]:
    return _round_geojson_numbers(mapping(geom), ndigits=ndigits)


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

    g1 = geom.intersection(b1)
    g2 = geom.intersection(b2)
    if g1.is_empty:
        g1 = None
    if g2.is_empty:
        g2 = None
    return g1, g2


def _worldpop_request_one(
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


def _worldpop_population(geom, cfg: Dict[str, Any], cache: Path) -> float:
    """Population total with automatic 413-safe polygon decomposition.

    WorldPop's polygon endpoint can reject highly fragmented flood masks with
    HTTP 413. Rather than changing the science or simplifying the mask until it
    loses shape, AutoCF recursively partitions the same polygon into disjoint
    pieces, queries each piece, and sums the cached totals.
    """
    if geom is None or geom.is_empty:
        return 0.0

    pcfg = cfg.get("analysis", {}).get("exposure", {}).get("population", {}) or {}
    year = pcfg.get("year", "auto")
    if str(year).lower() == "auto":
        year = pd.Timestamp(cfg["simulation"]["start"]).year
        year = min(2030, max(2015, int(year)))
    year = int(year)
    resolution = str(pcfg.get("resolution", "100m"))

    coordinate_decimals = max(
        4, min(8, int(pcfg.get("coordinate_decimals", 6)))
    )
    # Proactive split threshold. The server's actual HTTP-body limit is not
    # part of the public API contract, so 250 kB is a conservative transport
    # threshold; any unexpected 413 triggers additional adaptive splitting.
    max_geojson_bytes = max(
        20_000, int(pcfg.get("max_geojson_bytes", 250_000))
    )
    max_split_depth = max(
        1, int(pcfg.get("max_split_depth", 12))
    )

    # Backward-compatible cache lookup from V2.3/V2.4. This prevents already
    # successful WorldPop jobs from being submitted again after upgrading the
    # HTTP-413 transport logic.
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
            "method": "adaptive_disjoint_split_v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    aggregate_sig = hashlib.sha256(
        aggregate_key.encode()
    ).hexdigest()[:24]
    cache.mkdir(parents=True, exist_ok=True)
    aggregate_cp = cache / f"worldpop_total_{aggregate_sig}.json"
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
            subtotal = _worldpop_request_one(
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
                "method": "adaptive_disjoint_split_v1",
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


def analyze(cfg: Dict[str, Any], skip_population: bool = False) -> Path:
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
    results_dir = root / 'results'
    results_dir.mkdir(parents=True, exist_ok=True)

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
        cfg, ref_x, ref_y, ref_epsg, active_mask, results_dir
    )
    land_cfg = cfg.get('analysis', {}).get('land_mask', {}) or {}
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

    # Event-depth arrays used ONLY for thresholded hazard/exposure functionals.
    # Raw scenario depth arrays are retained for the continuous spatial Shapley
    # and factorial interaction decomposition below.
    exposure_arrays: Dict[FrozenSet[str], np.ndarray] = {}
    for active in all_subsets():
        arr = analysis_arrays[active]
        valid = exposure_universe
        ed = np.full(arr.shape, np.nan, dtype=np.float32)

        if depth_definition == 'excess_over_none':
            ed[valid] = np.maximum(
                arr[valid] - none[valid], 0.0
            ).astype(np.float32)
        else:
            ed[valid] = np.maximum(arr[valid], 0.0).astype(np.float32)

        exposure_arrays[active] = ed
        _write_tif(
            results_dir / f'event_depth_{_scenario_code(active)}.tif',
            ed, ref_x, ref_y, ref_epsg,
        )

    if depth_definition == 'excess_over_none':
        none_excess = exposure_arrays[frozenset()]
        finite_none_excess = none_excess[np.isfinite(none_excess)]
        if finite_none_excess.size and float(np.nanmax(np.abs(finite_none_excess))) > 1e-7:
            raise RuntimeError(
                'NONE event-induced excess depth is not zero; implementation error.'
            )

    full_event_depth = exposure_arrays[frozenset(DRIVERS)]
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
        _write_tif(results_dir / f'shapley_max_depth_{d}.tif', arr, ref_x, ref_y, ref_epsg)
        land_arr = np.where(static_land, arr, np.nan).astype(np.float32)
        _write_tif(
            results_dir / f'shapley_max_depth_{d}_land.tif',
            land_arr, ref_x, ref_y, ref_epsg,
        )

    factorial_terms = factorial_terms_arrays(analysis_arrays)
    for name, arr in factorial_terms.items():
        if name.startswith('interaction_'):
            _write_tif(
                results_dir / f'factorial_{name}_max_depth.tif',
                arr, ref_x, ref_y, ref_epsg,
            )
            _write_tif(
                results_dir / f'factorial_{name}_max_depth_land.tif',
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
        results_dir / 'compound_amplification_max_depth_full_domain.tif',
        amp_full_domain, ref_x, ref_y, ref_epsg,
    )
    amp = np.where(static_land, amp_full_domain, np.nan).astype(np.float32)
    _write_tif(
        results_dir / 'compound_amplification_max_depth.tif',
        amp, ref_x, ref_y, ref_epsg,
    )

    amp_cfg = acfg.get('compound_amplification', {}) or {}
    rel_eps = float(amp_cfg.get('relative_epsilon_m', 1e-6))
    amp_rel = _safe_ratio(
        amp, full, static_land & np.isfinite(full), rel_eps
    )
    _write_tif(
        results_dir / 'compound_amplification_relative.tif',
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
            results_dir / f'compound_amplification_ge_{_slug_num(thr)}m.tif',
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
            results_dir / f'compound_amplification_relative_ge_{_slug_num(thr)}.tif',
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
        results_dir / 'compound_amplification_spatial_thresholds.csv', index=False
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
            results_dir / f'shapley_fraction_max_depth_{d}.tif',
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
        results_dir / 'dominant_shapley_driver.tif',
        dominant, ref_x, ref_y, ref_epsg,
    )
    _write_tif(
        results_dir / 'dominant_shapley_fraction.tif',
        dominant_fraction, ref_x, ref_y, ref_epsg,
    )
    _write_tif(
        results_dir / 'dominant_shapley_driver_land.tif',
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
    buildings_path = root / 'exposure_data' / 'overture_buildings.parquet'
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
    # Thresholded event hazard/exposure on ONE static-land universe.
    #
    # Default V2.3 functional:
    #
    #   delta_h_S(x) = max(H_S(x) - H_NONE(x), 0)
    #
    #   E_S(d) = sum_x land(x) * I[delta_h_S(x) >= d]
    #
    # The NONE scenario is zero by definition and the same land universe is
    # used for every threshold.
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
    pop_cache = root / 'exposure_data' / 'worldpop_cache'
    scalar_values: Dict[str, Dict[float, Dict[FrozenSet[str], float]]] = {
        'flooded_area_km2': {},
        'buildings_exposed': {},
        'population_exposed': {},
    }

    none_building = building_depth.get(frozenset())

    # Building footprints themselves are exposure assets. Retain buildings on
    # the shared land/grid universe and remove NONE depth from the sampled
    # scenario depth rather than using NONE as an eligibility threshold.
    building_eligible = None
    building_event_depth: Dict[FrozenSet[str], np.ndarray] = {}
    if building_land is not None:
        building_eligible = building_land.copy()

        for active in all_subsets():
            bd = building_depth.get(active)
            if bd is None:
                continue
            out_bd = np.full(bd.shape, np.nan, dtype=np.float32)

            if depth_definition == 'excess_over_none' and none_building is not None:
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

    for threshold in thresholds:
        scalar_values['flooded_area_km2'][threshold] = {}
        scalar_values['buildings_exposed'][threshold] = {}
        scalar_values['population_exposed'][threshold] = {}

        for active in all_subsets():
            arr = exposure_arrays[active]
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
            if active in building_event_depth and building_eligible is not None:
                bd = building_event_depth[active]
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
                'metric_definition': (
                    'event-induced excess depth above NONE on static land'
                    if depth_definition == 'excess_over_none'
                    else 'absolute maximum depth on static land'
                ),
                'flooded_area_km2': area,
                'buildings_exposed': bcount,
                'population_exposed': pop,
            })
        log(f'Local land/exposure metrics complete for {threshold:g} m')

    # WorldPop requests are independent. Use bounded concurrency and preserve
    # the existing per-polygon disk cache.
    if use_population and population_jobs:
        max_workers = max(1, min(8, int(pcfg.get('max_parallel_tasks', 4))))
        log(
            f'WorldPop exposure: {len(population_jobs)} jobs, '
            f'max_parallel_tasks={max_workers}; cached jobs return immediately'
        )

        def _pop_worker(job):
            threshold, active, geom = job
            return threshold, active, float(_worldpop_population(geom, cfg, pop_cache))

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

    scenario_df = pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # Exposure-depth monotonicity QC.
    # A valid exceedance curve over a fixed mask cannot increase as the
    # threshold increases. Area/building failures are hard errors because they
    # indicate an implementation/mask bug. Population is reported with a small
    # tolerance because it is returned by an external polygon-statistics API.
    # ------------------------------------------------------------------
    monotonicity_qc: Dict[str, Any] = {
        'fixed_static_land_universe_expected': True,
        'depth_definition': depth_definition,
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

    # In excess-over-NONE mode, NONE must be exactly zero for every threshold.
    if depth_definition == 'excess_over_none':
        none_active = frozenset()
        none_nonzero = []
        for thr in ordered_thresholds:
            for metric in ('flooded_area_km2', 'buildings_exposed'):
                val = scalar_values[metric][thr][none_active]
                if np.isfinite(val) and abs(float(val)) > 1e-9:
                    none_nonzero.append((metric, thr, float(val)))
        if none_nonzero:
            raise RuntimeError(
                'NONE excess-depth exposure is nonzero; implementation error: '
                + repr(none_nonzero)
            )
    scenario_df.to_csv(results_dir / 'scenario_hazard_exposure.csv', index=False)

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
            fig.savefig(results_dir / out_name, dpi=300, bbox_inches='tight')
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
    shp_df.to_csv(results_dir / 'shapley_hazard_exposure.csv', index=False)

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
        results_dir / 'factorial_interactions_hazard_exposure.csv', index=False
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
        results_dir / 'compound_amplification_hazard_exposure.csv', index=False
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
                'cells. By default event depth is delta_h=max(H_scenario-H_NONE,0), so the NONE '
                'counterfactual removes persistent/background model water without discarding '
                'dry land cells.'
            ),
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
            'dry-cell NaN is 0 m and event depth is background-corrected against '
            'the same cell in 000_none'
        ),
        'population_exposure_source': (
            'WorldPop API v2' if use_population else None
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
    (results_dir / 'attribution_summary.json').write_text(
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
                    results_dir / f'shapley_{metric}{suffix}.png',
                    dpi=300, bbox_inches='tight',
                )
                plt.close(fig)

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
