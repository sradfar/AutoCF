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
  - ocean boundary: NOAA CO-OPS, selected GESLA/UHSLC/JASL stations, user time series, or a constant level
  - river discharge: USGS auto-selection, explicit USGS sites, user time series, or none
  - infiltration: USGS Annual NLCD + USDA SSURGO CN-with-recovery, global GCN250, or user CN raster
  - evaluation observations: NOAA CO-OPS, GESLA, UHSLC/JASL, or user observations

AORC is downloaded/subset automatically from NOAA Public Data on AWS when source=aorc and no path is supplied. A user-supplied AORC NetCDF path remains supported. MRMS is file-backed in this first public version.

Resume behavior:
  - Layout-v2 runs write <output_dir>/metadata/run_state.json.
  - Each completed stage is marked done.
  - Restart with the same command: python -u sfincs_agent.py
  - Existing downloaded/preprocessed files are reused.
  - Existing SFINCS outputs are not overwritten unless project.overwrite=true.

Important Della note:
  - Run this script on a login node for downloading/building because compute nodes may not have internet.
  - No Slurm or sbatch is used. Use workflow.mode=build_only first, then workflow.mode=run_direct_only for the CPU simulation.
"""

from __future__ import annotations

import errno
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
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
import yaml

from sfincs_config import DEFAULT_ATMOSPHERE
from run_layout import RunLayout, refresh_artifact_catalog, run_layout
from vertical_datum import (
    datum_unit_label, harmonize_gapfill_to_target, normalize_vertical_datum,
    source_datum_metadata, source_temporal_metadata, source_vertical_datum,
    transformez_status, vertical_datums_equivalent, write_datum_provenance,
    write_source_provenance_raster,
)

try:
    import geopandas as gpd
    from shapely.geometry import Point, box, shape, mapping
    from shapely.ops import unary_union
    from pyproj import CRS, Transformer
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Missing geospatial Python packages. Install geopandas, shapely, pyproj.\n"
        f"Original import error: {exc}"
    )


DEFAULT_CONFIG = "sfincs_agent_config.yml"
PREPARED_RASTER_PIXEL_LIMIT = 150_000_000
CDS_API_SETUP_URL = "https://cds.climate.copernicus.eu/how-to-api"
CDS_ERA5_DATASET_URL = (
    "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels"
)
CDS_GTSM_DATASET = "sis-water-level-change-timeseries-cmip6"
CDS_GTSM_DATASET_URL = (
    "https://cds.climate.copernicus.eu/datasets/sis-water-level-change-timeseries-cmip6"
)
EWDS_API_URL = "https://ewds.climate.copernicus.eu/api"
EWDS_GLOFAS_DATASET = "cems-glofas-historical"
EWDS_GLOFAS_DATASET_URL = (
    "https://ewds.climate.copernicus.eu/datasets/cems-glofas-historical"
)
EWDS_GLOFAS_TERMS_URL = (
    "https://ewds.climate.copernicus.eu/datasets/cems-glofas-historical"
    "?tab=download#manage-licences"
)
GLOFAS_UPAREA_V4_URL = (
    "https://confluence.ecmwf.int/download/attachments/242067380/"
    "uparea_glofas_v4_0.nc?api=v2&modificationDate=1668604690076&version=2"
)
GLOFAS_LDD_V4_URL = (
    "https://confluence.ecmwf.int/download/attachments/242067380/"
    "ldd_glofas_v4_0.nc?api=v2&modificationDate=1669994937993&version=1"
)

NOAA_NINTH_TOPOBATHY_TILE_INDEX_URL = (
    "https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/"
    "NCEI_ninth_Topobathy_2014_8483/"
    "tileindex_NCEI_ninth_Topobathy_2014.zip"
)
GEBCO_2024_COG_URL = (
    "https://s3.us-west-2.amazonaws.com/"
    "us-west-2.opendata.source.coop/alexgleith/gebco-2024/GEBCO_2024.tif"
)
USGS_3DEP_1_3_ARCSEC_VRT_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
    "USGS_Seamless_DEM_13.vrt"
)
GEDTM30_COG_URL = (
    "https://s3.opengeohub.org/global/edtm/"
    "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v20250611.tif"
)
COPERNICUS_GLO_30_VRT_URL = (
    "https://opentopography.s3.sdsc.edu/raster/COP30/COP30_hh.vrt"
)
BUILTIN_PRIMARY_TOPOBATHY_URLS = {
    "usgs_3dep_1_3_arcsec": USGS_3DEP_1_3_ARCSEC_VRT_URL,
    "usgs_3dep": USGS_3DEP_1_3_ARCSEC_VRT_URL,
    "gebco_2024_15_arcsec": GEBCO_2024_COG_URL,
    "gedtm30": GEDTM30_COG_URL,
    "copernicus_glo_30": COPERNICUS_GLO_30_VRT_URL,
}
BUILTIN_GAPFILL_TOPOBATHY_URLS = dict(BUILTIN_PRIMARY_TOPOBATHY_URLS)
LOCAL_TOPOBATHY_SOURCES = {"file", "nasa_deltax_2021"}
AUTOMATIC_TOPOBATHY_SOURCES = {"noaa_cudem_1_9_arcsec"}
ESA_WORLDCOVER_TILE_URL_TEMPLATE = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)

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
DEFAULT_NLCD_MANNING = {
    11: ("Open water", 0.023), 21: ("Developed - open space", 0.070),
    22: ("Developed - low density", 0.100), 23: ("Developed - medium density", 0.120),
    24: ("Developed - high density", 0.140), 31: ("Barren land", 0.070),
    41: ("Deciduous forest", 0.120), 42: ("Evergreen forest", 0.150),
    43: ("Mixed forest", 0.120), 54: ("Shrub/scrub", 0.050),
    71: ("Grassland/herbaceous", 0.034), 81: ("Pasture/hay", 0.030),
    82: ("Cultivated crops", 0.035), 90: ("Woody wetlands", 0.100),
    95: ("Emergent herbaceous wetlands", 0.035),
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


def era5_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return complete ERA5 settings for canonical and legacy YAML files.

    Current desktop configurations keep the user's provider choices under
    ``forcing`` and intentionally omit the old ``atmosphere`` block.  Advanced
    legacy configurations may still override the internal ERA5 defaults there.
    """
    atmosphere = cfg.get("atmosphere", {}) or {}
    if not isinstance(atmosphere, dict):
        raise RuntimeError("atmosphere must be a YAML mapping when provided")
    return _deep_merge_dict(DEFAULT_ATMOSPHERE, atmosphere)


def _normalize_windows_paths(value: Any) -> Any:
    """Convert absolute Windows paths throughout a config when running in WSL."""
    if isinstance(value, dict):
        return {key: _normalize_windows_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_windows_paths(item) for item in value]
    if isinstance(value, str) and os.name != "nt":
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", value.strip())
        if match:
            return f"/mnt/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/')}"
    return value


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
    raw = _normalize_windows_paths(raw)

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

    for section in (cfg.get("topobathy", {}) or {}).get("additional_sources", []) or []:
        if section.get("path"):
            candidate = Path(str(section["path"])).expanduser()
            section["path"] = str((candidate if candidate.is_absolute() else path.parent / candidate).resolve())
    return cfg


def get_model_mode(cfg: Dict[str, Any]) -> str:
    mode = str(cfg.get("model", {}).get("mode", "regular")).strip().lower()
    if mode not in {"regular", "subgrid"}:
        raise RuntimeError(f"Unsupported model.mode={mode!r}; use 'regular' or 'subgrid'.")
    return mode


def geoseries_union_all(series: Any) -> Any:
    """Return one geometry across GeoPandas 0.14 and 1.x runtimes."""
    union_all = getattr(series, "union_all", None)
    if callable(union_all):
        return union_all()
    return series.unary_union


def subgrid_sampling_resolution_m(cfg: Dict[str, Any]) -> float:
    """Nominal SFINCS subgrid pixel size implied by dx / nr_subgrid_pixels."""
    dx = float(cfg["mesh"]["dx_m"])
    n = int(cfg.get("subgrid", {}).get("nr_subgrid_pixels", 20))
    if n <= 0:
        raise RuntimeError("subgrid.nr_subgrid_pixels must be > 0")
    return dx / float(n)


def effective_topobathy_resolution_m(cfg: Dict[str, Any], requested_m: float) -> float:
    """Return the finest useful elevation resolution for this SFINCS model.

    HydroMT resamples elevation onto either the regular SFINCS grid or the
    configured subgrid sampling lattice. Preparing a source more finely than
    that lattice only creates a much larger in-memory raster; it cannot add a
    finer hydraulic sample to the model.
    """
    requested = float(requested_m)
    if requested <= 0:
        raise RuntimeError("Topobathy output resolution must be > 0")
    useful = (
        subgrid_sampling_resolution_m(cfg)
        if get_model_mode(cfg) == "subgrid"
        else float(cfg["mesh"]["dx_m"])
    )
    return max(requested, useful)


def projected_raster_plan(
    bbox4326: List[float], t_srs: str, resolution_m: float,
) -> Dict[str, Any]:
    """Calculate the deterministic projected extent and raster dimensions."""
    resolution = float(resolution_m)
    if resolution <= 0:
        raise RuntimeError("Prepared raster resolution must be > 0")
    minlon, minlat, maxlon, maxlat = map(float, bbox4326)
    transformer = Transformer.from_crs("EPSG:4326", t_srs, always_xy=True)
    minx, miny, maxx, maxy = transformer.transform_bounds(
        minlon, minlat, maxlon, maxlat, densify_pts=21,
    )
    width_m = max(0.0, float(maxx - minx))
    height_m = max(0.0, float(maxy - miny))
    columns = max(1, int(math.ceil(width_m / resolution)))
    rows = max(1, int(math.ceil(height_m / resolution)))
    return {
        "minx": float(minx), "miny": float(miny),
        "maxx": float(maxx), "maxy": float(maxy),
        "width_m": width_m, "height_m": height_m,
        "resolution_m": resolution,
        "columns": columns, "rows": rows, "pixels": columns * rows,
    }


def _rounded_safe_resolution(required_m: float) -> float:
    """Round upward to a stable, readable resolution without losing safety."""
    required = float(required_m)
    if required < 25.0:
        step = 0.5
    elif required < 100.0:
        step = 1.0
    elif required < 500.0:
        step = 5.0
    else:
        step = 10.0
    return math.ceil(required / step) * step


def memory_safe_raster_resolution_m(
    bbox4326: List[float], t_srs: str, requested_m: float,
    pixel_limit: int = PREPARED_RASTER_PIXEL_LIMIT,
) -> Tuple[float, Dict[str, Any], Dict[str, Any]]:
    """Preserve the requested resolution and return its deterministic raster plan."""
    requested_plan = projected_raster_plan(bbox4326, t_srs, requested_m)
    return float(requested_m), requested_plan, requested_plan


def resolution_tag(value: float) -> str:
    return f"{float(value):g}".replace(".", "p")


def infiltration_enabled(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("infiltration", {}).get("enabled", False))


def infiltration_signature(cfg: Dict[str, Any]) -> str:
    """Stable hash used to prevent running a stale HydroMT build after edits."""
    obj = cfg.get("infiltration", {}) if infiltration_enabled(cfg) else {"enabled": False}
    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def roughness_signature(cfg: Dict[str, Any]) -> str:
    """Hash land-cover and class-to-Manning choices for resumable builds."""
    landcover = dict(cfg.get("landcover", {}) or {})
    if str(landcover.get("manning_mapping", "default_esa_worldcover")).lower() == "default_esa_worldcover":
        landcover.pop("manual_manning_classes", None)
    obj = {
        "landcover": landcover,
        "model_mode": cfg.get("model", {}).get("mode", "regular"),
        "subgrid_landcover_resolution_m": cfg.get("subgrid", {}).get("landcover_output_resolution_m"),
    }
    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def source_file_signature(value: Any) -> str:
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def geojson_component_signatures(document: Any) -> Dict[str, str]:
    """Hash study-area components independently for targeted cache invalidation."""
    features = document.get("features", []) if isinstance(document, dict) else []
    groups: Dict[str, List[Dict[str, Any]]] = {
        "domain": [],
        "ocean_boundary": [],
        "ocean_forcing_point": [],
        "observation_point": [],
        "river_source": [],
        "roughness_override": [],
    }
    for feature in features:
        if not isinstance(feature, dict):
            continue
        role = str((feature.get("properties") or {}).get("role", "")).lower()
        if role in groups:
            groups[role].append(feature)
    if not groups["domain"]:
        groups["domain"] = [
            feature for feature in features
            if isinstance(feature, dict)
            and str((feature.get("geometry") or {}).get("type", "")) in {"Polygon", "MultiPolygon"}
            and str((feature.get("properties") or {}).get("role", "")).lower() != "roughness_override"
        ][:1]
    result: Dict[str, str] = {}
    for role, selected in groups.items():
        canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"), default=str)
        result[role] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return result


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


def topobathy_source_signature(
    section: Dict[str, Any], bbox: Iterable[float], role: str
) -> str:
    """Fingerprint one ordered elevation source and its local-file revision."""
    source = str(section.get("source", "")).strip().lower()
    path_value = str(section.get("path", "")).strip()
    file_revision: Tuple[int, int] | None = None
    if source in LOCAL_TOPOBATHY_SOURCES and path_value:
        local_path = Path(path_value).expanduser()
        if local_path.is_file():
            stat = local_path.stat()
            file_revision = (int(stat.st_size), int(stat.st_mtime_ns))
    payload = {
        "role": role,
        "source": source,
        "url": section.get("vrt_url") or section.get("cog_url"),
        "path": path_value,
        "output_resolution_m": section.get("output_resolution_m"),
        "vertical_datum": source_vertical_datum(section),
        "vertical_datum_offset_m": section.get("vertical_datum_offset_m"),
        "file_revision": file_revision,
        "bbox": [round(float(value), 8) for value in bbox],
    }
    if source == "noaa_cudem_1_9_arcsec":
        payload["tile_index"] = NOAA_NINTH_TOPOBATHY_TILE_INDEX_URL
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


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


def run_cmd(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    heartbeat_message: Optional[str] = None,
    heartbeat_seconds: float = 30.0,
) -> None:
    log("RUN: " + " ".join(str(c) for c in cmd))
    if heartbeat_message:
        interval = max(1.0, float(heartbeat_seconds))
        started = time.monotonic()
        log(f"{heartbeat_message} Progress will be reported every {interval:g} seconds.")
        p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env)
        while True:
            try:
                returncode = p.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                elapsed = max(0, int(time.monotonic() - started))
                minutes, seconds = divmod(elapsed, 60)
                log(
                    f"{heartbeat_message} Still working; elapsed "
                    f"{minutes}m {seconds:02d}s. NOAA response time can vary."
                )
    else:
        returncode = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, env=env
        ).returncode
    if returncode != 0:
        if returncode in {-9, 137} and any("hydromt" in str(part).lower() for part in cmd):
            raise RuntimeError(
                "HydroMT was forcibly killed by Linux (out-of-memory is the usual cause). "
                "AutoCF limits prepared terrain to the finest resolution used by the "
                "configured SFINCS grid/subgrid. If this still occurs, increase mesh.dx_m, "
                "reduce subgrid.nr_subgrid_pixels, or use a smaller study area."
            )
        raise RuntimeError(f"Command failed with status {returncode}: {' '.join(cmd)}")


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
    # AORC precipitation is an hourly accumulation labelled at the END of
    # its interval. Fetch one additional boundary record so shifting it back
    # one hour still covers the exact simulation end. Wind and pressure are
    # subsequently sliced back to the normal atmospheric window.
    if "precipitation" in kinds:
        end += pd.Timedelta(hours=1)
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
    interval_accumulation = bool(provider.get("interval_accumulation", source in {"aorc", "mrms"}))
    time_label = str(provider.get(
        "accumulation_time_label", "right" if source in {"aorc", "mrms"} else "left"
    )).strip().lower()
    defer_time_slice = kind == "precipitation" and interval_accumulation and time_label == "right"
    if not defer_time_slice:
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
        dst = paths.data_file("forcing", f"{source}_wind_standardized.nc")
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
        dst = paths.data_file("forcing", f"{source}_pressure_standardized_pa.nc")
    else:
        raw = str(provider.get("variable", dflt[0]))
        da = one(raw, "precip", float(provider.get("factor", 1.0)), float(provider.get("offset", 0.0)))
        # AORC and MRMS precipitation products are interval accumulations.
        # hydromt_sfincs 1.0.3 writes the supplied precipitation values directly
        # as SFINCS precipitation intensity, so convert an interval depth [mm]
        # to the equivalent mean rate [mm/hr]. Do NOT cumulatively sum the
        # successive hourly amounts. For native hourly AORC, the numerical
        # values are unchanged because 1 mm over 1 hour = 1 mm/hr.
        if interval_accumulation:
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
            if time_label not in {"left", "right"}:
                raise RuntimeError(
                    f"forcing.{kind}.accumulation_time_label must be left or right; got {time_label!r}"
                )
            if time_label == "right":
                shift = pd.to_timedelta(dt_hours, unit="h")
                da = da.assign_coords(time=pd.DatetimeIndex(pd.to_datetime(da["time"].values)) - shift)
                da = da.sel(time=slice(start, end))
                if da.sizes.get("time", 0) == 0:
                    raise RuntimeError(
                        f"{source}/{kind} has no data after converting right-labelled intervals "
                        "to SFINCS interval-start times"
                    )
            da.attrs["units"] = "mm/hr"
            log(
                f"Standardized {source.upper()} precipitation interval depth to rate: "
                f"dt={dt_hours:g} h; time_label={time_label}; no cumulative sum applied."
            )
        out = xr.Dataset({"precip": da})
        dst = paths.data_file("forcing", f"{source}_precip_standardized.nc")
    # Only materialize the selected variable(s), not every field in a shared
    # automatic AORC subset. This avoids loading the same four-variable file
    # three times when rain, wind and pressure all use AORC.
    out = out.load()
    ds.close()
    out.to_netcdf(dst)
    return dst


def complete_aorc_coastal_gaps(path: Path, kind: str) -> Dict[str, float]:
    """Complete AORC coastal gaps as part of forcing preparation itself.

    The resulting NetCDF is complete before HydroMT sees it. Model building is
    therefore only a consumer and never owns this correction.
    """
    import xarray as xr  # type: ignore
    from scipy.ndimage import distance_transform_edt  # type: ignore

    with xr.open_dataset(path) as source:
        dataset = source.load()
    lon_dim = "lon" if "lon" in dataset.dims else "longitude" if "longitude" in dataset.dims else None
    lat_dim = "lat" if "lat" in dataset.dims else "latitude" if "latitude" in dataset.dims else None
    if lon_dim is None or lat_dim is None:
        raise RuntimeError(f"AORC {kind} output has no latitude/longitude dimensions")
    original: Dict[str, float] = {}
    for name in dataset.data_vars:
        before = float(dataset[name].notnull().mean())
        original[name] = before
        if before < 0.50:
            raise RuntimeError(
                f"AORC {kind}/{name} has only {before:.2%} finite coverage before coastal filling; "
                "select ERA5 or verify the study area lies within the AORC domain."
            )
        if before >= 0.999999:
            continue
        source_da = dataset[name]
        values = np.asarray(source_da.values, dtype=float)
        lat_axis, lon_axis = source_da.dims.index(lat_dim), source_da.dims.index(lon_dim)
        moved = np.moveaxis(values, (lat_axis, lon_axis), (-2, -1))
        fields = moved.reshape((-1,) + moved.shape[-2:])
        for field in fields:
            missing = ~np.isfinite(field)
            if not missing.any():
                continue
            if missing.all():
                raise RuntimeError(f"AORC {kind}/{name} contains an entirely missing spatial time slice")
            nearest = distance_transform_edt(missing, return_distances=False, return_indices=True)
            field[missing] = field[tuple(nearest[:, missing])]
        restored = np.moveaxis(moved.reshape(moved.shape), (-2, -1), (lat_axis, lon_axis))
        filled = source_da.copy(data=restored)
        after = float(filled.notnull().mean())
        if after < 0.99:
            raise RuntimeError(
                f"AORC {kind}/{name} coastal-gap filling was incomplete: "
                f"finite coverage {before:.2%} -> {after:.2%}"
            )
        filled.attrs.update(source_da.attrs)
        filled.attrs["autocf_spatial_fill"] = "nearest coastal extrapolation during atmosphere preparation"
        filled.attrs["autocf_original_finite_fraction"] = before
        dataset[name] = filled
        log(
            f"Atmosphere preparation filled AORC coastal gaps for {kind}/{name}: "
            f"finite coverage {before:.2%} -> {after:.2%}"
        )
    temporary = path.with_suffix(path.suffix + ".forcing-tmp")
    if temporary.exists():
        temporary.unlink()
    dataset.to_netcdf(temporary)
    temporary.replace(path)
    return original


def validate_atmospheric_forcing(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], resolved: Dict[str, Optional[Path]]
) -> Dict[str, Any]:
    """Fail early when atmospheric forcing cannot safely drive SFINCS."""
    import xarray as xr  # type: ignore

    start = parse_time(cfg["simulation"]["start"]).tz_localize(None)
    end = atmospheric_forcing_end(cfg).tz_localize(None)
    west, south, east, north = map(float, geo_meta["bbox"])
    details: Dict[str, Any] = {}
    variable_names = {
        "precipitation": ("precip", "Precipitation", "rainfall"),
        "wind": ("wind_u", "eastward_wind", "u10"),
        "pressure": ("press", "barometric_pressure", "pressure", "press_msl"),
    }

    for kind in ("precipitation", "wind", "pressure"):
        path = resolved.get(kind)
        if path is None:
            details[kind] = {"status": "disabled"}
            continue
        ds = xr.open_dataset(path)
        try:
            time_name = "time" if "time" in ds.coords else "valid_time" if "valid_time" in ds.coords else None
            if time_name is None or ds.sizes.get(time_name, 0) < 2:
                raise RuntimeError(f"{kind} forcing must contain at least two time steps: {path}")
            times = pd.DatetimeIndex(pd.to_datetime(ds[time_name].values)).tz_localize(None)
            if not times.is_monotonic_increasing or times.has_duplicates:
                raise RuntimeError(f"{kind} forcing time coordinate is not strictly increasing: {path}")
            if times[0] > start or times[-1] < end:
                raise RuntimeError(
                    f"{kind} forcing does not cover the simulation window {start}..{end}; "
                    f"available={times[0]}..{times[-1]}"
                )
            gaps_h = np.diff(times.values).astype("timedelta64[s]").astype(float) / 3600.0
            max_gap_h = float(np.max(gaps_h))
            if not np.isfinite(max_gap_h) or max_gap_h > 3.01:
                raise RuntimeError(f"{kind} forcing has a time gap of {max_gap_h:g} h; maximum allowed is 3 h")

            lat_name = "lat" if "lat" in ds.coords else "latitude" if "latitude" in ds.coords else None
            lon_name = "lon" if "lon" in ds.coords else "longitude" if "longitude" in ds.coords else None
            if lat_name is None or lon_name is None:
                raise RuntimeError(f"{kind} forcing requires latitude/longitude coordinates: {path}")
            lat = np.asarray(ds[lat_name].values, dtype=float)
            lon = np.asarray(ds[lon_name].values, dtype=float)
            if lat.ndim != 1 or lon.ndim != 1:
                raise RuntimeError(f"{kind} forcing latitude/longitude coordinates must be one-dimensional")
            if float(np.nanmin(lon)) > west or float(np.nanmax(lon)) < east or float(np.nanmin(lat)) > south or float(np.nanmax(lat)) < north:
                raise RuntimeError(
                    f"{kind} forcing grid does not cover the study-area AOI {geo_meta['bbox']}; "
                    f"grid={[float(np.nanmin(lon)), float(np.nanmin(lat)), float(np.nanmax(lon)), float(np.nanmax(lat))]}"
                )

            candidates = [name for name in variable_names[kind] if name in ds.data_vars]
            if kind == "wind":
                u_name = candidates[0] if candidates else None
                v_name = next((name for name in ("wind_v", "northward_wind", "v10") if name in ds.data_vars), None)
                if u_name is None or v_name is None:
                    raise RuntimeError(f"Cannot identify both wind components in {path}; variables={list(ds.data_vars)}")
                arrays = [ds[u_name], ds[v_name]]
                speed_max = float(np.hypot(arrays[0], arrays[1]).max(skipna=True))
                if speed_max > 150.0:
                    raise RuntimeError(f"Wind range check failed: maximum speed={speed_max:g} m/s")
                value_range = [float(min(a.min(skipna=True) for a in arrays)), float(max(a.max(skipna=True) for a in arrays))]
            else:
                if not candidates:
                    raise RuntimeError(f"Cannot identify {kind} variable in {path}; variables={list(ds.data_vars)}")
                arrays = [ds[candidates[0]]]
                value_range = [float(arrays[0].min(skipna=True)), float(arrays[0].max(skipna=True))]
                if kind == "precipitation" and (value_range[0] < -1e-6 or value_range[1] > 500.0):
                    raise RuntimeError(f"Rainfall range check failed: {value_range} mm/h")
                if kind == "pressure" and (value_range[0] < 70000.0 or value_range[1] > 120000.0):
                    raise RuntimeError(f"Pressure range check failed: {value_range} Pa")

            finite = np.asarray([float(a.notnull().mean().compute() if hasattr(a.data, "compute") else a.notnull().mean()) for a in arrays])
            finite_fraction = float(np.min(finite))
            if finite_fraction < 0.99:
                raise RuntimeError(f"{kind} forcing contains too many missing values: finite fraction={finite_fraction:.3%}")
            details[kind] = {
                "status": "PASS", "path": str(path), "time_start": times[0].isoformat(),
                "time_end": times[-1].isoformat(), "time_steps": int(len(times)),
                "maximum_gap_hours": max_gap_h, "finite_fraction": finite_fraction,
                "value_range": value_range,
            }
            log(
                f"Atmosphere validation PASS: {kind} | steps={len(times)} | "
                f"time={times[0]}..{times[-1]} | finite={finite_fraction:.2%} | range={value_range}"
            )
        finally:
            ds.close()
    return {
        "status": "PASS", "components": [key for key, value in details.items() if value.get("status") == "PASS"],
        "simulation_start": start.isoformat(), "simulation_end": end.isoformat(),
        "aoi_bbox_wgs84": [west, south, east, north], "details": details,
    }


def prepare_atmospheric_forcing(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths, state: State
) -> Tuple[Optional[Path], Optional[Path], Optional[Path], Dict[str, Any]]:
    """Download, standardize, and validate all selected atmospheric providers."""
    fcfg = forcing_cfg(cfg)
    sources = {kind: _provider_source(cfg, kind) for kind in ("wind", "pressure", "precipitation")}
    era_ws = era_tp = None
    if "era5" in sources.values():
        era_meta = state.data.get("meta", {}).get("era5", {})
        expected_ws = Path(str(era_meta.get("wind_pressure", ""))).expanduser()
        expected_tp = Path(str(era_meta.get("precip", ""))).expanduser()
        if (not state.is_done("era5") or not file_ready(expected_ws, 1024)
                or not file_ready(expected_tp, 1024)):
            era_ws, era_tp = download_era5(cfg, geo_meta, paths)
            state.mark("era5", wind_pressure=str(era_ws), precip=str(era_tp))
        else:
            era_ws, era_tp = expected_ws, expected_tp

    automatic_aorc = [
        kind for kind in ("wind", "pressure", "precipitation")
        if sources[kind] == "aorc" and not fcfg.get(kind, {}).get("path")
    ]
    aorc_file = None
    if automatic_aorc:
        aorc_file = download_aorc_subset(cfg, geo_meta, paths, automatic_aorc)
        state.mark("aorc", path=str(aorc_file), components=automatic_aorc)

    resolved: Dict[str, Optional[Path]] = {}
    for kind in ("wind", "pressure", "precipitation"):
        source = sources[kind]
        provider = dict(fcfg.get(kind, {}) or {})
        if source == "none":
            output = None
        elif source == "era5":
            output = era_tp if kind == "precipitation" else era_ws
        elif source in ({"aorc", "file"} if kind in {"wind", "pressure"} else {"aorc", "mrms", "file"}):
            if source == "aorc" and not provider.get("path"):
                if aorc_file is None:
                    raise RuntimeError(f"Automatic AORC subset was not resolved for forcing.{kind}")
                provider["path"] = str(aorc_file)
            output = standardize_atmospheric_file(cfg, provider, kind, paths)
        else:
            raise RuntimeError(f"Unsupported forcing.{kind}.source={source!r}")
        if output is not None and source == "aorc":
            complete_aorc_coastal_gaps(output, kind)
        resolved[kind] = output

    validate_pressure_grid_pa(resolved["pressure"])
    validation = validate_atmospheric_forcing(cfg, geo_meta, resolved)
    write_json(validation, paths.result_file("simulation", "atmosphere_forcing_validation.json"))
    state.mark(
        "atmosphere_forcing", signature=forcing_signature(cfg), validation="PASS",
        **{key: str(value) if value is not None else "" for key, value in resolved.items()},
    )
    return resolved["wind"], resolved["pressure"], resolved["precipitation"], validation


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
    write_json(report, paths.result_file("simulation", "forcing_report.json"))

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
    domain_path: Optional[Path] = None,
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

    # HydroMT clips discharge locations to the exact model region.  Apply the
    # same clip to both sides of the forcing pair first; otherwise a nearby
    # gauge outside an irregular HUC polygon is removed from the GeoJSON while
    # its CSV series remains, and HydroMT rejects the mismatched pair.
    if domain_path is not None and domain_path.exists():
        domain = gpd.read_file(domain_path)
        if not domain.empty:
            if domain.crs is None:
                domain = domain.set_crs(4326)
            if gdf.crs is None:
                gdf = gdf.set_crs(domain.crs)
            elif gdf.crs != domain.crs:
                gdf = gdf.to_crs(domain.crs)
            domain_geom = unary_union(domain.geometry.values)
            keep = [bool(domain_geom.covers(geometry)) for geometry in gdf.geometry]
            if not all(keep):
                dropped = len(keep) - sum(keep)
                retained_columns = [column for column, retain in zip(df.columns, keep) if retain]
                gdf = gdf.loc[keep].reset_index(drop=True)
                df = df.loc[:, retained_columns]
                log(
                    f"Study-area clip removed {dropped} out-of-domain river gauge(s) "
                    "from both locations and discharge time series."
                )
            if gdf.empty:
                raise RuntimeError(
                    "No river discharge gauges fall inside the exact study-area polygon. "
                    "Choose in-domain gauges or set forcing.river.source to none."
                )

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
    # hydromt_sfincs documents `index` as the explicit location key for
    # tabular point forcing.  Keep it alongside ID to avoid relying on dtype
    # inference after a GeoJSON round trip.
    gdf["index"] = numeric_ids

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


def raster_has_valid_pixels(path: Path) -> bool:
    """Return True as soon as one valid raster pixel is found.

    File size alone cannot validate remote-VRT results: GDAL can successfully
    create a small, correctly georeferenced GeoTIFF that contains only nodata
    when the selected VRT partition does not cover the study area.
    """
    import rasterio  # type: ignore

    try:
        with rasterio.open(path) as dataset:
            if dataset.count < 1:
                return False
            for _, window in dataset.block_windows(1):
                if np.any(dataset.read_masks(1, window=window)):
                    return True
    except Exception as exc:
        log(f"WARNING: could not validate raster coverage for {path}: {exc}")
    return False


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
    run: Path
    out: Path
    cache: Path
    work: Path
    gis: Path
    validation: Path
    state: Path
    layout: RunLayout

    def data_file(self, section: str, name: str) -> Path:
        path = self.layout.data_path(section, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def result_file(self, section: str, name: str) -> Path:
        path = self.layout.result_path(section, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


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


def extract_geojson_parts(
    geojson_path: Path, paths: Paths, require_boundary_points: bool = True,
) -> Dict[str, Any]:
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
        roughness_overrides = gdf[(roles == "roughness_override") &
                                  gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    else:
        # Fully backward compatible geometry-type convention.
        polys = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        lines = gdf[gdf.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
        points = gdf[gdf.geometry.geom_type == "Point"].copy()
        river_points = gdf.iloc[0:0].copy(); obs_points = gdf.iloc[0:0].copy()
        roughness_overrides = gdf.iloc[0:0].copy()

    if len(polys) != 1:
        raise RuntimeError(f"Expected exactly one domain Polygon/MultiPolygon; found {len(polys)}.")
    if len(lines) < 1:
        raise RuntimeError("Expected at least one ocean_boundary LineString/MultiLineString.")
    if require_boundary_points and len(points) < 1:
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
    # Provider/station fields are required when the user explicitly selected
    # catalog gauges in the desktop study-area builder.
    if len(pts):
        pts.to_file(bnd_points_path, driver="GeoJSON")

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
    roughness_path = None
    if len(roughness_overrides):
        ro = roughness_overrides.reset_index(drop=True).copy()
        roughness_path = paths.gis / "roughness_overrides.geojson"
        ro.to_file(roughness_path, driver="GeoJSON")

    minx, miny, maxx, maxy = domain.total_bounds
    centroid = domain.geometry.iloc[0].centroid
    zone = int((centroid.x + 180) // 6) + 1
    epsg = 32600 + zone if centroid.y >= 0 else 32700 + zone
    meta = {
        "domain_path": str(domain_path), "ocean_path": str(ocean_path),
        "bnd_points_path": str(bnd_points_path) if len(pts) else "",
        "river_points_path": str(river_path) if river_path else "", "observation_points_path": str(obs_path) if obs_path else "",
        "roughness_overrides_path": str(roughness_path) if roughness_path else "",
        "bbox": [float(minx), float(miny), float(maxx), float(maxy)],
        "centroid_lonlat": [float(centroid.x), float(centroid.y)], "auto_utm_epsg": epsg,
        "n_boundary_points": int(len(pts)), "n_river_points": int(len(river_points)), "n_observation_points": int(len(obs_points)),
        "n_roughness_overrides": int(len(roughness_overrides)),
        "role_schema_used": has_roles,
    }
    write_json(meta, paths.result_file("simulation", "geojson_report.json"))
    log(f"GeoJSON parsed: domain=1, ocean lines={len(lines)}, boundary points={len(points)}, "
        f"river points={len(river_points)}, obs points={len(obs_points)}, "
        f"roughness overrides={len(roughness_overrides)}, UTM EPSG:{epsg}")
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


def gdalwarp_remote(src: str, dst: Path, bbox4326: List[float], t_srs: str, tr_m: float,
                    resampling: str = "bilinear", force: bool = False,
                    progress_message: Optional[str] = None) -> float:
    """
    Warp a remote raster to the model CRS using a deterministic target extent.

    The geographic bbox is transformed to the target CRS in Python rather than
    passing ``-te_srs EPSG:4326`` to gdalwarp. Different GDAL/PROJ releases can
    derive different projected envelopes from a geographic -te, which changes
    raster dimensions and therefore the SFINCS active mask and subgrid tables.
    """
    ensure_dir(dst.parent)

    if file_ready(dst, 1024) and not force:
        log(f"Reusing existing raster: {dst}")
        return
    if file_ready(dst, 1024):
        log(f"Study-area inputs changed; rebuilding prepared raster: {dst}")

    require_executable("gdalwarp")

    effective_res, requested_plan, plan = memory_safe_raster_resolution_m(
        bbox4326, t_srs, tr_m,
    )
    te = [
        repr(plan["minx"]), repr(plan["miny"]),
        repr(plan["maxx"]), repr(plan["maxy"]),
    ]

    log(
        "Deterministic projected warp extent: "
        f"{t_srs} | "
        f"x={plan['minx']:.6f}..{plan['maxx']:.6f}, "
        f"y={plan['miny']:.6f}..{plan['maxy']:.6f}"
    )
    if requested_plan["pixels"] > PREPARED_RASTER_PIXEL_LIMIT:
        log(
            "Large raster workload warning: "
            f"{requested_plan['columns']:,} x {requested_plan['rows']:,} = "
            f"{requested_plan['pixels']:,} pixels at {float(tr_m):g} m; "
            "the configured resolution is preserved and AutoCF will not coarsen it automatically."
        )
    log(
        f"Prepared raster plan: {plan['columns']:,} x {plan['rows']:,} = "
        f"{plan['pixels']:,} pixels at {effective_res:g} m"
    )

    cmd = [
        "gdalwarp",
        "-overwrite",
        "-of", "GTiff",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-multi",
        "-wo", "NUM_THREADS=ALL_CPUS",
        "-te", *te,
        "-te_srs", t_srs,
        "-t_srs", t_srs,
        "-tr", str(effective_res), str(effective_res),
        "-r", resampling,
        src,
        str(dst),
    ]

    run_cmd(cmd, heartbeat_message=progress_message)
    return effective_res




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


def esa_tiles_for_geometry(geometry: Any) -> List[str]:
    """Return only 3-degree WorldCover cells with positive AOI overlap."""
    candidates = esa_tiles_for_bbox(list(geometry.bounds))
    selected: List[str] = []
    for tile in candidates:
        match = re.fullmatch(r"([NS])(\d{2})([EW])(\d{3})", tile)
        if match is None:
            continue
        lat = int(match.group(2)) * (1 if match.group(1) == "N" else -1)
        lon = int(match.group(4)) * (1 if match.group(3) == "E" else -1)
        overlap = geometry.intersection(box(lon, lat, lon + 3, lat + 3))
        if not overlap.is_empty and float(overlap.area) > 0.0:
            selected.append(tile)
    return selected


def esa_tiles_for_domain(
    domain_path: Path, buffer_km: float, model_crs: str,
) -> Tuple[List[str], List[float]]:
    """Select WorldCover cells from the actual buffered domain, not its bbox."""
    domain = gpd.read_file(domain_path)
    if domain.empty:
        raise RuntimeError(f"Domain geometry is empty: {domain_path}")
    if domain.crs is None:
        domain = domain.set_crs(4326)
    projected = domain.to_crs(model_crs)
    geometry = unary_union(projected.geometry.values)
    if buffer_km > 0:
        geometry = geometry.buffer(buffer_km * 1000.0)
    buffered = gpd.GeoSeries([geometry], crs=model_crs).to_crs(4326).iloc[0]
    return esa_tiles_for_geometry(buffered), [float(value) for value in buffered.bounds]


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


def _cudem_tile_identity(url: str) -> Tuple[str, Tuple[int, int]]:
    """Return the geographic tile key and publication revision from a CUDEM URL."""
    name = Path(url.split("?", 1)[0]).name
    match = re.search(r"_(\d{4})v(\d+)\.tif$", name, flags=re.IGNORECASE)
    if not match:
        return name.lower(), (0, 0)
    key = name[:match.start()].lower().replace("x", ".")
    return key, (int(match.group(1)), int(match.group(2)))


def _buffered_domain_geometry(
    domain_path: Optional[Path], bbox4326: List[float], buffer_km: float, model_crs: str,
) -> Any:
    """Return the actual buffered study geometry in geographic coordinates."""
    if domain_path is None or not domain_path.exists():
        return box(*bbox4326)
    domain = gpd.read_file(domain_path)
    if domain.empty:
        return box(*bbox4326)
    domain = domain.set_crs(4326) if domain.crs is None else domain.to_crs(4326)
    projected = domain.to_crs(model_crs)
    geometry = unary_union(projected.geometry.values)
    if buffer_km > 0:
        geometry = geometry.buffer(float(buffer_km) * 1000.0)
    return gpd.GeoSeries([geometry], crs=model_crs).to_crs(4326).iloc[0]


def prepare_noaa_cudem_1_9_tiles(
    paths: Paths,
    domain_path: Optional[Path],
    bbox4326: List[float],
    buffer_km: float,
    model_crs: str,
) -> Tuple[Path, Dict[str, Any]]:
    """Cache and mosaic only current NOAA CUDEM 1/9 tiles intersecting the AOI."""
    cache_dir = paths.cache / "noaa_cudem_1_9_arcsec"
    index_zip = cache_dir / "tileindex_NCEI_ninth_Topobathy_2014.zip"
    download_file(NOAA_NINTH_TOPOBATHY_TILE_INDEX_URL, index_zip, min_bytes=10000)

    try:
        index = gpd.read_file(f"zip://{index_zip}")
    except Exception as exc:
        raise RuntimeError(f"Could not read NOAA CUDEM 1/9 tile index: {exc}") from exc
    if index.empty:
        raise RuntimeError("NOAA CUDEM 1/9 tile index is empty.")
    index = index.set_crs(4269) if index.crs is None else index
    index = index.to_crs(4326)
    url_column = next((column for column in index.columns if column.lower() == "url"), None)
    if url_column is None:
        raise RuntimeError("NOAA CUDEM 1/9 tile index does not contain its URL field.")

    selection_geometry = _buffered_domain_geometry(
        domain_path, bbox4326, buffer_km, model_crs,
    )
    selected = index[index.geometry.intersects(selection_geometry)].copy()
    if selected.empty:
        raise RuntimeError(
            "NOAA CUDEM 1/9 does not cover this study area. Select another primary "
            "terrain source or provide your own integrated topobathy GeoTIFF."
        )

    newest: Dict[str, Tuple[Tuple[int, int], str, Any]] = {}
    for _, row in selected.iterrows():
        url = str(row[url_column]).strip()
        if not url.lower().endswith(".tif"):
            continue
        key, revision = _cudem_tile_identity(url)
        prior = newest.get(key)
        if prior is None or revision > prior[0]:
            newest[key] = (revision, url, row.geometry)
    if not newest:
        raise RuntimeError("No downloadable NOAA CUDEM 1/9 GeoTIFFs intersect the study area.")

    tile_records = [newest[key] for key in sorted(newest)]
    coverage_tiles = gpd.GeoSeries(
        [record[2] for record in tile_records], crs=4326,
    ).to_crs(model_crs)
    selected_area = gpd.GeoSeries([selection_geometry], crs=4326).to_crs(model_crs).iloc[0]
    covered_area = unary_union(coverage_tiles.values).intersection(selected_area).area
    coverage_fraction = min(1.0, max(0.0, covered_area / max(selected_area.area, 1.0)))
    log(
        f"NOAA CUDEM 1/9 tile selection: {len(tile_records)} current 0.25-degree "
        f"tile(s); buffered-domain index coverage={coverage_fraction:.1%}."
    )

    tile_paths: List[Path] = []
    for _, url, _ in tile_records:
        tile_path = cache_dir / "tiles" / Path(url.split("?", 1)[0]).name
        download_file(url, tile_path, min_bytes=1_000_000)
        if not raster_has_valid_pixels(tile_path):
            raise RuntimeError(f"Downloaded NOAA CUDEM tile has no valid cells: {tile_path}")
        tile_paths.append(tile_path)

    urls = [record[1] for record in tile_records]
    tile_signature = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()[:16]
    vrt = paths.data_file(
        "terrain", f"noaa_cudem_1_9_local_tiles_{tile_signature}.vrt",
    )
    if not file_ready(vrt, 256):
        require_executable("gdalbuildvrt")
        run_cmd(["gdalbuildvrt", "-overwrite", str(vrt), *[str(path) for path in tile_paths]])

    metadata = {
        "tile_index": NOAA_NINTH_TOPOBATHY_TILE_INDEX_URL,
        "tile_count": len(tile_paths),
        "coverage_fraction": coverage_fraction,
        "tiles": urls,
        "local_vrt": str(vrt),
    }
    write_json(metadata, paths.data_file("terrain", "noaa_cudem_1_9_tiles.json"))
    if coverage_fraction < 0.999:
        log(
            "WARNING: NOAA CUDEM 1/9 tile footprints do not cover the entire buffered "
            f"domain ({coverage_fraction:.1%}); Source 2 may fill only the uncovered area."
        )
    return vrt, metadata


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


def download_esa_worldcover(
    cfg: Dict[str, Any], bbox: List[float], paths: Paths, model_crs: str,
    domain_path: Optional[Path] = None,
) -> Path:
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
    buffer_km = float(lc.get("buffer_km", 10))
    bbox_lc = bbox_buffer_km(bbox, buffer_km)
    rectangular_tiles = esa_tiles_for_bbox(bbox_lc)
    if domain_path is not None and Path(domain_path).exists():
        tiles, bbox_lc = esa_tiles_for_domain(Path(domain_path), buffer_km, model_crs)
        excluded = sorted(set(rectangular_tiles) - set(tiles))
        if excluded:
            log(
                "ESA WorldCover: excluded bbox-only tile(s) outside the actual buffered domain: "
                + ", ".join(excluded)
            )
    else:
        tiles = rectangular_tiles
        log("ESA WorldCover: domain geometry unavailable; using rectangular tile selection.")
    tile_paths = []
    tile_url_template = str(
        lc.get("tile_url_template") or ESA_WORLDCOVER_TILE_URL_TEMPLATE)
    for tile in tiles:
        url = tile_url_template.format(tile=tile)
        dst = paths.cache / "esa_worldcover" / f"ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
        try:
            download_file(url, dst, min_bytes=10000)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 404 and tile_url_template == ESA_WORLDCOVER_TILE_URL_TEMPLATE:
                log(
                    f"ESA WorldCover: no published raster for {tile}; "
                    "treating this official open-ocean/no-data cell as absent."
                )
                continue
            raise
        tile_paths.append(dst)

    if not tile_paths:
        raise RuntimeError(
            "No published ESA WorldCover land-cover tiles intersect the buffered domain. "
            "Use a land-cover file if this is an ocean-only model."
        )

    vrt = paths.data_file("surface", "esa_worldcover_mosaic.vrt")
    if mode == "subgrid":
        sg = cfg.get("subgrid", {})
        target_res = float(sg.get("landcover_output_resolution_m", 10.0))
        if target_res <= 0:
            raise RuntimeError("subgrid.landcover_output_resolution_m must be > 0")
        out = paths.data_file("surface", f"esa_worldcover_model_crs_subgrid_{target_res:g}m.tif")
        log(
            f"Subgrid roughness preprocessing: WorldCover target={target_res:g} m; "
            f"nominal SFINCS subgrid pixel={subgrid_sampling_resolution_m(cfg):g} m"
        )
    else:
        target_res = float(cfg["mesh"]["dx_m"])
        out = paths.data_file("surface", "esa_worldcover_model_crs.tif")

    if not file_ready(vrt, 1024):
        require_executable("gdalbuildvrt")
        run_cmd(["gdalbuildvrt", str(vrt), *[str(p) for p in tile_paths]])
    if not file_ready(out, 1024):
        require_executable("gdalwarp")

        # Transform the geographic extent ourselves. Passing a geographic
        # -te together with -te_srs EPSG:4326 produces different projected
        # envelopes across GDAL/PROJ versions.
        transformer = Transformer.from_crs(
            "EPSG:4326",
            model_crs,
            always_xy=True,
        )

        minlon, minlat, maxlon, maxlat = map(float, bbox_lc)

        minx, miny, maxx, maxy = transformer.transform_bounds(
            minlon,
            minlat,
            maxlon,
            maxlat,
            densify_pts=21,
        )

        te = [
            repr(float(minx)),
            repr(float(miny)),
            repr(float(maxx)),
            repr(float(maxy)),
        ]

        log(
            "Deterministic WorldCover projected extent: "
            f"{model_crs} | "
            f"x={minx:.6f}..{maxx:.6f}, "
            f"y={miny:.6f}..{maxy:.6f}"
        )

        run_cmd([
            "gdalwarp",
            "-overwrite",
            "-of", "GTiff",
            "-co", "COMPRESS=DEFLATE",
            "-co", "TILED=YES",
            "-te", *te,
            "-te_srs", model_crs,
            "-t_srs", model_crs,
            "-tr", str(target_res), str(target_res),
            "-r", "mode",
            str(vrt),
            str(out),
        ])

    return out

def write_esa_manning_table(path: Path) -> None:
    ensure_dir(path.parent)
    rows = []
    for code, (desc, n) in DEFAULT_ESA_MANNING.items():
        rows.append({"esa_worldcover": code, "description": desc, "landuse": desc, "N": n})
    rows.append({"esa_worldcover": 0, "description": "No data", "landuse": "No data", "N": -999.0})
    pd.DataFrame(rows).to_csv(path, index=False)


def write_nlcd_manning_table(path: Path) -> None:
    ensure_dir(path.parent)
    rows = [
        {"esa_worldcover": code, "description": desc, "landuse": desc, "N": value}
        for code, (desc, value) in DEFAULT_NLCD_MANNING.items()
    ]
    rows.append({"esa_worldcover": 0, "description": "No data", "landuse": "No data", "N": -999.0})
    pd.DataFrame(rows).to_csv(path, index=False)


def prepare_manning_table(lcfg: Dict[str, Any], paths: Paths) -> Path:
    """Resolve automatic, file-based, or manual LULC-class Manning values."""
    mode = str(lcfg.get("manning_mapping", "default_esa_worldcover")).strip().lower()
    manual = lcfg.get("manual_manning_classes")
    if mode in {"manual_lulc_classes", "manual", "custom"} and isinstance(manual, list) and manual:
        rows = []
        codes: set[int] = set()
        for index, record in enumerate(manual, 1):
            if not isinstance(record, dict):
                raise RuntimeError(f"landcover.manual_manning_classes row {index} must be a mapping")
            try:
                code = int(record.get("code", record.get("class")))
                value = float(record.get("n", record.get("N")))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"landcover.manual_manning_classes row {index} needs integer code and numeric n"
                ) from exc
            if code < 0 or code in codes:
                raise RuntimeError(f"Manual Manning LULC class code must be unique and non-negative: {code}")
            if not (0.0 < value <= 1.0):
                raise RuntimeError(f"Manual Manning n for class {code} must be > 0 and <= 1")
            name = str(record.get("name", record.get("description", f"Class {code}"))).strip() or f"Class {code}"
            codes.add(code)
            rows.append({"esa_worldcover": code, "description": name, "landuse": name, "N": value})
        if 0 not in codes:
            rows.append({"esa_worldcover": 0, "description": "No data", "landuse": "No data", "N": -999.0})
        path = paths.data_file("surface", "manual_lulc_manning.csv")
        ensure_dir(path.parent)
        pd.DataFrame(rows).to_csv(path, index=False)
        log(f"Using manual LULC-class Manning lookup with {len(codes)} classes: {path}")
        return path
    if lcfg.get("manning_table"):
        return copy_user_file(
            lcfg.get("manning_table"), paths.data_file("surface", "user_manning_table.csv"), "landcover.manning_table")
    if "nlcd" in str(lcfg.get("source", "")).lower():
        path = paths.data_file("surface", "nlcd_manning_default.csv")
        write_nlcd_manning_table(path)
    else:
        path = paths.data_file("surface", "esa_worldcover_manning_default.csv")
        write_esa_manning_table(path)
    return path


def apply_roughness_overrides(
    landcover: Path, manning_table: Path, geo_meta: Dict[str, Any], paths: Paths
) -> Tuple[Path, Path]:
    """Burn optional user-drawn adaptation polygons into a copy of the LULC raster."""
    override_value = geo_meta.get("roughness_overrides_path")
    override_path = Path(str(override_value)) if override_value else None
    if override_path is None or not override_path.is_file():
        return landcover, manning_table

    import rasterio
    from rasterio.features import rasterize

    polygons = gpd.read_file(override_path)
    polygons = polygons[polygons.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    if polygons.empty:
        return landcover, manning_table
    required = [name for name in ("lulc_code", "manning_n") if name not in polygons.columns]
    if required:
        raise RuntimeError(
            "Roughness override polygons are missing properties: " + ", ".join(required))

    lookup: Dict[int, Tuple[str, float]] = {}
    for index, row in polygons.iterrows():
        try:
            code = int(row["lulc_code"])
            value = float(row["manning_n"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Roughness override polygon {index + 1} has invalid lulc_code or manning_n") from exc
        if code < 1 or not (0.0 < value <= 1.0):
            raise RuntimeError(
                f"Roughness override polygon {index + 1} needs lulc_code >= 1 and 0 < manning_n <= 1")
        name = str(row.get("name", f"Adaptation {code}")).strip() or f"Adaptation {code}"
        if code in lookup and abs(lookup[code][1] - value) > 1e-12:
            raise RuntimeError(f"Roughness override class {code} is assigned more than one Manning n value")
        lookup[code] = (name, value)

    output = paths.data_file("surface", "landcover_with_roughness_overrides.tif")
    sentinel = -2_147_483_648
    with rasterio.open(landcover) as source:
        projected = polygons.set_crs(4326) if polygons.crs is None else polygons
        projected = projected.to_crs(source.crs)
        shapes = [(geometry, int(code)) for geometry, code in zip(projected.geometry, projected["lulc_code"])]
        burned = rasterize(
            shapes, out_shape=(source.height, source.width), transform=source.transform,
            fill=sentinel, dtype="int32", all_touched=True)
        values = source.read(1, masked=True).filled(sentinel).astype("int32")
        changed = burned != sentinel
        values[changed] = burned[changed]
        profile = source.profile.copy()
        profile.update(dtype="int32", nodata=sentinel, compress="deflate", predictor=2)
        with rasterio.open(output, "w", **profile) as target:
            target.write(values, 1)

    table = pd.read_csv(manning_table)
    code_column = table.columns[0]
    numeric_codes = pd.to_numeric(table[code_column], errors="coerce")
    table = table.loc[~numeric_codes.isin(list(lookup))].copy()
    additions = [
        {code_column: code, "description": name, "landuse": name, "N": value}
        for code, (name, value) in lookup.items()
    ]
    table = pd.concat([table, pd.DataFrame(additions)], ignore_index=True)
    output_table = paths.data_file("surface", "manning_with_adaptations.csv")
    table.to_csv(output_table, index=False)
    log(
        f"Applied {len(polygons)} NBS/adaptation roughness polygon(s) using {len(lookup)} class value(s): "
        f"{output}")
    return output, output_table


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
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths,
    landcover_config: Optional[Dict[str, Any]] = None,
) -> Path:
    """Download an Annual NLCD land-cover subset from the official USGS WCS.

    Annual NLCD is requested on its native EPSG:5070, 30 m grid. Keeping the
    categorical source at native resolution avoids unnecessary resampling before
    HydroMT combines it with SSURGO HSG data.
    """
    icfg = cfg.get("infiltration", {})
    lcfg = landcover_config if landcover_config is not None else icfg.get("landcover", {})
    year_cfg = lcfg.get("year", "simulation")
    if year_cfg is None or str(year_cfg).strip().lower() in {"simulation", "event", "auto"}:
        year = int(parse_time(cfg["simulation"]["start"]).year)
    else:
        year = int(year_cfg)
    native_res = float(lcfg.get("native_resolution_m", lcfg.get("resolution_m", 30.0)))
    native_crs = str(lcfg.get("native_crs", "EPSG:5070"))
    buffer_km = float(lcfg.get("buffer_km", 5.0))
    workspace = str(lcfg.get("workspace", USGS_ANNUAL_NLCD_LNDCOV_WORKSPACE))
    base = str(lcfg.get("wcs_base_url", USGS_ANNUAL_NLCD_WCS_BASE)).rstrip("/")

    domain = gpd.read_file(geo_meta["domain_path"])
    if domain.crs is None:
        domain = domain.set_crs(4326)
    domain = domain.to_crs(native_crs)
    if buffer_km > 0:
        geom = geoseries_union_all(domain.geometry).buffer(buffer_km * 1000.0)
        bounds = geom.bounds
    else:
        bounds = domain.total_bounds
    minx, miny, maxx, maxy = _snap_bounds(bounds, native_res)

    sig = _bbox_signature([minx, miny, maxx, maxy], year, workspace, native_res)
    cache_file = paths.cache / "annual_nlcd" / f"Annual_NLCD_LndCov_{year}_{sig}.tif"
    crs_tag = native_crs.upper().replace(":", "")
    out = paths.data_file("surface", f"annual_nlcd_lndcov_{year}_{crs_tag.lower()}_{native_res:g}m.tif")

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
        paths.result_file("simulation", "annual_nlcd_infiltration_report.json"),
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
            # WFS 1.1.0 was verified independently to return the same reference
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

    hsg_tif = paths.data_file("surface", f"ssurgo_hsg_{res:g}m_{soil_sig}.tif")
    ksat_tif = paths.data_file("surface", f"ssurgo_ksat_um_s_{res:g}m_{soil_sig}.tif")
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
    raw.to_csv(paths.data_file("surface", f"ssurgo_component_horizon_raw_{soil_sig}.csv"), index=False)

    props = _aggregate_ssurgo_properties(raw, cfg)
    props.to_csv(paths.data_file("surface", f"ssurgo_mapunit_properties_{soil_sig}.csv"), index=False)
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
    geom = geoseries_union_all(domain.geometry).buffer(
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
    write_json(report, paths.result_file("simulation", "ssurgo_infiltration_report.json"))
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
            # Do not hand HydroMT the 640 MB global raster.  HydroMT 0.9.x can
            # materialize most of it while applying median reprojection, which
            # used more than 16 GB during the Windows sample run.  Clip and
            # project once to the model neighbourhood; the small result is also
            # reused by surface previews and resumed builds.
            target_res = float(icfg.get("output_resolution_m", cfg.get("mesh", {}).get("dx_m", 250.0)))
            if target_res <= 0:
                raise RuntimeError("infiltration.output_resolution_m must be > 0")
            bbox = bbox_buffer_km(geo_meta["bbox"], float(icfg.get("buffer_km", 5.0)))
            target_res, requested_plan, safe_plan = memory_safe_raster_resolution_m(
                bbox, model_crs, target_res,
            )
            if requested_plan["pixels"] > safe_plan["pixels"]:
                log(
                    "Automatic GCN250 resolution adjustment: "
                    f"using {target_res:g} m for this project extent."
                )
            clip_sig = _bbox_signature(bbox, cond, model_crs, target_res)
            gcn_clip = paths.data_file("surface", f"gcn250_{cond}_model_crs_{target_res:g}m_{clip_sig}.tif")
            gdalwarp_remote(str(gcn), gcn_clip, bbox, model_crs, target_res, "near")
            log(
                "Infiltration specification: ordinary SCS CN using "
                f"GCN250 ({cond}) from authoritative Figshare source; "
                f"AOI clip={gcn_clip}"
            )
            return {"gcn": gcn_clip}
        if source == "file":
            cn = copy_user_file(icfg.get("path"), paths.data_file("surface", "user_curve_number.tif"), "infiltration.path")
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
        table = write_default_nlcd_cn_table(paths.data_file("surface", "nlcd_hsg_cn_ii_lookup.csv"))
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
    ts_path = paths.data_file("forcing", "noaa_boundary_timeseries.csv")
    loc_path = paths.gis / "noaa_boundary_locations.geojson"
    write_hydromt_timeseries_csv(ts, ts_path)
    gpd.GeoDataFrame(loc_rows, crs=4326).to_file(loc_path, driver="GeoJSON")
    pd.DataFrame(report_rows).to_csv(paths.result_file("simulation", "noaa_boundary_station_selection.csv"), index=False)
    log(f"NOAA boundary forcing: wrote {ts_path} and {loc_path}")
    return ts_path, loc_path, pd.DataFrame(report_rows)


CATALOG_WATER_LEVEL_PROVIDERS = {"noaa_coops", "gesla", "uhslc_rq", "uhslc_fast"}


def _station_text(row: pd.Series, *fields: str) -> str:
    for field in fields:
        value = row.get(field)
        if value is not None and not pd.isna(value):
            text = str(value).strip()
            if text and text.lower() != "nan":
                return text
    return ""


def _catalog_station_from_row(row: pd.Series) -> Optional[Tuple[str, str, str]]:
    provider = _station_text(row, "provider", "source", "observation_type").lower()
    aliases = {"noaa": "noaa_coops", "coops": "noaa_coops", "gesla4": "gesla", "jasl": "uhslc_rq"}
    provider = aliases.get(provider, provider)
    station_id = _station_text(row, "station_id", "provider_station_id", "noaa_id")
    if provider not in CATALOG_WATER_LEVEL_PROVIDERS or not station_id:
        return None
    dataset = _station_text(row, "dataset")
    if provider == "gesla":
        dataset = dataset or "global_hourly_gesla"
    elif provider == "uhslc_rq":
        dataset = dataset or "global_hourly_rqds"
    elif provider == "uhslc_fast":
        dataset = dataset or "global_hourly_fast"
    return provider, station_id, dataset


def fetch_selected_water_level(
    provider: str, station_id: str, dataset: str,
    start: pd.Timestamp, end: pd.Timestamp, cfg: Dict[str, Any],
) -> pd.DataFrame:
    """Fetch one exact GUI-selected station in the standard validation schema."""
    if provider == "noaa_coops":
        ncfg = cfg.get("noaa_boundary", {})
        series = download_noaa_water_level(
            station_id, start, end,
            datum=str(ncfg.get("datum", "MSL")), units=str(ncfg.get("units", "metric")),
            product=str(ncfg.get("product", "water_level")),
            time_zone=str(ncfg.get("time_zone", "gmt")),
        )
        return pd.DataFrame({"time": series.index, "observed_zs_m": series.to_numpy()})
    try:
        from sfincs_global_validation import fetch_gesla, fetch_uhslc
    except ImportError:  # pragma: no cover - package-style test imports
        from app.sfincs_global_validation import fetch_gesla, fetch_uhslc
    if provider == "gesla":
        return fetch_gesla(station_id, start, end)
    if provider in {"uhslc_rq", "uhslc_fast"}:
        return fetch_uhslc(station_id, start, end, dataset)
    raise RuntimeError(f"Unsupported selected water-level provider: {provider}")


def select_boundary_catalog_stations(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths,
) -> Tuple[Path, Path, pd.DataFrame]:
    """Use the exact catalog stations stored on ocean-forcing point features."""
    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])
    ocfg = forcing_cfg(cfg).get("ocean_boundary", {})
    cfg2 = dict(cfg)
    cfg2["noaa_boundary"] = {**cfg.get("noaa_boundary", {}), **ocfg}
    pts = gpd.read_file(geo_meta["bnd_points_path"]).to_crs(4326).reset_index(drop=True)
    selected = [_catalog_station_from_row(row) for _, row in pts.iterrows()]
    if any(item is None for item in selected):
        raise RuntimeError(
            "selected_stations boundary forcing requires every water-level point to be a selected NOAA, GESLA, or UHSLC/JASL station."
        )
    selected_items = [item for item in selected if item is not None]
    interval = 60 if any(provider != "noaa_coops" for provider, _, _ in selected_items) else int(ocfg.get("interval_minutes", 6))
    expected = pd.date_range(start, end, freq=f"{interval}min")
    max_missing = float(ocfg.get("max_missing_fraction", cfg2["noaa_boundary"].get("max_missing_fraction", 0.25)))
    alignment_hours = float(ocfg.get("relative_datum_alignment_hours", 12.0))
    reference_m = float(ocfg.get("relative_datum_reference_m", 0.0))
    series_list: List[pd.Series] = []
    location_rows: List[Dict[str, Any]] = []
    report_rows: List[Dict[str, Any]] = []

    for index, ((_, row), station) in enumerate(zip(pts.iterrows(), selected_items), start=1):
        provider, station_id, dataset = station
        obs = fetch_selected_water_level(provider, station_id, dataset, start, end, cfg2)
        values = pd.to_numeric(obs["observed_zs_m"], errors="coerce")
        times = pd.to_datetime(obs["time"], utc=True, errors="coerce")
        raw = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(times), dtype=float).dropna().sort_index()
        raw = raw[~raw.index.isna()]
        raw = raw[~raw.index.duplicated(keep="first")]
        if raw.empty:
            raise RuntimeError(f"{provider} station {station_id} returned no usable event water levels")
        datum_offset = 0.0
        if provider != "noaa_coops":
            baseline_end = min(end, start + pd.Timedelta(hours=alignment_hours))
            baseline = raw.loc[(raw.index >= start) & (raw.index <= baseline_end)]
            if baseline.empty:
                raise RuntimeError(f"{provider} station {station_id} has no data in the first {alignment_hours:g}-hour datum window")
            datum_offset = reference_m - float(baseline.median())
            raw = raw + datum_offset
        sampled = raw.reindex(expected)
        missing_before = float(sampled.isna().mean())
        if missing_before > max_missing:
            raise RuntimeError(
                f"{provider} station {station_id} event missing fraction {missing_before:.1%} exceeds {max_missing:.1%}"
            )
        aligned = sampled.interpolate(method="time", limit_direction="both")
        missing = float(aligned.isna().mean())
        aligned.name = index
        series_list.append(aligned)
        location_rows.append({
            "ID": index, "name": _station_text(row, "name", "ID") or station_id,
            "provider": provider, "provider_station_id": station_id,
            "dataset": dataset, "datum": _station_text(row, "datum"), "geometry": row.geometry,
        })
        report_rows.append({
            "forcing_id": index, "provider": provider, "station_id": station_id,
            "station_name": _station_text(row, "name"), "dataset": dataset,
            "datum": _station_text(row, "datum"), "datum_offset_m": datum_offset,
            "datum_method": "absolute configured datum" if provider == "noaa_coops" else f"first {alignment_hours:g} h median anchored to {reference_m:g} m",
            "missing_fraction_before_interp": missing_before,
            "missing_fraction_after_interp": missing,
        })

    ts_path = paths.data_file("forcing", "selected_station_boundary_timeseries.csv")
    loc_path = paths.gis / "selected_station_boundary_locations.geojson"
    write_hydromt_timeseries_csv(pd.concat(series_list, axis=1), ts_path)
    gpd.GeoDataFrame(location_rows, geometry="geometry", crs=4326).to_file(loc_path, driver="GeoJSON")
    report = pd.DataFrame(report_rows)
    report.to_csv(paths.result_file("simulation", "selected_station_boundary_report.csv"), index=False)
    log(f"Selected-station ocean boundary forcing: wrote {ts_path} and {loc_path}")
    return ts_path, loc_path, report


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
    inside.drop(columns="geometry").to_csv(
        paths.result_file("simulation", "noaa_validation_stations_inside_domain.csv"), index=False)
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
    domain = gpd.read_file(geo_meta["domain_path"]).to_crs(4326)
    domain_geom = unary_union(domain.geometry.values)
    inside = sites.apply(
        lambda row: bool(domain_geom.covers(Point(float(row["lon"]), float(row["lat"])))),
        axis=1,
    )
    outside_count = int((~inside).sum())
    sites = sites.loc[inside].copy()
    if outside_count:
        log(f"Excluded {outside_count} nearby USGS gauge(s) outside the exact study-area polygon.")
    if sites.empty:
        log("No USGS discharge gages fall inside the study-area polygon. Skipping river discharge forcing.")
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
    ts_path = paths.data_file("forcing", "usgs_discharge_m3s.csv")
    loc_path = paths.gis / "usgs_discharge_locations.geojson"
    write_hydromt_timeseries_csv(ts, ts_path)
    gpd.GeoDataFrame(loc_rows, crs=4326).to_file(loc_path, driver="GeoJSON")
    pd.DataFrame(rep_rows).to_csv(
        paths.result_file("simulation", "usgs_discharge_station_selection.csv"), index=False)
    log(f"USGS discharge forcing: wrote {ts_path} and {loc_path}")
    return ts_path, loc_path


# -----------------------------------------------------------------------------
# Copernicus global ocean and river forcing
# -----------------------------------------------------------------------------


def _copernicus_client(label: str, dataset_url: str, api_url: Optional[str] = None) -> Any:
    """Create a CDS/EWDS client without ever copying credentials into a project."""
    try:
        import cdsapi  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            f"{label} requires the Copernicus 'cdsapi' client. Use AutoCF Settings > "
            f"Install / repair runtime, then follow {CDS_API_SETUP_URL}."
        ) from exc
    rc_value = os.environ.get("CDSAPI_RC", "").strip()
    rc_path = Path(rc_value).expanduser() if rc_value else Path.home() / ".cdsapirc"
    env_ready = bool(os.environ.get("CDSAPI_URL", "").strip() and os.environ.get("CDSAPI_KEY", "").strip())
    if not env_ready and not rc_path.is_file():
        raise RuntimeError(
            f"{label} requires your personal Copernicus credentials. No credential file "
            f"was found at {rc_path}. Create the OS-independent user-home file described "
            f"at {CDS_API_SETUP_URL}, and accept this dataset's Terms of Use: {dataset_url}"
        )
    try:
        return cdsapi.Client(url=api_url) if api_url else cdsapi.Client()
    except Exception as exc:
        raise RuntimeError(
            f"AutoCF found Copernicus credentials but could not initialize {label}. "
            f"Refresh the token using {CDS_API_SETUP_URL}, accept the dataset Terms of Use "
            f"at {dataset_url}, and retry. Original error: {exc}"
        ) from exc


def _coordinate_name(obj: Any, candidates: Iterable[str]) -> str:
    names = list(getattr(obj, "coords", {})) + list(getattr(obj, "variables", {}))
    lowered = {str(name).lower(): str(name) for name in names}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    raise RuntimeError(f"Required coordinate not found. Tried {', '.join(candidates)}; available: {names}")


def _coordinate_slice(values: Any, low: float, high: float) -> slice:
    arr = np.asarray(values, dtype=float)
    return slice(low, high) if arr[0] <= arr[-1] else slice(high, low)


def _extract_download_payload(path: Path, output_dir: Path, stem: str) -> Path:
    """Return a NetCDF/GRIB payload, extracting a CDS-created ZIP when needed."""
    if not zipfile.is_zipfile(path):
        return path
    ensure_dir(output_dir)
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith((".nc", ".nc4", ".grib", ".grib2"))]
        if not members:
            raise RuntimeError(f"Copernicus archive contains no NetCDF or GRIB data: {path}")
        member = members[0]
        suffix = Path(member).suffix or ".nc"
        destination = output_dir / f"{stem}{suffix}"
        with archive.open(member) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    return destination


def _open_xarray_payload(path: Path) -> Any:
    import xarray as xr  # type: ignore
    try:
        return xr.open_dataset(path)
    except Exception as netcdf_exc:
        try:
            return xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
        except Exception as grib_exc:
            raise RuntimeError(
                f"Could not open Copernicus data {path} as NetCDF or GRIB. "
                f"NetCDF error: {netcdf_exc}; GRIB error: {grib_exc}"
            ) from grib_exc


def _download_glofas_discharge(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths, rcfg: Dict[str, Any],
) -> Path:
    start = parse_time(cfg["simulation"]["start"]).floor("D") - pd.Timedelta(days=1)
    end = parse_time(cfg["simulation"]["end"]).ceil("D") + pd.Timedelta(days=1)
    if start.year < 1979:
        raise RuntimeError("GloFAS historical discharge coverage begins in 1979 for this AutoCF provider.")
    bbox = bbox_buffer_degrees(geo_meta["bbox"], float(rcfg.get("download_buffer_degrees", 0.15)))
    west, south, east, north = bbox
    dates = pd.date_range(start, end, freq="D")
    signature = _bbox_signature(
        bbox, start.isoformat(), end.isoformat(), str(rcfg.get("system_version", "version_4_0"))
    )
    raw = paths.cache / "glofas" / f"glofas_discharge_{signature}.download"
    ensure_dir(raw.parent)
    if file_ready(raw, 512):
        log(f"Reusing GloFAS discharge: {raw}")
        return _extract_download_payload(
            raw, paths.data_file("forcing", "payload.placeholder").parent,
            f"glofas_discharge_{signature}")

    client = _copernicus_client("GloFAS", EWDS_GLOFAS_DATASET_URL, EWDS_API_URL)
    common = {
        "system_version": [str(rcfg.get("system_version", "version_4_0"))],
        "hydrological_model": ["lisflood"],
        "product_type": [str(rcfg.get("product_type", "consolidated"))],
        "area": [north, west, south, east],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    current_request = {
        **common,
        "variable": ["average_river_discharge_in_the_last_24_hours"],
        "year": sorted({f"{date.year:04d}" for date in dates}),
        "month": sorted({f"{date.month:02d}" for date in dates}),
        "day": sorted({f"{date.day:02d}" for date in dates}),
        "timespan": ["time_mean"],
    }
    log(
        "Downloading a spatial/time subset of GloFAS daily discharge from EWDS: "
        f"{start.date()} through {end.date()} | bbox={bbox}"
    )
    try:
        client.retrieve(EWDS_GLOFAS_DATASET, current_request, str(raw))
    except Exception as current_exc:
        if raw.exists():
            raw.unlink()
        policy_message = str(current_exc).lower()
        if ("didn't accept" in policy_message or "did not accept" in policy_message
                or "not accepted" in policy_message or "missing policies" in policy_message
                or "terms of use" in policy_message or "licence" in policy_message
                or "license" in policy_message or "403 client error" in policy_message):
            raise RuntimeError(
                "GloFAS access was denied by the Copernicus Early Warning Data Store. "
                "The most common cause is that this account has not accepted the required "
                "GloFAS dataset licence. AutoCF cannot accept an account agreement for you. "
                "Open Manage licences while signed into the same Copernicus account, accept "
                f"the required licence, then retry: {EWDS_GLOFAS_TERMS_URL}. "
                f"EWDS response: {current_exc}"
            ) from current_exc
        raise RuntimeError(
            "GloFAS download failed using the current EWDS request schema. Confirm dataset "
            f"availability at {EWDS_GLOFAS_DATASET_URL}. EWDS response: {current_exc}"
        ) from current_exc
    return _extract_download_payload(
        raw, paths.data_file("forcing", "payload.placeholder").parent,
        f"glofas_discharge_{signature}")


_LDD_OFFSETS = {
    1: (-1, -1), 2: (0, -1), 3: (1, -1), 4: (-1, 0),
    6: (1, 0), 7: (-1, 1), 8: (0, 1), 9: (1, 1),
}


def detect_glofas_boundary_inflows(
    domain_geom: Any, longitudes: Any, latitudes: Any,
    upstream_area_m2: Any, ldd_codes: Any, min_upstream_area_km2: float,
) -> List[Dict[str, Any]]:
    """Detect LDD edges that flow from outside the model domain to inside it."""
    lon = np.asarray(longitudes, dtype=float)
    lat = np.asarray(latitudes, dtype=float)
    uparea = np.asarray(upstream_area_m2, dtype=float)
    ldd = np.asarray(ldd_codes)
    if uparea.shape != (len(lat), len(lon)) or ldd.shape != uparea.shape:
        raise RuntimeError("GloFAS upstream-area/LDD arrays are not aligned on latitude,longitude.")
    threshold = float(min_upstream_area_km2) * 1_000_000.0
    candidates: List[Dict[str, Any]] = []
    for iy, ix in np.argwhere(np.isfinite(uparea) & (uparea >= threshold)):
        code = int(round(float(ldd[iy, ix]))) if np.isfinite(ldd[iy, ix]) else 5
        offset = _LDD_OFFSETS.get(code)
        if offset is None:
            continue
        target_lon = float(lon[ix]) + offset[0] * (abs(float(np.median(np.diff(lon)))) if len(lon) > 1 else 0.05)
        target_lat = float(lat[iy]) + offset[1] * (abs(float(np.median(np.diff(lat)))) if len(lat) > 1 else 0.05)
        upstream_point = Point(float(lon[ix]), float(lat[iy]))
        downstream_point = Point(target_lon, target_lat)
        if not domain_geom.covers(upstream_point) and domain_geom.covers(downstream_point):
            candidates.append({
                "grid_x": int(ix), "grid_y": int(iy),
                "sample_lon": float(lon[ix]), "sample_lat": float(lat[iy]),
                "source_lon": target_lon, "source_lat": target_lat,
                "upstream_area_km2": float(uparea[iy, ix]) / 1_000_000.0,
                "ldd": code,
            })
    # Multiple outside cells can flow into one inside cell at a confluence. One
    # SFINCS source at that target must use the largest (already combined) flow.
    unique: Dict[Tuple[float, float], Dict[str, Any]] = {}
    for candidate in sorted(candidates, key=lambda item: item["upstream_area_km2"], reverse=True):
        key = (round(candidate["source_lon"], 6), round(candidate["source_lat"], 6))
        unique.setdefault(key, candidate)
    return list(unique.values())


def _load_glofas_river_network(paths: Paths, bbox: List[float]) -> Tuple[Any, Any, Any, Any]:
    import xarray as xr  # type: ignore
    network_dir = paths.cache / "glofas_network_v4"
    ensure_dir(network_dir)
    up_path = network_dir / "uparea_glofas_v4_0.nc"
    ldd_path = network_dir / "ldd_glofas_v4_0.nc"
    if not file_ready(up_path, 10000):
        log("Downloading the reusable GloFAS v4 upstream-area river network (one-time cache).")
        download_file(GLOFAS_UPAREA_V4_URL, up_path, min_bytes=10000)
    if not file_ready(ldd_path, 10000):
        log("Downloading the reusable GloFAS v4 flow-direction network (one-time cache).")
        download_file(GLOFAS_LDD_V4_URL, ldd_path, min_bytes=10000)
    with xr.open_dataset(up_path) as up_ds, xr.open_dataset(ldd_path) as ldd_ds:
        up_lon = _coordinate_name(up_ds, ("longitude", "lon", "x"))
        up_lat = _coordinate_name(up_ds, ("latitude", "lat", "y"))
        ldd_lon = _coordinate_name(ldd_ds, ("longitude", "lon", "x"))
        ldd_lat = _coordinate_name(ldd_ds, ("latitude", "lat", "y"))
        west, south, east, north = bbox_buffer_degrees(bbox, 0.10)
        up_var = up_ds["uparea"] if "uparea" in up_ds else next(iter(up_ds.data_vars.values()))
        ldd_var = ldd_ds["ldd"] if "ldd" in ldd_ds else next(iter(ldd_ds.data_vars.values()))
        up = up_var.sel({up_lon: _coordinate_slice(up_ds[up_lon], west, east), up_lat: _coordinate_slice(up_ds[up_lat], south, north)}).load()
        ldd = ldd_var.sel({ldd_lon: _coordinate_slice(ldd_ds[ldd_lon], west, east), ldd_lat: _coordinate_slice(ldd_ds[ldd_lat], south, north)}).load()
        # Select the LDD values on the upstream-area grid to eliminate harmless
        # coordinate-name/order differences between the two official files.
        ldd = ldd.interp({ldd_lon: up[up_lon], ldd_lat: up[up_lat]}, method="nearest")
        return np.asarray(up[up_lon]), np.asarray(up[up_lat]), np.asarray(up.squeeze()), np.asarray(ldd.squeeze())


def prepare_glofas_river_forcing(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths,
) -> Tuple[Optional[Path], Optional[Path]]:
    rcfg = forcing_cfg(cfg).get("river", {})
    domain = gpd.read_file(geo_meta["domain_path"]).to_crs(4326)
    domain_geom = unary_union(domain.geometry.values)
    lon, lat, uparea, ldd = _load_glofas_river_network(paths, geo_meta["bbox"])
    inflows = detect_glofas_boundary_inflows(
        domain_geom, lon, lat, uparea, ldd,
        float(rcfg.get("min_upstream_area_km2", 500.0)),
    )
    max_sources = int(rcfg.get("max_sources", rcfg.get("max_gages", 12)))
    if max_sources > 0:
        inflows = sorted(inflows, key=lambda item: item["upstream_area_km2"], reverse=True)[:max_sources]
    report_path = paths.result_file("simulation", "glofas_boundary_inflows.json")
    if not inflows:
        write_json({"status": "no_inflows", "source": "GloFAS", "candidates": []}, report_path)
        log("GloFAS river-network pre-check found no qualifying river entering the domain.")
        return None, None

    discharge_path = _download_glofas_discharge(cfg, geo_meta, paths, rcfg)
    dataset = _open_xarray_payload(discharge_path)
    try:
        lon_name = _coordinate_name(dataset, ("longitude", "lon", "x"))
        lat_name = _coordinate_name(dataset, ("latitude", "lat", "y"))
        time_name = _coordinate_name(dataset, ("valid_time", "time", "date"))
        variables = [name for name in dataset.data_vars if "discharge" in name.lower()]
        discharge = dataset[variables[0] if variables else next(iter(dataset.data_vars))]
        start = parse_time(cfg["simulation"]["start"])
        end = parse_time(cfg["simulation"]["end"])
        interval = int(rcfg.get("interval_minutes", 60))
        expected = pd.date_range(start, end, freq=f"{interval}min")
        series: List[pd.Series] = []
        locations: List[Dict[str, Any]] = []
        accepted: List[Dict[str, Any]] = []
        for candidate in inflows:
            sampled = discharge.sel(
                {lon_name: candidate["sample_lon"], lat_name: candidate["sample_lat"]}, method="nearest"
            ).squeeze()
            raw_times = pd.to_datetime(np.asarray(sampled[time_name]), utc=True, errors="coerce")
            raw = pd.Series(np.asarray(sampled, dtype=float).reshape(-1), index=raw_times).dropna().sort_index()
            raw = raw[~raw.index.duplicated(keep="first")].clip(lower=0.0)
            # GloFAS provides daily mean discharge. Linear time interpolation
            # preserves a continuous boundary flux; missing hours are never zero-filled.
            combined = raw.reindex(raw.index.union(expected)).interpolate(method="time")
            aligned = combined.reindex(expected)
            missing = float(aligned.isna().mean())
            if missing > float(rcfg.get("max_missing_fraction", 0.0)):
                candidate["status"] = "invalid_event_coverage"
                candidate["missing_fraction"] = missing
                continue
            forcing_id = len(series) + 1
            aligned.name = forcing_id
            series.append(aligned)
            locations.append({
                "ID": forcing_id, "name": f"GloFAS_inflow_{forcing_id}", "provider": "glofas",
                "upstream_area_km2": candidate["upstream_area_km2"],
                "sample_lon": candidate["sample_lon"], "sample_lat": candidate["sample_lat"],
                "geometry": Point(candidate["source_lon"], candidate["source_lat"]),
            })
            candidate.update({"status": "valid", "forcing_id": forcing_id, "missing_fraction": missing})
            accepted.append(candidate)
        if not series:
            write_json({"status": "invalid_event_coverage", "source": "GloFAS", "candidates": inflows}, report_path)
            raise RuntimeError("GloFAS found river inflows, but none has complete discharge coverage for the simulation period.")
        ts_path = paths.data_file("forcing", "glofas_boundary_discharge_m3s.csv")
        loc_path = paths.gis / "glofas_boundary_inflows.geojson"
        write_hydromt_timeseries_csv(pd.concat(series, axis=1), ts_path)
        gpd.GeoDataFrame(locations, geometry="geometry", crs=4326).to_file(loc_path, driver="GeoJSON")
        write_json({
            "status": "valid", "source": "GloFAS", "daily_values_are_zero_filled": False,
            "time_axis_method": "linear interpolation between daily mean discharge values",
            "candidates": inflows, "accepted_count": len(accepted),
        }, report_path)
        log(f"GloFAS automatic boundary inflows: wrote {ts_path} and {loc_path}")
        return ts_path, loc_path
    finally:
        dataset.close()


def _download_gtsm_files(
    cfg: Dict[str, Any], paths: Paths, ocfg: Dict[str, Any], start: pd.Timestamp, end: pd.Timestamp,
) -> List[Path]:
    if start.year < 1950 or end.year > 2024:
        raise RuntimeError(
            "GTSM v3 ERA5 reanalysis covers 1950 through 2024. Choose another boundary "
            "source or use a simulation period inside that range."
        )
    months = pd.period_range(start=start.tz_localize(None), end=end.tz_localize(None), freq="M")
    client = _copernicus_client("GTSM", CDS_GTSM_DATASET_URL)
    raw_dir = paths.cache / "gtsm_v3"
    payload_dir = paths.data_file("forcing", "gtsm_v3/payload.placeholder").parent
    ensure_dir(raw_dir); ensure_dir(payload_dir)
    output: List[Path] = []
    for year in sorted({period.year for period in months}):
        selected_months = [f"{period.month:02d}" for period in months if period.year == year]
        raw = raw_dir / f"gtsm_v3_hourly_{year}_{'-'.join(selected_months)}.zip"
        if not file_ready(raw, 512):
            log(f"Downloading GTSM v3 hourly water levels for {year}, months {selected_months}.")
            client.retrieve(CDS_GTSM_DATASET, {
                "variable": ["total_water_level"], "experiment": "reanalysis",
                "temporal_aggregation": [str(ocfg.get("temporal_aggregation", "hourly"))],
                "year": [str(year)], "month": selected_months,
                "version": ["v3"], "format": "zip",
            }, str(raw))
        if zipfile.is_zipfile(raw):
            with zipfile.ZipFile(raw) as archive:
                for member in archive.namelist():
                    if not member.lower().endswith((".nc", ".nc4")):
                        continue
                    destination = payload_dir / Path(member).name
                    if not file_ready(destination, 512):
                        with archive.open(member) as source, destination.open("wb") as target:
                            shutil.copyfileobj(source, target)
                    output.append(destination)
        else:
            output.append(raw)
    if not output:
        raise RuntimeError("The GTSM download contained no NetCDF files.")
    return sorted(set(output))


def prepare_gtsm_ocean_boundary(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths,
) -> Tuple[Path, Path]:
    import xarray as xr  # type: ignore
    ocfg = forcing_cfg(cfg).get("ocean_boundary", {})
    datum = str(ocfg.get("datum", "MSL")).strip().upper()
    offset = ocfg.get("vertical_datum_offset_m")
    if datum != "MSL" and offset in (None, ""):
        raise RuntimeError(
            "GTSM v3 total water level is referenced to its 1986-2005 mean sea level. "
            f"The model boundary datum is {datum}; provide forcing.ocean_boundary."
            "vertical_datum_offset_m or select an MSL model datum."
        )
    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])
    files = _download_gtsm_files(cfg, paths, ocfg, start - pd.Timedelta(hours=1), end + pd.Timedelta(hours=1))
    boundary = gpd.read_file(geo_meta["ocean_path"]).to_crs(4326)
    boundary_geom = unary_union(boundary.geometry.values)
    first = xr.open_dataset(files[0])
    try:
        x_name = _coordinate_name(first, ("station_x_coordinate", "station_lon", "longitude", "lon"))
        y_name = _coordinate_name(first, ("station_y_coordinate", "station_lat", "latitude", "lat"))
        x = np.asarray(first[x_name], dtype=float).reshape(-1)
        y = np.asarray(first[y_name], dtype=float).reshape(-1)
        station_dim = first[x_name].dims[0]
    finally:
        first.close()
    stations = gpd.GeoDataFrame(
        {"station_index": np.arange(len(x), dtype=int), "lon": x, "lat": y},
        geometry=gpd.points_from_xy(x, y), crs=4326,
    )
    max_distance_km = float(ocfg.get("max_station_distance_km", 150.0))
    # Projecting all 43,119 global nodes into one local UTM zone creates
    # invalid geometries on the far side of the world. Restrict projection to
    # a generous regional window, while retaining enough nodes to report the
    # true nearest distance when the configured limit is too small.
    min_lon, min_lat, max_lon, max_lat = boundary_geom.bounds
    diagnostic_distance_km = max(250.0, max_distance_km)
    latitude_pad = diagnostic_distance_km / 110.57
    longitude_pad = diagnostic_distance_km / max(
        20.0, 111.32 * math.cos(math.radians(0.5 * (min_lat + max_lat))))
    regional = stations.loc[
        stations["lon"].between(min_lon - longitude_pad, max_lon + longitude_pad)
        & stations["lat"].between(min_lat - latitude_pad, max_lat + latitude_pad)
    ].copy()
    if regional.empty:
        raise RuntimeError("GTSM contains no station nodes in the ocean boundary region.")
    metric_crs = boundary.estimate_utm_crs() or "EPSG:3857"
    station_metric = regional.to_crs(metric_crs)
    boundary_metric = gpd.GeoSeries([boundary_geom], crs=4326).to_crs(metric_crs).iloc[0]
    regional["distance_km"] = station_metric.geometry.distance(boundary_metric).to_numpy() / 1000.0
    nearest_distance_km = float(regional["distance_km"].min())
    nearby = regional.loc[regional["distance_km"] <= max_distance_km]
    nearby = nearby.sort_values("distance_km").head(int(ocfg.get("max_nodes", 12)))
    if nearby.empty:
        raise RuntimeError(
            f"No GTSM station node is within the configured {max_distance_km:g} km "
            f"distance of the ocean boundary. The nearest node is {nearest_distance_km:.1f} km away; "
            "increase Forcing > Ocean > Station distance and retry."
        )
    indices = nearby["station_index"].astype(int).tolist()
    frames: List[pd.DataFrame] = []
    for path in files:
        with xr.open_dataset(path) as dataset:
            time_name = _coordinate_name(dataset, ("time", "valid_time", "date"))
            variables = [name for name in dataset.data_vars if "waterlevel" in name.lower() or "water_level" in name.lower()]
            water = dataset[variables[0] if variables else next(iter(dataset.data_vars))]
            selected = water.isel({station_dim: indices}).transpose(time_name, station_dim)
            frame = pd.DataFrame(
                np.asarray(selected, dtype=float),
                index=pd.to_datetime(np.asarray(selected[time_name]), utc=True, errors="coerce"),
            )
            frames.append(frame)
    raw = pd.concat(frames).sort_index()
    raw = raw[~raw.index.duplicated(keep="first")]
    raw += float(offset or 0.0)
    expected = pd.date_range(start, end, freq=f"{int(ocfg.get('interval_minutes', 60))}min")
    aligned = raw.reindex(raw.index.union(expected)).interpolate(method="time").reindex(expected)
    missing = aligned.isna().mean(axis=0)
    keep = [column for column in aligned.columns if float(missing[column]) <= float(ocfg.get("max_missing_fraction", 0.10))]
    if not keep:
        raise RuntimeError("GTSM nodes were nearby, but none has sufficient event-period coverage.")
    aligned = aligned[keep]
    accepted = nearby.iloc[[int(column) for column in keep]].copy().reset_index(drop=True)
    accepted["ID"] = np.arange(1, len(accepted) + 1)
    accepted["name"] = accepted["station_index"].map(lambda value: f"GTSM_v3_{value}")
    accepted["provider"] = "gtsm_v3"
    aligned.columns = accepted["ID"].tolist()
    ts_path = paths.data_file("forcing", "gtsm_boundary_waterlevel_m.csv")
    loc_path = paths.gis / "gtsm_boundary_locations.geojson"
    write_hydromt_timeseries_csv(aligned, ts_path)
    accepted[["ID", "name", "provider", "station_index", "distance_km", "geometry"]].to_file(loc_path, driver="GeoJSON")
    write_json({
        "status": "valid", "source": "GTSM v3 ERA5 reanalysis",
        "reference_level": "1986-2005 mean sea level", "vertical_datum_offset_m": float(offset or 0.0),
        "independent_validation": False, "station_count": len(accepted),
    }, paths.result_file("simulation", "gtsm_boundary_report.json"))
    log(f"GTSM ocean boundary forcing: wrote {ts_path} and {loc_path}")
    return ts_path, loc_path


# -----------------------------------------------------------------------------
# Flexible point forcing providers
# -----------------------------------------------------------------------------


def _point_forcing_unit_factor(kind: str, units: str) -> float:
    normalized = units.strip().lower().replace("³", "3").replace(" ", "")
    factors = ({"m": 1.0, "metre": 1.0, "meter": 1.0, "ft": 0.3048,
                "feet": 0.3048, "cm": 0.01, "mm": 0.001}
               if kind == "ocean" else
               {"m3/s": 1.0, "m^3/s": 1.0, "cms": 1.0,
                "ft3/s": 0.028316846592, "ft^3/s": 0.028316846592,
                "cfs": 0.028316846592, "l/s": 0.001})
    if normalized not in factors:
        expected = "m, ft, cm, or mm" if kind == "ocean" else "m3/s, ft3/s (cfs), or L/s"
        raise RuntimeError(f"Unsupported {kind} forcing units {units!r}; use {expected}.")
    return factors[normalized]


def _read_user_point_forcing(ts_value: Any, cfg: Dict[str, Any], kind: str) -> pd.DataFrame:
    """Read wide/long CSV or CF-style point NetCDF into canonical time x point form."""
    src = resolve_user_path(ts_value, f"{kind}.timeseries")
    layout = str(cfg.get("input_layout", "auto")).strip().lower()
    source_units = str(cfg.get("units", "m" if kind == "ocean" else "m3/s"))
    if src.suffix.lower() in {".nc", ".nc4", ".cdf"} or layout == "netcdf":
        import xarray as xr  # type: ignore
        with xr.open_dataset(src) as dataset:
            time_name = next((name for name in ("time", "valid_time", "datetime", "date")
                              if name in dataset.coords or name in dataset.variables), None)
            if not time_name:
                raise RuntimeError(f"{src} has no recognizable time coordinate.")
            tokens = ("water_level", "waterlevel", "level") if kind == "ocean" else ("discharge", "flow")
            candidates = [name for name, variable in dataset.data_vars.items() if time_name in variable.dims]
            if not candidates:
                raise RuntimeError(f"{src} has no time-dependent data variable.")
            variable_name = next((name for name in candidates
                                  if any(token in name.lower() for token in tokens)), candidates[0])
            variable = dataset[variable_name].squeeze(drop=True)
            station_dims = [dimension for dimension in variable.dims if dimension != time_name]
            if len(station_dims) > 1:
                raise RuntimeError(
                    f"{src}:{variable_name} is gridded, not point forcing; expected only time and station dimensions.")
            if station_dims:
                station_dim = station_dims[0]
                variable = variable.transpose(time_name, station_dim)
                station_ids = (dataset[station_dim].values if station_dim in dataset.coords
                               else np.arange(1, variable.sizes[station_dim] + 1))
                values = np.asarray(variable, dtype=float)
            else:
                station_ids = np.asarray([1])
                values = np.asarray(variable, dtype=float).reshape(-1, 1)
            frame = pd.DataFrame(values, index=pd.to_datetime(dataset[time_name].values),
                                 columns=[str(value) for value in station_ids])
            source_units = str(variable.attrs.get("units") or source_units)
    else:
        raw = pd.read_csv(src)
        if raw.empty or len(raw.columns) < 2:
            raise RuntimeError(f"{src} must contain a time column and at least one forcing value.")
        lower = {str(column).strip().lower(): column for column in raw.columns}
        time_column = next((lower[name] for name in ("time", "datetime", "timestamp", "date")
                            if name in lower), raw.columns[0])
        id_names = ("station_id", "source_id", "point_id", "id")
        value_names = (("water_level", "water_level_m", "level", "value") if kind == "ocean"
                       else ("discharge", "discharge_m3s", "flow", "value"))
        id_column = next((lower[name] for name in id_names if name in lower), None)
        value_column = next((lower[name] for name in value_names if name in lower), None)
        long_layout = layout == "long" or (layout == "auto" and id_column is not None and value_column is not None)
        if long_layout:
            if id_column is None or value_column is None:
                raise RuntimeError(
                    f"Long {kind} CSV requires time, station_id/source_id, and "
                    f"{'water_level' if kind == 'ocean' else 'discharge'} columns.")
            raw[value_column] = pd.to_numeric(raw[value_column], errors="coerce")
            frame = raw.pivot_table(index=time_column, columns=id_column, values=value_column,
                                    aggfunc="first", dropna=False)
        else:
            frame = raw.set_index(time_column)
            frame = frame.apply(pd.to_numeric, errors="coerce")
    index = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="coerce"))
    if index.isna().any():
        raise RuntimeError(f"{src} contains invalid timestamps.")
    declared_zone = str(cfg.get("time_zone", "UTC")).strip() or "UTC"
    if index.tz is None:
        try:
            index = index.tz_localize(declared_zone)
        except Exception as exc:
            raise RuntimeError(f"Unsupported or ambiguous forcing time zone {declared_zone!r}.") from exc
    index = index.tz_convert("UTC")
    frame.index = index
    frame = frame.sort_index()
    if frame.index.duplicated().any():
        raise RuntimeError(f"{src} contains duplicate timestamps; resolve them explicitly before building.")
    if frame.columns.astype(str).duplicated().any():
        raise RuntimeError(f"{src} contains duplicate point IDs.")
    if frame.isna().any().any():
        count = int(frame.isna().sum().sum())
        raise RuntimeError(f"{src} contains {count} missing or non-numeric forcing value(s).")
    frame = frame.astype(float) * _point_forcing_unit_factor(kind, source_units)
    if kind == "river" and bool((frame < 0).any().any()):
        raise RuntimeError(f"{src} contains negative river discharge.")
    if kind == "ocean":
        policy = str(cfg.get("datum_policy", "match")).strip().lower()
        if policy == "unknown":
            raise RuntimeError("Ocean file datum is unknown; declare a model-datum match or documented offset.")
        source_datum = str(cfg.get("source_vertical_datum", "")).strip().upper()
        target_datum = str(cfg.get("datum", "")).strip().upper()
        if policy == "match" and source_datum and target_datum and source_datum != target_datum:
            raise RuntimeError(
                f"Ocean source datum {source_datum} does not match model boundary datum {target_datum}; "
                "declare a documented vertical_datum_offset_m.")
        if policy == "offset":
            frame += float(cfg.get("vertical_datum_offset_m", 0.0))
    return frame


def _copy_point_forcing_pair(ts_value: Any, loc_value: Any, paths: Paths, stem: str,
                             cfg: Dict[str, Any], kind: str) -> Tuple[Path, Path]:
    ts = paths.data_file("forcing", f"{stem}_timeseries.csv")
    ensure_dir(ts.parent)
    write_hydromt_timeseries_csv(_read_user_point_forcing(ts_value, cfg, kind), ts)
    loc = copy_user_file(loc_value, paths.gis / f"{stem}_locations.geojson", f"{stem}.locations")
    return ts, loc


def prepare_ocean_boundary(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Path, Path]:
    ocfg = forcing_cfg(cfg).get("ocean_boundary", {})
    source = str(ocfg.get("source", "noaa_coops")).lower()
    if source in {"selected_stations", "catalog_stations"}:
        ts, loc, _ = select_boundary_catalog_stations(cfg, geo_meta, paths)
        return ts, loc
    if source in {"noaa", "noaa_coops", "coops"}:
        # Merge new provider options over legacy NOAA defaults.
        cfg2 = dict(cfg); cfg2["noaa_boundary"] = {**cfg.get("noaa_boundary", {}), **ocfg}
        ts, loc, _ = select_boundary_noaa(cfg2, geo_meta, paths)
        return ts, loc
    if source in {"gtsm", "gtsm_v3"}:
        return prepare_gtsm_ocean_boundary(cfg, geo_meta, paths)
    if source == "file":
        return _copy_point_forcing_pair(
            ocfg.get("timeseries"), ocfg.get("locations") or geo_meta.get("bnd_points_path"),
            paths, "ocean_boundary_user", ocfg, "ocean")
    if source == "constant":
        value = float(ocfg.get("value_m", 0.0))
        loc_src = Path(geo_meta["bnd_points_path"])
        gdf = gpd.read_file(loc_src).reset_index(drop=True)
        ids = list(range(1, len(gdf)+1)); gdf["ID"] = ids
        start = parse_time(cfg["simulation"]["start"]); end = parse_time(cfg["simulation"]["end"])
        idx = pd.DatetimeIndex([start, end])
        df = pd.DataFrame({i: [value, value] for i in ids}, index=idx)
        ts = paths.data_file("forcing", "ocean_boundary_constant.csv"); loc = paths.gis / "ocean_boundary_constant.geojson"
        write_hydromt_timeseries_csv(df, ts); gdf.to_file(loc, driver="GeoJSON")
        return ts, loc
    raise RuntimeError(f"Unsupported forcing.ocean_boundary.source={source!r}; use noaa_coops, selected_stations, gtsm, file, or constant")


def prepare_river_forcing(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Optional[Path], Optional[Path]]:
    rcfg = forcing_cfg(cfg).get("river", {})
    source = str(rcfg.get("source", "usgs_auto")).lower()
    if source == "none": return None, None
    if source in {"usgs_auto", "usgs"}:
        cfg2 = dict(cfg); cfg2["usgs_rivers"] = {**cfg.get("usgs_rivers", {}), **rcfg, "enabled": True}
        return select_usgs_discharge(cfg2, geo_meta, paths)
    if source in {"glofas_auto", "glofas"}:
        return prepare_glofas_river_forcing(cfg, geo_meta, paths)
    if source == "file":
        loc = rcfg.get("locations") or geo_meta.get("river_points_path")
        return _copy_point_forcing_pair(
            rcfg.get("timeseries"), loc, paths, "river_user", rcfg, "river")
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
        ts=paths.data_file("forcing", "usgs_explicit_discharge_m3s.csv"); loc=paths.gis/"usgs_explicit_discharge_locations.geojson"
        write_hydromt_timeseries_csv(pd.concat(series,axis=1),ts); gpd.GeoDataFrame(rows,crs=4326).to_file(loc,driver="GeoJSON")
        return ts,loc
    raise RuntimeError(f"Unsupported forcing.river.source={source!r}; use glofas_auto, usgs_auto, usgs_sites, file, or none")


def prepare_selected_water_level_observations(
    cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths,
) -> Optional[Path]:
    source_path = str(geo_meta.get("observation_points_path", "")).strip()
    if not source_path or not Path(source_path).exists():
        return None
    points = gpd.read_file(source_path).to_crs(4326).reset_index(drop=True)
    rows = [(row, _catalog_station_from_row(row)) for _, row in points.iterrows()]
    selected = [(row, station) for row, station in rows if station is not None]
    if not selected:
        return None

    # A gauge used to force the boundary is not an independent observation.
    # Exclude exact provider/station matches instead of reporting circular skill.
    forcing_stations: set[Tuple[str, str]] = set()
    boundary_path = Path(str(geo_meta.get("bnd_points_path", "")))
    if boundary_path.is_file():
        boundary_points = gpd.read_file(boundary_path).to_crs(4326)
        for _, boundary_row in boundary_points.iterrows():
            station = _catalog_station_from_row(boundary_row)
            if station is not None:
                forcing_stations.add((station[0], station[1]))
    excluded = [station for _, station in selected if (station[0], station[1]) in forcing_stations]
    selected = [
        (row, station) for row, station in selected
        if (station[0], station[1]) not in forcing_stations
    ]
    if not selected:
        outdir = paths.validation
        ensure_dir(outdir)
        write_json({
            "status": "unavailable", "reason": "no_independent_observations",
            "excluded_forcing_stations": [f"{provider}:{station_id}" for provider, station_id, _ in excluded],
        }, outdir / "manifest.json")
        log("Water-level evaluation unavailable: all selected gauges are also boundary-forcing gauges.")
        return None

    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])
    wlcfg = cfg.get("validation", {}).get("water_level", {}) or {}
    min_observations = int(wlcfg.get("min_observations", 4))
    max_missing = float(wlcfg.get("max_missing_fraction", 0.20))
    cfg2 = dict(cfg)
    cfg2["noaa_boundary"] = {
        **cfg.get("noaa_boundary", {}),
        **cfg.get("noaa_validation", {}),
        **(wlcfg.get("noaa", {}) or {}),
    }
    outdir = paths.validation
    ensure_dir(outdir)
    station_rows: List[Dict[str, Any]] = []

    for index, (row, station) in enumerate(selected, start=1):
        provider, station_id, dataset = station
        obs = fetch_selected_water_level(provider, station_id, dataset, start, end, cfg2)
        obs = obs.dropna(subset=["time", "observed_zs_m"]).copy()
        if len(obs) < min_observations:
            raise RuntimeError(
                f"Selected {provider} observation station {station_id} has only {len(obs)} usable values; need {min_observations}."
            )
        interval = 6 if provider == "noaa_coops" else 60
        expected_count = max(1, int(math.floor((end - start).total_seconds() / (60 * interval))) + 1)
        coverage = min(1.0, pd.to_datetime(obs["time"], utc=True).nunique() / expected_count)
        if coverage < 1.0 - max_missing:
            raise RuntimeError(
                f"Selected {provider} observation station {station_id} event coverage {coverage:.1%} is below {1.0-max_missing:.1%}."
            )
        safe_station = re.sub(r"[^A-Za-z0-9_.-]+", "_", station_id).strip("_") or "station"
        obs_path = outdir / f"obs_{provider}_{safe_station}.csv"
        obs.to_csv(obs_path, index=False)
        station_rows.append({
            "ID": index, "provider": provider, "station_id": station_id,
            "provider_station_id": station_id,
            "name": _station_text(row, "name", "ID") or station_id,
            "country": _station_text(row, "state", "country"),
            "lon": float(row.geometry.x), "lat": float(row.geometry.y),
            "dataset": dataset, "datum": _station_text(row, "datum"),
            "observation_path": str(obs_path), "n_observations": int(len(obs)),
            "coverage_fraction": float(coverage), "geometry": row.geometry,
        })

    stations = gpd.GeoDataFrame(station_rows, geometry="geometry", crs=4326)
    station_file = outdir / "stations.geojson"
    stations.to_file(station_file, driver="GeoJSON")
    obs_locations = paths.gis / "noaa_validation_obs.geojson"
    stations[["ID", "name", "provider", "provider_station_id", "geometry"]].to_file(
        obs_locations, driver="GeoJSON")
    manifest = {
        "status": "ready", "mode": "gui_selected_stations",
        "stations_file": str(station_file), "n_stations": len(station_rows),
        "stations": [{k: v for k, v in item.items() if k != "geometry"} for item in station_rows],
        "note": "Exact pre-simulation water-level stations selected in the study-area builder.",
    }
    write_json(manifest, outdir / "manifest.json")
    log(f"Prepared {len(station_rows)} selected water-level observation station(s): {station_file}")
    return obs_locations


def prepare_validation_observations(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Optional[Path]:
    selected = prepare_selected_water_level_observations(cfg, geo_meta, paths)
    if selected is not None:
        return selected
    manifest = read_json(paths.validation / "manifest.json", {})
    if manifest.get("status") == "unavailable" and manifest.get("reason") == "no_independent_observations":
        return None
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
            copy_user_file(
                vcfg.get("timeseries"), paths.data_file("validation", "validation_observations.csv"),
                "evaluation.observations.timeseries")
        return loc
    raise RuntimeError(f"Unsupported evaluation.observations.source={source!r}; use noaa_coops, file, or none")

# -----------------------------------------------------------------------------
# ERA5
# -----------------------------------------------------------------------------


def era5_artifact_paths(
    cfg: Dict[str, Any], bbox: Iterable[float], paths: Paths,
    start: pd.Timestamp, forcing_end: pd.Timestamp,
) -> Tuple[Path, Path, Path, Path]:
    """Derive collision-safe ERA5 files for one event, AOI, and time window."""
    acfg = era5_config(cfg)
    era_dir = paths.cache / acfg.get("cache_subdir", "era5")
    event = _slug(cfg.get("simulation", {}).get("event_name", "event"))
    era_signature = _bbox_signature(
        bbox, start.isoformat(), forcing_end.isoformat(),
        acfg.get("cds_dataset", ""),
        json.dumps(acfg.get("variables", {}), sort_keys=True, default=str),
    )
    period = f"{start:%Y%m%d%H}_{forcing_end:%Y%m%d%H}"
    return (
        era_dir / f"era5_{event}_{period}_{era_signature}_wind_pressure_raw.nc",
        era_dir / f"era5_{event}_{period}_{era_signature}_precip_raw.nc",
        paths.data_file("forcing", f"era5_{period}_{era_signature}_wind_pressure_pa.nc"),
        paths.data_file("forcing", f"era5_{period}_{era_signature}_precip_mm_cumulative.nc"),
    )


def download_era5(cfg: Dict[str, Any], geo_meta: Dict[str, Any], paths: Paths) -> Tuple[Path, Path]:
    try:
        import cdsapi  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "ERA5 requires the Copernicus CDS API client, but 'cdsapi' is not installed "
            "in this AutoCF runtime. Select AutoCF Settings > Install / repair runtime, "
            "then try again. CDS setup guide: " + CDS_API_SETUP_URL
        ) from exc
    import xarray as xr  # type: ignore

    rc_value = os.environ.get("CDSAPI_RC", "").strip()
    rc_path = Path(rc_value).expanduser() if rc_value else Path.home() / ".cdsapirc"
    has_environment_credentials = bool(
        os.environ.get("CDSAPI_URL", "").strip()
        and os.environ.get("CDSAPI_KEY", "").strip()
    )
    if not has_environment_credentials and not rc_path.is_file():
        raise RuntimeError(
            "ERA5 requires personal Copernicus Climate Data Store credentials. "
            f"No CDS credential file was found at {rc_path}. Sign in or register, accept "
            "the ERA5 single-levels dataset Terms of Use, and copy the two lines shown "
            "by the CDS setup page into a file named .cdsapirc in your user home folder. "
            f"CDS setup: {CDS_API_SETUP_URL} | ERA5 dataset: {CDS_ERA5_DATASET_URL}"
        )

    acfg = era5_config(cfg)
    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])
    forcing_end = atmospheric_forcing_end(cfg)
    bbox = bbox_buffer_degrees(geo_meta["bbox"], float(acfg.get("spatial_buffer_degrees", 0.5)))
    west, south, east, north = bbox
    era_dir = paths.cache / acfg.get("cache_subdir", "era5")
    ensure_dir(era_dir)
    # Every AOI/time/provider combination gets distinct raw and processed files.
    # Fixed event filenames can silently pass file_ready() after the event dates
    # change, causing a different storm's atmospheric forcing to be reused.
    raw_ws, raw_tp, proc_ws, proc_tp = era5_artifact_paths(
        cfg, bbox, paths, start, forcing_end)

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

    try:
        c = cdsapi.Client()
    except Exception as exc:
        raise RuntimeError(
            "AutoCF found the CDS API client and credential file but could not initialize "
            "ERA5 access. Replace .cdsapirc with the current two-line configuration from "
            f"{CDS_API_SETUP_URL}; the key must be your current personal access token. "
            "Also accept the ERA5 single-levels dataset Terms of Use. "
            f"Original CDS error: {exc}"
        ) from exc
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


def hydromt_scaled_raster_options(path: Path) -> Dict[str, Any]:
    """Preserve a GeoTIFF band scale/offset with HydroMT 0.9.x.

    HydroMT 0.9.x rejects scaled rasters unless ``mask_nodata`` is enabled,
    while its raster reader then drops the scale/offset metadata instead of
    applying it. Pass the same conversion explicitly through the adapter so
    integer-packed elevations (including GEDTM30) remain in metres.
    """
    import rasterio  # type: ignore

    options: Dict[str, Any] = {
        # HydroMT-SFINCS clips these rasters repeatedly to subgrid blocks. If
        # the source is one Dask chunk, loading any clipped block evaluates the
        # entire regional raster (multi-billion pixels for large domains).
        "driver_kwargs": {"chunks": {"x": 1024, "y": 1024}},
    }
    with rasterio.open(path) as src:
        if src.count != 1:
            return options
        scale = float(src.scales[0])
        offset = float(src.offsets[0])
    if math.isclose(scale, 1.0) and math.isclose(offset, 0.0):
        return options
    variable = path.stem
    log(
        "HydroMT scaled-raster compatibility: "
        f"{path.name} | scale={scale:g}, offset={offset:g}"
    )
    options["driver_kwargs"]["mask_nodata"] = True
    options.update({
        "unit_mult": {variable: scale},
        "unit_add": {variable: offset},
    })
    return options


def prepare_additional_topobathy(cfg, paths, state, geo_meta, bbox, model_crs,
                                 primary, primary_signature):
    """Prepare ordered sources 3+ using the same warp/datum policy as Source 2."""
    tcfg = cfg.get("topobathy", {})
    sections = tcfg.get("additional_sources", []) or []
    target = normalize_vertical_datum(tcfg.get("target_vertical_datum", "NAVD88"))
    selection = _bbox_signature(bbox, json.dumps(sections, sort_keys=True), primary_signature,
                               model_crs, target, tcfg.get("datum_engine", "auto"))
    prior = state.data.get("meta", {}).get("additional_topobathy", {})
    if prior.get("source_signature") != selection:
        state.data.setdefault("done", {})["hydromt_build"] = False
    prepared = []
    for index, section in enumerate(sections, 3):
        source = str(section.get("source", "")).strip().lower()
        if source == "none":
            continue
        native = source_vertical_datum(section)
        if native == "UNKNOWN":
            raise RuntimeError(f"Terrain Source {index}: native vertical datum is required.")
        resolution = effective_topobathy_resolution_m(cfg, float(section.get("output_resolution_m", 30)))
        resolution, _, _ = memory_safe_raster_resolution_m(bbox, model_crs, resolution)
        tag = re.sub(r"[^a-z0-9]+", "_", source).strip("_") or "custom"
        key = f"topobathy_{index:02d}"
        raster = paths.data_file("terrain", f"{key}_{tag}_model_crs_{resolution_tag(resolution)}m.tif")
        signature = _bbox_signature(bbox, topobathy_source_signature(section, bbox, key),
                                    primary_signature, model_crs, resolution, target,
                                    tcfg.get("datum_engine", "auto"))
        old = state.data.get("meta", {}).get(key, {})
        metadata = source_datum_metadata(section)
        metadata.update(index=index, temporal=source_temporal_metadata(
            section, (cfg.get("simulation", {}) or {}).get("start")))
        if (old.get("source_signature") == signature and file_ready(Path(old.get("path", "")), 1)
                and raster_has_valid_pixels(Path(old["path"]))):
            raster = Path(old["path"])
            metadata = old.get("metadata", metadata)
            log(f"Skipping Terrain Source {index}; already prepared: {raster}")
        else:
            if source in LOCAL_TOPOBATHY_SOURCES:
                local = Path(str(section.get("path", ""))).expanduser().resolve()
                if not local.is_file():
                    raise RuntimeError(f"Terrain Source {index}: raster does not exist: {local}")
                input_path = str(local)
            elif source == "noaa_cudem_1_9_arcsec":
                vrt, _ = prepare_noaa_cudem_1_9_tiles(
                    paths, Path(geo_meta["domain_path"]), bbox,
                    float(tcfg.get("buffer_km", 10)), model_crs)
                input_path = str(vrt)
            else:
                url = str(section.get("cog_url") or section.get("vrt_url")
                          or BUILTIN_PRIMARY_TOPOBATHY_URLS.get(source)
                          or BUILTIN_GAPFILL_TOPOBATHY_URLS.get(source) or "").strip()
                if not url:
                    raise RuntimeError(f"Terrain Source {index}: a remote VRT/COG URL is required.")
                input_path = url if url.startswith("/vsi") else "/vsicurl/" + url
            gdalwarp_remote(input_path, raster, bbox, model_crs, resolution, force=True,
                            progress_message=f"Terrain Source {index} download/reprojection is active.")
            if not raster_has_valid_pixels(raster):
                raise RuntimeError(f"Terrain Source {index} produced no valid pixels for this study area.")
            if not vertical_datums_equivalent(native, target):
                if str(tcfg.get("datum_policy", "align_gapfill")).lower() != "align_gapfill":
                    raise RuntimeError(f"Terrain Source {index}: {native} differs from model datum {target}.")
                aligned = paths.data_file("terrain", f"{key}_{tag}_aligned_{target.lower()}_{resolution_tag(resolution)}m.tif")
                metadata["alignment"] = harmonize_gapfill_to_target(
                    primary, raster, aligned, target_datum=target,
                    primary_datum=target, gapfill_datum=native,
                    engine=tcfg.get("datum_engine", "auto"),
                    configured_offset_m=section.get("vertical_datum_offset_m"),
                    cache_dir=paths.cache / "transformez")
                raster = aligned
            else:
                metadata["alignment"] = {"method": "none-required", "offset_m": 0.0,
                                         "source_datum": native, "target_datum": target}
            state.mark(key, path=str(raster), source_signature=signature, metadata=metadata)
            state.data.setdefault("done", {})["hydromt_build"] = False
        log(f"Terrain Source {index}: {raster}; fills only gaps left by earlier sources.")
        if metadata.get("alignment", {}).get("quality") == "approximate":
            log(f"WARNING: Terrain Source {index} uses approximate datum alignment; inspect provenance.")
        prepared.append(dict(index=index, source=source, path=str(raster), metadata=metadata))
    state.mark("additional_topobathy", source_signature=selection, sources=prepared)
    return prepared


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
    primary_grid_tif: Optional[Path] = None,
) -> Path:
    primary_grid_tif = primary_grid_tif or cudem_tif
    model_datum = normalize_vertical_datum(
        cfg.get("topobathy", {}).get("target_vertical_datum", "NAVD88")
    )
    elevation_unit = datum_unit_label(model_datum)
    catalog = {
        "meta": {"version": "1", "root": str(paths.out)},
        "topobathy_primary": {
            "data_type": "RasterDataset",
            "path": str(cudem_tif),
            "driver": "raster",
            "filesystem": "local",
            "crs": None,
            "meta": {"category": "topography", "unit": elevation_unit, "vertical_datum": model_datum, "source": "configured primary topobathy"},
            **hydromt_scaled_raster_options(cudem_tif),
        },
        "topobathy_primary_grid": {
            "data_type": "RasterDataset",
            "path": str(primary_grid_tif),
            "driver": "raster",
            "filesystem": "local",
            "crs": None,
            "meta": {"category": "topography", "unit": elevation_unit, "vertical_datum": model_datum, "source": "primary topobathy at model-grid resolution"},
            **hydromt_scaled_raster_options(primary_grid_tif),
        },
        "gebco_500m": {
            "data_type": "RasterDataset",
            "path": str(gebco_tif),
            "driver": "raster",
            "filesystem": "local",
            "crs": None,
            "meta": {"category": "topography", "unit": elevation_unit, "vertical_datum": model_datum, "source": "configured aligned gap-fill topobathy"},
            **hydromt_scaled_raster_options(gebco_tif),
        },
        "esa_worldcover": {
            "data_type": "RasterDataset",
            "path": str(esa_tif),
            "driver": "raster",
            "filesystem": "local",
            "driver_kwargs": {"chunks": {"x": 1024, "y": 1024}},
            "meta": {"category": "landuse", "source": "ESA WorldCover 2021 v200"},
        },
        "esa_worldcover_mapping": {
            "data_type": "DataFrame",
            "path": str(manning_csv),
            "driver": "csv",
            "meta": {"category": "landuse", "notes": "Manning n by ESA WorldCover class"},
        },
    }
    for extra in cfg.get("_additional_topobathy", []):
        extra_path = Path(extra["path"])
        catalog[f"topobathy_{extra['index']:02d}"] = {
            "data_type": "RasterDataset", "path": str(extra_path), "driver": "raster",
            "filesystem": "local", "crs": None,
            "meta": {"category": "topography", "unit": elevation_unit,
                     "vertical_datum": model_datum, "source": extra["source"]},
            **hydromt_scaled_raster_options(extra_path),
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
    # GUI-generated/legacy configurations may omit the optional backend-only
    # HydroMT section. Every value used below already has an AutoCF default.
    hc = cfg.get("hydromt", {}) or {}
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
        {"elevtn": "topobathy_primary_grid", "merge_method": "first", "reproj_method": "bilinear"},
    ]
    subgrid_dep_sources = [
        {"elevtn": "topobathy_primary", "merge_method": "first", "reproj_method": "bilinear"},
    ]
    if str(cfg.get("topobathy", {}).get("gapfill", {}).get("source", "GEBCO_2024_15_ARCSEC")).lower() != "none":
        dep_sources.append({"elevtn": "gebco_500m", "merge_method": "first", "reproj_method": "bilinear"})
        subgrid_dep_sources.append({"elevtn": "gebco_500m", "merge_method": "first", "reproj_method": "bilinear"})
    for extra in cfg.get("_additional_topobathy", []):
        entry = {"elevtn": f"topobathy_{extra['index']:02d}", "merge_method": "first", "reproj_method": "bilinear"}
        dep_sources.append(dict(entry)); subgrid_dep_sources.append(dict(entry))

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
            "datasets_dep": [dict(d) for d in subgrid_dep_sources],
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

    waterlevel_buffer_m = float(hc.get("boundary_point_spacing_m", 5000.0))
    ocean_source = str(
        forcing_cfg(cfg).get("ocean_boundary", {}).get("source", "noaa_coops")
    ).lower()
    if ocean_source in {"gtsm", "gtsm_v3"}:
        # GTSM nodes are selected using max_station_distance_km above, but
        # HydroMT independently clips supplied point locations around the
        # generated boundary mask.  A fixed 5 km clip therefore discarded
        # valid GTSM nodes (the nearest Idai node is 5.23 km away).  Derive the
        # clip from the nodes that were actually accepted, with a small grid
        # tolerance for the difference between the drawn line and mask cells.
        location_doc = read_json(wl_locs, {})
        accepted_distances_m = [
            float(feature.get("properties", {}).get("distance_km")) * 1000.0
            for feature in location_doc.get("features", [])
            if feature.get("properties", {}).get("distance_km") is not None
        ]
        if accepted_distances_m:
            grid_tolerance_m = max(2000.0, 2.0 * float(mesh.get("dx_m", 200.0)))
            waterlevel_buffer_m = max(
                waterlevel_buffer_m,
                max(accepted_distances_m) + grid_tolerance_m,
            )

    build["setup_waterlevel_forcing"] = {
        "timeseries": str(wl_ts),
        "locations": str(wl_locs),
        "buffer": waterlevel_buffer_m,
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
            (cfg.get("hydromt", {}) or {}).get("build_verbosity", "-vv"),
        ]
    else:
        cmd = [
            "hydromt", "build", "sfincs", str(paths.out),
            "-r", "{'geom': '" + geo_meta["domain_path"] + "'}",
            "-i", str(build_cfg),
            "-d", str(catalog),
            (cfg.get("hydromt", {}) or {}).get("build_verbosity", "-vv"),
        ]
    # A failed HydroMT build can leave partial model files. Since this function
    # is only called while state['hydromt_build'] is false, overwrite those
    # partial HydroMT model files and rebuild the stage deterministically.
    partial_model = (
        file_ready(sfincs_inp, 100)
        or (paths.out / "gis").exists()
        or (paths.out / "subgrid").exists()
        or any(paths.out.glob("sfincs.*"))
    )
    if partial_model:
        log("Incomplete previous HydroMT build detected while hydromt_build is not complete.")
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
    the simulation results tables folder and only rewrites sfincs.obs when
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
    out = paths.result_file("simulation", "noaa_obs_auto_snap_report.csv")
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


def run_process_with_pty_tee(
    command: List[str], cwd: Path, env: Dict[str, str], stream: Any,
) -> int:
    """Run a console program on a PTY and immediately tee its native output."""
    import pty

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)

    try:
        while True:
            try:
                data = os.read(master_fd, 65536)
            except OSError as exc:
                # Linux PTY masters report EIO after the child closes its slave.
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            text = data.decode("utf-8", errors="replace")
            stream.write(text)
            stream.flush()
            sys.stdout.write(text)
            sys.stdout.flush()
    finally:
        os.close(master_fd)

    return int(proc.wait())


def run_sfincs_direct(cfg: Dict[str, Any], paths: Paths) -> None:
    """Run the SFINCS executable directly in the case folder.

    This is intentionally not a Slurm/sbatch workflow. The Windows package runs
    the bundled OpenMP SFINCS executable directly on the local CPU.
    Every explicit simulation run starts SFINCS and replaces prior hydraulic
    outputs. Prepared terrain, forcing, and model-build inputs remain reusable.
    """
    exe = Path(str(cfg["sfincs"]["executable"])).expanduser()
    if not exe.exists():
        raise RuntimeError(f"SFINCS executable not found: {exe}")

    inp = paths.out / str(cfg["sfincs"].get("input_file", "sfincs.inp"))
    if not inp.exists():
        raise RuntimeError(f"SFINCS input file not found: {inp}")

    map_nc = paths.out / "sfincs_map.nc"
    his_nc = paths.out / "sfincs_his.nc"

    existing_outputs = [path for path in (map_nc, his_nc) if path.exists()]
    if existing_outputs:
        log(
            "Explicit Run requested: replacing existing SFINCS hydraulic output(s): "
            + ", ".join(path.name for path in existing_outputs)
        )
        for path in existing_outputs:
            path.unlink()

    log("Running SFINCS directly from Python. No Slurm/sbatch is used.")
    log(f"Case directory: {paths.out}")
    log(f"Executable    : {exe}")

    log_file = (
        paths.layout.logs / "simulation" / "run_cpu.log"
        if paths.layout.organized else paths.out / "run_cpu.log"
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("w", encoding="utf-8") as f:
        f.write("Running SFINCS directly from Python\n")
        f.write(f"Executable: {exe}\n")
        f.write(f"Working directory: {paths.out}\n")
        f.flush()

        returncode = run_process_with_pty_tee(
            [str(exe)],
            paths.out,
            os.environ.copy(),
            f,
        )

    if returncode != 0:
        raise RuntimeError(f"SFINCS direct run failed with status {returncode}. See {log_file}")

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
    ocean_source = _provider_source(cfg, "ocean_boundary", "noaa_coops")
    require_boundary_points = ocean_source not in {"gtsm", "gtsm_v3"}

    layout = run_layout(cfg)
    run = layout.root
    out = layout.model
    cache = Path(cfg["project"]["cache_dir"]).expanduser().resolve()
    paths = Paths(
        run=run,
        out=out,
        cache=cache,
        work=layout.work,
        gis=layout.data_path("geospatial", "placeholder.geojson").parent,
        validation=layout.validation,
        state=layout.state,
        layout=layout,
    )
    layout.ensure_base_directories()
    for p in [paths.out, paths.cache, paths.work, paths.gis, paths.validation]:
        ensure_dir(p)
    state = State(paths.state)

    def finish() -> int:
        refresh_artifact_catalog(layout)
        return 0
    log(f"Configuration: {cfg_path.resolve()}")
    log(f"SFINCS hydraulic mode: {model_mode}")
    log(f"Run directory: {paths.run}")
    log(f"SFINCS model directory: {paths.out}")

    geo_source_sig = source_file_signature(cfg.get("geojson", {}).get("path", ""))
    geo_meta_state = state.data.setdefault("meta", {}).setdefault("geojson", {})
    old_geo_sig = geo_meta_state.get("source_signature")
    old_component_sigs = geo_meta_state.get("component_signatures", {}) or {}
    geo_source_path = Path(str(cfg.get("geojson", {}).get("path", ""))).expanduser().resolve()
    try:
        geo_document = json.loads(geo_source_path.read_text(encoding="utf-8"))
        geo_features = geo_document.get("features", []) if isinstance(geo_document, dict) else []
        component_sigs = geojson_component_signatures(geo_document)
        has_new_overrides = any(
            str((feature.get("properties") or {}).get("role", "")).lower() == "roughness_override"
            for feature in geo_features if isinstance(feature, dict))
    except Exception:
        component_sigs = {}
        has_new_overrides = False
    geo_changed = state.is_done("geojson") and (
        (old_geo_sig is not None and old_geo_sig != geo_source_sig) or
        (old_geo_sig is None and has_new_overrides))
    domain_changed = bool(
        geo_changed
        and old_component_sigs.get("domain")
        and component_sigs.get("domain")
        and old_component_sigs["domain"] != component_sigs["domain"]
    )
    if state.is_done("geojson") and old_geo_sig is None and not geo_changed:
        # One-time migration for runs created before source signatures existed.
        geo_meta_state["source_signature"] = geo_source_sig
        geo_meta_state["component_signatures"] = component_sigs
        write_json(state.data, state.path)
    elif geo_changed:
        if domain_changed:
            log("Study-area domain changed; invalidating terrain, surface, forcing, and model preparation.")
        else:
            log("Study-area annotations changed; retaining terrain/surface rasters and rebuilding dependent forcing/model stages.")
        for key in ("geojson", "ocean_boundary", "validation_observations",
                    "river_forcing", "hydromt_build"):
            state.data.setdefault("done", {})[key] = False
        if domain_changed:
            for key in ("cudem", "cudem_grid", "gebco", "esa_worldcover",
                        "era5", "aorc", "atmosphere_forcing"):
                state.data.setdefault("done", {})[key] = False
        geo_meta_state["source_signature"] = geo_source_sig
        geo_meta_state["component_signatures"] = component_sigs
        write_json(state.data, state.path)
    elif component_sigs and old_component_sigs != component_sigs:
        # Upgrade state created before component-level signatures without
        # invalidating already prepared rasters.
        geo_meta_state["component_signatures"] = component_sigs
        write_json(state.data, state.path)

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

    rough_sig = hashlib.sha256(
        f"{roughness_signature(cfg)}|{component_sigs.get('roughness_override', '')}".encode("utf-8")
    ).hexdigest()[:16]
    old_rough_sig = state.data.get("meta", {}).get("hydromt_build", {}).get("roughness_signature")
    if state.is_done("hydromt_build") and old_rough_sig != rough_sig:
        log(
            "Land-cover or Manning roughness configuration changed; invalidating only the HydroMT build stage. "
            "Downloaded/preprocessed data are retained."
        )
        state.data.setdefault("done", {})["hydromt_build"] = False
        write_json(state.data, state.path)

    forc_sig = forcing_signature(cfg)
    state_meta = state.data.setdefault("meta", {})
    old_forc_sig = (
        state_meta.get("workflow", {}).get("forcing_signature")
        or state_meta.get("hydromt_build", {}).get("forcing_signature")
    )
    forcing_stage_keys = (
        "ocean_boundary", "validation_observations", "river_forcing",
        "era5", "aorc", "atmosphere_forcing", "hydromt_build",
    )
    # Do not tie invalidation only to a completed HydroMT build. A failed build
    # can still leave NOAA/USGS stages marked complete; changing from one storm
    # window to another must invalidate those intermediate products too.
    if any(state.is_done(key) for key in forcing_stage_keys) and old_forc_sig != forc_sig:
        reason = "changed" if old_forc_sig else "cannot be verified for this configuration"
        log(
            f"Forcing/evaluation state {reason}; invalidating forcing-dependent stages "
            "while retaining provider downloads."
        )
        for key in forcing_stage_keys:
            state.data.setdefault("done", {})[key] = False
    workflow_meta = state_meta.setdefault("workflow", {})
    if workflow_meta.get("forcing_signature") != forc_sig:
        workflow_meta["forcing_signature"] = forc_sig
        workflow_meta["forcing_signature_time_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(state.data, state.path)

    mode = cfg.get("workflow", {}).get("mode", "build_only")
    allowed_modes = {"build_only", "run_direct_only", "build_run_direct", "topobathy_only", "surface_only", "atmosphere_only"}
    if mode not in allowed_modes:
        raise RuntimeError(
            f"Unsupported workflow.mode={mode!r}. Use one of: "
            "build_only, run_direct_only, build_run_direct, topobathy_only, surface_only, atmosphere_only."
        )

    if mode == "run_direct_only":
        built_rough_sig = state.data.get("meta", {}).get("hydromt_build", {}).get("roughness_signature")
        if built_rough_sig != rough_sig or not state.is_done("hydromt_build"):
            raise RuntimeError(
                "run_direct_only requested, but the current land-cover/Manning configuration has not been built. "
                "Set workflow.mode: build_only first."
            )
        if infiltration_enabled(cfg):
            built_sig = state.data.get("meta", {}).get("hydromt_build", {}).get("infiltration_signature")
            if built_sig != inf_sig or not state.is_done("hydromt_build"):
                raise RuntimeError("run_direct_only requested, but the current infiltration configuration has not been built. Set workflow.mode: build_only first.")
        built_forc_sig = state.data.get("meta", {}).get("hydromt_build", {}).get("forcing_signature")
        if built_forc_sig != forc_sig or not state.is_done("hydromt_build"):
            raise RuntimeError("run_direct_only requested, but the current forcing configuration has not been built. Set workflow.mode: build_only first.")
        geo_meta = read_json(paths.result_file("simulation", "geojson_report.json"), {})
        if not geo_meta:
            geo_meta = extract_geojson_parts(
                Path(cfg["geojson"]["path"]), paths,
                require_boundary_points=require_boundary_points,
            )
        validate_built_model(cfg, paths)
        snap_noaa_observation_points_to_wet_cells(paths)
        run_sfincs_direct(cfg, paths)
        return finish()

    # Atmospheric preparation needs only the Python data stack. Other build
    # modes additionally require the raster and HydroMT command-line tools.
    if mode != "atmosphere_only":
        require_executable("gdalwarp")
        require_executable("gdalbuildvrt")
        require_executable("hydromt")
        if infiltration_enabled(cfg) and str(cfg.get("infiltration",{}).get("method","cn_with_recovery")).lower()=="cn_with_recovery":
            require_executable("gdal_rasterize")

    if not state.is_done("geojson"):
        geo_meta = extract_geojson_parts(
            Path(cfg["geojson"]["path"]), paths,
            require_boundary_points=require_boundary_points,
        )
        state.mark(
            "geojson", source_signature=geo_source_sig,
            component_signatures=component_sigs, **geo_meta,
        )
    else:
        geo_meta = read_json(paths.result_file("simulation", "geojson_report.json"), {})
        log("Skipping GeoJSON parsing; already complete.")

    if mode == "atmosphere_only":
        wind_grid, pressure_grid, precip_grid, validation = prepare_atmospheric_forcing(
            cfg, geo_meta, paths, state
        )
        write_forcing_provenance(cfg, paths, {
            "ocean_timeseries": None, "ocean_locations": None,
            "river_timeseries": None, "river_locations": None,
            "wind": wind_grid, "pressure": pressure_grid, "precipitation": precip_grid,
        })
        log("Atmosphere-only mode complete. SFINCS was not built or run.")
        log("Validated components: " + ", ".join(validation["components"]))
        log(f"Validation report: {paths.result_file('simulation', 'atmosphere_forcing_validation.json')}")
        return finish()

    model_crs = f"EPSG:{geo_meta.get('auto_utm_epsg')}" if cfg["mesh"].get("crs") == "utm" else str(cfg["mesh"].get("crs"))

    if mode == "surface_only":
        # Prepare only the inputs represented on the Roughness & Infiltration
        # map. This action must not trigger elevation or forcing downloads.
        lcfg = cfg.get("landcover", {})
        lsource = str(lcfg.get("source", "ESA_WorldCover_2021_v200")).lower()
        if lsource == "file":
            esa = copy_user_file(
                lcfg.get("path"), paths.data_file("surface", "user_landcover.tif"),
                "landcover.path")
        elif "nlcd" in lsource:
            esa = download_annual_nlcd_landcover(cfg, geo_meta, paths, landcover_config=lcfg)
        else:
            if model_mode == "subgrid":
                lc_res = float(cfg.get("subgrid", {}).get("landcover_output_resolution_m", 10.0))
                esa = paths.data_file("surface", f"esa_worldcover_model_crs_subgrid_{lc_res:g}m.tif")
            else:
                esa = paths.data_file("surface", "esa_worldcover_model_crs.tif")
            if (not state.is_done("esa_worldcover")) or (not file_ready(esa, 1024)):
                esa = download_esa_worldcover(
                    cfg, geo_meta["bbox"], paths, model_crs,
                    domain_path=Path(geo_meta["domain_path"]),
                )
                state.mark("esa_worldcover", path=str(esa))
            else:
                log(f"Skipping ESA WorldCover; already complete: {esa}")
        manning_csv = prepare_manning_table(lcfg, paths)
        esa, manning_csv = apply_roughness_overrides(esa, manning_csv, geo_meta, paths)
        infiltration_inputs = prepare_infiltration_inputs(cfg, geo_meta, paths, model_crs)
        log("Surface-only mode complete. Roughness and infiltration inputs are prepared for the build.")
        log(f"Land-cover / roughness raster: {esa}")
        log(f"Manning lookup: {manning_csv}")
        if infiltration_inputs:
            for key, value in infiltration_inputs.items():
                if isinstance(value, Path):
                    log(f"Infiltration {key}: {value}")
        else:
            log("Infiltration: disabled")
        return finish()

    bbox_topo = bbox_buffer_km(geo_meta["bbox"], float(cfg["topobathy"].get("buffer_km", 10)))

    # Topobathy provider: default CUDEM + GEBCO, or user rasters. Never prepare
    # elevation more finely than the configured SFINCS sampling lattice: those
    # extra pixels cannot be used by the model and can exhaust WSL memory.
    tcfg = cfg.get("topobathy", {})
    target_datum = normalize_vertical_datum(tcfg.get("target_vertical_datum", "NAVD88"))
    primary_datum = source_vertical_datum(tcfg.get("primary", {}))
    if primary_datum == "UNKNOWN":
        raise RuntimeError(
            "topobathy.primary.vertical_datum is required for a user or unknown elevation source."
        )
    if not vertical_datums_equivalent(primary_datum, target_datum):
        raise RuntimeError(
            f"Primary topobathy is {primary_datum}, but the model target is {target_datum}. "
            "AutoCF never relabels a primary raster. Choose the source's native datum or use a pre-converted raster."
        )
    log(f"Vertical datum: model={target_datum}; primary source={primary_datum}")
    requested_primary_res = float(tcfg.get("primary", {}).get("output_resolution_m", 3))
    model_useful_primary_res = effective_topobathy_resolution_m(cfg, requested_primary_res)
    primary_res, requested_primary_plan, primary_plan = memory_safe_raster_resolution_m(
        bbox_topo, model_crs, model_useful_primary_res,
    )
    grid_res = float(cfg["mesh"]["dx_m"])
    log(
        "Primary topobathy resolution: "
        f"requested={requested_primary_res:g} m, "
        f"model-useful={model_useful_primary_res:g} m, "
        f"prepared={primary_res:g} m, "
        f"grid-mask={grid_res:g} m"
    )
    runtime_adjustment = (cfg.get("runtime", {}) or {}).get("automatic_raster_memory_adjustment", {})
    safety_report = {
        "policy": "configured_resolution_preserved",
        "pixel_limit": PREPARED_RASTER_PIXEL_LIMIT,
        "requested_source_resolution_m": requested_primary_res,
        "model_useful_resolution_m": model_useful_primary_res,
        "prepared_resolution_m": primary_res,
        "adjusted": primary_res != model_useful_primary_res,
        "requested_plan": requested_primary_plan,
        "prepared_plan": primary_plan,
        "mesh_dx_m": grid_res,
        "model_mode": model_mode,
        "configured_subgrid_pixels": int(cfg.get("subgrid", {}).get("nr_subgrid_pixels", 20)),
        "runtime_adjustment": runtime_adjustment,
        "note": (
            "AutoCF preserves the configured mesh, subgrid samples, and prepared "
            "source-raster resolution. Large workloads are reported but are not "
            "automatically coarsened."
        ),
    }
    safety_report_path = paths.result_file(
        "simulation", "terrain_resolution_safety.json",
    )
    write_json(safety_report, safety_report_path)
    if safety_report["adjusted"] or runtime_adjustment.get("adjusted"):
        log(f"Terrain memory-safety report: {safety_report_path}")
    psrc = str(tcfg.get("primary", {}).get("source", "NOAA_CUDEM_1_9_ARCSEC")).lower()
    primary_input: str
    if psrc == "noaa_cudem_1_9_arcsec":
        primary_input = "NOAA CUDEM 1/9 managed local tiles"
        primary_tag = re.sub(r"[^a-z0-9]+", "_", psrc).strip("_")
        cudem = paths.data_file(
            "terrain", f"topobathy_01_{primary_tag}_model_crs_{resolution_tag(primary_res)}m.tif")
    elif psrc in LOCAL_TOPOBATHY_SOURCES:
        source_path = Path(str(tcfg["primary"].get("path", ""))).expanduser().resolve()
        if not source_path.is_file():
            raise RuntimeError(f"topobathy.primary.path does not exist: {source_path}")
        primary_input = str(source_path)
        primary_tag = re.sub(r"[^a-z0-9]+", "_", psrc).strip("_") or "local"
        cudem = paths.data_file(
            "terrain", f"topobathy_01_{primary_tag}_model_crs_{resolution_tag(primary_res)}m.tif")
    else:
        primary_url = str(
            tcfg.get("primary", {}).get("vrt_url")
            or BUILTIN_PRIMARY_TOPOBATHY_URLS.get(psrc, "")
        ).strip()
        if not primary_url:
            raise RuntimeError(
                f"topobathy.primary.vrt_url is required for remote source {psrc!r}. "
                "Select a built-in AutoCF dataset or provide a directly readable VRT/COG URL."
            )
        primary_input = primary_url if primary_url.startswith("/vsi") else "/vsicurl/" + primary_url
        primary_tag = re.sub(r"[^a-z0-9]+", "_", psrc).strip("_") or "remote"
        cudem = paths.data_file(
            "terrain", f"topobathy_01_{primary_tag}_model_crs_{resolution_tag(primary_res)}m.tif")
    primary_source_signature = topobathy_source_signature(
        tcfg.get("primary", {}), bbox_topo, "primary",
    )
    primary_signature = hashlib.sha256(
        f"{primary_source_signature}|prepared_resolution_m={primary_res:g}".encode("utf-8")
    ).hexdigest()[:16]
    prior_primary_signature = (
        state.data.get("meta", {}).get("cudem", {}).get("source_signature")
    )
    if state.is_done("cudem") and prior_primary_signature != primary_signature:
        log("Primary topobathy selection changed; preparing Source 1 again and rebuilding the model.")
        for key in ("cudem", "cudem_grid", "hydromt_build"):
            state.data.setdefault("done", {})[key] = False
        write_json(state.data, state.path)
    prepared_primary_valid = file_ready(cudem, 1024) and raster_has_valid_pixels(cudem)
    if state.is_done("cudem") and not prepared_primary_valid:
        log(
            "Previously prepared primary topobathy contains no valid pixels; "
            "preparing Source 1 again."
        )
        for key in ("cudem", "cudem_grid", "hydromt_build"):
            state.data.setdefault("done", {})[key] = False
        write_json(state.data, state.path)
    if (not state.is_done("cudem")) or (not prepared_primary_valid):
        warp_input = primary_input
        successful_input: Optional[str] = None
        if psrc == "noaa_cudem_1_9_arcsec":
            local_vrt, _ = prepare_noaa_cudem_1_9_tiles(
                paths,
                Path(str(geo_meta.get("domain_path", ""))) if geo_meta.get("domain_path") else None,
                bbox_topo,
                float(tcfg.get("buffer_km", 10.0)),
                model_crs,
            )
            log(
                "Aggregating the native approximately 3 m CUDEM tiles onto the "
                f"configured {primary_res:g} m terrain-sampling grid using cell averages."
            )
            gdalwarp_remote(
                str(local_vrt), cudem, bbox_topo, model_crs, primary_res,
                resampling="average", force=True,
                progress_message="NOAA CUDEM 1/9 local-tile reprojection is active.",
            )
            if raster_has_valid_pixels(cudem):
                successful_input = str(local_vrt)
        else:
            gdalwarp_remote(
                warp_input, cudem, bbox_topo, model_crs, primary_res, force=True,
                progress_message="Terrain Source 1 download/reprojection is active.",
            )
            if raster_has_valid_pixels(cudem):
                successful_input = warp_input
        if successful_input is None:
            if psrc == "noaa_cudem_1_9_arcsec":
                raise RuntimeError(
                    "The selected NOAA CUDEM 1/9 tiles produced no valid terrain cells. "
                    "Select another primary terrain source or provide your own GeoTIFF."
                )
            raise RuntimeError(
                f"Primary terrain source produced no valid pixels for the study area: {warp_input}"
            )
        state.mark(
            "cudem", path=str(cudem), resolution_m=primary_res,
            source=psrc, source_signature=primary_signature,
            resolved_input=successful_input,
        )
    else:
        log(f"Skipping primary topobathy; already complete: {cudem}")

    # setup_dep only derives the computational mask/boundary. Give it a small
    # grid-resolution raster rather than making it load the full subgrid source.
    grid_signature = _bbox_signature(bbox_topo, primary_signature, primary_res, grid_res)
    prior_grid_signature = state.data.get("meta", {}).get("cudem_grid", {}).get("source_signature")
    if state.is_done("cudem_grid") and prior_grid_signature != grid_signature:
        state.data.setdefault("done", {})["cudem_grid"] = False
        write_json(state.data, state.path)
    if grid_res > primary_res:
        cudem_grid = paths.data_file(
            "terrain", f"topobathy_primary_grid_{resolution_tag(grid_res)}m.tif")
        if (not state.is_done("cudem_grid")) or (not file_ready(cudem_grid, 1024)):
            # Derive the small mask raster from the prepared local source. This
            # avoids traversing the remote CUDEM VRT and all of its tiles twice.
            gdalwarp_remote(str(cudem), cudem_grid, bbox_topo, model_crs, grid_res, force=True)
            state.mark(
                "cudem_grid", path=str(cudem_grid), resolution_m=grid_res,
                source_signature=grid_signature,
            )
        else:
            log(f"Skipping grid-mask topobathy; already complete: {cudem_grid}")
    else:
        cudem_grid = cudem
    gcfg = tcfg.get("gapfill", {}); gsrc = str(gcfg.get("source", "GEBCO_2024_15_ARCSEC")).lower()
    gap_selection_signature = topobathy_source_signature(gcfg, bbox_topo, "gapfill")
    prior_gap_selection = (
        state.data.get("meta", {}).get("topobathy_gapfill", {}).get("source_signature")
    )
    if prior_gap_selection != gap_selection_signature:
        if prior_gap_selection is not None:
            log("Source 2 topobathy selection/order changed; rebuilding dependent model files.")
            state.data.setdefault("done", {})["hydromt_build"] = False
        state.data.setdefault("meta", {})["topobathy_gapfill"] = {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "source": gsrc,
            "source_signature": gap_selection_signature,
        }
        write_json(state.data, state.path)
    gebco = None
    if gsrc == "none":
        log("Topobathy gapfill: none")
    elif gsrc in LOCAL_TOPOBATHY_SOURCES:
        gap_source = Path(str(gcfg.get("path", ""))).expanduser().resolve()
        if not gap_source.is_file():
            raise RuntimeError(f"topobathy.gapfill.path does not exist: {gap_source}")
        gap_res = max(float(gcfg.get("output_resolution_m", 500)), primary_res)
        gap_tag = re.sub(r"[^a-z0-9]+", "_", gsrc).strip("_") or "local"
        gebco = paths.data_file(
            "terrain", f"topobathy_02_{gap_tag}_model_crs_{resolution_tag(gap_res)}m.tif")
        gap_signature = topobathy_source_signature(gcfg, bbox_topo, "gapfill")
        prior_gap_signature = state.data.get("meta", {}).get("gebco", {}).get("source_signature")
        if state.is_done("gebco") and prior_gap_signature != gap_signature:
            log("Gap-fill topobathy selection changed; preparing Source 2 again and rebuilding the model.")
            for key in ("gebco", "hydromt_build"):
                state.data.setdefault("done", {})[key] = False
            write_json(state.data, state.path)
        if (not state.is_done("gebco")) or (not file_ready(gebco, 1024)):
            gdalwarp_remote(str(gap_source), gebco, bbox_topo, model_crs, gap_res, force=True)
            state.mark(
                "gebco", path=str(gebco), resolution_m=gap_res,
                source=gsrc, source_signature=gap_signature,
            )
        else:
            log(f"Skipping gap-fill topobathy; already complete: {gebco}")
    else:
        gap_res = float(gcfg.get("output_resolution_m", 500))
        gap_tag = re.sub(r"[^a-z0-9]+", "_", gsrc).strip("_") or "remote"
        gebco = paths.data_file(
            "terrain", f"topobathy_02_{gap_tag}_model_crs_{resolution_tag(gap_res)}m.tif")
        gap_signature = topobathy_source_signature(gcfg, bbox_topo, "gapfill")
        prior_gap_signature = state.data.get("meta", {}).get("gebco", {}).get("source_signature")
        if state.is_done("gebco") and prior_gap_signature != gap_signature:
            log("Gap-fill topobathy selection changed; preparing Source 2 again and rebuilding the model.")
            for key in ("gebco", "hydromt_build"):
                state.data.setdefault("done", {})[key] = False
            write_json(state.data, state.path)
        if (not state.is_done("gebco")) or (not file_ready(gebco, 1024)):
            gap_url = str(
                gcfg.get("cog_url") or BUILTIN_GAPFILL_TOPOBATHY_URLS.get(gsrc, "")
            ).strip()
            if not gap_url:
                raise RuntimeError(
                    f"topobathy.gapfill.cog_url is required for remote source {gsrc!r}."
                )
            src = gap_url if gap_url.startswith("/vsi") else "/vsicurl/" + gap_url
            gdalwarp_remote(
                src, gebco, bbox_topo, model_crs, gap_res, force=True,
                progress_message="Terrain Source 2 download/reprojection is active.",
            )
            state.mark(
                "gebco", path=str(gebco), resolution_m=gap_res,
                source=gsrc, source_signature=gap_signature,
            )
        else:
            log(f"Skipping GEBCO; already complete: {gebco}")

    datum_report: Dict[str, Any] = {
        "target_vertical_datum": target_datum,
        "primary": source_datum_metadata(tcfg.get("primary", {})),
        "gapfill": None,
        "policy": str(tcfg.get("datum_policy", "align_gapfill")),
        "engine_requested": str(tcfg.get("datum_engine", "auto")),
        "transformez": transformez_status(),
    }
    event_start = (cfg.get("simulation", {}) or {}).get("start")
    datum_report["primary"]["temporal"] = source_temporal_metadata(
        tcfg.get("primary", {}), event_start
    )
    if datum_report["primary"]["temporal"]["compatibility"] == "post-event":
        log("WARNING: Source 1 acquisition date is after the modeled event; terrain is modern, not historical.")
    if gebco is not None:
        gap_datum = source_vertical_datum(gcfg)
        gap_meta = source_datum_metadata(gcfg)
        gap_meta["temporal"] = source_temporal_metadata(gcfg, event_start)
        if gap_meta["temporal"]["compatibility"] == "post-event":
            log("WARNING: Source 2 acquisition date is after the modeled event; gap-filled cells use modern terrain.")
        if gap_datum == "UNKNOWN":
            raise RuntimeError(
                "topobathy.gapfill.vertical_datum is required for a user or unknown elevation source."
            )
        if not vertical_datums_equivalent(gap_datum, target_datum):
            policy = str(tcfg.get("datum_policy", "align_gapfill")).strip().lower()
            if policy != "align_gapfill":
                raise RuntimeError(
                    f"Gap-fill topobathy is {gap_datum}, while the model is {target_datum}. "
                    "Set topobathy.datum_policy=align_gapfill or provide a converted raster."
                )
            aligned_signature = _bbox_signature(
                bbox_topo, gap_selection_signature, primary_signature, target_datum,
                gcfg.get("vertical_datum_offset_m", "auto"),
                tcfg.get("datum_engine", "auto"),
                datum_report["transformez"].get("available", False),
                datum_report["transformez"].get("version", ""),
            )
            aligned = paths.data_file(
                "terrain",
                f"topobathy_02_{gap_tag}_aligned_{target_datum.lower()}_{resolution_tag(gap_res)}m.tif",
            )
            prior_alignment = state.data.get("meta", {}).get("topobathy_gapfill_alignment", {})
            if (
                not state.is_done("topobathy_gapfill_alignment")
                or prior_alignment.get("source_signature") != aligned_signature
                or not file_ready(aligned, 1024)
            ):
                log(
                    f"Harmonizing Source 2 from {gap_datum} to {target_datum}; "
                    "AutoCF will prefer a rigorous grid transformation and retain the local fallback."
                )
                diagnostics = harmonize_gapfill_to_target(
                    cudem, gebco, aligned,
                    target_datum=target_datum,
                    primary_datum=primary_datum,
                    gapfill_datum=gap_datum,
                    engine=tcfg.get("datum_engine", "auto"),
                    configured_offset_m=gcfg.get("vertical_datum_offset_m"),
                    cache_dir=paths.cache / "transformez",
                )
                state.mark(
                    "topobathy_gapfill_alignment", path=str(aligned),
                    source_signature=aligned_signature, **diagnostics,
                )
                state.data.setdefault("done", {})["hydromt_build"] = False
                write_json(state.data, state.path)
            else:
                diagnostics = dict(prior_alignment)
                log(f"Skipping gap-fill datum alignment; already complete: {aligned}")
            gebco = aligned
            gap_meta["alignment"] = diagnostics
            if diagnostics.get("quality") == "approximate":
                log(
                    "WARNING: Transformez was unavailable or could not complete this conversion; "
                    "the gap filler uses approximate local overlap alignment. See provenance for the reason."
                )
        else:
            gap_meta["alignment"] = {
                "method": "none-required", "offset_m": 0.0,
                "source_datum": gap_datum, "target_datum": target_datum,
            }
        datum_report["gapfill"] = gap_meta
    additional = prepare_additional_topobathy(
        cfg, paths, state, geo_meta, bbox_topo, model_crs, cudem, primary_signature)
    cfg["_additional_topobathy"] = additional
    datum_report["additional_sources"] = [entry["metadata"] for entry in additional]
    datum_provenance = write_datum_provenance(
        paths.result_file("simulation", "vertical_datum_provenance.json"), datum_report
    )
    log(f"Vertical-datum provenance: {datum_provenance}")

    source_provenance_path = paths.data_file(
        "terrain", "topobathy_source_provenance_model_grid.tif"
    )
    source_provenance = write_source_provenance_raster(
        cudem_grid, gebco, source_provenance_path,
        additional_sources=[(entry["index"], Path(entry["path"])) for entry in additional],
    )
    source_provenance.update({
        "primary": source_datum_metadata(tcfg.get("primary", {})),
        "gapfill": source_datum_metadata(gcfg) if gebco is not None else None,
        "priority": "Sources are applied in numbered order; each supplies only remaining NoData gaps.",
        "additional_sources": [entry["metadata"] for entry in additional],
    })
    source_report = write_datum_provenance(
        paths.result_file("simulation", "topobathy_source_provenance.json"),
        source_provenance,
    )
    log(f"Cell-level terrain source provenance: {source_provenance_path}")
    log(f"Terrain source coverage report: {source_report}")

    if mode == "topobathy_only":
        # Elevation rasters only. The resumable state marks cudem/gebco done, so a
        # later build_only continues from here without redownloading them.
        log("Topobathy-only mode complete. Elevation rasters are prepared for the build.")
        log(f"Primary topobathy: {cudem}")
        log(f"Grid-mask topobathy: {cudem_grid}")
        log(f"Gapfill topobathy: {gebco if gebco else 'none'}")
        return finish()

    lcfg = cfg.get("landcover", {})
    lsource = str(lcfg.get("source", "ESA_WorldCover_2021_v200")).lower()
    if lsource == "file":
        esa = copy_user_file(
            lcfg.get("path"), paths.data_file("surface", "user_landcover.tif"),
            "landcover.path")
    elif "nlcd" in lsource:
        esa = download_annual_nlcd_landcover(cfg, geo_meta, paths, landcover_config=lcfg)
    else:
        if model_mode == "subgrid":
            lc_res = float(cfg.get("subgrid", {}).get("landcover_output_resolution_m", 10.0)); esa = paths.data_file("surface", f"esa_worldcover_model_crs_subgrid_{lc_res:g}m.tif")
        else: esa = paths.data_file("surface", "esa_worldcover_model_crs.tif")
        if (not state.is_done("esa_worldcover")) or (not file_ready(esa, 1024)):
            esa = download_esa_worldcover(
                cfg, geo_meta["bbox"], paths, model_crs,
                domain_path=Path(geo_meta["domain_path"]),
            ); state.mark("esa_worldcover", path=str(esa))
        else: log(f"Skipping ESA WorldCover; already complete: {esa}")
    manning_csv = prepare_manning_table(lcfg, paths)
    esa, manning_csv = apply_roughness_overrides(esa, manning_csv, geo_meta, paths)

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

    # The same preparation/validation path is exposed independently by the
    # Atmosphere tab and reused here during a normal model build.
    wind_grid, pressure_grid, precip_grid, _atmosphere_validation = prepare_atmospheric_forcing(
        cfg, geo_meta, paths, state
    )

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
        dis_ts, dis_locs, source_id_field="river_source_id",
        domain_path=Path(geo_meta["domain_path"]),
    )

    # Catalog/build now consume canonical provider outputs rather than source-specific names.
    if gebco is None:
        # Reuse primary raster as a harmless second source only for the legacy function signature;
        # dep source assembly below filters duplicates.
        gebco = cudem
    catalog = make_hydromt_catalog(
        cfg, paths, cudem, gebco, esa, manning_csv, wind_grid, pressure_grid, precip_grid,
        infiltration_inputs=infiltration_inputs,
        primary_grid_tif=cudem_grid,
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
            roughness_signature=rough_sig,
            forcing_signature=forc_sig,
        )
    else:
        log("Skipping HydroMT build; already complete.")
        validate_built_model(cfg, paths)

    snap_noaa_observation_points_to_wet_cells(paths)

    if mode == "build_only":
        log("Build-only mode complete. SFINCS was not run.")
        log(f"Next step: set workflow.mode: run_direct_only and run python -u sfincs_agent.py {cfg_path}")
        return finish()

    if mode == "build_run_direct":
        run_sfincs_direct(cfg, paths)
        return finish()

    raise RuntimeError(f"Unexpected workflow.mode at end of build: {mode}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log("FAILED")
        log(str(exc))
        raise
