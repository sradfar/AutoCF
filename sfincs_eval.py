#!/usr/bin/env python3
"""
sfincs_eval.py

Production evaluator for AutoCF/SFINCS Agent.

Included:
  - HydroMT/SFINCS maximum-depth map
  - global water-level gauge validation (NOAA CO-OPS -> UHSLC fallback -> file)
  - USGS/user high-water-mark validation with vertical-datum safeguards

Explicitly NOT included:
  - OPERA DSWx-S1
  - Sentinel-1/SAR flood mapping
  - Copernicus GFM
  - Google Flood Hub
  - any other remote-sensing flood-extent score

Those experimental branches were removed from the production evaluator rather
than merely disabled by a flag.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sfincs_config import read_config, resolved_output_dir, target_vertical_datum
from sfincs_global_validation import compare_model_to_water_levels

if not hasattr(np, "NaN"):
    np.NaN = np.nan


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _get_result(sf: Any, name: str):
    r = getattr(sf, "results", None)
    if r is None:
        return None
    if isinstance(r, dict):
        return r.get(name)
    try:
        return r[name] if name in r else None
    except Exception:
        return None


def _open_sfincs_minimal(run: Path):
    from hydromt_sfincs import SfincsModel
    sf = SfincsModel(root=str(run), mode="r")
    # Avoid reading forcing: HydroMT 0.9.x + newer geospatial stacks can fail
    # on geometry arrays in forcing files, while evaluation does not need them.
    sf.read_config()
    sf.read_grid()
    try:
        sf.read_geoms()
    except Exception as exc:
        log(f"HydroMT geometry-read note: {exc}")
    sf.read_results()
    sf._forcing = {}
    return sf


def _subgrid_dep_path(run: Path) -> Optional[Path]:
    candidates = [
        run / "subgrid" / "dep_subgrid.tif",
        run / "agent_data" / "dep_subgrid.tif",
        run / "dep_subgrid.tif",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Current agent commonly writes the source subgrid DEM under subgrid/.
    hits = list((run / "subgrid").glob("*dep*.tif")) if (run / "subgrid").exists() else []
    return hits[0] if hits else None


def _downscale_subgrid_depth(
    zsmax: xr.DataArray,
    dep_path: Path,
    output_tif: Path,
    hmin: float,
    block_size: int = 2000,
) -> Path:
    """Blockwise water-level minus subgrid DEM downscaling."""
    import rasterio
    from rasterio.windows import Window

    output_tif.parent.mkdir(parents=True, exist_ok=True)

    try:
        spatial_dims = set(zsmax.raster.dims)
    except Exception:
        spatial_dims = {"x", "y"}
    extra = [d for d in zsmax.dims if d not in spatial_dims]
    if extra:
        zsmax = zsmax.max(extra)

    with rasterio.open(dep_path) as src:
        profile = src.profile.copy()
        profile.update(dtype="float32", nodata=np.nan, compress="deflate", tiled=True)
        with rasterio.open(output_tif, "w", **profile) as dst:
            ntotal = math.ceil(src.height / block_size) * math.ceil(src.width / block_size)
            iblock = 0
            for row0 in range(0, src.height, block_size):
                for col0 in range(0, src.width, block_size):
                    iblock += 1
                    h = min(block_size, src.height - row0)
                    w = min(block_size, src.width - col0)
                    win = Window(col0, row0, w, h)
                    dep = src.read(1, window=win).astype(np.float32)
                    transform = src.window_transform(win)

                    x = transform.c + (np.arange(w) + 0.5) * transform.a
                    y = transform.f + (np.arange(h) + 0.5) * transform.e
                    dep_da = xr.DataArray(dep, dims=("y", "x"), coords={"x": x, "y": y}, name="dep")
                    if src.crs is None:
                        raise RuntimeError(f"No CRS found in {dep_path}")
                    epsg = src.crs.to_epsg()
                    dep_da.raster.set_crs(int(epsg) if epsg is not None else src.crs.to_wkt())
                    dep_da.raster.set_nodata(np.nan)

                    zsb = zsmax.raster.reproject_like(dep_da, method="nearest")
                    try:
                        zsb = zsb.raster.mask_nodata()
                    except Exception:
                        pass
                    depth = (zsb - dep_da).astype("float32")
                    vals = np.asarray(depth.values, dtype=np.float32)
                    if vals.shape != dep.shape:
                        if vals.T.shape == dep.shape:
                            vals = vals.T
                        else:
                            raise RuntimeError(f"Unexpected downscaled shape {vals.shape} vs {dep.shape}")
                    vals[~np.isfinite(dep)] = np.nan
                    vals[vals < hmin] = np.nan
                    dst.write(vals, 1, window=win)
                    if iblock == 1 or iblock % 10 == 0 or iblock == ntotal:
                        log(f"Subgrid depth downscale: {iblock}/{ntotal}")
    return output_tif


def make_hmax_map(
    cfg: Dict[str, Any],
    run: Path,
    out_dir: Path,
    *,
    hmin: float = 0.05,
    vmax: float = 3.0,
    use_satellite_basemap: bool = True,
) -> Path:
    """Generate the established HydroMT/SFINCS maximum-depth map."""
    sf = _open_sfincs_minimal(run)
    mode = str(cfg.get("model", {}).get("mode", "regular")).lower()

    if mode == "subgrid":
        zsmax = _get_result(sf, "zsmax")
        if zsmax is None:
            zs = _get_result(sf, "zs")
            if zs is None:
                raise RuntimeError("Neither zsmax nor zs found in SFINCS results.")
            zsmax = zs.max("time") if "time" in zs.dims else zs
        if "timemax" in zsmax.dims:
            zsmax = zsmax.max("timemax")
        dep_path = _subgrid_dep_path(run)
        if dep_path is None:
            raise RuntimeError("Subgrid mode requested but dep_subgrid.tif could not be found.")
        tif = out_dir / "hmax_subgrid_downscaled.tif"
        _downscale_subgrid_depth(zsmax, dep_path, tif, hmin)
        try:
            import hydromt
            da = hydromt.open_raster(str(tif), mask_nodata=True, chunks={"x": 2048, "y": 2048}).squeeze(drop=True)
            dep = hydromt.open_raster(str(dep_path), mask_nodata=True, chunks={"x": 2048, "y": 2048}).squeeze(drop=True)
            da = da.where(dep > -1.0)
            da.name = "hmax"
        except Exception as exc:
            log(f"HydroMT raster-open note: {exc}")
            da = None
    else:
        tif = out_dir / "hmax_model_grid.tif"
        hmax = _get_result(sf, "hmax")
        if hmax is None:
            zsmax = _get_result(sf, "zsmax")
            if zsmax is None:
                raise RuntimeError("Neither hmax nor zsmax found in SFINCS results.")
            dep = sf.grid.get("dep") if hasattr(sf.grid, "get") else sf.grid["dep"]
            hmax = zsmax - dep
        if "timemax" in hmax.dims:
            hmax = hmax.max("timemax")
        hmax = hmax.where(hmax >= hmin)
        hmax.name = "hmax"
        try:
            hmax.raster.to_raster(str(tif), compress="deflate")
        except TypeError:
            hmax.raster.to_raster(str(tif))
        da = hmax

    png = out_dir / ("hmax_subgrid_hydromt_300dpi.png" if mode == "subgrid" else "hmax_hydromt_300dpi.png")
    if da is not None and use_satellite_basemap:
        try:
            fig, ax = sf.plot_basemap(
                variable=da,
                plot_bounds=False,
                plot_region=False,
                plot_geoms=True,
                bmap="sat",
                zoomlevel=12,
                figsize=(8, 6),
                vmin=0,
                vmax=vmax,
                cmap="Blues",
            )
            if len(fig.axes) > 1:
                fig.axes[-1].set_ylabel("Maximum water depth [m]")
            ax.set_title("Maximum Water Depth")
            fig.savefig(png, dpi=300, bbox_inches="tight")
            plt.close(fig)
            log(f"Wrote HydroMT maximum-depth map: {png}")
            return png
        except Exception as exc:
            log(f"Satellite basemap unavailable ({exc}); using model-coordinate fallback.")

    # Offline fallback from GeoTIFF.
    import rasterio
    with rasterio.open(tif) as ds:
        arr = ds.read(1).astype(float)
        arr[arr == ds.nodata] = np.nan if ds.nodata is not None else arr[arr == ds.nodata]
        extent = [ds.bounds.left, ds.bounds.right, ds.bounds.bottom, ds.bounds.top]
    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(arr, extent=extent, origin="upper", vmin=0, vmax=vmax, cmap="Blues")
    fig.colorbar(im, ax=ax, label="Maximum water depth [m]")
    ax.set_title("Maximum Water Depth")
    fig.savefig(png, dpi=300, bbox_inches="tight")
    plt.close(fig)
    log(f"Wrote maximum-depth map: {png}")
    return png


def _model_max_zs_grid(run: Path) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    ds = xr.open_dataset(run / "sfincs_map.nc")
    try:
        if "zsmax" in ds:
            zs = ds["zsmax"]
            if "timemax" in zs.dims:
                zs = zs.max("timemax")
        elif "zs" in ds:
            zs = ds["zs"]
            if "time" in zs.dims:
                zs = zs.max("time")
        elif "hmax" in ds and "zb" in ds:
            h = ds["hmax"]
            if "timemax" in h.dims:
                h = h.max("timemax")
            zs = h + ds["zb"]
        else:
            raise RuntimeError("Cannot derive maximum water level from sfincs_map.nc")
        z = np.asarray(zs.values, dtype=float)
        if z.ndim > 2:
            z = np.squeeze(z)
        x = np.asarray(ds["x"].values, dtype=float)
        y = np.asarray(ds["y"].values, dtype=float)
        if x.ndim == 1 and y.ndim == 1:
            x, y = np.meshgrid(x, y)
        msk = np.asarray(ds["msk"].values if "msk" in ds else np.ones_like(z), dtype=float)
        if msk.ndim > 2:
            msk = np.squeeze(msk)
        epsg = None
        if "crs" in ds:
            try:
                epsg = int(np.asarray(ds["crs"].values).reshape(-1)[0])
            except Exception:
                pass
        if epsg is None:
            for key in ("epsg", "crs"):
                if key in ds.attrs:
                    try:
                        epsg = int(ds.attrs[key])
                        break
                    except Exception:
                        pass
        if epsg is None:
            # sfincs.inp normally has epsg; parse it as final fallback.
            inp = run / "sfincs.inp"
            if inp.exists():
                for line in inp.read_text(errors="ignore").splitlines():
                    if line.strip().lower().startswith("epsg"):
                        try:
                            epsg = int(line.split("=", 1)[-1].strip().split()[0])
                            break
                        except Exception:
                            pass
        if epsg is None:
            raise RuntimeError("Could not determine model EPSG for HWM comparison")
        return z, x, y, msk, int(epsg)
    finally:
        ds.close()


def _find_col(gdf: gpd.GeoDataFrame, names: List[str]) -> Optional[str]:
    lookup = {str(c).lower(): c for c in gdf.columns}
    for n in names:
        if n.lower() in lookup:
            return lookup[n.lower()]
    return None


def compare_high_water_marks(cfg: Dict[str, Any], run: Path, out_dir: Path) -> pd.DataFrame:
    """Evaluate prepared USGS/user HWMs. Datum mismatch never produces fake errors."""
    hcfg = cfg.get("validation", {}).get("hwm", {}) or {}
    if not bool(hcfg.get("enabled", True)):
        return pd.DataFrame()

    candidates = [
        run / "agent_validation" / "usgs_high_water_marks.geojson",
        run / "agent_validation" / "high_water_marks.geojson",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        log("HWM evaluation skipped: no prepared HWM file.")
        return pd.DataFrame()

    gdf = gpd.read_file(path)
    if gdf.empty:
        return pd.DataFrame()
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)

    elev_col = _find_col(
        gdf,
        ["elev_ft", "elevation_ft", "hwm_elev_ft", "elev_m", "elevation_m", "elevation"],
    )
    if elev_col is None:
        raise RuntimeError(f"HWM file has no recognizable elevation field: {list(gdf.columns)}")
    datum_col = _find_col(gdf, ["verticalDatumName", "vertical_datum_name", "vertical_datum", "vdatum", "vdatum_name"])
    quality_col = _find_col(gdf, ["hwmQualityName", "hwm_quality_name", "quality", "quality_name"])
    id_col = _find_col(gdf, ["hwm_id", "id", "hwm_label"])

    raw = pd.to_numeric(gdf[elev_col], errors="coerce")
    hwm_m = raw * 0.3048 if "ft" in elev_col.lower() else raw

    zs, x, y, msk, epsg = _model_max_zs_grid(run)
    gm = gdf.to_crs(f"EPSG:{epsg}").reset_index(drop=True)
    gw = gdf.to_crs(4326).reset_index(drop=True)
    active = (msk > 0) & np.isfinite(zs) & np.isfinite(x) & np.isfinite(y)

    target = target_vertical_datum(cfg)
    offset = hcfg.get("vertical_datum_offset_m")
    policy = str(hcfg.get("datum_policy", "require_match")).lower()
    dx = float(cfg.get("model", {}).get("dx_m", 200))
    max_dist = float(hcfg.get("max_snap_distance_m", max(3 * dx, 1000.0)))

    rows: List[Dict[str, Any]] = []
    for i, row in gm.iterrows():
        if not np.isfinite(hwm_m.iloc[i]):
            continue
        gx, gy = float(row.geometry.x), float(row.geometry.y)
        d2 = np.where(active, (x - gx) ** 2 + (y - gy) ** 2, np.nan)
        if not np.isfinite(d2).any():
            continue
        ij = np.unravel_index(np.nanargmin(d2), d2.shape)
        dist = float(np.sqrt(d2[ij]))
        obs_datum = str(gdf.iloc[i][datum_col]) if datum_col else ""
        datum_match = bool(obs_datum and target and obs_datum.strip().upper() == target.strip().upper())

        metric_ok = dist <= max_dist
        adjusted_hwm = float(hwm_m.iloc[i])
        datum_note = "matching datum"
        if not datum_match:
            if offset is not None:
                adjusted_hwm += float(offset)
                metric_ok = metric_ok and True
                datum_note = f"applied configured offset {float(offset):+.3f} m"
            elif policy == "ignore":
                datum_note = "datum mismatch ignored by configuration"
            else:
                metric_ok = False
                datum_note = f"datum mismatch: HWM={obs_datum or 'unknown'}, model={target}"

        model_z = float(zs[ij])
        rec = {
            "hwm_id": str(gdf.iloc[i][id_col]) if id_col else str(i + 1),
            "quality": str(gdf.iloc[i][quality_col]) if quality_col else "",
            "longitude": float(gw.geometry.iloc[i].x),
            "latitude": float(gw.geometry.iloc[i].y),
            "hwm_elevation_m": adjusted_hwm,
            "hwm_datum": obs_datum,
            "model_datum": target,
            "model_max_zs_m": model_z,
            "snap_distance_m": dist,
            "metric_eligible": bool(metric_ok),
            "datum_note": datum_note,
            "error_m": float(model_z - adjusted_hwm) if metric_ok else np.nan,
        }
        rows.append(rec)

    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "sfincs_hwm_validation.csv", index=False)
    if not df.empty:
        eligible = df[df["metric_eligible"] & np.isfinite(df["error_m"])]
        summary = {
            "n_total": int(len(df)),
            "n_metric_eligible": int(len(eligible)),
            "rmse_m": float(np.sqrt(np.mean(eligible["error_m"] ** 2))) if len(eligible) else None,
            "mae_m": float(np.mean(np.abs(eligible["error_m"]))) if len(eligible) else None,
            "bias_m": float(np.mean(eligible["error_m"])) if len(eligible) else None,
        }
        (out_dir / "sfincs_hwm_validation_summary.json").write_text(json.dumps(summary, indent=2))
        log(f"HWM validation: {summary}")
    return df


def make_observation_location_map(cfg: Dict[str, Any], run: Path, out_dir: Path) -> Optional[Path]:
    stations = run / "agent_validation" / "water_level" / "stations.geojson"
    if not stations.exists():
        return None
    try:
        sf = _open_sfincs_minimal(run)
        g = gpd.read_file(stations)
        if g.crs is None:
            g = g.set_crs(4326)
        # sf.plot_basemap already shows model geoms; overlay observations explicitly.
        fig, ax = sf.plot_basemap(
            plot_bounds=False,
            plot_region=False,
            plot_geoms=True,
            bmap="sat",
            zoomlevel=10,
            figsize=(8, 6),
        )
        epsg = None
        try:
            epsg = sf.crs
        except Exception:
            pass
        if epsg is not None:
            gg = g.to_crs(epsg)
            gg.plot(ax=ax, marker="o", edgecolor="black", facecolor="none", markersize=40, label="Validation gauges")
            ax.legend()
        png = out_dir / "water_level_gauge_locations_hydromt.png"
        fig.savefig(png, dpi=300, bbox_inches="tight")
        plt.close(fig)
        return png
    except Exception as exc:
        log(f"Gauge location map skipped: {exc}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate a completed AutoCF/SFINCS run.")
    ap.add_argument("--config", default="sfincs_agent_config.yml")
    ap.add_argument("--skip-hmax", action="store_true")
    ap.add_argument("--skip-water-level", action="store_true")
    ap.add_argument("--skip-hwm", action="store_true")
    ap.add_argument("--hmin", type=float, default=0.05)
    ap.add_argument("--hmax-vmax", type=float, default=3.0)
    ap.add_argument("--no-hydromt-basemap", action="store_true")
    args = ap.parse_args()

    cfg = read_config(Path(args.config))
    run = resolved_output_dir(cfg)
    out = run / "agent_plots"
    out.mkdir(parents=True, exist_ok=True)
    (run / "agent_reports").mkdir(parents=True, exist_ok=True)

    if not (run / "sfincs_map.nc").exists():
        raise RuntimeError(f"Missing completed SFINCS map output: {run / 'sfincs_map.nc'}")

    log("=" * 80)
    log("AUTOCF / SFINCS PRODUCTION EVALUATION")
    log("Flood-extent remote sensing: EXCLUDED")
    log(f"Run: {run}")
    log("=" * 80)

    if not args.skip_hmax:
        make_hmax_map(
            cfg, run, out,
            hmin=args.hmin,
            vmax=args.hmax_vmax,
            use_satellite_basemap=not args.no_hydromt_basemap,
        )

    if not args.skip_water_level:
        if not (run / "sfincs_his.nc").exists():
            log("Water-level validation skipped: sfincs_his.nc missing.")
        else:
            df = compare_model_to_water_levels(cfg, run, out)
            log(f"Water-level validation stations scored: {len(df)}")
            make_observation_location_map(cfg, run, out)

    if not args.skip_hwm:
        dfh = compare_high_water_marks(cfg, run, out)
        log(f"HWM records processed: {len(dfh)}")

    report = {
        "run": str(run),
        "remote_sensing_extent_validation": False,
        "included_validation": ["water_level_gauges", "high_water_marks"],
        "outputs": str(out),
    }
    (run / "agent_reports" / "evaluation_scope.json").write_text(json.dumps(report, indent=2))
    log("Evaluation complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
