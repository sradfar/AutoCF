#!/usr/bin/env python3
"""Attribute SFINCS flood depths to buildings, roads, and population.

The workflow uses Shapely for flood-footprint construction and spatial
intersection, Overture Maps for building/transportation exposure, and
WorldPop API v2 for population totals. Source downloads and API responses are
cached below ``<run>/data/exposure/source`` in layout-v2 runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import numpy as np
import pandas as pd
import rasterio
import requests
import yaml
from affine import Affine
from matplotlib import pyplot as plt
from rasterio import features
from rasterio.enums import Resampling
from rasterio.warp import reproject
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from run_layout import RunLayout, refresh_artifact_catalog, run_layout

def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def read_config(path: Path, stack: Optional[List[Path]] = None) -> Dict[str, Any]:
    path = path.expanduser().resolve()
    chain = list(stack or [])
    if path in chain:
        raise RuntimeError("Circular YAML inheritance: " + " -> ".join(map(str, chain + [path])))
    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    parent = raw.pop("extends", None)
    if parent:
        parent_path = Path(str(parent)).expanduser()
        if not parent_path.is_absolute():
            parent_path = path.parent / parent_path
        cfg = deep_merge(read_config(parent_path, chain + [path]), raw)
    else:
        cfg = raw
    project = cfg.get("project", {})
    suffix = project.get("output_dir_suffix")
    if suffix:
        base = Path(str(project["output_dir"])).expanduser()
        suffix = str(suffix)
        if not base.name.endswith(suffix):
            project["output_dir"] = str(base.with_name(base.name + suffix))
        cfg["project"] = project
    return cfg


def setting_path(value: Any) -> Path:
    return Path(str(value)).expanduser().resolve()


def find_hmax(run: Path, impact_cfg: Dict[str, Any], layout: RunLayout) -> Path:
    configured = impact_cfg.get("hmax_path")
    candidates = []
    if configured:
        candidates.append(setting_path(configured))
    candidates.extend(
        [
            layout.result_path("evaluation", "hmax_subgrid_downscaled.tif"),
            layout.result_path("evaluation", "hmax_hydromt_depth.tif"),
            layout.result_path("evaluation", "hmax_model_grid.tif"),
            run / "agent_plots" / "hmax_subgrid_downscaled.tif",
            run / "agent_plots" / "hmax_hydromt_depth.tif",
            run / "agent_plots" / "hmax.tif",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "No flood-depth GeoTIFF was found. Run 'Evaluate results' first so AutoCF "
        "creates hmax_subgrid_downscaled.tif or hmax_hydromt_depth.tif."
    )


def polygonal(geometry: Any) -> Any:
    if geometry is None or geometry.is_empty:
        return geometry
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    polygons = [g for g in getattr(geometry, "geoms", []) if g.geom_type in {"Polygon", "MultiPolygon"}]
    return unary_union(polygons) if polygons else geometry


def build_flood_footprints(
    hmax_path: Path,
    thresholds: List[float],
    analysis_resolution_m: float,
) -> Tuple[gpd.GeoDataFrame, Dict[float, Any], Any]:
    log(f"Reading flood depths: {hmax_path}")
    with rasterio.open(hmax_path) as src:
        if src.crs is None:
            raise RuntimeError(f"Flood-depth raster has no CRS: {hmax_path}")
        px = max(abs(float(src.transform.a)), abs(float(src.transform.e)))
        scale = max(1.0, float(analysis_resolution_m) / max(px, 1e-9))
        width = max(1, int(round(src.width / scale)))
        height = max(1, int(round(src.height / scale)))
        transform = src.transform * Affine.scale(src.width / width, src.height / height)
        crs = src.crs
        array = np.full((height, width), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=array,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=transform,
            dst_crs=src.crs,
            dst_nodata=np.nan,
            resampling=Resampling.max,
        )

    footprints: Dict[float, Any] = {}
    records: List[Dict[str, Any]] = []
    for threshold in sorted(thresholds):
        mask = np.isfinite(array) & (array >= float(threshold))
        geometries = [
            shape(geom)
            for geom, value in features.shapes(mask.astype("uint8"), mask=mask, transform=transform)
            if int(value) == 1
        ]
        footprint = polygonal(unary_union(geometries)) if geometries else None
        footprints[float(threshold)] = footprint
        area = 0.0 if footprint is None or footprint.is_empty else float(footprint.area)
        records.append({"depth_threshold_m": float(threshold), "flood_area_km2": area / 1_000_000.0, "geometry": footprint})
        log(f"Flood footprint >= {threshold:g} m: {area / 1_000_000.0:,.2f} km2")
    return gpd.GeoDataFrame(records, geometry="geometry", crs=crs), footprints, crs


def read_domain(cfg: Dict[str, Any]) -> gpd.GeoDataFrame:
    value = cfg.get("geojson", {}).get("path")
    if not value:
        raise RuntimeError("geojson.path is required to define the exposure download area.")
    domain = gpd.read_file(setting_path(value))
    if domain.empty:
        raise RuntimeError(f"Domain file contains no features: {value}")
    if domain.crs is None:
        domain = domain.set_crs(4326)
    return domain.to_crs(4326)


def download_overture(feature_type: str, output: Path, bbox: Iterable[float], force: bool) -> None:
    if output.is_file() and not force:
        log(f"Using cached Overture {feature_type}: {output}")
        return
    executable = shutil.which("overturemaps")
    if not executable:
        raise RuntimeError("The Overture Maps client is missing. Run 'Install / repair runtime'.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    bbox_text = ",".join(f"{float(v):.8f}" for v in bbox)
    command = [executable, "download", f"--bbox={bbox_text}", "-f", "geoparquet", "--type", feature_type, "-o", str(output)]
    log("Downloading Overture Maps " + feature_type + " exposure...")
    subprocess.run(command, check=True)
    if not output.is_file():
        raise RuntimeError(f"Overture Maps did not create {output}")


def load_overture(
    section: Dict[str, Any],
    feature_type: str,
    default_path: Path,
    bbox: Iterable[float],
    force: bool,
) -> gpd.GeoDataFrame:
    source = str(section.get("source", "overture_maps")).strip().lower()
    custom = section.get("path")
    if source == "file" or custom:
        path = setting_path(custom)
    elif source == "overture_maps":
        path = default_path
        download_overture(feature_type, path, bbox, force)
    else:
        raise RuntimeError(f"Unsupported exposure source {source!r} for {feature_type}.")
    frame = gpd.read_parquet(path) if path.suffix.lower() in {".parquet", ".geoparquet"} else gpd.read_file(path)
    if frame.crs is None:
        frame = frame.set_crs(4326)
    return frame


def clip_to_domain(frame: gpd.GeoDataFrame, domain: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    projected_domain = domain.to_crs(frame.crs)
    union = projected_domain.geometry.union_all() if hasattr(projected_domain.geometry, "union_all") else projected_domain.unary_union
    valid = frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    positions = np.asarray(valid.sindex.query(union, predicate="intersects"), dtype=int)
    return valid.iloc[np.unique(positions)].copy()


def classify_buildings(
    buildings: gpd.GeoDataFrame,
    footprints: Dict[float, Any],
    crs: Any,
) -> Tuple[List[Dict[str, Any]], gpd.GeoDataFrame]:
    buildings = buildings.to_crs(crs)
    depth_class = np.full(len(buildings), np.nan, dtype="float64")
    rows: List[Dict[str, Any]] = []
    for threshold in sorted(footprints):
        flood = footprints[threshold]
        hit = np.zeros(len(buildings), dtype=bool)
        if flood is not None and not flood.is_empty:
            positions = np.asarray(buildings.sindex.query(flood, predicate="intersects"), dtype=int)
            hit[np.unique(positions)] = True
        depth_class[hit] = threshold
        rows.append(
            {
                "depth_threshold_m": threshold,
                "impacted_buildings": int(hit.sum()),
                "impacted_building_footprint_km2": float(buildings.geometry.area[hit].sum()) / 1_000_000.0,
            }
        )
    impacted = buildings.loc[np.isfinite(depth_class)].copy()
    impacted["max_depth_class_m"] = depth_class[np.isfinite(depth_class)]
    keep = [c for c in ("id", "subtype", "class", "height", "num_floors", "max_depth_class_m", "geometry") if c in impacted.columns]
    return rows, impacted[keep]


def classify_roads(
    segments: gpd.GeoDataFrame,
    footprints: Dict[float, Any],
    crs: Any,
) -> Tuple[List[Dict[str, Any]], gpd.GeoDataFrame]:
    if "subtype" in segments.columns:
        segments = segments.loc[segments["subtype"].astype(str).str.lower().eq("road")].copy()
    roads = segments.to_crs(crs)
    rows: List[Dict[str, Any]] = []
    lowest = min(footprints)
    impacted = gpd.GeoDataFrame(geometry=[], crs=crs)
    for threshold in sorted(footprints):
        flood = footprints[threshold]
        if flood is None or flood.is_empty:
            hit = np.zeros(len(roads), dtype=bool)
            length = 0.0
        else:
            hit = np.zeros(len(roads), dtype=bool)
            positions = np.asarray(roads.sindex.query(flood, predicate="intersects"), dtype=int)
            hit[np.unique(positions)] = True
            length = float(roads.loc[hit].geometry.intersection(flood).length.sum())
        rows.append({"depth_threshold_m": threshold, "impacted_road_segments": int(hit.sum()), "flooded_road_km": length / 1000.0})
        if threshold == lowest and hit.any():
            clipped = roads.loc[hit].copy()
            clipped["geometry"] = clipped.geometry.intersection(flood)
            clipped["flooded_length_m"] = clipped.geometry.length
            keep = [c for c in ("id", "subtype", "class", "flooded_length_m", "geometry") if c in clipped.columns]
            impacted = clipped[keep]
    return rows, impacted


def worldpop_population(
    footprint: Any,
    crs: Any,
    threshold: float,
    year: int,
    resolution: str,
    api_url: str,
    source_dir: Path,
    force: bool,
    simplify_m: float,
    timeout_seconds: int,
) -> Dict[str, Any]:
    if footprint is None or footprint.is_empty:
        return {"total_population": 0.0, "area_km2": 0.0, "data_year": year, "data_source": "WorldPop API v2"}
    flood = gpd.GeoSeries([footprint], crs=crs)
    simplified = flood.geometry.iloc[0].simplify(float(simplify_m), preserve_topology=True)
    wgs84 = gpd.GeoSeries([simplified], crs=crs).to_crs(4326).iloc[0]
    geometry_json = mapping(polygonal(wgs84))
    fingerprint = hashlib.sha256(json.dumps(geometry_json, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    cache = source_dir / f"worldpop_{year}_{resolution}_{threshold:g}m_{fingerprint}.json"
    if cache.is_file() and not force:
        log(f"Using cached WorldPop result for >= {threshold:g} m")
        return json.loads(cache.read_text(encoding="utf-8"))["result"]

    payload = {"geojson": geometry_json, "year": int(year), "resolution": resolution}
    headers: Dict[str, str] = {}
    if os.environ.get("WORLDPOP_API_KEY"):
        headers["X-API-Key"] = os.environ["WORLDPOP_API_KEY"]
    log(f"Submitting WorldPop API v2 population request for >= {threshold:g} m...")
    response = requests.post(api_url.rstrip("/") + "/population", json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    submission = response.json()
    task_id = submission.get("task_id") or submission.get("taskid")
    if not task_id:
        raise RuntimeError(f"WorldPop did not return a task ID: {submission}")

    deadline = time.monotonic() + timeout_seconds
    last_stage = None
    while time.monotonic() < deadline:
        status_response = requests.get(api_url.rstrip("/") + f"/tasks/{task_id}", timeout=60)
        status_response.raise_for_status()
        status = status_response.json()
        state = str(status.get("status", "")).lower()
        stage = status.get("stage") or state
        if stage != last_stage:
            log(f"WorldPop {threshold:g} m: {stage}")
            last_stage = stage
        if state == "success":
            cache.write_text(json.dumps(status, indent=2), encoding="utf-8")
            return status.get("result") or {}
        if state in {"failure", "failed"}:
            raise RuntimeError(f"WorldPop task failed: {status.get('error') or status}")
        time.sleep(2.0)
    raise TimeoutError(f"WorldPop task {task_id} did not finish within {timeout_seconds} seconds.")


def write_plot(summary: pd.DataFrame, output: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    labels = [f">= {v:g} m" for v in summary["depth_threshold_m"]]
    series = [
        ("impacted_buildings", "Impacted buildings", "#b44"),
        ("flooded_road_km", "Flooded road length (km)", "#d87a16"),
        ("impacted_population", "Impacted population", "#4676b8"),
    ]
    for ax, (column, title, color) in zip(axes, series):
        if column in summary:
            values = pd.to_numeric(summary[column], errors="coerce").fillna(0.0)
        else:
            values = pd.Series(np.zeros(len(summary)), index=summary.index)
        ax.bar(labels, values, color=color)
        ax.set_title(title)
        ax.tick_params(axis="x", rotation=40)
        ax.grid(axis="y", alpha=0.25)
    fig.suptitle("AutoCF cumulative flood-impact attribution")
    fig.tight_layout()
    fig.savefig(output, dpi=250, bbox_inches="tight")
    plt.close(fig)


def write_map(
    domain: gpd.GeoDataFrame,
    footprints: gpd.GeoDataFrame,
    buildings: Optional[gpd.GeoDataFrame],
    roads: Optional[gpd.GeoDataFrame],
    output: Path,
) -> None:
    crs = footprints.crs
    fig, ax = plt.subplots(figsize=(10, 10))
    domain.to_crs(crs).boundary.plot(ax=ax, color="black", linewidth=1.1, label="Model domain")
    lowest = footprints.sort_values("depth_threshold_m").iloc[[0]]
    lowest.plot(ax=ax, color="#3a8fd8", alpha=0.4, edgecolor="#1a5d91", label="Flood footprint")
    if roads is not None and not roads.empty:
        roads.plot(ax=ax, color="#ee8b21", linewidth=0.5, alpha=0.8, label="Flooded roads")
    if buildings is not None and not buildings.empty:
        sample = buildings if len(buildings) <= 150_000 else buildings.sample(150_000, random_state=1)
        sample.plot(ax=ax, color="#b22222", markersize=0.4, alpha=0.65, label="Impacted buildings")
    ax.set_title("AutoCF impact attribution")
    ax.set_axis_off()
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output, dpi=250, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--thresholds-m", help="Comma-separated flood-depth thresholds")
    parser.add_argument("--analysis-resolution-m", type=float)
    parser.add_argument("--skip-buildings", action="store_true")
    parser.add_argument("--skip-roads", action="store_true")
    parser.add_argument("--skip-population", action="store_true")
    parser.add_argument("--force-downloads", action="store_true")
    args = parser.parse_args()

    cfg = read_config(Path(args.config))
    impact_cfg = cfg.get("impact", {})
    layout = run_layout(cfg)
    run = layout.model
    output_dir = layout.result_section("exposure")
    source_dir = layout.exposure_source
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    def artifact(name: str) -> Path:
        path = layout.result_path("exposure", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    thresholds_value = args.thresholds_m or impact_cfg.get("flood_depth_thresholds_m", [0.15, 0.3, 0.6, 1.0, 2.0])
    if isinstance(thresholds_value, str):
        thresholds = [float(v.strip()) for v in thresholds_value.split(",") if v.strip()]
    else:
        thresholds = [float(v) for v in thresholds_value]
    thresholds = sorted(set(v for v in thresholds if v >= 0.0))
    if not thresholds:
        raise RuntimeError("At least one non-negative impact flood-depth threshold is required.")
    resolution = float(args.analysis_resolution_m or impact_cfg.get("analysis_resolution_m", 30.0))

    hmax = find_hmax(run, impact_cfg, layout)
    flood_gdf, flood_by_threshold, flood_crs = build_flood_footprints(hmax, thresholds, resolution)
    flood_gdf.to_parquet(artifact("flood_footprints.parquet"), index=False)
    domain = read_domain(cfg)
    bbox = domain.total_bounds.tolist()

    rows = [{"depth_threshold_m": t, "flood_area_km2": float(flood_gdf.loc[flood_gdf.depth_threshold_m.eq(t), "flood_area_km2"].iloc[0])} for t in thresholds]
    summary = pd.DataFrame(rows)
    provider_errors: Dict[str, str] = {}
    impacted_buildings: Optional[gpd.GeoDataFrame] = None
    impacted_roads: Optional[gpd.GeoDataFrame] = None

    buildings_cfg = impact_cfg.get("buildings", {})
    if not args.skip_buildings and bool(buildings_cfg.get("enabled", True)):
        try:
            buildings = load_overture(buildings_cfg, "building", source_dir / "overture_buildings.parquet", bbox, args.force_downloads)
            buildings = clip_to_domain(buildings, domain)
            log(f"Loaded {len(buildings):,} Overture building footprints in the model domain.")
            building_rows, impacted_buildings = classify_buildings(buildings, flood_by_threshold, flood_crs)
            summary = summary.merge(pd.DataFrame(building_rows), on="depth_threshold_m", how="left")
            impacted_buildings.to_parquet(artifact("impacted_buildings.parquet"), index=False)
        except Exception as exc:
            provider_errors["buildings"] = str(exc)
            log(f"WARNING: building attribution failed: {exc}")

    roads_cfg = impact_cfg.get("roads", {})
    if not args.skip_roads and bool(roads_cfg.get("enabled", True)):
        try:
            segments = load_overture(roads_cfg, "segment", source_dir / "overture_segments.parquet", bbox, args.force_downloads)
            segments = clip_to_domain(segments, domain)
            log(f"Loaded {len(segments):,} Overture transportation segments in the model domain.")
            road_rows, impacted_roads = classify_roads(segments, flood_by_threshold, flood_crs)
            summary = summary.merge(pd.DataFrame(road_rows), on="depth_threshold_m", how="left")
            impacted_roads.to_parquet(artifact("impacted_roads.parquet"), index=False)
        except Exception as exc:
            provider_errors["roads"] = str(exc)
            log(f"WARNING: road attribution failed: {exc}")

    population_cfg = impact_cfg.get("population", {})
    if not args.skip_population and bool(population_cfg.get("enabled", True)):
        try:
            year_value = population_cfg.get("year", "simulation")
            if str(year_value).lower() == "simulation":
                year = int(str(cfg["simulation"]["start"])[:4])
            else:
                year = int(year_value)
            population_rows = []
            for threshold in thresholds:
                result = worldpop_population(
                    flood_by_threshold[threshold],
                    flood_crs,
                    threshold,
                    year,
                    str(population_cfg.get("resolution", "100m")),
                    str(population_cfg.get("api_url", "https://api.worldpop.org/v2")),
                    source_dir,
                    args.force_downloads,
                    float(population_cfg.get("geometry_simplify_m", 100.0)),
                    int(population_cfg.get("timeout_seconds", 900)),
                )
                population_rows.append(
                    {
                        "depth_threshold_m": threshold,
                        "impacted_population": float(result.get("total_population", 0.0)),
                        "worldpop_area_km2": float(result.get("area_km2", np.nan)),
                        "worldpop_year": int(result.get("data_year", year)),
                    }
                )
            summary = summary.merge(pd.DataFrame(population_rows), on="depth_threshold_m", how="left")
        except Exception as exc:
            provider_errors["population"] = str(exc)
            log(f"WARNING: WorldPop attribution failed: {exc}")

    summary.to_csv(artifact("impact_summary.csv"), index=False)
    write_plot(summary, artifact("impact_summary_250dpi.png"))
    write_map(domain, flood_gdf, impacted_buildings, impacted_roads, artifact("impact_map_250dpi.png"))
    report = {
        "created": datetime.now().astimezone().isoformat(),
        "configuration": str(Path(args.config).resolve()),
        "run_directory": str(run),
        "hmax_raster": str(hmax),
        "analysis_resolution_m": resolution,
        "thresholds_m": thresholds,
        "providers": {"buildings": "Overture Maps", "roads": "Overture Maps", "population": "WorldPop API v2"},
        "provider_errors": provider_errors,
        "summary": summary.where(pd.notna(summary), None).to_dict(orient="records"),
    }
    artifact("impact_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    refresh_artifact_catalog(layout)

    print("\nImpact attribution summary:")
    print(summary.to_string(index=False))
    log(f"Impact outputs: {output_dir}")
    if provider_errors:
        log("Impact completed with provider warnings; see impact_report.json.")
    else:
        log("Impact attribution complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
