#!/usr/bin/env python3
"""Create lightweight, time-aware map overlays from prepared SFINCS forcing files."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import xarray as xr
from matplotlib import colormaps
from PIL import Image, ImageDraw
from pyproj import CRS, Transformer
from shapely import contains_xy

from sfincs_config import read_config
from run_layout import run_layout


PALETTES = {
    "rainfall": {
        "cmap": "viridis",
        "colors": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    },
    "wind": {
        "cmap": "plasma",
        "colors": ["#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"],
    },
    "pressure": {
        "cmap": "coolwarm",
        "colors": ["#3b4cc0", "#8db0fe", "#dddddd", "#f49a7b", "#b40426"],
    },
}


def log(message: str) -> None:
    print(f"[forcing-preview {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _grid_crs(ds: xr.Dataset) -> CRS:
    if "spatial_ref" in ds:
        attrs = ds["spatial_ref"].attrs
        value = attrs.get("crs_wkt") or attrs.get("spatial_ref")
        if value:
            return CRS.from_user_input(value)
    for value in (ds.attrs.get("crs"), ds.attrs.get("epsg")):
        if value:
            return CRS.from_user_input(value)
    if any(name in ds.coords for name in ("lon", "longitude")) and any(
        name in ds.coords for name in ("lat", "latitude")):
        return CRS.from_epsg(4326)
    raise RuntimeError("Forcing NetCDF does not contain coordinate-reference metadata.")


def _grid_axes(ds: xr.Dataset) -> tuple[np.ndarray, np.ndarray]:
    """Return horizontal axes from built SFINCS or pre-build provider files."""
    if "x" in ds.coords and "y" in ds.coords:
        return np.asarray(ds["x"].values), np.asarray(ds["y"].values)
    lon_name = "lon" if "lon" in ds.coords else "longitude" if "longitude" in ds.coords else None
    lat_name = "lat" if "lat" in ds.coords else "latitude" if "latitude" in ds.coords else None
    if lon_name is None or lat_name is None:
        raise RuntimeError(f"Forcing grid has no recognizable horizontal coordinates: {list(ds.coords)}")
    x, y = np.asarray(ds[lon_name].values), np.asarray(ds[lat_name].values)
    if x.ndim != 1 or y.ndim != 1:
        raise RuntimeError("Forcing preview requires one-dimensional longitude/latitude coordinates")
    return x, y


def _domain_mask(cfg: dict[str, Any], x: np.ndarray, y: np.ndarray, crs: CRS) -> np.ndarray:
    gdf = gpd.read_file(cfg["geometry"]["path"])
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    if "role" in gdf.columns:
        selected = gdf[gdf["role"].astype(str).str.lower().eq("domain")]
        if not selected.empty:
            gdf = selected
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    if gdf.empty:
        raise RuntimeError("The study-area file has no domain polygon.")
    projected = gdf.to_crs(crs).geometry
    geom = projected.union_all() if hasattr(projected, "union_all") else projected.unary_union
    xx, yy = np.meshgrid(x, y)
    return np.asarray(contains_xy(geom, xx, yy), dtype=bool)


def _bounds_wgs84(x: np.ndarray, y: np.ndarray, crs: CRS) -> tuple[float, float, float, float]:
    dx = abs(float(np.nanmedian(np.diff(x)))) if len(x) > 1 else 1.0
    dy = abs(float(np.nanmedian(np.diff(y)))) if len(y) > 1 else 1.0
    xmin, xmax = float(np.nanmin(x) - dx / 2), float(np.nanmax(x) + dx / 2)
    ymin, ymax = float(np.nanmin(y) - dy / 2), float(np.nanmax(y) + dy / 2)
    transformer = Transformer.from_crs(crs, 4326, always_xy=True)
    corners = [transformer.transform(px, py) for px, py in (
        (xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax))]
    lon = [p[0] for p in corners]
    lat = [p[1] for p in corners]
    return min(lon), min(lat), max(lon), max(lat)


def _orient(array: np.ndarray, mask: np.ndarray, x: np.ndarray, y: np.ndarray):
    out, mout = np.asarray(array, dtype=float), np.asarray(mask, dtype=bool)
    xo, yo = np.asarray(x), np.asarray(y)
    if len(xo) > 1 and xo[0] > xo[-1]:
        out, mout, xo = out[:, ::-1], mout[:, ::-1], xo[::-1]
    # PNG row zero must be north/top.
    if len(yo) > 1 and yo[0] < yo[-1]:
        out, mout, yo = out[::-1, :], mout[::-1, :], yo[::-1]
    return out, mout, xo, yo


def _limits(values: np.ndarray, key: str) -> tuple[float, float]:
    valid = values[np.isfinite(values)]
    if not valid.size:
        return 0.0, 1.0
    if key in {"rainfall", "wind"}:
        low = 0.0
        high = float(np.nanpercentile(valid, 98))
    else:
        low, high = (float(v) for v in np.nanpercentile(valid, [2, 98]))
    if not math.isfinite(high) or high <= low:
        high = low + max(1.0, abs(low) * 0.01)
    return low, high


def _rgba(values: np.ndarray, mask: np.ndarray, key: str, low: float, high: float) -> np.ndarray:
    normalized = np.clip((values - low) / (high - low), 0.0, 1.0)
    rgba = (colormaps[PALETTES[key]["cmap"]](normalized) * 255).astype(np.uint8)
    visible = mask & np.isfinite(values)
    if key == "rainfall":
        visible &= values > 1e-10
    rgba[..., 3] = np.where(visible, 195, 0).astype(np.uint8)
    return rgba


def _wind_arrows(image: Image.Image, u: np.ndarray, v: np.ndarray, mask: np.ndarray, vmax: float) -> None:
    draw = ImageDraw.Draw(image)
    ny, nx = u.shape
    step = max(5, int(max(nx, ny) / 16))
    scale = max(vmax, 1e-6)
    for row in range(step // 2, ny, step):
        for col in range(step // 2, nx, step):
            if not mask[row, col] or not np.isfinite(u[row, col]) or not np.isfinite(v[row, col]):
                continue
            speed = math.hypot(float(u[row, col]), float(v[row, col]))
            if speed <= 1e-9:
                continue
            length = 3.0 + 7.0 * min(1.0, speed / scale)
            dx, dy = length * float(u[row, col]) / speed, -length * float(v[row, col]) / speed
            x0, y0 = float(col), float(row)
            x1, y1 = x0 + dx, y0 + dy
            angle = math.atan2(dy, dx)
            head = 3.0
            wings = [
                (x1 - head * math.cos(angle - 0.65), y1 - head * math.sin(angle - 0.65)),
                (x1 - head * math.cos(angle + 0.65), y1 - head * math.sin(angle + 0.65)),
            ]
            for width, color in ((3, (0, 0, 0, 210)), (1, (255, 255, 255, 245))):
                draw.line((x0, y0, x1, y1), fill=color, width=width)
                draw.line((x1, y1, *wings[0]), fill=color, width=width)
                draw.line((x1, y1, *wings[1]), fill=color, width=width)


def _time_label(value: Any, index: int) -> str:
    try:
        return np.datetime_as_string(np.datetime64(value), unit="m").replace("T", " ") + " UTC"
    except Exception:
        return f"Time step {index + 1}"


def _write_layer(
    *, key: str, label: str, units: str, source: Path, ds: xr.Dataset,
    values: xr.DataArray, mask: np.ndarray, x: np.ndarray, y: np.ndarray,
    outdir: Path, u_values: xr.DataArray | None = None, v_values: xr.DataArray | None = None,
) -> dict[str, Any]:
    layer_dir = outdir / key
    layer_dir.mkdir(parents=True, exist_ok=True)
    times = values.coords.get("time")
    count = int(values.sizes.get("time", 1))
    frames = []
    for index in range(count):
        raw = values.isel(time=index).load().values if "time" in values.dims else values.load().values
        arr, oriented_mask, _, _ = _orient(raw, mask, x, y)
        inside = arr[oriented_mask & np.isfinite(arr)]
        low, high = _limits(inside, key)
        rgba = _rgba(arr, oriented_mask, key, low, high)
        image = Image.fromarray(rgba, mode="RGBA")
        if key == "wind" and u_values is not None and v_values is not None:
            uu = u_values.isel(time=index).load().values
            vv = v_values.isel(time=index).load().values
            uu, _, _, _ = _orient(uu, mask, x, y)
            vv, _, _, _ = _orient(vv, mask, x, y)
            _wind_arrows(image, uu, vv, oriented_mask, high)
        filename = f"{index:04d}.png"
        image.save(layer_dir / filename, optimize=True)
        time_value = times.values[index] if times is not None else index
        frames.append({
            "png": f"{key}/{filename}", "time": _time_label(time_value, index),
            "min": low, "max": high,
            "mean": float(np.nanmean(inside)) if inside.size else None,
        })
        if index == 0 or (index + 1) % 25 == 0 or index + 1 == count:
            log(f"{label}: {index + 1}/{count}")
    return {
        "key": key, "label": label, "units": units, "source": str(source),
        "palette": PALETTES[key]["colors"], "frames": frames,
    }


def create_previews(cfg: dict[str, Any]) -> Path:
    layout = run_layout(cfg)
    run = layout.model
    outdir = layout.forcing_previews
    outdir.mkdir(parents=True, exist_ok=True)
    validation_path = layout.result_path("simulation", "atmosphere_forcing_validation.json")
    validation: dict[str, Any] = {}
    if validation_path.is_file():
        try:
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"could not read atmosphere validation report: {exc}")

    def prepared(component: str, built_name: str) -> Path:
        built = run / built_name
        value = ((validation.get("details") or {}).get(component) or {}).get("path")
        source = Path(str(value)) if value else None
        # A newly built SFINCS file is the most faithful preview. Otherwise use
        # the provider-standardized file created by Atmosphere preparation.
        if built.is_file() and (not validation_path.is_file() or built.stat().st_mtime >= validation_path.stat().st_mtime):
            return built
        if source is not None and source.is_file():
            return source
        return built

    definitions = [
        ("rainfall", "Rainfall rate", "mm h⁻¹", prepared("precipitation", "precip_2d.nc")),
        ("wind", "Wind speed and direction", "m s⁻¹", prepared("wind", "wind_2d.nc")),
        ("pressure", "Barometric pressure", "hPa", prepared("pressure", "press_2d.nc")),
    ]
    layers = []
    bounds = None
    for key, label, units, source in definitions:
        if not source.is_file():
            log(f"skipped {label}: {source.name} is not prepared")
            continue
        ds = xr.open_dataset(source)
        try:
            x, y = _grid_axes(ds)
            crs = _grid_crs(ds)
            mask = _domain_mask(cfg, x, y, crs)
            layer_bounds = _bounds_wgs84(x, y, crs)
            bounds = bounds or layer_bounds
            if key == "rainfall":
                var = next(name for name in ("Precipitation", "precip", "rainfall") if name in ds)
                values = ds[var]
                layer = _write_layer(key=key, label=label, units=units, source=source,
                                     ds=ds, values=values, mask=mask, x=x, y=y, outdir=outdir)
            elif key == "wind":
                uname = next(name for name in ("eastward_wind", "wind_u", "u10") if name in ds)
                vname = next(name for name in ("northward_wind", "wind_v", "v10") if name in ds)
                u, v = ds[uname], ds[vname]
                values = np.hypot(u, v).rename("wind_speed")
                layer = _write_layer(key=key, label=label, units=units, source=source,
                                     ds=ds, values=values, mask=mask, x=x, y=y, outdir=outdir,
                                     u_values=u, v_values=v)
            else:
                var = next(name for name in ("barometric_pressure", "press", "pressure") if name in ds)
                values = ds[var]
                median = float(values.isel(time=0).median(skipna=True)) if "time" in values.dims else float(values.median(skipna=True))
                if median > 2000:
                    values = values / 100.0
                layer = _write_layer(key=key, label=label, units=units, source=source,
                                     ds=ds, values=values, mask=mask, x=x, y=y, outdir=outdir)
            layer["west"], layer["south"], layer["east"], layer["north"] = layer_bounds
            layers.append(layer)
        finally:
            ds.close()
    if not layers or bounds is None:
        raise RuntimeError(f"No prepared forcing NetCDF files were found in {run}")
    west, south, east, north = bounds
    manifest = {
        "version": 1, "run": str(run), "created": datetime.now().isoformat(timespec="seconds"),
        "west": west, "south": south, "east": east, "north": north,
        "outside_aoi": "transparent", "validation": validation, "layers": layers,
    }
    path = outdir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"wrote {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create map previews from prepared forcing NetCDF files.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    print(create_previews(read_config(Path(args.config))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
