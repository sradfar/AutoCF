"""Run the validated AutoCF core inside the bundled Windows/WSL runtime."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


ESA_WORLDCOVER_TILE_URL_TEMPLATE = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
ESA_WORLDCOVER_SOURCES = {"esa_worldcover_2021_v200", "esa_worldcover"}

WORKFLOW_MODES = {
    "build": "build_only",
    # A desktop user should not have to know whether a prepared surface layer
    # invalidated the HydroMT model.  build_run_direct is resumable: it skips a
    # valid existing build, but rebuilds any stale/missing stages before SFINCS.
    "run": "build_run_direct",
    "full": "build_run_direct",
    "terrain": "topobathy_only",
    "surface": "surface_only",
    "atmosphere": "atmosphere_only",
}

# Longest edge of the browser preview written beside a prepared elevation raster.
PREVIEW_MAX_PIXELS = 1400
PREVIEW_STEM = "topobathy_preview"
PREPARED_RASTER_PIXEL_LIMIT = 150_000_000
STANDARD_MESH_CELL_SIZES_M = (30.0, 50.0, 100.0, 200.0, 500.0, 1000.0)


def _organized_layout(cfg: dict[str, Any]) -> bool:
    try:
        return int((cfg.get("project", {}) or {}).get("layout_version", 1)) >= 2
    except (TypeError, ValueError):
        return False


def _run_paths(cfg: dict[str, Any]) -> dict[str, Path]:
    root = Path(str((cfg.get("project", {}) or {}).get("output_dir", "./runs"))).expanduser().resolve()
    if not _organized_layout(cfg):
        return {
            "root": root, "model": root, "terrain": root / "agent_data",
            "surface": root / "agent_data", "vectors": root / "agent_gis",
            "state": root / "agent_state.json", "previews": root / "agent_data",
        }
    return {
        "root": root, "model": root / "data" / "model" / "main",
        "terrain": root / "data" / "terrain" / "rasters",
        "surface": root / "data" / "surface" / "rasters",
        "vectors": root / "data" / "geospatial" / "vectors",
        "state": root / "metadata" / "run_state.json",
        "previews": root / "data" / "previews",
    }


def _windows_path_to_wsl(value: str) -> str:
    """Translate an absolute drive path when this launcher is running in WSL."""
    if os.name == "nt":
        return value
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", value.strip())
    if not match:
        return value
    tail = match.group(2).replace("\\", "/")
    return f"/mnt/{match.group(1).lower()}/{tail}"


def _normalize_windows_paths(value: Any) -> Any:
    """Recursively normalize Windows absolute paths embedded anywhere in YAML."""
    if isinstance(value, dict):
        return {key: _normalize_windows_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_windows_paths(item) for item in value]
    if isinstance(value, str):
        return _windows_path_to_wsl(value)
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge configuration mappings without changing either input."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_config_file_paths(cfg: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Resolve portable YAML paths against the directory containing that YAML."""
    path_fields = (
        ("project", "output_dir"), ("project", "cache_dir"),
        ("geojson", "path"),
        ("topobathy", "primary", "path"), ("topobathy", "gapfill", "path"),
        ("landcover", "path"), ("infiltration", "path"),
        ("infiltration", "cn_lookup", "path"), ("infiltration", "soils", "path"),
        ("forcing", "precipitation", "path"), ("forcing", "wind", "path"),
        ("forcing", "pressure", "path"),
        ("forcing", "ocean_boundary", "timeseries"),
        ("forcing", "ocean_boundary", "locations"),
        ("forcing", "river", "timeseries"), ("forcing", "river", "locations"),
        ("evaluation", "observations", "timeseries"),
        ("evaluation", "observations", "locations"),
    )
    for parts in path_fields:
        node: Any = cfg
        for key in parts[:-1]:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if not isinstance(node, dict):
            continue
        key = parts[-1]
        value = node.get(key)
        if value in (None, "") or "://" in str(value):
            continue
        candidate = Path(_windows_path_to_wsl(str(value))).expanduser()
        if not candidate.is_absolute():
            candidate = config_dir / candidate
        node[key] = str(candidate.resolve())
    for section in (cfg.get("topobathy", {}) or {}).get("additional_sources", []) or []:
        value = section.get("path")
        if value and "://" not in str(value):
            candidate = Path(_windows_path_to_wsl(str(value))).expanduser()
            section["path"] = str((candidate if candidate.is_absolute() else config_dir / candidate).resolve())
    return cfg


def _resolved_yaml(path: Path, stack: list[Path] | None = None) -> dict[str, Any]:
    """Read the same ``extends`` and output-suffix rules as the AutoCF core."""
    path = path.expanduser().resolve()
    chain = list(stack or [])
    if path in chain:
        raise RuntimeError("Circular YAML inheritance: " + " -> ".join(map(str, chain + [path])))
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Top-level YAML object must be a mapping: {path}")
    raw = _normalize_windows_paths(raw)
    parent = raw.pop("extends", None)
    if parent:
        parent_path = Path(_windows_path_to_wsl(str(parent))).expanduser()
        if not parent_path.is_absolute():
            parent_path = path.parent / parent_path
        raw = _deep_merge(_resolved_yaml(parent_path, chain + [path]), raw)
    raw = _resolve_config_file_paths(raw, path.parent)
    project = raw.get("project", {})
    if os.name != "nt":
        for key in ("output_dir", "cache_dir"):
            if project.get(key):
                project[key] = str(project[key]).replace("\\", "/")
    suffix = project.get("output_dir_suffix")
    if suffix and project.get("output_dir"):
        output = Path(str(project["output_dir"])).expanduser()
        if not output.name.endswith(str(suffix)):
            project["output_dir"] = str(output.with_name(output.name + str(suffix)))
        raw["project"] = project
    return raw


def _coordinate_pairs(value: Any):
    if (
        isinstance(value, (list, tuple)) and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        yield float(value[0]), float(value[1])
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _coordinate_pairs(item)


def _domain_bbox(cfg: dict[str, Any]) -> list[float]:
    geojson = cfg.get("geojson", {}) if isinstance(cfg.get("geojson"), dict) else {}
    geometry = cfg.get("geometry", {}) if isinstance(cfg.get("geometry"), dict) else {}
    value = geojson.get("path") or geometry.get("path")
    if not value:
        raise RuntimeError("Cannot apply raster memory preflight without a study-area path.")
    path = Path(str(value)).expanduser().resolve()
    document = json.loads(path.read_text(encoding="utf-8"))
    features = document.get("features", []) if isinstance(document, dict) else []
    polygon_features = [
        feature for feature in features
        if isinstance(feature, dict)
        and isinstance(feature.get("geometry"), dict)
        and str(feature["geometry"].get("type", "")).lower() in {"polygon", "multipolygon"}
    ]
    domains = [
        feature for feature in polygon_features
        if str((feature.get("properties") or {}).get("role", "")).lower() == "domain"
    ]
    selected = domains or polygon_features
    points = [
        pair for feature in selected
        for pair in _coordinate_pairs(feature["geometry"].get("coordinates", []))
        if math.isfinite(pair[0]) and math.isfinite(pair[1])
    ]
    if not points:
        raise RuntimeError(f"No domain polygon coordinates were found in {path}")
    lon = [pair[0] for pair in points]
    lat = [pair[1] for pair in points]
    return [min(lon), min(lat), max(lon), max(lat)]


def _buffer_bbox_km(bbox: list[float], buffer_km: float) -> list[float]:
    minlon, minlat, maxlon, maxlat = map(float, bbox)
    mid_lat = 0.5 * (minlat + maxlat)
    dlat = float(buffer_km) / 111.0
    dlon = float(buffer_km) / max(20.0, 111.0 * math.cos(math.radians(mid_lat)))
    return [minlon - dlon, minlat - dlat, maxlon + dlon, maxlat + dlat]


def _projected_crs(cfg: dict[str, Any], bbox: list[float]) -> str:
    mesh = cfg.get("mesh", {}) if isinstance(cfg.get("mesh"), dict) else {}
    value = str(mesh.get("crs", "utm")).strip()
    if value.lower() != "utm":
        return value
    longitude = 0.5 * (bbox[0] + bbox[2])
    latitude = 0.5 * (bbox[1] + bbox[3])
    zone = max(1, min(60, int(math.floor((longitude + 180.0) / 6.0)) + 1))
    return f"EPSG:{32600 + zone if latitude >= 0 else 32700 + zone}"


def _projected_raster_plan(
    bbox: list[float], target_crs: str, resolution_m: float,
) -> dict[str, Any]:
    from pyproj import Transformer

    resolution = float(resolution_m)
    if resolution <= 0:
        raise RuntimeError("Raster resolution must be greater than zero.")
    transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
    minx, miny, maxx, maxy = transformer.transform_bounds(*bbox, densify_pts=21)
    width = max(0.0, float(maxx - minx))
    height = max(0.0, float(maxy - miny))
    columns = max(1, int(math.ceil(width / resolution)))
    rows = max(1, int(math.ceil(height / resolution)))
    return {
        "resolution_m": resolution, "columns": columns, "rows": rows,
        "pixels": columns * rows, "width_m": width, "height_m": height,
    }


def _next_standard_mesh_size(required_m: float) -> float:
    for value in STANDARD_MESH_CELL_SIZES_M:
        if value >= required_m:
            return value
    return math.ceil(float(required_m) / 100.0) * 100.0


def _apply_global_raster_preflight(cfg: dict[str, Any], mode: str) -> None:
    """Report large raster workloads without changing the user's model settings."""
    if mode not in {"terrain", "build", "run", "full"}:
        return
    mesh = cfg.setdefault("mesh", {})
    subgrid = cfg.setdefault("subgrid", {})
    model = cfg.setdefault("model", {})
    topobathy = cfg.setdefault("topobathy", {})
    primary = topobathy.setdefault("primary", {})
    model_mode = str(model.get("mode", "regular")).strip().lower()
    original_dx = float(mesh.get("dx_m", 200.0))
    original_pixels = max(1, int(subgrid.get("nr_subgrid_pixels", 20)))
    source_resolution = float(primary.get("output_resolution_m", 3.0))
    bbox = _buffer_bbox_km(_domain_bbox(cfg), float(topobathy.get("buffer_km", 10.0)))
    target_crs = _projected_crs(cfg, bbox)
    def plans(dx: float, samples: int) -> tuple[dict[str, Any], dict[str, Any]]:
        useful = dx / samples if model_mode == "subgrid" else dx
        sampling = _projected_raster_plan(bbox, target_crs, useful)
        prepared = _projected_raster_plan(bbox, target_crs, max(source_resolution, useful))
        return sampling, prepared

    original_sampling_plan, original_plan = plans(original_dx, original_pixels)
    runtime = cfg.setdefault("runtime", {})
    runtime["automatic_raster_memory_adjustment"] = {
        "policy": "report_only",
        "pixel_limit": PREPARED_RASTER_PIXEL_LIMIT,
        "adjusted": False,
        "model_mode": model_mode,
        "target_crs": target_crs,
        "original_mesh_dx_m": original_dx,
        "effective_mesh_dx_m": original_dx,
        "original_nr_subgrid_pixels": original_pixels,
        "effective_nr_subgrid_pixels": original_pixels,
        "source_resolution_m": source_resolution,
        "original_sampling_plan": original_sampling_plan,
        "effective_sampling_plan": original_sampling_plan,
        "original_plan": original_plan,
        "effective_plan": original_plan,
    }
    if original_sampling_plan["pixels"] > PREPARED_RASTER_PIXEL_LIMIT:
        print(
            "AutoCF large-workload warning: "
            f"{original_sampling_plan['pixels']:,} sampling pixels exceed the "
            f"{PREPARED_RASTER_PIXEL_LIMIT:,}-pixel portable budget; "
            f"mesh {original_dx:g} m and subgrid samples {original_pixels} are preserved exactly. "
            "AutoCF will not change the model settings automatically.",
            flush=True,
        )


def create_runtime_overlay(config: Path, mode: str) -> Path:
    """Write the exact temporary YAML passed to the Linux AutoCF core."""
    override = _resolved_yaml(config)

    _apply_global_raster_preflight(override, mode)
    override.setdefault("workflow", {})["mode"] = WORKFLOW_MODES[mode]
    override.setdefault("sfincs", {})["executable"] = "/usr/local/bin/sfincs-cpu"

    # Desktop YAML keeps only user-facing ocean-boundary choices. Restore the
    # internal NOAA defaults before handing the configuration to the hydraulic
    # core, which also keeps projects saved by earlier desktop builds usable.
    forcing = override.get("forcing")
    if isinstance(forcing, dict):
        ocean = forcing.get("ocean_boundary")
        if isinstance(ocean, dict):
            ocean_source = str(ocean.get("source", "noaa_coops")).strip().lower()
            if ocean_source in {"noaa", "noaa_coops", "coops"}:
                ocean.setdefault("product", "water_level")
                ocean.setdefault("units", "metric")
                ocean.setdefault("time_zone", "gmt")
                ocean.setdefault("interval_minutes", 6)
                ocean.setdefault("max_station_distance_km", 150)
                ocean.setdefault("max_missing_fraction", 0.10)

        # Edition 1 builds created before the defaults fix could save every
        # untouched numeric control at its Qt minimum. Detect that complete
        # placeholder fingerprint and migrate it only in the temporary runtime
        # overlay. A deliberate individual zero or small distance is preserved.
        river = forcing.get("river")
        sfincs = override.get("sfincs")
        subgrid = override.get("subgrid")
        if all(isinstance(item, dict) for item in (ocean, river, sfincs, subgrid)):
            try:
                legacy_placeholder_profile = (
                    float(ocean.get("max_station_distance_km")) <= 1.0
                    and float(river.get("max_gage_distance_km")) <= 1.0
                    and int(river.get("max_gages")) <= 0
                    and float(sfincs.get("dtout_seconds")) <= 0.0
                    and float(sfincs.get("dthisout_seconds")) <= 0.0
                    and float(sfincs.get("dtmaxout_seconds")) <= 0.0
                    and int(subgrid.get("nlevels")) <= 2
                    and int(subgrid.get("nr_subgrid_pixels")) <= 2
                )
            except (TypeError, ValueError):
                legacy_placeholder_profile = False
            if legacy_placeholder_profile:
                ocean["max_station_distance_km"] = 150.0
                river["max_gage_distance_km"] = 50.0
                river["max_gages"] = 12
                sfincs["dtout_seconds"] = 3600.0
                sfincs["dthisout_seconds"] = 360.0
                sfincs["dtmaxout_seconds"] = 3600.0
                subgrid["nlevels"] = 10
                subgrid["nr_subgrid_pixels"] = 20
                print(
                    "AutoCF compatibility repair: replaced uninitialized desktop "
                    "numeric placeholders with standard runtime defaults.",
                    flush=True,
                )

    landcover = override.setdefault("landcover", {})
    landcover_source = str(landcover.get("source", "ESA_WorldCover_2021_v200")).strip().lower()
    if landcover_source in ESA_WORLDCOVER_SOURCES:
        landcover.setdefault("tile_url_template", ESA_WORLDCOVER_TILE_URL_TEMPLATE)

    handle, temporary_name = tempfile.mkstemp(
        prefix=".autocf_windows_", suffix=".yml", dir=config.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump(override, stream, sort_keys=False)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def prepared_additional_topobathy(cfg: dict[str, Any]) -> list[Path]:
    state_path = _run_paths(cfg)["state"]
    if not state_path.is_file():
        return []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    entries = state.get("meta", {}).get("additional_topobathy", {}).get("sources", [])
    return [Path(entry["path"]) for entry in entries if Path(entry["path"]).is_file()]


def prepared_topobathy(config: Path) -> tuple[Path | None, Path | None, str, Path | None]:
    """Find prepared rasters and the exact model-domain AOI from run state."""
    cfg = _resolved_yaml(config)
    project = cfg.get("project", {})
    output_value = project.get("output_dir")
    if not output_value:
        datum = str(cfg.get("topobathy", {}).get("target_vertical_datum", "model datum"))
        return None, None, datum, None
    paths = _run_paths(cfg)
    output = paths["root"]
    data_dir = paths["terrain"]
    state_path = paths["state"]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except Exception:
        state = {}
    meta = state.get("meta", {}) if isinstance(state, dict) else {}

    topobathy = cfg.get("topobathy", {})
    primary_cfg = topobathy.get("primary", {})
    gap_cfg = topobathy.get("gapfill", {})

    def state_path_for(key: str) -> Path | None:
        value = meta.get(key, {}).get("path") if isinstance(meta.get(key), dict) else None
        candidate = Path(str(value)).expanduser() if value else None
        return candidate if candidate is not None and candidate.is_file() else None

    primary = state_path_for("cudem")
    if primary is None:
        if str(primary_cfg.get("source", "")).strip().lower() in {"file", "nasa_deltax_2021"}:
            primary = data_dir / "user_topobathy_primary.tif"
        else:
            primary = data_dir / "cudem_1_9arcsec_model_crs.tif"
    if not primary.is_file():
        primary = None

    gap_source = str(gap_cfg.get("source", "")).strip().lower()
    if gap_source == "none":
        gapfill = None
    else:
        # When datums differ, preview the harmonized raster that the model
        # actually uses—not the native-datum download recorded under ``gebco``.
        gapfill = state_path_for("topobathy_gapfill_alignment") or state_path_for("gebco")
        if gapfill is None:
            gapfill = (
                data_dir / "user_topobathy_gapfill.tif"
                if gap_source in {"file", "nasa_deltax_2021"}
                else data_dir / "gebco_2024_model_crs.tif"
            )
    if gapfill is not None and not gapfill.is_file():
        gapfill = None

    datum = str(topobathy.get("target_vertical_datum", "model datum")).strip() or "model datum"
    geo_meta = meta.get("geojson", {}) if isinstance(meta.get("geojson"), dict) else {}
    domain_value = geo_meta.get("domain_path")
    domain = Path(str(domain_value)).expanduser() if domain_value else paths["vectors"] / "domain.geojson"
    if not domain.is_file():
        domain = paths["vectors"] / "domain.geojson"
    if not domain.is_file():
        domain = None
    return primary, gapfill, datum, domain


def _to_lonlat_grid(raster: Path, transform: Any, width: int, height: int,
                    resampling: Any = None) -> Any:
    """Reproject one raster onto an existing EPSG:4326 grid, NaN where absent."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    if resampling is None:
        resampling = Resampling.average

    grid = np.full((height, width), np.nan, dtype="float32")
    with rasterio.open(raster) as src:
        reproject(
            source=rasterio.band(src, 1), destination=grid,
            src_transform=src.transform, src_crs=src.crs, src_nodata=src.nodata,
            dst_transform=transform, dst_crs="EPSG:4326", dst_nodata=float("nan"),
            resampling=resampling)
    return grid


def write_topobathy_preview(primary: Path, gapfill: Path | None = None,
                            vertical_datum: str = "model datum",
                            clip_geojson: Path | None = None,
                            output_dir: Path | None = None,
                            additional_sources: list[Path] | None = None) -> dict[str, Any] | None:
    """Render the effective topobathy as a lon/lat PNG plus a colour bar.

    The desktop map can only overlay an axis-aligned image, so the rasters are
    reprojected to EPSG:4326 and colour-mapped here, inside the runtime that
    already has rasterio. Primary elevation wins wherever it has data and the
    gapfill covers the rest, which is the order the build itself uses. Failure
    is never fatal: the preview is cosmetic.
    """
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import calculate_default_transform
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import cm, colors, pyplot as plt
    from PIL import Image

    with rasterio.open(primary) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
    scale = max(width, height) / PREVIEW_MAX_PIXELS
    if scale > 1:
        with rasterio.open(primary) as src:
            transform, width, height = calculate_default_transform(
                src.crs, "EPSG:4326", src.width, src.height, *src.bounds,
                dst_width=max(1, int(width / scale)), dst_height=max(1, int(height / scale)))

    data = _to_lonlat_grid(primary, transform, width, height)
    used = [primary.name] if np.isfinite(data).any() else []
    for source in ([gapfill] if gapfill is not None else []) + (additional_sources or []):
        if not source.is_file():
            continue
        filler = _to_lonlat_grid(source, transform, width, height)
        missing = ~np.isfinite(data) & np.isfinite(filler)
        if missing.any():
            data[missing] = filler[missing]
            used.append(source.name)

    aoi_mask = None
    if clip_geojson is not None and clip_geojson.is_file():
        document = json.loads(clip_geojson.read_text(encoding="utf-8"))
        if document.get("type") == "FeatureCollection":
            geometries = [
                feature.get("geometry") for feature in document.get("features", [])
                if isinstance(feature, dict)
                and isinstance(feature.get("geometry"), dict)
                and feature["geometry"].get("type") in {"Polygon", "MultiPolygon"}
            ]
        elif document.get("type") == "Feature":
            geometry = document.get("geometry")
            geometries = [geometry] if isinstance(geometry, dict) else []
        else:
            geometries = [document] if document.get("type") in {"Polygon", "MultiPolygon"} else []
        if geometries:
            aoi_mask = geometry_mask(
                geometries, out_shape=(height, width), transform=transform,
                invert=True, all_touched=False,
            )
            data[~aoi_mask] = np.nan

    valid = np.isfinite(data)
    if not valid.any():
        return None
    low = float(np.nanpercentile(data[valid], 1))
    high = float(np.nanpercentile(data[valid], 99))
    if high <= low:
        high = low + 1.0
    # Blues below sea level, land tints above, joined exactly at 0 m so the
    # shoreline falls on the colour break instead of mid-ramp.
    water = matplotlib.colormaps["Blues_r"](np.linspace(0.15, 0.92, 256))
    land = matplotlib.colormaps["terrain"](np.linspace(0.30, 0.95, 256))
    cmap = colors.LinearSegmentedColormap.from_list(
        "autocf_topobathy", np.vstack((water, land)))
    norm = colors.TwoSlopeNorm(vmin=min(low, -0.1), vcenter=0.0, vmax=max(high, 0.1))
    mapper = cm.ScalarMappable(norm=norm, cmap=cmap)
    rgba = mapper.to_rgba(data, bytes=True)
    rgba[..., 3] = np.where(valid, 235, 0).astype("uint8")

    output_dir = output_dir or primary.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    out_png = output_dir / f"{PREVIEW_STEM}.png"
    Image.fromarray(rgba, mode="RGBA").save(out_png, optimize=True)

    bar_png = output_dir / f"{PREVIEW_STEM}_colorbar.png"
    # A light chip with dark text stays readable on both the dark and light UI themes.
    figure, axis = plt.subplots(figsize=(4.6, 0.72))
    figure.subplots_adjust(bottom=0.52, top=0.88, left=0.07, right=0.93)
    bar = figure.colorbar(mapper, cax=axis, orientation="horizontal")
    bar.set_label(f"Elevation (m, {vertical_datum})", fontsize=8, color="#16222c")
    bar.ax.tick_params(labelsize=7, colors="#16222c")
    bar.outline.set_edgecolor("#8fa3b0")
    figure.savefig(bar_png, dpi=170, facecolor="#eef2f6", edgecolor="none")
    plt.close(figure)

    west, north = transform.c, transform.f
    east, south = west + width * transform.a, north + height * transform.e
    meta = {
        "png": out_png.name, "colorbar": bar_png.name, "sources": used,
        "west": west, "south": south, "east": east, "north": north,
        "min_elevation_m": float(np.nanmin(data)), "max_elevation_m": float(np.nanmax(data)),
        "display_low_m": low, "display_high_m": high,
        "coverage_fraction": float(valid.sum() / max(1, aoi_mask.sum() if aoi_mask is not None else valid.size)),
        "width": int(width), "height": int(height), "vertical_datum": vertical_datum,
        "clipped_to_aoi": bool(aoi_mask is not None),
    }
    manifest_name = "manifest.json" if output_dir != primary.parent else f"{PREVIEW_STEM}.json"
    (output_dir / manifest_name).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def _surface_context(config: Path) -> tuple[dict[str, Any], Path, Path | None]:
    cfg = _resolved_yaml(config)
    paths = _run_paths(cfg)
    output = paths["root"]
    state_path = paths["state"]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except Exception:
        state = {}
    geo_meta = state.get("meta", {}).get("geojson", {}) if isinstance(state, dict) else {}
    value = geo_meta.get("domain_path") if isinstance(geo_meta, dict) else None
    domain = Path(str(value)).expanduser() if value else paths["vectors"] / "domain.geojson"
    if not domain.is_file():
        domain = paths["vectors"] / "domain.geojson"
    return cfg, output, domain if domain.is_file() else None


def _surface_rasters(config: Path) -> tuple[Path, Path | None, list[dict[str, Any]]]:
    """Discover surface rasters prepared by surface-only or full builds."""
    cfg, output, domain = _surface_context(config)
    data = _run_paths(cfg)["surface"]
    specs: list[dict[str, Any]] = []

    try:
        state = json.loads(_run_paths(cfg)["state"].read_text(encoding="utf-8"))
    except Exception:
        state = {}
    geo_meta = state.get("meta", {}).get("geojson", {}) if isinstance(state, dict) else {}
    override_active = int(geo_meta.get("n_roughness_overrides", 0) or 0) > 0
    landcover_source = str(cfg.get("landcover", {}).get("source", "")).lower()
    landcover_candidates = []
    if override_active:
        landcover_candidates += sorted(data.glob("landcover_with_roughness_overrides.tif"))
    if landcover_source == "file":
        landcover_candidates += sorted(data.glob("user_landcover.tif"))
    elif "nlcd" in landcover_source:
        landcover_candidates += sorted(data.glob("annual_nlcd_lndcov_*.tif"))
    else:
        # A project may contain rasters left by both regular and subgrid runs.
        # Select the raster for the active hydraulic mode explicitly; a simple
        # alphabetical glob selected the smaller regular-grid raster first and
        # made the subgrid map look cropped even though its real input was not.
        model_mode = str((cfg.get("model", {}) or {}).get("mode", "regular")).strip().lower()
        if model_mode == "subgrid":
            resolution = float((cfg.get("subgrid", {}) or {}).get("landcover_output_resolution_m", 10.0))
            preferred = data / f"esa_worldcover_model_crs_subgrid_{resolution:g}m.tif"
        else:
            preferred = data / "esa_worldcover_model_crs.tif"
        landcover_candidates.append(preferred)
        landcover_candidates += sorted(
            (path for path in data.glob("esa_worldcover_model_crs*.tif") if path != preferred),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    landcover = next((path for path in landcover_candidates if path.is_file()), None)
    if landcover:
        landcover_style = "nlcd" if "nlcd" in landcover_source else "esa"
        landcover_label = "Annual NLCD land cover" if landcover_style == "nlcd" else "Land cover"
        specs.append({"key": "landcover", "label": landcover_label, "path": landcover, "style": landcover_style})
        table_candidates = [
            *([data / "manning_with_adaptations.csv"] if override_active else []),
            *([data / "manual_lulc_manning.csv"]
              if str(cfg.get("landcover", {}).get("manning_mapping", "")).lower() in
              {"manual_lulc_classes", "manual", "custom"} else []),
            *([data / "user_manning_table.csv"] if cfg.get("landcover", {}).get("manning_table") else []),
            data / ("nlcd_manning_default.csv" if "nlcd" in landcover_source
                    else "esa_worldcover_manning_default.csv"),
        ]
        table = next((path for path in table_candidates if path.is_file()), None)
        if table:
            specs.append({
                "key": "manning", "label": "Manning roughness", "path": landcover,
                "style": "manning", "mapping": table,
            })

    infiltration = cfg.get("infiltration", {})
    if bool(infiltration.get("enabled", False)):
        method = str(infiltration.get("method", "cn")).strip().lower()
        source = str(infiltration.get("source", "gcn250")).strip().lower()
        if method == "cn":
            if source == "file":
                candidate = data / "user_curve_number.tif"
            else:
                aliases = {"dry": "ARCI", "avg": "ARCII", "average": "ARCII", "wet": "ARCIII"}
                condition = aliases.get(str(infiltration.get("antecedent_moisture", "avg")).lower(), "ARCII")
                clipped = list(data.glob("gcn250_*_model_crs_*.tif"))
                if clipped:
                    candidate = max(clipped, key=lambda path: path.stat().st_mtime)
                else:
                    cache = Path(str(cfg.get("project", {}).get("cache_dir", "./cache"))).expanduser().resolve()
                    candidate = cache / "gcn250" / f"GCN250_{condition}.tif"
            if candidate.is_file():
                spec = {"key": "curve_number", "label": "SCS curve number", "path": candidate, "style": "cn"}
                if landcover is not None:
                    spec.update({
                        "coverage_mask": landcover,
                        "coverage_exclude_values": [0, 11] if landcover_style == "nlcd" else [0, 80],
                        "coverage_basis": "non-water land",
                    })
                specs.append(spec)
        elif method == "cn_with_recovery":
            nlcd = next(iter(sorted(data.glob("annual_nlcd_lndcov_*.tif"))), None)
            hsg = next(iter(sorted(data.glob("ssurgo_hsg_*.tif"))), None)
            ksat = next(iter(sorted(data.glob("ssurgo_ksat_um_s_*.tif"))), None)
            if nlcd:
                specs.append({"key": "nlcd", "label": "Annual NLCD", "path": nlcd, "style": "nlcd"})
            if hsg:
                specs.append({"key": "hsg", "label": "Hydrologic soil group", "path": hsg, "style": "hsg"})
            if ksat:
                specs.append({"key": "ksat", "label": "Saturated conductivity", "path": ksat, "style": "ksat"})
    return data, domain, specs


def _aoi_grid(domain: Path, max_pixels: int = PREVIEW_MAX_PIXELS) -> tuple[list[dict[str, Any]], Any, int, int]:
    from rasterio.transform import from_bounds

    document = json.loads(domain.read_text(encoding="utf-8"))
    features = document.get("features", []) if document.get("type") == "FeatureCollection" else [document]
    geometries = []
    coordinates: list[tuple[float, float]] = []

    def collect(node: Any) -> None:
        if isinstance(node, list) and len(node) >= 2 and all(isinstance(value, (int, float)) for value in node[:2]):
            coordinates.append((float(node[0]), float(node[1])))
        elif isinstance(node, list):
            for child in node:
                collect(child)

    for feature in features:
        geometry = feature.get("geometry", feature) if isinstance(feature, dict) else None
        if isinstance(geometry, dict) and geometry.get("type") in {"Polygon", "MultiPolygon"}:
            geometries.append(geometry)
            collect(geometry.get("coordinates", []))
    if not geometries or not coordinates:
        raise RuntimeError(f"No polygon geometry found in study-area AOI: {domain}")
    west, east = min(p[0] for p in coordinates), max(p[0] for p in coordinates)
    south, north = min(p[1] for p in coordinates), max(p[1] for p in coordinates)
    span_x, span_y = max(east - west, 1e-9), max(north - south, 1e-9)
    if span_x >= span_y:
        width, height = max_pixels, max(1, round(max_pixels * span_y / span_x))
    else:
        height, width = max_pixels, max(1, round(max_pixels * span_x / span_y))
    return geometries, from_bounds(west, south, east, north, width, height), width, height


def write_surface_preview(spec: dict[str, Any], output: Path, domain: Path) -> dict[str, Any] | None:
    """Render one prepared roughness/infiltration raster inside the exact AOI."""
    import numpy as np
    from rasterio.features import geometry_mask
    from rasterio.warp import Resampling
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import cm, colors, pyplot as plt
    from PIL import Image

    geometries, transform, width, height = _aoi_grid(domain)
    values = _to_lonlat_grid(Path(spec["path"]), transform, width, height, Resampling.nearest)
    inside = geometry_mask(geometries, out_shape=(height, width), transform=transform, invert=True)
    values[~inside] = np.nan
    style = str(spec["style"])
    display = values.copy()
    ticks = None
    ticklabels = None

    if style == "manning":
        mapping: dict[int, float] = {}
        with Path(spec["mapping"]).open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                try:
                    code, value = int(float(row["esa_worldcover"])), float(row["N"])
                    if value >= 0:
                        mapping[code] = value
                except (KeyError, TypeError, ValueError):
                    continue
        display[:] = np.nan
        for code, value in mapping.items():
            display[np.isclose(values, code)] = value
        cmap = matplotlib.colormaps["viridis"]
        label = "Manning n"
    elif style in {"esa", "nlcd", "hsg"}:
        if style == "esa":
            codes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]
            palette = ["#006400", "#ffbb22", "#ffff4c", "#f096ff", "#fa0000", "#b4b4b4", "#f0f0f0", "#0064c8", "#0096a0", "#00cf75", "#fae6a0"]
            extras = sorted(int(value) for value in np.unique(values[np.isfinite(values)])
                            if int(value) not in codes and int(value) >= 0)
            codes.extend(extras)
            palette.extend(matplotlib.colors.to_hex(matplotlib.colormaps["tab20"](index % 20))
                           for index in range(len(extras)))
            label = "ESA WorldCover class"
        elif style == "hsg":
            codes, palette, label = [1, 2, 3, 4], ["#2c7bb6", "#abd9e9", "#fdae61", "#d7191c"], "Hydrologic soil group (A–D)"
        else:
            codes = sorted(int(value) for value in np.unique(values[np.isfinite(values)]))
            palette = [matplotlib.colors.to_hex(matplotlib.colormaps["tab20"](index % 20)) for index in range(len(codes))]
            label = "Annual NLCD class"
        display[:] = np.nan
        for index, code in enumerate(codes):
            display[np.isclose(values, code)] = index
        cmap = colors.ListedColormap(palette)
        norm = colors.BoundaryNorm(np.arange(-0.5, len(codes) + 0.5), cmap.N)
        ticks = list(range(len(codes)))
        ticklabels = ["A", "B", "C", "D"] if style == "hsg" else [str(code) for code in codes]
    else:
        cmap = matplotlib.colormaps["YlOrRd"] if style == "cn" else matplotlib.colormaps["plasma"]
        label = "SCS curve number" if style == "cn" else "Ksat (µm/s)"

    valid = np.isfinite(display)
    if not valid.any():
        return None
    coverage_mask = inside.copy()
    coverage_basis = "AOI"
    coverage_raster = spec.get("coverage_mask")
    if coverage_raster:
        eligibility = _to_lonlat_grid(
            Path(coverage_raster), transform, width, height, Resampling.nearest
        )
        coverage_mask &= np.isfinite(eligibility)
        excluded = spec.get("coverage_exclude_values", [])
        if excluded:
            coverage_mask &= ~np.isin(eligibility, np.asarray(excluded, dtype="float32"))
        coverage_basis = str(spec.get("coverage_basis", "eligible AOI"))
    covered = valid & coverage_mask
    coverage_fraction = float(covered.sum() / max(1, coverage_mask.sum()))
    aoi_coverage_fraction = float(valid.sum() / max(1, inside.sum()))
    reported_valid = values[valid] if style in {"esa", "nlcd", "hsg"} else display[valid]
    if style not in {"esa", "nlcd", "hsg"}:
        low = float(np.nanpercentile(display[valid], 1))
        high = float(np.nanpercentile(display[valid], 99))
        if high <= low:
            high = low + 1.0
        norm = colors.Normalize(vmin=low, vmax=high)
    mapper = cm.ScalarMappable(norm=norm, cmap=cmap)
    rgba = mapper.to_rgba(display, bytes=True)
    rgba[..., 3] = np.where(valid, 225, 0).astype("uint8")

    stem = f"surface_preview_{spec['key']}"
    png = output / f"{stem}.png"
    bar_png = output / f"{stem}_colorbar.png"
    Image.fromarray(rgba, mode="RGBA").save(png, optimize=True)
    figure, axis = plt.subplots(figsize=(5.2, 0.82))
    figure.subplots_adjust(bottom=0.52, top=0.88, left=0.06, right=0.94)
    bar = figure.colorbar(mapper, cax=axis, orientation="horizontal", ticks=ticks)
    if ticklabels:
        bar.ax.set_xticklabels(ticklabels)
    bar.set_label(label, fontsize=8, color="#16222c")
    bar.ax.tick_params(labelsize=7, colors="#16222c")
    bar.outline.set_edgecolor("#8fa3b0")
    figure.savefig(bar_png, dpi=170, facecolor="#eef2f6", edgecolor="none")
    plt.close(figure)

    west, north = transform.c, transform.f
    east, south = west + width * transform.a, north + height * transform.e
    return {
        "key": spec["key"], "label": spec["label"], "source": Path(spec["path"]).name,
        "png": png.name, "colorbar": bar_png.name,
        "west": west, "south": south, "east": east, "north": north,
        "min": float(np.nanmin(reported_valid)), "max": float(np.nanmax(reported_valid)),
        "coverage_fraction": coverage_fraction,
        "coverage_basis": coverage_basis,
        "aoi_coverage_fraction": aoi_coverage_fraction,
        "clipped_to_aoi": True,
    }


def write_surface_previews(config: Path) -> list[dict[str, Any]]:
    data, domain, specs = _surface_rasters(config)
    if domain is None:
        raise FileNotFoundError("prepared study-area domain was not found")
    cfg = _resolved_yaml(config)
    preview_dir = (
        _run_paths(cfg)["previews"] / "surface"
        if int(cfg.get("project", {}).get("layout_version", 1) or 1) >= 2 else data
    )
    preview_dir.mkdir(parents=True, exist_ok=True)
    previews = []
    for spec in specs:
        preview = write_surface_preview(spec, preview_dir, domain)
        if preview:
            previews.append(preview)
    manifest = preview_dir / ("manifest.json" if preview_dir != data else "surface_previews.json")
    manifest.write_text(json.dumps({"layers": previews}, indent=1), encoding="utf-8")
    return previews


def invalidate_surface_previews(config: Path, status: str) -> None:
    """Prevent a failed preparation from leaving prior-method maps visible."""
    cfg = _resolved_yaml(config)
    paths = _run_paths(cfg)
    data = paths["surface"]
    preview_dir = paths["previews"] / "surface" if _organized_layout(cfg) else data
    preview_dir.mkdir(parents=True, exist_ok=True)
    manifest = preview_dir / ("manifest.json" if preview_dir != data else "surface_previews.json")
    manifest.write_text(json.dumps({"layers": [], "status": status}, indent=1), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--mode", choices=sorted(WORKFLOW_MODES), default="full")
    args = parser.parse_args()

    app = args.app.expanduser().resolve()
    config = args.config.expanduser().resolve()
    if not app.is_file():
        raise SystemExit(f"AutoCF core not found: {app}")
    if not config.is_file():
        raise SystemExit(f"Configuration not found: {config}")

    # Keep the temporary overlay beside the requested configuration so a
    # relative `extends:` path continues to resolve exactly as before.
    try:
        temporary = create_runtime_overlay(config, args.mode)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
    try:
        print(f"AutoCF Windows mode: {args.mode}", flush=True)
        print(f"Configuration       : {config}", flush=True)
        command = [sys.executable, "-u", str(app), str(temporary)]
        if args.mode != "terrain":
            if args.mode == "surface":
                try:
                    invalidate_surface_previews(config, "preparing")
                except Exception as exc:
                    print(f"Could not invalidate prior surface previews: {exc}", flush=True)
            code = int(subprocess.run(command).returncode)
            # Surface previews are created by the explicit surface-preparation
            # operation.  Generating them after a model build needlessly scans
            # the very large 10 m global land-cover raster and leaves the GUI
            # displaying "Running" even though the SFINCS project is complete.
            if code == 0 and args.mode == "surface":
                try:
                    previews = write_surface_previews(config)
                    print("Surface map previews: " + (
                        ", ".join(item["label"] for item in previews) if previews else "no prepared raster layers"
                    ), flush=True)
                except Exception as exc:
                    print(f"Surface map previews skipped: {exc}", flush=True)
                    try:
                        invalidate_surface_previews(config, "preview_failed")
                    except Exception:
                        pass
            elif args.mode == "surface":
                try:
                    invalidate_surface_previews(config, "failed")
                except Exception as exc:
                    print(f"Could not mark surface previews as failed: {exc}", flush=True)
            return code

        # Terrain mode additionally renders a map preview, so the agent's log is
        # echoed line by line here to pick up the raster paths it reports.
        rasters: list[Path] = []
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            for marker in ("Primary topobathy:", "Gapfill topobathy:"):
                if marker in line:
                    value = line.split(marker, 1)[1].strip()
                    if value and value.lower() != "none":
                        rasters.append(Path(value))
        code = int(process.wait())
        if code == 0:
            try:
                # The run state is authoritative. Log scraping is retained only
                # as a compatibility fallback for older core versions.
                primary, gapfill, datum, domain = prepared_topobathy(config)
                if primary is None and rasters:
                    primary = rasters[0]
                    gapfill = rasters[1] if len(rasters) > 1 else None
                if primary is None:
                    raise FileNotFoundError("prepared primary topobathy raster was not found")
                cfg = _resolved_yaml(config)
                preview_dir = (
                    _run_paths(cfg)["previews"] / "terrain"
                    if _organized_layout(cfg) else primary.parent
                )
                meta = write_topobathy_preview(
                    primary, gapfill, datum, domain, output_dir=preview_dir,
                    additional_sources=prepared_additional_topobathy(cfg))
                if meta:
                    print(f"Terrain preview: {preview_dir / meta['png']}", flush=True)
                    print(f"Terrain preview sources: {', '.join(meta['sources'])}", flush=True)
                    print("Terrain preview range: "
                          f"{meta['min_elevation_m']:.1f} to {meta['max_elevation_m']:.1f} m, "
                          f"{meta['coverage_fraction'] * 100:.0f}% of the extent covered", flush=True)
                else:
                    print("Terrain preview: the prepared rasters held no valid elevation values.", flush=True)
            except Exception as exc:
                print(f"Terrain preview skipped: {exc}", flush=True)
        return code
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
