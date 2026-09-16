"""AutoCF native Windows desktop application with project/map/result panels."""
from __future__ import annotations

import csv
import io
import concurrent.futures
import html
import importlib
import json
import math
import os
import re
import subprocess
import sys
import time
import base64
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QBuffer, QDateTime, QEvent, QIODevice, QProcess, QProcessEnvironment, QRectF, QSize, QSettings, QSaveFile, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDockWidget,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QInputDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QButtonGroup, QProgressBar, QRadioButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QToolBar, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

try:
    from ruamel.yaml import YAML
except Exception:
    YAML = None

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception:
    QWebEngineView = None
    QWebEngineSettings = None

from host_runtime import main as host_runtime_main
from platform_runtime import PlatformRuntime
from riva_assistant import RivaAssistantDock
from sfincs_manager import (
    DOCKER_HUB_URL, WINDOWS_DOWNLOAD_URL, DownloadCancelled,
    download_release_source, fetch_latest_release, inspect_installation, is_newer,
)

APP_VERSION = os.environ.get("AUTOCF_VERSION", "2.2")
STANDARD_MESH_CELL_SIZES_M = (30.0, 50.0, 100.0, 200.0, 500.0, 1000.0)
CDS_API_SETUP_URL = "https://cds.climate.copernicus.eu/how-to-api"
CDS_ERA5_DATASET_URL = (
    "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels"
)
CDS_GTSM_DATASET_URL = (
    "https://cds.climate.copernicus.eu/datasets/sis-water-level-change-timeseries-cmip6"
)
EWDS_GLOFAS_DATASET_URL = (
    "https://ewds.climate.copernicus.eu/datasets/cems-glofas-historical"
)
EWDS_GLOFAS_TERMS_URL = (
    "https://ewds.climate.copernicus.eu/datasets/cems-glofas-historical"
    "?tab=download#manage-licences"
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
NASA_DELTAX_DEM_PAGE = "https://doi.org/10.3334/ORNLDAAC/2181"
NOAA_DIGITAL_COAST_DATA_VIEWER = "https://coast.noaa.gov/dataviewer/"
UHSLC_ERDDAP_ROOT = "https://uhslc.soest.hawaii.edu/erddap/tabledap"
WATER_LEVEL_PROVIDERS = {
    "noaa_coops": {"label": "NOAA CO-OPS"},
    "gesla": {"label": "GESLA 4.1", "dataset": "global_hourly_gesla"},
    "uhslc_rq": {"label": "UHSLC/JASL Research Quality", "dataset": "global_hourly_rqds"},
    "uhslc_fast": {"label": "UHSLC Fast Delivery", "dataset": "global_hourly_fast"},
}
TOPOBATHY_DATASETS = {
    "NOAA_CUDEM_1_9_ARCSEC": {
        "resolution_m": 3.0,
        "access": "automatic_tiles",
        "vertical_datum": "NAVD88",
        "datum_note": "Managed NOAA 0.25-degree integrated bathymetric-topographic tiles.",
    },
    "USGS_3DEP_1_3_ARCSEC": {
        "url": USGS_3DEP_1_3_ARCSEC_VRT_URL,
        "resolution_m": 10.0,
        "access": "direct",
        "vertical_datum": "NAVD88",
    },
    "GEBCO_2024_15_ARCSEC": {
        "url": GEBCO_2024_COG_URL,
        "resolution_m": 500.0,
        "access": "direct",
        "vertical_datum": "MSL",
        "datum_note": "Assumed MSL; compiled source datums may vary locally.",
    },
    "GEDTM30": {
        "url": GEDTM30_COG_URL,
        "resolution_m": 30.0,
        "access": "direct",
        "vertical_datum": "EGM2008",
    },
    "COPERNICUS_GLO_30": {
        "url": COPERNICUS_GLO_30_VRT_URL,
        "resolution_m": 30.0,
        "access": "direct",
        "vertical_datum": "EGM2008",
    },
    "NASA_DELTAX_2021": {
        "resolution_m": 10.0,
        "access": "earthdata_file",
        "landing_page": NASA_DELTAX_DEM_PAGE,
    },
}
BUILTIN_PRIMARY_TOPOBATHY_URLS = {
    key: str(value["url"]) for key, value in TOPOBATHY_DATASETS.items() if value.get("url")
}
BUILTIN_PRIMARY_TOPOBATHY_URLS["USGS_3DEP"] = USGS_3DEP_1_3_ARCSEC_VRT_URL
BUILTIN_GAPFILL_TOPOBATHY_URLS = dict(BUILTIN_PRIMARY_TOPOBATHY_URLS)
LOCAL_TOPOBATHY_SOURCES = {"file", "nasa_deltax_2021"}
AUTOMATIC_TOPOBATHY_SOURCES = {"noaa_cudem_1_9_arcsec"}


def normalize_vertical_datum_name(value: Any) -> str:
    text = str(value or "").strip().upper().replace("_", "").replace("-", "").replace(":", "")
    return {
        "NAVD": "NAVD88", "NAVD88": "NAVD88", "MSL": "MSL", "LMSL": "MSL",
        "EGM2008": "EGM2008", "EPSG3855": "EGM2008", "EGM96": "EGM96",
        "EPSG5773": "EGM96",
    }.get(text, text)
DEFAULT_ESA_MANNING_ROWS = [
    (10, "Tree cover", 0.120),
    (20, "Shrubland", 0.050),
    (30, "Grassland", 0.034),
    (40, "Cropland", 0.037),
    (50, "Built-up", 0.100),
    (60, "Bare sparse vegetation", 0.023),
    (70, "Snow and ice", 0.010),
    (80, "Permanent water bodies", 0.020),
    (90, "Herbaceous wetland", 0.035),
    (95, "Mangroves", 0.070),
    (100, "Moss and lichen", 0.025),
]
DEFAULT_NLCD_MANNING_ROWS = [
    (11, "Open water", 0.023),
    (21, "Developed — open space", 0.070),
    (22, "Developed — low density", 0.100),
    (23, "Developed — medium density", 0.120),
    (24, "Developed — high density", 0.140),
    (31, "Barren land", 0.070),
    (41, "Deciduous forest", 0.120),
    (42, "Evergreen forest", 0.150),
    (43, "Mixed forest", 0.120),
    (54, "Shrub/scrub", 0.050),
    (71, "Grassland/herbaceous", 0.034),
    (81, "Pasture/hay", 0.030),
    (82, "Cultivated crops", 0.035),
    (90, "Woody wetlands", 0.100),
    (95, "Emergent herbaceous wetlands", 0.035),
]
IBTRACS_URL = (
    "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-"
    "stewardship-ibtracs/v04r01/access/csv/ibtracs.since1980.list.v04r01.csv"
)
ESA_WORLDCOVER_TILE_URL_TEMPLATE = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
LANDCOVER_DATASETS = {
    "ESA_WORLDCOVER_2021_V200": {
        "resolution_m": 10.0,
        "tile_url_template": ESA_WORLDCOVER_TILE_URL_TEMPLATE,
    },
    "ANNUAL_NLCD": {"resolution_m": 30.0},
}
GEOCODER_URL = os.environ.get(
    "AUTOCF_GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
BASINS = {
    "NA": "North Atlantic", "EP": "Eastern North Pacific",
    "WP": "Western North Pacific", "NI": "North Indian",
    "SI": "South Indian", "SP": "Southern Pacific", "SA": "South Atlantic",
}
CONTINENTS = {
    "North America": (-170.0, 5.0, -50.0, 83.0),
    "South America": (-82.0, -56.0, -34.0, 13.0),
    "Europe": (-25.0, 34.0, 45.0, 72.0),
    "Africa": (-20.0, -36.0, 55.0, 38.0),
    "Asia": (25.0, 0.0, 180.0, 80.0),
    "Oceania": (110.0, -50.0, 180.0, 5.0),
}
BASEMAPS = {
    "OpenStreetMap": {"tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], "attribution": "© OpenStreetMap contributors", "maxzoom": 19},
    "Esri Light Gray Canvas": {"tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"], "attribution": "Esri, HERE, Garmin", "maxzoom": 16},
    "Esri Dark Gray Canvas": {"tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"], "attribution": "Esri, HERE, Garmin", "maxzoom": 16},
    "Esri World Imagery": {"tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], "attribution": "Esri, Maxar, Earthstar Geographics", "maxzoom": 19},
    "Esri World Topographic": {"tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"], "attribution": "Esri, HERE, Garmin, USGS, NGA", "maxzoom": 19},
    "Esri Ocean Basemap": {"tiles": ["https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}"], "attribution": "Esri, GEBCO, NOAA, NatGeo", "maxzoom": 13},
    "USGS Imagery Topo": {"tiles": ["https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}"], "attribution": "USGS The National Map", "maxzoom": 16},
    "OpenTopoMap": {"tiles": ["https://a.tile.opentopomap.org/{z}/{x}/{y}.png"], "attribution": "© OpenStreetMap contributors, SRTM, © OpenTopoMap (CC-BY-SA)", "maxzoom": 17},
    "No basemap (offline)": {"tiles": [], "attribution": "", "maxzoom": 19},
}
WBD_QUERY_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/{layer}/query"
# The USGS WBD service exposes polygon layers 1–8 for HUC2 through HUC16.
# HUC8 stays first/default because it is the most common AutoCF regional AOI.
HUC_LEVELS = {
    "HUC8 — Subbasin": (4, "huc8"),
    "HUC6 — Basin": (3, "huc6"),
    "HUC10 — Watershed": (5, "huc10"),
    "HUC12 — Subwatershed": (6, "huc12"),
    "HUC4 — Subregion": (2, "huc4"),
    "HUC2 — Region": (1, "huc2"),
    "HUC14": (7, "huc14"),
    "HUC16": (8, "huc16"),
}
HUC_SIMPLIFY_DEGREES = 0.0005
COOPS_STATIONS_URL = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=waterlevels&units=metric"
USGS_SITE_URL = "https://waterservices.usgs.gov/nwis/site/"
USGS_STN_EVENTS_URL = "https://stn.wim.usgs.gov/STNServices/Events.json"
USGS_STN_HWM_URL = "https://stn.wim.usgs.gov/STNServices/HWMs/FilteredHWMs.json"
ROLE_STYLES = {
    "domain": ("Domain polygon", "#20a4f3"), "ocean_boundary": ("Ocean boundary line", "#ff9f1c"),
    "ocean_forcing_point": ("Water-level point", "#e63946"), "observation_point": ("Observation point", "#8ac926"),
    "river_source": ("River source point", "#9d4edd"),
    "roughness_override": ("NBS/adaptation roughness polygon", "#00b894"),
}
SFINCS_PARAMETERS = [
    ("advection", "Advection", "choice", 1, "", "Enable momentum advection."),
    ("alpha", "Momentum relaxation", "float", 0.5, "", "SFINCS alpha numerical parameter."),
    ("huthresh", "Wet/dry threshold", "float", 0.05, "m", "Minimum depth used in wet/dry logic."),
    ("theta", "Theta", "float", 1.0, "", "Time-integration weighting parameter."),
    ("qinf", "Uniform infiltration", "float", 0.0, "mm h⁻¹", "Uniform infiltration without a spatial file."),
    ("viscosity", "Viscosity", "float", 1.0, "m² s⁻¹", "Horizontal eddy viscosity."),
    ("storecumprcp", "Store cumulative precipitation", "choice", 1, "", "Write cumulative precipitation."),
    ("storevel", "Store velocity fields", "choice", 0, "", "Write time-varying velocity fields."),
    ("storevelmax", "Store maximum velocity", "choice", 0, "", "Write maximum velocity; requires dtmaxout > 0."),
    ("storefluxmax", "Store maximum flux", "choice", 0, "", "Write maximum flux; requires dtmaxout > 0."),
    ("storetzsmax", "Store time of maximum level", "choice", 0, "", "Write time at maximum water level."),
    ("storemaxwind", "Store maximum wind", "choice", 0, "", "Write event-maximum wind speed."),
]


def package_root() -> Path:
    packaged_root = os.environ.get("AUTOCF_PACKAGE_ROOT", "").strip()
    if packaged_root:
        candidate = Path(packaged_root).expanduser().resolve()
        if (candidate / "app").is_dir() and (candidate / "windows").is_dir():
            return candidate
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    executable = Path(sys.executable).resolve()
    # A macOS executable lives inside AutoCF-Desktop.app/Contents/MacOS.  The
    # deployable AutoCF data and container recipe live beside the .app, while a
    # Windows one-file executable lives directly in that package root.
    for candidate in (executable.parent, *executable.parents):
        if (candidate / "app").is_dir() and (candidate / "windows").is_dir():
            return candidate
    return executable.parent


def deep_get(mapping: Any, path: str, default: Any = None) -> Any:
    node = mapping
    for key in path.split("."):
        if not hasattr(node, "get") or key not in node:
            return default
        node = node[key]
    return node


def deep_set(mapping: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    node = mapping
    for key in parts[:-1]:
        child = node.get(key)
        if not hasattr(child, "get"):
            node[key] = {}
        node = node[key]
    node[parts[-1]] = value


def portable_yaml_path(value: str, config_path: Path | None) -> str:
    """Store files inside a model workspace relative to its YAML file."""
    text = str(value or "").strip()
    if not text or "://" in text or config_path is None:
        return text
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        normalized = candidate.as_posix()
        return normalized if normalized.startswith(".") else f"./{normalized}"
    try:
        relative = candidate.resolve().relative_to(config_path.parent.resolve())
    except ValueError:
        return text
    normalized = relative.as_posix()
    return "." if normalized == "." else f"./{normalized}"


def repair_uninitialized_desktop_defaults(config: Any) -> bool:
    """Repair the complete numeric fingerprint saved by older new-model UI."""
    try:
        placeholder_profile = (
            float(deep_get(config, "forcing.ocean_boundary.max_station_distance_km")) <= 1.0
            and float(deep_get(config, "forcing.river.max_gage_distance_km")) <= 1.0
            and int(deep_get(config, "forcing.river.max_gages")) <= 0
            and float(deep_get(config, "sfincs.dtout_seconds")) <= 0.0
            and float(deep_get(config, "sfincs.dthisout_seconds")) <= 0.0
            and float(deep_get(config, "sfincs.dtmaxout_seconds")) <= 0.0
            and int(deep_get(config, "subgrid.nlevels")) <= 2
            and int(deep_get(config, "subgrid.nr_subgrid_pixels")) <= 2
        )
    except (TypeError, ValueError):
        placeholder_profile = False
    if not placeholder_profile:
        return False
    for path, value in {
        "forcing.ocean_boundary.max_station_distance_km": 150.0,
        "forcing.river.max_gage_distance_km": 50.0,
        "forcing.river.max_gages": 12,
        "sfincs.dtout_seconds": 3600.0,
        "sfincs.dthisout_seconds": 360.0,
        "sfincs.dtmaxout_seconds": 3600.0,
        "subgrid.nlevels": 10,
        "subgrid.nr_subgrid_pixels": 20,
    }.items():
        deep_set(config, path, value)
    return True


def deep_pop(mapping: Any, path: str) -> None:
    """Remove one optional nested setting without disturbing sibling keys."""
    parts = path.split(".")
    node = mapping
    for key in parts[:-1]:
        if not hasattr(node, "get"):
            return
        node = node.get(key)
        if node is None:
            return
    if hasattr(node, "pop"):
        node.pop(parts[-1], None)


def point_in_geojson_geometry(lon: float, lat: float, geometry: Any) -> bool:
    """Return whether a longitude/latitude point is inside a GeoJSON polygon."""
    if not isinstance(geometry, dict):
        return False
    kind = str(geometry.get("type", ""))
    coordinates = geometry.get("coordinates") or []
    polygons = [coordinates] if kind == "Polygon" else coordinates if kind == "MultiPolygon" else []

    def in_ring(ring: Any) -> bool:
        if not isinstance(ring, list) or len(ring) < 3:
            return False
        inside = False
        previous = ring[-1]
        for current in ring:
            try:
                x1, y1 = float(previous[0]), float(previous[1])
                x2, y2 = float(current[0]), float(current[1])
            except (TypeError, ValueError, IndexError):
                previous = current
                continue
            cross = (x2 - x1) * (lat - y1) - (y2 - y1) * (lon - x1)
            if abs(cross) <= 1.0e-10 and min(x1, x2) - 1.0e-10 <= lon <= max(x1, x2) + 1.0e-10 \
                    and min(y1, y2) - 1.0e-10 <= lat <= max(y1, y2) + 1.0e-10:
                return True
            if (y1 > lat) != (y2 > lat):
                intersection = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
                if lon < intersection:
                    inside = not inside
            previous = current
        return inside

    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon or not in_ring(polygon[0]):
            continue
        if not any(in_ring(hole) for hole in polygon[1:]):
            return True
    return False


def plain(value: Any) -> Any:
    if hasattr(value, "items"):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def resolved_yaml(path: Path, stack: list[Path] | None = None) -> dict[str, Any]:
    if YAML is None:
        raise RuntimeError("The bundled YAML editor is unavailable.")
    path = path.expanduser().resolve()
    chain = list(stack or [])
    if path in chain:
        raise RuntimeError("Circular YAML inheritance: " + " -> ".join(map(str, chain + [path])))
    parser = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as stream:
        raw = plain(parser.load(stream) or {})
    parent = raw.pop("extends", None)
    if parent:
        parent_path = Path(str(parent)).expanduser()
        if not parent_path.is_absolute():
            parent_path = path.parent / parent_path
        raw = merge(resolved_yaml(parent_path, chain + [path]), raw)
    project = raw.get("project", {})
    suffix = project.get("output_dir_suffix")
    if suffix and project.get("output_dir"):
        base = Path(str(project["output_dir"]))
        if not base.name.endswith(str(suffix)):
            project["output_dir"] = str(base.with_name(base.name + str(suffix)))
    return raw


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance, matching haversine_km() in app/sfincs_agent.py."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def geometry_points(geometry: Any) -> Iterable[tuple[float, float]]:
    if not isinstance(geometry, dict):
        return
    if geometry.get("type") == "GeometryCollection":
        for child in geometry.get("geometries", []):
            yield from geometry_points(child)
        return
    def walk(node: Any) -> Iterable[tuple[float, float]]:
        if isinstance(node, (list, tuple)) and len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
            yield float(node[0]), float(node[1])
        elif isinstance(node, (list, tuple)):
            for child in node:
                yield from walk(child)
    yield from walk(geometry.get("coordinates"))


def geojson_points(document: Any) -> list[tuple[float, float]]:
    if not isinstance(document, dict):
        return []
    if document.get("type") == "FeatureCollection":
        return [point for feature in document.get("features", []) for point in geometry_points(feature.get("geometry"))]
    if document.get("type") == "Feature":
        return list(geometry_points(document.get("geometry")))
    return list(geometry_points(document))


def feature_list(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    if document.get("type") == "FeatureCollection":
        return [f for f in document.get("features", []) if isinstance(f, dict)]
    if document.get("type") == "Feature":
        return [document]
    if document.get("type"):
        return [{"type": "Feature", "properties": {}, "geometry": document}]
    return []


def split_study_geojson(document: Any) -> dict[str, list[dict[str, Any]]]:
    """Split a study-area GeoJSON into AutoCF roles.

    Mirrors extract_geojson_parts() in app/sfincs_agent.py: an explicit ``role``
    property wins, otherwise the legacy geometry-type convention applies.
    """
    parts: dict[str, list[dict[str, Any]]] = {key: [] for key in ROLE_STYLES}
    features = feature_list(document)
    for feature in features:
        role = str((feature.get("properties") or {}).get("role", "")).strip().lower()
        if role in parts:
            parts[role].append(feature)
            continue
        # Apply the legacy convention per feature. A partially attributed file
        # must not lose an unlabelled AOI merely because another feature has a
        # role property.
        kind = str((feature.get("geometry") or {}).get("type", ""))
        if kind in ("Polygon", "MultiPolygon"):
            parts["domain"].append(feature)
        elif kind in ("LineString", "MultiLineString"):
            parts["ocean_boundary"].append(feature)
        elif kind == "Point":
            parts["ocean_forcing_point"].append(feature)
    return parts


def role_feature(role: str, geometry: dict[str, Any], properties: dict[str, Any] | None = None) -> dict[str, Any]:
    props = dict(properties or {})
    props["role"] = role
    return {"type": "Feature", "properties": props, "geometry": geometry}


def merge_adjacent_polygon_geometries(geometries: list[dict[str, Any]]) -> dict[str, Any]:
    """Dissolve adjacent GeoJSON polygons using Qt's local geometry engine."""
    merged = QPainterPath()
    merged.setFillRule(Qt.FillRule.OddEvenFill)
    for geometry in geometries:
        geometry_type = str(geometry.get("type", ""))
        coordinates = geometry.get("coordinates") or []
        polygons = ([coordinates] if geometry_type == "Polygon" else
                    coordinates if geometry_type == "MultiPolygon" else [])
        if not polygons:
            raise ValueError("Every selected watershed must be a Polygon or MultiPolygon.")
        for polygon in polygons:
            part = QPainterPath()
            part.setFillRule(Qt.FillRule.OddEvenFill)
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 3:
                    continue
                part.moveTo(float(ring[0][0]), float(ring[0][1]))
                for point in ring[1:]:
                    part.lineTo(float(point[0]), float(point[1]))
                part.closeSubpath()
            merged = part if merged.isEmpty() else merged.united(part)
    merged = merged.simplified()
    rings: list[list[list[float]]] = []
    for polygon in merged.toSubpathPolygons():
        ring = [[float(point.x()), float(point.y())] for point in polygon]
        if len(ring) >= 4:
            if ring[0] != ring[-1]:
                ring.append(ring[0][:])
            rings.append(ring)
    if not rings:
        raise ValueError("The selected watershed geometries could not be merged.")

    def signed_area(ring: list[list[float]]) -> float:
        return 0.5 * sum(
            ring[index][0] * ring[index + 1][1] - ring[index + 1][0] * ring[index][1]
            for index in range(len(ring) - 1))

    def inside(point: list[float], ring: list[list[float]]) -> bool:
        x, y = point
        result = False
        for index in range(len(ring) - 1):
            x1, y1 = ring[index]
            x2, y2 = ring[index + 1]
            if (y1 > y) != (y2 > y):
                crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < crossing:
                    result = not result
        return result

    rings.sort(key=lambda ring: abs(signed_area(ring)), reverse=True)
    outer, remaining = rings[0], rings[1:]
    if any(not inside(ring[0], outer) for ring in remaining):
        raise ValueError(
            "The selected watersheds do not form one connected area. Select watersheds that share an edge.")
    if signed_area(outer) < 0:
        outer.reverse()
    holes = []
    for ring in remaining:
        if signed_area(ring) > 0:
            ring.reverse()
        holes.append(ring)
    return {"type": "Polygon", "coordinates": [outer, *holes]}


class FittedPixmapLabel(QLabel):
    """Display an image fitted to the available panel without distorting it."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._source_pixmap = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def set_fitted_pixmap(self, pixmap: QPixmap) -> None:
        self._source_pixmap = QPixmap(pixmap)
        self._refresh_pixmap()

    def clear(self) -> None:
        self._source_pixmap = QPixmap()
        super().clear()

    def _refresh_pixmap(self) -> None:
        if self._source_pixmap.isNull():
            return
        available = self.contentsRect().size()
        if available.width() <= 1 or available.height() <= 1:
            return
        super().setPixmap(self._source_pixmap.scaled(
            available,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._refresh_pixmap()


class MapColorbarLabel(QLabel):
    """Small floating map child that can never occupy or cover the map panel."""

    def setPixmap(self, pixmap: QPixmap) -> None:
        super().setPixmap(pixmap)
        self.adjustSize()
        self.raise_()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.adjustSize()
        self.raise_()


class RivaPet(QWidget):
    """A slow, task-aware animated Riva for the bottom-right anchor dock."""

    activated = Signal()
    FRAME_SEQUENCES = {
        "idle": (0, 0, 1, 0, 2, 3, 0, 0, 1, 0),
        "wave": (4, 5, 6, 5, 4, 7),
        "thinking": (8, 9, 10, 11),
        "talk": (12, 13, 14, 15),
    }
    ACTIVITY_LABELS = {
        "idle": "Ask Riva!",
        "listening": "Riva is listening",
        "thinking": "Riva is thinking…",
        "working": "Riva is working…",
        "responding": "Riva is answering",
        "panic": "I panicked",
    }

    def __init__(self, icon_path: Path, owner: QWidget | None = None) -> None:
        super().__init__(owner)
        self.setObjectName("rivaPet")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(204, 226)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click Riva to open the local AutoCF research assistant.")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 2)
        layout.setSpacing(0)
        self.name = QLabel("Ask Riva!")
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_theme(False)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._frames = self._load_animation_frames(icon_path)
        panic_path = icon_path.with_name("Riva-panic.png")
        panic_source = QPixmap(str(panic_path)) if panic_path.is_file() else QPixmap()
        self._panic_frame = (
            panic_source.scaled(
                194, 194,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if not panic_source.isNull() else QPixmap()
        )
        if self._frames:
            self.image.setPixmap(self._frames[0])
        elif icon_path.is_file():
            pixmap = QPixmap(str(icon_path))
            self.image.setPixmap(pixmap.scaled(
                194, 194,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        layout.addWidget(self.name)
        layout.addWidget(self.image, 1)

        self._hovered = False
        self._pressed = False
        self._last_activation = 0.0
        self._assistant_visible = False
        self._reaction_until = 0.0
        self._activity = "idle"
        self._activity_until = 0.0
        self._animation_step = 0
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(340)
        self._animation_timer.timeout.connect(self._animate)

    def _load_animation_frames(self, icon_path: Path) -> list[QPixmap]:
        """Load the generated 4x4 sheet and key its green backdrop to alpha."""
        sheet_path = icon_path.with_name("Riva-animated.png")
        sheet = QPixmap(str(sheet_path)) if sheet_path.is_file() else QPixmap()
        if sheet.isNull():
            return []
        frames = []
        for row in range(4):
            top = round(row * sheet.height() / 4)
            bottom = round((row + 1) * sheet.height() / 4)
            for column in range(4):
                left = round(column * sheet.width() / 4)
                right = round((column + 1) * sheet.width() / 4)
                frame = sheet.copy(left, top, right - left, bottom - top).scaled(
                    194, 194,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                image = frame.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
                pixels = image.bits().cast("B")
                for offset in range(0, len(pixels), 4):
                    red, green, blue = pixels[offset], pixels[offset + 1], pixels[offset + 2]
                    dominance = green - max(red, blue)
                    if green > 115 and dominance > 35:
                        pixels[offset + 3] = max(0, min(255, int((85 - dominance) * 5.1)))
                        if pixels[offset + 3]:
                            pixels[offset + 1] = min(green, max(red, blue) + 18)
                frames.append(QPixmap.fromImage(image))
        return frames

    def start_animation(self) -> None:
        self.show()
        if self._frames:
            self._animation_timer.start()

    def set_assistant_visible(self, visible: bool) -> None:
        self._assistant_visible = visible
        self._animation_step = 0
        if visible and self._activity == "idle":
            self._activity = "listening"
        elif not visible and self._activity == "listening":
            self._activity = "idle"
        self._update_caption()
        action = "close" if visible else "open"
        self.setToolTip(f"Click Riva to {action} the local AutoCF research assistant.")

    def set_activity(self, activity: str) -> None:
        if activity not in self.ACTIVITY_LABELS:
            activity = "idle"
        self._activity = activity
        self._activity_until = (
            time.monotonic() + 5.0 if activity == "responding" else 0.0
        )
        self._animation_step = 0
        self._update_caption()

    def panic(self, duration_seconds: float = 8.0) -> None:
        """Show Riva's visible hand-on-heart reaction after an AutoCF error."""
        self._activity = "panic"
        self._activity_until = time.monotonic() + max(1.0, duration_seconds)
        self._animation_step = 0
        self._update_caption()
        if not self._panic_frame.isNull():
            self.image.setPixmap(self._panic_frame)

    def set_theme(self, is_light: bool) -> None:
        text_color = "#1e2933" if is_light else "#eef1f4"
        self.name.setStyleSheet(
            f"background:transparent;color:{text_color};font-size:17px;"
            "font-weight:900;padding:0px"
        )

    def _update_caption(self) -> None:
        if self._hovered and self._activity in ("idle", "listening"):
            caption = "Hello!"
        else:
            caption = self.ACTIVITY_LABELS[self._activity]
        self.name.setText(caption)

    def _animate(self) -> None:
        if not self._frames:
            return
        if self._activity_until and time.monotonic() >= self._activity_until:
            self._activity = "listening" if self._assistant_visible else "idle"
            self._activity_until = 0.0
            self._animation_step = 0
            self._update_caption()
        if self._activity == "responding":
            state = "talk"
        elif self._activity == "panic":
            if not self._panic_frame.isNull():
                self.image.setPixmap(self._panic_frame)
                return
            state = "thinking"
        elif self._activity in ("thinking", "working"):
            state = "thinking"
        elif self._hovered or time.monotonic() < self._reaction_until:
            state = "wave"
        else:
            state = "idle"
        sequence = self.FRAME_SEQUENCES[state]
        self.image.setPixmap(self._frames[sequence[self._animation_step % len(sequence)]])
        self._animation_step += 1

    def enterEvent(self, event: Any) -> None:
        self._hovered = True
        self._animation_step = 0
        self._update_caption()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hovered = False
        self._update_caption()
        super().leaveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            if time.monotonic() - self._last_activation > 0.35:
                self._last_activation = time.monotonic()
                self._reaction_until = self._last_activation + 2.4
                self._animation_step = 0
                self.activated.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class HucWorker(QThread):
    """Streams USGS Watershed Boundary Dataset polygons for a bounding box."""
    loaded = Signal(object, str)
    failed = Signal(str)

    def __init__(self, bounds: tuple[float, float, float, float], layer: int, field: str) -> None:
        super().__init__()
        self.bounds, self.layer, self.field = bounds, layer, field

    def run(self) -> None:
        try:
            west, south, east, north = self.bounds
            query = urllib.parse.urlencode({
                "geometry": f"{west},{south},{east},{north}", "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "outSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                "outFields": f"{self.field},name,areasqkm", "returnGeometry": "true",
                "maxAllowableOffset": str(HUC_SIMPLIFY_DEGREES), "f": "geojson",
            })
            url = WBD_QUERY_URL.format(layer=self.layer) + "?" + query
            request = urllib.request.Request(url, headers={"User-Agent": f"AutoCF-Desktop/{APP_VERSION}"})
            with urllib.request.urlopen(request, timeout=180) as response:
                document = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(document, dict) and document.get("error"):
                raise RuntimeError(str(document["error"].get("message", "Watershed service error")))
            records = []
            for feature in feature_list(document):
                properties = feature.get("properties") or {}
                records.append({
                    "code": str(properties.get(self.field) or properties.get(self.field.upper()) or "").strip(),
                    "name": str(properties.get("name") or properties.get("NAME") or "Unnamed").strip(),
                    "area_km2": properties.get("areasqkm") or properties.get("AREASQKM"),
                    "geometry": feature.get("geometry"),
                })
            records = [record for record in records if record["code"] and record["geometry"]]
            records.sort(key=lambda record: record["name"].lower())
            self.loaded.emit(records, self.field.upper())
        except Exception as exc:
            self.failed.emit(str(exc))


class CoopsWorker(QThread):
    """Downloads and caches the NOAA CO-OPS water-level station catalog."""
    loaded = Signal(object, str)
    failed = Signal(str)

    def __init__(self, cache_file: Path, refresh: bool) -> None:
        super().__init__()
        self.cache_file, self.refresh = cache_file, refresh

    def run(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            source = "cache"
            if self.refresh or not self.cache_file.is_file():
                request = urllib.request.Request(COOPS_STATIONS_URL, headers={"User-Agent": f"AutoCF-Desktop/{APP_VERSION}"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = response.read()
                temporary = self.cache_file.with_suffix(".download")
                temporary.write_bytes(payload)
                temporary.replace(self.cache_file)
                source = "NOAA download"
            document = json.loads(self.cache_file.read_text(encoding="utf-8", errors="replace"))
            stations = []
            for entry in document.get("stations", []):
                try:
                    stations.append({
                        "id": str(entry.get("id") or "").strip(), "name": str(entry.get("name") or "").strip(),
                        "state": str(entry.get("state") or "").strip(),
                        "lat": float(entry["lat"]), "lon": float(entry["lng"]),
                    })
                except Exception:
                    continue
            stations = [station for station in stations if station["id"]]
            stations.sort(key=lambda station: (station["state"], station["name"]))
            self.loaded.emit(stations, source)
        except Exception as exc:
            self.failed.emit(str(exc))


class GlobalWaterLevelWorker(QThread):
    """Downloads a normalized GESLA or UHSLC/JASL station catalog."""
    loaded = Signal(str, object, str)
    failed = Signal(str, str)

    def __init__(self, provider: str, cache_file: Path, refresh: bool) -> None:
        super().__init__()
        self.provider = str(provider)
        self.cache_file, self.refresh = cache_file, refresh

    @staticmethod
    def _number(value: Any) -> float:
        return float(str(value).strip())

    def _download_catalog(self) -> list[dict[str, Any]]:
        spec = WATER_LEVEL_PROVIDERS[self.provider]
        dataset = str(spec["dataset"])
        if self.provider == "gesla":
            variables = [
                "latitude", "longitude", "station_name", "agency_id",
                "station_code", "station_country_code", "record_id",
            ]
        else:
            variables = [
                "latitude", "longitude", "station_name", "station_country",
                "record_id", "reference_datum",
            ]
        url = f"{UHSLC_ERDDAP_ROOT}/{dataset}.csv?" + ",".join(variables) + "&distinct()"
        request = urllib.request.Request(url, headers={"User-Agent": f"AutoCF-Desktop/{APP_VERSION}"})
        with urllib.request.urlopen(request, timeout=180) as response:
            text = response.read().decode("utf-8", errors="replace")
        stations: list[dict[str, Any]] = []
        for row in csv.DictReader(text.splitlines()):
            try:
                lat = self._number(row.get("latitude"))
                lon = self._number(row.get("longitude"))
                if lon > 180.0:
                    lon -= 360.0
                raw_id = str(row.get("record_id") or "").strip()
                if self.provider.startswith("uhslc"):
                    identifier = str(int(float(raw_id)))
                else:
                    identifier = raw_id
                if not identifier:
                    continue
            except Exception:
                # ERDDAP inserts a units row below the CSV header.
                continue
            country = str(
                row.get("station_country_code") or row.get("station_country") or ""
            ).strip()
            stations.append({
                "id": identifier,
                "name": str(row.get("station_name") or identifier).strip(),
                "state": country,
                "lat": lat,
                "lon": lon,
                "provider": self.provider,
                "dataset": dataset,
                "datum": (
                    "unspecified" if self.provider == "gesla"
                    else str(row.get("reference_datum") or "station zero").strip()
                ),
                "agency_id": str(row.get("agency_id") or "").strip(),
                "station_code": str(row.get("station_code") or "").strip(),
            })
        stations.sort(key=lambda station: (station["state"], station["name"], station["id"]))
        return stations

    def run(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            source = "cache"
            if self.refresh or not self.cache_file.is_file():
                stations = self._download_catalog()
                temporary = self.cache_file.with_suffix(".download")
                temporary.write_text(json.dumps(stations), encoding="utf-8")
                temporary.replace(self.cache_file)
                source = "ERDDAP download"
            else:
                stations = json.loads(self.cache_file.read_text(encoding="utf-8"))
            stations = [station for station in stations if str(station.get("id", "")).strip()]
            self.loaded.emit(self.provider, stations, source)
        except Exception as exc:
            self.failed.emit(self.provider, str(exc))


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coverage_result(sample_count: int, start: datetime, end: datetime,
                     interval_minutes: int, max_missing_fraction: float = 0.25) -> dict[str, Any]:
    expected = max(1, int((end - start).total_seconds() // (interval_minutes * 60)) + 1)
    fraction = min(1.0, max(0.0, float(sample_count) / float(expected)))
    valid = fraction >= (1.0 - max_missing_fraction)
    return {
        "coverage_valid": valid,
        "coverage_fraction": fraction,
        "coverage_samples": int(sample_count),
        "coverage_expected": expected,
        "coverage_message": (
            f"{fraction:.0%} event coverage ({sample_count}/{expected} expected samples)"
        ),
    }


def _station_event_coverage(provider: str, station: dict[str, Any],
                            start_text: str, end_text: str) -> dict[str, Any]:
    """Return event-period data coverage without downloading a complete station series."""
    start, end = _utc_datetime(start_text), _utc_datetime(end_text)
    station_id = str(station.get("id", "")).strip()
    if not station_id or end <= start:
        raise RuntimeError("invalid station identifier or simulation period")

    if provider == "noaa_coops":
        # NOAA limits six-minute water-level requests to about one month. Split
        # longer events and count unique timestamps across chunk boundaries.
        observed: set[str] = set()
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=30))
            params = {
                "begin_date": chunk_start.strftime("%Y%m%d %H:%M"),
                "end_date": chunk_end.strftime("%Y%m%d %H:%M"),
                "station": station_id, "product": "water_level", "datum": "MSL",
                "time_zone": "gmt", "units": "metric", "format": "json",
                "application": "autocf",
            }
            url = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?" + urllib.parse.urlencode(params)
            request = urllib.request.Request(url, headers={"User-Agent": f"AutoCF-Desktop/{APP_VERSION}"})
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if payload.get("error"):
                message = str(payload["error"].get("message", payload["error"]))
                if "no data" in message.lower() or "no predictions" in message.lower():
                    break
                raise RuntimeError(message)
            observed.update(str(row.get("t")) for row in payload.get("data", []) if row.get("t"))
            if chunk_end >= end:
                break
            chunk_start = chunk_end + timedelta(minutes=6)
        return _coverage_result(len(observed), start, end, 6)

    spec = WATER_LEVEL_PROVIDERS.get(provider, {})
    dataset = str(station.get("dataset") or spec.get("dataset") or "")
    if not dataset:
        raise RuntimeError(f"no ERDDAP dataset configured for {provider}")
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    if provider == "gesla":
        station_constraint = urllib.parse.quote(json.dumps(station_id), safe="")
    else:
        station_constraint = str(int(float(station_id)))
    query = (
        "record_id,sea_level"
        f"&record_id={station_constraint}"
        f"&time>={urllib.parse.quote(json.dumps(start_iso), safe=':-TZ')}"
        f"&time<={urllib.parse.quote(json.dumps(end_iso), safe=':-TZ')}"
    )
    if provider == "gesla":
        query += "&flag2=1"
    query += '&orderByCount("record_id")'
    url = f"{UHSLC_ERDDAP_ROOT}/{dataset}.csv?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": f"AutoCF-Desktop/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _coverage_result(0, start, end, 60)
        raise
    count = 0
    for row in csv.DictReader(text.splitlines()):
        try:
            count = max(count, int(float(str(row.get("sea_level", "0")).strip())))
        except (TypeError, ValueError):
            continue
    return _coverage_result(count, start, end, 60)


class WaterLevelCoverageWorker(QThread):
    """Checks candidate gauges against the selected event before they can be used."""
    checked = Signal(str, object, str)

    def __init__(self, provider: str, stations: list[dict[str, Any]],
                 start_text: str, end_text: str) -> None:
        super().__init__()
        self.provider = str(provider)
        self.stations = [dict(station) for station in stations]
        self.start_text, self.end_text = str(start_text), str(end_text)

    def run(self) -> None:
        def check(station: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            identifier = str(station.get("id", ""))
            try:
                result = _station_event_coverage(
                    self.provider, station, self.start_text, self.end_text)
            except Exception as exc:
                result = {
                    "coverage_valid": None,
                    "coverage_fraction": None,
                    "coverage_message": f"coverage check unavailable: {exc}",
                }
            return identifier, result

        results: dict[str, dict[str, Any]] = {}
        if self.stations:
            workers = min(6, len(self.stations))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for identifier, result in pool.map(check, self.stations):
                    results[identifier] = result
        self.checked.emit(self.provider, results, f"{self.start_text} to {self.end_text}")


class UsgsSiteWorker(QThread):
    """Finds USGS stream gauges in a bounding box.

    Mirrors fetch_usgs_sites() in app/sfincs_agent.py so the gauges offered here
    are the same ones the build would consider: same endpoint, same filters, and
    bBox rounded to six decimals (more can return HTTP 400).
    """
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, bounds: tuple[float, float, float, float], parameter_cd: str = "00060") -> None:
        super().__init__()
        self.bounds, self.parameter_cd = bounds, parameter_cd

    def run(self) -> None:
        try:
            west, south, east, north = (round(float(value), 6) for value in self.bounds)
            query = urllib.parse.urlencode({
                "format": "rdb", "bBox": f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}",
                "parameterCd": str(self.parameter_cd), "hasDataTypeCd": "iv",
                "siteStatus": "all", "siteType": "ST",
            })
            request = urllib.request.Request(USGS_SITE_URL + "?" + query,
                                             headers={"User-Agent": f"AutoCF-Desktop/{APP_VERSION}"})
            with urllib.request.urlopen(request, timeout=180) as response:
                text = response.read().decode("utf-8", errors="replace")
            rows = [line for line in text.splitlines() if line and not line.startswith("#")]
            if len(rows) < 3:
                self.loaded.emit([])
                return
            # RDB: header row, a type/width row, then data.
            header = rows[0].split("\t")
            index = {name: position for position, name in enumerate(header)}
            required = ("site_no", "station_nm", "dec_lat_va", "dec_long_va")
            if any(name not in index for name in required):
                raise RuntimeError("USGS site response did not include the expected columns.")
            sites, seen = [], set()
            for row in rows[2:]:
                fields = row.split("\t")
                if len(fields) < len(header):
                    continue
                site_no = fields[index["site_no"]].strip()
                try:
                    lat = float(fields[index["dec_lat_va"]])
                    lon = float(fields[index["dec_long_va"]])
                except ValueError:
                    continue
                if not site_no or site_no in seen:
                    continue
                seen.add(site_no)
                sites.append({"site_no": site_no, "name": fields[index["station_nm"]].strip() or site_no,
                              "lat": lat, "lon": lon})
            self.loaded.emit(sites)
        except Exception as exc:
            self.failed.emit(str(exc))


class UsgsHwmWorker(QThread):
    """Loads the public USGS STN flood-event catalog or HWMs for one event."""
    loaded = Signal(object, str)
    failed = Signal(str)

    def __init__(self, event_id: int | None = None,
                 bounds: tuple[float, float, float, float] | None = None) -> None:
        super().__init__()
        self.event_id, self.bounds = event_id, bounds

    def run(self) -> None:
        try:
            if self.event_id is None:
                url, kind = USGS_STN_EVENTS_URL, "events"
            else:
                query = urllib.parse.urlencode({"Event": int(self.event_id)})
                url, kind = USGS_STN_HWM_URL + "?" + query, "marks"
            request = urllib.request.Request(url, headers={"User-Agent": f"AutoCF-Desktop/{APP_VERSION}"})
            with urllib.request.urlopen(request, timeout=180) as response:
                document = json.loads(response.read().decode("utf-8", errors="replace"))
            if not isinstance(document, list):
                raise RuntimeError("USGS STN returned an unexpected response.")
            if kind == "events":
                records = []
                for entry in document:
                    try:
                        records.append({
                            "id": int(entry["event_id"]),
                            "name": str(entry.get("event_name") or entry["event_id"]).strip(),
                            "start": str(entry.get("event_start_date") or ""),
                            "end": str(entry.get("event_end_date") or ""),
                        })
                    except Exception:
                        continue
                records.sort(key=lambda item: (item["start"], item["id"]), reverse=True)
            else:
                records = []
                west, south, east, north = self.bounds or (-180.0, -90.0, 180.0, 90.0)
                for entry in document:
                    try:
                        lat, lon = float(entry["latitude"]), float(entry["longitude"])
                    except Exception:
                        continue
                    if not (west <= lon <= east and south <= lat <= north):
                        continue
                    records.append({
                        "id": str(entry.get("hwm_id") or "").strip(),
                        "site_no": str(entry.get("site_no") or "").strip(),
                        "lat": lat, "lon": lon,
                        "elev_ft": entry.get("elev_ft"),
                        "datum": str(entry.get("verticalDatumName") or "").strip(),
                        "quality": str(entry.get("hwmQualityName") or "").strip(),
                        "event": str(entry.get("eventName") or "").strip(),
                        "state": str(entry.get("stateName") or "").strip(),
                    })
                records = [record for record in records if record["id"]]
                records.sort(key=lambda item: (item["site_no"], item["id"]))
            self.loaded.emit(records, kind)
        except Exception as exc:
            self.failed.emit(str(exc))


class PlaceSearchWorker(QThread):
    """Runs one explicit place search without blocking the map UI."""
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, query: str) -> None:
        super().__init__()
        self.query = query

    def run(self) -> None:
        try:
            params = urllib.parse.urlencode({
                "q": self.query, "format": "jsonv2", "limit": 1,
                "addressdetails": 0,
            })
            separator = "&" if "?" in GEOCODER_URL else "?"
            request = urllib.request.Request(
                GEOCODER_URL + separator + params,
                headers={
                    "User-Agent": f"AutoCF-Desktop/{APP_VERSION} (research flood-modeling application)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                document = json.loads(response.read().decode("utf-8", errors="replace"))
            if not isinstance(document, list) or not document:
                raise RuntimeError(f"No location found for {self.query!r}.")
            item = document[0]
            lon, lat = float(item["lon"]), float(item["lat"])
            raw_bounds = item.get("boundingbox") or []
            bounds = None
            if len(raw_bounds) == 4:
                south, north, west, east = map(float, raw_bounds)
                if west <= east and south <= north:
                    bounds = [west, south, east, north]
            self.found.emit({
                "query": self.query,
                "name": str(item.get("display_name") or self.query),
                "center": [lon, lat],
                "bounds": bounds,
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class IBTrACSWorker(QThread):
    loaded = Signal(object, str)
    failed = Signal(str)

    def __init__(self, cache_file: Path, refresh: bool) -> None:
        super().__init__()
        self.cache_file, self.refresh = cache_file, refresh

    def run(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            source = "cache"
            if self.refresh or not self.cache_file.is_file():
                request = urllib.request.Request(IBTRACS_URL, headers={"User-Agent": "AutoCF-Desktop/1.2"})
                with urllib.request.urlopen(request, timeout=180) as response:
                    payload = response.read()
                temporary = self.cache_file.with_suffix(".download")
                temporary.write_bytes(payload)
                temporary.replace(self.cache_file)
                source = "NOAA download"
            storms: dict[str, dict[str, Any]] = {}
            with self.cache_file.open("r", encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    sid, iso_time = (row.get("SID") or "").strip(), (row.get("ISO_TIME") or "").strip()
                    if not sid or not iso_time or not iso_time[:4].isdigit():
                        continue
                    try:
                        season = int((row.get("SEASON") or iso_time[:4]).strip())
                        lat, lon = float((row.get("LAT") or "").strip()), float((row.get("LON") or "").strip())
                    except Exception:
                        continue
                    storm = storms.setdefault(sid, {"sid": sid, "season": season, "name": (row.get("NAME") or "UNNAMED").strip() or "UNNAMED", "basins": set(), "points": []})
                    basin = (row.get("BASIN") or "").strip()
                    if basin:
                        storm["basins"].add(basin)
                    storm["points"].append({"time": iso_time, "lat": lat, "lon": lon, "wind": (row.get("WMO_WIND") or "").strip(), "pressure": (row.get("WMO_PRES") or "").strip(), "nature": (row.get("NATURE") or "").strip()})
            records = []
            for storm in storms.values():
                storm["basins"] = sorted(storm["basins"])
                storm["points"].sort(key=lambda point: point["time"])
                records.append(storm)
            self.loaded.emit(records, source)
        except Exception as exc:
            self.failed.emit(str(exc))


class SfincsReleaseWorker(QThread):
    """Checks GitHub and optionally installs the release source without blocking the UI."""
    succeeded = Signal(str, object)
    failed = Signal(str)
    progress = Signal(int, str)

    def __init__(self, action: str, software_root: Path) -> None:
        super().__init__()
        self.action, self.software_root = action, software_root

    def run(self) -> None:
        try:
            release = fetch_latest_release()
            if self.action == "check":
                self.succeeded.emit("check", release)
                return

            def report(downloaded: int, total: int) -> None:
                percent = int(downloaded * 100 / total) if total else 0
                detail = f"{human_size(downloaded)} / {human_size(total)}" if total else human_size(downloaded)
                self.progress.emit(percent, detail)

            target = download_release_source(
                release, self.software_root, report, self.isInterruptionRequested,
            )
            self.succeeded.emit("download", {"release": release, "path": str(target)})
        except DownloadCancelled:
            self.failed.emit("Download cancelled.")
        except Exception as exc:
            self.failed.emit(str(exc))


class WebMap(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.domain: dict[str, Any] | None = None
        self.track: list[dict[str, Any]] = []
        self.theme = "Dark"
        if QWebEngineView is None:
            self.web = None
            label = QLabel("Interactive map component unavailable. The remaining GUI functions still work.")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("mapFallback")
            layout.addWidget(label)
        else:
            self.web = QWebEngineView()
            self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            layout.addWidget(self.web)
            self.render()

    def set_layers(self, domain: dict[str, Any] | None, track: list[dict[str, Any]] | None = None) -> None:
        self.domain, self.track = domain, list(track or [])
        self.render()

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.render()

    def render(self) -> None:
        if self.web is None:
            return
        if not self.domain:
            domain = {"type": "FeatureCollection", "features": []}
        elif self.domain.get("type") == "FeatureCollection":
            domain = self.domain
        elif self.domain.get("type") == "Feature":
            domain = {"type": "FeatureCollection", "features": [self.domain]}
        else:
            domain = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": self.domain}]}
        track = {"type": "FeatureCollection", "features": []}
        if self.track:
            track["features"] = [{"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in self.track]}}]
        points = geojson_points(domain) + [(p["lon"], p["lat"]) for p in self.track]
        if points:
            west, east = min(p[0] for p in points), max(p[0] for p in points)
            south, north = min(p[1] for p in points), max(p[1] for p in points)
            fit = f"map.fitBounds([[{west},{south}],[{east},{north}]],{{padding:45,maxZoom:11}});"
        else:
            fit = "map.setCenter([-82.6,27.8]);map.setZoom(6);"
        background = "#f4f6f8" if self.theme == "Light" else "#17212a"
        page = f"""<!doctype html><html><head><meta charset='utf-8'>
<link href='https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css' rel='stylesheet'>
<style>html,body,#map{{height:100%;margin:0;background:{background}}}</style></head><body><div id='map'></div>
<script src='https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js'></script><script>
const map=new maplibregl.Map({{container:'map',style:{{version:8,sources:{{osm:{{type:'raster',tiles:['https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png'],tileSize:256,attribution:'© OpenStreetMap contributors'}}}},layers:[{{id:'osm',type:'raster',source:'osm'}}]}},center:[-82.6,27.8],zoom:6}});
map.addControl(new maplibregl.NavigationControl());map.on('load',()=>{{
map.addSource('domain',{{type:'geojson',data:{json.dumps(domain)}}});
map.addLayer({{id:'domain-fill',type:'fill',source:'domain',paint:{{'fill-color':'#20a4f3','fill-opacity':0.0}}}});
map.addLayer({{id:'domain-line',type:'line',source:'domain',paint:{{'line-color':'#0077b6','line-width':3}}}});
map.addSource('track',{{type:'geojson',data:{json.dumps(track)}}});
map.addLayer({{id:'track-line',type:'line',source:'track',paint:{{'line-color':'#e63946','line-width':4}}}});
map.addLayer({{id:'track-points',type:'circle',source:'track',paint:{{'circle-radius':3,'circle-color':'#ffb703','circle-stroke-width':1,'circle-stroke-color':'#6b1d1d'}}}});{fit}}});
</script></body></html>"""
        self.web.setHtml(page, QUrl("https://localhost/"))


STUDY_MAP_PAGE = """<!doctype html><html><head><meta charset='utf-8'>
<link href='https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css' rel='stylesheet'>
<style>
html,body,#map{height:100%;margin:0;background:__BACKGROUND__}
.acf-hint{position:absolute;left:10px;top:10px;z-index:5;background:rgba(20,26,32,.88);color:#fff;
 font:12px/1.45 'Segoe UI',system-ui,sans-serif;padding:7px 11px;border-radius:5px;max-width:340px;
 pointer-events:none;display:none}
.acf-hint b{color:#7fd3ff}
.maplibregl-popup-content{font:12px/1.4 'Segoe UI',system-ui,sans-serif;padding:7px 10px}
</style></head><body><div id='map'></div><div class='acf-hint' id='hint'></div>
<script src='https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js'></script><script>
window.__acfErrors=[];
window.addEventListener('error',e=>{window.__acfErrors.push(String(e.message||e.type))});
function acfDiag(){
 const probe=fn=>{try{return fn()}catch(e){return null}};
 return JSON.stringify({
  maplibre: probe(()=>typeof maplibregl!=='undefined')===true,
  mapObject: probe(()=>typeof map!=='undefined'&&map!==null)===true,
  styleLoaded: probe(()=>map.isStyleLoaded())===true,
  ready: probe(()=>AutoCF.ready)===true,
  webgl: probe(()=>maplibregl.supported?!!maplibregl.supported():
   !!(document.createElement('canvas').getContext('webgl2')||
      document.createElement('canvas').getContext('webgl')))===true,
  errors:(window.__acfErrors||[]).slice(0,4)});
}
const EMPTY={type:'FeatureCollection',features:[]};
const NONE='--none--';
const HINTS={
 polygon:"<b>Draw domain polygon</b><br>Click each corner. Double-click, right-click, or press Enter to close it. Esc cancels.",
 rectangle:"<b>Draw domain rectangle</b><br>Click one corner, then click the opposite corner. Esc cancels.",
 ocean_boundary:"<b>Draw ocean boundary line</b><br>Click roughly along the open-water edge. When you finish, AutoCF traces the matching section of the exact study-area/HUC boundary.",
 ocean_forcing_point:"<b>Place water-level points</b><br>Click on or near the ocean boundary line. Each click adds one forcing point.",
 observation_point:"<b>Place observation points</b><br>Click where model output should be sampled for evaluation.",
 river_source:"<b>Place river source points</b><br>Click where river discharge enters the domain.",
 roughness_override:"<b>Draw an NBS/adaptation polygon</b><br>Click each corner. Double-click, right-click, or press Enter to close it. Esc cancels.",
 station:"<b>Pick water-level stations</b><br>Click a catalog station marker to add or remove it as a water-level point.",
 obs_noaa:"<b>Select water-level observation gauges</b><br>Click a catalog station marker to select or deselect it for evaluation.",
 hwm:"<b>Select USGS high-water marks</b><br>Click a high-water-mark marker to select or deselect it for evaluation.",
  huc:"<b>Pick watersheds</b><br>Click polygons to add or remove them. AutoCF dissolves adjacent selections into one study-area boundary.",
 usgs:"<b>Pick USGS gauges</b><br>Click a gauge marker to add or remove it as a river source point."};

const AutoCF={
 rev:0, mode:'none', seq:0, pending:[], rectStart:null, hover:null, ready:false,
 draft:{domain:[],ocean_boundary:[],ocean_forcing_point:[],observation_point:[],river_source:[],roughness_override:[]},
 overrideProperties:{name:'Adaptation',lulc_code:1001,manning_n:0.04},
  selectedHucs:[], hucSelectionRev:0, hucCount:0, hucData:EMPTY, selectedStations:[], selectedUsgs:[],
 selectedObsNoaa:[],selectedHwm:[],stationCount:0,obsNoaaCount:0,hwmCount:0,usgsCount:0,
 demOn:false, terrainOn:false, exaggeration:1.5, demImage:null,
 demImageLoaded:false, demImageError:null, demImageToken:0,

 bump(){this.rev++;this.redraw();},
 fc(features){return {type:'FeatureCollection',features:features};},
 allDraft(){let out=[];for(const role in this.draft){for(const f of this.draft[role])out.push(f);}return out;},

 setMode(mode){
  this.mode=mode; this.pending=[]; this.rectStart=null;
  const hint=document.getElementById('hint');
  if(HINTS[mode]){hint.innerHTML=HINTS[mode];hint.style.display='block';}else{hint.style.display='none';}
  map.getCanvas().style.cursor=(mode==='none')?'':'crosshair';
  if(mode==='none'){map.doubleClickZoom.enable();}else{map.doubleClickZoom.disable();}
  this.updateCandidateVisibility();
  this.bump();
 },

 updateCandidateVisibility(){
  const groups={
   huc:['huc-fill','huc-line','huc-selected'],
   station:['stations-circle','stations-selected'],
   obs_noaa:['obsnoaa-circle','obsnoaa-selected'],
   hwm:['hwm-circle','hwm-selected'],
   usgs:['usgs-circle','usgs-selected']};
  for(const kind in groups){for(const layer of groups[kind]){
   if(map.getLayer(layer))map.setLayoutProperty(
    layer,'visibility',this.mode===kind?'visible':'none');
  }}
 },

 add(role,geometry,properties){
   const f={type:'Feature',properties:Object.assign({role:role,_seq:++this.seq},properties||{}),geometry:geometry};
   if(role==='domain'){
    this.draft.domain=[f];
    // The loaded-file outline is only a reference. Once the user creates a
    // replacement AOI, remove that old outline so it cannot look like a
    // rectangular second domain beside the editable HUC/drawn polygon.
    if(map.getSource('domain'))map.getSource('domain').setData(EMPTY);
   }else{this.draft[role].push(f);}
   this.bump(); return f;
  },
  nextId(role){
   const prefixes={ocean_forcing_point:'bnd',observation_point:'obs',river_source:'riv'};
   const prefix=prefixes[role]||'pt';
   const used=new Set((this.draft[role]||[]).map(
    f=>String((f.properties||{}).ID||'').toLowerCase()));
   let index=1,candidate='';
   do{candidate=prefix+String(index++).padStart(3,'0');}while(used.has(candidate.toLowerCase()));
   return candidate;
  },
  removeById(role,id){
   if(this.draft[role]===undefined)return;
   this.draft[role]=this.draft[role].filter(
    f=>String((f.properties||{}).ID||'')!==String(id));
   this.bump();
  },
  focusPoint(role,id){
   const feature=(this.draft[role]||[]).find(
    f=>String((f.properties||{}).ID||'')===String(id));
   if(feature&&feature.geometry&&feature.geometry.type==='Point'){
    map.easeTo({center:feature.geometry.coordinates,zoom:Math.max(map.getZoom(),13),duration:500});
   }
  },
  replaceDomain(feature,resetCoast){
   const copy=JSON.parse(JSON.stringify(feature));
   copy.properties=Object.assign({},copy.properties||{},{role:'domain',_seq:++this.seq});
   this.draft.domain=[copy];
   if(map.getSource('domain'))map.getSource('domain').setData(EMPTY);
   if(resetCoast){
    this.draft.ocean_boundary=[];
    this.draft.ocean_forcing_point=[];
    this.selectedStations=[];
   }
   this.pending=[];this.rectStart=null;this.bump();
  },
 setOverrideProperties(properties){this.overrideProperties=Object.assign({},properties||{});},
 clearRole(role){if(this.draft[role]){this.draft[role]=[];this.bump();}},
 clearAll(){for(const role in this.draft)this.draft[role]=[];this.pending=[];this.rectStart=null;this.seq=0;this.bump();},
 undo(){
  if(this.pending.length){this.pending.pop();this.bump();return;}
  let best=null,bestRole=null;
  for(const role in this.draft){for(const f of this.draft[role]){
   if(!best||f.properties._seq>best.properties._seq){best=f;bestRole=role;}}}
  if(best){this.draft[bestRole]=this.draft[bestRole].filter(f=>f!==best);this.bump();}
 },

 near(a,b){const pa=map.project(a),pb=map.project(b);return Math.hypot(pa.x-pb.x,pa.y-pb.y)<8;},
 dedupe(points){const out=[];for(const p of points){if(!out.length||!this.near(out[out.length-1],p))out.push(p);}return out;},

 // Geometry helpers used to replace an ocean-boundary sketch with the exact
 // matching arc of the study-area perimeter. Calculations use a locally scaled
 // longitude axis, which is sufficiently accurate for selecting and projecting
 // points onto a HUC-scale boundary while preserving the original HUC vertices.
 metricPoint(p,lat0){return [p[0]*Math.cos(lat0*Math.PI/180),p[1]];},
 projectToSegment(p,a,b){
  const lat0=(p[1]+a[1]+b[1])/3;
  const pp=this.metricPoint(p,lat0),aa=this.metricPoint(a,lat0),bb=this.metricPoint(b,lat0);
  const dx=bb[0]-aa[0],dy=bb[1]-aa[1],den=dx*dx+dy*dy;
  const t=den?Math.max(0,Math.min(1,((pp[0]-aa[0])*dx+(pp[1]-aa[1])*dy)/den)):0;
  const coordinate=[a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])];
  const qq=this.metricPoint(coordinate,lat0);
  return {coordinate:coordinate,t:t,d2:(pp[0]-qq[0])**2+(pp[1]-qq[1])**2};
 },
 openRing(raw){
  const ring=(raw||[]).map(p=>[Number(p[0]),Number(p[1])]);
  if(ring.length>1){const a=ring[0],b=ring[ring.length-1];
   if(Math.abs(a[0]-b[0])<1e-12&&Math.abs(a[1]-b[1])<1e-12)ring.pop();}
  return ring;
 },
 domainOuterRings(){
  if(!this.draft.domain.length)return [];
  const g=this.draft.domain[0].geometry||{};
  if(g.type==='Polygon')return g.coordinates&&g.coordinates.length?[this.openRing(g.coordinates[0])]:[];
  if(g.type==='MultiPolygon')return (g.coordinates||[]).filter(p=>p&&p.length).map(p=>this.openRing(p[0]));
  return [];
 },
 nearestOnRing(point,ring){
  let best=null;
  for(let i=0;i<ring.length;i++){
   const hit=this.projectToSegment(point,ring[i],ring[(i+1)%ring.length]);
   if(!best||hit.d2<best.d2)best=Object.assign({segment:i},hit);
  }
  return best;
 },
 forwardRingArc(ring,start,end){
  if(start.segment===end.segment&&start.t<=end.t)return [start.coordinate,end.coordinate];
  const out=[start.coordinate];let i=(start.segment+1)%ring.length;
  for(let guard=0;guard<=ring.length;guard++){
   out.push(ring[i]);
   if(i===end.segment)break;
   i=(i+1)%ring.length;
  }
  out.push(end.coordinate);return out;
 },
 pointLineDistance2(point,line){
  let best=Infinity;
  for(let i=0;i+1<line.length;i++)best=Math.min(best,this.projectToSegment(point,line[i],line[i+1]).d2);
  return best;
 },
 pathScore(path,sketch,endpointPenalty){
  let fromSketch=0;for(const p of sketch)fromSketch+=this.pointLineDistance2(p,path);
  fromSketch/=Math.max(1,sketch.length);
  // Symmetric scoring rejects the long way around a closed watershed even when
  // both possible arcs share the same snapped endpoints.
  let fromPath=0,samples=0;
  const stride=Math.max(1,Math.floor(path.length/250));
  for(let i=0;i<path.length;i+=stride){fromPath+=this.pointLineDistance2(path[i],sketch);samples++;}
  return fromSketch+fromPath/Math.max(1,samples)+endpointPenalty;
 },
 cleanCoordinates(points){
  const out=[];
  for(const p of points){const q=[Number(p[0]),Number(p[1])];const last=out[out.length-1];
   if(!last||Math.abs(last[0]-q[0])>1e-12||Math.abs(last[1]-q[1])>1e-12)out.push(q);}
  return out;
 },
 snapOceanBoundary(sketch){
  let best=null;
  for(const ring of this.domainOuterRings()){
   if(ring.length<3)continue;
   const start=this.nearestOnRing(sketch[0],ring),end=this.nearestOnRing(sketch[sketch.length-1],ring);
   const forward=this.forwardRingArc(ring,start,end);
   const reverse=this.forwardRingArc(ring,end,start).reverse();
   const endpointPenalty=start.d2+end.d2;
   for(const path of [forward,reverse]){
    const score=this.pathScore(path,sketch,endpointPenalty);
    if(!best||score<best.score)best={score:score,coordinates:this.cleanCoordinates(path)};
   }
  }
  return best&&best.coordinates.length>=2?best.coordinates:null;
 },

 finish(){
  const points=this.dedupe(this.pending);
  if(this.mode==='polygon'){
   if(points.length<3){return 'A domain polygon needs at least three corners.';}
   const ring=points.slice(); ring.push(ring[0]);
   this.pending=[]; this.add('domain',{type:'Polygon',coordinates:[ring]},{source:'drawn'});
   this.setMode('none'); return '';
  }
  if(this.mode==='roughness_override'){
   if(points.length<3){return 'An adaptation polygon needs at least three corners.';}
   const ring=points.slice(); ring.push(ring[0]);
   this.pending=[]; this.add('roughness_override',{type:'Polygon',coordinates:[ring]},this.overrideProperties);
   this.setMode('none'); return '';
  }
  if(this.mode==='ocean_boundary'){
   if(points.length<2){return 'An ocean boundary line needs at least two points.';}
   const snapped=this.snapOceanBoundary(points);
   if(!snapped){return 'Draw or load a study-area/HUC domain before creating the ocean boundary.';}
   this.pending=[];
   this.add('ocean_boundary',{type:'LineString',coordinates:snapped},
    {source:'drawn',snapped_to_domain:true,snap_source:'study_area_boundary'});
   this.setMode('none'); return '';
  }
  this.pending=[];this.bump();return '';
 },
 cancel(){this.pending=[];this.rectStart=null;this.bump();},

 setDraft(payload){
  for(const role in this.draft)this.draft[role]=[];
  this.seq=0;
  for(const f of (payload.features||[])){
   const role=(f.properties&&f.properties.role)||'';
   if(this.draft[role]!==undefined){
    const copy=JSON.parse(JSON.stringify(f));
    copy.properties=copy.properties||{}; copy.properties._seq=++this.seq; copy.properties.role=role;
    if(role==='domain'){this.draft.domain=[copy];}else{this.draft[role].push(copy);}
   }
  }
  this.bump();
 },
 addFeatures(payload){
  for(const f of (payload.features||[])){
   const role=(f.properties&&f.properties.role)||'';
   if(this.draft[role]!==undefined){
    const id=f.properties&&f.properties.ID;
    if(id)this.draft[role]=this.draft[role].filter(old=>!(old.properties&&old.properties.ID===id));
    this.add(role,f.geometry,f.properties);
   }
  }
 },
 setRole(role,payload){
  if(this.draft[role]===undefined)return;
  this.draft[role]=[];
  for(const f of (payload.features||[])){
   if(!f.geometry)continue;
   const copy=JSON.parse(JSON.stringify(f));
   copy.properties=Object.assign({},copy.properties||{},{role:role,_seq:++this.seq});
   this.draft[role].push(copy);
  }
  this.bump();
 },
 syncManaged(role,payload,group){
  if(this.draft[role]===undefined)return;
  const incomingIds=new Set((payload.features||[]).map(
   f=>String((f.properties||{}).ID||'')).filter(Boolean));
  this.draft[role]=this.draft[role].filter(
   f=>String((f.properties||{}).selection_group||'')!==String(group)&&
      !incomingIds.has(String((f.properties||{}).ID||'')));
  for(const f of (payload.features||[])){
   if(!f.geometry)continue;
   const copy=JSON.parse(JSON.stringify(f));
   copy.properties=Object.assign({},copy.properties||{},
    {role:role,selection_group:String(group),_seq:++this.seq});
   this.draft[role].push(copy);
  }
  this.bump();
 },
 draftGeoJSON(){
  const order=['domain','ocean_boundary','ocean_forcing_point','observation_point','river_source','roughness_override'];
  let out=[];
  for(const role of order){for(const f of this.draft[role]){
   const copy=JSON.parse(JSON.stringify(f)); delete copy.properties._seq; out.push(copy);}}
  return JSON.stringify(this.fc(out));
 },

 setHuc(payload,revision){
  this.hucData=payload; this.hucCount=(payload.features||[]).length; this.selectedHucs=[];
  const requested=Number(revision);
  if(Number.isFinite(requested))this.hucSelectionRev=Math.max(this.hucSelectionRev,requested);
  if(map.getSource('huc'))map.getSource('huc').setData(payload);
  this.bump();
  if(this.hucCount)this.fitData(payload);
 },
 selectHucs(codes,revision){
  this.selectedHucs=(codes||[]).map(String);
  const requested=Number(revision);
  this.hucSelectionRev=Number.isFinite(requested)?Math.max(this.hucSelectionRev,requested):this.hucSelectionRev+1;
  this.bump();
 },
 selectHuc(code,revision){this.selectHucs((code===null||code===undefined)?[]:[String(code)],revision);},
 toggleHuc(code){
  code=String(code);const at=this.selectedHucs.indexOf(code);
  if(at>=0)this.selectedHucs.splice(at,1);else this.selectedHucs.push(code);
  this.hucSelectionRev++;this.bump();
 },
 hucGeometry(){
  const chosen=(this.hucData.features||[]).filter(f=>this.selectedHucs.includes(String(f.properties.code)));
  return chosen.length?JSON.stringify(this.fc(chosen)):'';
 },

 setStations(payload){this.stationCount=(payload.features||[]).length;if(map.getSource('stations'))map.getSource('stations').setData(payload);this.bump();},
 setSelectedStations(ids){this.selectedStations=ids.slice();this.bump();},
 toggleStation(id){
  id=String(id);
  const at=this.selectedStations.indexOf(id);
  if(at>=0)this.selectedStations.splice(at,1);else this.selectedStations.push(id);
  this.bump();
 },

 setObsNoaa(payload){this.obsNoaaCount=(payload.features||[]).length;if(map.getSource('obsnoaa'))map.getSource('obsnoaa').setData(payload);this.bump();},
 setSelectedObsNoaa(ids){this.selectedObsNoaa=ids.slice();this.bump();},
 toggleObsNoaa(id){
  id=String(id);const at=this.selectedObsNoaa.indexOf(id);
  if(at>=0)this.selectedObsNoaa.splice(at,1);else this.selectedObsNoaa.push(id);this.bump();
 },
 setHwm(payload){this.hwmCount=(payload.features||[]).length;if(map.getSource('hwm'))map.getSource('hwm').setData(payload);this.bump();},
 setSelectedHwm(ids){this.selectedHwm=ids.slice();this.bump();},
 toggleHwm(id){
  id=String(id);const at=this.selectedHwm.indexOf(id);
  if(at>=0)this.selectedHwm.splice(at,1);else this.selectedHwm.push(id);this.bump();
 },

 setUsgs(payload){this.usgsCount=(payload.features||[]).length;if(map.getSource('usgs'))map.getSource('usgs').setData(payload);this.bump();},
 setSelectedUsgs(ids){this.selectedUsgs=ids.slice();this.bump();},
 toggleUsgs(id){
  id=String(id);
  const at=this.selectedUsgs.indexOf(id);
  if(at>=0)this.selectedUsgs.splice(at,1);else this.selectedUsgs.push(id);
  this.bump();
 },

 setDem(on){
  this.demOn=!!on;
  if(map.getLayer('dem-hillshade'))
   map.setLayoutProperty('dem-hillshade','visibility',this.demOn?'visible':'none');
  this.applyTerrain(); this.bump();
 },
 setTerrain3d(on,exaggeration){
  this.terrainOn=!!on;
  if(exaggeration)this.exaggeration=Number(exaggeration);
  this.applyTerrain(); this.bump();
 },
 applyTerrain(){
  // exaggeration 0 keeps the view flat while still loading DEM tiles, so
  // elevation queries work without switching the map into 3D.
  if(this.demOn||this.terrainOn){
   map.setTerrain({source:'dem',exaggeration:this.terrainOn?this.exaggeration:0});
  }else{
   map.setTerrain(null);
  }
 },
 setDemOpacity(value){
  if(map.getLayer('dem-hillshade'))
   map.setPaintProperty('dem-hillshade','hillshade-exaggeration',Number(value));
 },
 setDemExtent(payload){if(map.getSource('demextent'))map.getSource('demextent').setData(payload);},
 setDemImage(payload){
   const token=++this.demImageToken;
   if(map.getLayer('dem-image'))map.removeLayer('dem-image');
   if(map.getSource('demimg'))map.removeSource('demimg');
   this.demImage=null;this.demImageLoaded=false;this.demImageError=null;
   if(!payload||!payload.uri){this.bump();return;}
   this.demImage={west:payload.west,south:payload.south,east:payload.east,north:payload.north};
   try{
    map.addSource('demimg',{type:'image',url:payload.uri,coordinates:[
     [payload.west,payload.north],[payload.east,payload.north],
     [payload.east,payload.south],[payload.west,payload.south]]});
    map.addLayer({id:'dem-image',type:'raster',source:'demimg',
     paint:{'raster-opacity':payload.opacity===undefined?0.85:Number(payload.opacity)}},'demextent-line');
    const loaded=()=>{
     if(token!==this.demImageToken||!map.getSource('demimg'))return;
     if(map.isSourceLoaded('demimg')){this.demImageLoaded=true;this.demImageError=null;this.bump();}
    };
    const sourceData=e=>{
     if(token!==this.demImageToken){map.off('sourcedata',sourceData);return;}
     if(e.sourceId==='demimg'){
      loaded();
      if(this.demImageLoaded)map.off('sourcedata',sourceData);
     }
    };
    map.once('idle',loaded);
    map.on('sourcedata',sourceData);
   }catch(e){
    this.demImageError=String((e&&e.message)||e||'topobathy image could not be added');
   }
   this.bump();
  },
 setDemImageOpacity(value){
  if(map.getLayer('dem-image'))map.setPaintProperty('dem-image','raster-opacity',Number(value));
 },
 showDemImage(on){
  if(map.getLayer('dem-image'))
   map.setLayoutProperty('dem-image','visibility',on?'visible':'none');
  this.bump();
 },
 fitDemImage(){
  if(!this.demImage)return;
  map.fitBounds([[this.demImage.west,this.demImage.south],[this.demImage.east,this.demImage.north]],
   {padding:40,duration:700});
 },
 elevationAt(lon,lat){
  try{
   const v=map.queryTerrainElevation(new maplibregl.LngLat(Number(lon),Number(lat)),{exaggerated:false});
   return JSON.stringify(v===null||v===undefined?null:v);
  }catch(e){return JSON.stringify(null);}
 },

 redraw(){
  if(!this.ready)return;
  map.getSource('draft').setData(this.fc(this.allDraft()));
  const pendingFeatures=[];
  let live=this.pending.slice();
  if(this.hover&&this.pending.length&&(this.mode==='polygon'||this.mode==='ocean_boundary'||this.mode==='roughness_override'))live=live.concat([this.hover]);
  if(this.mode==='rectangle'&&this.rectStart&&this.hover){
   const a=this.rectStart,b=this.hover;
   pendingFeatures.push({type:'Feature',properties:{},geometry:{type:'LineString',
    coordinates:[[a[0],a[1]],[b[0],a[1]],[b[0],b[1]],[a[0],b[1]],[a[0],a[1]]]}});
  }else if(live.length>1){
   const coords=live.slice();
    if((this.mode==='polygon'||this.mode==='roughness_override')&&coords.length>2)coords.push(coords[0]);
   pendingFeatures.push({type:'Feature',properties:{},geometry:{type:'LineString',coordinates:coords}});
  }
  for(const p of this.pending)pendingFeatures.push({type:'Feature',properties:{},geometry:{type:'Point',coordinates:p}});
  map.getSource('pending').setData(this.fc(pendingFeatures));
  map.setFilter('huc-selected',['in',['to-string',['get','code']],['literal',this.selectedHucs]]);
  map.setFilter('stations-selected',['in',['get','id'],['literal',this.selectedStations]]);
  map.setFilter('usgs-selected',['in',['get','id'],['literal',this.selectedUsgs]]);
  map.setFilter('obsnoaa-selected',['in',['get','id'],['literal',this.selectedObsNoaa]]);
  map.setFilter('hwm-selected',['in',['get','id'],['literal',this.selectedHwm]]);
 },

 summary(){
   const counts={};
   for(const role in this.draft)counts[role]=this.draft[role].length;
   const demVisible=!!map.getLayer('dem-image')&&map.getLayoutProperty('dem-image','visibility')!=='none';
   return JSON.stringify({ready:this.ready,rev:this.rev,mode:this.mode,pending:this.pending.length,
    counts:counts,huc:(this.selectedHucs[0]||null),hucs:this.selectedHucs.slice(),
    huc_selection_rev:this.hucSelectionRev,huc_count:this.hucCount,dem_image:!!this.demImage,
    dem_image_loaded:this.demImageLoaded,dem_image_visible:demVisible,dem_image_error:this.demImageError,
    domain_bounds:this.boundsOf(this.draft.domain),
    domain_geometries:this.draft.domain.map(feature=>feature.geometry),
    candidate_counts:{huc:this.hucCount,station:this.stationCount,obs_noaa:this.obsNoaaCount,
     hwm:this.hwmCount,usgs:this.usgsCount},
    stations:this.selectedStations.slice(),usgs:this.selectedUsgs.slice(),
    obs_noaa:this.selectedObsNoaa.slice(),hwm:this.selectedHwm.slice()});
 },

 setBasemap(spec){
  if(map.getLayer('basemap'))map.removeLayer('basemap');
  if(map.getSource('basemap'))map.removeSource('basemap');
  if(spec.tiles&&spec.tiles.length){
   map.addSource('basemap',{type:'raster',tiles:spec.tiles,tileSize:256,
    maxzoom:spec.maxzoom||19,attribution:spec.attribution||''});
    // Keep the opaque basemap directly above the background. Prepared
    // topobathy, hillshade, extents, and all study-area overlays must remain
    // visible above it.
    map.addLayer({id:'basemap',type:'raster',source:'basemap'},'dem-hillshade');
  }
 },
 setBackground(color){map.setPaintProperty('bg','background-color',color);},
 setLayer(name,payload){if(map.getSource(name))map.getSource(name).setData(payload);},
 fitData(payload){
  let west=180,south=90,east=-180,north=-90,found=false;
  const walk=n=>{
   if(Array.isArray(n)&&n.length>=2&&typeof n[0]==='number'&&typeof n[1]==='number'){
    found=true;west=Math.min(west,n[0]);east=Math.max(east,n[0]);
    south=Math.min(south,n[1]);north=Math.max(north,n[1]);}
   else if(Array.isArray(n))n.forEach(walk);};
  for(const f of (payload.features||[]))if(f.geometry)walk(f.geometry.coordinates);
  if(found)map.fitBounds([[west,south],[east,north]],{padding:50,maxZoom:12,duration:600});
 },
 fitDraft(){this.fitData(this.fc(this.allDraft()));},
 zoomToPlace(place){
  if(!place||!Array.isArray(place.center)||place.center.length<2)return;
  // Keep required search-data credit inside MapLibre's normal attribution
  // control instead of occupying space beside the search field.
  if(!map.getSource('place-search-credit')){
   map.addSource('place-search-credit',{type:'geojson',data:EMPTY,
    attribution:"Place search © <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap contributors</a>"});
  }
  const center=[Number(place.center[0]),Number(place.center[1])];
  const b=place.bounds;
  if(Array.isArray(b)&&b.length===4&&b.every(Number.isFinite)&&
     (Math.abs(b[2]-b[0])>1e-6||Math.abs(b[3]-b[1])>1e-6)){
   map.fitBounds([[b[0],b[1]],[b[2],b[3]]],{padding:55,maxZoom:13,duration:800});
  }else{
   map.flyTo({center:center,zoom:12,duration:800});
  }
 },
 boundsOf(features){
  let west=180,south=90,east=-180,north=-90,found=false;
  const walk=n=>{
   if(Array.isArray(n)&&n.length>=2&&typeof n[0]==='number'&&typeof n[1]==='number'){
    found=true;west=Math.min(west,n[0]);east=Math.max(east,n[0]);
    south=Math.min(south,n[1]);north=Math.max(north,n[1]);}
   else if(Array.isArray(n))n.forEach(walk);};
  for(const f of features||[])if(f.geometry)walk(f.geometry.coordinates);
  return found?[west,south,east,north]:null;
 },
 viewBounds(){const b=map.getBounds();return JSON.stringify([b.getWest(),b.getSouth(),b.getEast(),b.getNorth()]);}
};

const map=new maplibregl.Map({container:'map',
 style:{version:8,sources:{},layers:[{id:'bg',type:'background',paint:{'background-color':'__BACKGROUND__'}}]},
 center:[-82.6,27.8],zoom:6});
map.addControl(new maplibregl.NavigationControl());
map.addControl(new maplibregl.ScaleControl({unit:'metric'}));
map.on('error',e=>{
 const message=String((e&&e.error&&e.error.message)||'map error');
 window.__acfErrors.push(message);
 if(e&&e.sourceId==='demimg'){
  AutoCF.demImageLoaded=false;AutoCF.demImageError=message;AutoCF.bump();
 }
});

map.on('load',()=>{
 for(const name of ['huc','domain','track','draft','stations','usgs','obsnoaa','hwm','demextent','pending'])
  map.addSource(name,{type:'geojson',data:EMPTY});

 map.addSource('dem',{type:'raster-dem',encoding:'terrarium',tileSize:256,maxzoom:14,
  tiles:['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
  attribution:'Elevation: Mapzen Terrarium (SRTM, NED, and others)'});
 map.addLayer({id:'dem-hillshade',type:'hillshade',source:'dem',layout:{visibility:'none'},
  paint:{'hillshade-exaggeration':0.5,'hillshade-shadow-color':'#1b2733','hillshade-highlight-color':'#f3f7fa'}});
 map.addLayer({id:'demextent-line',type:'line',source:'demextent',
  paint:{'line-color':'#ffd166','line-width':2,'line-dasharray':[5,3]}});

 map.addLayer({id:'huc-fill',type:'fill',source:'huc',layout:{visibility:'none'},paint:{'fill-color':'#6c8ea4','fill-opacity':0.14}});
 map.addLayer({id:'huc-line',type:'line',source:'huc',
  layout:{visibility:'none'},
  paint:{'line-color':'#4a6f88','line-width':1.4,'line-dasharray':[3,2]}});
 map.addLayer({id:'huc-selected',type:'line',source:'huc',filter:['==',['get','code'],NONE],
  layout:{visibility:'none'},
  paint:{'line-color':'#00d1b2','line-width':3.6}});
 map.addLayer({id:'domain-fill',type:'fill',source:'domain',paint:{'fill-color':'#20a4f3','fill-opacity':0.0}});
 map.addLayer({id:'domain-line',type:'line',source:'domain',
  paint:{'line-color':'#0077b6','line-width':2,'line-dasharray':[4,2]}});
 map.addLayer({id:'track-line',type:'line',source:'track',paint:{'line-color':'#e63946','line-width':3}});
 map.addLayer({id:'track-points',type:'circle',source:'track',paint:{'circle-radius':2.5,'circle-color':'#ffb703'}});

 map.addLayer({id:'draft-fill',type:'fill',source:'draft',filter:['==',['get','role'],'domain'],
  paint:{'fill-color':'#20a4f3','fill-opacity':0.0}});
  map.addLayer({id:'draft-line',type:'line',source:'draft',
  filter:['match',['get','role'],['domain','ocean_boundary'],true,false],
  paint:{'line-color':['match',['get','role'],'ocean_boundary','#ff9f1c','#0077b6'],
         'line-width':['match',['get','role'],'ocean_boundary',4,2.6]}});
  map.addLayer({id:'roughness-override-fill',type:'fill',source:'draft',
   filter:['==',['get','role'],'roughness_override'],
   paint:{'fill-color':'#00b894','fill-opacity':0.32}});
  map.addLayer({id:'roughness-override-line',type:'line',source:'draft',
   filter:['==',['get','role'],'roughness_override'],
   paint:{'line-color':'#007f68','line-width':3,'line-dasharray':[2,1]}});

 map.addLayer({id:'stations-circle',type:'circle',source:'stations',
  layout:{visibility:'none'},
  paint:{'circle-radius':5,'circle-color':'#12232e','circle-stroke-width':2,'circle-stroke-color':'#5bc0eb'}});
 map.addLayer({id:'stations-selected',type:'circle',source:'stations',
  filter:['in',['get','id'],['literal',[]]],
  layout:{visibility:'none'},
  paint:{'circle-radius':7,'circle-color':'#e63946','circle-stroke-width':2,'circle-stroke-color':'#ffffff'}});

 map.addLayer({id:'obsnoaa-circle',type:'circle',source:'obsnoaa',
  layout:{visibility:'none'},
  paint:{'circle-radius':5,'circle-color':'#ffd166','circle-stroke-width':1.8,'circle-stroke-color':'#614700'}});
 map.addLayer({id:'obsnoaa-selected',type:'circle',source:'obsnoaa',filter:['in',['get','id'],['literal',[]]],
  layout:{visibility:'none'},
  paint:{'circle-radius':7,'circle-color':'#8ac926','circle-stroke-width':2,'circle-stroke-color':'#ffffff'}});
 map.addLayer({id:'hwm-circle',type:'circle',source:'hwm',
  layout:{visibility:'none'},
  paint:{'circle-radius':5,'circle-color':'#f4a261','circle-stroke-width':1.8,'circle-stroke-color':'#5d3300'}});
 map.addLayer({id:'hwm-selected',type:'circle',source:'hwm',filter:['in',['get','id'],['literal',[]]],
  layout:{visibility:'none'},
  paint:{'circle-radius':7,'circle-color':'#2ec4b6','circle-stroke-width':2,'circle-stroke-color':'#ffffff'}});

 map.addLayer({id:'usgs-circle',type:'circle',source:'usgs',
  layout:{visibility:'none'},
  paint:{'circle-radius':5,'circle-color':'#241a33','circle-stroke-width':2,'circle-stroke-color':'#b07cf0'}});
 map.addLayer({id:'usgs-selected',type:'circle',source:'usgs',filter:['in',['get','id'],['literal',[]]],
  layout:{visibility:'none'},
  paint:{'circle-radius':7,'circle-color':'#9d4edd','circle-stroke-width':2,'circle-stroke-color':'#ffffff'}});

 map.addLayer({id:'draft-point',type:'circle',source:'draft',filter:['==',['geometry-type'],'Point'],
  paint:{'circle-radius':6,
   'circle-color':['match',['get','role'],'observation_point','#8ac926','river_source','#9d4edd','#e63946'],
   'circle-stroke-width':1.6,'circle-stroke-color':'#ffffff'}});

 map.addLayer({id:'pending-line',type:'line',source:'pending',
  paint:{'line-color':'#00d1b2','line-width':2,'line-dasharray':[2,1.5]}});
 map.addLayer({id:'pending-point',type:'circle',source:'pending',filter:['==',['geometry-type'],'Point'],
  paint:{'circle-radius':4,'circle-color':'#00d1b2','circle-stroke-width':1.4,'circle-stroke-color':'#04252b'}});

 AutoCF.setBasemap(__BASEMAP__);
 AutoCF.ready=true;
 AutoCF.bump();
});

const hitLayers=names=>names.filter(n=>map.getLayer(n));

map.on('click',e=>{
 const ll=[Number(e.lngLat.lng.toFixed(6)),Number(e.lngLat.lat.toFixed(6))];
 const mode=AutoCF.mode;
 if(mode==='station'){
  const hits=map.queryRenderedFeatures(e.point,{layers:hitLayers(['stations-selected','stations-circle'])});
  if(hits.length)AutoCF.toggleStation(hits[0].properties.id);
  return;
 }
 if(mode==='obs_noaa'){
  const hits=map.queryRenderedFeatures(e.point,{layers:hitLayers(['obsnoaa-selected','obsnoaa-circle'])});
  if(hits.length)AutoCF.toggleObsNoaa(hits[0].properties.id);
  return;
 }
 if(mode==='hwm'){
  const hits=map.queryRenderedFeatures(e.point,{layers:hitLayers(['hwm-selected','hwm-circle'])});
  if(hits.length)AutoCF.toggleHwm(hits[0].properties.id);
  return;
 }
 if(mode==='usgs'){
  const hits=map.queryRenderedFeatures(e.point,{layers:hitLayers(['usgs-selected','usgs-circle'])});
  if(hits.length)AutoCF.toggleUsgs(hits[0].properties.id);
  return;
 }
 if(mode==='huc'){
  const hits=map.queryRenderedFeatures(e.point,{layers:hitLayers(['huc-fill'])});
  if(hits.length)AutoCF.toggleHuc(hits[0].properties.code);
  return;
 }
 if(mode==='polygon'||mode==='ocean_boundary'||mode==='roughness_override'){AutoCF.pending.push(ll);AutoCF.bump();return;}
 if(mode==='rectangle'){
  if(!AutoCF.rectStart){AutoCF.rectStart=ll;AutoCF.bump();}
  else{
   const a=AutoCF.rectStart,b=ll; AutoCF.rectStart=null;
   AutoCF.add('domain',{type:'Polygon',
    coordinates:[[[a[0],a[1]],[b[0],a[1]],[b[0],b[1]],[a[0],b[1]],[a[0],a[1]]]]},{source:'drawn'});
   AutoCF.setMode('none');
  }
  return;
 }
 if(mode==='ocean_forcing_point'||mode==='observation_point'||mode==='river_source'){
   AutoCF.add(mode,{type:'Point',coordinates:ll},{source:'drawn',ID:AutoCF.nextId(mode),name:AutoCF.nextId(mode)});
  }
});

map.on('mousemove',e=>{
 AutoCF.hover=[e.lngLat.lng,e.lngLat.lat];
 if(AutoCF.pending.length||AutoCF.rectStart)AutoCF.redraw();
});
map.on('dblclick',e=>{
 if(AutoCF.mode==='polygon'||AutoCF.mode==='ocean_boundary'||AutoCF.mode==='roughness_override'){e.preventDefault();AutoCF.finish();}});
map.on('contextmenu',e=>{
 if(AutoCF.mode==='polygon'||AutoCF.mode==='ocean_boundary'||AutoCF.mode==='roughness_override'){e.preventDefault();AutoCF.finish();}});
document.addEventListener('keydown',ev=>{
 if(ev.key==='Escape'){AutoCF.cancel();AutoCF.setMode('none');}
 else if(ev.key==='Enter'){AutoCF.finish();}
 else if(ev.ctrlKey&&ev.key.toLowerCase()==='z'){AutoCF.undo();}
});

const popup=new maplibregl.Popup({closeButton:false,closeOnClick:false});
for(const layer of ['usgs-circle','usgs-selected']){
 map.on('mouseenter',layer,e=>{
  map.getCanvas().style.cursor='pointer';
  const p=e.features[0].properties;
  popup.setLngLat(e.lngLat).setHTML('<b>'+p.name+'</b><br>USGS '+p.id).addTo(map);
 });
 map.on('mouseleave',layer,()=>{
  map.getCanvas().style.cursor=(AutoCF.mode==='none')?'':'crosshair';popup.remove();});
}
for(const layer of ['stations-circle','stations-selected']){
 map.on('mouseenter',layer,e=>{
  map.getCanvas().style.cursor='pointer';
  const p=e.features[0].properties;
  popup.setLngLat(e.lngLat).setHTML('<b>'+p.name+'</b><br>'+(p.provider_label||'Water-level')+' '+p.id+(p.state?' &middot; '+p.state:'')).addTo(map);
 });
 map.on('mouseleave',layer,()=>{
  map.getCanvas().style.cursor=(AutoCF.mode==='none')?'':'crosshair';popup.remove();});
}
for(const layer of ['obsnoaa-circle','obsnoaa-selected']){
 map.on('mouseenter',layer,e=>{
  map.getCanvas().style.cursor='pointer';const p=e.features[0].properties;
  popup.setLngLat(e.lngLat).setHTML('<b>'+p.name+'</b><br>'+(p.provider_label||'Water-level')+' observation '+p.id).addTo(map);
 });
 map.on('mouseleave',layer,()=>{
  map.getCanvas().style.cursor=(AutoCF.mode==='none')?'':'crosshair';popup.remove();});
}
for(const layer of ['hwm-circle','hwm-selected']){
 map.on('mouseenter',layer,e=>{
  map.getCanvas().style.cursor='pointer';const p=e.features[0].properties;
  popup.setLngLat(e.lngLat).setHTML('<b>USGS HWM '+p.id+'</b><br>'+p.event+' &middot; '+p.elev_ft+' ft '+p.datum).addTo(map);
 });
 map.on('mouseleave',layer,()=>{
  map.getCanvas().style.cursor=(AutoCF.mode==='none')?'':'crosshair';popup.remove();});
}
map.on('mouseenter','huc-fill',e=>{
 if(AutoCF.mode!=='huc')return;
 map.getCanvas().style.cursor='pointer';
 const p=e.features[0].properties;
 popup.setLngLat(e.lngLat).setHTML('<b>'+p.name+'</b><br>'+p.level+' '+p.code).addTo(map);
});
map.on('mouseleave','huc-fill',()=>{
 map.getCanvas().style.cursor=(AutoCF.mode==='none')?'':'crosshair';popup.remove();});
</script></body></html>"""


class StudyAreaMap(QWidget):
    """Interactive study-area map.

    Keeps WebMap's set_layers/set_theme interface so existing callers are
    unaffected, and adds drawing, watershed streaming, station picking, and
    switchable basemaps on top.
    """
    state_changed = Signal(dict)
    unavailable = Signal(dict)

    # AutoCF only exists once MapLibre has been fetched and its style has
    # loaded, which is well after loadFinished. Readiness is therefore probed
    # from the page itself rather than inferred from the load signal.
    READY_PROBE = "(typeof AutoCF==='undefined'||!AutoCF.ready)?'':AutoCF.summary()"
    READY_TIMEOUT_TICKS = 75

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.domain: dict[str, Any] | None = None
        self.track: list[dict[str, Any]] = []
        self.theme = "Dark"
        self.basemap = "OpenStreetMap"
        self.loaded = False
        self.ready_ticks = 0
        self.gave_up = False
        self.queue: list[str] = []
        self.last_summary: dict[str, Any] = {}
        if QWebEngineView is None:
            self.web = None
            label = QLabel("Interactive map component unavailable. File-based study areas still work.")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("mapFallback")
            layout.addWidget(label)
            return
        self.web = QWebEngineView()
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.web.loadFinished.connect(self._page_loaded)
        layout.addWidget(self.web)
        self.poll = QTimer(self)
        self.poll.setInterval(320)
        self.poll.timeout.connect(self._poll_state)
        self.load_page()
        self.poll.start()

    # -- page lifecycle ----------------------------------------------------
    def background(self) -> str:
        return "#f4f6f8" if self.theme == "Light" else "#17212a"

    def load_page(self) -> None:
        if self.web is None:
            return
        self.loaded, self.ready_ticks, self.gave_up = False, 0, False
        page = (STUDY_MAP_PAGE
                .replace("__BACKGROUND__", self.background())
                .replace("__BASEMAP__", json.dumps(BASEMAPS.get(self.basemap, BASEMAPS["OpenStreetMap"]))))
        self.web.setHtml(page, QUrl("https://localhost/"))

    def _page_loaded(self, _ok: bool) -> None:
        # The document is up, but MapLibre and the style still have to load.
        self.loaded, self.ready_ticks = False, 0

    def run(self, script: str) -> None:
        if self.web is None:
            return
        if self.loaded:
            self.web.page().runJavaScript(script)
        else:
            self.queue.append(script)

    def fetch(self, expression: str, callback: Any) -> None:
        if self.web is None or not self.loaded:
            callback(None)
            return
        self.web.page().runJavaScript(expression, callback)

    def _poll_state(self) -> None:
        if self.web is None:
            return
        self.web.page().runJavaScript(self.READY_PROBE, self._summary_ready)

    def _diagnose(self, payload: Any) -> None:
        try:
            self.unavailable.emit(json.loads(payload) if payload else {})
        except Exception:
            self.unavailable.emit({})

    def _summary_ready(self, payload: Any) -> None:
        if not payload:
            if not self.loaded and not self.gave_up:
                self.ready_ticks += 1
                if self.ready_ticks >= self.READY_TIMEOUT_TICKS:
                    self.gave_up = True
                    self.web.page().runJavaScript("acfDiag()", self._diagnose)
            return
        if not self.loaded:
            self.loaded, self.gave_up = True, False
            for script in self.queue:
                self.web.page().runJavaScript(script)
            self.queue.clear()
            self.push_layers()
        try:
            summary = json.loads(payload)
        except Exception:
            return
        if summary != self.last_summary:
            self.last_summary = summary
            self.state_changed.emit(summary)

    # -- WebMap-compatible interface ---------------------------------------
    def set_layers(self, domain: dict[str, Any] | None, track: list[dict[str, Any]] | None = None) -> None:
        self.domain, self.track = domain, list(track or [])
        self.push_layers()

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        self.run(f"AutoCF.setBackground({json.dumps(self.background())});")

    # -- additional controls -----------------------------------------------
    def set_basemap(self, name: str) -> None:
        self.basemap = name if name in BASEMAPS else "OpenStreetMap"
        self.run(f"AutoCF.setBasemap({json.dumps(BASEMAPS[self.basemap])});")

    def push_layers(self) -> None:
        domain = {"type": "FeatureCollection", "features": feature_list(self.domain)}
        track = {"type": "FeatureCollection", "features": []}
        if self.track:
            track["features"] = [{"type": "Feature", "properties": {}, "geometry": {
                "type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in self.track]}}]
        self.run(f"AutoCF.setLayer('domain',{json.dumps(domain)});AutoCF.setLayer('track',{json.dumps(track)});")

    def set_mode(self, mode: str) -> None:
        self.run(f"AutoCF.setMode({json.dumps(mode)});")

    def set_override_properties(self, properties: dict[str, Any]) -> None:
        self.run(f"AutoCF.setOverrideProperties({json.dumps(properties)});")

    def set_draft(self, document: dict[str, Any]) -> None:
        self.run(f"AutoCF.setDraft({json.dumps(document)});")

    def add_features(self, features: list[dict[str, Any]]) -> None:
        self.run(f"AutoCF.addFeatures({json.dumps({'type': 'FeatureCollection', 'features': features})});")

    def remove_feature(self, role: str, identifier: str) -> None:
        self.run(f"AutoCF.removeById({json.dumps(role)},{json.dumps(identifier)});")

    def focus_feature(self, role: str, identifier: str) -> None:
        self.run(f"AutoCF.focusPoint({json.dumps(role)},{json.dumps(identifier)});")

    def set_role_features(self, role: str, features: list[dict[str, Any]]) -> None:
        payload = {"type": "FeatureCollection", "features": features}
        self.run(f"AutoCF.setRole({json.dumps(role)},{json.dumps(payload)});")

    def sync_managed_features(self, role: str, features: list[dict[str, Any]], group: str) -> None:
        payload = {"type": "FeatureCollection", "features": features}
        self.run(
            f"AutoCF.syncManaged({json.dumps(role)},{json.dumps(payload)},{json.dumps(group)});")

    def replace_domain(self, feature: dict[str, Any], reset_coast: bool = True) -> None:
        self.run(f"AutoCF.replaceDomain({json.dumps(feature)},{json.dumps(bool(reset_coast))});")

    def fetch_draft(self, callback: Any) -> None:
        self.fetch("AutoCF.draftGeoJSON()", callback)

    def fetch_view_bounds(self, callback: Any) -> None:
        self.fetch("AutoCF.viewBounds()", callback)

    def fetch_huc_geometry(self, callback: Any) -> None:
        self.fetch("AutoCF.hucGeometry()", callback)

    def finish_shape(self) -> None:
        self.run("AutoCF.finish();")

    def undo(self) -> None:
        self.run("AutoCF.undo();")

    def clear_role(self, role: str) -> None:
        self.run(f"AutoCF.clearRole({json.dumps(role)});")

    def clear_draft(self) -> None:
        self.run("AutoCF.clearAll();")

    def fit_draft(self) -> None:
        self.run("AutoCF.fitDraft();")

    def zoom_to_place(self, place: dict[str, Any]) -> None:
        self.run(f"AutoCF.zoomToPlace({json.dumps(place)});")

    def set_huc_features(self, features: list[dict[str, Any]], revision: int = 0) -> None:
        self.run(
            f"AutoCF.setHuc({json.dumps({'type': 'FeatureCollection', 'features': features})},"
            f"{int(revision)});")

    def select_huc(self, code: str | None, revision: int = 0) -> None:
        self.run(f"AutoCF.selectHuc({json.dumps(code)},{int(revision)});")

    def select_hucs(self, codes: list[str], revision: int = 0) -> None:
        self.run(f"AutoCF.selectHucs({json.dumps(codes)},{int(revision)});")

    def set_stations(self, features: list[dict[str, Any]]) -> None:
        self.run(f"AutoCF.setStations({json.dumps({'type': 'FeatureCollection', 'features': features})});")

    def set_selected_stations(self, ids: list[str]) -> None:
        self.run(f"AutoCF.setSelectedStations({json.dumps(ids)});")

    def set_observation_noaa(self, features: list[dict[str, Any]]) -> None:
        self.run(f"AutoCF.setObsNoaa({json.dumps({'type': 'FeatureCollection', 'features': features})});")

    def set_selected_observation_noaa(self, ids: list[str]) -> None:
        self.run(f"AutoCF.setSelectedObsNoaa({json.dumps(ids)});")

    def set_high_water_marks(self, features: list[dict[str, Any]]) -> None:
        self.run(f"AutoCF.setHwm({json.dumps({'type': 'FeatureCollection', 'features': features})});")

    def set_selected_high_water_marks(self, ids: list[str]) -> None:
        self.run(f"AutoCF.setSelectedHwm({json.dumps(ids)});")

    def set_dem(self, on: bool) -> None:
        self.run(f"AutoCF.setDem({json.dumps(bool(on))});")

    def set_terrain_3d(self, on: bool, exaggeration: float) -> None:
        self.run(f"AutoCF.setTerrain3d({json.dumps(bool(on))},{float(exaggeration)});")

    def set_dem_opacity(self, value: float) -> None:
        self.run(f"AutoCF.setDemOpacity({float(value)});")

    def set_dem_image(self, payload: dict[str, Any] | None) -> None:
        self.run(f"AutoCF.setDemImage({json.dumps(payload)});")

    def show_dem_image(self, on: bool) -> None:
        self.run(f"AutoCF.showDemImage({json.dumps(bool(on))});")

    def set_dem_image_opacity(self, value: float) -> None:
        self.run(f"AutoCF.setDemImageOpacity({float(value)});")

    def fit_dem_image(self) -> None:
        self.run("AutoCF.fitDemImage();")

    def resize_map(self) -> None:
        self.run("map.resize();")

    def set_dem_extent(self, bounds: tuple[float, float, float, float] | None) -> None:
        features = []
        if bounds:
            west, south, east, north = bounds
            features.append({"type": "Feature", "properties": {}, "geometry": {
                "type": "LineString",
                "coordinates": [[west, south], [east, south], [east, north], [west, north], [west, south]]}})
        self.run(f"AutoCF.setDemExtent({json.dumps({'type': 'FeatureCollection', 'features': features})});")

    def fetch_elevation(self, lon: float, lat: float, callback: Any) -> None:
        self.fetch(f"AutoCF.elevationAt({float(lon)},{float(lat)})", callback)

    def set_usgs_sites(self, features: list[dict[str, Any]]) -> None:
        self.run(f"AutoCF.setUsgs({json.dumps({'type': 'FeatureCollection', 'features': features})});")

    def set_selected_usgs(self, ids: list[str]) -> None:
        self.run(f"AutoCF.setSelectedUsgs({json.dumps(ids)});")


class AutoCFWindow(QMainWindow):
    NAVIGATION = [
        ("⚙  AutoCF Settings", "settings"), ("▣  Project", "project"),
        ("◷  Event & Time", "event"), ("⬡  Study Area", "study"),
        ("▰  Terrain", "terrain"), ("≋  Roughness & Infiltration", "surface"),
        ("☂  Forcing", "forcing"), ("▦  SFINCS Model", "sfincs"),
        ("▶  Build & Run", "run"), ("✓  Evaluation", "evaluation"),
        ("⌖  Attribution & Analysis", "impact"), ("▤  Results", "results"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.root = package_root()
        self.logo_path = self.root / "logo" / "AutoCF1.png"
        self.riva_icon_path = self.root / "logo" / "Riva.png"
        if self.logo_path.is_file():
            self.setWindowIcon(QIcon(str(self.logo_path)))
        self.settings = QSettings("AutoCF", "AutoCF Desktop")
        self.runtime = PlatformRuntime(
            self.root,
            backend=str(self.settings.value("runtime/backend", "wsl")),
            compute=str(self.settings.value("runtime/compute", "auto")),
        )
        self.config_path: Path | None = None
        self.raw_config: Any = {}
        self.config: dict[str, Any] = {}
        self.session_mode = "none"
        self.loading = self.dirty = False
        self.process: QProcess | None = None
        self.command_queue: list[list[str]] = []
        self.process_failed = False
        self.job_kind = ""
        self.job_started_at = 0.0
        self.process_output_tail = ""
        self.sfincs_log_offsets: dict[str, int] = {}
        self.sfincs_log_history_mode = False
        self.restart_required = False
        self.ibtracs_worker: IBTrACSWorker | None = None
        self.ibtracs: list[dict[str, Any]] = []
        self.current_storm: dict[str, Any] | None = None
        self.location_worker: PlaceSearchWorker | None = None
        self.last_location_request = 0.0
        self.location_search_pending = False
        self.domain_geojson: dict[str, Any] | None = None
        self.domain_bounds: tuple[float, float, float, float] | None = None
        self.draft_domain_bounds: tuple[float, float, float, float] | None = None
        self.draft_domain_geometries: list[dict[str, Any]] = []
        self.draft_domain_known = False
        self.advanced_widgets: dict[str, QWidget] = {}
        self.huc_worker: HucWorker | None = None
        self.huc_search_generation = 0
        self.pending_huc_search_after_worker = False
        self.searched_location_bounds: list[float] | None = None
        self.huc_records: list[dict[str, Any]] = []
        self.current_huc: str | None = None
        self.selected_hucs: list[str] = []
        # Python list selection and WebEngine map summaries are asynchronous.
        # Version the selection so an older poll cannot erase a newer click.
        self.huc_selection_revision = 0
        self.coops_worker: CoopsWorker | None = None
        self.global_water_level_worker: GlobalWaterLevelWorker | None = None
        self.water_level_coverage_worker: WaterLevelCoverageWorker | None = None
        self.sfincs_worker: SfincsReleaseWorker | None = None
        self.sfincs_latest: dict[str, Any] | None = None
        self.sfincs_inspection: dict[str, Any] | None = None
        self.sfincs_download_after_check = False
        self.coops_stations: list[dict[str, Any]] = []
        self.coops_source = "cache"
        self.active_water_level_provider = "noaa_coops"
        self.water_level_catalogs: dict[str, list[dict[str, Any]]] = {}
        self.boundary_station_selections: dict[str, list[str]] = {}
        self.observation_station_selections: dict[str, list[str]] = {}
        self.selected_station_ids: list[str] = []
        self.loading_stations = False
        self.selected_obs_noaa_ids: list[str] = []
        self.loading_obs_noaa = False
        self.hwm_worker: UsgsHwmWorker | None = None
        self.hwm_events: list[dict[str, Any]] = []
        self.hwm_marks: list[dict[str, Any]] = []
        self.hwm_aoi_geometries: list[dict[str, Any]] = []
        self.selected_hwm_ids: list[str] = []
        self.loading_hwm = False
        self.usgs_worker: UsgsSiteWorker | None = None
        self.usgs_sites: list[dict[str, Any]] = []
        self.selected_usgs_ids: list[str] = []
        self.loading_usgs = False
        self.map_hosts: dict[str, QWidget] = {}
        self.info_buttons: dict[QWidget, QToolButton] = {}
        self.map_panel: QWidget | None = None
        self.draft_counts: dict[str, int] = {}
        self.coordinate_editors: dict[str, dict[str, Any]] = {}
        self.coordinate_table_revision = -1
        self._forcing_signature = None
        self._forcing_save_pending = False
        self._forcing_save_error = ""
        self.setWindowTitle(f"AutoCF Desktop {APP_VERSION}")
        self.resize(1540, 980)
        self.setMinimumSize(1120, 720)
        self._build_ui()
        self._restore_settings()
        self._apply_style(self.theme_choice.currentText())
        # Project/model state is intentionally never restored at startup. Saved
        # application preferences remain, but every launch begins as a clean
        # session so an old YAML, output folder, or cache cannot silently become
        # the active model.
        self.config_display.setPlaceholderText(
            "Fresh session — import an existing YAML or start a new model")
        self.validity.setText("Fresh session")
        self.refresh_analysis_status()

    def _build_ui(self) -> None:
        toolbar = QToolBar("Project")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        brand_logo = QLabel()
        brand_logo.setObjectName("brandLogo")
        brand_logo.setFixedSize(46, 40)
        brand_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self.logo_path.is_file():
            brand_pixmap = QPixmap(str(self.logo_path))
            brand_logo.setPixmap(brand_pixmap.scaled(
                42, 36, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        brand = QLabel("AutoCF  ")
        brand.setObjectName("brand")
        toolbar.addWidget(brand_logo)
        toolbar.addWidget(brand)
        toolbar.addSeparator()
        self.config_display = QLineEdit()
        self.config_display.setReadOnly(True)
        self.config_display.setMinimumWidth(460)
        toolbar.addWidget(self.config_display)
        open_action = QAction("Open YAML…", self)
        open_action.triggered.connect(self.choose_config)
        toolbar.addAction(open_action)
        self.save_action = QAction("Save", self)
        self.save_action.triggered.connect(self.save_config)
        toolbar.addAction(self.save_action)
        save_as_action = QAction("Save As…", self)
        save_as_action.triggered.connect(self.save_config_as)
        toolbar.addAction(save_as_action)
        toolbar.addSeparator()
        self.validity = QLabel("Not loaded")
        toolbar.addWidget(self.validity)

        self.project_validation = QTextEdit()
        self.project_validation.setReadOnly(True)
        self.project_validation.setFont(QFont("Cascadia Mono", 9))
        self.project_validation.setPlaceholderText(
            "Configuration validation results will appear here.")

        splitter = QSplitter()
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(235)
        self.pages = QStackedWidget()
        self.page_keys: list[str] = []
        for label, key in self.NAVIGATION:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.navigation.addItem(item)
            self.pages.addWidget(getattr(self, f"_make_{key}_page")())
            self.page_keys.append(key)
        self.navigation.currentRowChanged.connect(self._page_changed)
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.pages)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.log_dock = QDockWidget("Jobs and Logs", self)
        self.log_dock.setObjectName("LogDock")
        self.log_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.log_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        log_widget = QWidget()
        log_widget.setMinimumHeight(120)
        log_layout = QVBoxLayout(log_widget)
        controls = QHBoxLayout()
        self.job_status = QLabel("Ready — SFINCS execution is CPU (OpenMP).")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_process)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.output_clear)
        self.log_size_button = QPushButton("Expand")
        self.log_size_button.clicked.connect(self.toggle_log_size)
        controls.addWidget(self.job_status)
        controls.addStretch(1)
        controls.addWidget(self.log_size_button)
        controls.addWidget(clear)
        controls.addWidget(self.cancel_button)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Cascadia Mono", 9))
        self.output.setMinimumHeight(70)
        self.job_log_tabs = QTabWidget()
        self.job_log_tabs.addTab(self.output, "AutoCF job")
        sfincs_log_panel = QWidget()
        sfincs_log_layout = QVBoxLayout(sfincs_log_panel)
        sfincs_log_layout.setContentsMargins(4, 4, 4, 4)
        sfincs_log_header = QHBoxLayout()
        self.sfincs_log_status = QLabel("No SFINCS log loaded.")
        load_sfincs_log = QPushButton("Load current logs")
        load_sfincs_log.clicked.connect(self.load_existing_sfincs_logs)
        sfincs_log_header.addWidget(self.sfincs_log_status, 1)
        sfincs_log_header.addWidget(load_sfincs_log)
        self.sfincs_log_output = QTextEdit()
        self.sfincs_log_output.setReadOnly(True)
        self.sfincs_log_output.setFont(QFont("Cascadia Mono", 9))
        sfincs_log_layout.addLayout(sfincs_log_header)
        sfincs_log_layout.addWidget(self.sfincs_log_output, 1)
        self.job_log_tabs.addTab(sfincs_log_panel, "SFINCS live")
        self.configuration_validation_tab = QWidget()
        validation_layout = QVBoxLayout(self.configuration_validation_tab)
        validation_layout.setContentsMargins(4, 4, 4, 4)
        validation_header = QHBoxLayout()
        validation_description = self.info_button(
            "Check the current model configuration at any stage of the workflow.",
            "Configuration validation")
        self.validation_button = QPushButton("Validate configuration")
        self.validation_button.setObjectName("primary")
        self.validation_button.clicked.connect(self.validate_config)
        validation_header.addWidget(validation_description, 1)
        validation_header.addWidget(self.validation_button)
        validation_layout.addLayout(validation_header)
        validation_layout.addWidget(self.project_validation, 1)
        self.job_log_tabs.addTab(
            self.configuration_validation_tab, "Configuration validation")
        self.process_response_panel = QWidget()
        input_row = QHBoxLayout(self.process_response_panel)
        input_row.setContentsMargins(0, 0, 0, 0)
        self.process_action_label = QLabel("Action required: reply to the running job")
        self.process_input = QLineEdit()
        self.process_input.setPlaceholderText("Type the requested response, then press Enter")
        self.process_input.setEnabled(False)
        self.process_input.returnPressed.connect(self.send_process_input)
        self.send_input_button = QPushButton("Send")
        self.send_input_button.setEnabled(False)
        self.send_input_button.clicked.connect(self.send_process_input)
        input_row.addWidget(self.process_action_label)
        input_row.addWidget(self.process_input, 1)
        input_row.addWidget(self.send_input_button)
        self.process_response_panel.setVisible(False)
        log_layout.addLayout(controls)
        log_layout.addWidget(self.job_log_tabs, 1)
        log_layout.addWidget(self.process_response_panel)
        self.log_dock.setWidget(log_widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.riva_dock = RivaAssistantDock(
            self, self.root, self.run_dir, self.riva_context,
            lambda: self.process is not None,
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.riva_dock)
        self.riva_dock.hide()

        self.riva_pet = RivaPet(self.riva_icon_path, self)
        self.riva_pet.activated.connect(self._toggle_riva)
        self.riva_dock.visibilityChanged.connect(self.riva_pet.set_assistant_visible)
        self.riva_dock.activityChanged.connect(self.riva_pet.set_activity)
        self.riva_anchor_dock = QDockWidget("Riva", self)
        self.riva_anchor_dock.setObjectName("rivaAnchorDock")
        self.riva_anchor_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.riva_anchor_dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.riva_anchor_dock.setMinimumWidth(216)
        self.riva_anchor_dock.setMaximumWidth(236)
        self.riva_anchor_dock.setTitleBarWidget(QWidget(self.riva_anchor_dock))
        self.riva_anchor = QWidget()
        self.riva_anchor.setObjectName("rivaAnchor")
        riva_anchor_layout = QVBoxLayout(self.riva_anchor)
        riva_anchor_layout.setContentsMargins(6, 4, 6, 4)
        riva_anchor_layout.addWidget(
            self.riva_pet, 0,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        )
        self.riva_anchor_dock.setWidget(self.riva_anchor)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.riva_anchor_dock)
        self.splitDockWidget(
            self.log_dock, self.riva_anchor_dock, Qt.Orientation.Horizontal)
        QTimer.singleShot(0, self.riva_pet.start_animation)
        self.sfincs_log_timer = QTimer(self)
        self.sfincs_log_timer.setInterval(450)
        self.sfincs_log_timer.timeout.connect(self.poll_sfincs_logs)
        self.resizeDocks([self.log_dock], [260], Qt.Orientation.Vertical)
        self.resizeDocks(
            [self.log_dock, self.riva_anchor_dock], [1000, 226],
            Qt.Orientation.Horizontal,
        )
        self.navigation.setCurrentRow(1)

    def _toggle_riva(self) -> None:
        visible = self.riva_dock.isVisible()
        self.riva_dock.setVisible(not visible)
        if not visible:
            self.riva_dock.raise_()

    def _riva_panic(self) -> None:
        """Use one visible failure reaction for every AutoCF stage."""
        if hasattr(self, "riva_pet"):
            self.riva_pet.panic()

    def output_clear(self) -> None:
        self.output.clear()
        self.sfincs_log_output.clear()

    def riva_context(self) -> dict[str, Any]:
        row = self.navigation.currentRow()
        page = self.page_keys[row] if 0 <= row < len(self.page_keys) else "project"
        return {
            "page": page,
            "project": self.project_name.text() if hasattr(self, "project_name") else "",
            "yaml": str(self.config_path) if self.config_path else "",
            "run": str(self.run_dir()),
            "study_bounds": list(self.domain_bounds) if self.domain_bounds else None,
        }

    def run_layout_version(self) -> int:
        source = self.config or self.raw_config or {}
        try:
            return int(deep_get(source, "project.layout_version", 1))
        except (TypeError, ValueError):
            return 1

    def uses_organized_run_layout(self) -> bool:
        return self.run_layout_version() >= 2

    def model_dir(self) -> Path:
        run = self.run_dir()
        return run / "data" / "model" / "main" if self.uses_organized_run_layout() else run

    def data_dir(self, section: str, kind: str | None = None) -> Path:
        run = self.run_dir()
        if not self.uses_organized_run_layout():
            if section == "geospatial":
                return run / "agent_gis"
            if section == "validation":
                return run / "agent_validation" / "water_level"
            if section == "attribution":
                return run / "agent_analysis"
            return run / "agent_data"
        path = run / "data" / section
        return path / kind if kind else path

    def results_dir(self, section: str, kind: str | None = None) -> Path:
        run = self.run_dir()
        if not self.uses_organized_run_layout():
            legacy = {
                "simulation": run / "agent_reports",
                "evaluation": run / "agent_plots",
                "attribution": run / "agent_analysis" / "results",
                "exposure": run / "agent_impact",
            }
            return legacy[section]
        path = run / "results" / section
        return path / kind if kind else path

    def sfincs_log_candidates(self) -> list[Path]:
        model = self.model_dir()
        run = self.run_dir()
        candidates = [model / "sfincs.log", model / "hydromt.log"]
        candidates.append(
            run / "logs" / "simulation" / "run_cpu.log"
            if self.uses_organized_run_layout() else model / "run_cpu.log")
        return candidates

    def append_sfincs_log_text(self, text: str) -> None:
        cursor = self.sfincs_log_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.sfincs_log_output.setTextCursor(cursor)
        self.sfincs_log_output.ensureCursorVisible()

    def load_existing_sfincs_logs(self) -> None:
        self.sfincs_log_output.clear()
        self.sfincs_log_offsets = {}
        self.sfincs_log_history_mode = True
        self.job_started_at = 0.0
        self.poll_sfincs_logs()
        if not any(path.is_file() for path in self.sfincs_log_candidates()):
            self.sfincs_log_status.setText(f"No SFINCS/model logs found in {self.run_dir()}")

    def poll_sfincs_logs(self) -> None:
        loaded = 0
        for path in self.sfincs_log_candidates():
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                if self.job_started_at and stat.st_mtime < self.job_started_at - 2.0:
                    continue
                key = str(path.resolve())
                offset = self.sfincs_log_offsets.get(key)
                if offset is None:
                    offset = max(0, stat.st_size - 1_000_000) if self.sfincs_log_history_mode else 0
                    prefix = f"\n===== {path.name} =====\n"
                    if offset:
                        prefix += "[Showing the most recent 1 MB]\n"
                    self.append_sfincs_log_text(prefix)
                if stat.st_size < offset:
                    offset = 0
                    self.append_sfincs_log_text(f"\n===== {path.name} restarted =====\n")
                if stat.st_size > offset:
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        data = stream.read()
                    offset += len(data)
                    text = data.decode("utf-8", errors="replace").replace("\x00", "")
                    if text:
                        self.append_sfincs_log_text(text)
                        loaded += len(data)
                self.sfincs_log_offsets[key] = offset
            except (OSError, PermissionError):
                continue
        if self.sfincs_log_offsets:
            names = ", ".join(Path(path).name for path in self.sfincs_log_offsets)
            self.sfincs_log_status.setText(f"Following {names}" if self.sfincs_log_timer.isActive() else f"Loaded {names}")
            if loaded:
                self.job_log_tabs.setTabText(1, "SFINCS live ●")
        elif self.sfincs_log_timer.isActive():
            self.sfincs_log_status.setText("Waiting for SFINCS/model log files…")

    def toggle_log_size(self) -> None:
        expanded = self.log_dock.height() >= int(self.height() * 0.45)
        target = 220 if expanded else max(380, int(self.height() * 0.58))
        self.resizeDocks([self.log_dock], [target], Qt.Orientation.Vertical)
        self.log_size_button.setText("Expand" if expanded else "Collapse")

    def page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        # Navigation already names the active section. Repeating a large title
        # and description on every page wastes vertical working space.
        _ = title, subtitle
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(24, 10, 24, 20)
        return outer, layout

    def scroll(self, content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        return area

    def path_row(self, line: QLineEdit, callback: Any, label: str = "Browse…") -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(line, 1)
        layout.addWidget(button)
        return widget

    def info_button(self, text: str, title: str = "Information") -> QToolButton:
        """Compact help control: hover for a preview, click for the full text."""
        button = QToolButton()
        button.setText("i")
        button.setFixedSize(18, 18)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(title)
        button.setProperty("info_title", title)
        button.setProperty("info_text", text)
        button.setStyleSheet(
            "QToolButton { background-color:#2477c9; color:white; border:1px solid #69aeea; "
            "border-radius:9px; font:700 11px 'Segoe UI'; padding:0px; }"
            "QToolButton:hover { background-color:#3d91df; border-color:#9bd0ff; }"
            "QToolButton:pressed { background-color:#155a9a; }")
        self.refresh_info_tooltip(button)
        button.clicked.connect(lambda _checked=False, item=button: QMessageBox.information(
            self, str(item.property("info_title")), str(item.property("info_text"))))
        return button

    def refresh_info_tooltip(self, button: QToolButton) -> None:
        tooltip = html.escape(str(button.property("info_text") or "")).replace("\n", "<br>")
        button.setToolTip(f"<div style='max-width:520px'>{tooltip}</div>")

    def set_info_text(self, button: QToolButton, text: str) -> None:
        button.setProperty("info_text", text)
        self.refresh_info_tooltip(button)

    def add_info(self, layout: Any, text: str, title: str = "Information") -> QToolButton:
        """Attach one unobtrusive information icon to the owning section."""
        anchor = layout.parentWidget()
        if anchor is None:
            button = self.info_button(text, title)
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignRight)
            return button
        button = self.info_buttons.get(anchor)
        if button is None:
            button = self.info_button(text, title)
            button.setParent(anchor)
            self.info_buttons[anchor] = button
            anchor.installEventFilter(self)
        else:
            existing = str(button.property("info_text") or "")
            button.setProperty("info_text", existing + "\n\n" + text)
            self.refresh_info_tooltip(button)
        self.position_info_button(anchor)
        return button

    def position_info_button(self, anchor: QWidget) -> None:
        button = self.info_buttons.get(anchor)
        if button is None:
            return
        if isinstance(anchor, QGroupBox) and anchor.title():
            x = 11 + anchor.fontMetrics().horizontalAdvance(anchor.title()) + 8
            y = 0
        else:
            x = max(4, anchor.width() - button.width() - 6)
            y = 4
        button.move(x, y)
        button.raise_()

    def eventFilter(self, watched: Any, event: QEvent) -> bool:
        if watched in self.info_buttons and event.type() in {
                QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.FontChange}:
            self.position_info_button(watched)
        return super().eventFilter(watched, event)

    def _make_settings_page(self) -> QWidget:
        page, layout = self.page("AutoCF Settings", "Application, runtime, storage, and CPU preferences. These settings are separate from the scientific YAML.")
        group = QGroupBox("Execution")
        form = QFormLayout(group)
        self.cpu_threads = QSpinBox()
        self.cpu_threads.setRange(0, max(1, os.cpu_count() or 64))
        self.cpu_threads.setSpecialValueText("Automatic")
        self.theme_choice = QComboBox()
        self.theme_choice.addItems(["Dark", "Light", "System"])
        self.default_project_dir = QLineEdit()
        self.default_project_dir.setPlaceholderText("Parent folder for new AutoCF model workspaces")
        self.map_basemap = QCheckBox("Use OpenStreetMap basemap when internet is available")
        self.map_basemap.setChecked(True)
        form.addRow("CPU threads", self.cpu_threads)
        form.addRow("Theme", self.theme_choice)
        form.addRow("Working directory for new models", self.path_row(
            self.default_project_dir, self.choose_default_project_dir))
        form.addRow(self.map_basemap)
        layout.addWidget(group)
        runtime = QGroupBox("Managed runtime")
        runtime_layout = QVBoxLayout(runtime)
        runtime_form = QFormLayout()
        self.runtime_choice = QComboBox()
        if self.runtime.platform_name == "Windows":
            self.runtime_choice.addItem("Managed WSL2", "wsl")
            self.runtime_choice.addItem("Docker Desktop", "docker")
        else:
            self.runtime_choice.addItem("Docker / Podman", "container")
            self.runtime_choice.setEnabled(False)
        runtime_form.addRow("Execution backend", self.runtime_choice)
        self.compute_choice = QComboBox()
        self.compute_choice.addItem("Automatic — validated GPU when available", "auto")
        self.compute_choice.addItem("CPU — OpenMP", "cpu")
        if self.runtime.platform_name == "Windows":
            self.compute_choice.addItem(
                "NVIDIA GPU — validated SFINCS payload required", "gpu",
            )
        runtime_form.addRow("SFINCS compute", self.compute_choice)
        runtime_layout.addLayout(runtime_form)
        self.runtime_info = QLabel()
        self.runtime_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.runtime_info.setWordWrap(True)
        runtime_note = (
            "Windows can use either backend. Docker Desktop gives Windows the same container "
            "execution path as Linux and macOS; Managed WSL2 remains available for existing installations."
            if self.runtime.platform_name == "Windows" else
            "AutoCF uses the installed Docker or Podman engine for scientific execution."
        )
        buttons = QHBoxLayout()
        self.status_button = QPushButton("Check system status")
        self.install_button = QPushButton("Install / repair runtime")
        self.restart_button = QPushButton("Restart to continue installation")
        self.restart_button.setVisible(False)
        self.status_button.clicked.connect(self.system_status)
        self.install_button.clicked.connect(self.install_runtime)
        self.restart_button.clicked.connect(self.restart_host)
        buttons.addWidget(self.status_button)
        buttons.addWidget(self.install_button)
        buttons.addWidget(self.restart_button)
        buttons.addStretch(1)
        runtime_layout.addWidget(self.runtime_info)
        self.add_info(runtime_layout, runtime_note, "Managed runtime")
        runtime_layout.addLayout(buttons)
        layout.addWidget(runtime)

        sfincs = QGroupBox("SFINCS software")
        sfincs_layout = QVBoxLayout(sfincs)
        sfincs_intro = (
            "Do you already have SFINCS? Select its installation or model directory, or let AutoCF "
            "download the newest official Deltares source release into this AutoCF package. Version "
            "checks use the Deltares/SFINCS GitHub releases."
        )
        self.add_info(sfincs_layout, sfincs_intro, "SFINCS software")
        sfincs_form = QFormLayout()
        self.sfincs_choice = QComboBox()
        self.sfincs_choice.addItem("Yes — use an existing SFINCS folder", "existing")
        self.sfincs_choice.addItem("No — download SFINCS during setup", "download")
        self.sfincs_directory = QLineEdit()
        self.sfincs_directory.setReadOnly(True)
        self.sfincs_browse_button = QPushButton("Browse…")
        self.sfincs_browse_button.clicked.connect(self.choose_sfincs_directory)
        sfincs_path_row = QWidget()
        sfincs_path_layout = QHBoxLayout(sfincs_path_row)
        sfincs_path_layout.setContentsMargins(0, 0, 0, 0)
        sfincs_path_layout.addWidget(self.sfincs_directory, 1)
        sfincs_path_layout.addWidget(self.sfincs_browse_button)
        sfincs_form.addRow("Do you have SFINCS?", self.sfincs_choice)
        sfincs_form.addRow("SFINCS directory", sfincs_path_row)
        sfincs_layout.addLayout(sfincs_form)
        self.sfincs_status = QLabel("Choose an option to configure SFINCS.")
        self.sfincs_status.setWordWrap(True)
        self.sfincs_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sfincs_layout.addWidget(self.sfincs_status)
        self.sfincs_progress = QProgressBar()
        self.sfincs_progress.setRange(0, 100)
        self.sfincs_progress.setVisible(False)
        sfincs_layout.addWidget(self.sfincs_progress)
        sfincs_buttons = QHBoxLayout()
        self.sfincs_check_button = QPushButton("Check latest version")
        self.sfincs_download_button = QPushButton("Download latest source")
        self.sfincs_official_button = QPushButton("Get runnable SFINCS")
        self.sfincs_check_button.clicked.connect(self.check_sfincs_latest)
        self.sfincs_download_button.clicked.connect(self.download_sfincs_latest)
        self.sfincs_official_button.clicked.connect(self.open_sfincs_download)
        sfincs_buttons.addWidget(self.sfincs_check_button)
        sfincs_buttons.addWidget(self.sfincs_download_button)
        sfincs_buttons.addWidget(self.sfincs_official_button)
        sfincs_buttons.addStretch(1)
        sfincs_layout.addLayout(sfincs_buttons)
        sfincs_note = (
            "GitHub supplies the GPL source code. Deltares distributes the precompiled Windows executable "
            "through its download portal and runnable Linux/macOS packages as an official Docker image. "
            "AutoCF model runs continue to use the validated managed runtime."
        )
        self.add_info(sfincs_layout, sfincs_note, "SFINCS distribution")
        layout.addWidget(sfincs)
        layout.addStretch(1)
        self.cpu_threads.valueChanged.connect(self.save_app_settings)
        self.theme_choice.currentTextChanged.connect(self._apply_style)
        self.theme_choice.currentTextChanged.connect(self.save_app_settings)
        self.map_basemap.toggled.connect(self.save_app_settings)
        self.default_project_dir.editingFinished.connect(self.save_app_settings)
        self.runtime_choice.currentIndexChanged.connect(self.runtime_backend_changed)
        self.compute_choice.currentIndexChanged.connect(self.compute_backend_changed)
        self.sfincs_choice.currentIndexChanged.connect(self.sfincs_choice_changed)
        return self.scroll(page)

    def _make_project_page(self) -> QWidget:
        page, layout = self.page("Project", "Open an AutoCF YAML recipe and choose its work and cache locations.")
        start_group = QGroupBox("Start this session")
        start_layout = QVBoxLayout(start_group)
        self.project_session_status = QLabel(
            "No model is active. Import an existing AutoCF YAML, or start a new model in the working directory from Settings.")
        self.project_session_status.setWordWrap(True)
        start_layout.addWidget(self.project_session_status)
        start_buttons = QHBoxLayout()
        new_project = QPushButton("Start new model")
        new_project.setObjectName("primary")
        new_project.setToolTip("Guided setup: choose the event first, then define the Study Area.")
        new_project.clicked.connect(self.start_new_model)
        import_project = QPushButton("Import existing project…")
        import_project.setToolTip("Open an existing YAML and review only missing or incompatible settings.")
        import_project.clicked.connect(self.choose_config)
        default_folder = QPushButton("Open working directory")
        default_folder.clicked.connect(self.open_default_project_folder)
        start_buttons.addWidget(new_project)
        start_buttons.addWidget(import_project)
        start_buttons.addWidget(default_folder)
        start_buttons.addStretch(1)
        start_layout.addLayout(start_buttons)
        layout.addWidget(start_group)
        group = QGroupBox("Project identity and storage")
        form = QFormLayout(group)
        self.project_name, self.model_workspace, self.output_dir, self.cache_dir = (
            QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit())
        self.model_workspace.setReadOnly(True)
        self.model_workspace.setPlaceholderText("Created after event selection inside the working directory")
        self.overwrite = QCheckBox("Allow AutoCF to replace generated project products")
        form.addRow("Project name", self.project_name)
        form.addRow("Current model workspace", self.path_row(
            self.model_workspace, self.open_model_workspace, "Open"))
        form.addRow("Work directory", self.path_row(self.output_dir, lambda: self.choose_directory(self.output_dir)))
        form.addRow("Download cache", self.path_row(self.cache_dir, lambda: self.choose_directory(self.cache_dir)))
        form.addRow(self.overwrite)
        storage_note = (
            "New-model layout inside the working directory:\n"
            "Event_or_Project\\  →  YAML + study-area GeoJSON + runs\\ + cache\\\n"
            "Imported projects remain beside their selected YAML, and relative paths resolve from that YAML folder.")
        self.add_info(form, storage_note, "Project storage layout")
        layout.addWidget(group)
        actions = QHBoxLayout()
        button = QPushButton("Save project")
        button.setToolTip("Save the one authoritative YAML in this model's canonical workspace.")
        button.clicked.connect(self.save_config)
        actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        for widget in (self.project_name, self.output_dir, self.cache_dir):
            widget.textChanged.connect(self.mark_dirty)
        self.overwrite.toggled.connect(self.mark_dirty)
        return self.scroll(page)

    def _make_study_page(self) -> QWidget:
        page, layout = self.page("Study Area", "Select and inspect the local GeoJSON model domain. AutoCF never uploads it.")
        controls = QGroupBox("Domain and computational grid")
        form = QFormLayout(controls)
        self.geojson_path, self.domain_info = QLineEdit(), QLabel("No domain loaded")
        self.domain_info.setWordWrap(True)
        self.mesh_cell_size = QComboBox()
        for cell_size in STANDARD_MESH_CELL_SIZES_M:
            label = f"{cell_size:g} m"
            if cell_size == 200.0:
                label += " — recommended starting point"
            self.mesh_cell_size.addItem(label, cell_size)
        self.mesh_cell_size.addItem("Custom…", None)
        self.mesh_dx = QDoubleSpinBox()
        self.mesh_dx.setRange(1, 10000)
        self.mesh_dx.setDecimals(1)
        self.mesh_dx.setSuffix(" m")
        self.mesh_dx.setValue(200.0)
        self.mesh_cell_size.setCurrentIndex(self.mesh_cell_size.findData(200.0))
        self.mesh_custom_warning = QLabel(
            "Custom cell sizes can create extremely large terrain rasters and model grids. "
            "Very small values may exhaust memory or make builds and simulations impractical."
        )
        self.mesh_custom_warning.setWordWrap(True)
        self.mesh_custom_warning.setStyleSheet("color:#d97706;font-weight:600")
        self.mesh_crs = QComboBox()
        self.mesh_crs.setEditable(True)
        self.mesh_crs.addItems(["utm", "EPSG:4326"])
        self.topo_buffer = QDoubleSpinBox()
        self.topo_buffer.setRange(0, 1000)
        self.topo_buffer.setValue(10.0)
        self.topo_buffer.setSuffix(" km")
        form.addRow("Domain inspection", self.domain_info)
        form.addRow("Grid cell size", self.mesh_cell_size)
        form.addRow("Custom cell size", self.mesh_dx)
        self.mesh_custom_label = form.labelForField(self.mesh_dx)
        form.addRow(self.mesh_custom_warning)
        form.addRow("Model CRS", self.mesh_crs)
        form.addRow("Topobathy buffer", self.topo_buffer)
        self.mesh_dx.setVisible(False)
        self.mesh_custom_label.setVisible(False)
        self.mesh_custom_warning.setVisible(False)

        upper = QWidget()
        upper_layout = QVBoxLayout(upper)
        upper_layout.setContentsMargins(0, 0, 0, 0)
        upper_layout.addWidget(controls)
        upper_layout.addWidget(self._make_study_builder())
        upper_layout.addStretch(1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.scroll(upper), 1)
        left.setMinimumWidth(360)

        lower = self.map_host("study")
        lower.setMinimumWidth(380)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(lower)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 840])
        layout.addWidget(splitter, 1)

        self.geojson_path.editingFinished.connect(self.load_domain_preview)
        self.mesh_cell_size.currentIndexChanged.connect(self.sync_mesh_cell_size)
        self.mesh_dx.valueChanged.connect(self.mark_dirty)
        self.mesh_crs.currentTextChanged.connect(self.mark_dirty)
        self.topo_buffer.valueChanged.connect(self.mark_dirty)
        return page

    def sync_mesh_cell_size(self, *_: Any) -> None:
        """Apply a standard grid size or expose the guarded custom-size input."""
        cell_size = self.mesh_cell_size.currentData()
        custom = cell_size is None
        self.mesh_dx.setVisible(custom)
        self.mesh_custom_label.setVisible(custom)
        self.mesh_custom_warning.setVisible(custom)
        if not custom:
            self.mesh_dx.blockSignals(True)
            self.mesh_dx.setValue(float(cell_size))
            self.mesh_dx.blockSignals(False)
        elif not self.loading:
            QMessageBox.warning(
                self,
                "Custom grid cell size",
                "Custom cell sizes can produce very large terrain rasters and SFINCS grids. "
                "Small values may exhaust memory or make the model impractical. "
                "Check the study-area size carefully before preparing terrain or building the model.",
            )
        if not self.loading:
            self.mark_dirty()

    def set_mesh_cell_size(self, value: float) -> None:
        """Load a YAML cell size without replacing non-standard values."""
        value = float(value)
        standard_index = next(
            (
                index for index in range(self.mesh_cell_size.count())
                if self.mesh_cell_size.itemData(index) is not None
                and math.isclose(float(self.mesh_cell_size.itemData(index)), value)
            ),
            -1,
        )
        self.mesh_cell_size.blockSignals(True)
        self.mesh_dx.blockSignals(True)
        self.mesh_dx.setValue(value)
        self.mesh_cell_size.setCurrentIndex(
            standard_index if standard_index >= 0 else self.mesh_cell_size.count() - 1
        )
        self.mesh_dx.blockSignals(False)
        self.mesh_cell_size.blockSignals(False)
        self.sync_mesh_cell_size()

    def map_host(self, key: str) -> QWidget:
        """Empty slot that the single shared map panel is moved into.

        Study Area and Forcing edit the same study-area draft, so they share one
        map widget rather than each owning a copy of the state.
        """
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        self.map_hosts[key] = host
        if self.map_panel is None:
            self.map_panel = self._make_map_panel()
        return host

    def _make_map_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        basemap_row = QHBoxLayout()
        self.basemap_choice = QComboBox()
        self.basemap_choice.addItems(list(BASEMAPS))
        self.basemap_choice.setToolTip("Background imagery for the map. Tiles are streamed from the selected provider.")
        self.location_search = QLineEdit()
        self.location_search.setPlaceholderText("Search city or place")
        self.location_search.setClearButtonEnabled(True)
        self.location_search.setMaxLength(200)
        self.location_search.setToolTip(
            "Type a city or place, then press Enter. Search only moves the map; it does not change the AOI.")
        self.location_search_button = QPushButton("Search")
        self.location_search_button.clicked.connect(self.search_location)
        self.location_search.returnPressed.connect(self.search_location)
        self.fit_button = QPushButton("Zoom to draft")
        self.fit_button.clicked.connect(lambda: self.study_map.fit_draft())
        self.export_map_button = QPushButton("Export PNG…")
        self.export_map_button.setToolTip("Save this map at 300 dpi with latitude/longitude around the border; no grid lines.")
        self.export_map_button.clicked.connect(self.export_map_png)
        basemap_row.addWidget(QLabel("Basemap"))
        basemap_row.addWidget(self.basemap_choice, 1)
        basemap_row.addWidget(QLabel("Location"))
        basemap_row.addWidget(self.location_search, 1)
        basemap_row.addWidget(self.location_search_button)
        basemap_row.addWidget(self.fit_button)
        basemap_row.addWidget(self.export_map_button)
        layout.addLayout(basemap_row)
        self.surface_layer_controls = QWidget()
        surface_row = QHBoxLayout(self.surface_layer_controls)
        surface_row.setContentsMargins(0, 0, 0, 0)
        self.surface_layer_choice = QComboBox()
        self.surface_layer_choice.setEnabled(False)
        self.surface_layer_choice.setToolTip(
            "Prepared roughness and infiltration rasters available for the current run.")
        self.surface_layer_choice.currentIndexChanged.connect(self.surface_layer_changed)
        self.surface_layer_reload = QPushButton("Reload prepared layers")
        self.surface_layer_reload.clicked.connect(lambda: self.load_surface_previews(True))
        surface_row.addWidget(QLabel("Prepared layer"))
        surface_row.addWidget(self.surface_layer_choice, 1)
        surface_row.addWidget(self.surface_layer_reload)
        self.surface_layer_controls.setVisible(False)
        layout.addWidget(self.surface_layer_controls)
        self.forcing_layer_controls = QWidget()
        forcing_layout = QVBoxLayout(self.forcing_layer_controls)
        forcing_layout.setContentsMargins(0, 0, 0, 0)
        forcing_row = QHBoxLayout()
        self.forcing_layer_choice = QComboBox(); self.forcing_layer_choice.setEnabled(False)
        self.forcing_active_layer_label = QLabel("Rainfall")
        self.forcing_active_layer_label.setStyleSheet("font-weight:600")
        self.forcing_time_choice = QComboBox(); self.forcing_time_choice.setEnabled(False)
        self.forcing_previous = QPushButton("◀"); self.forcing_next = QPushButton("▶")
        self.forcing_opacity = QDoubleSpinBox(); self.forcing_opacity.setRange(0.05, 1.0)
        self.forcing_opacity.setSingleStep(0.05); self.forcing_opacity.setValue(0.78)
        self.forcing_layer_choice.currentIndexChanged.connect(self.forcing_layer_changed)
        self.forcing_time_choice.currentIndexChanged.connect(self.forcing_frame_changed)
        self.forcing_previous.clicked.connect(lambda: self.step_forcing_frame(-1))
        self.forcing_next.clicked.connect(lambda: self.step_forcing_frame(1))
        self.forcing_opacity.valueChanged.connect(lambda value: self.study_map.set_dem_image_opacity(float(value)))
        forcing_row.addWidget(QLabel("Displayed layer:")); forcing_row.addWidget(self.forcing_active_layer_label)
        forcing_row.addWidget(QLabel("Time")); forcing_row.addWidget(self.forcing_previous)
        forcing_row.addWidget(self.forcing_time_choice, 1); forcing_row.addWidget(self.forcing_next)
        forcing_row.addWidget(QLabel("Opacity")); forcing_row.addWidget(self.forcing_opacity)
        forcing_layout.addLayout(forcing_row)
        self.forcing_map_status = QLabel("Prepared rainfall, wind, and pressure fields will appear automatically.")
        self.forcing_map_status.setWordWrap(True); forcing_layout.addWidget(self.forcing_map_status)
        self.forcing_layer_controls.setVisible(False)
        layout.addWidget(self.forcing_layer_controls)
        self.study_map = StudyAreaMap()
        self.study_map.setMinimumHeight(240)
        self.study_map.state_changed.connect(self.map_state_changed)
        self.study_map.unavailable.connect(self.map_unavailable)
        # Keep every dynamic raster scale in a compact vertical map overlay.
        # The map is the wrapper's only laid-out widget; the compact color-bar
        # label is a floating child, so it can never expand over or hide it.
        self.map_stack = QWidget()
        map_stack_layout = QVBoxLayout(self.map_stack)
        map_stack_layout.setContentsMargins(0, 0, 0, 0)
        map_stack_layout.addWidget(self.study_map)
        self.dem_colorbar = MapColorbarLabel(self.map_stack)
        self.dem_colorbar_available = False
        self.dem_colorbar.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.dem_colorbar.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.dem_colorbar.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.dem_colorbar.setToolTip("Scale for the prepared layer displayed on the map.")
        self.dem_colorbar.setVisible(False)
        layout.addWidget(self.map_stack, 1)
        self.map_legend = QLabel(self.map_stack)
        self.map_legend.setTextFormat(Qt.TextFormat.RichText)
        self.map_legend.setWordWrap(True)
        self.map_legend.setMaximumWidth(220)
        self.map_legend.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.map_legend.setStyleSheet(
            "QLabel { background: rgba(20, 25, 29, 210); color: white; "
            "border: 1px solid rgba(220, 230, 235, 150); border-radius: 3px; padding: 5px; }")
        self.map_legend.setVisible(False)
        self.surface_layers: list[dict[str, Any]] = []
        self.forcing_layers: list[dict[str, Any]] = []
        self.forcing_manifest: dict[str, Any] = {}
        self.active_preview_context: str | None = None
        # Geometry/save controls belong only to Study Area.  The map itself is
        # shared with data tabs, but those tabs must not repeat this workflow.
        self.study_draft_bar = self._make_draft_bar()
        layout.addWidget(self.study_draft_bar)
        self.basemap_choice.currentTextChanged.connect(self.basemap_changed)
        return panel

    def show_map_on(self, key: str) -> None:
        host = self.map_hosts.get(key)
        if host is None or self.map_panel is None:
            return
        if self.map_panel.parent() is not host:
            host.layout().addWidget(self.map_panel)
        self.surface_layer_controls.setVisible(key == "surface")
        self.forcing_layer_controls.setVisible(key == "forcing")
        self.study_draft_bar.setVisible(key == "study")
        if key == "study":
            self.clear_active_map_raster()
        self.map_panel.show()
        # MapLibre does not always detect a Qt WebEngine view being reparented
        # between tabs. Resize after the new host has completed layout.
        QTimer.singleShot(0, self.study_map.resize_map)

    def legend_html(self, summary: dict[str, Any]) -> str:
        swatch = "<span style='color:%s;font-size:15px'>&#9632;</span> %s"
        counts = summary.get("counts", {})
        items: list[tuple[str, str]] = []
        for role, color, label in (
            ("domain", "#0077b6", "Domain"),
            ("ocean_boundary", "#ff9f1c", "Ocean boundary"),
            ("ocean_forcing_point", "#e63946", "Boundary water level"),
            ("observation_point", "#8ac926", "Observation"),
            ("river_source", "#9d4edd", "River source"),
            ("roughness_override", "#00b894", "NBS/adaptation"),
        ):
            if counts.get(role, 0):
                items.append((color, label))
        mode = summary.get("mode", "none")
        candidates = summary.get("candidate_counts", {})
        candidate_legend = {
            "huc": ("#00d1b2", "HUC candidate"),
            "station": ("#5bc0eb", "Water-level boundary candidate"),
            "obs_noaa": ("#ffd166", "Water-level observation candidate"),
            "hwm": ("#f4a261", "USGS HWM candidate"),
            "usgs": ("#b07cf0", "USGS river-gauge candidate"),
        }
        if mode in candidate_legend and candidates.get(mode, 0):
            items.append(candidate_legend[mode])
        return "<br>".join(swatch % item for item in items)

    def update_map_legend(self, summary: dict[str, Any]) -> None:
        content = self.legend_html(summary)
        self.map_legend.setText(content)
        self.map_legend.setVisible(bool(content))
        self.map_legend.adjustSize()
        self.position_map_overlays()

    def position_map_overlays(self) -> None:
        """Stack only visible overlays in a narrow rail along the map's left edge."""
        if not hasattr(self, "map_legend"):
            return
        y = 12
        if self.map_legend.isVisible():
            self.map_legend.adjustSize()
            self.map_legend.move(12, y)
            self.map_legend.raise_()
            y += self.map_legend.height() + 8
        if self.dem_colorbar.isVisible():
            self.dem_colorbar.adjustSize()
            self.dem_colorbar.move(12, y)
            self.dem_colorbar.raise_()

    def export_map_png(self) -> None:
        if not self.study_map.loaded or self.study_map.web is None:
            QMessageBox.warning(self, "Map not ready", "Wait for the map to finish loading before exporting.")
            return
        maximum = min(16.0, (self.study_map.web.width() * 5 + 252) / 300)
        width, ok = QInputDialog.getDouble(
            self, "Export map at 300 dpi", "Image width including coordinate border (inches):",
            min(10.0, maximum), 4.0, maximum, 1)
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export map PNG", str(self.project_base_dir() / "map.png"), "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        from map_export import SNAPSHOT_JS
        self.export_map_button.setEnabled(False)
        self.statusBar().showMessage("Rendering map at 300 dpi; waiting for tiles…")
        self.study_map.fetch(SNAPSHOT_JS, lambda payload: self._start_map_export(payload, width, path))

    def _start_map_export(self, payload: Any, width: float, path: str) -> None:
        try:
            from map_export import MapPngExport
            snapshot = json.loads(payload)
            scale = (round(width * 300) - 252) / snapshot["width"]
            overlays = []
            for widget in (self.map_legend, self.dem_colorbar):
                if not widget.isVisible():
                    continue
                image = QImage(round(widget.width() * scale), round(widget.height() * scale),
                               QImage.Format.Format_ARGB32)
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image); painter.scale(scale, scale)
                widget.render(painter, widget.rect().topLeft())
                painter.end()
                overlays.append((image, widget.x(), widget.y()))
            self._map_export_job = MapPngExport(snapshot, width, Path(path), overlays)
            self._map_export_job.finished.connect(self._map_export_finished)
        except Exception as exc:
            self._map_export_finished(path, str(exc))

    def _map_export_finished(self, path: str, error: str) -> None:
        self.export_map_button.setEnabled(True)
        if error:
            QMessageBox.warning(self, "Map export failed", error)
            self.statusBar().showMessage("Map export failed; no completed PNG was saved.", 8000)
        else:
            self.statusBar().showMessage(f"Saved 300 dpi map: {path}", 12000)
            self.output.append(f"> Saved 300 dpi map PNG: {path}")

    def clear_active_map_raster(self) -> None:
        """Study Area shows geometry only; prepared rasters belong to data tabs."""
        self.study_map.set_dem_image(None)
        self.dem_colorbar_available = False
        self.dem_colorbar.clear()
        self.dem_colorbar.setVisible(False)
        self.active_preview_context = None
        self.position_map_overlays()

    def _make_study_builder(self) -> QWidget:
        """Optional map-based study-area builder. The GeoJSON file path above
        stays the single source of truth; this only writes a file into it."""
        group = QGroupBox("Study-area builder (optional)")
        outer = QVBoxLayout(group)
        intro = (
            "Define the model domain first, then define its ocean boundary and observation locations. "
            "AutoCF validates these roles and writes one study-area GeoJSON for every later workflow step.")
        self.add_info(outer, intro, "Study-area builder")
        tabs = QTabWidget()
        tabs.addTab(self._make_draw_tab(), "Draw AOI")
        tabs.addTab(self._make_ocean_tab(), "Ocean boundary")
        tabs.addTab(self._make_observation_tab(), "Observation points")
        tabs.setMinimumHeight(300)
        tabs.currentChanged.connect(lambda _: self.study_map.set_mode("none"))
        outer.addWidget(tabs)
        return group

    def _make_draft_bar(self) -> QWidget:
        """Draft summary and save action, pinned below the scrolling controls."""
        frame = QGroupBox("Study-area draft")
        outer = QVBoxLayout(frame)
        self.draft_status = QLabel("Draft is empty.")
        self.draft_status.setWordWrap(True)
        outer.addWidget(self.draft_status)
        actions = QHBoxLayout()
        load_current = QPushButton("Reload")
        load_current.setToolTip("Copy the domain, ocean line, and points from the study-area GeoJSON into the editable draft.")
        load_current.clicked.connect(self.load_draft_from_file)
        clear_all = QPushButton("Clear draft")
        clear_all.setToolTip("Discard every drawn shape and point in the draft.")
        clear_all.clicked.connect(self.clear_draft)
        self.save_study_button = QPushButton("Save study area & continue")
        self.save_study_button.setObjectName("primary")
        self.save_study_button.setToolTip(
            "Validate the study-area draft, save it as the project's GeoJSON, and continue to Terrain.")
        self.save_study_button.clicked.connect(self.prepare_study_geojson)
        actions.addWidget(load_current)
        actions.addWidget(clear_all)
        actions.addWidget(self.save_study_button)
        actions.addStretch(1)
        outer.addLayout(actions)
        return frame

    def _make_draw_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        upload = QGroupBox("Use your own AOI file")
        upload_form = QFormLayout(upload)
        self.add_info(upload_form,
                      "Upload an existing polygon GeoJSON, or define the AOI with drawing or a USGS watershed below.",
                      "AOI GeoJSON")
        upload_form.addRow("AOI GeoJSON", self.path_row(self.geojson_path, self.choose_geojson))
        layout.addWidget(upload)

        drawing = QGroupBox("Draw an AOI")
        drawing_layout = QVBoxLayout(drawing)
        self.add_info(drawing_layout,
                      "Draw directly on the map. Double-click, right-click, or press Enter to close a polygon; Esc cancels; Ctrl+Z undoes.",
                      "Draw an AOI")
        row = QHBoxLayout()
        for text, mode, tip in (
            ("Draw domain polygon", "polygon", "Click each corner of the model domain."),
            ("Draw domain rectangle", "rectangle", "Click two opposite corners."),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, m=mode: self.start_draw(m))
            row.addWidget(button)
        row.addStretch(1)
        drawing_layout.addLayout(row)
        tools = QHBoxLayout()
        for text, callback in (
            ("Finish shape", lambda: self.study_map.finish_shape()),
            ("Undo", lambda: self.study_map.undo()),
            ("Stop drawing", lambda: self.study_map.set_mode("none")),
            ("Clear domain", lambda: self.study_map.clear_role("domain")),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, cb=callback: cb())
            tools.addWidget(button)
        tools.addStretch(1)
        drawing_layout.addLayout(tools)
        self.draw_status = QLabel("Not drawing.")
        self.draw_status.setWordWrap(True)
        drawing_layout.addWidget(self.draw_status)
        layout.addWidget(drawing)
        layout.addWidget(self._make_huc_tab())
        layout.addStretch(1)
        return tab

    def _make_huc_tab(self) -> QWidget:
        tab = QGroupBox("Select a USGS watershed boundary")
        layout = QVBoxLayout(tab)
        hint = (
            "Choose one or more HUC2–HUC16 polygons from the USGS Watershed Boundary Dataset. "
            "Adjacent selections are dissolved into one AOI with their shared edges removed. United States coverage only.")
        self.add_info(layout, hint, "USGS watershed boundary")
        form = QFormLayout()
        self.huc_level = QComboBox()
        self.huc_level.addItems(list(HUC_LEVELS))
        self.huc_level.setCurrentText("HUC8 — Subbasin")
        self.huc_extent = QComboBox()
        self.huc_extent.addItems([
            "Current map view", "Around searched location",
            "Around the loaded domain", "Around the drawn draft domain"])
        form.addRow("Level", self.huc_level)
        form.addRow("Search extent", self.huc_extent)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.huc_search_button = QPushButton("Search watersheds")
        self.huc_search_button.clicked.connect(self.search_huc)
        pick = QPushButton("Pick on map")
        pick.setToolTip("Click a watershed polygon on the map to select it.")
        pick.clicked.connect(lambda: self.study_map.set_mode("huc"))
        self.huc_use_button = QPushButton("Use / merge selected as domain")
        self.huc_use_button.setEnabled(False)
        self.huc_use_button.clicked.connect(self.use_huc_as_domain)
        clear = QPushButton("Clear results")
        clear.clicked.connect(self.clear_huc)
        row.addWidget(self.huc_search_button)
        row.addWidget(pick)
        row.addWidget(self.huc_use_button)
        row.addWidget(clear)
        row.addStretch(1)
        layout.addLayout(row)
        self.huc_list = QListWidget()
        self.huc_list.setMinimumHeight(130)
        self.huc_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.huc_list.itemSelectionChanged.connect(self.huc_selection_changed)
        layout.addWidget(self.huc_list)
        self.huc_status = QLabel("No watersheds loaded.")
        self.huc_status.setWordWrap(True)
        layout.addWidget(self.huc_status)
        return tab

    def _make_ocean_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        boundary = QGroupBox("Ocean boundary line")
        boundary_layout = QVBoxLayout(boundary)
        boundary_hint = (
            "Trace only the seaward/open-model edge where tide or surge may enter and leave. Do not trace the "
            "shoreline or the complete AOI outline. Water-level points supply the boundary time series; they do "
            "not create or move the boundary line.")
        self.add_info(boundary_layout, boundary_hint, "Ocean boundary line")
        boundary_row = QHBoxLayout()
        draw_boundary = QPushButton("Draw ocean boundary")
        draw_boundary.setToolTip("Click along the seaward edge of the AOI; double-click, right-click, or press Enter to finish.")
        draw_boundary.clicked.connect(lambda: self.start_draw("ocean_boundary"))
        finish_boundary = QPushButton("Finish ocean line")
        finish_boundary.clicked.connect(lambda: self.study_map.finish_shape())
        clear_boundary = QPushButton("Clear ocean line")
        clear_boundary.clicked.connect(lambda: self.study_map.clear_role("ocean_boundary"))
        for button in (draw_boundary, finish_boundary, clear_boundary):
            boundary_row.addWidget(button)
        boundary_row.addStretch(1)
        boundary_layout.addLayout(boundary_row)
        layout.addWidget(boundary)
        layout.addWidget(self._make_waterlevel_group())
        layout.addStretch(1)
        return tab

    def _make_waterlevel_group(self) -> QWidget:
        tab = QGroupBox("Water-level forcing points")
        layout = QVBoxLayout(tab)
        self.add_info(
            layout,
            "The SFINCS ocean forcing locations written into the study-area GeoJSON "
            "(role ocean_forcing_point). Save the draft from the map panel beside this.",
            "Water-level forcing points")
        self.wl_mode_group = QButtonGroup(tab)
        self.wl_keep = QRadioButton("Keep the points already in the file")
        self.wl_keep.setToolTip("Reload the ocean forcing points defined in the study-area GeoJSON selected above.")
        self.wl_stations = QRadioButton("Pick catalog water-level stations")
        self.wl_manual = QRadioButton("Place points manually on the map")
        self.wl_keep.setChecked(True)
        for index, button in enumerate((self.wl_keep, self.wl_stations, self.wl_manual)):
            self.wl_mode_group.addButton(button, index)
            layout.addWidget(button)
        self.wl_mode_group.idToggled.connect(self.waterlevel_mode_changed)
        provider_row = QFormLayout()
        self.wl_catalog_provider = QComboBox()
        for key, spec in WATER_LEVEL_PROVIDERS.items():
            self.wl_catalog_provider.addItem(str(spec["label"]), key)
        self.wl_catalog_provider.currentIndexChanged.connect(
            lambda: self.waterlevel_provider_changed(str(self.wl_catalog_provider.currentData())))
        provider_row.addRow("Station catalog", self.wl_catalog_provider)
        layout.addLayout(provider_row)
        row = QHBoxLayout()
        self.coops_button = QPushButton("Load stations")
        self.coops_button.clicked.connect(lambda: self.load_coops(False))
        refresh = QPushButton("Refresh catalog")
        refresh.clicked.connect(lambda: self.load_coops(True))
        self.coops_add_button = QPushButton("Use selected stations")
        self.coops_add_button.setToolTip(
            "Selected catalog markers are synchronized into the draft automatically; this button also exits selection mode.")
        self.coops_add_button.clicked.connect(self.add_selected_stations)
        clear = QPushButton("Clear points")
        clear.clicked.connect(self.clear_waterlevel_points)
        row.addWidget(self.coops_button)
        row.addWidget(refresh)
        row.addWidget(self.coops_add_button)
        row.addWidget(clear)
        row.addStretch(1)
        layout.addLayout(row)
        self.coops_near_domain = QCheckBox("Only stations near the domain")
        self.coops_near_domain.setToolTip(
            "List only stations within the station-distance set above. "
            "Clear this to browse every station in the selected catalog.")
        self.coops_near_domain.setChecked(True)
        self.coops_near_domain.toggled.connect(self.water_level_scope_changed)
        layout.addWidget(self.coops_near_domain)
        self.station_list = QListWidget()
        self.station_list.setMinimumHeight(130)
        self.station_list.itemChanged.connect(self.station_item_changed)
        layout.addWidget(self.station_list)
        self.wl_status = QLabel("No water-level points in the draft.")
        self.wl_status.setWordWrap(True)
        layout.addWidget(self.wl_status)
        datum_note = (
            "Datum note: GESLA datum metadata is unspecified and UHSLC/JASL levels are relative to station zero. "
            "For boundary forcing AutoCF fixes the first 12-hour median at model 0 m; observation comparisons remain datum-neutral.")
        self.add_info(layout, datum_note, "Water-level datum note")
        return tab

    def _make_river_group(self) -> QWidget:
        """Optional USGS/manual river points; GloFAS detects crossings automatically."""
        group = QGroupBox("USGS or manual river source points")
        layout = QVBoxLayout(group)
        hint = (
            "Used only when USGS is the selected provider. GloFAS instead detects river-network "
            "cells flowing from outside to inside the domain and creates its own inflow points."
            " USGS/manual locations are written with role river_source.")
        self.add_info(layout, hint, "River source points")
        row = QHBoxLayout()
        self.usgs_button = QPushButton("Find USGS gauges in domain")
        self.usgs_button.clicked.connect(self.search_usgs)
        self.usgs_pick_button = QPushButton("Pick on map")
        self.usgs_pick_button.setToolTip("Click gauge markers on the map to select or deselect them.")
        self.usgs_pick_button.clicked.connect(lambda: self.study_map.set_mode("usgs"))
        self.usgs_add_button = QPushButton("Add checked gauges")
        self.usgs_add_button.clicked.connect(self.add_selected_usgs)
        row.addWidget(self.usgs_button)
        row.addWidget(self.usgs_pick_button)
        row.addWidget(self.usgs_add_button)
        row.addStretch(1)
        layout.addLayout(row)
        manual_row = QHBoxLayout()
        manual = QPushButton("Place points manually")
        manual.clicked.connect(lambda: self.start_draw("river_source"))
        stop = QPushButton("Stop placing")
        stop.clicked.connect(lambda: self.study_map.set_mode("none"))
        clear = QPushButton("Clear river points")
        clear.clicked.connect(self.clear_forcing_river_points)
        manual_row.addWidget(manual)
        manual_row.addWidget(stop)
        manual_row.addWidget(clear)
        manual_row.addStretch(1)
        layout.addLayout(manual_row)
        layout.addWidget(self._make_coordinate_editor(
            "river", "river_source", "riv",
            "Enter the exact inflow latitude and longitude, or paste 'latitude, longitude'."))
        self.usgs_limit_to_max = QCheckBox("List only the nearest gauges, up to the maximum-gauges setting")
        self.usgs_limit_to_max.setChecked(True)
        self.usgs_limit_to_max.setToolTip(
            "Ranks gauges by distance to the domain centroid and keeps the closest, matching how the build "
            "selects them. Clear this to see every gauge the query returned.")
        self.usgs_limit_to_max.toggled.connect(self.populate_usgs)
        layout.addWidget(self.usgs_limit_to_max)
        self.usgs_list = QListWidget()
        self.usgs_list.setMinimumHeight(130)
        self.usgs_list.itemChanged.connect(self.usgs_item_changed)
        layout.addWidget(self.usgs_list)
        self.usgs_status = QLabel("No river source points in the draft.")
        self.usgs_status.setWordWrap(True)
        layout.addWidget(self.usgs_status)
        return group

    def _make_coordinate_editor(self, key: str, role: str, prefix: str,
                                help_text: str) -> QWidget:
        """Create a coordinate-first editor shared by observations and file forcing."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(help_text)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        identifier = QLineEdit()
        identifier.setPlaceholderText(f"Automatic ({prefix}001)")
        name = QLineEdit()
        name.setPlaceholderText("Optional descriptive name")
        latitude = QLineEdit()
        latitude.setPlaceholderText("e.g. 54.063594 or paste 54.063594, -2.791879")
        longitude = QLineEdit()
        longitude.setPlaceholderText("e.g. -2.791879")
        form.addRow("Point ID", identifier)
        form.addRow("Name", name)
        form.addRow("Latitude", latitude)
        form.addRow("Longitude", longitude)
        layout.addLayout(form)
        actions = QHBoxLayout()
        add = QPushButton("Add / update point")
        remove = QPushButton("Delete selected")
        zoom = QPushButton("Zoom to selected")
        actions.addWidget(add); actions.addWidget(remove); actions.addWidget(zoom)
        actions.addStretch(1)
        layout.addLayout(actions)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["ID", "Name", "Latitude", "Longitude"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(115)
        layout.addWidget(table)
        editor = {
            "role": role, "prefix": prefix, "id": identifier, "name": name,
            "lat": latitude, "lon": longitude, "table": table,
        }
        self.coordinate_editors[key] = editor
        add.clicked.connect(lambda _=False, editor_key=key: self.add_coordinate_point(editor_key))
        remove.clicked.connect(lambda _=False, editor_key=key: self.delete_coordinate_point(editor_key))
        zoom.clicked.connect(lambda _=False, editor_key=key: self.zoom_coordinate_point(editor_key))
        table.itemSelectionChanged.connect(
            lambda editor_key=key: self.coordinate_point_selected(editor_key))
        return panel

    def _coordinate_pair(self, editor: dict[str, Any]) -> tuple[float, float]:
        lat_text = editor["lat"].text().strip()
        lon_text = editor["lon"].text().strip()
        if not lon_text:
            pieces = [part.strip() for part in re.split(r"[,;]", lat_text) if part.strip()]
            if len(pieces) == 2:
                lat_text, lon_text = pieces
        try:
            latitude, longitude = float(lat_text), float(lon_text)
        except ValueError as exc:
            raise ValueError("Enter numeric latitude and longitude, for example 54.063594, -2.791879.") from exc
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("Latitude must be between -90 and 90 degrees.")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("Longitude must be between -180 and 180 degrees.")
        return latitude, longitude

    def _next_coordinate_id(self, editor: dict[str, Any]) -> str:
        used = {
            editor["table"].item(row, 0).text().strip().casefold()
            for row in range(editor["table"].rowCount())
            if editor["table"].item(row, 0)
        }
        index = 1
        while f"{editor['prefix']}{index:03d}".casefold() in used:
            index += 1
        return f"{editor['prefix']}{index:03d}"

    def add_coordinate_point(self, key: str) -> None:
        editor = self.coordinate_editors[key]
        try:
            latitude, longitude = self._coordinate_pair(editor)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid coordinates", str(exc))
            return
        geometries = self.draft_domain_geometries or self.active_aoi_geometries()
        if geometries and not any(point_in_geojson_geometry(longitude, latitude, geometry)
                                  for geometry in geometries):
            answer = QMessageBox.question(
                self, "Point outside study area",
                "This coordinate is outside the current AOI. Add it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        identifier = editor["id"].text().strip() or self._next_coordinate_id(editor)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", identifier):
            QMessageBox.warning(
                self, "Invalid point ID",
                "Point IDs may contain only letters, numbers, underscore, hyphen, and period.")
            return
        table: QTableWidget = editor["table"]
        selected = table.selectionModel().selectedRows()
        old_id = table.item(selected[0].row(), 0).text() if selected else ""
        if old_id and old_id != identifier:
            self.study_map.remove_feature(editor["role"], old_id)
        name = editor["name"].text().strip() or identifier
        feature = role_feature(
            editor["role"], {"type": "Point", "coordinates": [longitude, latitude]},
            {"ID": identifier, "name": name, "source": "user_coordinates"})
        self.study_map.add_features([feature])
        self.study_map.focus_feature(editor["role"], identifier)
        editor["id"].setText(identifier)
        self.mark_dirty()
        if key in {"ocean", "river"}:
            self.request_forcing_point_save()

    def coordinate_point_selected(self, key: str) -> None:
        editor = self.coordinate_editors[key]
        table: QTableWidget = editor["table"]
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        editor["id"].setText(table.item(row, 0).text())
        editor["name"].setText(table.item(row, 1).text())
        editor["lat"].setText(table.item(row, 2).text())
        editor["lon"].setText(table.item(row, 3).text())

    def delete_coordinate_point(self, key: str) -> None:
        editor = self.coordinate_editors[key]
        table: QTableWidget = editor["table"]
        rows = table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "Select a point", "Select a point row to delete.")
            return
        identifier = table.item(rows[0].row(), 0).text()
        self.study_map.remove_feature(editor["role"], identifier)
        for field in (editor["id"], editor["name"], editor["lat"], editor["lon"]):
            field.clear()
        self.mark_dirty()
        if key in {"ocean", "river"}:
            self.request_forcing_point_save()

    def zoom_coordinate_point(self, key: str) -> None:
        editor = self.coordinate_editors[key]
        rows = editor["table"].selectionModel().selectedRows()
        if rows:
            self.study_map.focus_feature(
                editor["role"], editor["table"].item(rows[0].row(), 0).text())

    def refresh_coordinate_tables(self, payload: Any, monitor: bool = True) -> None:
        if not payload:
            return
        try:
            document = json.loads(payload)
        except Exception:
            return
        by_role: dict[str, list[dict[str, Any]]] = {}
        for feature in document.get("features", []):
            role = str((feature.get("properties") or {}).get("role", ""))
            if feature.get("geometry", {}).get("type") == "Point":
                by_role.setdefault(role, []).append(feature)
        for editor in self.coordinate_editors.values():
            table: QTableWidget = editor["table"]
            selected_ids = {
                table.item(index.row(), 0).text() for index in table.selectionModel().selectedRows()
                if table.item(index.row(), 0)
            }
            features = by_role.get(editor["role"], [])
            table.setRowCount(len(features))
            selected_row = -1
            for row, feature in enumerate(features):
                properties = feature.get("properties") or {}
                coordinates = feature.get("geometry", {}).get("coordinates", [None, None])
                identifier = str(properties.get("ID") or f"{editor['prefix']}{row + 1:03d}")
                values = [identifier, str(properties.get("name") or identifier),
                          f"{float(coordinates[1]):.6f}", f"{float(coordinates[0]):.6f}"]
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))
                if identifier in selected_ids:
                    selected_row = row
            if selected_row >= 0:
                table.selectRow(selected_row)
        signature = self.forcing_point_signature(document)
        previous = self._forcing_signature
        self._forcing_signature = signature
        row = self.navigation.currentRow()
        in_forcing = 0 <= row < len(self.page_keys) and self.page_keys[row] == "forcing"
        if monitor and not self.loading and previous is not None and previous != signature and in_forcing:
            self._save_forcing_point_payload(payload)

    @staticmethod
    def forcing_point_signature(document: dict[str, Any]) -> str:
        features = []
        for feature in document.get("features", []):
            props = feature.get("properties") or {}
            if props.get("role") in {"ocean_forcing_point", "river_source"}:
                features.append({"geometry": feature.get("geometry"),
                                 "properties": {k: v for k, v in props.items() if k != "_seq"}})
        return json.dumps(features, sort_keys=True)

    @staticmethod
    def merge_forcing_points(saved: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        roles = {"ocean_forcing_point", "river_source"}
        points, ids = [], set()
        for feature in draft.get("features", []):
            props = dict(feature.get("properties") or {})
            role = props.get("role")
            if role not in roles:
                continue
            geometry = feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []
            identifier = str(props.get("ID", "")).strip()
            if not identifier or (role, identifier) in ids:
                raise ValueError("Forcing point IDs must be nonempty and unique within each forcing type.")
            if (geometry.get("type") != "Point" or len(coordinates) < 2
                    or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in coordinates[:2])
                    or not -180 <= coordinates[0] <= 180 or not -90 <= coordinates[1] <= 90):
                raise ValueError(f"Invalid WGS84 coordinates for forcing point {identifier}.")
            ids.add((role, identifier)); props.pop("_seq", None); props["ID"] = identifier
            points.append({**feature, "properties": props})
        document = {**saved, "features": [f for f in saved.get("features", [])
                                          if (f.get("properties") or {}).get("role") not in roles] + points}
        if len(split_study_geojson(document)["domain"]) != 1:
            raise ValueError("Define one study-area domain before saving forcing locations.")
        return document

    @staticmethod
    def atomic_write_text(path: Path, content: str) -> None:
        output = QSaveFile(str(path))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(output.errorString())
        data = content.encode("utf-8")
        if output.write(data) != len(data):
            output.cancelWriting(); raise OSError(output.errorString())
        if not output.commit():
            raise OSError(output.errorString())

    def request_forcing_point_save(self, after: Any = None) -> None:
        self._forcing_save_pending = True
        self.study_map.fetch_draft(lambda payload: self._save_forcing_point_payload(payload, after))

    def clear_forcing_river_points(self) -> None:
        self.study_map.clear_role("river_source")
        self.request_forcing_point_save()

    def _save_forcing_point_payload(self, payload: Any, after: Any = None) -> bool:
        target = None
        original = None
        written = False
        try:
            if not payload:
                raise ValueError("The map is not ready. Wait for it to load and retry validation.")
            draft = json.loads(payload)
            target = (self.user_path(self.geojson_path.text().strip()) if self.geojson_path.text().strip()
                      else self.study_geojson_default())
            original = target.read_text(encoding="utf-8") if target.exists() else None
            saved = json.loads(original) if original is not None else draft
            merged = self.merge_forcing_points(saved, draft)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.atomic_write_text(target, json.dumps(merged, indent=1))
            written = True
            self.geojson_path.setText(str(target))
            self.mark_dirty()
            if not self.save_config():
                raise OSError("The YAML could not be saved; forcing-location changes were not committed.")
            self.refresh_coordinate_tables(payload, monitor=False)
            self._forcing_save_error = ""
            for kind in ("ocean", "river"):
                getattr(self, f"{kind}_file_status").setText(
                    "Locations saved to the study-area file. Validate the time series before building.")
            self.statusBar().showMessage(f"Forcing locations saved: {target}", 7000)
        except Exception as exc:
            if written and target is not None:
                try:
                    if original is not None:
                        self.atomic_write_text(target, original)
                    else:
                        target.unlink(missing_ok=True)
                except Exception as rollback:
                    exc = OSError(f"{exc}; restoring the previous GeoJSON also failed: {rollback}")
            self._forcing_save_error = str(exc)
            self.mark_dirty()
            for kind in ("ocean", "river"):
                getattr(self, f"{kind}_file_status").setText(f"NOT SAVED — {exc}. Retry Validate file and points.")
            QMessageBox.warning(self, "Forcing locations not saved", str(exc))
            return False
        finally:
            self._forcing_save_pending = False
        if after:
            after()
        return True

    def _make_observation_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        hint = (
            "Select measured locations for model evaluation. NOAA CO-OPS, GESLA, and UHSLC/JASL gauges support water-level time-series "
            "comparison; USGS high-water marks support event-peak comparison when their vertical datum is compatible.")
        self.add_info(layout, hint, "Observation points")

        noaa = QGroupBox("Water-level observation gauges")
        noaa_layout = QVBoxLayout(noaa)
        obs_provider_row = QFormLayout()
        self.obs_catalog_provider = QComboBox()
        for key, spec in WATER_LEVEL_PROVIDERS.items():
            self.obs_catalog_provider.addItem(str(spec["label"]), key)
        self.obs_catalog_provider.currentIndexChanged.connect(
            lambda: self.waterlevel_provider_changed(str(self.obs_catalog_provider.currentData())))
        obs_provider_row.addRow("Station catalog", self.obs_catalog_provider)
        noaa_layout.addLayout(obs_provider_row)
        noaa_row = QHBoxLayout()
        self.obs_noaa_load = QPushButton("Show stations")
        self.obs_noaa_load.clicked.connect(lambda: self.load_coops(False))
        obs_noaa_pick = QPushButton("Select on map")
        obs_noaa_pick.clicked.connect(lambda: self.study_map.set_mode("obs_noaa"))
        self.obs_noaa_add = QPushButton("Use selected observations")
        self.obs_noaa_add.setToolTip(
            "Selected catalog markers are synchronized into the draft automatically; this button also exits selection mode.")
        self.obs_noaa_add.clicked.connect(self.add_observation_noaa)
        for button in (self.obs_noaa_load, obs_noaa_pick, self.obs_noaa_add):
            noaa_row.addWidget(button)
        noaa_row.addStretch(1); noaa_layout.addLayout(noaa_row)
        self.obs_noaa_list = QListWidget(); self.obs_noaa_list.setMinimumHeight(105)
        self.obs_noaa_list.itemChanged.connect(self.observation_noaa_item_changed)
        noaa_layout.addWidget(self.obs_noaa_list)
        self.obs_noaa_status = QLabel("Load a station catalog to select observation gauges.")
        self.obs_noaa_status.setWordWrap(True); noaa_layout.addWidget(self.obs_noaa_status)
        layout.addWidget(noaa)

        hwm = QGroupBox("USGS STN high-water marks")
        hwm_layout = QVBoxLayout(hwm)
        hwm_event_row = QHBoxLayout()
        self.hwm_load_events = QPushButton("Load flood events")
        self.hwm_load_events.clicked.connect(self.load_hwm_events)
        self.hwm_event = QComboBox(); self.hwm_event.setMinimumWidth(220)
        self.hwm_event.currentIndexChanged.connect(lambda *_: self.load_hwm_marks())
        self.hwm_load_marks = QPushButton("Show marks in AOI")
        self.hwm_load_marks.clicked.connect(self.load_hwm_marks)
        hwm_pick = QPushButton("Select on map"); hwm_pick.clicked.connect(lambda: self.study_map.set_mode("hwm"))
        self.hwm_add = QPushButton("Use selected observations")
        self.hwm_add.setToolTip(
            "Selected high-water marks are synchronized into the draft automatically; this button also exits selection mode.")
        self.hwm_add.clicked.connect(self.add_hwm_observations)
        hwm_event_row.addWidget(self.hwm_load_events)
        hwm_event_row.addWidget(self.hwm_event, 1)
        hwm_event_row.addWidget(self.hwm_load_marks)
        hwm_event_row.addWidget(hwm_pick)
        hwm_event_row.addWidget(self.hwm_add)
        hwm_layout.addLayout(hwm_event_row)
        hwm_quality_row = QHBoxLayout()
        hwm_quality_row.addWidget(QLabel("HWM quality"))
        self.hwm_quality = QComboBox()
        self.hwm_quality.addItem("All qualities", "")
        for quality in ("Excellent", "Good", "Fair", "Poor", "VP"):
            self.hwm_quality.addItem(quality, quality)
        self.hwm_quality.currentIndexChanged.connect(lambda *_: self.populate_hwm())
        hwm_quality_row.addWidget(self.hwm_quality)
        hwm_quality_row.addStretch(1)
        hwm_layout.addLayout(hwm_quality_row)
        self.hwm_list = QListWidget(); self.hwm_list.setMinimumHeight(105)
        self.hwm_list.itemChanged.connect(self.hwm_item_changed); hwm_layout.addWidget(self.hwm_list)
        self.hwm_status = QLabel("Load the USGS flood-event catalog, choose the event, then show marks inside the AOI.")
        self.hwm_status.setWordWrap(True); hwm_layout.addWidget(self.hwm_status)
        layout.addWidget(hwm)

        manual = QGroupBox("Manual observation points")
        manual_layout = QVBoxLayout(manual)
        row = QHBoxLayout()
        place = QPushButton("Place observation points")
        place.clicked.connect(lambda: self.start_draw("observation_point"))
        stop = QPushButton("Stop placing")
        stop.clicked.connect(lambda: self.study_map.set_mode("none"))
        clear = QPushButton("Clear observation points")
        clear.clicked.connect(self.clear_observation_points)
        row.addWidget(place)
        row.addWidget(stop)
        row.addWidget(clear)
        row.addStretch(1)
        manual_layout.addLayout(row)
        manual_layout.addWidget(self._make_coordinate_editor(
            "observation", "observation_point", "obs",
            "Enter exact WGS84 coordinates, or paste 'latitude, longitude' into Latitude."))
        self.extra_status = QLabel("No observation points in the draft.")
        self.extra_status.setWordWrap(True)
        manual_layout.addWidget(self.extra_status)
        layout.addWidget(manual)
        layout.addStretch(1)
        return tab

    def _make_event_page(self) -> QWidget:
        page, layout = self.page("Event & Time", "Choose dates manually or load a track and recommended window from NOAA IBTrACS.")
        splitter = QSplitter(Qt.Orientation.Horizontal)
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        manual = QGroupBox("Simulation period (UTC)")
        form = QFormLayout(manual)
        self.event_name = QLineEdit()
        self.start_time, self.end_time = QDateTimeEdit(), QDateTimeEdit()
        for widget in (self.start_time, self.end_time):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm 'UTC'")
            widget.setTimeSpec(Qt.TimeSpec.UTC)
        form.addRow("Event name", self.event_name)
        form.addRow("Start", self.start_time)
        form.addRow("End", self.end_time)
        controls_layout.addWidget(manual)
        catalog = QGroupBox("IBTrACS tropical-cyclone catalog")
        cat = QFormLayout(catalog)
        self.region_mode, self.region_choice = QComboBox(), QComboBox()
        self.region_mode.addItems(["Ocean basin", "Continent / coast", "Near study area"])
        self.ib_year, self.ib_storm = QComboBox(), QComboBox()
        cat.addRow("Filter", self.region_mode)
        cat.addRow("Region", self.region_choice)
        cat.addRow("Season", self.ib_year)
        cat.addRow("Storm", self.ib_storm)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.load_ibtracs_button = QPushButton("Load IBTrACS")
        refresh = QPushButton("Refresh catalog")
        apply = QPushButton("Use selected event & continue")
        apply.setObjectName("primary")
        self.load_ibtracs_button.clicked.connect(lambda: self.load_ibtracs(False))
        refresh.clicked.connect(lambda: self.load_ibtracs(True))
        apply.clicked.connect(self.apply_selected_storm)
        row_layout.addWidget(self.load_ibtracs_button)
        row_layout.addWidget(refresh)
        row_layout.addWidget(apply)
        cat.addRow(row)
        self.ib_info = QLabel("Load the NOAA catalog, then filter by basin, continent/coast, or proximity to the domain.")
        self.ib_info.setWordWrap(True)
        cat.addRow(self.ib_info)
        controls_layout.addWidget(catalog)
        controls_layout.addStretch(1)
        self.event_map = WebMap()
        splitter.addWidget(controls)
        splitter.addWidget(self.event_map)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.region_mode.currentTextChanged.connect(self.populate_regions)
        self.region_choice.currentTextChanged.connect(self.populate_ib_years)
        self.ib_year.currentTextChanged.connect(self.populate_ib_storms)
        self.ib_storm.currentIndexChanged.connect(self.ib_storm_changed)
        # Choosing a storm only updates its preview. Advancing to Study Area is
        # an explicit user decision made with "Use selected event & continue".
        self.event_name.textChanged.connect(self.mark_dirty)
        self.event_name.editingFinished.connect(self.ensure_event_workspace_current)
        self.start_time.dateTimeChanged.connect(self.mark_dirty)
        self.end_time.dateTimeChanged.connect(self.mark_dirty)
        self.start_time.dateTimeChanged.connect(self.invalidate_water_level_coverage)
        self.end_time.dateTimeChanged.connect(self.invalidate_water_level_coverage)
        self.populate_regions()
        return page

    def _make_terrain_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        primary = QGroupBox("Source 1 — highest priority")
        form = QFormLayout(primary)
        dataset_names = list(TOPOBATHY_DATASETS) + ["file"]
        self.topo_source = QComboBox(); self.topo_source.setEditable(True); self.topo_source.addItems(dataset_names)
        self.topo_url, self.topo_file = QLineEdit(), QLineEdit()
        self.topo_resolution = QDoubleSpinBox(); self.topo_resolution.setRange(0.1, 5000); self.topo_resolution.setSuffix(" m")
        self.vertical_datum = QComboBox(); self.vertical_datum.setEditable(True); self.vertical_datum.addItems(["NAVD88", "EGM2008", "MSL", "EGM96"])
        self.topo_native_datum = QComboBox(); self.topo_native_datum.setEditable(True); self.topo_native_datum.addItems(["NAVD88", "EGM2008", "MSL", "EGM96"])
        self.topo_acquisition_date = QLineEdit(); self.topo_acquisition_date.setPlaceholderText("YYYY-MM-DD (optional)")
        self.topo_provider = QButtonGroup(primary)
        self.topo_download = QRadioButton("AutoCF downloads it")
        self.topo_own = QRadioButton("Use my own GeoTIFF")
        self.topo_download.setChecked(True)
        provider_row = QWidget(); provider_layout = QHBoxLayout(provider_row)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        for index, button in enumerate((self.topo_download, self.topo_own)):
            self.topo_provider.addButton(button, index)
            provider_layout.addWidget(button)
        provider_layout.addStretch(1)
        form.addRow("Elevation data", provider_row)
        form.addRow("Source", self.topo_source)
        form.addRow("Remote VRT/COG URL", self.topo_url)
        form.addRow("User raster", self.path_row(self.topo_file, lambda: self.choose_file(self.topo_file, "Raster (*.tif *.tiff *.vrt)")))
        form.addRow("Preparation resolution (automatic)", self.topo_resolution)
        form.addRow("Source native datum", self.topo_native_datum)
        form.addRow("Acquisition date", self.topo_acquisition_date)
        form.addRow("Target vertical datum", self.vertical_datum)
        self.topo_provider.idToggled.connect(self.topo_provider_changed)
        self.topo_source.currentTextChanged.connect(self.sync_topo_provider)
        # activated is emitted only for a user's menu choice, so loading a YAML
        # never opens a browser or displays an unsolicited credential notice.
        self.topo_source.activated.connect(self.topobathy_dataset_activated)
        layout.addWidget(primary)
        gap = QGroupBox("Source 2 — fills gaps after Source 1")
        gap_form = QFormLayout(gap)
        self.gap_source = QComboBox(); self.gap_source.setEditable(True)
        self.gap_source.addItems(["GEBCO_2024_15_ARCSEC", "none"] + [
            name for name in dataset_names
            if name != "GEBCO_2024_15_ARCSEC"
            and name.lower() not in AUTOMATIC_TOPOBATHY_SOURCES
        ])
        self.gap_url = QLineEdit()
        self.gap_resolution = QDoubleSpinBox(); self.gap_resolution.setRange(1, 10000); self.gap_resolution.setSuffix(" m")
        self.gap_file = QLineEdit()
        self.gap_native_datum = QComboBox(); self.gap_native_datum.setEditable(True); self.gap_native_datum.addItems(["MSL", "EGM2008", "NAVD88", "EGM96"])
        self.gap_acquisition_date = QLineEdit(); self.gap_acquisition_date.setPlaceholderText("YYYY-MM-DD (optional)")
        gap_form.addRow("Source", self.gap_source)
        gap_form.addRow("Remote COG URL", self.gap_url)
        gap_form.addRow("User raster", self.path_row(self.gap_file, lambda: self.choose_file(self.gap_file, "Raster (*.tif *.tiff *.vrt)")))
        gap_form.addRow("Preparation resolution (automatic)", self.gap_resolution)
        gap_form.addRow("Source native datum", self.gap_native_datum)
        gap_form.addRow("Acquisition date", self.gap_acquisition_date)
        self.gap_source.currentTextChanged.connect(self.sync_gap_source)
        self.gap_source.activated.connect(self.gapfill_dataset_activated)
        layout.addWidget(gap)
        self.extra_terrain_entries = []
        self.extra_terrain_layout = QVBoxLayout()
        layout.addLayout(self.extra_terrain_layout)
        self.add_terrain_entry_button = QPushButton("Add Data Entry")
        self.add_terrain_entry_button.clicked.connect(lambda: self.add_terrain_entry())
        layout.addWidget(self.add_terrain_entry_button)
        priority_hint = QLabel("Sources are applied from top to bottom; later sources fill only NoData gaps. "
                              "Place detailed regional data before broad-coverage fallback data such as GEBCO.")
        priority_hint.setWordWrap(True); layout.addWidget(priority_hint)
        self.noaa_dav_download = QPushButton("Find newer NOAA coastal DEM / lidar (optional)")
        self.noaa_dav_download.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(NOAA_DIGITAL_COAST_DATA_VIEWER)))
        self.noaa_dav_download.setToolTip(
            "Open NOAA Digital Coast Data Access Viewer. Download a GeoTIFF, select 'Use my own GeoTIFF', "
            "and enter its native datum and acquisition date.")
        layout.addWidget(self.noaa_dav_download)
        layout.addWidget(self._make_terrain_action_group())
        note = (
            "Built-in source datums are fixed from provider metadata. If a later source differs from the target, "
            "the managed Windows runtime first tries AutoCF's isolated Transformez grid helper, then uses "
            "the cached local-overlap method only as an explicitly approximate fallback. The helper is "
            "separate from the Python 3.11 model runtime. AutoCF writes method, uncertainty, source-date, "
            "and cell-level source provenance. "
            "It never silently relabels Source 1."
        )
        self.add_info(layout, note, "Terrain vertical datums")
        # Keep the prepared-raster state widgets alive for the automatic map
        # overlay, but do not expose a separate preview-controls box. The
        # prepared topobathy and its dynamic colour bar are the terrain preview.
        self.terrain_preview_controls = self._make_dem_group()
        self.terrain_preview_controls.setVisible(False)
        layout.addWidget(self.terrain_preview_controls)
        layout.addStretch(1)
        # Built-in datasets own their canonical remote locations. Populate the
        # fields before dirty-state connections are installed so a fresh model
        # always serializes a complete terrain configuration.
        self.sync_topo_provider()
        self.sync_gap_source()
        self.connect_dirty([self.topo_source, self.vertical_datum, self.topo_native_datum,
                            self.topo_acquisition_date, self.gap_source, self.gap_native_datum,
                            self.gap_acquisition_date, self.topo_url, self.topo_file, self.gap_url,
                            self.gap_file, self.topo_resolution, self.gap_resolution])

        left = QWidget(); left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.scroll(page), 1)
        left.setMinimumWidth(360)
        right = self.map_host("terrain"); right.setMinimumWidth(380)
        outer, outer_layout = self.page("Terrain & Bathymetry", "Configure primary elevation, gap filling, and vertical datum, and preview the terrain over the model domain.")
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left); splitter.addWidget(right)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 840])
        outer_layout.addWidget(splitter, 1)
        return outer

    def _make_terrain_action_group(self) -> QWidget:
        group = QGroupBox("Prepare terrain")
        layout = QVBoxLayout(group)
        note = (
            "Downloads and reprojects the elevation rasters onto the model grid now, so the build does not "
            "have to. Own GeoTIFFs are copied and reprojected the same way. Results are cached in the run "
            "directory, and a later build reuses them instead of fetching again.")
        self.add_info(layout, note, "Prepare terrain")
        row = QHBoxLayout()
        self.terrain_button = QPushButton("Load terrain / topobathy")
        self.terrain_button.setObjectName("primary")
        self.terrain_button.clicked.connect(self.run_terrain)
        show_files = QPushButton("Show prepared files")
        show_files.clicked.connect(self.show_terrain_files)
        row.addWidget(self.terrain_button)
        row.addWidget(show_files)
        row.addStretch(1)
        layout.addLayout(row)
        self.terrain_status = QLabel("Not prepared yet in this session.")
        self.terrain_status.setWordWrap(True)
        layout.addWidget(self.terrain_status)
        return group

    def topo_provider_changed(self, index: int, checked: bool) -> None:
        if not checked or self.loading:
            return
        if index == 1:
            self.topo_source.setCurrentText("file")
        elif self.topo_source.currentText().strip().lower() == "file":
            self.topo_source.setCurrentText("NOAA_CUDEM_1_9_ARCSEC")
        self.sync_topo_provider()

    def sync_topo_provider(self, *_: Any) -> None:
        source = self.topo_source.currentText().strip()
        source_key = source.lower()
        own = source_key in LOCAL_TOPOBATHY_SOURCES
        automatic = source_key in AUTOMATIC_TOPOBATHY_SOURCES
        dataset = TOPOBATHY_DATASETS.get(source.upper(), {})
        if source.upper() == "USGS_3DEP":
            dataset = TOPOBATHY_DATASETS["USGS_3DEP_1_3_ARCSEC"]
        builtin_url = BUILTIN_PRIMARY_TOPOBATHY_URLS.get(source.upper())
        current_url = self.topo_url.text().strip()
        self.topo_url.blockSignals(True)
        if automatic:
            self.topo_url.clear()
        elif builtin_url:
            self.topo_url.setText(builtin_url)
        elif current_url in BUILTIN_PRIMARY_TOPOBATHY_URLS.values():
            # Do not accidentally pair NOAA's URL with another editable source.
            self.topo_url.clear()
        self.topo_url.setReadOnly(bool(builtin_url) or automatic)
        self.topo_url.setToolTip(
            "AutoCF selects, downloads, validates, and mosaics only the official NOAA "
            "CUDEM 1/9 tiles that intersect the buffered study area."
            if automatic else (
                "AutoCF supplies this URL for the selected built-in dataset."
                if builtin_url else "Enter a directly readable remote VRT or COG URL."
            )
        )
        self.topo_url.blockSignals(False)
        if dataset.get("resolution_m") is not None:
            self.topo_resolution.setValue(float(dataset["resolution_m"]))
        self.topo_resolution.setReadOnly(bool(dataset))
        if dataset.get("vertical_datum"):
            self.topo_native_datum.setCurrentText(str(dataset["vertical_datum"]))
        self.topo_native_datum.setEnabled(not bool(dataset))
        self.topo_native_datum.setToolTip(
            str(dataset.get("datum_note") or "Fixed by the selected provider's product metadata.")
            if dataset else "Required: select the native vertical datum of your raster."
        )
        if own != self.topo_own.isChecked():
            self.topo_provider.blockSignals(True)
            for button in (self.topo_download, self.topo_own):
                button.blockSignals(True)
            self.topo_own.setChecked(own)
            self.topo_download.setChecked(not own)
            for button in (self.topo_download, self.topo_own):
                button.blockSignals(False)
            self.topo_provider.blockSignals(False)
        self.topo_url.setEnabled(not own and not automatic)
        self.topo_file.setEnabled(own)
        self.topo_file.parentWidget().setEnabled(True)

    def sync_gap_source(self, *_: Any) -> None:
        source = self.gap_source.currentText().strip()
        source_key = source.lower()
        dataset = TOPOBATHY_DATASETS.get(source.upper(), {})
        if source.upper() == "USGS_3DEP":
            dataset = TOPOBATHY_DATASETS["USGS_3DEP_1_3_ARCSEC"]
        builtin_url = BUILTIN_GAPFILL_TOPOBATHY_URLS.get(source.upper())
        current_url = self.gap_url.text().strip()
        self.gap_url.blockSignals(True)
        if builtin_url:
            self.gap_url.setText(builtin_url)
        elif current_url in BUILTIN_GAPFILL_TOPOBATHY_URLS.values():
            self.gap_url.clear()
        self.gap_url.setReadOnly(bool(builtin_url))
        enabled = source_key not in LOCAL_TOPOBATHY_SOURCES | {"none"}
        self.gap_url.setEnabled(enabled)
        self.gap_file.setEnabled(source_key in LOCAL_TOPOBATHY_SOURCES)
        if dataset.get("resolution_m") is not None:
            self.gap_resolution.setValue(float(dataset["resolution_m"]))
        self.gap_resolution.setReadOnly(bool(dataset))
        if dataset.get("vertical_datum"):
            self.gap_native_datum.setCurrentText(str(dataset["vertical_datum"]))
        self.gap_native_datum.setEnabled(source_key != "none" and not bool(dataset))
        self.gap_native_datum.setToolTip(
            str(dataset.get("datum_note") or "Fixed by the selected provider's product metadata.")
            if dataset else "Required for a user or custom remote raster."
        )
        self.gap_url.blockSignals(False)

    def add_terrain_entry(self, values: dict[str, Any] | None = None) -> None:
        values = dict(values or {})
        box = QGroupBox(); form = QFormLayout(box)
        source = QComboBox(); source.setEditable(True)
        source.addItems(["file"] + list(TOPOBATHY_DATASETS) + ["none"])
        url, path, date = QLineEdit(), QLineEdit(), QLineEdit()
        date.setPlaceholderText("YYYY-MM-DD (optional)")
        resolution = QDoubleSpinBox(); resolution.setRange(.1, 10000); resolution.setSuffix(" m")
        resolution.setValue(30)
        datum = QComboBox(); datum.setEditable(True)
        datum.addItems(["", "NAVD88", "EGM2008", "MSL", "EGM96", "ODN"])
        entry = dict(box=box, source=source, url=url, path=path, date=date,
                     resolution=resolution, datum=datum, original=values)
        form.addRow("Source", source); form.addRow("Remote VRT/COG URL", url)
        form.addRow("User raster", self.path_row(path, lambda: self.choose_file(path, "Raster (*.tif *.tiff *.vrt)")))
        form.addRow("Preparation resolution", resolution); form.addRow("Source native datum", datum)
        form.addRow("Acquisition date", date)
        remove = QPushButton("Remove this entry")
        remove.clicked.connect(lambda: self.remove_terrain_entry(entry))
        form.addRow(remove)
        self.extra_terrain_entries.append(entry); self.extra_terrain_layout.addWidget(box)
        source.currentTextChanged.connect(lambda: self.sync_extra_terrain_entry(entry))
        source.setCurrentText(str(values.get("source", "file")))
        path.setText(str(values.get("path", ""))); url.setText(str(values.get("cog_url") or values.get("vrt_url") or ""))
        datum.setCurrentText(str(values.get("vertical_datum", "")))
        resolution.setValue(float(values.get("output_resolution_m", 30)))
        date.setText(str(values.get("acquisition_date", "")))
        self.sync_extra_terrain_entry(entry)
        self.connect_dirty([source, url, path, datum, resolution, date])
        self.renumber_terrain_entries(); self.mark_dirty()

    def renumber_terrain_entries(self) -> None:
        for index, entry in enumerate(self.extra_terrain_entries, 3):
            entry["box"].setTitle(f"Source {index} — fills gaps after Sources 1–{index - 1}")

    def remove_terrain_entry(self, entry: dict[str, Any]) -> None:
        self.extra_terrain_entries.remove(entry)
        self.extra_terrain_layout.removeWidget(entry["box"]); entry["box"].deleteLater()
        self.renumber_terrain_entries(); self.mark_dirty()

    def sync_extra_terrain_entry(self, entry: dict[str, Any]) -> None:
        key = entry["source"].currentText().strip().upper()
        dataset = TOPOBATHY_DATASETS.get(key, {})
        builtin = BUILTIN_PRIMARY_TOPOBATHY_URLS.get(key) or BUILTIN_GAPFILL_TOPOBATHY_URLS.get(key)
        if builtin:
            entry["url"].setText(builtin)
        elif entry["url"].text() in list(BUILTIN_PRIMARY_TOPOBATHY_URLS.values()) + list(BUILTIN_GAPFILL_TOPOBATHY_URLS.values()):
            entry["url"].clear()
        local = key.lower() in LOCAL_TOPOBATHY_SOURCES
        entry["path"].setEnabled(local)
        entry["url"].setReadOnly(bool(builtin))
        entry["url"].setEnabled(not local and key.lower() not in AUTOMATIC_TOPOBATHY_SOURCES | {"none"})
        if dataset.get("vertical_datum"):
            entry["datum"].setCurrentText(str(dataset["vertical_datum"]))
        if dataset.get("resolution_m"):
            entry["resolution"].setValue(float(dataset["resolution_m"]))
        entry["datum"].setEnabled(not bool(dataset) and key != "NONE")
        entry["resolution"].setReadOnly(bool(dataset))

    def extra_terrain_values(self) -> list[dict[str, Any]]:
        result = []
        for entry in self.extra_terrain_entries:
            values = dict(entry["original"])
            for key in ("path", "cog_url", "vrt_url", "acquisition_date"):
                values.pop(key, None)
            source = entry["source"].currentText().strip()
            values.update(source=source, output_resolution_m=float(entry["resolution"].value()),
                          vertical_datum=entry["datum"].currentText().strip())
            if source.lower() in LOCAL_TOPOBATHY_SOURCES:
                values["path"] = portable_yaml_path(entry["path"].text(), self.config_path)
            elif entry["url"].text().strip():
                values["cog_url"] = entry["url"].text().strip()
            if entry["date"].text().strip():
                values["acquisition_date"] = entry["date"].text().strip()
            result.append(values)
        return result

    def load_extra_terrain_entries(self, values: list[dict[str, Any]]) -> None:
        for entry in list(self.extra_terrain_entries):
            self.remove_terrain_entry(entry)
        for entry in values:
            self.add_terrain_entry(entry)

    def topobathy_dataset_activated(self, *_: Any) -> None:
        dataset = TOPOBATHY_DATASETS.get(self.topo_source.currentText().strip().upper(), {})
        if dataset.get("vertical_datum"):
            datum = str(dataset["vertical_datum"])
            self.vertical_datum.setCurrentText(datum)
            self.topo_native_datum.setCurrentText(datum)
            if datum == "NAVD88" and hasattr(self, "ocean_datum"):
                self.ocean_datum.setCurrentText("NAVD")
        if self.topo_source.currentText().strip().upper() == "NASA_DELTAX_2021":
            self.open_delta_x_download()

    def gapfill_dataset_activated(self, *_: Any) -> None:
        if self.gap_source.currentText().strip().upper() == "NASA_DELTAX_2021":
            self.open_delta_x_download()

    def open_delta_x_download(self, *_: Any) -> None:
        answer = QMessageBox.information(
            self,
            "NASA Earthdata privacy",
            "The NASA Delta-X files require a free NASA Earthdata sign-in.\n\n"
            "AutoCF will not ask for, read, collect, store, or transmit your username, password, "
            "browser cookies, Earthdata token, or temporary S3 credentials. Your sign-in stays in "
            "your web browser.\n\n"
            "Select OK to open NASA's official Delta-X dataset page. Download "
            "DeltaX_MultiSource_DEM_Atchafalaya-Terrebonne_Basin_2021_V1.tif, then return to "
            "AutoCF and choose that local COG as the user raster.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if answer == QMessageBox.StandardButton.Ok:
            QDesktopServices.openUrl(QUrl(NASA_DELTAX_DEM_PAGE))

    def terrain_targets(self) -> list[Path]:
        run = (
            self.data_dir("terrain", "rasters")
            if self.uses_organized_run_layout() else self.data_dir("terrain")
        )
        candidates = sorted(run.glob("topobathy_[0-9]*_*.tif"))
        candidates.extend((
            run / "user_topobathy_primary.tif", run / "cudem_1_9arcsec_model_crs.tif",
            run / "user_topobathy_gapfill.tif", run / "gebco_2024_model_crs.tif",
        ))
        return [path for path in dict.fromkeys(candidates) if path.is_file()]

    def show_terrain_files(self) -> None:
        prepared = self.terrain_targets()
        if not prepared:
            folder = (
                self.data_dir("terrain", "rasters")
                if self.uses_organized_run_layout() else self.data_dir("terrain")
            )
            self.terrain_status.setText(f"No prepared elevation rasters yet under {folder}.")
            return
        lines = [f"{path.name} — {human_size(path.stat().st_size)}" for path in prepared]
        self.terrain_status.setText("Prepared: " + "; ".join(lines))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(prepared[0].parent)))

    def run_terrain(self) -> None:
        # Capture the current HUC/drawn draft immediately before preparation so
        # terrain cannot silently use an older GeoJSON selected earlier.
        self.study_map.fetch_draft(self._run_terrain_with_current_study)

    def _run_terrain_with_current_study(self, payload: Any) -> None:
        if payload and not self._save_study_geojson_payload(payload, str(self.study_geojson_default())):
            return
        self._start_terrain_command()

    def _start_terrain_command(self) -> None:
        common = self.config_args()
        if not common:
            return
        self.terrain_status.setText("Preparing elevation rasters…")
        self.start_commands(
            [self.powershell(["-Action", "Run", *common, "-Mode", "terrain", "-CpuThreads", str(self.cpu_threads.value())])],
            "> Prepare terrain / topobathy", job_kind="terrain")

    def _make_dem_group(self) -> QWidget:
        group = QGroupBox("Terrain preview")
        layout = QVBoxLayout(group)
        note = (
            "Shaded relief over the model domain, from the global Mapzen Terrarium elevation tiles. "
            "This is independent reference terrain for checking the domain and buffer — it is not the "
            "configured source above, which GDAL reads at build time.")
        self.add_info(layout, note, "Terrain preview")
        self.dem_prepared_show = QCheckBox("Show prepared topobathy")
        self.dem_prepared_show.setToolTip("Overlay the elevation raster produced by 'Load terrain / topobathy'.")
        self.dem_prepared_show.setEnabled(False)
        self.dem_prepared_show.toggled.connect(self.prepared_dem_toggled)
        layout.addWidget(self.dem_prepared_show)
        opacity_row = QHBoxLayout()
        self.dem_image_opacity = QDoubleSpinBox()
        self.dem_image_opacity.setRange(0.1, 1.0); self.dem_image_opacity.setSingleStep(0.05)
        self.dem_image_opacity.setValue(0.85)
        self.dem_image_opacity.valueChanged.connect(lambda value: self.study_map.set_dem_image_opacity(value))
        zoom = QPushButton("Zoom to topobathy")
        zoom.clicked.connect(lambda: self.study_map.fit_dem_image())
        opacity_row.addWidget(QLabel("Overlay opacity"))
        opacity_row.addWidget(self.dem_image_opacity)
        opacity_row.addWidget(zoom)
        opacity_row.addStretch(1)
        layout.addLayout(opacity_row)
        self.dem_show = QCheckBox("Show shaded relief (global reference terrain)")
        self.dem_show.toggled.connect(self.dem_toggled)
        layout.addWidget(self.dem_show)
        form = QFormLayout()
        self.dem_strength = QDoubleSpinBox()
        self.dem_strength.setRange(0.1, 1.0); self.dem_strength.setSingleStep(0.1); self.dem_strength.setValue(0.5)
        self.dem_strength.valueChanged.connect(lambda value: self.study_map.set_dem_opacity(value))
        self.dem_3d = QCheckBox("3D terrain")
        self.dem_3d.toggled.connect(self.dem_toggled)
        self.dem_exaggeration = QDoubleSpinBox()
        self.dem_exaggeration.setRange(0.5, 8.0); self.dem_exaggeration.setSingleStep(0.5); self.dem_exaggeration.setValue(1.5)
        self.dem_exaggeration.setSuffix(" ×")
        self.dem_exaggeration.valueChanged.connect(self.dem_toggled)
        form.addRow("Relief strength", self.dem_strength)
        form.addRow(self.dem_3d)
        form.addRow("Vertical exaggeration", self.dem_exaggeration)
        layout.addLayout(form)
        self.dem_extent_show = QCheckBox("Outline the topobathy download extent")
        self.dem_extent_show.setChecked(False)
        self.dem_extent_show.setToolTip("The domain bounds expanded by the topobathy buffer — the area the build downloads elevation for.")
        self.dem_extent_show.toggled.connect(self.update_dem_extent)
        layout.addWidget(self.dem_extent_show)
        row = QHBoxLayout()
        sample = QPushButton("Sample elevation at domain centre")
        sample.clicked.connect(self.sample_elevation)
        row.addWidget(sample)
        row.addStretch(1)
        layout.addLayout(row)
        self.dem_status = QLabel("Shaded relief is off.")
        self.dem_status.setWordWrap(True)
        layout.addWidget(self.dem_status)
        self.topo_buffer.valueChanged.connect(self.update_dem_extent)
        return group

    def _make_surface_page(self) -> QWidget:
        surface_tabs = QTabWidget()
        controls = QWidget()
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        adaptation_page = QWidget()
        adaptation_page_layout = QVBoxLayout(adaptation_page)
        adaptation_page_layout.setContentsMargins(0, 0, 0, 0)
        land = QGroupBox("Land cover and roughness")
        form = QFormLayout(land)
        self.landcover_source = QComboBox(); self.landcover_source.setEditable(True); self.landcover_source.addItems(["ESA_WorldCover_2021_v200", "annual_nlcd", "file"])
        self.landcover_resolution = QDoubleSpinBox(); self.landcover_resolution.setRange(1, 1000); self.landcover_resolution.setSuffix(" m")
        self.landcover_source.currentTextChanged.connect(self.sync_landcover_source)
        self.manning_mapping = QComboBox()
        self.manning_mapping.addItem("Automatic — AutoCF defaults", "default_esa_worldcover")
        self.manning_mapping.addItem("Manual — values by LULC class", "manual_lulc_classes")
        self.manning_mapping.currentIndexChanged.connect(self.sync_manning_mapping)
        form.addRow("Land-cover source", self.landcover_source)
        form.addRow("Resolution", self.landcover_resolution)
        form.addRow("Manning lookup", self.manning_mapping)
        layout.addWidget(land)
        self.manual_manning_panel = QGroupBox("Manual LULC-class roughness (optional)")
        manual_layout = QVBoxLayout(self.manual_manning_panel)
        manual_note = (
            "Use this only when you want to assign Manning n yourself. Class codes must match the selected "
            "land-cover raster. Automatic mode keeps these values but does not apply them.")
        self.add_info(manual_layout, manual_note, "Manual roughness")
        self.manual_manning_table = QTableWidget(0, 3)
        self.manual_manning_table.setHorizontalHeaderLabels(["LULC class code", "Class name", "Manning n"])
        self.manual_manning_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.manual_manning_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.manual_manning_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.manual_manning_table.setMinimumHeight(245)
        self.manual_manning_table.cellChanged.connect(self.manual_manning_changed)
        manual_layout.addWidget(self.manual_manning_table)
        manual_buttons = QHBoxLayout()
        add_class = QPushButton("Add class")
        add_class.clicked.connect(self.add_manual_manning_row)
        remove_class = QPushButton("Remove selected")
        remove_class.clicked.connect(self.remove_manual_manning_rows)
        reset_classes = QPushButton("Load dataset defaults")
        reset_classes.clicked.connect(self.reset_manual_manning_rows)
        manual_buttons.addWidget(add_class)
        manual_buttons.addWidget(remove_class)
        manual_buttons.addWidget(reset_classes)
        manual_buttons.addStretch(1)
        manual_layout.addLayout(manual_buttons)
        self.set_manual_manning_rows(DEFAULT_ESA_MANNING_ROWS)
        self.manual_manning_panel.setVisible(False)
        layout.addWidget(self.manual_manning_panel)
        adaptations = QGroupBox("NBS / flood-adaptation polygons (optional)")
        adaptation_layout = QVBoxLayout(adaptations)
        adaptation_note = (
            "The selected dataset supplies base LULC roughness. Draw optional intervention polygons to override it "
            "locally—for example wetlands, restored mangroves, floodplain vegetation, or another research scenario. "
            "Save the study-area GeoJSON below the map after drawing.")
        self.add_info(adaptation_layout, adaptation_note, "NBS and adaptations")
        adaptation_form = QFormLayout()
        self.adaptation_name = QLineEdit("Nature-based solution")
        self.adaptation_class_code = QSpinBox()
        self.adaptation_class_code.setRange(1, 2_000_000_000)
        self.adaptation_class_code.setValue(1001)
        self.adaptation_class_code.setToolTip("Use a code not present in the base LULC dataset.")
        self.adaptation_manning = QDoubleSpinBox()
        self.adaptation_manning.setDecimals(4)
        self.adaptation_manning.setRange(0.0001, 1.0)
        self.adaptation_manning.setSingleStep(0.005)
        self.adaptation_manning.setValue(0.070)
        adaptation_form.addRow("Intervention name", self.adaptation_name)
        adaptation_form.addRow("Override class code", self.adaptation_class_code)
        adaptation_form.addRow("Manning n", self.adaptation_manning)
        adaptation_layout.addLayout(adaptation_form)
        adaptation_buttons = QHBoxLayout()
        draw_adaptation = QPushButton("Draw intervention polygon")
        draw_adaptation.setObjectName("primary")
        draw_adaptation.clicked.connect(self.start_roughness_override)
        finish_adaptation = QPushButton("Finish polygon")
        finish_adaptation.clicked.connect(self.study_map.finish_shape)
        stop_adaptation = QPushButton("Stop drawing")
        stop_adaptation.clicked.connect(lambda: self.study_map.set_mode("none"))
        clear_adaptations = QPushButton("Clear intervention polygons")
        clear_adaptations.clicked.connect(lambda: self.study_map.clear_role("roughness_override"))
        adaptation_buttons.addWidget(draw_adaptation)
        adaptation_buttons.addWidget(finish_adaptation)
        adaptation_buttons.addWidget(stop_adaptation)
        adaptation_buttons.addWidget(clear_adaptations)
        adaptation_buttons.addStretch(1)
        adaptation_layout.addLayout(adaptation_buttons)
        self.adaptation_status = QLabel("No NBS/adaptation polygons in the study-area draft.")
        self.adaptation_status.setWordWrap(True)
        adaptation_layout.addWidget(self.adaptation_status)
        adaptation_page_layout.addWidget(adaptations)
        adaptation_page_layout.addStretch(1)
        infiltration = QGroupBox("Rainfall infiltration")
        inf = QFormLayout(infiltration)
        self.infiltration_enabled = QCheckBox("Enable spatial infiltration")
        self.infiltration_method = QComboBox(); self.infiltration_method.addItems(["cn", "cn_with_recovery", "none"])
        self.infiltration_source = QComboBox(); self.infiltration_source.addItems(["gcn250", "file"])
        self.infiltration_file = QLineEdit()
        self.infiltration_file_row = self.path_row(
            self.infiltration_file,
            lambda: self.choose_file(
                self.infiltration_file, "GeoTIFF (*.tif *.tiff);;All files (*)"))
        self.antecedent = QComboBox(); self.antecedent.addItems(["dry", "avg", "wet"])
        self.effective = QDoubleSpinBox(); self.effective.setRange(0, 1); self.effective.setSingleStep(0.05); self.effective.setValue(0.5)
        self.soil_source = QComboBox(); self.soil_source.addItem("ssurgo_sda")
        inf.addRow(self.infiltration_enabled)
        self.infiltration_method_label = QLabel("Method")
        self.infiltration_source_label = QLabel("Curve-number source")
        self.infiltration_file_label = QLabel("Curve-number GeoTIFF")
        self.antecedent_label = QLabel("Antecedent moisture")
        self.effective_label = QLabel("Initial effective storage")
        inf.addRow(self.infiltration_method_label, self.infiltration_method)
        inf.addRow(self.infiltration_source_label, self.infiltration_source)
        inf.addRow(self.infiltration_file_label, self.infiltration_file_row)
        inf.addRow(self.antecedent_label, self.antecedent)
        inf.addRow(self.effective_label, self.effective)
        self.infiltration_method.currentTextChanged.connect(self.sync_infiltration_ui)
        self.infiltration_source.currentTextChanged.connect(self.sync_infiltration_ui)
        self.infiltration_enabled.toggled.connect(self.sync_infiltration_ui)
        self.sync_infiltration_ui()
        layout.addWidget(infiltration)
        prepare = QGroupBox("Prepare surface layers")
        prepare_layout = QVBoxLayout(prepare)
        note = (
            "Download and prepare the selected land-cover, Manning roughness, and infiltration inputs now. "
            "Each available raster is clipped to the Study Area AOI and then shown automatically on the map.")
        self.add_info(prepare_layout, note, "Prepare surface layers")
        row = QHBoxLayout()
        self.surface_button = QPushButton("Prepare roughness / infiltration")
        self.surface_button.setObjectName("primary")
        self.surface_button.clicked.connect(self.run_surface)
        row.addWidget(self.surface_button)
        row.addStretch(1)
        prepare_layout.addLayout(row)
        self.surface_status = QLabel("No prepared surface layers loaded.")
        self.surface_status.setWordWrap(True)
        prepare_layout.addWidget(self.surface_status)
        layout.addWidget(prepare)
        layout.addStretch(1)
        surface_tabs.addTab(self.scroll(controls), "Base roughness && infiltration")
        surface_tabs.addTab(self.scroll(adaptation_page), "NBS && adaptations")
        surface_tabs.currentChanged.connect(lambda _: self.study_map.set_mode("none"))
        self.connect_dirty([self.landcover_source, self.landcover_resolution, self.manning_mapping,
                            self.infiltration_enabled, self.infiltration_method, self.infiltration_source,
                            self.infiltration_file, self.antecedent, self.effective, self.soil_source])

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(surface_tabs, 1)
        left.setMinimumWidth(360)
        right = self.map_host("surface")
        right.setMinimumWidth(380)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 840])
        page, page_layout = self.page(
            "Roughness & Infiltration",
            "Configure surface inputs and inspect every prepared layer inside the Study Area AOI.")
        page_layout.addWidget(splitter, 1)
        return page

    def sync_infiltration_ui(self, *_: Any) -> None:
        method = self.infiltration_method.currentText().strip().lower()
        current_source = self.infiltration_source.currentText().strip().lower()
        wanted_sources = ["nlcd_ssurgo"] if method == "cn_with_recovery" else ["gcn250", "file"]
        if [self.infiltration_source.itemText(index) for index in range(self.infiltration_source.count())] != wanted_sources:
            self.infiltration_source.blockSignals(True)
            self.infiltration_source.clear()
            self.infiltration_source.addItems(wanted_sources)
            self.infiltration_source.setCurrentText(
                current_source if current_source in wanted_sources else wanted_sources[0])
            self.infiltration_source.blockSignals(False)
        source = self.infiltration_source.currentText().strip().lower()
        ordinary_cn = method == "cn"
        recovery = method == "cn_with_recovery"
        self.infiltration_source_label.setText(
            "Land-cover + soils source" if recovery else "Curve-number source")
        for widget in (self.infiltration_source_label, self.infiltration_source):
            widget.setVisible(ordinary_cn or recovery)
        self.infiltration_source.setEnabled(ordinary_cn and self.infiltration_enabled.isChecked())
        for widget in (self.infiltration_file_label, self.infiltration_file_row):
            widget.setVisible(ordinary_cn and source == "file")
            widget.setEnabled(self.infiltration_enabled.isChecked())
        for widget in (self.antecedent_label, self.antecedent):
            widget.setVisible(ordinary_cn)
            widget.setEnabled(self.infiltration_enabled.isChecked())
        for widget in (self.effective_label, self.effective):
            widget.setVisible(recovery)
            widget.setEnabled(self.infiltration_enabled.isChecked())
        if not self.loading and hasattr(self, "run_summary"):
            self.mark_dirty()

    def sync_manning_mapping(self, *_: Any) -> None:
        manual = self.manning_mapping.currentData() == "manual_lulc_classes"
        self.manual_manning_panel.setVisible(manual)
        if not self.loading:
            self.mark_dirty()

    def sync_landcover_source(self, *_: Any) -> None:
        dataset = LANDCOVER_DATASETS.get(self.landcover_source.currentText().strip().upper(), {})
        if dataset.get("resolution_m") is not None:
            self.landcover_resolution.setValue(float(dataset["resolution_m"]))
        self.landcover_resolution.setReadOnly(bool(dataset))
        if not self.loading:
            self.mark_dirty()

    def set_manual_manning_rows(self, rows: Iterable[Any]) -> None:
        self.manual_manning_table.blockSignals(True)
        self.manual_manning_table.setRowCount(0)
        for record in rows:
            if isinstance(record, dict):
                code = record.get("code", record.get("class"))
                name = record.get("name", record.get("description", ""))
                value = record.get("n", record.get("N"))
            else:
                try:
                    code, name, value = record
                except Exception:
                    continue
            row = self.manual_manning_table.rowCount()
            self.manual_manning_table.insertRow(row)
            self.manual_manning_table.setItem(row, 0, QTableWidgetItem(str(code)))
            self.manual_manning_table.setItem(row, 1, QTableWidgetItem(str(name)))
            self.manual_manning_table.setItem(row, 2, QTableWidgetItem(f"{float(value):.4f}"))
        self.manual_manning_table.blockSignals(False)

    def manual_manning_rows(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        codes: set[int] = set()
        for row in range(self.manual_manning_table.rowCount()):
            texts = [self.manual_manning_table.item(row, column).text().strip()
                     if self.manual_manning_table.item(row, column) else "" for column in range(3)]
            if not any(texts):
                continue
            try:
                code = int(texts[0])
            except ValueError as exc:
                raise ValueError(f"Manual Manning row {row + 1}: LULC class code must be an integer.") from exc
            if code < 0:
                raise ValueError(f"Manual Manning row {row + 1}: LULC class code must be non-negative.")
            try:
                value = float(texts[2])
            except ValueError as exc:
                raise ValueError(f"Manual Manning row {row + 1}: Manning n must be a number.") from exc
            if code in codes:
                raise ValueError(f"Manual Manning class code {code} is repeated.")
            if not (0.0 < value <= 1.0):
                raise ValueError(f"Manual Manning class {code}: Manning n must be greater than 0 and at most 1.")
            codes.add(code)
            records.append({"code": code, "name": texts[1] or f"Class {code}", "n": value})
        if not records:
            raise ValueError("Manual Manning mode needs at least one LULC class.")
        return records

    def manual_manning_changed(self, *_: Any) -> None:
        if not self.loading:
            self.mark_dirty()

    def add_manual_manning_row(self) -> None:
        row = self.manual_manning_table.rowCount()
        self.manual_manning_table.insertRow(row)
        self.manual_manning_table.setItem(row, 0, QTableWidgetItem(""))
        self.manual_manning_table.setItem(row, 1, QTableWidgetItem(""))
        self.manual_manning_table.setItem(row, 2, QTableWidgetItem("0.0400"))
        self.manual_manning_table.setCurrentCell(row, 0)
        self.manual_manning_table.editItem(self.manual_manning_table.item(row, 0))
        self.mark_dirty()

    def remove_manual_manning_rows(self) -> None:
        rows = sorted({index.row() for index in self.manual_manning_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.manual_manning_table.removeRow(row)
        if rows:
            self.mark_dirty()

    def reset_manual_manning_rows(self) -> None:
        rows = DEFAULT_NLCD_MANNING_ROWS if "nlcd" in self.landcover_source.currentText().lower() else DEFAULT_ESA_MANNING_ROWS
        self.set_manual_manning_rows(rows)
        self.mark_dirty()

    def start_roughness_override(self) -> None:
        name = self.adaptation_name.text().strip() or "Adaptation"
        properties = {
            "name": name,
            "source": "user_drawn_adaptation",
            "lulc_code": int(self.adaptation_class_code.value()),
            "manning_n": float(self.adaptation_manning.value()),
        }
        self.study_map.set_override_properties(properties)
        self.study_map.set_mode("roughness_override")
        self.adaptation_status.setText(
            f"Drawing {name}: class {properties['lulc_code']}, Manning n={properties['manning_n']:.4f}. "
            "Click polygon corners, then finish the polygon.")

    def _make_forcing_page(self) -> QWidget:
        page, layout = self.page("Forcing", "Prepare atmospheric, ocean-boundary, and river forcing before the model build.")
        self.forcing_tabs = QTabWidget()

        atmosphere = QWidget(); atmosphere_layout = QVBoxLayout(atmosphere)
        atmosphere_intro = (
            "Select one provider for each atmospheric component. AutoCF downloads/subsets the data, "
            "standardizes units and time labels, checks the AOI and simulation period, and prepares map previews."
        )
        self.add_info(atmosphere_layout, atmosphere_intro, "Atmospheric forcing")
        self.atmosphere_panels = QVBoxLayout()

        rain = QGroupBox("Rainfall"); rain_form = QFormLayout(rain)
        self.rain_source = QComboBox(); self.rain_source.setEditable(True); self.rain_source.addItems(["aorc", "mrms", "era5", "file", "none"])
        rain_form.addRow("Precipitation source", self.rain_source)
        self.rain_file = QLineEdit(); self.rain_browse = QPushButton("Browse…")
        rain_file_row = QWidget(); rain_file_layout = QHBoxLayout(rain_file_row); rain_file_layout.setContentsMargins(0, 0, 0, 0)
        rain_file_layout.addWidget(self.rain_file, 1); rain_file_layout.addWidget(self.rain_browse)
        self.rain_browse.clicked.connect(lambda: self.choose_atmosphere_file(self.rain_file, "rainfall"))
        rain_form.addRow("Local NetCDF", rain_file_row)
        self.add_info(rain_form, "Prepared as precipitation rate in mm/h. AORC is downloaded automatically.", "Rainfall")
        self.rain_view = QPushButton("Show rainfall on map")
        self.rain_view.clicked.connect(lambda: self.show_atmosphere_layer("rainfall"))
        rain_form.addRow(self.rain_view)

        wind = QGroupBox("Wind"); wind_form = QFormLayout(wind)
        self.wind_source = QComboBox(); self.wind_source.setEditable(True); self.wind_source.addItems(["aorc", "era5", "file", "none"])
        wind_form.addRow("Wind source", self.wind_source)
        self.wind_file = QLineEdit(); self.wind_browse = QPushButton("Browse…")
        wind_file_row = QWidget(); wind_file_layout = QHBoxLayout(wind_file_row); wind_file_layout.setContentsMargins(0, 0, 0, 0)
        wind_file_layout.addWidget(self.wind_file, 1); wind_file_layout.addWidget(self.wind_browse)
        self.wind_browse.clicked.connect(lambda: self.choose_atmosphere_file(self.wind_file, "wind"))
        wind_form.addRow("Local NetCDF", wind_file_row)
        self.add_info(wind_form, "Prepared as 10 m u/v wind in m/s; the map shows speed and direction arrows.", "Wind")
        self.wind_view = QPushButton("Show wind on map")
        self.wind_view.clicked.connect(lambda: self.show_atmosphere_layer("wind"))
        wind_form.addRow(self.wind_view)

        pressure = QGroupBox("Pressure"); pressure_form = QFormLayout(pressure)
        self.pressure_source = QComboBox(); self.pressure_source.setEditable(True); self.pressure_source.addItems(["aorc", "era5", "file", "none"])
        pressure_form.addRow("Pressure source", self.pressure_source)
        self.pressure_file = QLineEdit(); self.pressure_browse = QPushButton("Browse…")
        pressure_file_row = QWidget(); pressure_file_layout = QHBoxLayout(pressure_file_row); pressure_file_layout.setContentsMargins(0, 0, 0, 0)
        pressure_file_layout.addWidget(self.pressure_file, 1); pressure_file_layout.addWidget(self.pressure_browse)
        self.pressure_browse.clicked.connect(lambda: self.choose_atmosphere_file(self.pressure_file, "pressure"))
        pressure_form.addRow("Local NetCDF", pressure_file_row)
        self.add_info(pressure_form, "Prepared and validated as barometric pressure in Pa; displayed on the map in hPa.", "Pressure")
        self.pressure_view = QPushButton("Show pressure on map")
        self.pressure_view.clicked.connect(lambda: self.show_atmosphere_layer("pressure"))
        pressure_form.addRow(self.pressure_view)

        self.atmosphere_panels.addWidget(rain)
        self.atmosphere_panels.addWidget(wind)
        self.atmosphere_panels.addWidget(pressure)
        atmosphere_layout.addLayout(self.atmosphere_panels)
        self.atmosphere_prepare = QPushButton("Prepare atmosphere forcing")
        self.atmosphere_prepare.setObjectName("primary")
        self.atmosphere_prepare.clicked.connect(self.prepare_atmosphere_forcing)
        self.atmosphere_status = QLabel(
            "Not prepared yet. This step is independent of the terrain/model build and does not run SFINCS."
        )
        self.atmosphere_status.setWordWrap(True)
        atmosphere_layout.addWidget(self.atmosphere_prepare)
        atmosphere_layout.addWidget(self.atmosphere_status)
        atmosphere_layout.addStretch(1)

        ocean = QWidget(); ocean_layout = QVBoxLayout(ocean)
        ocean_box = QGroupBox("Ocean boundary provider"); oc = QFormLayout(ocean_box)
        self.ocean_provider_form = oc
        self.ocean_source = QComboBox(); self.ocean_source.setEditable(True); self.ocean_source.addItems(["noaa_coops", "selected_stations", "gtsm", "file", "none"])
        self.ocean_datum = QComboBox(); self.ocean_datum.setEditable(True); self.ocean_datum.addItems(["MSL", "NAVD", "MLLW"])
        self.ocean_distance = QDoubleSpinBox(); self.ocean_distance.setRange(1, 2000); self.ocean_distance.setValue(150); self.ocean_distance.setSuffix(" km")
        oc.addRow("Ocean boundary", self.ocean_source); oc.addRow("Provider datum", self.ocean_datum); oc.addRow("Station distance", self.ocean_distance)
        ocean_layout.addWidget(ocean_box)
        self.ocean_geometry_note = self.add_info(
            oc,
            "Draw the ocean boundary and select its water-level points in Study Area → Ocean boundary. "
            "This panel only controls the forcing provider, datum, and station-search distance.",
            "Ocean boundary provider")
        self.ocean_file_group = QGroupBox("User-defined ocean water-level file")
        ocean_file_layout = QVBoxLayout(self.ocean_file_group)
        ocean_file_form = QFormLayout()
        self.ocean_timeseries = QLineEdit()
        ocean_file_form.addRow("Time-series file", self.path_row(
            self.ocean_timeseries,
            lambda: self.choose_file(self.ocean_timeseries, "Point time series (*.csv *.nc *.nc4);;All files (*)")))
        self.ocean_file_layout = QComboBox()
        self.ocean_file_layout.addItem("Auto-detect", "auto")
        self.ocean_file_layout.addItem("Wide CSV — time plus one column per point", "wide")
        self.ocean_file_layout.addItem("Long CSV — time, station_id, water_level", "long")
        self.ocean_file_layout.addItem("CF NetCDF", "netcdf")
        ocean_file_form.addRow("Input layout", self.ocean_file_layout)
        self.ocean_file_units = QComboBox(); self.ocean_file_units.setEditable(True)
        self.ocean_file_units.addItems(["m", "ft", "cm", "mm"])
        ocean_file_form.addRow("Water-level units", self.ocean_file_units)
        self.ocean_file_timezone = QComboBox(); self.ocean_file_timezone.setEditable(True)
        self.ocean_file_timezone.addItems(["UTC", "GMT"])
        ocean_file_form.addRow("Time zone", self.ocean_file_timezone)
        self.ocean_file_datum = QComboBox(); self.ocean_file_datum.setEditable(True)
        self.ocean_file_datum.addItems(["ODN", "NAVD88", "MSL", "LAT", "MLLW", "ACD", "EGM2008", "EGM96", "Local/station datum"])
        ocean_file_form.addRow("Source water-level datum", self.ocean_file_datum)
        self.ocean_datum_policy = QComboBox()
        self.ocean_datum_policy.addItem("Values already use the model target datum", "match")
        self.ocean_datum_policy.addItem("Convert using a documented constant offset", "offset")
        self.ocean_datum_policy.addItem("Unknown / undocumented — cannot build", "unknown")
        ocean_file_form.addRow("Datum relationship", self.ocean_datum_policy)
        self.ocean_datum_offset = QDoubleSpinBox()
        self.ocean_datum_offset.setRange(-1000.0, 1000.0); self.ocean_datum_offset.setDecimals(4)
        self.ocean_datum_offset.setSuffix(" m")
        ocean_file_form.addRow("Add to supplied levels", self.ocean_datum_offset)
        ocean_file_layout.addLayout(ocean_file_form)
        ocean_file_layout.addWidget(self._make_coordinate_editor(
            "ocean", "ocean_forcing_point", "bnd",
            "Enter each boundary point in WGS84 coordinates. IDs must match the file's series columns or station_id values."))
        ocean_file_actions = QHBoxLayout()
        import_ocean_locations = QPushButton("Import point GeoJSON…")
        import_ocean_locations.clicked.connect(
            lambda: self.import_point_locations("ocean", "ocean_forcing_point"))
        ocean_template = QPushButton("Save CSV templates…")
        ocean_template.clicked.connect(lambda: self.save_forcing_template("ocean"))
        validate_ocean = QPushButton("Validate file and points")
        validate_ocean.clicked.connect(lambda: self.request_forcing_point_save(
            lambda: self.validate_file_forcing("ocean")))
        for button in (import_ocean_locations, ocean_template, validate_ocean):
            ocean_file_actions.addWidget(button)
        ocean_file_actions.addStretch(1)
        ocean_file_layout.addLayout(ocean_file_actions)
        self.ocean_file_status = QLabel("Select a time-series file and define matching boundary points.")
        self.ocean_file_status.setWordWrap(True); ocean_file_layout.addWidget(self.ocean_file_status)
        ocean_layout.addWidget(self.ocean_file_group)
        ocean_layout.addStretch(1)

        rivers = QWidget(); rivers_layout = QVBoxLayout(rivers)
        river_box = QGroupBox("River discharge provider"); rv = QFormLayout(river_box)
        self.river_provider_form = rv
        self.river_source = QComboBox(); self.river_source.setEditable(True); self.river_source.addItems(["glofas_auto", "usgs_auto", "file", "none"])
        self.river_distance = QDoubleSpinBox(); self.river_distance.setRange(1, 1000); self.river_distance.setValue(50); self.river_distance.setSuffix(" km")
        self.max_gages = QSpinBox(); self.max_gages.setRange(0, 100); self.max_gages.setValue(12)
        rv.addRow("River discharge", self.river_source); rv.addRow("Gauge distance", self.river_distance); rv.addRow("Maximum gauges", self.max_gages)
        self.glofas_terms_button = QPushButton("Open GloFAS licence management")
        self.glofas_terms_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(EWDS_GLOFAS_TERMS_URL)))
        rv.addRow("Copernicus access", self.glofas_terms_button)
        rivers_layout.addWidget(river_box)
        self.river_points_group = self._make_river_group()
        rivers_layout.addWidget(self.river_points_group)
        self.river_file_group = QGroupBox("User-defined river-discharge file")
        river_file_layout = QVBoxLayout(self.river_file_group)
        river_file_form = QFormLayout()
        self.river_timeseries = QLineEdit()
        river_file_form.addRow("Time-series file", self.path_row(
            self.river_timeseries,
            lambda: self.choose_file(self.river_timeseries, "Point time series (*.csv *.nc *.nc4);;All files (*)")))
        self.river_file_layout = QComboBox()
        self.river_file_layout.addItem("Auto-detect", "auto")
        self.river_file_layout.addItem("Wide CSV — time plus one column per source", "wide")
        self.river_file_layout.addItem("Long CSV — time, source_id, discharge", "long")
        self.river_file_layout.addItem("CF NetCDF", "netcdf")
        river_file_form.addRow("Input layout", self.river_file_layout)
        self.river_file_units = QComboBox(); self.river_file_units.setEditable(True)
        self.river_file_units.addItems(["m3/s", "ft3/s", "L/s"])
        river_file_form.addRow("Discharge units", self.river_file_units)
        self.river_file_timezone = QComboBox(); self.river_file_timezone.setEditable(True)
        self.river_file_timezone.addItems(["UTC", "GMT"])
        river_file_form.addRow("Time zone", self.river_file_timezone)
        river_file_layout.addLayout(river_file_form)
        river_file_actions = QHBoxLayout()
        import_river_locations = QPushButton("Import source GeoJSON…")
        import_river_locations.clicked.connect(
            lambda: self.import_point_locations("river", "river_source"))
        river_template = QPushButton("Save CSV templates…")
        river_template.clicked.connect(lambda: self.save_forcing_template("river"))
        validate_river = QPushButton("Validate file and points")
        validate_river.clicked.connect(lambda: self.request_forcing_point_save(
            lambda: self.validate_file_forcing("river")))
        for button in (import_river_locations, river_template, validate_river):
            river_file_actions.addWidget(button)
        river_file_actions.addStretch(1)
        river_file_layout.addLayout(river_file_actions)
        self.river_file_status = QLabel("Select a discharge file and define matching river-source points above.")
        self.river_file_status.setWordWrap(True); river_file_layout.addWidget(self.river_file_status)
        rivers_layout.addWidget(self.river_file_group)
        rivers_layout.addStretch(1)

        self.forcing_tabs.addTab(self.scroll(atmosphere), "Atmosphere")
        self.forcing_tabs.addTab(self.scroll(ocean), "Ocean")
        self.forcing_tabs.addTab(self.scroll(rivers), "Rivers")
        self.forcing_tabs.currentChanged.connect(self.forcing_tab_changed)
        self.rain_source.currentTextChanged.connect(self.sync_atmosphere_sources)
        self.wind_source.currentTextChanged.connect(self.sync_atmosphere_sources)
        self.pressure_source.currentTextChanged.connect(self.sync_atmosphere_sources)
        self.ocean_source.currentTextChanged.connect(self.sync_ocean_source_ui)
        self.river_source.currentTextChanged.connect(self.sync_river_source_ui)
        self.ocean_datum_policy.currentIndexChanged.connect(self.sync_ocean_source_ui)
        self.sync_atmosphere_sources()
        self.sync_ocean_source_ui()
        self.sync_river_source_ui()

        left = QWidget(); left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.forcing_tabs, 1)
        left.setMinimumWidth(360)
        right = self.map_host("forcing"); right.setMinimumWidth(380)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left); splitter.addWidget(right)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 840])
        layout.addWidget(splitter, 1)
        self.connect_dirty([self.rain_source, self.rain_file, self.wind_source, self.wind_file,
                            self.pressure_source, self.pressure_file, self.ocean_source, self.ocean_datum,
                            self.ocean_distance, self.ocean_timeseries, self.ocean_file_layout,
                            self.ocean_file_units, self.ocean_file_timezone, self.ocean_file_datum,
                            self.ocean_datum_policy, self.ocean_datum_offset,
                            self.river_source, self.river_distance, self.max_gages,
                            self.river_timeseries, self.river_file_layout,
                            self.river_file_units, self.river_file_timezone])
        return page

    def _make_sfincs_page(self) -> QWidget:
        page, layout = self.page(
            "SFINCS Model",
            "Choose how SFINCS represents hydraulics on the computational grid and inspect exact sfincs.inp parameters.",
        )
        tabs = QTabWidget()
        basic = QWidget(); form = QFormLayout(basic)
        self.model_mode = QComboBox()
        self.model_mode.addItem("Regular — cell-average terrain", "regular")
        self.model_mode.addItem("Subgrid — fine terrain within each cell", "subgrid")
        self.model_mode_help = self.add_info(
            form,
            "Both choices use the same regular computational grid and the cell size selected on Study Area. "
            "Subgrid adds fine terrain sampling inside each computational cell; it is not a second mesh type.",
            "Hydraulic representation")
        self.sfincs_input = QLineEdit("sfincs.inp")
        self.input_format = QComboBox(); self.input_format.addItems(["bin", "net"])
        self.output_format = QComboBox(); self.output_format.addItems(["net", "bin"])
        self.dtout = QDoubleSpinBox(); self.dthisout = QDoubleSpinBox(); self.dtmaxout = QDoubleSpinBox()
        for widget in (self.dtout, self.dthisout, self.dtmaxout):
            widget.setRange(0, 1_000_000); widget.setSuffix(" s")
        self.dtout.setValue(3600); self.dthisout.setValue(360); self.dtmaxout.setValue(3600)
        self.subgrid_levels, self.subgrid_pixels = QSpinBox(), QSpinBox()
        self.subgrid_levels.setRange(2, 100); self.subgrid_pixels.setRange(2, 100)
        self.subgrid_levels.setValue(10); self.subgrid_pixels.setValue(20)
        form.addRow("Hydraulic representation", self.model_mode)
        form.addRow("Input file", self.sfincs_input)
        form.addRow("Input format", self.input_format); form.addRow("Output format", self.output_format)
        form.addRow("Map output interval", self.dtout); form.addRow("History output interval", self.dthisout)
        form.addRow("Maximum-field interval", self.dtmaxout); form.addRow("Subgrid levels", self.subgrid_levels)
        form.addRow("Subgrid pixels/cell", self.subgrid_pixels)
        tabs.addTab(basic, "Basic")
        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        self.advanced_table = QTableWidget(len(SFINCS_PARAMETERS), 5)
        self.advanced_table.setHorizontalHeaderLabels(["sfincs.inp keyword", "Value", "Units", "Default", "Meaning"])
        for index in range(4): self.advanced_table.horizontalHeader().setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        self.advanced_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for row, (key, label, kind, default, units, meaning) in enumerate(SFINCS_PARAMETERS):
            item = QTableWidgetItem(key); item.setToolTip(label); self.advanced_table.setItem(row, 0, item)
            if kind == "choice":
                editor = QComboBox(); editor.addItems(["0", "1"]); editor.setCurrentText(str(default)); editor.currentTextChanged.connect(self.sfincs_changed)
            else:
                editor = QDoubleSpinBox(); editor.setDecimals(6); editor.setRange(-1_000_000, 1_000_000); editor.setValue(float(default)); editor.valueChanged.connect(self.sfincs_changed)
            self.advanced_widgets[key] = editor
            self.advanced_table.setCellWidget(row, 1, editor)
            self.advanced_table.setItem(row, 2, QTableWidgetItem(units)); self.advanced_table.setItem(row, 3, QTableWidgetItem(str(default))); self.advanced_table.setItem(row, 4, QTableWidgetItem(meaning))
        advanced_layout.addWidget(self.advanced_table)
        reset = QPushButton("Reset advanced values to SFINCS defaults"); reset.clicked.connect(self.reset_sfincs_defaults); advanced_layout.addWidget(reset)
        tabs.addTab(advanced, "Advanced sfincs.inp")
        preview = QWidget(); preview_layout = QVBoxLayout(preview)
        self.sfincs_preview = QTextEdit(); self.sfincs_preview.setReadOnly(True); self.sfincs_preview.setFont(QFont("Cascadia Mono", 10))
        self.add_info(preview_layout, "Generated preview; HydroMT adds resolved file paths during build.", "Generated sfincs.inp")
        preview_layout.addWidget(self.sfincs_preview)
        tabs.addTab(preview, "Generated sfincs.inp")
        tabs.currentChanged.connect(lambda _: self.update_sfincs_preview())
        layout.addWidget(tabs, 1)
        self.model_mode.currentIndexChanged.connect(self.sync_model_mode_ui)
        self.connect_sfincs([self.model_mode, self.sfincs_input, self.input_format, self.output_format, self.dtout, self.dthisout, self.dtmaxout, self.subgrid_levels, self.subgrid_pixels])
        self.sync_model_mode_ui()
        return page

    def sync_model_mode_ui(self, *_: Any) -> None:
        """Make it clear that subgrid parameters apply only to subgrid hydraulics."""
        subgrid = self.model_mode.currentData() == "subgrid"
        self.subgrid_levels.setEnabled(subgrid)
        self.subgrid_pixels.setEnabled(subgrid)

    def _make_run_page(self) -> QWidget:
        page, layout = self.page("Build & Run", "Build or run with the bundled SFINCS CPU (OpenMP) backend.")
        group = QGroupBox("Operation"); form = QFormLayout(group)
        self.run_operation = QComboBox()
        self.run_operation.addItem("Build project only (CPU)", "build")
        self.run_operation.addItem("Run project — build/update if needed (CPU)", "run")
        self.run_summary = QLabel(); self.run_summary.setWordWrap(True)
        form.addRow("Operation", self.run_operation); form.addRow("Configuration", self.run_summary)
        layout.addWidget(group)
        actions = QHBoxLayout()
        status = QPushButton("System status")
        status.clicked.connect(self.system_status)
        self.run_button = QPushButton("Run AutoCF"); self.run_button.setObjectName("primary"); self.run_button.clicked.connect(self.run_model)
        actions.addWidget(status); actions.addStretch(1); actions.addWidget(self.run_button)
        layout.addLayout(actions)
        self.add_info(
            layout,
            "Before running\n\nSave YAML changes.\nValidate the domain, dates, and SFINCS dependencies.\n"
            "Build requires internet for remote inputs.\nSimulation uses CPU; Docker and NVIDIA are not required.",
            "Before running")
        layout.addStretch(1)
        return self.scroll(page)

    def _make_evaluation_page(self) -> QWidget:
        page, layout = self.page("Evaluation", "Generate flood-depth products and compare model water levels with observations.")
        self.evaluation_tabs = QTabWidget()
        self.evaluation_tabs.setUsesScrollButtons(True)
        self.evaluation_single_plot_widgets: list[QWidget] = []
        setup_page = QWidget(); setup_layout = QVBoxLayout(setup_page)
        group = QGroupBox("Evaluation products"); form = QFormLayout(group)
        self.eval_hmax = QCheckBox("Create flood-depth map and GeoTIFF"); self.eval_hmax.setChecked(True)
        self.eval_observations = QCheckBox("Create gauge plots and error metrics"); self.eval_observations.setChecked(True)
        self.eval_tolerance = QDoubleSpinBox(); self.eval_tolerance.setRange(0.1, 120); self.eval_tolerance.setValue(4); self.eval_tolerance.setSuffix(" min")
        self.eval_vmax = QDoubleSpinBox(); self.eval_vmax.setRange(0.1, 100); self.eval_vmax.setValue(3); self.eval_vmax.setSuffix(" m")
        form.addRow(self.eval_hmax); form.addRow(self.eval_observations); form.addRow("Gauge tolerance", self.eval_tolerance); form.addRow("Map color maximum", self.eval_vmax)
        setup_layout.addWidget(group)
        actions = QHBoxLayout()
        run, both, plots = QPushButton("Run evaluation"), QPushButton("Run evaluation + impact"), QPushButton("View plots")
        run.setObjectName("primary"); run.clicked.connect(self.run_evaluation); both.clicked.connect(self.run_evaluation_and_impact)
        plots.clicked.connect(lambda: (self.refresh_evaluation_plots(True), self.evaluation_tabs.setCurrentIndex(1)))
        actions.addWidget(run); actions.addWidget(both); actions.addWidget(plots); actions.addStretch(1)
        setup_layout.addLayout(actions); setup_layout.addStretch(1)

        plots_page = QWidget(); plots_layout = QVBoxLayout(plots_page)
        plot_actions = QHBoxLayout(); refresh_plots = QPushButton("Refresh plots"); refresh_plots.clicked.connect(lambda: self.refresh_evaluation_plots(True))
        self.evaluation_plot_status = QLabel("Run evaluation to create the maximum-water-level and gauge-comparison plots.")
        self.evaluation_plot_status.setWordWrap(True)
        plot_actions.addWidget(refresh_plots); plot_actions.addWidget(self.evaluation_plot_status, 1); plots_layout.addLayout(plot_actions)
        plot_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.evaluation_plot_list = QListWidget(); self.evaluation_plot_list.setMinimumWidth(280)
        self.evaluation_plot_list.currentItemChanged.connect(self.evaluation_plot_selected)
        plot_scroll = QScrollArea(); plot_scroll.setWidgetResizable(True)
        self.evaluation_plot_image = FittedPixmapLabel("No evaluation plots are available yet.")
        plot_scroll.setWidget(self.evaluation_plot_image)
        plot_splitter.addWidget(self.evaluation_plot_list); plot_splitter.addWidget(plot_scroll); plot_splitter.setStretchFactor(1, 1)
        plots_layout.addWidget(plot_splitter, 1)

        self.evaluation_tabs.addTab(setup_page, "Run")
        self.evaluation_tabs.addTab(plots_page, "Plots")
        self.evaluation_tabs.currentChanged.connect(lambda index: self.refresh_evaluation_plots() if index == 1 else None)
        layout.addWidget(self.evaluation_tabs, 1)
        return page

    def _make_impact_page(self) -> QWidget:
        page, layout = self.page(
            "Attribution & Analysis",
            "Analyze the active completed model with coastal, precipitation, and river counterfactuals.")
        self.analysis_tabs = QTabWidget()
        self.analysis_tabs.setUsesScrollButtons(True)

        # Status ---------------------------------------------------------
        status_page = QWidget(); status_layout = QVBoxLayout(status_page)
        status_intro = (
            "This workspace analyzes the model currently loaded in the toolbar. "
            "It does not create or switch models.")
        self.add_info(status_layout, status_intro, "Attribution and analysis")
        self.analysis_state_banner = QLabel("No model loaded")
        self.analysis_state_banner.setObjectName("analysisState")
        status_layout.addWidget(self.analysis_state_banner)
        model_group = QGroupBox("Active model and analysis state")
        model_form = QFormLayout(model_group)
        self.analysis_yaml_status = QLabel("Not loaded")
        self.analysis_run_status = QLabel("Not available")
        self.analysis_parent_status = QLabel("Not available")
        self.analysis_prepared_status = QLabel("0 of 7 counterfactual folders")
        self.analysis_completed_status = QLabel("0 of 8 scenario outputs")
        self.analysis_report_status = QLabel("Not generated")
        for value in (
            self.analysis_yaml_status, self.analysis_run_status,
            self.analysis_parent_status, self.analysis_prepared_status,
            self.analysis_completed_status, self.analysis_report_status,
        ):
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        model_form.addRow("Model YAML", self.analysis_yaml_status)
        model_form.addRow("Parent run", self.analysis_run_status)
        model_form.addRow("Parent SFINCS result", self.analysis_parent_status)
        model_form.addRow("Prepared", self.analysis_prepared_status)
        model_form.addRow("Completed", self.analysis_completed_status)
        model_form.addRow("Report", self.analysis_report_status)
        status_layout.addWidget(model_group)
        status_actions = QHBoxLayout()
        load_model = QPushButton("Load a different model YAML…")
        refresh_status = QPushButton("Refresh status")
        open_run = QPushButton("Open parent run folder")
        load_model.clicked.connect(self.choose_config)
        refresh_status.clicked.connect(self.refresh_analysis_status)
        open_run.clicked.connect(self.open_analysis_run_folder)
        status_actions.addWidget(load_model); status_actions.addWidget(refresh_status)
        status_actions.addWidget(open_run); status_actions.addStretch(1)
        status_layout.addLayout(status_actions)
        next_group = QGroupBox("Recommended next step")
        next_layout = QVBoxLayout(next_group)
        self.analysis_next_detail = QLabel("Load an existing model YAML to begin.")
        self.analysis_next_detail.setWordWrap(True)
        self.analysis_next_button = QPushButton("Load model YAML…")
        self.analysis_next_button.setObjectName("primary")
        self.analysis_next_button.clicked.connect(self.run_analysis_next_step)
        next_layout.addWidget(self.analysis_next_detail)
        next_layout.addWidget(self.analysis_next_button)
        status_layout.addWidget(next_group); status_layout.addStretch(1)

        # Setup ----------------------------------------------------------
        setup_page = QWidget(); setup_layout = QVBoxLayout(setup_page)
        outputs_group = QGroupBox("Impact and equity outputs")
        outputs_form = QFormLayout(outputs_group)
        self.impact_buildings = QCheckBox("Building exposure — Overture Maps")
        self.impact_buildings.setChecked(True)
        self.impact_population = QCheckBox("Population exposure and affected percentage — WorldPop")
        self.impact_population.setChecked(True)
        self.impact_svi = QCheckBox("Social-vulnerability burden — CDC/ATSDR SVI")
        self.impact_svi.setChecked(True)
        self.impact_duration = QCheckBox("Flood duration and duration attribution — saved time outputs")
        self.impact_duration.setChecked(True)
        self.impact_thresholds = QLineEdit("0.15,0.30,0.60,1.00,2.00")
        self.impact_thresholds.setPlaceholderText("Example: 0.15, 0.30, 0.60, 1.00, 2.00")
        self.duration_persistence = QLineEdit("6,12,24,48")
        self.duration_persistence.setPlaceholderText("Example: 6, 12, 24, 48")
        outputs_form.addRow(self.impact_buildings)
        outputs_form.addRow(self.impact_population)
        outputs_form.addRow(self.impact_svi)
        outputs_form.addRow(self.impact_duration)
        outputs_form.addRow("Flood-depth thresholds [m]", self.impact_thresholds)
        outputs_form.addRow("Persistent-duration thresholds [h]", self.duration_persistence)
        setup_layout.addWidget(outputs_group)
        prepare_group = QGroupBox("Prepare analysis workspace")
        prepare_layout = QVBoxLayout(prepare_group)
        prepare_note = (
            "Creates the seven counterfactual folders from the active parent model and "
            "caches selected exposure data. It does not run SFINCS.")
        self.add_info(prepare_layout, prepare_note, "Prepare analysis workspace")
        self.analysis_force = QCheckBox("Recreate existing prepared counterfactual folders")
        self.analysis_force.setToolTip(
            "Replace prepared counterfactual folders and their generated results. "
            "The parent model is not deleted.")
        prepare_layout.addWidget(self.analysis_force)
        self.analysis_prepare_button = QPushButton("Prepare analysis workspace")
        self.analysis_prepare_button.setObjectName("primary")
        self.analysis_prepare_button.setToolTip(
            "Prepare counterfactual inputs and Overture building data for the active model.")
        self.analysis_prepare_button.clicked.connect(self.prepare_analysis)
        prepare_layout.addWidget(self.analysis_prepare_button)
        setup_layout.addWidget(prepare_group); setup_layout.addStretch(1)

        # Simulations ----------------------------------------------------
        simulation_page = QWidget(); simulation_layout = QVBoxLayout(simulation_page)
        simulation_group = QGroupBox("Counterfactual simulations")
        simulation_group_layout = QVBoxLayout(simulation_group)
        self.analysis_simulation_status = QLabel("Prepare the analysis workspace first.")
        self.analysis_simulation_status.setWordWrap(True)
        simulation_group_layout.addWidget(self.analysis_simulation_status)
        simulation_note = (
            "Runs only missing counterfactual scenarios on the CPU. Completed outputs are "
            "reused unless recreation is selected in Setup.")
        self.add_info(simulation_group_layout, simulation_note, "Counterfactual simulations")
        self.analysis_run_button = QPushButton("Run missing counterfactual simulations")
        self.analysis_run_button.setObjectName("primary")
        self.analysis_run_button.setToolTip(
            "Run the prepared coastal, precipitation, and river combinations that do not yet have sfincs_map.nc.")
        self.analysis_run_button.clicked.connect(self.run_analysis_scenarios)
        simulation_group_layout.addWidget(self.analysis_run_button)
        simulation_layout.addWidget(simulation_group)
        all_group = QGroupBox("Run from the current state")
        all_layout = QVBoxLayout(all_group)
        all_note = (
            "Prepare missing files, run missing simulations, then generate the report for the active model.")
        self.add_info(all_layout, all_note, "Run from the current state")
        self.analysis_all_button = QPushButton("Run all remaining analysis steps")
        self.analysis_all_button.clicked.connect(self.run_complete_analysis)
        all_layout.addWidget(self.analysis_all_button)
        simulation_layout.addWidget(all_group); simulation_layout.addStretch(1)

        # Report ---------------------------------------------------------
        report_page = QWidget(); report_layout = QVBoxLayout(report_page)
        report_group = QGroupBox("Analysis report")
        report_group_layout = QVBoxLayout(report_group)
        self.analysis_report_detail = QLabel("Complete all eight scenario outputs before reporting.")
        self.analysis_report_detail.setWordWrap(True)
        report_group_layout.addWidget(self.analysis_report_detail)
        products = QFormLayout()
        for label, description in (
            ("Impacts", "Absolute area, buildings, population, and population percentage"),
            ("Attribution", "Shapley contributions, interactions, and amplification"),
            ("Duration", "Duration maps, area-hours, building-hours, and persistence"),
            ("Equity", "CDC SVI burden and Driver Impact Shift (DIS)"),
        ):
            description_label = QLabel(description)
            description_label.setWordWrap(True)
            products.addRow(label, description_label)
        report_group_layout.addLayout(products)
        report_actions = QHBoxLayout()
        self.analysis_report_button = QPushButton("Generate report from completed scenarios")
        self.analysis_report_button.setObjectName("primary")
        open_results = QPushButton("Open analysis results")
        self.analysis_report_button.clicked.connect(self.run_analysis_report)
        open_results.clicked.connect(self.open_analysis_results)
        report_actions.addWidget(self.analysis_report_button)
        report_actions.addWidget(open_results); report_actions.addStretch(1)
        report_group_layout.addLayout(report_actions)
        report_layout.addWidget(report_group); report_layout.addStretch(1)

        # Single-run tool ------------------------------------------------
        single_page = QWidget(); single_layout = QVBoxLayout(single_page)
        single_group = QGroupBox("Quick impact from one completed model run")
        single_group_layout = QVBoxLayout(single_group)
        single_note = (
            "Use this only when you want exposure totals for the parent flood map without driver attribution.")
        self.add_info(single_group_layout, single_note, "Quick impact")
        single_form = QFormLayout()
        self.impact_roads = QCheckBox("Road exposure — Overture transportation segments")
        self.impact_roads.setChecked(True)
        self.impact_resolution = QDoubleSpinBox(); self.impact_resolution.setRange(3, 1000)
        self.impact_resolution.setValue(30); self.impact_resolution.setSuffix(" m")
        self.force_downloads = QCheckBox("Refresh cached provider data")
        single_form.addRow(self.impact_roads)
        single_form.addRow("Analysis grid", self.impact_resolution)
        single_form.addRow(self.force_downloads)
        single_group_layout.addLayout(single_form)
        single_selection_note = (
            "Building and population choices are taken from the Setup tab.")
        self.add_info(single_group_layout, single_selection_note, "Quick impact inputs")
        single_actions = QHBoxLayout()
        single_run = QPushButton("Analyze the parent run only")
        single_both = QPushButton("Evaluate model, then analyze parent run")
        single_run.clicked.connect(self.run_impact)
        single_both.clicked.connect(self.run_evaluation_and_impact)
        single_actions.addWidget(single_run); single_actions.addWidget(single_both)
        single_actions.addStretch(1); single_group_layout.addLayout(single_actions)
        single_layout.addWidget(single_group); single_layout.addStretch(1)

        self.analysis_tabs.addTab(self.scroll(status_page), "Status")
        self.analysis_tabs.addTab(self.scroll(setup_page), "1  Setup")
        self.analysis_tabs.addTab(self.scroll(simulation_page), "2  Simulations")
        self.analysis_tabs.addTab(self.scroll(report_page), "3  Report")
        self.analysis_tabs.addTab(self.scroll(single_page), "Single-run tool")
        self.analysis_tabs.currentChanged.connect(lambda _index: self.refresh_analysis_status())
        layout.addWidget(self.analysis_tabs, 1)
        self.connect_dirty([
            self.impact_buildings, self.impact_population, self.impact_svi,
            self.impact_duration, self.impact_thresholds,
            self.duration_persistence, self.impact_roads, self.impact_resolution,
        ])
        self.impact_population.toggled.connect(self.sync_analysis_options)
        self.impact_duration.toggled.connect(self.sync_analysis_options)
        self.sync_analysis_options()
        return page

    def _make_results_page(self) -> QWidget:
        page, layout = self.page("Results", "Browse simulation files, evaluation plots, impact maps, reports, and CSV calculations.")
        top = QHBoxLayout()
        self.results_path = QLineEdit(); self.results_path.setReadOnly(True)
        refresh, open_folder = QPushButton("Refresh"), QPushButton("Open folder")
        refresh.clicked.connect(self.refresh_results); open_folder.clicked.connect(self.open_results_folder)
        top.addWidget(QLabel("Run directory")); top.addWidget(self.results_path, 1); top.addWidget(refresh); top.addWidget(open_folder); layout.addLayout(top)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.result_tree = QTreeWidget(); self.result_tree.setHeaderLabels(["Product", "Formats / size"]); self.result_tree.setMinimumWidth(340); self.result_tree.itemSelectionChanged.connect(self.result_selected)
        self.result_tabs = QTabWidget(); self.result_overview = QTextEdit(); self.result_overview.setReadOnly(True)
        image_scroll = QScrollArea(); image_scroll.setWidgetResizable(True)
        self.result_image = FittedPixmapLabel("Select an image result."); image_scroll.setWidget(self.result_image)
        self.result_table = QTableWidget()
        self.result_tabs.addTab(self.result_overview, "Overview / text"); self.result_tabs.addTab(image_scroll, "Plots"); self.result_tabs.addTab(self.result_table, "Table")
        splitter.addWidget(self.result_tree); splitter.addWidget(self.result_tabs); splitter.setStretchFactor(1, 1); layout.addWidget(splitter, 1)
        return page

    def connect_dirty(self, widgets: list[QWidget]) -> None:
        for widget in widgets:
            if isinstance(widget, QComboBox): widget.currentTextChanged.connect(self.mark_dirty)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)): widget.valueChanged.connect(self.mark_dirty)
            elif isinstance(widget, QLineEdit): widget.textChanged.connect(self.mark_dirty)
            elif isinstance(widget, QCheckBox): widget.toggled.connect(self.mark_dirty)

    def sync_analysis_options(self, *_: Any) -> None:
        self.impact_svi.setEnabled(self.impact_population.isChecked())
        self.impact_svi.setToolTip(
            "CDC SVI burden uses the affected-population calculation from WorldPop."
            if self.impact_population.isChecked()
            else "Enable population exposure to calculate CDC SVI population burden.")
        self.duration_persistence.setEnabled(self.impact_duration.isChecked())

    def connect_sfincs(self, widgets: list[QWidget]) -> None:
        for widget in widgets:
            if isinstance(widget, QComboBox): widget.currentTextChanged.connect(self.sfincs_changed)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)): widget.valueChanged.connect(self.sfincs_changed)
            elif isinstance(widget, QLineEdit): widget.textChanged.connect(self.sfincs_changed)

    def _restore_settings(self) -> None:
        self.cpu_threads.setValue(int(self.settings.value("cpu_threads", 0)))
        self.theme_choice.setCurrentText(str(self.settings.value("theme", "Dark")))
        self.default_project_dir.setText(str(self.settings.value("default_project_dir", str(self.root))))
        self.map_basemap.setChecked(str(self.settings.value("map_basemap", "true")).lower() == "true")
        runtime_index = self.runtime_choice.findData(self.runtime.backend)
        self.runtime_choice.blockSignals(True)
        self.runtime_choice.setCurrentIndex(max(0, runtime_index))
        self.runtime_choice.blockSignals(False)
        compute_index = self.compute_choice.findData(self.runtime.compute)
        self.compute_choice.blockSignals(True)
        self.compute_choice.setCurrentIndex(max(0, compute_index))
        self.compute_choice.blockSignals(False)
        self.refresh_runtime_info()
        sfincs_mode = str(self.settings.value("sfincs/mode", "existing"))
        sfincs_index = self.sfincs_choice.findData(sfincs_mode)
        self.sfincs_choice.setCurrentIndex(max(0, sfincs_index))
        self.sfincs_directory.setText(str(self.settings.value("sfincs/directory", "")))
        self.refresh_sfincs_inspection()
        stored = str(self.settings.value("basemap", "OpenStreetMap"))
        self.basemap_choice.setCurrentText(stored if stored in BASEMAPS else "OpenStreetMap")
        self.study_map.set_basemap(self.basemap_choice.currentText())

    def save_app_settings(self, *_: Any) -> None:
        self.settings.setValue("cpu_threads", self.cpu_threads.value())
        self.settings.setValue("theme", self.theme_choice.currentText())
        self.settings.setValue("default_project_dir", self.default_project_dir.text())
        self.settings.setValue("map_basemap", self.map_basemap.isChecked())
        self.settings.setValue("runtime/backend", self.runtime.backend)
        self.settings.setValue("runtime/compute", self.runtime.compute)
        self.settings.setValue("sfincs/mode", self.sfincs_choice.currentData())
        self.settings.setValue("sfincs/directory", self.sfincs_directory.text())

    def refresh_runtime_info(self) -> None:
        self.runtime_info.setText(
            f"Platform: {self.runtime.platform_name}\n"
            f"Backend: {self.runtime.backend_name}\n"
            f"Compute preference: {self.runtime.compute_name}\n"
            f"Package: {self.root}\n"
            "Automatic mode uses GPU only when both compatible hardware and a validated "
            "SFINCS GPU payload are available; otherwise it uses CPU.\n"
            "Evaluation: Cartopy\n"
            "Impact: Shapely + Overture Maps + WorldPop API v2"
        )

    def runtime_backend_changed(self, *_: Any) -> None:
        backend = str(self.runtime_choice.currentData() or "")
        updated = PlatformRuntime(self.root, backend=backend, compute=self.runtime.compute)
        if updated.backend == self.runtime.backend:
            return
        self.runtime = updated
        self.restart_required = False
        self.restart_button.setVisible(False)
        self.refresh_runtime_info()
        self.settings.setValue("runtime/backend", self.runtime.backend)
        self.statusBar().showMessage(
            f"Execution backend changed to {self.runtime.backend_name}. Check system status before running.",
            8000,
        )

    def compute_backend_changed(self, *_: Any) -> None:
        compute = str(self.compute_choice.currentData() or "auto")
        updated = PlatformRuntime(
            self.root, backend=self.runtime.backend, compute=compute,
        )
        if updated.compute == self.runtime.compute:
            return
        self.runtime = updated
        self.refresh_runtime_info()
        self.settings.setValue("runtime/compute", self.runtime.compute)
        self.statusBar().showMessage(
            f"SFINCS compute preference changed to {self.runtime.compute_name}. "
            "Check system status before running.",
            8000,
        )

    def choose_default_project_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Working directory for new AutoCF models",
            self.default_project_dir.text() or str(self.root))
        if selected: self.default_project_dir.setText(selected); self.save_app_settings()

    def open_default_project_folder(self) -> None:
        try:
            folder = self.project_base_dir()
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except Exception as exc:
            QMessageBox.warning(self, "Working directory", str(exc))

    def start_new_model(self) -> None:
        """Begin a clean model without inheriting paths or cached model state."""
        has_active_model = bool(
            self.config_path or self.geojson_path.text().strip()
            or self.model_workspace.text().strip())
        if has_active_model:
            answer = QMessageBox.question(
                self, "Start a new model",
                "Close the current model and start a fresh session? Unsaved changes in the current model will not be kept.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        selected_folder = QFileDialog.getExistingDirectory(
            self, "Choose working directory for the new AutoCF model",
            self.default_project_dir.text().strip() or str(self.root))
        if not selected_folder:
            self.statusBar().showMessage(
                "New model cancelled — choose a working directory to continue.", 6000)
            return
        selected_workspace = Path(selected_folder).expanduser().resolve()
        self.default_project_dir.setText(str(selected_workspace))
        self.save_app_settings()
        self.loading = True
        self.session_mode = "new"
        self.config_path = None
        self.raw_config = {}
        self.config = {}
        for widget in (
                self.config_display, self.project_name, self.model_workspace,
                self.output_dir, self.cache_dir, self.geojson_path,
                self.event_name, self.topo_url, self.topo_file, self.gap_url,
                self.gap_file, self.rain_file, self.wind_file, self.pressure_file,
                self.ocean_timeseries, self.river_timeseries):
            widget.clear()
        # The selected directory is a parent only.  No YAML, GeoJSON, runs, or
        # cache is written there directly; the canonical model workspace is
        # established from the project/event identity before the first save.
        self.model_workspace.clear()
        self.output_dir.clear()
        self.cache_dir.clear()
        self.topo_source.setCurrentText("NOAA_CUDEM_1_9_ARCSEC")
        self.gap_source.setCurrentText("GEBCO_2024_15_ARCSEC")
        self.load_extra_terrain_entries([])
        self.set_mesh_cell_size(200.0)
        self.mesh_crs.setCurrentText("utm")
        self.topo_buffer.setValue(10.0)
        self.vertical_datum.setCurrentText("NAVD88")
        self.topo_native_datum.setCurrentText("NAVD88")
        self.gap_native_datum.setCurrentText("MSL")
        self.landcover_source.setCurrentText("ESA_WorldCover_2021_v200")
        self.infiltration_enabled.setChecked(False)
        self.infiltration_method.setCurrentText("cn")
        self.infiltration_source.setCurrentText("gcn250")
        self.infiltration_file.clear()
        self.antecedent.setCurrentText("avg")
        self.effective.setValue(0.5)
        self.soil_source.setCurrentText("ssurgo_sda")
        self.sync_infiltration_ui()
        self.rain_source.setCurrentText("aorc")
        self.wind_source.setCurrentText("aorc")
        self.pressure_source.setCurrentText("aorc")
        self.ocean_source.setCurrentText("noaa_coops")
        self.ocean_datum.setCurrentText("NAVD")
        self.ocean_distance.setValue(150.0)
        self.ocean_file_layout.setCurrentIndex(0)
        self.ocean_file_units.setCurrentText("m")
        self.ocean_file_timezone.setCurrentText("UTC")
        self.ocean_file_datum.setCurrentText("MSL")
        self.ocean_datum_policy.setCurrentIndex(0)
        self.ocean_datum_offset.setValue(0.0)
        self.river_source.setCurrentText("glofas_auto")
        self.river_distance.setValue(50.0)
        self.max_gages.setValue(12)
        self.river_file_layout.setCurrentIndex(0)
        self.river_file_units.setCurrentText("m3/s")
        self.river_file_timezone.setCurrentText("UTC")
        self.model_mode.setCurrentIndex(max(0, self.model_mode.findData("regular")))
        self.input_format.setCurrentText("bin")
        self.output_format.setCurrentText("net")
        self.dtout.setValue(3600.0)
        self.dthisout.setValue(360.0)
        self.dtmaxout.setValue(3600.0)
        self.subgrid_levels.setValue(10)
        self.subgrid_pixels.setValue(20)
        self.sync_model_mode_ui()
        self.domain_geojson = None
        self.domain_bounds = None
        self.draft_domain_bounds = None
        self.draft_domain_geometries = []
        self.draft_domain_known = False
        self.hwm_aoi_geometries = []
        self.selected_station_ids = []
        self.selected_obs_noaa_ids = []
        self.boundary_station_selections = {}
        self.observation_station_selections = {}
        self.selected_hwm_ids = []
        self.selected_usgs_ids = []
        self.clear_huc()
        self.study_map.clear_draft()
        self.study_map.set_dem_image(None)
        self.domain_info.setText("No domain loaded")
        self.project_validation.clear()
        self.validity.setText("New model — not saved")
        self.validity.setStyleSheet("")
        self.loading = False
        self.sync_topo_provider()
        self.sync_gap_source()
        self.dirty = False
        self.update_title()
        folder = selected_workspace
        self.project_session_status.setText(
            f"New-model parent folder selected:\n{folder}\n"
            "Choose the project identity and event. AutoCF will create one canonical model folder before saving.")
        self.output.append(f"> New clean model session\nWorking directory: {folder}\n")
        self.refresh_analysis_status()
        self.statusBar().showMessage(
            "New model directories populated — review them, then open Event & Time when ready.", 9000)

    def sfincs_choice_changed(self, *_: Any) -> None:
        existing = self.sfincs_choice.currentData() == "existing"
        self.sfincs_browse_button.setEnabled(existing)
        self.sfincs_download_button.setEnabled(not existing and self.sfincs_worker is None)
        if existing:
            self.sfincs_status.setText("Select the folder containing SFINCS, its source, or an existing sfincs.inp model.")
        else:
            destination = self.root / "Software" / "SFINCS"
            self.sfincs_status.setText(f"The latest official GitHub source will be saved under:\n{destination}")
        self.save_app_settings()

    def choose_sfincs_directory(self) -> None:
        start = self.sfincs_directory.text() or str(self.root)
        selected = QFileDialog.getExistingDirectory(self, "Choose existing SFINCS directory", start)
        if not selected:
            return
        self.sfincs_directory.setText(selected)
        self.save_app_settings()
        self.refresh_sfincs_inspection(show_dialog=True)
        QTimer.singleShot(0, self.check_sfincs_latest)

    def refresh_sfincs_inspection(self, show_dialog: bool = False) -> None:
        selected = self.sfincs_directory.text().strip()
        self.sfincs_inspection = None
        if not selected:
            if self.sfincs_choice.currentData() == "existing":
                self.sfincs_status.setText("No existing SFINCS directory selected.")
            return
        try:
            info = inspect_installation(Path(selected))
            self.sfincs_inspection = info
            version = info.get("version") or "unknown"
            descriptions = {
                "runnable": "runnable SFINCS installation",
                "source": "SFINCS source tree",
                "model": "SFINCS model directory",
                "unrecognized": "directory (no SFINCS executable, source tree, or sfincs.inp found)",
            }
            self.sfincs_status.setText(f"Detected {descriptions[info['kind']]}\nVersion: {version}\n{selected}")
            if show_dialog and info["kind"] == "unrecognized":
                QMessageBox.warning(
                    self, "SFINCS not recognized",
                    "AutoCF saved this folder, but did not find a SFINCS executable, source tree, or sfincs.inp file in it.",
                )
        except Exception as exc:
            self.sfincs_status.setText(f"Could not inspect SFINCS directory: {exc}")

    def set_sfincs_busy(self, busy: bool, message: str = "") -> None:
        self.sfincs_choice.setEnabled(not busy)
        self.sfincs_browse_button.setEnabled(not busy and self.sfincs_choice.currentData() == "existing")
        self.sfincs_check_button.setEnabled(not busy)
        self.sfincs_official_button.setEnabled(not busy)
        downloading = bool(busy and self.sfincs_worker and self.sfincs_worker.action == "download")
        self.sfincs_download_button.setEnabled(downloading or (not busy and self.sfincs_choice.currentData() == "download"))
        self.sfincs_download_button.setText("Cancel download" if downloading else "Download latest source")
        self.sfincs_progress.setVisible(busy)
        if busy:
            self.sfincs_progress.setValue(0)
            if message:
                self.sfincs_status.setText(message)

    def start_sfincs_worker(self, action: str) -> None:
        if self.sfincs_worker is not None:
            return
        self.sfincs_download_after_check = False
        self.sfincs_worker = SfincsReleaseWorker(action, self.root / "Software")
        self.sfincs_worker.succeeded.connect(self.sfincs_worker_succeeded)
        self.sfincs_worker.failed.connect(self.sfincs_worker_failed)
        self.sfincs_worker.progress.connect(self.sfincs_worker_progress)
        self.sfincs_worker.finished.connect(self.sfincs_worker_finished)
        message = "Checking the official Deltares/SFINCS GitHub release…" if action == "check" else "Downloading the official SFINCS source from GitHub…"
        self.set_sfincs_busy(True, message)
        self.sfincs_worker.start()

    def check_sfincs_latest(self) -> None:
        self.refresh_sfincs_inspection()
        self.start_sfincs_worker("check")

    def download_sfincs_latest(self) -> None:
        if self.sfincs_worker is not None and self.sfincs_worker.action == "download":
            self.sfincs_worker.requestInterruption()
            self.sfincs_download_button.setEnabled(False)
            self.sfincs_status.setText("Cancelling the SFINCS download…")
            return
        self.start_sfincs_worker("download")

    def sfincs_worker_progress(self, percent: int, detail: str) -> None:
        self.sfincs_progress.setValue(percent)
        self.sfincs_status.setText(f"Downloading the official SFINCS source from GitHub…\n{detail}")

    def sfincs_worker_succeeded(self, action: str, payload: object) -> None:
        if action == "download":
            result = dict(payload)
            release, installed = result["release"], str(result["path"])
            self.sfincs_latest = release
            self.sfincs_directory.setText(installed)
            self.save_app_settings()
            self.refresh_sfincs_inspection()
            self.sfincs_status.setText(
                f"SFINCS {release.get('version') or release['tag_name']} source downloaded successfully.\n{installed}\n"
                "Use “Get runnable SFINCS” for the platform executable or container. AutoCF runs remain on the validated managed runtime."
            )
            QMessageBox.information(self, "SFINCS source downloaded", self.sfincs_status.text())
            return

        release = dict(payload)
        self.sfincs_latest = release
        latest_version = release.get("version") or release["tag_name"]
        installed_version = (self.sfincs_inspection or {}).get("version")
        if installed_version and is_newer(str(latest_version), str(installed_version)):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Newer SFINCS release available")
            box.setText(f"Your SFINCS version: {installed_version}\nLatest official release: {latest_version}")
            box.setInformativeText("Do you want to continue with your current version or download the updated source into AutoCF\\Software\\SFINCS?")
            keep = box.addButton("Continue with current", QMessageBox.ButtonRole.AcceptRole)
            update = box.addButton("Download updated source", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is update:
                self.sfincs_choice.setCurrentIndex(self.sfincs_choice.findData("download"))
                self.sfincs_download_after_check = True
            elif box.clickedButton() is keep:
                self.sfincs_status.setText(f"Continuing with SFINCS {installed_version}. Latest release is {latest_version}.")
            return
        if installed_version:
            self.sfincs_status.setText(f"Your SFINCS {installed_version} is current. Latest official release: {latest_version}.")
        elif self.sfincs_choice.currentData() == "existing":
            self.sfincs_status.setText(f"Latest official release: {latest_version}. AutoCF could not determine the selected folder's version.")
        else:
            self.sfincs_status.setText(f"Latest official release: {latest_version}. Select Download latest source to install it under AutoCF\\Software\\SFINCS.")

    def sfincs_worker_failed(self, message: str) -> None:
        self._riva_panic()
        self.sfincs_status.setText(f"SFINCS operation failed: {message}")
        QMessageBox.warning(self, "SFINCS setup", message)

    def sfincs_worker_finished(self) -> None:
        worker = self.sfincs_worker
        self.sfincs_worker = None
        if worker is not None:
            worker.deleteLater()
        self.set_sfincs_busy(False)
        if self.sfincs_download_after_check:
            self.sfincs_download_after_check = False
            QTimer.singleShot(0, self.download_sfincs_latest)

    def open_sfincs_download(self) -> None:
        url = WINDOWS_DOWNLOAD_URL if self.runtime.platform_name == "Windows" else DOCKER_HUB_URL
        QDesktopServices.openUrl(QUrl(url))

    def choose_directory(self, target: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose directory", target.text() or str(self.root))
        if selected: target.setText(selected)

    def open_model_workspace(self) -> None:
        value = self.model_workspace.text().strip()
        if value and Path(value).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(value))
        else:
            QMessageBox.information(
                self, "Model workspace",
                "Create or import the study-area GeoJSON first. AutoCF will then create the model workspace.")

    def choose_file(self, target: QLineEdit, filter_text: str) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Choose file", str(self.root), filter_text)
        if selected: target.setText(selected)

    def choose_config(self) -> None:
        start = self.config_path.parent if self.config_path else Path(self.default_project_dir.text() or self.root)
        selected, _ = QFileDialog.getOpenFileName(self, "Open AutoCF configuration", str(start), "YAML (*.yml *.yaml)")
        if selected: self.load_config(Path(selected))

    def reset_transient_project_state_for_import(self) -> None:
        self._forcing_signature = None
        self._forcing_save_error = ""
        self._forcing_save_pending = False
        """Remove every model-specific value before applying an imported YAML."""
        self.huc_search_generation += 1
        self.pending_huc_search_after_worker = False
        self.searched_location_bounds = None
        self.current_storm = None
        self.domain_geojson = None
        self.domain_bounds = None
        self.draft_domain_bounds = None
        self.draft_domain_geometries = []
        self.draft_domain_known = False
        self.hwm_aoi_geometries = []
        self.huc_records = []
        self.current_huc = None
        self.selected_hucs = []
        huc_revision = self.next_huc_selection_revision()
        self.selected_station_ids = []
        self.selected_obs_noaa_ids = []
        self.boundary_station_selections = {}
        self.observation_station_selections = {}
        self.water_level_catalogs = {}
        self.selected_hwm_ids = []
        self.selected_usgs_ids = []
        self.coops_stations = []
        self.hwm_marks = []
        self.usgs_sites = []
        self.forcing_layers = []
        self.forcing_manifest = {}
        self.study_map.set_mode("none")
        self.study_map.set_huc_features([], huc_revision)
        self.study_map.set_stations([])
        self.study_map.set_selected_stations([])
        self.study_map.set_observation_noaa([])
        self.study_map.set_selected_observation_noaa([])
        self.study_map.set_high_water_marks([])
        self.study_map.set_selected_high_water_marks([])
        self.study_map.set_usgs_sites([])
        self.study_map.set_selected_usgs([])
        self.study_map.set_dem_image(None)
        empty = {"type": "FeatureCollection", "features": []}
        self.study_map.set_layers(empty, [])
        self.event_map.set_layers(empty, [])
        self.study_map.clear_draft()

    def set_datetime(self, widget: QDateTimeEdit, text: str) -> None:
        value = QDateTime.fromString(text, Qt.DateFormat.ISODate)
        widget.setDateTime(value.toUTC() if value.isValid() else QDateTime.currentDateTimeUtc())

    def load_config(self, path: Path) -> None:
        try:
            editor = YAML(); editor.preserve_quotes = True
            with path.open("r", encoding="utf-8") as stream: raw = editor.load(stream) or {}
            config = resolved_yaml(path)
            repaired_defaults = repair_uninitialized_desktop_defaults(config)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open configuration", str(exc)); return
        self.loading = True
        self.reset_transient_project_state_for_import()
        self.session_mode = "imported"
        self.config_path, self.raw_config, self.config = path.resolve(), raw, config
        self.config_display.setText(str(self.config_path))
        values = [
            (self.project_name, "project.name", "AutoCF_project"),
            (self.output_dir, "project.output_dir", "./runs/autocf_project"),
            (self.cache_dir, "project.cache_dir", "./cache"), (self.geojson_path, "geojson.path", ""),
            (self.event_name, "simulation.event_name", ""), (self.topo_url, "topobathy.primary.vrt_url", ""),
            (self.topo_file, "topobathy.primary.path", ""), (self.gap_url, "topobathy.gapfill.cog_url", ""), (self.gap_file, "topobathy.gapfill.path", ""),
            (self.topo_acquisition_date, "topobathy.primary.acquisition_date", ""),
            (self.gap_acquisition_date, "topobathy.gapfill.acquisition_date", ""),
            (self.rain_file, "forcing.precipitation.path", ""), (self.wind_file, "forcing.wind.path", ""),
            (self.pressure_file, "forcing.pressure.path", ""),
            (self.ocean_timeseries, "forcing.ocean_boundary.timeseries", ""),
            (self.river_timeseries, "forcing.river.timeseries", ""),
            (self.infiltration_file, "infiltration.path", ""),
            (self.sfincs_input, "sfincs.input_file", "sfincs.inp"),
        ]
        for widget, key, default in values: widget.setText(str(deep_get(config, key, default)))
        # The imported YAML's own folder is the active model workspace. Output
        # and cache remain separate explicit fields and cannot retain the
        # previously open project's workspace.
        self.model_workspace.setText(str(path.parent.resolve()))
        self.overwrite.setChecked(bool(deep_get(config, "project.overwrite", False)))
        self.set_mesh_cell_size(float(deep_get(config, "mesh.dx_m", 200))); self.mesh_crs.setCurrentText(str(deep_get(config, "mesh.crs", "utm"))); self.topo_buffer.setValue(float(deep_get(config, "topobathy.buffer_km", 10)))
        self.set_datetime(self.start_time, str(deep_get(config, "simulation.start", "2024-01-01T00:00:00Z"))); self.set_datetime(self.end_time, str(deep_get(config, "simulation.end", "2024-01-02T00:00:00Z")))
        combos = [
            (self.topo_source, "topobathy.primary.source", "NOAA_CUDEM_1_9_ARCSEC"), (self.vertical_datum, "topobathy.target_vertical_datum", "NAVD88"),
            (self.topo_native_datum, "topobathy.primary.vertical_datum", "NAVD88"),
            (self.gap_source, "topobathy.gapfill.source", "GEBCO_2024_15_ARCSEC"), (self.landcover_source, "landcover.source", "ESA_WorldCover_2021_v200"),
            (self.gap_native_datum, "topobathy.gapfill.vertical_datum", "MSL"),
            (self.infiltration_method, "infiltration.method", "cn"),
            (self.infiltration_source, "infiltration.source", "gcn250"), (self.antecedent, "infiltration.antecedent_moisture", "avg"),
            (self.soil_source, "infiltration.soils.source", "ssurgo_sda"), (self.rain_source, "forcing.precipitation.source", "aorc"),
            (self.wind_source, "forcing.wind.source", "aorc"), (self.pressure_source, "forcing.pressure.source", "aorc"),
            (self.ocean_source, "forcing.ocean_boundary.source", "noaa_coops"), (self.ocean_datum, "forcing.ocean_boundary.datum", "MSL"),
            (self.ocean_file_units, "forcing.ocean_boundary.units", "m"),
            (self.ocean_file_timezone, "forcing.ocean_boundary.time_zone", "UTC"),
            (self.ocean_file_datum, "forcing.ocean_boundary.source_vertical_datum",
             str(deep_get(config, "forcing.ocean_boundary.datum", "MSL"))),
            (self.river_source, "forcing.river.source", "glofas_auto"),
            (self.river_file_units, "forcing.river.units", "m3/s"),
            (self.river_file_timezone, "forcing.river.time_zone", "UTC"),
            (self.input_format, "sfincs.inputformat", "bin"), (self.output_format, "sfincs.outputformat", "net"),
        ]
        for widget, key, default in combos: widget.setCurrentText(str(deep_get(config, key, default)))
        for widget, key, default in (
                (self.ocean_file_layout, "forcing.ocean_boundary.input_layout", "auto"),
                (self.ocean_datum_policy, "forcing.ocean_boundary.datum_policy", "match"),
                (self.river_file_layout, "forcing.river.input_layout", "auto")):
            value = str(deep_get(config, key, default))
            index = widget.findData(value)
            widget.setCurrentIndex(index if index >= 0 else 0)
        model_mode = str(deep_get(config, "model.mode", "regular")).strip().lower()
        self.model_mode.setCurrentIndex(max(0, self.model_mode.findData(model_mode)))
        self.sync_model_mode_ui()
        mapping = str(deep_get(config, "landcover.manning_mapping", "default_esa_worldcover")).lower()
        mapping_key = "manual_lulc_classes" if mapping in {"manual_lulc_classes", "manual", "custom"} else "default_esa_worldcover"
        mapping_index = self.manning_mapping.findData(mapping_key)
        self.manning_mapping.setCurrentIndex(max(0, mapping_index))
        configured_rows = deep_get(config, "landcover.manual_manning_classes", None)
        if isinstance(configured_rows, dict):
            configured_rows = [
                {"code": code, "name": f"Class {code}", "n": value}
                for code, value in configured_rows.items()
            ]
        if isinstance(configured_rows, list) and configured_rows:
            self.set_manual_manning_rows(configured_rows)
        else:
            self.set_manual_manning_rows(DEFAULT_ESA_MANNING_ROWS)
        numbers = [
            (self.topo_resolution, "topobathy.primary.output_resolution_m", 3), (self.gap_resolution, "topobathy.gapfill.output_resolution_m", 500),
            (self.landcover_resolution, "landcover.resolution_m", 10), (self.effective, "infiltration.effective", 0.5),
            (self.ocean_distance, "forcing.ocean_boundary.max_station_distance_km", 150), (self.river_distance, "forcing.river.max_gage_distance_km", 50),
            (self.max_gages, "forcing.river.max_gages", 12), (self.dtout, "sfincs.dtout_seconds", 3600),
            (self.ocean_datum_offset, "forcing.ocean_boundary.vertical_datum_offset_m", 0.0),
            (self.dthisout, "sfincs.dthisout_seconds", 360), (self.dtmaxout, "sfincs.dtmaxout_seconds", 3600),
            (self.subgrid_levels, "subgrid.nlevels", 10), (self.subgrid_pixels, "subgrid.nr_subgrid_pixels", 20),
        ]
        for widget, key, default in numbers:
            value = deep_get(config, key, default)
            widget.setValue(int(value) if isinstance(widget, QSpinBox) else float(value))
        self.infiltration_enabled.setChecked(bool(deep_get(config, "infiltration.enabled", False)))
        parameters = deep_get(config, "sfincs.additional_parameters", {}) or {}
        for key, _, kind, default, _, _ in SFINCS_PARAMETERS:
            widget = self.advanced_widgets[key]; value = parameters.get(key, default)
            widget.setCurrentText(str(int(value))) if kind == "choice" else widget.setValue(float(value))
        thresholds = deep_get(config, "analysis.attribution.depth_thresholds_m",
                              deep_get(config, "impact.flood_depth_thresholds_m", [0.05, 0.10, 0.25, 0.50, 1.00, 2.00]))
        self.impact_thresholds.setText(",".join(str(value) for value in thresholds))
        self.impact_resolution.setValue(float(deep_get(config, "impact.analysis_resolution_m", 30)))
        self.impact_buildings.setChecked(bool(deep_get(
            config, "analysis.exposure.buildings.enabled",
            deep_get(config, "impact.buildings.enabled", True))))
        self.impact_roads.setChecked(bool(deep_get(config, "impact.roads.enabled", True)))
        self.impact_population.setChecked(bool(deep_get(
            config, "analysis.exposure.population.enabled",
            deep_get(config, "impact.population.enabled", True))))
        self.impact_svi.setChecked(bool(deep_get(
            config, "analysis.social_vulnerability.enabled", True)))
        self.impact_duration.setChecked(bool(deep_get(
            config, "analysis.duration.enabled", True)))
        persistence = deep_get(
            config, "analysis.duration.persistence_thresholds_hours",
            [6, 12, 24, 48])
        self.duration_persistence.setText(",".join(str(value) for value in persistence))
        self.sync_manning_mapping()
        self.sync_landcover_source()
        self.load_extra_terrain_entries(deep_get(config, "topobathy.additional_sources", []) or [])
        self.loading = False; self.dirty = False
        self.sync_infiltration_ui(); self.sync_topo_provider(); self.sync_gap_source(); self.sync_atmosphere_sources(); self.sync_ocean_source_ui(); self.sync_river_source_ui()
        self.load_prepared_dem()
        self.load_domain_preview(); self.update_sfincs_preview(); self.refresh_results(); self.validate_config(silent=True); self.update_title(); self.refresh_analysis_status()
        if repaired_defaults:
            self.project_session_status.setText(
                f"Existing project loaded:\n{self.config_path}\n"
                "Uninitialized numeric placeholders were replaced with standard defaults.")
            self.output.append(
                f"> Opened configuration\n{self.config_path}\n"
                "Applied standard defaults to legacy uninitialized numeric fields.\n")
        else:
            self.project_session_status.setText(f"Existing project loaded:\n{self.config_path}")
            self.output.append(f"> Opened configuration\n{self.config_path}\n")

    def collect_config(self) -> None:
        raw = self.raw_config
        if self.session_mode == "new":
            deep_set(raw, "project.layout_version", 2)
        portable = lambda value: portable_yaml_path(value, self.config_path)
        assignments = {
            "project.name": self.project_name.text().strip(),
            "project.output_dir": portable(self.output_dir.text()), "project.cache_dir": portable(self.cache_dir.text()),
            "project.overwrite": self.overwrite.isChecked(), "geojson.path": portable(self.geojson_path.text()), "mesh.type": "regular",
            "mesh.dx_m": float(self.mesh_dx.value()), "mesh.crs": self.mesh_crs.currentText(), "topobathy.buffer_km": float(self.topo_buffer.value()),
            "simulation.event_name": self.event_name.text().strip(), "simulation.start": self.start_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate),
            "simulation.end": self.end_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate), "topobathy.target_vertical_datum": self.vertical_datum.currentText(),
            "topobathy.datum_policy": "align_gapfill",
            "topobathy.datum_engine": str(deep_get(
                self.config or raw, "topobathy.datum_engine", "auto")),
            "topobathy.primary.source": self.topo_source.currentText(), "topobathy.primary.output_resolution_m": float(self.topo_resolution.value()),
            "topobathy.primary.vertical_datum": self.topo_native_datum.currentText(),
            "topobathy.gapfill.source": self.gap_source.currentText(), "topobathy.gapfill.output_resolution_m": float(self.gap_resolution.value()),
            "topobathy.gapfill.vertical_datum": self.gap_native_datum.currentText(),
            "landcover.source": self.landcover_source.currentText(), "landcover.resolution_m": float(self.landcover_resolution.value()), "landcover.manning_mapping": self.manning_mapping.currentData(),
            "infiltration.enabled": self.infiltration_enabled.isChecked(), "infiltration.method": self.infiltration_method.currentText(), "infiltration.source": self.infiltration_source.currentText(),
            "infiltration.path": portable(self.infiltration_file.text()) if self.infiltration_method.currentText() == "cn" and self.infiltration_source.currentText() == "file" else "",
            "infiltration.antecedent_moisture": self.antecedent.currentText(), "infiltration.effective": float(self.effective.value()), "infiltration.soils.source": self.soil_source.currentText(),
            "forcing.precipitation.source": self.rain_source.currentText(), "forcing.wind.source": self.wind_source.currentText(), "forcing.pressure.source": self.pressure_source.currentText(),
            "forcing.precipitation.path": portable(self.rain_file.text()) if self.rain_source.currentText().strip().lower() in {"aorc", "mrms", "file"} else "",
            "forcing.wind.path": portable(self.wind_file.text()) if self.wind_source.currentText().strip().lower() in {"aorc", "file"} else "",
            "forcing.pressure.path": portable(self.pressure_file.text()) if self.pressure_source.currentText().strip().lower() in {"aorc", "file"} else "",
            "forcing.ocean_boundary.source": self.ocean_source.currentText(),
            "forcing.river.source": self.river_source.currentText(), "forcing.river.max_gage_distance_km": float(self.river_distance.value()), "forcing.river.max_gages": int(self.max_gages.value()),
            "model.mode": self.model_mode.currentData(), "sfincs.input_file": self.sfincs_input.text().strip(), "sfincs.inputformat": self.input_format.currentText(),
            "sfincs.outputformat": self.output_format.currentText(), "sfincs.dtout_seconds": float(self.dtout.value()), "sfincs.dthisout_seconds": float(self.dthisout.value()),
            "sfincs.dtmaxout_seconds": float(self.dtmaxout.value()), "subgrid.nlevels": int(self.subgrid_levels.value()), "subgrid.nr_subgrid_pixels": int(self.subgrid_pixels.value()),
            "impact.analysis_resolution_m": float(self.impact_resolution.value()), "impact.buildings.enabled": self.impact_buildings.isChecked(), "impact.roads.enabled": self.impact_roads.isChecked(), "impact.population.enabled": self.impact_population.isChecked(),
            "analysis.enabled": True, "analysis.attribution.primary_min_depth_m": 0.10,
            "analysis.exposure.enabled": self.impact_buildings.isChecked() or self.impact_population.isChecked(),
            "analysis.exposure.buildings.enabled": self.impact_buildings.isChecked(),
            "analysis.exposure.buildings.source": "overture",
            "analysis.exposure.population.enabled": self.impact_population.isChecked(),
            "analysis.exposure.population.source": "worldpop",
            "analysis.exposure.depth_definition": "excess_over_none",
            "analysis.duration.enabled": self.impact_duration.isChecked(),
            "analysis.social_vulnerability.enabled": (
                self.impact_svi.isChecked() and self.impact_population.isChecked()),
            "analysis.land_mask.enabled": True, "analysis.land_mask.worldcover_water_classes": [80],
            "analysis.land_mask.min_land_fraction": 0.5,
        }
        # The default project parent is an application preference, not part of
        # a portable scientific recipe. Drop the legacy duplicate field.
        project_block = raw.get("project") if isinstance(raw, dict) else None
        if isinstance(project_block, dict):
            project_block.pop("workspace_dir", None)
        primary_url = (
            BUILTIN_PRIMARY_TOPOBATHY_URLS.get(self.topo_source.currentText().strip().upper())
            or self.topo_url.text().strip()
        )
        gapfill_url = (
            BUILTIN_GAPFILL_TOPOBATHY_URLS.get(self.gap_source.currentText().strip().upper())
            or self.gap_url.text().strip()
        )
        if primary_url:
            assignments["topobathy.primary.vrt_url"] = primary_url
        else:
            deep_pop(raw, "topobathy.primary.vrt_url")
        primary_is_local = self.topo_source.currentText().strip().lower() in LOCAL_TOPOBATHY_SOURCES
        gapfill_is_local = self.gap_source.currentText().strip().lower() in LOCAL_TOPOBATHY_SOURCES
        if primary_is_local and self.topo_file.text().strip():
            assignments["topobathy.primary.path"] = portable(self.topo_file.text())
        else:
            deep_pop(raw, "topobathy.primary.path")
        if self.topo_acquisition_date.text().strip():
            assignments["topobathy.primary.acquisition_date"] = self.topo_acquisition_date.text().strip()
        else:
            deep_pop(raw, "topobathy.primary.acquisition_date")
        if gapfill_url:
            assignments["topobathy.gapfill.cog_url"] = gapfill_url
        else:
            deep_pop(raw, "topobathy.gapfill.cog_url")
        if self.gap_acquisition_date.text().strip():
            assignments["topobathy.gapfill.acquisition_date"] = self.gap_acquisition_date.text().strip()
        else:
            deep_pop(raw, "topobathy.gapfill.acquisition_date")
        landcover_dataset = LANDCOVER_DATASETS.get(
            self.landcover_source.currentText().strip().upper(), {})
        landcover_tile_url = landcover_dataset.get("tile_url_template")
        if landcover_tile_url:
            assignments["landcover.tile_url_template"] = landcover_tile_url
        else:
            # Never carry ESA's URL into a custom file or another provider.
            deep_pop(raw, "landcover.tile_url_template")
        if gapfill_is_local and self.gap_file.text().strip():
            assignments["topobathy.gapfill.path"] = portable(self.gap_file.text())
        else:
            deep_pop(raw, "topobathy.gapfill.path")
        if self.ocean_source.currentText().strip().lower() == "file":
            assignments.update({
                "forcing.ocean_boundary.timeseries": portable(self.ocean_timeseries.text()),
                "forcing.ocean_boundary.input_layout": self.ocean_file_layout.currentData(),
                "forcing.ocean_boundary.units": self.ocean_file_units.currentText().strip(),
                "forcing.ocean_boundary.time_zone": self.ocean_file_timezone.currentText().strip(),
                "forcing.ocean_boundary.source_vertical_datum": self.ocean_file_datum.currentText().strip(),
                "forcing.ocean_boundary.datum": self.vertical_datum.currentText().strip(),
                "forcing.ocean_boundary.datum_policy": self.ocean_datum_policy.currentData(),
                "forcing.ocean_boundary.vertical_datum_offset_m": (
                    float(self.ocean_datum_offset.value())
                    if self.ocean_datum_policy.currentData() == "offset" else 0.0),
            })
            # File locations live in the canonical study-area GeoJSON. Leaving a
            # second locations path would create two editable sources of truth.
            deep_pop(raw, "forcing.ocean_boundary.locations")
            deep_pop(raw, "forcing.ocean_boundary.max_station_distance_km")
        else:
            assignments["forcing.ocean_boundary.datum"] = self.ocean_datum.currentText()
            assignments["forcing.ocean_boundary.max_station_distance_km"] = float(self.ocean_distance.value())
            for key in ("timeseries", "input_layout", "units", "time_zone",
                        "source_vertical_datum", "datum_policy", "vertical_datum_offset_m"):
                deep_pop(raw, f"forcing.ocean_boundary.{key}")
        if self.river_source.currentText().strip().lower() == "file":
            assignments.pop("forcing.river.max_gage_distance_km", None)
            assignments.pop("forcing.river.max_gages", None)
            assignments.update({
                "forcing.river.timeseries": portable(self.river_timeseries.text()),
                "forcing.river.input_layout": self.river_file_layout.currentData(),
                "forcing.river.units": self.river_file_units.currentText().strip(),
                "forcing.river.time_zone": self.river_file_timezone.currentText().strip(),
            })
            deep_pop(raw, "forcing.river.locations")
            deep_pop(raw, "forcing.river.max_gage_distance_km")
            deep_pop(raw, "forcing.river.max_gages")
        else:
            for key in ("timeseries", "input_layout", "units", "time_zone"):
                deep_pop(raw, f"forcing.river.{key}")
        for path, value in assignments.items(): deep_set(raw, path, value)
        deep_set(raw, "topobathy.additional_sources", self.extra_terrain_values())
        if self.manning_mapping.currentData() == "manual_lulc_classes":
            deep_set(raw, "landcover.manual_manning_classes", self.manual_manning_rows())
        for key, _, kind, _, _, _ in SFINCS_PARAMETERS:
            widget = self.advanced_widgets[key]; value = int(widget.currentText()) if kind == "choice" else float(widget.value()); deep_set(raw, f"sfincs.additional_parameters.{key}", value)
        analysis_thresholds = [float(value.strip()) for value in self.impact_thresholds.text().split(",") if value.strip()]
        persistence_thresholds = [float(value.strip()) for value in self.duration_persistence.text().split(",") if value.strip()]
        deep_set(raw, "impact.flood_depth_thresholds_m", analysis_thresholds)
        deep_set(raw, "analysis.attribution.depth_thresholds_m", analysis_thresholds)
        deep_set(raw, "analysis.duration.depth_thresholds_m", analysis_thresholds)
        deep_set(raw, "analysis.duration.persistence_thresholds_hours", persistence_thresholds)
        deep_set(raw, "analysis.social_vulnerability.depth_thresholds_m", analysis_thresholds)
        if self.current_storm:
            deep_set(raw, "event_catalog.source", "IBTrACS_v04r01"); deep_set(raw, "event_catalog.sid", self.current_storm["sid"]); deep_set(raw, "event_catalog.basin", ",".join(self.current_storm["basins"])); deep_set(raw, "event_catalog.season", self.current_storm["season"])

    def save_config(self) -> bool:
        """Save the current YAML as a draft, even when setup is incomplete.

        Scientific completeness is enforced by config_args immediately before a
        build or run. Saving must remain available throughout the guided new-model
        workflow and while repairing an imported model.
        """
        if self.session_mode == "new" and self.config_path is not None:
            if not self.ensure_event_workspace_current():
                return False
        if self.session_mode == "new" and self.config_path is None:
            stem = self.canonical_workspace_stem()
            self.configure_model_workspace(self.project_base_dir() / stem, stem)
        if self.config_path is None:
            return self.save_config_as()
        try:
            self.collect_config(); editor = YAML(); editor.preserve_quotes = True; editor.width = 110
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            stream = io.StringIO(); editor.dump(self.raw_config, stream)
            self.atomic_write_text(self.config_path, stream.getvalue())
            self.config = resolved_yaml(self.config_path); self.dirty = False; self.update_title()
            self.validate_config(silent=True); self.refresh_analysis_status()
            errors, _ = self.validation_messages()
            if errors:
                self.statusBar().showMessage(
                    f"Draft YAML saved — {len(errors)} setup item(s) remain before Build & Run.", 8000)
            else:
                self.statusBar().showMessage(f"Saved {self.config_path}", 5000)
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Cannot save configuration", str(exc))
            return False

    def save_config_as(self) -> bool:
        if self.session_mode == "new":
            # A new project has one authoritative YAML. Its location is derived
            # from the selected parent directory and project/event identity.
            return self.save_config()
        initial = self.config_path or self.root / "examples" / "autocf_project.yml"
        selected, _ = QFileDialog.getSaveFileName(self, "Save AutoCF configuration", str(initial), "YAML (*.yml *.yaml)")
        if not selected:
            return False
        target = Path(selected)
        if target.suffix.lower() not in {".yml", ".yaml"}: target = target.with_suffix(".yml")
        self.config_path = target.resolve(); self.config_display.setText(str(self.config_path))
        return self.save_config()

    def mark_dirty(self, *_: Any) -> None:
        if self.loading: return
        self.dirty = True; self.update_title(); self.validate_config(silent=True)

    def update_title(self) -> None:
        marker = " *" if self.dirty else ""
        self.setWindowTitle(f"AutoCF Desktop {APP_VERSION} — {self.project_name.text().strip() or 'Untitled'}{marker}")
        self.run_summary.setText(f"{self.config_path or 'No YAML selected'}\nCPU threads: {'Automatic' if self.cpu_threads.value() == 0 else self.cpu_threads.value()}")

    def validation_messages(self) -> tuple[list[str], list[str]]:
        errors, warnings = [], []
        if self.config_path is None: errors.append("No YAML configuration selected.")
        domain = self.user_path(self.geojson_path.text())
        if not self.geojson_path.text().strip(): errors.append("Study-area GeoJSON is not selected.")
        elif not domain.is_file(): errors.append(f"Study-area file does not exist: {domain}")
        if self.end_time.dateTime() <= self.start_time.dateTime(): errors.append("Simulation end must be later than start.")
        for label, source, field in (("Primary topobathy", self.topo_source, self.topo_file),
                                     ("Topobathy gapfill", self.gap_source, self.gap_file)):
            if source.currentText().strip().lower() in LOCAL_TOPOBATHY_SOURCES:
                raster = field.text().strip()
                if not raster:
                    errors.append(f"{label} is set to 'file' but no raster is selected.")
                elif not self.user_path(raster).is_file():
                    errors.append(f"{label} raster does not exist: {self.user_path(raster)}")
        primary_source = self.topo_source.currentText().strip()
        if primary_source.lower() not in LOCAL_TOPOBATHY_SOURCES | AUTOMATIC_TOPOBATHY_SOURCES and not (
                BUILTIN_PRIMARY_TOPOBATHY_URLS.get(primary_source.upper())
                or self.topo_url.text().strip()):
            errors.append(
                f"Primary topobathy source '{primary_source}' has no remote VRT/COG URL.")
        gap_source = self.gap_source.currentText().strip()
        if gap_source.lower() not in LOCAL_TOPOBATHY_SOURCES | {"none"} and not (
                BUILTIN_GAPFILL_TOPOBATHY_URLS.get(gap_source.upper())
                or self.gap_url.text().strip()):
            errors.append(
                f"Topobathy gapfill source '{gap_source}' has no remote COG URL.")
        target_datum = normalize_vertical_datum_name(self.vertical_datum.currentText())
        for index, entry in enumerate(self.extra_terrain_entries, 3):
            source = entry["source"].currentText().strip().lower()
            if source == "none":
                continue
            if source in LOCAL_TOPOBATHY_SOURCES:
                value = entry["path"].text().strip()
                if not value or not self.user_path(value).is_file():
                    errors.append(f"Terrain Source {index}: select an existing raster file.")
            elif source not in AUTOMATIC_TOPOBATHY_SOURCES and not entry["url"].text().strip():
                errors.append(f"Terrain Source {index}: a remote VRT/COG URL is required.")
            datum = normalize_vertical_datum_name(entry["datum"].currentText())
            if not datum:
                errors.append(f"Terrain Source {index}: the native vertical datum is required.")
            elif datum != target_datum:
                warnings.append(f"Terrain Source {index}: {datum} requires alignment to {target_datum}; review datum provenance.")
            if entry["date"].text().strip():
                try:
                    _utc_datetime(entry["date"].text().strip())
                except ValueError:
                    errors.append(f"Terrain Source {index}: acquisition date must be an ISO date.")
        primary_datum = normalize_vertical_datum_name(self.topo_native_datum.currentText())
        gap_datum = normalize_vertical_datum_name(self.gap_native_datum.currentText())
        if not primary_datum:
            errors.append("Primary topobathy native vertical datum is required.")
        elif primary_datum != target_datum:
            errors.append(
                f"Primary topobathy is {primary_datum}, but the target is {target_datum}; "
                "select the primary native datum or provide a pre-converted raster."
            )
        if gap_source.lower() != "none":
            if not gap_datum:
                errors.append("Topobathy gapfill native vertical datum is required.")
            elif gap_datum != target_datum:
                warnings.append(
                    f"Gapfill is {gap_datum}; AutoCF will first try a rigorous Transformez grid conversion "
                    f"to {target_datum}. If the isolated helper is inaccessible or cannot cover this conversion, "
                    "it will use the approximate "
                    "local-overlap fallback and record the reason and uncertainty."
                )
        event_start = _utc_datetime(
            self.start_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)
        )
        for label, field in (
            ("Primary topobathy", self.topo_acquisition_date),
            ("Topobathy gapfill", self.gap_acquisition_date),
        ):
            declared = field.text().strip()
            if not declared:
                continue
            try:
                acquired = _utc_datetime(declared)
            except ValueError:
                errors.append(f"{label} acquisition date must use YYYY-MM-DD or an ISO date-time.")
                continue
            if acquired > event_start:
                warnings.append(
                    f"{label} was acquired after the modeled event; this run represents modern terrain, "
                    "not a historical reconstruction."
                )
        for label, source, field, local_sources in (
            ("Rainfall", self.rain_source, self.rain_file, {"mrms", "file"}),
            ("Wind", self.wind_source, self.wind_file, {"file"}),
            ("Pressure", self.pressure_source, self.pressure_file, {"file"}),
        ):
            selected_source = source.currentText().strip().lower()
            if selected_source in local_sources:
                forcing_file = field.text().strip()
                if not forcing_file:
                    errors.append(f"{label} source is '{selected_source}' but no NetCDF file is selected.")
                elif not self.user_path(forcing_file).is_file():
                    errors.append(f"{label} forcing file does not exist: {self.user_path(forcing_file)}")
        if self.ocean_source.currentText().strip().lower() == "file":
            if not self.ocean_timeseries.text().strip():
                errors.append("Ocean file forcing requires a time-series CSV or CF NetCDF file.")
            elif not self.user_path(self.ocean_timeseries.text()).is_file():
                errors.append(f"Ocean forcing file does not exist: {self.user_path(self.ocean_timeseries.text())}")
            if self.ocean_datum_policy.currentData() == "unknown":
                errors.append("Ocean file forcing requires a declared source-to-model datum relationship.")
            elif (self.ocean_datum_policy.currentData() == "match"
                  and normalize_vertical_datum_name(self.ocean_file_datum.currentText())
                  != normalize_vertical_datum_name(self.vertical_datum.currentText())):
                errors.append(
                    "Ocean source datum differs from the model target datum; configure a documented offset.")
            if not self.draft_counts.get("ocean_forcing_point", 0):
                errors.append("Ocean file forcing requires at least one identified boundary water-level point.")
        if self.river_source.currentText().strip().lower() == "file":
            if not self.river_timeseries.text().strip():
                errors.append("River file forcing requires a time-series CSV or CF NetCDF file.")
            elif not self.user_path(self.river_timeseries.text()).is_file():
                errors.append(f"River forcing file does not exist: {self.user_path(self.river_timeseries.text())}")
            if not self.draft_counts.get("river_source", 0):
                errors.append("River file forcing requires at least one identified river-source point.")
        if (self.ocean_source.currentText().strip().lower() in {"gtsm", "gtsm_v3"}
                and self.ocean_datum.currentText().strip().upper() != "MSL"):
            errors.append(
                "GTSM is relative to its 1986-2005 mean sea level; select ocean datum MSL "
                "or define an explicit vertical datum offset in YAML.")
        if self.ocean_source.currentText().strip().lower() in {"gtsm", "gtsm_v3"}:
            start_year = self.start_time.dateTime().date().year()
            end_year = self.end_time.dateTime().date().year()
            if start_year < 1950 or end_year > 2024:
                errors.append("GTSM v3 ERA5 reanalysis is available only for 1950-2024.")
        if (self.river_source.currentText().strip().lower() in {"glofas", "glofas_auto"}
                and self.start_time.dateTime().date().year() < 1979):
            errors.append("GloFAS historical discharge is unavailable before 1979.")
        if self.manning_mapping.currentData() == "manual_lulc_classes":
            try:
                self.manual_manning_rows()
            except ValueError as exc:
                errors.append(str(exc))
        if self.dtout.value() <= 0: warnings.append("dtout is zero; map evaluation products may be unavailable.")
        if self.dthisout.value() <= 0 and self.eval_observations.isChecked(): warnings.append("dthisout is zero but gauge evaluation is enabled.")
        for key in ("storevelmax", "storefluxmax"):
            if self.advanced_widgets[key].currentText() == "1" and self.dtmaxout.value() <= 0: errors.append(f"{key}=1 requires dtmaxout > 0.")
        try:
            values = [float(value.strip()) for value in self.impact_thresholds.text().split(",") if value.strip()]
            if not values or any(value < 0 for value in values): errors.append("Impact thresholds must be non-negative numbers.")
        except ValueError:
            errors.append("Impact thresholds must be comma-separated numbers.")
        if self.impact_duration.isChecked():
            try:
                values = [float(value.strip()) for value in self.duration_persistence.text().split(",") if value.strip()]
                if not values or any(value <= 0 for value in values):
                    errors.append("Duration thresholds must be positive numbers of hours.")
            except ValueError:
                errors.append("Duration thresholds must be comma-separated numbers of hours.")
        return errors, warnings

    @staticmethod
    def validation_page(message: str) -> str:
        """Return the workflow page that owns a validation message."""
        normalized = message.casefold()
        routes = (
            (("study-area",), "study"),
            (("simulation end",), "event"),
            (("topobathy", "raster"), "terrain"),
            (("rainfall", "wind", "pressure", "forcing"), "forcing"),
            (("manning",), "surface"),
            (("storevelmax", "storefluxmax", "dtmaxout"), "sfincs"),
            (("impact", "duration threshold"), "impact"),
        )
        for terms, page in routes:
            if any(term in normalized for term in terms):
                return page
        return "project"

    def validate_config(self, _checked: bool = False, silent: bool = False) -> bool:
        errors, warnings = self.validation_messages()
        lines = ["AUTOCF CONFIGURATION VALIDATION", ""] + [f"ERROR  {message}" for message in errors] + [f"WARN   {message}" for message in warnings]
        if not errors: lines.append("PASS   Required GUI checks passed.")
        lines.append("\nBuild performs additional provider, GDAL, HydroMT, and SFINCS validation.")
        self.project_validation.setPlainText("\n".join(lines))
        if self.is_light_theme:
            error_color, warning_color, valid_color = "#b42318", "#9a6700", "#18794e"
        else:
            error_color, warning_color, valid_color = "#ff6b6b", "#ffca3a", "#58d68d"
        if errors: self.validity.setText(f"Configuration: {len(errors)} error(s)"); self.validity.setStyleSheet(f"color:{error_color};font-weight:700")
        elif warnings: self.validity.setText(f"Valid, {len(warnings)} warning(s)"); self.validity.setStyleSheet(f"color:{warning_color};font-weight:700")
        else: self.validity.setText("Configuration valid ✓"); self.validity.setStyleSheet(f"color:{valid_color};font-weight:700")
        if not silent:
            self.log_dock.show()
            self.log_dock.raise_()
            self.job_log_tabs.setCurrentWidget(self.configuration_validation_tab)
            if errors:
                summary = "\n".join(f"• {message}" for message in errors[:8])
                if len(errors) > 8:
                    summary += f"\n• …and {len(errors) - 8} more"
                self.statusBar().showMessage(
                    f"Validation found {len(errors)} error(s). Review Configuration validation in Jobs and Logs.", 8000)
                QMessageBox.warning(
                    self, "Configuration validation",
                    f"Validation found {len(errors)} error(s):\n\n{summary}\n\n"
                    "The complete report is in Jobs and Logs > Configuration validation."
                )
            elif warnings:
                summary = "\n".join(f"• {message}" for message in warnings[:8])
                if len(warnings) > 8:
                    summary += f"\n• …and {len(warnings) - 8} more"
                self.statusBar().showMessage(
                    f"Configuration passed with {len(warnings)} warning(s).", 8000)
                QMessageBox.information(
                    self, "Configuration valid with warnings",
                    f"Required checks passed with {len(warnings)} warning(s):\n\n{summary}\n\n"
                    "The complete report is in Jobs and Logs > Configuration validation."
                )
            else:
                self.statusBar().showMessage("Configuration validation passed.", 8000)
                QMessageBox.information(
                    self, "Configuration validation",
                    "Required configuration checks passed.\n\n"
                    "Build will perform additional provider, GDAL, HydroMT, and SFINCS checks."
                )
        return not errors

    def user_path(self, value: str) -> Path:
        path = Path(value.strip()).expanduser()
        if path.is_absolute():
            return path
        # Relative model paths belong to the YAML that defines them. Resolving
        # them against the application package caused imported ./runs and
        # ./cache folders to appear beside AutoCF instead of beside the model.
        base = self.config_path.parent if self.config_path else self.current_workspace_path()
        return ((base or self.root) / path).resolve()

    @staticmethod
    def safe_workspace_name(value: str) -> str:
        name = "".join(character if character.isalnum() or character in "-_" else "_"
                       for character in value.strip())
        name = re.sub(r"_+", "_", name)
        return name.strip("._-") or "AutoCF_Model"

    @classmethod
    def event_workspace_stem(cls, current_stem: str, previous_event: str,
                             selected_event: str) -> str:
        """Return a stable model name with the newly selected event embedded."""
        seed = cls.safe_workspace_name(current_stem or "")
        old_token = cls.safe_workspace_name(previous_event) if previous_event.strip() else ""
        new_token = cls.safe_workspace_name(selected_event)
        if seed.casefold() in {"autocf_model", "autocf_project", "untitled"}:
            return new_token
        if old_token and re.search(re.escape(old_token), seed, flags=re.IGNORECASE):
            return cls.safe_workspace_name(
                re.sub(re.escape(old_token), new_token, seed, count=1, flags=re.IGNORECASE))
        if re.search(re.escape(new_token), seed, flags=re.IGNORECASE):
            return seed
        seed_parts = seed.split("_")
        event_parts = new_token.split("_")
        overlap = 0
        for size in range(1, min(len(seed_parts), len(event_parts)) + 1):
            if [part.casefold() for part in seed_parts[-size:]] == [
                    part.casefold() for part in event_parts[:size]]:
                overlap = size
        if overlap:
            return cls.safe_workspace_name("_".join(seed_parts + event_parts[overlap:]))
        return cls.safe_workspace_name(f"{seed}__{new_token}")

    def canonical_workspace_stem(self) -> str:
        project = self.project_name.text().strip()
        event = self.event_name.text().strip()
        if event:
            return self.event_workspace_stem(project or "AutoCF_Model", "", event)
        return self.safe_workspace_name(project or "AutoCF_Model")

    def project_base_dir(self) -> Path:
        value = self.default_project_dir.text().strip()
        if not value:
            value = str(self.root)
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    @classmethod
    def workspace_matches_event(cls, workspace: Path, event_name: str) -> bool:
        token = cls.safe_workspace_name(event_name).casefold()
        return bool(token and token in cls.safe_workspace_name(workspace.name).casefold())

    def available_workspace(self, stem: str) -> Path:
        """Return a non-destructive, traceable workspace below the chosen base."""
        base = self.project_base_dir()
        candidate = base / self.safe_workspace_name(stem)
        current = self.current_workspace_path()
        if current == candidate or not candidate.exists():
            return candidate
        index = 2
        while True:
            numbered = base / f"{candidate.name}_{index:02d}"
            if current == numbered or not numbered.exists():
                return numbered
            index += 1

    def configure_model_workspace(self, workspace: Path, stem: str) -> Path:
        """Keep every generated model location inside one workspace."""
        workspace = workspace.expanduser().resolve()
        stem = self.safe_workspace_name(stem or workspace.name)
        old_config = self.config_path
        if old_config is not None and self.raw_config.get("extends"):
            parent = Path(str(self.raw_config["extends"])).expanduser()
            if not parent.is_absolute():
                self.raw_config["extends"] = str((old_config.parent / parent).resolve())

        workspace.mkdir(parents=True, exist_ok=True)
        runs = workspace / "runs"
        cache = workspace / "cache"
        same_workspace = old_config is not None and old_config.parent.resolve() == workspace
        preserve_imported_storage = self.session_mode == "imported" and same_workspace
        if not preserve_imported_storage:
            runs.mkdir(parents=True, exist_ok=True)
            cache.mkdir(parents=True, exist_ok=True)

        self.model_workspace.setText(str(workspace))
        self.project_name.setText(stem)
        if not preserve_imported_storage:
            self.output_dir.setText(str(runs))
            self.cache_dir.setText(str(cache))
        deep_set(self.raw_config, "project.output_dir_suffix", "")
        self.config_path = old_config if same_workspace else workspace / f"{stem}.yml"
        self.config_display.setText(str(self.config_path))
        return workspace

    def ensure_event_workspace_current(self) -> bool:
        """Move a changed event into its own workspace before any operation."""
        event_name = self.event_name.text().strip()
        if not event_name or self.config_path is None:
            return True
        if self.session_mode == "imported":
            # Imported projects retain the YAML's directory. Changing an event
            # updates that model; it must not silently move it elsewhere.
            return True
        workspace_text = self.model_workspace.text().strip()
        workspace = Path(workspace_text) if workspace_text else self.config_path.parent
        if self.workspace_matches_event(workspace, event_name):
            return True
        previous_event = str(deep_get(self.raw_config, "simulation.event_name", "")).strip()
        try:
            new_workspace, removed_hwm, _ = self.activate_event_workspace(previous_event, event_name)
        except Exception as exc:
            QMessageBox.critical(
                self, "Cannot update event workspace",
                "AutoCF could not isolate the selected event before preparing data.\n\n" + str(exc))
            return False

        # A manually changed event cannot retain another storm's catalog ID.
        selected_catalog_event = ""
        if self.current_storm:
            selected_catalog_event = f"{self.current_storm['name'].title()}_{self.current_storm['season']}"
        if selected_catalog_event.casefold() != event_name.casefold():
            self.raw_config.pop("event_catalog", None)
            self.current_storm = None

        for source, field in (
                (self.rain_source, self.rain_file),
                (self.wind_source, self.wind_file),
                (self.pressure_source, self.pressure_file)):
            if source.currentText().strip().lower() not in {"file", "mrms"}:
                field.clear()
        self.forcing_layers = []
        self.forcing_manifest = {}
        self.study_map.set_dem_image(None)
        self.atmosphere_status.setText(
            "Event changed. Atmosphere, ocean, river, observation, and evaluation data will be prepared for the new event.")
        self.mark_dirty()
        self.output.append(
            f"> Event workspace corrected automatically\n"
            f"Event: {event_name}\nWorkspace: {new_workspace}\n"
            f"Removed {removed_hwm} old-event high-water-mark observation(s).\n")
        self.statusBar().showMessage(
            f"Event isolated automatically in {new_workspace.name}", 9000)
        return True

    def model_workspace_for_geojson(self, geojson: Path, base: Path | None = None) -> Path:
        stem = self.safe_workspace_name(geojson.stem)
        parent = (base or self.project_base_dir()).expanduser().resolve()
        return parent if parent.name.casefold() == stem.casefold() else parent / stem

    def current_workspace_path(self) -> Path | None:
        value = self.model_workspace.text().strip()
        if not value:
            return None
        return Path(value).expanduser().resolve()

    def activate_model_workspace(self, geojson: Path) -> Path:
        """Isolate YAML, generated products, and cache for one study-area file."""
        geojson = geojson.expanduser().resolve()
        workspace = geojson.parent.resolve()
        current_workspace = self.current_workspace_path()
        if workspace == current_workspace:
            stem = self.safe_workspace_name(
                self.project_name.text().strip() or workspace.name)
        else:
            stem = self.safe_workspace_name(geojson.stem.removesuffix("_study_area"))
        if workspace.name.casefold() != stem.casefold() and workspace != current_workspace:
            workspace = self.model_workspace_for_geojson(geojson)
        return self.configure_model_workspace(workspace, stem)

    def import_geojson_to_workspace(self, source: Path) -> Path:
        source = source.expanduser().resolve()
        current_workspace = self.current_workspace_path()
        if current_workspace is not None and self.session_mode in {"new", "imported"}:
            workspace = current_workspace
            stem = self.safe_workspace_name(
                self.project_name.text().strip() or workspace.name)
        else:
            stem = self.safe_workspace_name(source.stem)
            workspace = self.model_workspace_for_geojson(source)
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / f"{stem}_study_area.geojson"
        if source.resolve() != target.resolve():
            target.write_bytes(source.read_bytes())
        return target

    def activate_event_workspace(self, previous_event: str,
                                 selected_event: str) -> tuple[Path, int, bool]:
        """Create a clean event workspace while retaining the selected study area."""
        source = self.user_path(self.geojson_path.text()) if self.geojson_path.text().strip() else None
        if source is None or not source.is_file():
            # Event selection can precede study-area creation. In that case the
            # event still becomes the project identity; saving the GeoJSON later
            # will create its isolated workspace in the user-selected base path.
            stem = self.event_workspace_stem(
                self.project_name.text().strip(), previous_event, selected_event)
            workspace = self.available_workspace(stem)
            old_config = self.config_path
            self.configure_model_workspace(workspace, workspace.name)
            self._move_active_yaml(old_config)
            return workspace, 0, True

        current_stem = self.project_name.text().strip() or source.stem.removesuffix("_study_area")
        stem = self.event_workspace_stem(current_stem, previous_event, selected_event)
        workspace = self.available_workspace(stem)
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / f"{workspace.name}_study_area.geojson"
        if source.resolve() == target.resolve():
            return workspace, 0, False

        document = json.loads(source.read_text(encoding="utf-8"))
        retained: list[dict[str, Any]] = []
        removed_hwm = 0
        for feature in document.get("features", []):
            properties = feature.get("properties") or {}
            is_old_hwm = (
                str(properties.get("role", "")).lower() == "observation_point"
                and (str(properties.get("observation_type", "")).lower() == "usgs_high_water_mark"
                     or str(properties.get("source", "")).lower() == "usgs_stn")
            )
            if is_old_hwm:
                removed_hwm += 1
            else:
                retained.append(feature)
        document["features"] = retained
        # Move instead of copy: an event transition must not leave a second,
        # stale study-area file looking authoritative in the parent folder.
        source.replace(target)
        target.write_text(json.dumps(document, indent=1), encoding="utf-8")

        old_config = self.config_path
        self.configure_model_workspace(workspace, workspace.name)
        self._move_active_yaml(old_config)
        self.geojson_path.setText(str(target))
        self.selected_hwm_ids = []
        self.hwm_marks = []
        self.study_map.set_high_water_marks([])
        self.study_map.set_selected_high_water_marks([])
        self.sync_hwm_checks()
        self.hwm_status.setText(
            "Event changed. Load the matching USGS flood event before selecting high-water marks.")

        self.load_domain_preview()
        return workspace, removed_hwm, True

    def _move_active_yaml(self, old_config: Path | None) -> None:
        """Move the prior new-session YAML to the configured canonical path."""
        if old_config is None or self.config_path is None:
            return
        old_config = old_config.expanduser().resolve()
        target = self.config_path.expanduser().resolve()
        if old_config == target or not old_config.is_file():
            return
        if target.exists():
            raise RuntimeError(
                f"Cannot migrate the project YAML because the destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        old_config.replace(target)

    def choose_geojson(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Choose study-area GeoJSON", str(self.project_base_dir()),
            "GeoJSON (*.geojson *.json)")
        if not selected:
            return
        try:
            target = self.import_geojson_to_workspace(Path(selected))
            workspace = self.activate_model_workspace(target)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot create model workspace", str(exc)); return
        self.geojson_path.setText(str(target))
        self.load_domain_preview(); self.mark_dirty(); self.save_config()
        self.statusBar().showMessage(f"Created isolated model workspace: {workspace}", 7000)

    def load_domain_preview(self) -> None:
        try:
            document = json.loads(self.user_path(self.geojson_path.text()).read_text(encoding="utf-8"))
            domain_features = split_study_geojson(document)["domain"]
            if not domain_features:
                raise RuntimeError("No domain Polygon or MultiPolygon found")
            points = [point for feature in domain_features
                      for point in geometry_points(feature.get("geometry"))]
            if len(points) < 3:
                raise RuntimeError("No polygon coordinates found")
            west, east = min(p[0] for p in points), max(p[0] for p in points); south, north = min(p[1] for p in points), max(p[1] for p in points)
            self.domain_geojson, self.domain_bounds = document, (west, south, east, north)
            self.domain_info.setText(f"{len(points):,} vertices | W {west:.5f}, S {south:.5f}, E {east:.5f}, N {north:.5f}")
            domain_display = {"type": "FeatureCollection", "features": domain_features}
        except Exception as exc:
            self.domain_geojson, self.domain_bounds = None, None
            domain_display = {"type": "FeatureCollection", "features": []}
            self.domain_info.setText(f"Cannot preview domain: {exc}")
        track = self.current_storm["points"] if self.current_storm else []
        # Only the AOI polygon belongs in the reference-domain layer. Passing
        # the complete study file here used to redraw ocean lines and points as
        # a misleading blue rectangular/domain layer.
        self.study_map.set_layers(domain_display, track); self.event_map.set_layers(domain_display, track)
        self.sync_draft_from_file(); self.update_dem_extent(); self.validate_config(silent=True)

    # ------------------------------------------------------------------
    # Study-area builder
    # ------------------------------------------------------------------
    def basemap_changed(self, name: str) -> None:
        self.study_map.set_basemap(name)
        self.settings.setValue("basemap", name)

    def location_search_cache(self) -> dict[str, Any]:
        try:
            value = json.loads(str(self.settings.value("location_search_cache", "{}")))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def search_location(self) -> None:
        query = " ".join(self.location_search.text().strip().split())
        if not query:
            self.location_search.setFocus()
            return
        cache = self.location_search_cache()
        cached = cache.get(query.casefold())
        if isinstance(cached, dict):
            self.location_search_found(cached, from_cache=True)
            return
        if self.location_search_pending or (
                self.location_worker is not None and self.location_worker.isRunning()):
            self.statusBar().showMessage("A location search is already running.", 4000)
            return
        self.location_search_pending = True
        self.location_search_button.setEnabled(False)
        self.location_search_button.setText("Searching…")
        # Public geocoding is deliberately click/Enter driven, never autocomplete,
        # and is rate-limited to at most one request per second per application.
        delay_ms = max(0, int((1.05 - (time.monotonic() - self.last_location_request)) * 1000))
        QTimer.singleShot(delay_ms, lambda value=query: self._start_location_search(value))

    def _start_location_search(self, query: str) -> None:
        self.location_search_pending = False
        self.last_location_request = time.monotonic()
        self.location_worker = PlaceSearchWorker(query)
        self.location_worker.found.connect(self.location_search_found)
        self.location_worker.failed.connect(self.location_search_failed)
        self.location_worker.finished.connect(self.location_search_finished)
        self.location_worker.start()

    def location_search_found(self, result: dict[str, Any], from_cache: bool = False) -> None:
        center = result.get("center") or []
        bounds = result.get("bounds")
        if isinstance(bounds, list) and len(bounds) == 4:
            self.searched_location_bounds = [float(value) for value in bounds]
        elif isinstance(center, list) and len(center) == 2:
            lon, lat = map(float, center)
            self.searched_location_bounds = [lon - 0.25, lat - 0.25, lon + 0.25, lat + 0.25]
        else:
            self.searched_location_bounds = None
        # Invalidate candidates and any request tied to the previous location.
        # The next HUC search uses these geocoder bounds directly rather than an
        # in-progress animated map viewport.
        self.huc_search_generation += 1
        if self.huc_worker is not None and self.huc_worker.isRunning():
            self.pending_huc_search_after_worker = True
        self.clear_huc()
        if self.searched_location_bounds:
            self.huc_extent.setCurrentText("Around searched location")
            self.huc_status.setText(
                f"Location set to {result.get('name', self.location_search.text())}. "
                "Click Search watersheds to load HUC boundaries for this location.")
        self.study_map.zoom_to_place(result)
        self.location_search.setToolTip(str(result.get("name") or result.get("query") or "Location"))
        self.statusBar().showMessage(
            f"Map moved to {result.get('name', self.location_search.text())}"
            + (" (cached)" if from_cache else ""), 7000)
        if not from_cache:
            cache = self.location_search_cache()
            cache[str(result.get("query", self.location_search.text())).casefold()] = result
            # Bound persistent storage while retaining the most recently used searches.
            if len(cache) > 100:
                cache = dict(list(cache.items())[-100:])
            self.settings.setValue("location_search_cache", json.dumps(cache))

    def location_search_failed(self, message: str) -> None:
        self._riva_panic()
        self.statusBar().showMessage("Location search failed: " + message, 9000)
        QMessageBox.warning(self, "Location search", message)

    def location_search_finished(self) -> None:
        if self.location_worker is not None:
            self.location_worker.deleteLater()
        self.location_worker = None
        self.location_search_pending = False
        self.location_search_button.setEnabled(True)
        self.location_search_button.setText("Search")

    def start_draw(self, mode: str) -> None:
        self.study_map.set_mode(mode)

    def sync_draft_from_file(self) -> None:
        """Mirror the file's roles into the editable draft so 'keep existing
        points' and manual editing start from what the project already has."""
        parts = split_study_geojson(self.domain_geojson) if self.domain_geojson else {key: [] for key in ROLE_STYLES}
        features = [role_feature(role, feature.get("geometry"), feature.get("properties"))
                    for role, items in parts.items() for feature in items if feature.get("geometry")]
        self.study_map.set_draft({"type": "FeatureCollection", "features": features})

    def load_draft_from_file(self) -> None:
        if not self.domain_geojson:
            QMessageBox.information(self, "No study area loaded", "Select a study-area GeoJSON above first.")
            return
        self.sync_draft_from_file()
        self.study_map.fit_draft()

    def clear_draft(self) -> None:
        if QMessageBox.question(self, "Clear draft", "Discard every drawn shape and point in the draft?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.study_map.clear_draft()

    def map_unavailable(self, diagnostics: dict[str, Any]) -> None:
        if not diagnostics.get("maplibre"):
            cause = ("The MapLibre map library could not be downloaded. The builder needs internet access "
                     "to unpkg.com; check the connection or the proxy settings.")
        elif not diagnostics.get("webgl"):
            cause = ("This computer reports no working WebGL support, which the map requires. Updating the "
                     "graphics driver usually resolves it.")
        else:
            cause = "The map did not finish loading its style or basemap tiles."
        errors = "; ".join(str(item) for item in diagnostics.get("errors", [])[:2])
        message = ("Study-area builder unavailable. " + cause +
                   " Selecting a study-area GeoJSON file above still works normally." +
                   (f" ({errors})" if errors else ""))
        self.draft_status.setText(message)
        self.draw_status.setText(message)
        self.save_study_button.setEnabled(False)
        self.output.append("> Study-area map unavailable\n" + message + "\n")

    def map_state_changed(self, summary: dict[str, Any]) -> None:
        overlay_visible = bool(summary.get("dem_image_loaded") and summary.get("dem_image_visible"))
        self.dem_colorbar.setVisible(self.dem_colorbar_available and overlay_visible)
        self.position_map_overlays()
        error = summary.get("dem_image_error")
        if error:
            if self.active_preview_context == "surface" and hasattr(self, "surface_status"):
                self.surface_status.setText(f"Prepared surface layer could not be displayed on the map: {error}")
            elif self.active_preview_context == "forcing" and hasattr(self, "forcing_map_status"):
                self.forcing_map_status.setText(f"Prepared forcing layer could not be displayed on the map: {error}")
            elif hasattr(self, "terrain_status"):
                self.terrain_status.setText(f"Prepared topobathy could not be displayed on the map: {error}")
        counts = summary.get("counts", {})
        self.draft_counts = counts
        try:
            revision = int(summary.get("rev", -1))
        except (TypeError, ValueError):
            revision = -1
        if revision != self.coordinate_table_revision and self.coordinate_editors:
            self.coordinate_table_revision = revision
            self.study_map.fetch_draft(self.refresh_coordinate_tables)
        self.update_map_legend(summary)
        raw_bounds = summary.get("domain_bounds")
        new_draft_bounds = (tuple(float(value) for value in raw_bounds)
                            if isinstance(raw_bounds, list) and len(raw_bounds) == 4 else None)
        raw_geometries = summary.get("domain_geometries")
        new_draft_geometries = [geometry for geometry in raw_geometries
                                if isinstance(geometry, dict)] if isinstance(raw_geometries, list) else []
        bounds_changed = (new_draft_bounds != self.draft_domain_bounds or
                          new_draft_geometries != self.draft_domain_geometries)
        self.draft_domain_bounds = new_draft_bounds
        self.draft_domain_geometries = new_draft_geometries
        self.draft_domain_known = True
        parts = [f"{counts.get('domain', 0)} domain polygon", f"{counts.get('ocean_boundary', 0)} ocean line(s)",
                 f"{counts.get('ocean_forcing_point', 0)} water-level point(s)"]
        optional = counts.get("observation_point", 0), counts.get("river_source", 0)
        if any(optional):
            parts.append(f"{optional[0]} observation, {optional[1]} river point(s)")
        adaptation_count = counts.get("roughness_override", 0)
        if adaptation_count:
            parts.append(f"{adaptation_count} NBS/adaptation polygon(s)")
        required = [("domain polygon", "domain"), ("ocean boundary line", "ocean_boundary")]
        if self.ocean_source.currentText().strip().lower() not in {"gtsm", "gtsm_v3"}:
            required.append(("water-level point", "ocean_forcing_point"))
        missing = [name for name, key in required if not counts.get(key)]
        suffix = "Ready to save." if not missing else "Still needed: " + ", ".join(missing) + "."
        self.draft_status.setText("Draft: " + ", ".join(parts) + ". " + suffix)
        self.save_study_button.setEnabled(not missing)
        mode, pending = summary.get("mode", "none"), summary.get("pending", 0)
        if mode in ("polygon", "rectangle", "ocean_boundary", "roughness_override"):
            self.draw_status.setText(f"Drawing {mode.replace('_', ' ')} — {pending} vertex/vertices placed.")
        elif mode in ("ocean_forcing_point", "observation_point", "river_source"):
            self.draw_status.setText(f"Click the map to place {mode.replace('_', ' ')}s.")
        else:
            self.draw_status.setText("Not drawing.")
        self.wl_status.setText(f"{counts.get('ocean_forcing_point', 0)} water-level point(s) in the draft.")
        self.extra_status.setText(f"{optional[0]} observation point(s) in the draft.")
        if not self.usgs_sites:
            self.usgs_status.setText(f"{optional[1]} river source point(s) in the draft.")
        if hasattr(self, "adaptation_status") and mode != "roughness_override":
            self.adaptation_status.setText(
                f"{adaptation_count} NBS/adaptation polygon(s) in the study-area draft. "
                "Save the study-area GeoJSON after editing." if adaptation_count else
                "No NBS/adaptation polygons in the study-area draft.")
        try:
            incoming_huc_revision = int(summary.get("huc_selection_rev", 0))
        except (TypeError, ValueError):
            incoming_huc_revision = 0
        # WebEngine calls are asynchronous.  A summary requested before a list
        # click can arrive after that click; accepting it would immediately
        # clear the user's highlighted HUC rows.  Only an acknowledgement of
        # the current revision, or a newer map click, is authoritative.
        if incoming_huc_revision >= self.huc_selection_revision:
            self.huc_selection_revision = incoming_huc_revision
            selected_hucs = [str(value) for value in summary.get("hucs", [])]
            if not selected_hucs and summary.get("huc"):
                selected_hucs = [str(summary["huc"])]
            if selected_hucs != self.selected_hucs:
                self.selected_hucs = selected_hucs
                self.current_huc = selected_hucs[0] if selected_hucs else None
                selected_set = set(selected_hucs)
                self.huc_list.blockSignals(True)
                for row in range(self.huc_list.count()):
                    item = self.huc_list.item(row)
                    item.setSelected(str(item.data(Qt.ItemDataRole.UserRole)) in selected_set)
                self.huc_list.blockSignals(False)
        self.update_huc_selection_controls()
        selectable_station_ids = {
            str(station["id"]) for station in self.coops_stations
            if station.get("coverage_valid") is True
        }
        selected = [
            str(value) for value in summary.get("stations", [])
            if str(value) in selectable_station_ids
        ]
        if selected != self.selected_station_ids:
            self.selected_station_ids = selected
            self.boundary_station_selections[self.active_water_level_provider] = list(selected)
            self.sync_station_checks()
            self.sync_selected_waterlevel_draft()
        selected_obs_noaa = [
            str(value) for value in summary.get("obs_noaa", [])
            if str(value) in selectable_station_ids
        ]
        if selected_obs_noaa != self.selected_obs_noaa_ids:
            self.selected_obs_noaa_ids = selected_obs_noaa
            self.observation_station_selections[self.active_water_level_provider] = list(selected_obs_noaa)
            self.sync_observation_noaa_checks()
            self.sync_selected_noaa_observation_draft()
        selected_hwm = [str(value) for value in summary.get("hwm", [])]
        if selected_hwm != self.selected_hwm_ids:
            self.selected_hwm_ids = selected_hwm
            self.sync_hwm_checks()
            self.sync_selected_hwm_draft()
        chosen_usgs = [str(value) for value in summary.get("usgs", [])]
        if chosen_usgs != self.selected_usgs_ids:
            self.selected_usgs_ids = chosen_usgs
            self.sync_usgs_checks()
        if bounds_changed and self.coops_stations and hasattr(self, "station_list"):
            self.populate_stations()
            if hasattr(self, "obs_noaa_list"):
                self.populate_observation_noaa()

    def study_geojson_default(self) -> Path:
        current = self.geojson_path.text().strip()
        workspace = self.current_workspace_path()
        if current:
            candidate = self.user_path(current)
            # Preparing an edited draft must update the active model's GeoJSON,
            # not create another same-named workspace below it.
            if workspace is not None and candidate.parent.resolve() == workspace:
                return candidate
            if candidate.parent.name.casefold() == self.safe_workspace_name(candidate.stem).casefold():
                return candidate
        if self.session_mode == "new" and workspace is None:
            stem = self.canonical_workspace_stem()
            workspace = self.project_base_dir() / stem
            return workspace / f"{stem}_study_area.geojson"
        if workspace is not None:
            stem = self.safe_workspace_name(
                self.project_name.text().strip() or workspace.name)
            return workspace / f"{stem}_study_area.geojson"
        stem = self.safe_workspace_name(self.project_name.text().strip() or "AutoCF_Model")
        workspace = self.model_workspace_for_geojson(Path(f"{stem}.geojson"))
        return workspace / f"{stem}_study_area.geojson"

    def save_study_geojson(self) -> None:
        self.study_map.fetch_draft(self._save_study_geojson_payload)

    def prepare_study_geojson(self) -> None:
        """Validate and write the map draft to the project's stable study-area path.

        This is the guided workflow action used by the UI. Repeated preparation
        updates the same project GeoJSON instead of creating numbered copies.
        """
        target = self.study_geojson_default()
        self.study_map.fetch_draft(
            lambda payload, path=str(target): self._save_study_geojson_payload(
                payload, path, continue_to="terrain"))

    def _save_study_geojson_payload(self, payload: Any, prepared_target: str | None = None,
                                    continue_to: str | None = None) -> bool:
        if not payload:
            QMessageBox.warning(self, "Map not ready", "The interactive map has not finished loading.")
            return False
        try:
            document = json.loads(payload)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot read draft", str(exc)); return False
        parts = split_study_geojson(document)
        problems = []
        if len(parts["domain"]) != 1:
            problems.append(f"AutoCF needs exactly one domain polygon; the draft has {len(parts['domain'])}.")
        if not parts["ocean_boundary"]:
            problems.append("AutoCF needs at least one ocean boundary line.")
        if (self.ocean_source.currentText().strip().lower() not in {"gtsm", "gtsm_v3"}
                and not parts["ocean_forcing_point"]):
            problems.append("AutoCF needs at least one water-level (ocean forcing) point.")
        if problems:
            QMessageBox.warning(self, "Draft incomplete", "\n".join(problems)); return False
        features: list[dict[str, Any]] = []
        prefixes = {"ocean_forcing_point": "bnd", "observation_point": "obs", "river_source": "riv",
                    "roughness_override": "nbs"}
        for role in ("domain", "ocean_boundary", "ocean_forcing_point", "observation_point",
                     "river_source", "roughness_override"):
            for index, feature in enumerate(parts[role]):
                properties = {key: value for key, value in (feature.get("properties") or {}).items()
                              if key not in ("role", "_seq") and value is not None}
                if role in prefixes:
                    identifier = str(properties.get("ID") or f"{prefixes[role]}{index + 1:03d}")
                    properties["ID"] = identifier
                    properties.setdefault("name", identifier)
                features.append(role_feature(role, feature["geometry"], properties))
        default = self.study_geojson_default()
        default.parent.mkdir(parents=True, exist_ok=True)
        if prepared_target:
            selected = prepared_target
        else:
            selected, _ = QFileDialog.getSaveFileName(
                self, "Save study-area GeoJSON", str(default), "GeoJSON (*.geojson *.json)")
            if not selected:
                return False
        requested = Path(selected)
        if not prepared_target:
            # Save As chooses this model's location only. It must never rewrite
            # the application-wide Default project folder or cause repeated
            # same-name folders when the chosen path is already a workspace.
            requested_parent = requested.parent.expanduser().resolve()
            requested_stem = self.safe_workspace_name(requested.stem)
            current_workspace = self.current_workspace_path()
            if ((current_workspace is not None and requested_parent == current_workspace)
                    or requested_parent.name.casefold() == requested_stem.casefold()):
                workspace = requested_parent
            else:
                workspace = self.model_workspace_for_geojson(requested, requested_parent)
            target = workspace / requested.name
        else:
            target = requested
        if target.suffix.lower() not in (".geojson", ".json"):
            target = target.with_suffix(".geojson")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=1), encoding="utf-8")
        except Exception as exc:
            QMessageBox.critical(self, "Cannot save study area", str(exc)); return False
        workspace = self.activate_model_workspace(target)
        self.geojson_path.setText(str(target))
        self.load_domain_preview()
        self.mark_dirty()
        self.save_config()
        self.output.append(f"> Wrote study-area GeoJSON\n{target}\n"
                           f"{len(parts['ocean_forcing_point'])} water-level point(s), "
                           f"{len(parts['ocean_boundary'])} ocean boundary line(s)\n"
                           f"> Isolated model workspace\n{workspace}\n")
        self.statusBar().showMessage(
            f"Study-area prepared in its own model workspace: {workspace}", 7000)
        if self.session_mode == "new":
            self.project_session_status.setText(
                f"New model setup: event and Study Area saved. Continue with Terrain.\n{self.config_path}")
        if continue_to:
            self.go_to(continue_to)
        return True

    # -- watershed (HUC) ---------------------------------------------------
    def search_huc(self) -> None:
        if self.huc_worker and self.huc_worker.isRunning():
            return
        extent = self.huc_extent.currentText()
        if extent == "Current map view":
            self.study_map.fetch_view_bounds(lambda payload: self._start_huc(json.loads(payload) if payload else None))
        elif extent == "Around searched location":
            self._start_huc(self.searched_location_bounds)
        elif extent == "Around the loaded domain":
            self._start_huc(list(self.domain_bounds) if self.domain_bounds else None)
        else:
            self.study_map.fetch_draft(lambda payload: self._start_huc(self._draft_bounds(payload)))

    def _draft_bounds(self, payload: Any) -> list[float] | None:
        try:
            points = geojson_points(json.loads(payload))
        except Exception:
            return None
        if not points:
            return None
        return [min(p[0] for p in points), min(p[1] for p in points),
                max(p[0] for p in points), max(p[1] for p in points)]

    def _start_huc(self, bounds: list[float] | None) -> None:
        if not bounds:
            self.huc_status.setText("No search extent available. Pan the map or load a domain first."); return
        west, south, east, north = bounds
        if (east - west) > 12 or (north - south) > 12:
            self.huc_status.setText("Search extent is too large. Zoom in to roughly a state-sized area first."); return
        pad = 0.02
        layer, field = HUC_LEVELS[self.huc_level.currentText()]
        self.huc_status.setText("Streaming USGS watershed boundaries…")
        self.huc_search_button.setEnabled(False)
        self.huc_search_generation += 1
        generation = self.huc_search_generation
        worker = HucWorker((west - pad, south - pad, east + pad, north + pad), layer, field)
        self.huc_worker = worker
        worker.loaded.connect(
            lambda records, level, token=generation: self.huc_loaded(records, level, token))
        worker.failed.connect(
            lambda message, token=generation: self.huc_failed(message, token))
        worker.finished.connect(
            lambda active=worker, token=generation: self.huc_worker_finished(active, token))
        worker.start()

    def next_huc_selection_revision(self) -> int:
        self.huc_selection_revision += 1
        return self.huc_selection_revision

    def huc_loaded(self, records: list[dict[str, Any]], level: str,
                   generation: int | None = None) -> None:
        if generation is not None and generation != self.huc_search_generation:
            return
        self.huc_search_button.setEnabled(True)
        self.huc_records = records
        self.huc_list.blockSignals(True)
        self.huc_list.clear()
        self.current_huc = None
        self.selected_hucs = []
        features = []
        for record in records:
            area = record.get("area_km2")
            area_text = f" — {float(area):,.0f} km²" if isinstance(area, (int, float)) else ""
            item = QListWidgetItem(f"{record['name']} — {level} {record['code']}{area_text}")
            item.setData(Qt.ItemDataRole.UserRole, record["code"])
            self.huc_list.addItem(item)
            features.append({"type": "Feature", "geometry": record["geometry"],
                             "properties": {"code": record["code"], "name": record["name"], "level": level}})
        self.huc_list.blockSignals(False)
        revision = self.next_huc_selection_revision()
        self.study_map.set_huc_features(features, revision)
        self.study_map.set_mode("huc")
        self.huc_status.setText(
            (f"{len(records)} {level} watershed(s) in the search extent. Select one or more adjacent rows "
             "(Ctrl/Shift-click), or click polygons on the map, then use or merge the selection as the domain.")
            if records else "No watersheds intersect the search extent.")
        self.update_huc_selection_controls()

    def huc_failed(self, message: str, generation: int | None = None) -> None:
        if generation is not None and generation != self.huc_search_generation:
            return
        self._riva_panic()
        self.huc_search_button.setEnabled(True)
        self.huc_status.setText("Watershed service error: " + message)

    def huc_worker_finished(self, worker: HucWorker, generation: int) -> None:
        if self.huc_worker is worker:
            self.huc_worker = None
        worker.deleteLater()
        if self.pending_huc_search_after_worker:
            self.pending_huc_search_after_worker = False
            self.huc_search_button.setEnabled(True)
            QTimer.singleShot(0, self.search_huc)
        elif generation != self.huc_search_generation:
            self.huc_search_button.setEnabled(True)

    def huc_selection_changed(self) -> None:
        codes = [str(item.data(Qt.ItemDataRole.UserRole)) for item in self.huc_list.selectedItems()]
        self.selected_hucs = codes
        self.current_huc = codes[0] if codes else None
        revision = self.next_huc_selection_revision()
        self.study_map.select_hucs(codes, revision)
        self.update_huc_selection_controls()

    def update_huc_selection_controls(self) -> None:
        count = len(self.selected_hucs)
        self.huc_use_button.setEnabled(count > 0)
        self.huc_use_button.setText(
            f"Merge {count} selected as domain" if count > 1 else "Use selected as domain")

    def use_huc_as_domain(self) -> None:
        selected = set(self.selected_hucs)
        records = [record for record in self.huc_records if str(record.get("code")) in selected]
        if not records:
            QMessageBox.information(
                self, "No watershed selected", "Select one or more watersheds in the list or on the map first.")
            return
        try:
            geometry = (records[0]["geometry"] if len(records) == 1 else
                        merge_adjacent_polygon_geometries([record["geometry"] for record in records]))
        except Exception as exc:
            QMessageBox.warning(self, "Cannot merge watersheds", str(exc))
            return
        codes = [str(record["code"]) for record in records]
        names = [str(record["name"]) for record in records]
        feature = {
            "type": "Feature", "geometry": geometry,
            "properties": {
                "code": ",".join(codes), "name": " + ".join(names),
                "huc_codes": codes, "huc_names": names, "huc_count": len(records),
            },
        }
        self._apply_huc_geometry(feature)

    def _apply_huc_geometry(self, payload: Any) -> None:
        if not payload:
            QMessageBox.information(self, "No watershed selected", "Select a watershed in the list or on the map first.")
            return
        try:
            feature = json.loads(payload) if isinstance(payload, str) else dict(payload)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot read watershed", str(exc)); return
        properties = feature.get("properties") or {}
        self.study_map.replace_domain(role_feature("domain", feature["geometry"], {
            "source": "usgs_wbd", "huc": properties.get("code", ""), "huc_name": properties.get("name", ""),
            "huc_codes": properties.get("huc_codes", [properties.get("code", "")]),
            "huc_names": properties.get("huc_names", [properties.get("name", "")]),
            "huc_count": int(properties.get("huc_count", 1))}),
            reset_coast=True)
        # Keep the HUC candidates and selection visible after creating the
        # domain.  They are the user's traceability record and let the user
        # add/remove a watershed and merge again without repeating the search.
        self.study_map.set_mode("huc")
        self.study_map.fit_draft()
        selected_count = len(self.selected_hucs)
        self.huc_status.setText(
            f"{selected_count} selected watershed{'s were' if selected_count != 1 else ' was'} dissolved into the active draft domain. "
            "The selected HUC IDs and outlines remain visible so you can verify or adjust the merge. "
            "The previous ocean boundary and water-level points were cleared because they belonged to the old "
            "domain. Draw the new seaward boundary and add its water-level point, then prepare the GeoJSON.")
        self.update_huc_selection_controls()

    def clear_huc(self) -> None:
        self.huc_records = []
        self.current_huc = None
        self.selected_hucs = []
        self.huc_list.blockSignals(True)
        self.huc_list.clear()
        self.huc_list.blockSignals(False)
        revision = self.next_huc_selection_revision()
        self.study_map.set_huc_features([], revision)
        self.study_map.set_mode("none")
        self.huc_status.setText("No watersheds loaded.")
        self.update_huc_selection_controls()

    # -- Catalog water-level stations --------------------------------------
    def waterlevel_provider_label(self, provider: str | None = None) -> str:
        key = provider or self.active_water_level_provider
        return str(WATER_LEVEL_PROVIDERS.get(key, {}).get("label", key))

    def waterlevel_provider_changed(self, provider: str) -> None:
        if provider not in WATER_LEVEL_PROVIDERS or provider == self.active_water_level_provider:
            return
        old = self.active_water_level_provider
        self.boundary_station_selections[old] = list(self.selected_station_ids)
        self.observation_station_selections[old] = list(self.selected_obs_noaa_ids)
        self.active_water_level_provider = provider
        for combo_name in ("wl_catalog_provider", "obs_catalog_provider"):
            combo = getattr(self, combo_name, None)
            if combo is not None and combo.currentData() != provider:
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(provider))
                combo.blockSignals(False)
        self.coops_stations = list(self.water_level_catalogs.get(provider, []))
        self.selected_station_ids = list(self.boundary_station_selections.get(provider, []))
        self.selected_obs_noaa_ids = list(self.observation_station_selections.get(provider, []))
        self.study_map.set_selected_stations(self.selected_station_ids)
        self.study_map.set_selected_observation_noaa(self.selected_obs_noaa_ids)
        self.populate_stations()
        self.populate_observation_noaa()
        if provider not in self.water_level_catalogs:
            self.load_coops(False)
        else:
            self.start_water_level_coverage_check()

    def invalidate_water_level_coverage(self, _value: QDateTime | None = None) -> None:
        for catalog in self.water_level_catalogs.values():
            for station in catalog:
                station["coverage_valid"] = None
                station["coverage_message"] = "event period changed; reload/check station coverage"
                station.pop("coverage_window", None)
        if hasattr(self, "station_list"):
            self.populate_stations()
        if hasattr(self, "obs_noaa_list"):
            self.populate_observation_noaa()

    def waterlevel_mode_changed(self, index: int, checked: bool) -> None:
        if not checked:
            return
        if index == 0:
            self.study_map.set_mode("none")
            # Restore only this role. Reloading the complete file here used to
            # silently replace a newly selected HUC/drawn AOI and its ocean
            # line with the older file draft.
            points = split_study_geojson(self.domain_geojson)["ocean_forcing_point"] \
                if self.domain_geojson else []
            self.study_map.set_role_features("ocean_forcing_point", [
                role_feature("ocean_forcing_point", feature.get("geometry"), feature.get("properties"))
                for feature in points if feature.get("geometry")])
        elif index == 1:
            self.study_map.set_mode("station")
            if not self.coops_stations:
                self.load_coops(False)
        else:
            self.study_map.set_mode("ocean_forcing_point")

    def load_coops(self, refresh: bool) -> None:
        if ((self.coops_worker and self.coops_worker.isRunning()) or
                (self.global_water_level_worker and self.global_water_level_worker.isRunning())):
            return
        provider = self.active_water_level_provider
        label = self.waterlevel_provider_label(provider)
        self.coops_button.setEnabled(False)
        if hasattr(self, "obs_noaa_load"):
            self.obs_noaa_load.setEnabled(False)
            self.obs_noaa_status.setText(f"Loading {label} station catalog…")
        self.wl_status.setText(f"Loading {label} water-level station catalog…")
        if provider == "noaa_coops":
            self.coops_worker = CoopsWorker(self.root / "cache" / "noaa_coops_stations.json", refresh)
            self.coops_worker.loaded.connect(self.coops_loaded)
            self.coops_worker.failed.connect(self.coops_failed)
            self.coops_worker.start()
        else:
            cache = self.root / "cache" / "water_level_stations" / f"{provider}.json"
            self.global_water_level_worker = GlobalWaterLevelWorker(provider, cache, refresh)
            self.global_water_level_worker.loaded.connect(self.global_water_level_loaded)
            self.global_water_level_worker.failed.connect(self.global_water_level_failed)
            self.global_water_level_worker.start()

    def coops_loaded(self, stations: list[dict[str, Any]], source: str) -> None:
        for station in stations:
            station.update({"provider": "noaa_coops", "dataset": "", "datum": ""})
        self.water_level_catalog_loaded("noaa_coops", stations, source)

    def global_water_level_loaded(self, provider: str, stations: list[dict[str, Any]], source: str) -> None:
        self.water_level_catalog_loaded(provider, stations, source)

    def water_level_catalog_loaded(self, provider: str, stations: list[dict[str, Any]], source: str) -> None:
        self.coops_button.setEnabled(True)
        if hasattr(self, "obs_noaa_load"):
            self.obs_noaa_load.setEnabled(True)
        self.water_level_catalogs[provider] = stations
        if provider != self.active_water_level_provider:
            return
        self.coops_stations = stations
        self.coops_source = source
        self.populate_stations()
        if hasattr(self, "obs_noaa_list"):
            self.populate_observation_noaa()
        self.start_water_level_coverage_check()

    def coops_failed(self, message: str) -> None:
        self.water_level_catalog_failed("noaa_coops", message)

    def global_water_level_failed(self, provider: str, message: str) -> None:
        self.water_level_catalog_failed(provider, message)

    def water_level_catalog_failed(self, provider: str, message: str) -> None:
        self._riva_panic()
        self.coops_button.setEnabled(True)
        label = self.waterlevel_provider_label(provider)
        self.wl_status.setText(f"{label} error: {message}")
        if hasattr(self, "obs_noaa_load"):
            self.obs_noaa_load.setEnabled(True)
            self.obs_noaa_status.setText(f"{label} error: {message}")

    def water_level_scope_changed(self, _checked: bool) -> None:
        self.populate_stations()
        if hasattr(self, "obs_noaa_list"):
            self.populate_observation_noaa()
        self.start_water_level_coverage_check()

    def _coverage_candidates(self) -> list[dict[str, Any]]:
        candidates = self.visible_stations()
        if len(candidates) <= 100:
            return candidates
        active_bounds = self.draft_domain_bounds or self.domain_bounds
        if not active_bounds:
            return []
        west, south, east, north = active_bounds
        center_lon, center_lat = (west + east) / 2.0, (south + north) / 2.0
        return sorted(
            candidates,
            key=lambda station: (
                (float(station["lat"]) - center_lat) ** 2
                + ((float(station["lon"]) - center_lon) * math.cos(math.radians(center_lat))) ** 2
            ),
        )[:100]

    def start_water_level_coverage_check(self) -> None:
        if not self.coops_stations or not hasattr(self, "start_time"):
            return
        if self.water_level_coverage_worker and self.water_level_coverage_worker.isRunning():
            return
        candidates = self._coverage_candidates()
        candidate_ids = {str(station["id"]) for station in candidates}
        for station in self.coops_stations:
            station["coverage_valid"] = None
            station["coverage_message"] = (
                "checking event coverage…" if str(station["id"]) in candidate_ids
                else "coverage not checked; limit the list to stations near the domain"
            )
        self.populate_stations()
        if hasattr(self, "obs_noaa_list"):
            self.populate_observation_noaa()
        if not candidates:
            self.wl_status.setText(
                "Coverage was not checked because more than 100 stations are displayed. "
                "Enable ‘Only stations near the domain’ and load the catalog again.")
            return
        self.coops_add_button.setEnabled(False)
        if hasattr(self, "obs_noaa_add"):
            self.obs_noaa_add.setEnabled(False)
        start_text = self.start_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)
        end_text = self.end_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)
        self.wl_status.setText(
            f"Checking {len(candidates)} {self.waterlevel_provider_label()} station(s) against the event period…")
        if hasattr(self, "obs_noaa_status"):
            self.obs_noaa_status.setText(self.wl_status.text())
        self.water_level_coverage_worker = WaterLevelCoverageWorker(
            self.active_water_level_provider, candidates, start_text, end_text)
        self.water_level_coverage_worker.checked.connect(self.water_level_coverage_checked)
        self.water_level_coverage_worker.start()

    def water_level_coverage_checked(self, provider: str,
                                     results: dict[str, dict[str, Any]], window_text: str) -> None:
        current_window = (
            f"{self.start_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)} to "
            f"{self.end_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)}"
        )
        if window_text != current_window:
            QTimer.singleShot(100, self.start_water_level_coverage_check)
            return
        catalog = self.water_level_catalogs.get(provider, [])
        for station in catalog:
            result = results.get(str(station["id"]))
            if result:
                station.update(result)
                station["coverage_window"] = window_text
        if provider != self.active_water_level_provider:
            QTimer.singleShot(100, self.start_water_level_coverage_check)
            return
        valid_ids = {
            str(station["id"]) for station in self.coops_stations
            if station.get("coverage_valid") is True
        }
        removed_boundary = set(self.selected_station_ids) - valid_ids
        removed_observations = set(self.selected_obs_noaa_ids) - valid_ids
        self.selected_station_ids = [identifier for identifier in self.selected_station_ids if identifier in valid_ids]
        self.selected_obs_noaa_ids = [identifier for identifier in self.selected_obs_noaa_ids if identifier in valid_ids]
        self.boundary_station_selections[provider] = list(self.selected_station_ids)
        self.observation_station_selections[provider] = list(self.selected_obs_noaa_ids)
        self.study_map.set_selected_stations(self.selected_station_ids)
        self.study_map.set_selected_observation_noaa(self.selected_obs_noaa_ids)
        if removed_boundary:
            self.sync_selected_waterlevel_draft()
        if removed_observations:
            self.sync_selected_noaa_observation_draft()
        self.populate_stations()
        if hasattr(self, "obs_noaa_list"):
            self.populate_observation_noaa()
        self.coops_add_button.setEnabled(True)
        if hasattr(self, "obs_noaa_add"):
            self.obs_noaa_add.setEnabled(True)

    def visible_stations(self) -> list[dict[str, Any]]:
        active_bounds = self.draft_domain_bounds or self.domain_bounds
        if not self.coops_near_domain.isChecked() or not active_bounds:
            return self.coops_stations
        west, south, east, north = active_bounds
        pad = max(0.2, float(self.ocean_distance.value()) / 111.0)
        return [station for station in self.coops_stations
                if west - pad <= station["lon"] <= east + pad and south - pad <= station["lat"] <= north + pad]

    def active_aoi_geometries(self) -> list[dict[str, Any]]:
        if self.draft_domain_known:
            return self.draft_domain_geometries
        if not self.domain_geojson:
            return []
        return [feature["geometry"] for feature in split_study_geojson(self.domain_geojson)["domain"]
                if isinstance(feature.get("geometry"), dict)]

    def observation_stations_in_aoi(self) -> list[dict[str, Any]]:
        geometries = self.active_aoi_geometries()
        if not geometries:
            return []
        return [station for station in self.coops_stations
                if any(point_in_geojson_geometry(float(station["lon"]), float(station["lat"]), geometry)
                       for geometry in geometries)]

    def _water_level_item(self, station: dict[str, Any], selected: bool) -> QListWidgetItem:
        valid = station.get("coverage_valid")
        message = str(station.get("coverage_message") or "coverage not checked")
        prefix = "✓" if valid is True else ("✕" if valid is False else "…")
        label = f"{prefix} {station['name']} — {station['id']}"
        if station["state"]:
            label += f" ({station['state']})"
        if valid is False:
            label += " — invalid for event"
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, station["id"])
        item.setToolTip(message)
        flags = item.flags()
        if valid is True:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        else:
            flags &= ~Qt.ItemFlag.ItemIsUserCheckable
            item.setForeground(QColor("#e57373" if valid is False else "#9aa0a6"))
        item.setFlags(flags)
        item.setCheckState(Qt.CheckState.Checked if valid is True and selected else Qt.CheckState.Unchecked)
        return item

    def _water_level_coverage_summary(self, stations: list[dict[str, Any]]) -> str:
        valid = sum(station.get("coverage_valid") is True for station in stations)
        invalid = sum(station.get("coverage_valid") is False for station in stations)
        unchecked = len(stations) - valid - invalid
        return f"{valid} valid, {invalid} invalid, {unchecked} checking/not checked for the selected event"

    def populate_stations(self) -> None:
        stations = self.visible_stations()
        self.loading_stations = True
        self.station_list.clear()
        for station in stations:
            self.station_list.addItem(self._water_level_item(
                station, station["id"] in self.selected_station_ids))
        self.loading_stations = False
        self.study_map.set_stations([
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [station["lon"], station["lat"]]},
             "properties": {"id": station["id"], "name": station["name"], "state": station["state"],
                            "provider_label": self.waterlevel_provider_label(),
                            "coverage_valid": station.get("coverage_valid"),
                            "coverage_message": station.get("coverage_message", "coverage not checked")}}
            for station in stations])
        active_bounds = self.draft_domain_bounds or self.domain_bounds
        scope = "near the draft domain" if self.coops_near_domain.isChecked() and active_bounds else "in the catalog"
        self.wl_status.setText(
            f"{len(stations)} {self.waterlevel_provider_label()} station(s) {scope} from "
            f"{getattr(self, 'coops_source', 'cache')}; {self._water_level_coverage_summary(stations)}. "
            "Only stations marked ✓ can be selected.")

    def station_item_changed(self, item: QListWidgetItem) -> None:
        if self.loading_stations:
            return
        identifier = item.data(Qt.ItemDataRole.UserRole)
        station = next((entry for entry in self.coops_stations if entry["id"] == identifier), None)
        if not station or station.get("coverage_valid") is not True:
            return
        checked = item.checkState() == Qt.CheckState.Checked
        if checked and identifier not in self.selected_station_ids:
            self.selected_station_ids.append(identifier)
        elif not checked and identifier in self.selected_station_ids:
            self.selected_station_ids.remove(identifier)
        self.boundary_station_selections[self.active_water_level_provider] = list(self.selected_station_ids)
        self.study_map.set_selected_stations(self.selected_station_ids)
        self.sync_selected_waterlevel_draft()

    def sync_selected_waterlevel_draft(self) -> None:
        chosen = [station for station in self.coops_stations
                  if str(station["id"]) in self.selected_station_ids]
        features = [
            role_feature(
                "ocean_forcing_point",
                {"type": "Point", "coordinates": [station["lon"], station["lat"]]},
                {"ID": f"bnd_{self.active_water_level_provider}_{station['id']}", "name": station["name"],
                 "station_id": station["id"], "noaa_id": station["id"] if self.active_water_level_provider == "noaa_coops" else "",
                 "provider": self.active_water_level_provider, "dataset": station.get("dataset", ""),
                 "datum": station.get("datum", ""), "source": self.active_water_level_provider})
            for station in chosen]
        self.study_map.sync_managed_features(
            "ocean_forcing_point", features, f"{self.active_water_level_provider}_boundary_selection")
        if chosen:
            self.ocean_source.setCurrentText("selected_stations")

    def clear_waterlevel_points(self) -> None:
        self.selected_station_ids = []
        self.boundary_station_selections = {}
        self.study_map.set_selected_stations([])
        self.sync_station_checks()
        self.study_map.clear_role("ocean_forcing_point")

    def sync_station_checks(self) -> None:
        self.loading_stations = True
        for row in range(self.station_list.count()):
            item = self.station_list.item(row)
            state = Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in self.selected_station_ids else Qt.CheckState.Unchecked
            if item.checkState() != state:
                item.setCheckState(state)
        self.loading_stations = False

    def populate_observation_noaa(self) -> None:
        stations = self.observation_stations_in_aoi()
        visible_ids = {str(station["id"]) for station in stations}
        removed = set(self.selected_obs_noaa_ids) - visible_ids
        if removed:
            self.selected_obs_noaa_ids = [identifier for identifier in self.selected_obs_noaa_ids
                                          if identifier in visible_ids]
            self.observation_station_selections[self.active_water_level_provider] = list(self.selected_obs_noaa_ids)
            self.sync_selected_noaa_observation_draft()
        self.loading_obs_noaa = True
        self.obs_noaa_list.clear()
        for station in stations:
            self.obs_noaa_list.addItem(self._water_level_item(
                station, station["id"] in self.selected_obs_noaa_ids))
        self.loading_obs_noaa = False
        self.study_map.set_observation_noaa([
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [station["lon"], station["lat"]]},
             "properties": {"id": station["id"], "name": station["name"], "state": station["state"],
                            "provider_label": self.waterlevel_provider_label(),
                            "coverage_valid": station.get("coverage_valid"),
                            "coverage_message": station.get("coverage_message", "coverage not checked")}}
            for station in stations])
        self.obs_noaa_status.setText(
            (f"Showing {len(stations)} {self.waterlevel_provider_label()} station(s) inside the AOI; "
             f"{self._water_level_coverage_summary(stations)}. Only stations marked ✓ can be selected.")
            if self.active_aoi_geometries() else
            "No AOI is available. Draw, upload, or select an AOI before showing observation stations.")

    def observation_noaa_item_changed(self, item: QListWidgetItem) -> None:
        if self.loading_obs_noaa:
            return
        identifier = str(item.data(Qt.ItemDataRole.UserRole))
        station = next((entry for entry in self.coops_stations if str(entry["id"]) == identifier), None)
        if not station or station.get("coverage_valid") is not True:
            return
        checked = item.checkState() == Qt.CheckState.Checked
        if checked and identifier not in self.selected_obs_noaa_ids:
            self.selected_obs_noaa_ids.append(identifier)
        elif not checked and identifier in self.selected_obs_noaa_ids:
            self.selected_obs_noaa_ids.remove(identifier)
        self.observation_station_selections[self.active_water_level_provider] = list(self.selected_obs_noaa_ids)
        self.study_map.set_selected_observation_noaa(self.selected_obs_noaa_ids)
        self.sync_selected_noaa_observation_draft()

    def sync_selected_noaa_observation_draft(self) -> None:
        chosen = [station for station in self.coops_stations
                  if str(station["id"]) in self.selected_obs_noaa_ids]
        features = [
            role_feature(
                "observation_point",
                {"type": "Point", "coordinates": [station["lon"], station["lat"]]},
                {"ID": f"obs_{self.active_water_level_provider}_{station['id']}", "name": station["name"],
                 "station_id": station["id"], "noaa_id": station["id"] if self.active_water_level_provider == "noaa_coops" else "",
                 "provider": self.active_water_level_provider, "dataset": station.get("dataset", ""),
                 "datum": station.get("datum", ""), "observation_type": self.active_water_level_provider,
                 "source": self.active_water_level_provider})
            for station in chosen]
        self.study_map.sync_managed_features(
            "observation_point", features, f"{self.active_water_level_provider}_observation_selection")

    def clear_observation_points(self) -> None:
        self.selected_obs_noaa_ids = []
        self.observation_station_selections = {}
        self.selected_hwm_ids = []
        self.study_map.set_selected_observation_noaa([])
        self.study_map.set_selected_high_water_marks([])
        self.sync_observation_noaa_checks()
        self.sync_hwm_checks()
        self.study_map.clear_role("observation_point")

    def sync_observation_noaa_checks(self) -> None:
        self.loading_obs_noaa = True
        for row in range(self.obs_noaa_list.count()):
            item = self.obs_noaa_list.item(row)
            state = Qt.CheckState.Checked if str(item.data(Qt.ItemDataRole.UserRole)) in self.selected_obs_noaa_ids else Qt.CheckState.Unchecked
            if item.checkState() != state:
                item.setCheckState(state)
        self.loading_obs_noaa = False

    def add_observation_noaa(self) -> None:
        chosen = [station for station in self.coops_stations
                  if station["id"] in self.selected_obs_noaa_ids and station.get("coverage_valid") is True]
        if not chosen:
            QMessageBox.information(self, "No observations selected",
                                    "Check stations in the list or select their yellow markers on the map.")
            return
        self.sync_selected_noaa_observation_draft()
        self.study_map.set_mode("none")
        self.obs_noaa_status.setText(
            f"{len(chosen)} {self.waterlevel_provider_label()} gauge(s) are included as evaluation observations.")

    def load_hwm_events(self) -> None:
        if self.hwm_worker and self.hwm_worker.isRunning():
            return
        self.hwm_load_events.setEnabled(False)
        self.hwm_status.setText("Loading USGS STN flood-event catalog…")
        self.hwm_worker = UsgsHwmWorker()
        self.hwm_worker.loaded.connect(self.hwm_loaded)
        self.hwm_worker.failed.connect(self.hwm_failed)
        self.hwm_worker.start()

    def load_hwm_marks(self) -> None:
        event_id = self.hwm_event.currentData()
        if event_id is None:
            QMessageBox.information(self, "Choose a flood event", "Load the USGS event catalog and choose an event first.")
            return
        self.study_map.fetch_draft(lambda payload, eid=int(event_id): self._start_hwm_marks(payload, eid))

    def _start_hwm_marks(self, payload: Any, event_id: int) -> None:
        bounds = self._draft_bounds(payload)
        if not bounds:
            QMessageBox.information(self, "AOI required", "Upload, draw, or select an AOI before loading high-water marks.")
            return
        if self.hwm_worker and self.hwm_worker.isRunning():
            return
        self.hwm_load_marks.setEnabled(False)
        self.hwm_event.setEnabled(False)
        self.hwm_status.setText("Loading USGS high-water marks and filtering them to the AOI…")
        try:
            document = json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            document = None
        self.hwm_aoi_geometries = [
            feature["geometry"] for feature in split_study_geojson(document)["domain"]
            if isinstance(feature.get("geometry"), dict)
        ]
        west, south, east, north = bounds
        self.hwm_worker = UsgsHwmWorker(event_id, (west, south, east, north))
        self.hwm_worker.loaded.connect(self.hwm_loaded)
        self.hwm_worker.failed.connect(self.hwm_failed)
        self.hwm_worker.start()

    def hwm_loaded(self, records: list[dict[str, Any]], kind: str) -> None:
        self.hwm_load_events.setEnabled(True)
        self.hwm_load_marks.setEnabled(True)
        self.hwm_event.setEnabled(True)
        if kind == "events":
            self.hwm_events = records
            self.hwm_event.blockSignals(True)
            self.hwm_event.clear()
            for event in records:
                year = event["start"][:4] if event["start"] else ""
                label = event["name"] if not year or year in event["name"] else f"{year} — {event['name']}"
                self.hwm_event.addItem(label, event["id"])
            # Prefer the configured event when catalog and project names contain
            # the same words in a different order.
            wanted = set(self.event_name.text().lower().replace("_", " ").split()) if hasattr(self, "event_name") else set()
            best_row, best_score = -1, 0
            for row, event in enumerate(records):
                score = len(wanted.intersection(event["name"].lower().replace("_", " ").split()))
                if score > best_score:
                    best_row, best_score = row, score
            if best_row >= 0:
                self.hwm_event.setCurrentIndex(best_row)
            self.hwm_event.blockSignals(False)
            self.hwm_status.setText(f"Loaded {len(records)} USGS flood event(s). Loading marks for the selected event…")
            QTimer.singleShot(100, self.load_hwm_marks)
            return
        self.hwm_marks = [
            mark for mark in records
            if any(point_in_geojson_geometry(float(mark["lon"]), float(mark["lat"]), geometry)
                   for geometry in self.hwm_aoi_geometries)
        ]
        self.selected_hwm_ids = []
        self.populate_hwm()

    def hwm_failed(self, message: str) -> None:
        self._riva_panic()
        self.hwm_load_events.setEnabled(True)
        self.hwm_load_marks.setEnabled(True)
        self.hwm_event.setEnabled(True)
        self.hwm_status.setText("USGS STN error: " + message)

    def populate_hwm(self) -> None:
        quality_filter = self.hwm_quality.currentData() if hasattr(self, "hwm_quality") else ""
        if quality_filter == "VP":
            visible_marks = [mark for mark in self.hwm_marks
                             if mark["quality"].strip().casefold().startswith(("vp", "very poor"))]
        elif quality_filter:
            visible_marks = [mark for mark in self.hwm_marks
                             if mark["quality"].strip().casefold().startswith(str(quality_filter).casefold())]
        else:
            visible_marks = self.hwm_marks
        if quality_filter:
            visible_ids = {str(mark["id"]) for mark in visible_marks}
            retained_ids = [identifier for identifier in self.selected_hwm_ids
                            if identifier in visible_ids]
            if retained_ids != self.selected_hwm_ids:
                self.selected_hwm_ids = retained_ids
                self.sync_selected_hwm_draft()
        self.loading_hwm = True
        self.hwm_list.clear()
        for mark in visible_marks:
            try:
                elev = f"{float(mark['elev_ft']):.2f} ft"
            except (TypeError, ValueError):
                elev = "elevation unavailable"
            label = f"{mark['site_no'] or 'USGS site'} — HWM {mark['id']} — {elev} {mark['datum']} — {mark['quality']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, mark["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if str(mark["id"]) in self.selected_hwm_ids
                else Qt.CheckState.Unchecked)
            self.hwm_list.addItem(item)
        self.loading_hwm = False
        self.study_map.set_high_water_marks([
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [mark["lon"], mark["lat"]]},
             "properties": {"id": mark["id"], "name": mark["site_no"], "event": mark["event"],
                            "elev_ft": mark.get("elev_ft"), "datum": mark["datum"]}}
            for mark in visible_marks])
        self.study_map.set_selected_high_water_marks([
            str(mark["id"]) for mark in visible_marks
            if str(mark["id"]) in self.selected_hwm_ids])
        self.hwm_status.setText(
            f"Showing {len(visible_marks)} of {len(self.hwm_marks)} USGS high-water mark(s) inside the AOI. "
            "Selected markers are added to the draft automatically.")

    def hwm_item_changed(self, item: QListWidgetItem) -> None:
        if self.loading_hwm:
            return
        identifier = str(item.data(Qt.ItemDataRole.UserRole))
        checked = item.checkState() == Qt.CheckState.Checked
        if checked and identifier not in self.selected_hwm_ids:
            self.selected_hwm_ids.append(identifier)
        elif not checked and identifier in self.selected_hwm_ids:
            self.selected_hwm_ids.remove(identifier)
        self.study_map.set_selected_high_water_marks(self.selected_hwm_ids)
        self.sync_selected_hwm_draft()

    def sync_selected_hwm_draft(self) -> None:
        chosen = [mark for mark in self.hwm_marks if str(mark["id"]) in self.selected_hwm_ids]
        features = [
            role_feature(
                "observation_point",
                {"type": "Point", "coordinates": [mark["lon"], mark["lat"]]},
                {"ID": f"hwm_{mark['id']}", "name": mark["site_no"] or f"HWM {mark['id']}",
                 "hwm_id": mark["id"], "site_no": mark["site_no"], "elev_ft": mark.get("elev_ft"),
                 "verticalDatumName": mark["datum"], "hwmQualityName": mark["quality"],
                 "eventName": mark["event"], "observation_type": "usgs_high_water_mark",
                 "source": "usgs_stn"})
            for mark in chosen]
        self.study_map.sync_managed_features(
            "observation_point", features, "usgs_hwm_selection")

    def sync_hwm_checks(self) -> None:
        self.loading_hwm = True
        for row in range(self.hwm_list.count()):
            item = self.hwm_list.item(row)
            state = Qt.CheckState.Checked if str(item.data(Qt.ItemDataRole.UserRole)) in self.selected_hwm_ids else Qt.CheckState.Unchecked
            if item.checkState() != state:
                item.setCheckState(state)
        self.loading_hwm = False

    def add_hwm_observations(self) -> None:
        chosen = [mark for mark in self.hwm_marks if mark["id"] in self.selected_hwm_ids]
        if not chosen:
            QMessageBox.information(self, "No high-water marks selected",
                                    "Check USGS high-water marks in the list or select their orange markers on the map.")
            return
        self.sync_selected_hwm_draft()
        self.study_map.set_mode("none")
        self.hwm_status.setText(
            f"{len(chosen)} USGS high-water mark(s) are included as evaluation observations.")

    # -- terrain preview ---------------------------------------------------
    def dem_toggled(self, *_: Any) -> None:
        show, three_d = self.dem_show.isChecked(), self.dem_3d.isChecked()
        self.study_map.set_dem(show)
        self.study_map.set_terrain_3d(three_d, self.dem_exaggeration.value())
        self.study_map.set_dem_opacity(self.dem_strength.value())
        if three_d:
            self.dem_status.setText(f"3D terrain at {self.dem_exaggeration.value():g}× exaggeration. "
                                    "Hold Ctrl and drag the map to tilt and rotate.")
        elif show:
            self.dem_status.setText("Shaded relief on. Elevation sampling is available.")
        else:
            self.dem_status.setText("Shaded relief is off.")

    def prepared_dem_toggled(self, checked: bool) -> None:
        self.study_map.show_dem_image(checked)
        if not checked:
            self.dem_colorbar.setVisible(False)
            self.position_map_overlays()

    def vertical_colorbar_pixmap(self, label: str, minimum: float, maximum: float,
                                 colors: list[str], units: str = "") -> QPixmap:
        """Draw a readable vertical scale for the floating left-side map rail."""
        pixmap = QPixmap(142, 286)
        pixmap.fill(QColor(244, 246, 248, 232) if self.is_light_theme else QColor(27, 32, 36, 232))
        painter = QPainter(pixmap)
        foreground = QColor("#263238") if self.is_light_theme else QColor("#f1f5f7")
        outline = QColor("#607d8b") if self.is_light_theme else QColor("#b0bec5")
        painter.setPen(foreground)
        title = label + (f" ({units})" if units else "")
        painter.drawText(QRectF(7, 5, 128, 27), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, title)
        top, height = 38, 220
        gradient = QLinearGradient(0, top, 0, top + height)
        palette = colors or ["#3b4cc0", "#dddddd", "#b40426"]
        for index, color in enumerate(reversed(palette)):
            gradient.setColorAt(index / max(1, len(palette) - 1), QColor(str(color)))
        painter.fillRect(QRectF(14, top, 25, height), gradient)
        painter.setPen(outline)
        painter.drawRect(QRectF(14, top, 25, height))
        painter.setPen(foreground)
        painter.drawText(QRectF(47, top - 9, 87, 20), Qt.AlignmentFlag.AlignLeft, f"{maximum:.3g}")
        painter.drawText(QRectF(47, top + height / 2 - 9, 87, 20), Qt.AlignmentFlag.AlignLeft,
                         f"{(minimum + maximum) / 2:.3g}")
        painter.drawText(QRectF(47, top + height - 10, 87, 20), Qt.AlignmentFlag.AlignLeft, f"{minimum:.3g}")
        painter.end()
        return pixmap

    def preview_paths(self) -> tuple[Path, Path, Path]:
        data = (
            self.data_dir("previews") / "terrain"
            if self.uses_organized_run_layout() else self.data_dir("terrain")
        )
        manifest = "manifest.json" if self.uses_organized_run_layout() else "topobathy_preview.json"
        return (data / manifest, data / "topobathy_preview.png",
                data / "topobathy_preview_colorbar.png")

    def load_prepared_dem(self, announce: bool = False) -> bool:
        meta_path, png_path, bar_path = self.preview_paths()
        if not (meta_path.is_file() and png_path.is_file()):
            self.dem_prepared_show.setEnabled(False)
            self.dem_colorbar_available = False
            self.dem_colorbar.clear()
            self.dem_colorbar.setVisible(False)
            self.study_map.set_dem_image(None)
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            encoded = base64.b64encode(png_path.read_bytes()).decode("ascii")
        except Exception as exc:
            self.terrain_status.setText(f"Cannot read the terrain preview: {exc}")
            return False
        self.active_preview_context = "terrain"
        self.study_map.set_dem_image({
            "uri": "data:image/png;base64," + encoded,
            "west": meta["west"], "south": meta["south"],
            "east": meta["east"], "north": meta["north"],
            "opacity": self.dem_image_opacity.value(),
        })
        self.dem_prepared_show.setEnabled(True)
        self.dem_prepared_show.blockSignals(True)
        self.dem_prepared_show.setChecked(True)
        self.dem_prepared_show.blockSignals(False)
        self.dem_colorbar_available = False
        self.dem_colorbar.clear()
        low = float(meta.get("display_low_m", meta.get("min_elevation_m", 0)))
        high = float(meta.get("display_high_m", meta.get("max_elevation_m", 1)))
        self.dem_colorbar.setPixmap(self.vertical_colorbar_pixmap(
            "Elevation", low, high,
            ["#2166ac", "#67a9cf", "#d1e5f0", "#1fc96b", "#ffff99", "#c2a070", "#8c5a4a"],
            f"m, {meta.get('vertical_datum', 'model datum')}",
        ))
        self.dem_colorbar_available = True
        # The state callback reveals the color bar only after the image source
        # is loaded and the raster layer is actually visible.
        self.dem_colorbar.setVisible(False)
        self.study_map.fit_dem_image()
        status_text = ""
        if announce:
            sources = ", ".join(meta.get("sources", [])) or "unknown"
            status_text = (
                f"Topobathy ready: {meta['min_elevation_m']:.1f} to {meta['max_elevation_m']:.1f} m, "
                f"{meta.get('coverage_fraction', 0) * 100:.0f}% of the extent covered, from {sources}. "
                "Shown on the map."
            )
        datum_report = self.results_dir("simulation", "reports") / "vertical_datum_provenance.json"
        if datum_report.is_file():
            try:
                provenance = json.loads(datum_report.read_text(encoding="utf-8"))
                alignment = ((provenance.get("gapfill") or {}).get("alignment") or {})
                method = str(alignment.get("method") or "none-required")
                if method == "transformez-grid":
                    datum_summary = "Datum: rigorous Transformez grid conversion."
                elif alignment.get("quality") == "approximate":
                    datum_summary = "Datum: approximate local-overlap fallback; details are saved in Results."
                else:
                    datum_summary = "Datum: sources already match the model datum."
                status_text = (status_text + " " + datum_summary).strip()
            except Exception:
                pass
        if status_text:
            self.terrain_status.setText(status_text)
        return True

    # -- roughness and infiltration previews -----------------------------
    def run_surface(self) -> None:
        common = self.config_args()
        if not common:
            return
        self.surface_status.setText("Preparing land-cover, roughness, and infiltration rasters…")
        self.start_commands(
            [self.powershell(["-Action", "Run", *common, "-Mode", "surface",
                              "-CpuThreads", str(self.cpu_threads.value())])],
            "> Prepare roughness / infiltration", job_kind="surface")

    @staticmethod
    def _surface_hex_rgb(value: str) -> tuple[int, int, int]:
        color = value.lstrip("#")
        return tuple(int(color[index:index + 2], 16) for index in (0, 2, 4))

    @classmethod
    def _surface_landcover_colors(cls, class_codes: Iterable[int]) -> dict[tuple[int, int, int], int]:
        standard_codes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]
        standard_colors = [
            "#006400", "#ffbb22", "#ffff4c", "#f096ff", "#fa0000", "#b4b4b4",
            "#f0f0f0", "#0064c8", "#0096a0", "#00cf75", "#fae6a0",
        ]
        tab20 = [
            "#1f77b4", "#aec7e8", "#ff7f0e", "#ffbb78", "#2ca02c", "#98df8a",
            "#d62728", "#ff9896", "#9467bd", "#c5b0d5", "#8c564b", "#c49c94",
            "#e377c2", "#f7b6d2", "#7f7f7f", "#c7c7c7", "#bcbd22", "#dbdb8d",
            "#17becf", "#9edae5",
        ]
        result = {
            cls._surface_hex_rgb(color): code
            for code, color in zip(standard_codes, standard_colors)
        }
        extras = sorted(code for code in set(class_codes) if code >= 0 and code not in standard_codes)
        for index, code in enumerate(extras):
            result[cls._surface_hex_rgb(tab20[index % len(tab20)])] = code
        return result

    @classmethod
    def _surface_value_color(cls, value: float, minimum: float,
                             maximum: float) -> tuple[int, int, int]:
        palette = ["#440154", "#31688e", "#35b779", "#fde725"]
        colors = [cls._surface_hex_rgb(color) for color in palette]
        fraction = 0.0 if maximum <= minimum else (value - minimum) / (maximum - minimum)
        position = max(0.0, min(1.0, fraction)) * (len(colors) - 1)
        lower = min(int(position), len(colors) - 2)
        blend = position - lower
        return tuple(round(colors[lower][channel] * (1.0 - blend)
                           + colors[lower + 1][channel] * blend)
                     for channel in range(3))

    def derived_manning_surface_layer(self, layers: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Build a map-only Manning layer when organized preview discovery omitted its table."""
        if any(str(item.get("key", "")).casefold() == "manning" for item in layers):
            return None
        landcover = next((item for item in layers
                          if str(item.get("key", "")).casefold() == "landcover"), None)
        if landcover is None:
            return None
        landcover_source = str(deep_get(self.config, "landcover.source", "")).casefold()
        if "nlcd" in landcover_source:
            return None
        table_dir = self.data_dir("surface", "tables")
        source_name = str(landcover.get("source", "")).casefold()
        mapping_mode = str(deep_get(self.config, "landcover.manning_mapping", "")).casefold()
        table_candidates = [
            *([table_dir / "manning_with_adaptations.csv"]
              if "roughness_overrides" in source_name else []),
            *([table_dir / "manual_lulc_manning.csv"]
              if mapping_mode in {"manual_lulc_classes", "manual", "custom"} else []),
            *([table_dir / "user_manning_table.csv"]
              if deep_get(self.config, "landcover.manning_table", None) else []),
            table_dir / "esa_worldcover_manning_default.csv",
        ]
        table = next((path for path in table_candidates if path.is_file()), None)
        png_path = Path(str(landcover.get("_png_path", "")))
        if table is None or not png_path.is_file():
            return None
        try:
            with table.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                fieldnames = list(reader.fieldnames or [])
                n_field = next(name for name in fieldnames if name.casefold() in {"n", "manning_n"})
                code_field = next(name for name in fieldnames if name != n_field
                                  and name.casefold() in {"esa_worldcover", "lulc", "class", "code"})
                mapping = {}
                for row in reader:
                    code, value = int(float(row[code_field])), float(row[n_field])
                    if value >= 0:
                        mapping[code] = value
            if not mapping:
                return None
            source = QImage(str(png_path)).convertToFormat(QImage.Format.Format_RGBA8888)
            if source.isNull():
                return None
            color_to_code = self._surface_landcover_colors(mapping)
            minimum, maximum = min(mapping.values()), max(mapping.values())
            value_colors = {
                code: self._surface_value_color(value, minimum, maximum)
                for code, value in mapping.items()
            }
            converted = bytes(source.bits())
            mapped = 0
            for source_color, code in color_to_code.items():
                color = value_colors.get(code)
                if color is None:
                    continue
                for alpha in (225, 255):
                    original_pixel = bytes((*source_color, alpha))
                    count = converted.count(original_pixel)
                    if count:
                        converted = converted.replace(original_pixel, bytes((*color, alpha)))
                        mapped += count
            if not mapped:
                return None
            output = QImage(
                converted, source.width(), source.height(), source.bytesPerLine(),
                QImage.Format.Format_RGBA8888,
            ).copy()
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not output.save(buffer, "PNG"):
                return None
            entry = dict(landcover)
            entry.update({
                "key": "manning", "label": "Manning roughness", "source": table.name,
                "min": minimum, "max": maximum,
                "_image_uri": "data:image/png;base64," +
                base64.b64encode(bytes(buffer.data())).decode("ascii"),
            })
            entry.pop("_png_path", None)
            entry.pop("_bar_path", None)
            return entry
        except (OSError, KeyError, StopIteration, TypeError, ValueError):
            return None

    def surface_manifest_path(self) -> Path:
        if self.uses_organized_run_layout():
            return self.data_dir("previews") / "surface" / "manifest.json"
        return self.run_dir() / "agent_data" / "surface_previews.json"

    def load_surface_previews(self, announce: bool = False) -> bool:
        manifest_path = self.surface_manifest_path()
        previous = self.surface_layer_choice.currentData() if self.surface_layer_choice.count() else None
        layers: list[dict[str, Any]] = []
        error = ""
        if manifest_path.is_file():
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
                for item in document.get("layers", []):
                    if not isinstance(item, dict):
                        continue
                    png = manifest_path.parent / str(item.get("png", ""))
                    if png.is_file():
                        entry = dict(item)
                        entry["_png_path"] = str(png)
                        entry["_bar_path"] = str(manifest_path.parent / str(item.get("colorbar", "")))
                        layers.append(entry)
            except Exception as exc:
                error = str(exc)
        derived_manning = self.derived_manning_surface_layer(layers)
        if derived_manning is not None:
            insert_at = next((index + 1 for index, item in enumerate(layers)
                              if str(item.get("key", "")).casefold() == "landcover"), len(layers))
            layers.insert(insert_at, derived_manning)
        self.surface_layers = layers
        self.surface_layer_choice.blockSignals(True)
        self.surface_layer_choice.clear()
        for item in layers:
            self.surface_layer_choice.addItem(str(item.get("label", item.get("key", "Layer"))), item.get("key"))
        self.surface_layer_choice.setEnabled(bool(layers))
        selected = 0
        if previous is not None:
            match = self.surface_layer_choice.findData(previous)
            if match >= 0:
                selected = match
        if layers:
            self.surface_layer_choice.setCurrentIndex(selected)
        self.surface_layer_choice.blockSignals(False)
        if not layers:
            self.active_preview_context = "surface"
            self.study_map.set_dem_image(None)
            self.dem_colorbar_available = False
            self.dem_colorbar.clear()
            self.dem_colorbar.setVisible(False)
            if error:
                self.surface_status.setText(f"Cannot read prepared surface previews: {error}")
            else:
                self.surface_status.setText(
                    "No prepared surface layers were found. Click Prepare roughness / infiltration to create them.")
            return False
        self.surface_layer_changed(selected, fit=True)
        if announce:
            self.surface_status.setText(
                f"Prepared {len(layers)} surface layer(s). Select a layer above the map to inspect it.")
        return True

    def surface_layer_changed(self, index: int, fit: bool = True) -> None:
        if not (0 <= index < len(self.surface_layers)):
            return
        item = self.surface_layers[index]
        try:
            image_uri = str(item.get("_image_uri", ""))
            if not image_uri:
                encoded = base64.b64encode(Path(item["_png_path"]).read_bytes()).decode("ascii")
                image_uri = "data:image/png;base64," + encoded
        except Exception as exc:
            self.surface_status.setText(f"Cannot read the prepared surface layer: {exc}")
            return
        self.active_preview_context = "surface"
        self.study_map.set_dem_image({
            "uri": image_uri,
            "west": item["west"], "south": item["south"],
            "east": item["east"], "north": item["north"],
            "opacity": self.dem_image_opacity.value(),
        })
        self.dem_colorbar_available = False
        self.dem_colorbar.clear()
        minimum, maximum = float(item.get("min", 0)), float(item.get("max", 1))
        label = str(item.get("label", "Surface layer"))
        lower = label.lower()
        if "worldcover" in lower or "land cover" in lower or "nlcd" in lower:
            palette = ["#006400", "#ffbb22", "#ffff4c", "#f096ff", "#fa0000", "#b4b4b4", "#0064c8", "#00cf75"]
        elif "soil" in lower:
            palette = ["#2c7bb6", "#abd9e9", "#fdae61", "#d7191c"]
        elif "manning" in lower:
            palette = ["#440154", "#31688e", "#35b779", "#fde725"]
        else:
            palette = ["#ffffcc", "#fd8d3c", "#800026"]
        self.dem_colorbar.setPixmap(self.vertical_colorbar_pixmap(label, minimum, maximum, palette))
        self.dem_colorbar_available = True
        self.dem_colorbar.setVisible(False)
        if fit:
            self.study_map.fit_dem_image()
        minimum, maximum = item.get("min"), item.get("max")
        value_range = ""
        if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
            value_range = f" Range {minimum:g} to {maximum:g}."
        coverage = float(item.get("coverage_fraction", 0)) * 100
        coverage_basis = str(item.get("coverage_basis", "AOI"))
        if coverage_basis.casefold() == "aoi":
            coverage_text = f"{coverage:.0f}% of the AOI has data"
        else:
            coverage_text = f"{coverage:.0f}% of {coverage_basis} has data"
            aoi_coverage = item.get("aoi_coverage_fraction")
            if isinstance(aoi_coverage, (int, float)):
                coverage_text += f" ({float(aoi_coverage) * 100:.0f}% of the complete AOI including water)"
        self.surface_status.setText(
            f"Showing {item.get('label', 'surface layer')} from {item.get('source', 'prepared raster')}."
            f"{value_range} {coverage_text}; outside the AOI is transparent.")

    # -- rainfall, wind, and pressure map previews -----------------------
    def sync_atmosphere_sources(self, *_: Any) -> None:
        for source, field, button, local_sources in (
            (self.rain_source, self.rain_file, self.rain_browse, {"mrms", "file"}),
            (self.wind_source, self.wind_file, self.wind_browse, {"file"}),
            (self.pressure_source, self.pressure_file, self.pressure_browse, {"file"}),
        ):
            enabled = source.currentText().strip().lower() in local_sources
            field.setEnabled(enabled); button.setEnabled(enabled)
            field.setPlaceholderText("Select a prepared NetCDF file" if enabled else "Downloaded automatically")

    def import_point_locations(self, editor_key: str, role: str) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Import forcing locations", str(self.project_base_dir()),
            "GeoJSON (*.geojson *.json)")
        if not selected:
            return
        try:
            document = json.loads(Path(selected).read_text(encoding="utf-8"))
            prefix = self.coordinate_editors[editor_key]["prefix"]
            features = []
            for feature in document.get("features", []):
                geometry = feature.get("geometry") or {}
                if geometry.get("type") != "Point" or len(geometry.get("coordinates") or []) < 2:
                    continue
                properties = dict(feature.get("properties") or {})
                identifier = str(properties.get("ID") or properties.get("id")
                                 or properties.get("station_id") or properties.get("source_id")
                                 or f"{prefix}{len(features) + 1:03d}")
                properties.update({"ID": identifier,
                                   "name": str(properties.get("name") or identifier),
                                   "source": "imported_locations"})
                features.append(role_feature(role, geometry, properties))
            if not features:
                raise ValueError("The selected GeoJSON contains no Point features.")
            self.study_map.set_role_features(role, features)
            self.study_map.fit_draft()
            self.mark_dirty()
            status = getattr(self, f"{editor_key}_file_status", None)
            if isinstance(status, QLabel):
                status.setText(
                    f"Imported {len(features)} location(s). Saving to the study-area file…")
            self.request_forcing_point_save()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot import locations", str(exc))

    def save_forcing_template(self, kind: str) -> None:
        filename = "ocean_water_level_template.csv" if kind == "ocean" else "river_discharge_template.csv"
        selected, _ = QFileDialog.getSaveFileName(
            self, "Save forcing CSV template", str(self.project_base_dir() / filename),
            "CSV (*.csv)")
        if not selected:
            return
        target = Path(selected)
        if target.suffix.lower() != ".csv":
            target = target.with_suffix(".csv")
        if kind == "ocean":
            content = "time,bnd001\n2015-12-03T00:00:00Z,0.0\n2015-12-03T01:00:00Z,0.0\n"
        else:
            content = "time,riv001\n2015-12-03T00:00:00Z,0.0\n2015-12-03T01:00:00Z,0.0\n"
        try:
            target.write_text(content, encoding="utf-8")
            self.statusBar().showMessage(f"Saved forcing template: {target}", 7000)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot save template", str(exc))

    @staticmethod
    def _parse_forcing_time(value: str) -> datetime:
        text = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def inspect_forcing_file(self, path: Path, kind: str, requested_layout: str) -> dict[str, Any]:
        if path.suffix.lower() in {".nc", ".nc4"}:
            try:
                # Optional dynamic import keeps the packaged GUI small. The
                # managed scientific runtime always performs the authoritative
                # CF/units/time/location validation before building SFINCS.
                xr = importlib.import_module("xarray")
                dataset = xr.open_dataset(path)
                try:
                    time_name = next((name for name in ("time", "valid_time", "date")
                                      if name in dataset.coords or name in dataset.variables), None)
                    if not time_name:
                        raise ValueError("No time coordinate was found in the NetCDF file.")
                    candidates = [name for name, variable in dataset.data_vars.items()
                                  if time_name in variable.dims]
                    if not candidates:
                        raise ValueError("No time-dependent forcing variable was found in the NetCDF file.")
                    preferred = ("water_level", "waterlevel", "discharge", "flow")
                    variable_name = next((name for name in candidates
                                          if any(token in name.lower() for token in preferred)), candidates[0])
                    variable = dataset[variable_name]
                    station_dims = [dimension for dimension in variable.dims if dimension != time_name]
                    count = int(variable.sizes[station_dims[0]]) if station_dims else 1
                    times = [self._parse_forcing_time(str(value))
                             for value in dataset[time_name].values[[0, -1]]]
                    identifiers = [str(value) for value in (
                        dataset[station_dims[0]].values if station_dims and station_dims[0] in dataset.coords
                        else range(1, count + 1))]
                    return {"layout": "CF NetCDF", "series_ids": identifiers, "rows": int(variable.sizes[time_name]),
                            "start": min(times), "end": max(times), "invalid": 0}
                finally:
                    dataset.close()
            except ImportError:
                return {"layout": "CF NetCDF", "series_ids": None, "rows": 0,
                        "start": None, "end": None, "invalid": 0, "deferred": True}

        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        if len(rows) < 2 or len(rows[0]) < 2:
            raise ValueError("CSV must contain a header and at least one data row.")
        headers = [value.strip() for value in rows[0]]
        lowered = [value.casefold() for value in headers]
        id_names = {"station_id", "source_id", "id", "point_id"}
        value_names = ({"water_level", "water_level_m", "level", "value"} if kind == "ocean"
                       else {"discharge", "discharge_m3s", "flow", "value"})
        detected_long = any(name in id_names for name in lowered) and any(name in value_names for name in lowered)
        layout = requested_layout if requested_layout not in {"", "auto"} else ("long" if detected_long else "wide")
        time_index = next((index for index, name in enumerate(lowered)
                           if name in {"time", "datetime", "timestamp", "date"}), 0)
        timestamps: list[datetime] = []
        invalid = 0
        identifiers: list[str]
        if layout == "long":
            id_index = next((index for index, name in enumerate(lowered) if name in id_names), -1)
            value_index = next((index for index, name in enumerate(lowered) if name in value_names), -1)
            if id_index < 0 or value_index < 0:
                raise ValueError(
                    "Long CSV requires time, station_id/source_id, and water_level/discharge columns.")
            identifiers = []
            for row in rows[1:]:
                if len(row) <= max(time_index, id_index, value_index):
                    invalid += 1; continue
                try:
                    timestamps.append(self._parse_forcing_time(row[time_index])); float(row[value_index])
                except (ValueError, TypeError):
                    invalid += 1; continue
                if row[id_index].strip() not in identifiers:
                    identifiers.append(row[id_index].strip())
        else:
            identifiers = [header for index, header in enumerate(headers) if index != time_index]
            for row in rows[1:]:
                try:
                    timestamps.append(self._parse_forcing_time(row[time_index]))
                    values = [float(row[index]) for index in range(len(headers)) if index != time_index]
                    if kind == "river" and any(value < 0 for value in values):
                        invalid += 1
                except (ValueError, TypeError, IndexError):
                    invalid += 1
        if not timestamps:
            raise ValueError("No valid timestamps and numeric forcing values were found.")
        return {"layout": f"{layout.title()} CSV", "series_ids": identifiers,
                "rows": len(rows) - 1, "start": min(timestamps), "end": max(timestamps),
                "invalid": invalid}

    def validate_file_forcing(self, kind: str, show_dialog: bool = True) -> bool:
        path_field = self.ocean_timeseries if kind == "ocean" else self.river_timeseries
        status = self.ocean_file_status if kind == "ocean" else self.river_file_status
        layout_widget = self.ocean_file_layout if kind == "ocean" else self.river_file_layout
        path = self.user_path(path_field.text()) if path_field.text().strip() else None
        try:
            if self._forcing_save_pending or self._forcing_save_error:
                raise ValueError(self._forcing_save_error or "Forcing locations are still being saved. Retry shortly.")
            if path is None or not path.is_file():
                raise ValueError("Select an existing time-series CSV or CF NetCDF file.")
            report = self.inspect_forcing_file(path, kind, str(layout_widget.currentData() or "auto"))
            editor = self.coordinate_editors[kind]
            study = json.loads(self.user_path(self.geojson_path.text()).read_text(encoding="utf-8"))
            point_ids = {str((feature.get("properties") or {}).get("ID", "")).strip()
                         for feature in split_study_geojson(study)[editor["role"]]}
            draft_ids = {editor["table"].item(row, 0).text().strip()
                         for row in range(editor["table"].rowCount()) if editor["table"].item(row, 0)}
            if draft_ids != point_ids:
                raise ValueError("Locations have unsaved changes. Click Validate file and points to save and validate.")
            series_ids = ({str(value).strip() for value in report["series_ids"]}
                          if report["series_ids"] is not None else None)
            if not point_ids:
                raise ValueError("Define at least one matching forcing location.")
            if series_ids is not None and point_ids != series_ids:
                raise ValueError(
                    "Time-series IDs and map point IDs do not match. "
                    f"File: {sorted(series_ids)}; points: {sorted(point_ids)}")
            start = datetime.fromtimestamp(
                self.start_time.dateTime().toMSecsSinceEpoch() / 1000.0, tz=timezone.utc)
            end = datetime.fromtimestamp(
                self.end_time.dateTime().toMSecsSinceEpoch() / 1000.0, tz=timezone.utc)
            if (report["start"] is not None and report["end"] is not None
                    and (report["start"] > start or report["end"] < end)):
                raise ValueError(
                    f"File coverage {report['start'].isoformat()} through {report['end'].isoformat()} "
                    "does not cover the complete simulation period.")
            if report["invalid"]:
                raise ValueError(f"The file contains {report['invalid']} invalid or physically impossible row(s).")
            if kind == "ocean" and self.ocean_datum_policy.currentData() == "unknown":
                raise ValueError("Declare how the source water-level datum relates to the model target datum.")
            if (kind == "ocean" and self.ocean_datum_policy.currentData() == "match"
                    and normalize_vertical_datum_name(self.ocean_file_datum.currentText())
                    != normalize_vertical_datum_name(self.vertical_datum.currentText())):
                raise ValueError(
                    "The source datum differs from the model target datum. Select documented constant-offset "
                    "conversion and enter the offset, or correct the datum declarations.")
            if report.get("deferred"):
                message = (f"BASIC PASS — {report['layout']} exists and {len(point_ids)} point(s) are defined. "
                           "CF variables, units, IDs, missing values, and event coverage will be validated "
                           "by the managed scientific runtime before model preparation.")
            else:
                message = (f"PASS — {report['layout']}, {report['rows']} row(s), "
                           f"{len(series_ids or [])} matching series, complete event coverage.")
            status.setText(message)
            if show_dialog:
                QMessageBox.information(self, "File forcing validation", message)
            return True
        except Exception as exc:
            message = f"Not ready — {exc}"
            status.setText(message)
            if show_dialog:
                QMessageBox.warning(self, "File forcing validation", message)
            return False

    def sync_ocean_source_ui(self, *_: Any) -> None:
        source = self.ocean_source.currentText().strip().lower()
        file_mode = source == "file"
        provider_fields = source not in {"file", "none"}
        self.ocean_datum.setVisible(provider_fields)
        self.ocean_provider_form.labelForField(self.ocean_datum).setVisible(provider_fields)
        self.ocean_distance.setVisible(provider_fields)
        self.ocean_provider_form.labelForField(self.ocean_distance).setVisible(provider_fields)
        self.ocean_file_group.setVisible(file_mode)
        self.ocean_datum_offset.setEnabled(
            file_mode and self.ocean_datum_policy.currentData() == "offset")
        if source in {"gtsm", "gtsm_v3"}:
            if self.ocean_distance.value() < 10.0:
                self.ocean_distance.setValue(150.0)
            self.set_info_text(self.ocean_geometry_note,
                "Draw only the ocean boundary line. GTSM automatically selects nearby global "
                "model nodes, so catalog/manual water-level points are not required. GTSM v3 "
                "reanalysis covers 1950-2024 and uses a 1986-2005 mean-sea-level reference. "
                "The recommended node-search distance is 150 km.")
        elif file_mode:
            self.set_info_text(self.ocean_geometry_note,
                "User-file mode uses explicitly identified points and a CSV or CF NetCDF water-level series. "
                "Provider station distance and NOAA datum controls do not apply. The source datum and any "
                "documented conversion to the model target datum must be declared below.")
        else:
            self.set_info_text(self.ocean_geometry_note,
                "Draw the ocean boundary and select its water-level points in Study Area → "
                "Ocean boundary. This panel only controls the forcing provider, datum, and "
                "station-search distance.")
        summary = getattr(self.study_map, "last_summary", {})
        if summary:
            self.map_state_changed(summary)

    def sync_river_source_ui(self, *_: Any) -> None:
        source = self.river_source.currentText().strip().lower()
        uses_usgs = source in {"usgs", "usgs_auto", "usgs_sites"}
        uses_file = source == "file"
        self.glofas_terms_button.setVisible(source in {"glofas", "glofas_auto"})
        self.river_points_group.setEnabled(uses_usgs or uses_file)
        self.river_distance.setEnabled(uses_usgs)
        self.river_distance.setVisible(uses_usgs)
        self.river_provider_form.labelForField(self.river_distance).setVisible(uses_usgs)
        self.max_gages.setVisible(uses_usgs)
        self.river_provider_form.labelForField(self.max_gages).setVisible(uses_usgs)
        self.river_file_group.setVisible(uses_file)
        for widget in (self.usgs_button, self.usgs_pick_button, self.usgs_add_button,
                       self.usgs_limit_to_max, self.usgs_list):
            widget.setVisible(uses_usgs)
        if source in {"glofas", "glofas_auto"}:
            self.usgs_status.setText(
                "GloFAS will detect river-network cells that cross from outside to inside "
                "the model boundary. Manual river points and USGS gauges are not used.")
        elif uses_file:
            self.usgs_status.setText(
                "User-file mode: enter or import river-source coordinates whose IDs match the discharge series.")

    def era5_credentials_ready(self) -> bool:
        if os.environ.get("CDSAPI_URL", "").strip() and os.environ.get("CDSAPI_KEY", "").strip():
            return True
        rc_path = Path(os.environ.get("CDSAPI_RC", str(Path.home() / ".cdsapirc"))).expanduser()
        return rc_path.is_file()

    def check_era5_setup(self) -> bool:
        # Call through the class so this compatibility entry point remains
        # independently testable with the lightweight window facades used by
        # installers and diagnostics.
        return AutoCFWindow.check_copernicus_setup(self, atmosphere_only=True)

    def check_copernicus_setup(self, atmosphere_only: bool = False) -> bool:
        atmosphere_sources = {
            self.rain_source.currentText().strip().lower(),
            self.wind_source.currentText().strip().lower(),
            self.pressure_source.currentText().strip().lower(),
        }
        requirements: list[tuple[str, str]] = []
        if "era5" in atmosphere_sources:
            requirements.append(("ERA5", CDS_ERA5_DATASET_URL))
        if not atmosphere_only:
            if self.ocean_source.currentText().strip().lower() in {"gtsm", "gtsm_v3"}:
                requirements.append(("GTSM", CDS_GTSM_DATASET_URL))
            if self.river_source.currentText().strip().lower() in {"glofas", "glofas_auto"}:
                requirements.append(("GloFAS", EWDS_GLOFAS_TERMS_URL))
        if not requirements or self.era5_credentials_ready():
            return True

        rc_path = Path(os.environ.get("CDSAPI_RC", str(Path.home() / ".cdsapirc"))).expanduser()
        names = ", ".join(name for name, _ in requirements)
        if "ERA5" in names:
            self.atmosphere_status.setText(
                f"ERA5 setup required through Copernicus for {names}: create {rc_path} with your personal access token.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Copernicus data access setup required")
        box.setText(
            f"{names} downloads require your personal Copernicus account and token.\n\n"
            "1. Sign in or register on the CDS setup page.\n"
            "2. Accept the Terms of Use for each selected dataset.\n"
            "3. Copy the two configuration lines shown by CDS into:\n"
            f"   {rc_path}\n\n"
            "The file must be named .cdsapirc, not .cdsapirc.txt. AutoCF does not save "
            "the token in your project YAML or job log."
        )
        setup_button = box.addButton("Open CDS setup", QMessageBox.ButtonRole.ActionRole)
        dataset_buttons = {
            box.addButton(f"Open {name} terms", QMessageBox.ButtonRole.ActionRole): url
            for name, url in requirements
        }
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is setup_button:
            QDesktopServices.openUrl(QUrl(CDS_API_SETUP_URL))
        elif box.clickedButton() in dataset_buttons:
            QDesktopServices.openUrl(QUrl(dataset_buttons[box.clickedButton()]))
        return False

    def choose_atmosphere_file(self, field: QLineEdit, component: str) -> None:
        start = field.text().strip() or str(self.root / "data")
        selected, _ = QFileDialog.getOpenFileName(
            self, f"Choose {component} forcing NetCDF", start,
            "NetCDF (*.nc *.nc4 *.cdf);;All files (*)")
        if selected:
            field.setText(selected)

    def forcing_tab_changed(self, index: int) -> None:
        self.study_map.set_mode("none")
        if index != 0:
            title = "Ocean boundary" if index == 1 else "River discharge"
            self.forcing_active_layer_label.setText(title + " — no gridded overlay")
            self.forcing_time_choice.blockSignals(True); self.forcing_time_choice.clear()
            self.forcing_time_choice.blockSignals(False); self.forcing_time_choice.setEnabled(False)
            self.forcing_previous.setEnabled(False); self.forcing_next.setEnabled(False)
            self.active_preview_context = "forcing"; self.study_map.set_dem_image(None)
            self.dem_colorbar_available = False; self.dem_colorbar.clear(); self.dem_colorbar.setVisible(False)
            self.forcing_map_status.setText(
                "The map is reserved for selecting boundary stations and source points in this tab.")
            return
        key = str(self.forcing_layer_choice.currentData() or "rainfall")
        self.show_atmosphere_layer(key, fit=False)

    def show_atmosphere_layer(self, key: str, fit: bool = True) -> None:
        if self.forcing_tabs.currentIndex() != 0:
            self.forcing_tabs.setCurrentIndex(0)
        label = {"rainfall": "Rainfall rate", "wind": "Wind speed and direction",
                 "pressure": "Barometric pressure"}.get(key, key.title())
        self.forcing_active_layer_label.setText(label)
        self.forcing_previous.setEnabled(True); self.forcing_next.setEnabled(True)
        layer_index = self.forcing_layer_choice.findData(key)
        if layer_index >= 0:
            if self.forcing_layer_choice.currentIndex() == layer_index:
                self.forcing_layer_changed(layer_index, fit=fit)
            else:
                self.forcing_layer_choice.setCurrentIndex(layer_index)
        else:
            self.forcing_time_choice.clear(); self.forcing_time_choice.setEnabled(False)
            self.forcing_map_status.setText(f"{label} has not been prepared for this run.")

    def forcing_manifest_path(self) -> Path:
        if self.uses_organized_run_layout():
            return self.data_dir("previews") / "forcing" / "manifest.json"
        return self.run_dir() / "agent_data" / "forcing_previews" / "manifest.json"

    def forcing_sources_ready(self) -> bool:
        run = self.model_dir()
        data = (
            self.data_dir("forcing", "netcdf")
            if self.uses_organized_run_layout() else self.run_dir() / "agent_data"
        )
        return any((run / name).is_file() for name in ("precip_2d.nc", "wind_2d.nc", "press_2d.nc")) or any(
            data.glob("*_precip_standardized.nc")) or any(data.glob("*_wind_standardized.nc")) or any(
            data.glob("*_pressure_standardized_pa.nc"))

    def prepare_atmosphere_forcing(self) -> None:
        if not self.check_era5_setup():
            return
        common = self.config_args()
        if not common:
            return
        self.atmosphere_status.setText(
            "Preparing atmospheric forcing: downloading/subsetting providers, standardizing units and time, "
            "checking AOI coverage, then creating map previews…")
        self.start_commands(
            [
                self.powershell(["-Action", "Run", *common, "-Mode", "atmosphere",
                                 "-CpuThreads", str(self.cpu_threads.value())]),
                self.powershell(["-Action", "ForcingPreview", *common]),
            ],
            "> Prepare atmosphere forcing (rainfall, wind, and pressure)\nSFINCS will not be run.",
            job_kind="atmosphere",
        )

    def prepare_forcing_previews(self, automatic: bool = False) -> None:
        if self.process is not None:
            if not automatic:
                QMessageBox.information(self, "AutoCF is busy", "Wait for or cancel the current operation.")
            return
        if self.config_path is None or not self.config_path.is_file():
            self.forcing_map_status.setText("Open a saved YAML configuration before creating forcing maps.")
            return
        if self.dirty:
            if automatic:
                self.forcing_map_status.setText("Save the YAML changes, then select Forcing again to create map previews.")
                return
            common = self.config_args()
            if not common:
                return
        else:
            common = ["-Config", str(self.config_path)]
        self.forcing_map_status.setText("Creating AOI-clipped forcing frames from the prepared NetCDF files…")
        self.start_commands(
            [self.powershell(["-Action", "ForcingPreview", *common])],
            "> Create rainfall, wind, and pressure map previews from the existing run",
            job_kind="forcing_preview")

    def load_forcing_previews(self, announce: bool = False, automatic: bool = False) -> bool:
        manifest_path = self.forcing_manifest_path()
        previous = self.forcing_layer_choice.currentData() if self.forcing_layer_choice.count() else None
        layers: list[dict[str, Any]] = []
        error = ""
        document: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
                for layer in document.get("layers", []):
                    frames = []
                    for frame in layer.get("frames", []):
                        png = manifest_path.parent / str(frame.get("png", ""))
                        if png.is_file():
                            item = dict(frame); item["_png_path"] = str(png); frames.append(item)
                    if frames:
                        item = dict(layer); item["frames"] = frames; layers.append(item)
            except Exception as exc:
                error = str(exc)
        self.forcing_manifest, self.forcing_layers = document, layers
        self.forcing_layer_choice.blockSignals(True)
        self.forcing_layer_choice.clear()
        for layer in layers:
            self.forcing_layer_choice.addItem(str(layer.get("label", layer.get("key", "Forcing"))), layer.get("key"))
        self.forcing_layer_choice.setEnabled(bool(layers))
        selected = 0
        if previous is not None:
            match = self.forcing_layer_choice.findData(previous)
            if match >= 0:
                selected = match
        if layers:
            self.forcing_layer_choice.setCurrentIndex(selected)
        self.forcing_layer_choice.blockSignals(False)
        if not layers:
            self.forcing_time_choice.clear(); self.forcing_time_choice.setEnabled(False)
            self.active_preview_context = "forcing"; self.study_map.set_dem_image(None)
            self.dem_colorbar_available = False; self.dem_colorbar.clear(); self.dem_colorbar.setVisible(False)
            if error:
                self.forcing_map_status.setText(f"Cannot read prepared forcing previews: {error}")
            elif self.forcing_sources_ready():
                self.forcing_map_status.setText("Prepared forcing files found; creating map previews…" if automatic else
                                                "Prepared forcing files found. Select Create / refresh maps.")
                if automatic:
                    QTimer.singleShot(0, lambda: self.prepare_forcing_previews(True))
            else:
                self.forcing_map_status.setText(
                    "No atmospheric forcing has been prepared. Select providers and click Prepare atmosphere forcing.")
            return False
        active_tab = self.forcing_tabs.currentIndex() if hasattr(self, "forcing_tabs") else 0
        self.forcing_tab_changed(active_tab)
        if active_tab == 0:
            self.study_map.fit_dem_image()
        if hasattr(self, "atmosphere_status"):
            validation = document.get("validation", {})
            checked = validation.get("components", []) if isinstance(validation, dict) else []
            self.atmosphere_status.setText(
                f"Atmosphere forcing prepared and validated: {', '.join(map(str, checked))}."
                if checked else f"Atmosphere forcing prepared: {len(layers)} map layer(s).")
        if announce:
            self.forcing_map_status.setText(
                f"Loaded {len(layers)} atmospheric layers. Use the three Show on map buttons and select an hourly time step.")
        return True

    def forcing_layer_changed(self, index: int, fit: bool = True) -> None:
        if not (0 <= index < len(self.forcing_layers)):
            return
        layer = self.forcing_layers[index]
        self.forcing_time_choice.blockSignals(True)
        self.forcing_time_choice.clear()
        for number, frame in enumerate(layer.get("frames", [])):
            self.forcing_time_choice.addItem(str(frame.get("time", f"Time step {number + 1}")), number)
        self.forcing_time_choice.setEnabled(self.forcing_time_choice.count() > 0)
        self.forcing_time_choice.setCurrentIndex(0)
        self.forcing_time_choice.blockSignals(False)
        self.forcing_frame_changed(0, fit=fit)

    def step_forcing_frame(self, amount: int) -> None:
        count = self.forcing_time_choice.count()
        if count:
            self.forcing_time_choice.setCurrentIndex(
                max(0, min(count - 1, self.forcing_time_choice.currentIndex() + amount)))

    def forcing_colorbar_pixmap(self, layer: dict[str, Any], frame: dict[str, Any]) -> QPixmap:
        minimum, maximum = float(frame.get("min", 0)), float(frame.get("max", 1))
        return self.vertical_colorbar_pixmap(
            str(layer.get("label", "Forcing")), minimum, maximum,
            list(layer.get("palette") or ["#3b4cc0", "#dddddd", "#b40426"]),
            str(layer.get("units", "")),
        )

    def forcing_frame_changed(self, index: int, fit: bool = False) -> None:
        layer_index = self.forcing_layer_choice.currentIndex()
        if not (0 <= layer_index < len(self.forcing_layers)):
            return
        layer = self.forcing_layers[layer_index]; frames = layer.get("frames", [])
        if not (0 <= index < len(frames)):
            return
        frame = frames[index]
        try:
            encoded = base64.b64encode(Path(frame["_png_path"]).read_bytes()).decode("ascii")
        except Exception as exc:
            self.forcing_map_status.setText(f"Cannot read forcing map frame: {exc}")
            return
        self.active_preview_context = "forcing"
        self.study_map.set_dem_image({
            "uri": "data:image/png;base64," + encoded,
            "west": layer.get("west", self.forcing_manifest["west"]),
            "south": layer.get("south", self.forcing_manifest["south"]),
            "east": layer.get("east", self.forcing_manifest["east"]),
            "north": layer.get("north", self.forcing_manifest["north"]),
            "opacity": self.forcing_opacity.value(),
        })
        self.dem_colorbar.setPixmap(self.forcing_colorbar_pixmap(layer, frame))
        self.dem_colorbar_available = True; self.dem_colorbar.setVisible(False)
        if fit:
            self.study_map.fit_dem_image()
        mean = frame.get("mean"); mean_text = f", AOI mean {float(mean):.3g} {layer.get('units', '')}" if mean is not None else ""
        arrow_text = " White arrows show direction." if layer.get("key") == "wind" else ""
        self.forcing_map_status.setText(
            f"Showing {layer.get('label')} at {frame.get('time')}{mean_text}. "
            f"Scale: {float(frame.get('min', 0)):.3g}–{float(frame.get('max', 0)):.3g} {layer.get('units', '')}."
            f"{arrow_text} Outside the study-area AOI is transparent.")

    def dem_extent_bounds(self) -> tuple[float, float, float, float] | None:
        if not self.domain_bounds or not self.dem_extent_show.isChecked():
            return None
        west, south, east, north = self.domain_bounds
        # The build buffers the domain bbox by topobathy.buffer_km.
        pad_lat = float(self.topo_buffer.value()) / 111.0
        mid = math.radians(max(-89.0, min(89.0, (south + north) / 2.0)))
        pad_lon = pad_lat / max(0.15, math.cos(mid))
        return (west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat)

    def update_dem_extent(self, *_: Any) -> None:
        self.study_map.set_dem_extent(self.dem_extent_bounds())

    def sample_elevation(self) -> None:
        if not self.domain_bounds:
            self.dem_status.setText("Load or draw a study-area domain first.")
            return
        if not (self.dem_show.isChecked() or self.dem_3d.isChecked()):
            self.dem_status.setText("Turn on shaded relief or 3D terrain first so elevation tiles are loaded.")
            return
        west, south, east, north = self.domain_bounds
        lon, lat = (west + east) / 2.0, (south + north) / 2.0
        self.study_map.fetch_elevation(lon, lat, lambda payload: self._elevation_ready(lon, lat, payload))

    def _elevation_ready(self, lon: float, lat: float, payload: Any) -> None:
        try:
            value = json.loads(payload) if payload else None
        except Exception:
            value = None
        if value is None:
            self.dem_status.setText("No elevation returned yet — pan or zoom to the domain so the tiles load, then try again.")
        else:
            self.dem_status.setText(f"Elevation at domain centre ({lat:.4f}, {lon:.4f}): {float(value):.1f} m "
                                    "above the Terrarium reference. Model datum comes from the setting above.")

    # -- USGS river gauges ------------------------------------------------
    def search_usgs(self) -> None:
        if self.usgs_worker and self.usgs_worker.isRunning():
            return
        if not self.domain_bounds:
            self.usgs_status.setText("Load or draw a study-area domain first; the gauge search uses its bounds.")
            return
        west, south, east, north = self.domain_bounds
        pad = float(self.river_distance.value()) / 111.0
        self.usgs_status.setText("Querying the USGS NWIS site service…")
        self.usgs_button.setEnabled(False)
        self.usgs_worker = UsgsSiteWorker((west - pad, south - pad, east + pad, north + pad))
        self.usgs_worker.loaded.connect(self.usgs_loaded)
        self.usgs_worker.failed.connect(self.usgs_failed)
        self.usgs_worker.start()

    def usgs_loaded(self, sites: list[dict[str, Any]]) -> None:
        self.usgs_button.setEnabled(True)
        centroid = None
        if self.domain_bounds:
            west, south, east, north = self.domain_bounds
            centroid = ((west + east) / 2.0, (south + north) / 2.0)
        for site in sites:
            site["distance_km"] = (haversine_km(centroid[0], centroid[1], site["lon"], site["lat"])
                                   if centroid else 0.0)
        sites.sort(key=lambda site: site["distance_km"])
        self.usgs_sites = sites
        self.populate_usgs()

    def usgs_failed(self, message: str) -> None:
        self._riva_panic()
        self.usgs_button.setEnabled(True)
        self.usgs_status.setText("USGS site service error: " + message)

    def visible_usgs(self) -> list[dict[str, Any]]:
        if self.usgs_limit_to_max.isChecked() and self.max_gages.value() > 0:
            return self.usgs_sites[:self.max_gages.value()]
        return self.usgs_sites

    def populate_usgs(self) -> None:
        sites = self.visible_usgs()
        self.loading_usgs = True
        self.usgs_list.clear()
        for site in sites:
            item = QListWidgetItem(f"{site['name']} — {site['site_no']} — {site['distance_km']:.0f} km")
            item.setData(Qt.ItemDataRole.UserRole, site["site_no"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if site["site_no"] in self.selected_usgs_ids else Qt.CheckState.Unchecked)
            self.usgs_list.addItem(item)
        self.loading_usgs = False
        self.study_map.set_usgs_sites([
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [site["lon"], site["lat"]]},
             "properties": {"id": site["site_no"], "name": site["name"]}} for site in sites])
        if not self.usgs_sites:
            self.usgs_status.setText("No USGS discharge gauges found within the gauge distance of the domain.")
        else:
            shown = "nearest " + str(len(sites)) if self.usgs_limit_to_max.isChecked() else str(len(sites))
            self.usgs_status.setText(
                f"{len(self.usgs_sites)} gauge(s) found; showing {shown}. Check them here or click them on the map, "
                "then add them as river source points.")

    def usgs_item_changed(self, item: QListWidgetItem) -> None:
        if self.loading_usgs:
            return
        identifier = item.data(Qt.ItemDataRole.UserRole)
        checked = item.checkState() == Qt.CheckState.Checked
        if checked and identifier not in self.selected_usgs_ids:
            self.selected_usgs_ids.append(identifier)
        elif not checked and identifier in self.selected_usgs_ids:
            self.selected_usgs_ids.remove(identifier)
        self.study_map.set_selected_usgs(self.selected_usgs_ids)

    def sync_usgs_checks(self) -> None:
        self.loading_usgs = True
        for row in range(self.usgs_list.count()):
            item = self.usgs_list.item(row)
            state = Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in self.selected_usgs_ids else Qt.CheckState.Unchecked
            if item.checkState() != state:
                item.setCheckState(state)
        self.loading_usgs = False

    def add_selected_usgs(self) -> None:
        chosen = [site for site in self.usgs_sites if site["site_no"] in self.selected_usgs_ids]
        if not chosen:
            QMessageBox.information(self, "No gauges selected",
                                    "Check one or more USGS gauges in the list, or click them on the map.")
            return
        self.study_map.add_features([
            role_feature("river_source", {"type": "Point", "coordinates": [site["lon"], site["lat"]]},
                         {"ID": f"riv{index + 1:03d}", "name": site["name"],
                          "usgs_site_no": site["site_no"], "source": "usgs_nwis"})
            for index, site in enumerate(chosen)])
        self.study_map.set_mode("none")
        self.usgs_status.setText(f"Added {len(chosen)} USGS gauge(s) as river source points.")

    def add_selected_stations(self) -> None:
        chosen = [station for station in self.coops_stations
                  if station["id"] in self.selected_station_ids and station.get("coverage_valid") is True]
        if not chosen:
            QMessageBox.information(self, "No stations selected",
                                    "Check one or more stations in the list, or click them on the map.")
            return
        self.sync_selected_waterlevel_draft()
        self.study_map.set_mode("none")
        self.wl_status.setText(
            f"{len(chosen)} {self.waterlevel_provider_label()} station(s) are included as water-level points.")

    def populate_regions(self) -> None:
        mode = self.region_mode.currentText(); self.region_choice.blockSignals(True); self.region_choice.clear()
        if mode == "Ocean basin":
            for code, name in BASINS.items(): self.region_choice.addItem(f"{name} ({code})", code)
        elif mode == "Continent / coast":
            for name in CONTINENTS: self.region_choice.addItem(name, name)
        else: self.region_choice.addItem("Tracks within about 500 km", "study")
        self.region_choice.blockSignals(False); self.populate_ib_years()

    def load_ibtracs(self, refresh: bool) -> None:
        if self.ibtracs_worker and self.ibtracs_worker.isRunning(): return
        self.ib_info.setText("Loading NOAA IBTrACS catalog…"); self.load_ibtracs_button.setEnabled(False)
        self.ibtracs_worker = IBTrACSWorker(self.root / "cache" / "ibtracs.since1980.list.v04r01.csv", refresh)
        self.ibtracs_worker.loaded.connect(self.ibtracs_loaded); self.ibtracs_worker.failed.connect(self.ibtracs_failed); self.ibtracs_worker.start()

    def ibtracs_loaded(self, records: list[dict[str, Any]], source: str) -> None:
        self.ibtracs = records; self.load_ibtracs_button.setEnabled(True); self.ib_info.setText(f"Loaded {len(records):,} storms from {source}."); self.populate_ib_years()

    def ibtracs_failed(self, message: str) -> None:
        self._riva_panic()
        self.load_ibtracs_button.setEnabled(True); self.ib_info.setText("IBTrACS error: " + message)

    def storm_in_region(self, storm: dict[str, Any]) -> bool:
        mode, choice = self.region_mode.currentText(), self.region_choice.currentData()
        if mode == "Ocean basin": return choice in storm["basins"]
        if mode == "Continent / coast":
            bounds = CONTINENTS.get(str(choice)); return bool(bounds and any(bounds[0] <= p["lon"] <= bounds[2] and bounds[1] <= p["lat"] <= bounds[3] for p in storm["points"]))
        if not self.domain_bounds: return False
        west, south, east, north = self.domain_bounds
        return any(west - 5 <= p["lon"] <= east + 5 and south - 5 <= p["lat"] <= north + 5 for p in storm["points"])

    def populate_ib_years(self) -> None:
        years = sorted({storm["season"] for storm in self.ibtracs if self.storm_in_region(storm)}, reverse=True); current = self.ib_year.currentText()
        self.ib_year.blockSignals(True); self.ib_year.clear(); self.ib_year.addItems([str(year) for year in years])
        if current in [str(year) for year in years]: self.ib_year.setCurrentText(current)
        elif 2024 in years: self.ib_year.setCurrentText("2024")
        self.ib_year.blockSignals(False); self.populate_ib_storms()

    def populate_ib_storms(self) -> None:
        try: year = int(self.ib_year.currentText())
        except Exception: year = -1
        storms = sorted([storm for storm in self.ibtracs if storm["season"] == year and self.storm_in_region(storm)], key=lambda storm: (storm["name"], storm["sid"]))
        self.ib_storm.blockSignals(True); self.ib_storm.clear()
        for storm in storms: self.ib_storm.addItem(f"{storm['name']} — {'/'.join(storm['basins'])} — {storm['sid']}", storm["sid"])
        self.ib_storm.blockSignals(False); self.ib_storm_changed()

    def ib_storm_changed(self, *_: Any) -> None:
        sid = self.ib_storm.currentData(); self.current_storm = next((storm for storm in self.ibtracs if storm["sid"] == sid), None)
        if not self.current_storm: return
        points = self.current_storm["points"]; winds = []
        for point in points:
            try: winds.append(float(point["wind"]))
            except Exception: pass
        peak = f"{max(winds):g} kt" if winds else "not reported"
        self.ib_info.setText(f"{self.current_storm['name']} | SID {sid} | {points[0]['time']} to {points[-1]['time']} UTC | peak WMO wind {peak}")
        start, end = self.selected_storm_period()
        self.start_time.setDateTime(QDateTime.fromMSecsSinceEpoch(round(start.timestamp() * 1000), Qt.TimeSpec.UTC))
        self.end_time.setDateTime(QDateTime.fromMSecsSinceEpoch(round(end.timestamp() * 1000), Qt.TimeSpec.UTC))
        self.event_map.set_layers(self.domain_geojson, points); self.study_map.set_layers(self.domain_geojson, points)

    def selected_storm_period(self) -> tuple[datetime, datetime]:
        points = self.current_storm["points"]
        parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        return parse(points[0]["time"]), parse(points[-1]["time"])

    def apply_selected_storm(self) -> None:
        if not self.current_storm: QMessageBox.information(self, "No storm selected", "Load IBTrACS and select a storm first."); return
        start, end = self.selected_storm_period()
        previous_event = self.event_name.text().strip()
        previous_start = self.start_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)
        previous_end = self.end_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)
        selected_event = f"{self.current_storm['name'].title()}_{self.current_storm['season']}"
        if self.session_mode == "imported" and self.config_path is not None:
            workspace = self.current_workspace_path() or self.config_path.parent.resolve()
            removed_hwm, transitioned = 0, False
        else:
            try:
                workspace, removed_hwm, transitioned = self.activate_event_workspace(
                    previous_event, selected_event)
            except Exception as exc:
                QMessageBox.critical(
                    self, "Cannot create event model folder",
                    "AutoCF could not create the event-specific project workspace.\n\n" + str(exc))
                return
        self.event_name.setText(selected_event)
        self.start_time.setDateTime(QDateTime.fromMSecsSinceEpoch(round(start.timestamp() * 1000), Qt.TimeSpec.UTC))
        self.end_time.setDateTime(QDateTime.fromMSecsSinceEpoch(round(end.timestamp() * 1000), Qt.TimeSpec.UTC))
        window_changed = (
            previous_start != self.start_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate)
            or previous_end != self.end_time.dateTime().toUTC().toString(Qt.DateFormat.ISODate))
        if transitioned or window_changed:
            # Prepared files and previews are tied to a simulation window. Clear
            # their references so the Forcing step cannot display or reuse the
            # previous event's rainfall, wind, or pressure products.
            manual_components = []
            for label, source, field in (
                    ("rainfall", self.rain_source, self.rain_file),
                    ("wind", self.wind_source, self.wind_file),
                    ("pressure", self.pressure_source, self.pressure_file)):
                if source.currentText().strip().lower() in {"file", "mrms"}:
                    if field.text().strip():
                        manual_components.append(label)
                else:
                    field.clear()
            self.forcing_layers = []
            self.forcing_manifest = {}
            self.load_forcing_previews()
            manual_note = (
                " Verify that the manually supplied " + ", ".join(manual_components)
                + " file(s) cover this event." if manual_components else "")
            self.atmosphere_status.setText(
                "Event changed. Prepare atmosphere forcing for the new simulation window."
                + manual_note)
        # Keep catalog identity synchronized immediately; collect_config writes
        # the same values during the automatic YAML save below.
        deep_set(self.raw_config, "event_catalog.source", "IBTrACS_v04r01")
        deep_set(self.raw_config, "event_catalog.sid", self.current_storm["sid"])
        deep_set(self.raw_config, "event_catalog.basin", ",".join(self.current_storm["basins"]))
        deep_set(self.raw_config, "event_catalog.season", self.current_storm["season"])
        self.mark_dirty()
        self.save_config()
        if self.session_mode == "new":
            self.project_session_status.setText(
                f"New model setup — event selected and draft YAML saved. Next: define the Study Area.\n{self.config_path}")
            self.go_to("study")
        transition_note = (
            f" Created event model folder {workspace.name}; removed {removed_hwm} old-event HWM observation(s); "
            "ocean, river, atmosphere, and evaluation forcing will be regenerated."
            if transitioned else
            f" Event workspace {workspace.name} is already active; dates and forcing signature were updated."
        )
        self.ib_info.setText(
            f"{self.current_storm['name']} | SID {self.current_storm['sid']} | "
            f"{start.isoformat()} to {end.isoformat()} UTC.{transition_note}")
        self.output.append(
            f"> Event applied: {selected_event}\n"
            f"Simulation: {start.isoformat()} to {end.isoformat()}\n"
            f"Project workspace: {workspace}\n"
            "Event-dependent forcing will be prepared inside this workspace.\n")
        self.statusBar().showMessage(
            f"Event applied and model workspace updated: {workspace}", 9000)

    def sfincs_changed(self, *_: Any) -> None:
        self.mark_dirty(); self.update_sfincs_preview()

    def reset_sfincs_defaults(self) -> None:
        for key, _, kind, default, _, _ in SFINCS_PARAMETERS:
            widget = self.advanced_widgets[key]; widget.setCurrentText(str(default)) if kind == "choice" else widget.setValue(float(default))
        self.mark_dirty(); self.update_sfincs_preview()

    def update_sfincs_preview(self) -> None:
        start = self.start_time.dateTime().toUTC().toString("yyyyMMdd HHmmss"); end = self.end_time.dateTime().toUTC().toString("yyyyMMdd HHmmss")
        lines = [f"tref                 = {start}", f"tstart               = {start}", f"tstop                = {end}", f"inputformat          = {self.input_format.currentText()}", f"outputformat         = {self.output_format.currentText()}", f"dtout                = {self.dtout.value():g}", f"dthisout             = {self.dthisout.value():g}", f"dtmaxout             = {self.dtmaxout.value():g}"]
        for key, _, _, _, _, _ in SFINCS_PARAMETERS:
            widget = self.advanced_widgets[key]; value = widget.currentText() if isinstance(widget, QComboBox) else f"{widget.value():g}"; lines.append(f"{key:<20} = {value}")
        self.sfincs_preview.setPlainText("\n".join(lines))

    def powershell(self, arguments: list[str]) -> list[str]:
        return self.runtime.command(arguments)

    def config_args(self) -> list[str] | None:
        if not self.ensure_event_workspace_current():
            return None
        if self.ocean_source.currentText().strip().lower() == "file" and not self.validate_file_forcing("ocean", False):
            QMessageBox.warning(self, "Ocean forcing unavailable", self.ocean_file_status.text())
            return None
        if self.river_source.currentText().strip().lower() == "file" and not self.validate_file_forcing("river", False):
            QMessageBox.warning(self, "River forcing unavailable", self.river_file_status.text())
            return None
        if self.config_path is None or not self.config_path.is_file(): QMessageBox.warning(self, "Configuration unavailable", "Open and save a YAML file first."); return None
        if self.dirty:
            if QMessageBox.question(self, "Unsaved changes", "Save changes before running?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes: return None
            if not self.save_config(): return None
        if not self.validate_config(silent=True): QMessageBox.warning(self, "Configuration invalid", "Correct validation errors before running."); return None
        return ["-Config", str(self.config_path)]

    def evaluation_args(self) -> list[str]:
        args = ["-Action", "Evaluate", "-EvaluationToleranceMinutes", f"{self.eval_tolerance.value():g}", "-EvaluationHmaxVmax", f"{self.eval_vmax.value():g}"]
        if not self.eval_hmax.isChecked(): args.append("-SkipEvaluationHmax")
        if not self.eval_observations.isChecked(): args.append("-SkipEvaluationObservations")
        return args

    def impact_args(self) -> list[str]:
        args = ["-Action", "Impact", "-ImpactThresholds", self.impact_thresholds.text().strip(), "-ImpactAnalysisResolution", f"{self.impact_resolution.value():g}"]
        if not self.impact_buildings.isChecked(): args.append("-SkipImpactBuildings")
        if not self.impact_roads.isChecked(): args.append("-SkipImpactRoads")
        if not self.impact_population.isChecked(): args.append("-SkipImpactPopulation")
        if self.force_downloads.isChecked(): args.append("-ForceImpactDownloads")
        return args

    def analysis_args(self, action: str) -> list[str]:
        args = ["-Action", action]
        if self.analysis_force.isChecked() and action in {"AnalysisPrepare", "AnalysisPrepareExposure", "AnalysisRun"}:
            args.append("-ForceAnalysis")
        if action == "AnalysisReport" and not self.impact_population.isChecked():
            args.append("-SkipAnalysisPopulation")
        return args

    def analysis_root_dir(self) -> Path:
        custom = deep_get(self.config, "analysis.root", None) if self.config else None
        if custom:
            return self.user_path(str(custom))
        return self.data_dir("attribution")

    def analysis_state(self) -> dict[str, Any]:
        has_model = bool(self.config_path and self.config_path.is_file())
        run = self.model_dir()
        root = self.analysis_root_dir()
        parent_input = run / "sfincs.inp"
        parent_output = run / "sfincs_map.nc"
        scenario_root = root / "scenarios"
        counterfactual_codes = (
            "000_none", "100_c", "010_p", "001_r",
            "110_cp", "101_cr", "011_pr",
        )
        prepared = sum(
            (scenario_root / code / "sfincs.inp").is_file()
            for code in counterfactual_codes
        )
        completed_counterfactuals = sum(
            (scenario_root / code / "sfincs_map.nc").is_file()
            for code in counterfactual_codes
        )
        parent_complete = parent_output.is_file()
        return {
            "has_model": has_model,
            "run": run,
            "root": root,
            "parent_built": parent_input.is_file(),
            "parent_complete": parent_complete,
            "manifest": (root / "scenario_manifest.json").is_file(),
            "prepared": prepared,
            "completed": completed_counterfactuals + int(parent_complete),
            "report": (self.results_dir("attribution", "reports") / "attribution_summary.json").is_file(),
        }

    def refresh_analysis_status(self, *_: Any) -> None:
        if not hasattr(self, "analysis_state_banner"):
            return
        state = self.analysis_state()
        def wrapped_path(value: Any) -> str:
            return str(value).replace("\\", "\\\u200b").replace("/", "/\u200b")
        self.analysis_yaml_status.setText(
            wrapped_path(self.config_path)
            if state["has_model"] else "No model YAML is loaded"
        )
        self.analysis_run_status.setText(
            wrapped_path(state["run"]) if state["has_model"] else "Not available"
        )
        self.analysis_parent_status.setText(
            "Completed — sfincs_map.nc found"
            if state["parent_complete"] else (
                "Built, but the SFINCS result is missing"
                if state["parent_built"] else "Model has not been built"
            )
        )
        self.analysis_prepared_status.setText(
            f"{state['prepared']} of 7 counterfactual folders"
            + (" — manifest found" if state["manifest"] else " — not prepared")
        )
        self.analysis_completed_status.setText(
            f"{state['completed']} of 8 scenario outputs"
        )
        self.analysis_report_status.setText(
            "Available — " + wrapped_path(state['root'] / 'results')
            if state["report"] else "Not generated"
        )

        if not state["has_model"]:
            banner = "NO ACTIVE MODEL"
            detail = "Load an existing AutoCF YAML. Analysis always uses the model shown here."
            button = "Load model YAML…"
            stage = "load"
        elif not state["parent_complete"]:
            banner = "PARENT MODEL NOT COMPLETE"
            detail = "Build and run the active model before preparing attribution scenarios."
            button = "Open Build & Run"
            stage = "parent"
        elif not state["manifest"] or state["prepared"] < 7:
            banner = "READY TO PREPARE"
            detail = "The parent result is ready. Create the counterfactual analysis workspace next."
            button = "Prepare analysis workspace"
            stage = "prepare"
        elif state["completed"] < 8:
            missing = 8 - int(state["completed"])
            banner = "SCENARIOS READY"
            detail = f"Run the {missing} missing scenario output{'s' if missing != 1 else ''}."
            button = "Run missing counterfactual simulations"
            stage = "run"
        elif not state["report"]:
            banner = "READY TO REPORT"
            detail = "All eight scenario outputs are available. Generate the analysis products."
            button = "Generate analysis report"
            stage = "report"
        else:
            banner = "ANALYSIS COMPLETE"
            detail = "The report is available. Open Results to inspect maps, plots, and tables."
            button = "Open analysis results"
            stage = "results"
        self.analysis_state_banner.setText(banner)
        self.analysis_next_detail.setText(detail)
        self.analysis_next_button.setText(button)
        self.analysis_next_stage = stage

        can_prepare = bool(
            state["has_model"] and state["parent_built"] and state["parent_complete"]
        )
        self.analysis_prepare_button.setEnabled(can_prepare)
        self.analysis_run_button.setEnabled(bool(
            state["manifest"] and state["prepared"] == 7
        ))
        self.analysis_report_button.setEnabled(state["completed"] == 8)
        self.analysis_all_button.setEnabled(can_prepare)
        self.analysis_simulation_status.setText(
            f"Prepared: {state['prepared']}/7    Completed outputs: {state['completed']}/8"
        )
        self.analysis_report_detail.setText(
            "All scenario outputs are ready. Reporting reuses them and does not run SFINCS."
            if state["completed"] == 8 else
            f"{state['completed']} of 8 scenario outputs are available. Complete the missing simulations first."
        )

    def run_analysis_next_step(self) -> None:
        stage = getattr(self, "analysis_next_stage", "load")
        if stage == "load":
            self.choose_config()
        elif stage == "parent":
            self.go_to("run")
        elif stage == "prepare":
            self.analysis_tabs.setCurrentIndex(1)
            self.prepare_analysis()
        elif stage == "run":
            self.analysis_tabs.setCurrentIndex(2)
            self.run_analysis_scenarios()
        elif stage == "report":
            self.analysis_tabs.setCurrentIndex(3)
            self.run_analysis_report()
        else:
            self.open_analysis_results()

    def open_analysis_run_folder(self) -> None:
        run = self.run_dir()
        if run.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(run)))
        else:
            QMessageBox.information(
                self, "Parent run folder", "The active model run folder does not exist yet.")

    def open_analysis_results(self) -> None:
        results = self.results_dir("attribution")
        if not results.is_dir():
            QMessageBox.information(
                self, "Analysis results", "No analysis report has been generated for the active model.")
            return
        self.go_to("results")
        self.refresh_results()

    def prepare_analysis(self) -> None:
        common = self.config_args()
        if common:
            commands = [self.powershell([*self.analysis_args("AnalysisPrepare"), *common])]
            if self.impact_buildings.isChecked():
                commands.append(self.powershell([*self.analysis_args("AnalysisPrepareExposure"), *common]))
            self.start_commands(
                commands, "> Prepare attribution workspace for the active model",
                "analysis_prepare")

    def run_analysis_scenarios(self) -> None:
        common = self.config_args()
        if common:
            self.start_commands(
                [self.powershell([*self.analysis_args("AnalysisRun"), *common])],
                "> Run missing counterfactual SFINCS simulations",
                "analysis_run")

    def run_analysis_report(self) -> None:
        common = self.config_args()
        if common:
            self.start_commands(
                [self.powershell([*self.analysis_args("AnalysisReport"), *common])],
                "> Generate attribution, impact, duration, equity, and DIS report",
                "analysis_report")

    def run_complete_analysis(self) -> None:
        common = self.config_args()
        if not common:
            return
        answer = QMessageBox.question(
            self,
            "Run all remaining analysis steps",
            "Use the active model shown on the Status tab, prepare its analysis workspace, "
            "run any missing counterfactual SFINCS simulations, and generate the report?\n\n"
            "This can run up to seven simulations. Existing completed outputs are reused "
            "unless recreation is selected in Setup.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        commands = [self.powershell([*self.analysis_args("AnalysisPrepare"), *common])]
        if self.impact_buildings.isChecked():
            commands.append(self.powershell([*self.analysis_args("AnalysisPrepareExposure"), *common]))
        commands.extend([
            self.powershell([*self.analysis_args("AnalysisRun"), *common]),
            self.powershell([*self.analysis_args("AnalysisReport"), *common]),
        ])
        self.start_commands(
            commands, "> Run all remaining attribution-analysis steps for the active model",
            "analysis_complete")

    def run_model(self) -> None:
        if not self.check_copernicus_setup():
            return
        common = self.config_args()
        if common: self.start_commands([self.powershell(["-Action", "Run", *common, "-Mode", self.run_operation.currentData(), "-CpuThreads", str(self.cpu_threads.value())])], f"> {self.run_operation.currentText()}")

    def run_evaluation(self) -> None:
        common = self.config_args()
        if common: self.start_commands([self.powershell([*self.evaluation_args(), *common])], "> AutoCF evaluation", "evaluation")

    def run_impact(self) -> None:
        common = self.config_args()
        if common: self.start_commands([self.powershell([*self.impact_args(), *common])], "> AutoCF impact attribution")

    def run_evaluation_and_impact(self) -> None:
        common = self.config_args()
        if common: self.start_commands([self.powershell([*self.evaluation_args(), *common]), self.powershell([*self.impact_args(), *common])], "> Evaluation + impact attribution", "evaluation")

    def system_status(self) -> None:
        self.start_commands([self.runtime.command(["-Action", "Status"])], "> AutoCF system status", "status")

    def install_runtime(self) -> None:
        self.restart_required = False
        self.restart_button.setVisible(False)
        commands = [self.runtime.install_command(), self.runtime.command(["-Action", "Status"])]
        requirement = (
            "Docker Desktop or Podman must be installed and running."
            if self.runtime.uses_container else
            "The UI will request administrator permission if WSL2 must be enabled."
        )
        self.start_commands(
            commands,
            f"> Install / repair {self.runtime.backend_name}\n{requirement}",
            "install",
        )

    def restart_host(self) -> None:
        command = self.runtime.restart_command()
        if command is None:
            QMessageBox.information(self, "Restart unavailable", "Restart this computer normally, then reopen AutoCF to continue installation.")
            return
        answer = QMessageBox.question(
            self, "Restart Windows",
            "Restart Windows now? Save work in every application before continuing.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes: return
        started = QProcess.startDetached(command[0], command[1:])
        ok = started[0] if isinstance(started, tuple) else bool(started)
        if ok: QApplication.quit()
        else: QMessageBox.warning(self, "Restart failed", "Windows could not be restarted. Restart it normally to continue installation.")

    def start_commands(self, commands: list[list[str]], heading: str, job_kind: str = "operation") -> None:
        if self.process is not None: QMessageBox.information(self, "AutoCF is busy", "Wait for or cancel the current operation."); return
        if hasattr(self, "riva_dock"):
            # The local model is deliberately unloaded before scientific work
            # so Riva never competes with SFINCS for CPU or memory.
            self.riva_dock.shutdown()
        self.output.clear(); self.output.append(heading + "\n"); self.log_dock.show(); self.job_log_tabs.setCurrentWidget(self.output); self.command_queue = commands; self.process_failed = False; self.job_kind = job_kind
        self.job_started_at = time.time(); self.sfincs_log_offsets = {}; self.sfincs_log_history_mode = False
        self.process_output_tail = ""; self.set_process_response_needed(False)
        self.sfincs_log_output.clear(); self.sfincs_log_status.setText("Waiting for SFINCS/model log files…")
        self.job_log_tabs.setTabText(1, "SFINCS live"); self.sfincs_log_timer.start()
        self.cancel_button.setEnabled(True); self.run_button.setEnabled(False); self._start_next()

    def _start_next(self) -> None:
        if not self.command_queue:
            self.poll_sfincs_logs(); self.sfincs_log_timer.stop()
            if self.sfincs_log_offsets:
                self.sfincs_log_status.setText(
                    "SFINCS/model log complete." if not self.process_failed else "SFINCS/model log stopped after failure.")
            self.job_log_tabs.setTabText(1, "SFINCS live")
            self.cancel_button.setEnabled(False); self.run_button.setEnabled(True); self.set_process_response_needed(False)
            if self.restart_required:
                self.job_status.setText("Restart required to continue installation")
            else:
                self.job_status.setText("Failed" if self.process_failed else "Completed successfully")
            self.refresh_results()
            if self.job_kind == "terrain" and hasattr(self, "terrain_status"):
                prepared = self.terrain_targets()
                self.terrain_status.setText(
                    "Prepared: " + "; ".join(f"{path.name} — {human_size(path.stat().st_size)}" for path in prepared)
                    if prepared else "The terrain step finished but no elevation raster was found; check the log.")
                if not self.process_failed:
                    self.go_to("terrain")
                    if not self.load_prepared_dem(announce=True) and prepared:
                        self.terrain_status.setText(
                            self.terrain_status.text() + " No map preview was written; check the log.")
            elif self.job_kind == "surface" and hasattr(self, "surface_status"):
                if not self.process_failed:
                    self.go_to("surface")
                    if not self.load_surface_previews(announce=True):
                        self.surface_status.setText(
                            "The surface step finished but no map layers were prepared; check the job log.")
                else:
                    self.load_surface_previews(announce=False)
                    self.surface_status.setText("Surface preparation failed; check the job log.")
            elif self.job_kind == "evaluation" and hasattr(self, "evaluation_tabs"):
                self.refresh_evaluation_plots(select_preferred=not self.process_failed)
                if not self.process_failed:
                    self.go_to("evaluation")
                    self.evaluation_tabs.setCurrentIndex(1)
            elif self.job_kind in {"forcing_preview", "atmosphere"}:
                if not self.process_failed:
                    self.go_to("forcing")
                    self.load_forcing_previews(announce=True)
                    if self.job_kind == "atmosphere":
                        self.atmosphere_status.setText(
                            "Atmosphere forcing downloaded, prepared, and validated. Select a component to inspect it on the map.")
                else:
                    self.forcing_map_status.setText("Atmosphere forcing preparation failed; check the job log.")
                    if hasattr(self, "atmosphere_status"):
                        self.atmosphere_status.setText("Atmosphere forcing failed validation; check the job log for the exact component and reason.")
            elif self.job_kind.startswith("analysis_"):
                self.refresh_analysis_status()
                if self.process_failed:
                    self.go_to("impact")
                elif self.job_kind == "analysis_prepare":
                    self.go_to("impact")
                    self.analysis_tabs.setCurrentIndex(2)
                elif self.job_kind == "analysis_run":
                    self.go_to("impact")
                    self.analysis_tabs.setCurrentIndex(3)
                else:
                    self.go_to("results")
            return
        command = self.command_queue.pop(0); self.process = QProcess(self); self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment(); environment.insert("PYTHONIOENCODING", "utf-8"); environment.insert("PYTHONUTF8", "1")
        self.process.setProcessEnvironment(environment); self.process.setWorkingDirectory(str(self.root)); self.process.readyReadStandardOutput.connect(self.read_output); self.process.finished.connect(self.process_finished); self.process.errorOccurred.connect(self.process_error); self.job_status.setText("Running…"); self.set_process_response_needed(False); self.process.start(command[0], command[1:])

    @staticmethod
    def process_output_requests_response(text: str) -> bool:
        """Return True only when the latest process output looks like an input prompt."""
        tail = text.rstrip()
        if not tail:
            return False
        line = tail.splitlines()[-1].strip().casefold()
        patterns = (
            r".*(?:\[(?:y/n|yes/no)\]|\((?:y/n|yes/no)\))\s*:?\s*$",
            r"(?:press enter|type .+ and press enter)\s*:?\s*$",
            r"(?:enter|provide|select|choose) .+:\s*$",
        )
        return any(re.fullmatch(pattern, line) for pattern in patterns)

    def set_process_response_needed(self, needed: bool, prompt: str = "") -> None:
        active = bool(needed and self.process is not None
                      and self.process.state() != QProcess.ProcessState.NotRunning)
        self.process_response_panel.setVisible(active)
        self.process_input.setEnabled(active)
        self.send_input_button.setEnabled(active)
        if active:
            prompt = prompt.strip()
            self.process_action_label.setText(
                f"Action required: {prompt[:160]}" if prompt else
                "Action required: reply to the running job")
            self.process_input.setFocus()
        else:
            self.process_input.clear()

    def send_process_input(self) -> None:
        if self.process is None or self.process.state() == QProcess.ProcessState.NotRunning: return
        response = self.process_input.text()
        self.process.write((response + "\r\n").encode("utf-8"))
        self.process_output_tail = ""
        self.set_process_response_needed(False)

    def read_output(self) -> None:
        if self.process is None: return
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace").replace("\x00", "")
        if data:
            cursor = self.output.textCursor(); cursor.movePosition(cursor.MoveOperation.End); cursor.insertText(data); self.output.setTextCursor(cursor); self.output.ensureCursorVisible()
            self.process_output_tail = (self.process_output_tail + data)[-1600:]
            if self.process_output_requests_response(self.process_output_tail):
                prompt = self.process_output_tail.rstrip().splitlines()[-1]
                self.set_process_response_needed(True, prompt)

    def process_finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        self.read_output(); self.process = None
        if self.job_kind == "install" and code == 3010 and self.runtime.supports_restart:
            self.restart_required = True; self.command_queue.clear(); self.restart_button.setVisible(True)
            self.output.append("\nWindows enabled WSL2. Restart to continue the AutoCF runtime installation.\n")
            self._start_next(); return
        if code != 0:
            self.process_failed = True; self.command_queue.clear()
            self.output.append(f"\nFailed with exit code {code}.\n")
            self._riva_panic()
        self._start_next()

    def process_error(self, error: QProcess.ProcessError) -> None:
        self.output.append(f"\nCould not start process: {error}\n"); self.set_process_response_needed(False)
        self._riva_panic()
    def cancel_process(self) -> None:
        self.command_queue.clear(); self.process_failed = True
        if self.process is not None:
            process_id = int(self.process.processId())
            if os.name == "nt" and process_id > 0:
                # QProcess.kill() stops only the PowerShell wrapper. Terminate
                # its descendants too so WSL cannot keep downloading or writing
                # project files after the desktop reports that the job stopped.
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
            else:
                self.process.kill()
        self.set_process_response_needed(False); self.job_status.setText("Cancelled")

    def run_dir(self) -> Path: return self.user_path(self.output_dir.text().strip() or "./runs")

    def evaluation_plot_files(self) -> list[Path]:
        folder = self.results_dir("evaluation", "figures")
        if not folder.is_dir():
            return []
        files = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}]
        def order(path: Path) -> tuple[int, str]:
            name = path.name.lower()
            if name.startswith("hmax_"): return (0, name)
            if name == "sfincs_point_zs_vs_obs_all_gauges.png": return (1, name)
            if name.startswith("sfincs_point_zs_vs_obs"): return (2, name)
            return (3, name)
        return sorted(files, key=order)

    def evaluation_plot_label(self, path: Path) -> str:
        name = path.name.lower()
        if name.startswith("hmax_"):
            return "Maximum water level / flood depth"
        if name == "sfincs_point_zs_vs_obs_all_gauges.png":
            return "SFINCS vs NOAA — all gauges"
        if name.startswith("sfincs_point_zs_vs_obs"):
            station = path.stem.removeprefix("sfincs_point_zs_vs_").replace("_", " ")
            return f"SFINCS vs NOAA — {station}"
        return path.stem.replace("_", " ")

    def rebuild_evaluation_single_plot_tabs(self, files: list[Path]) -> None:
        """Keep the combined browser and add one fitted full panel per plot."""
        while self.evaluation_tabs.count() > 2:
            widget = self.evaluation_tabs.widget(2)
            self.evaluation_tabs.removeTab(2)
            widget.deleteLater()
        self.evaluation_single_plot_widgets = []
        used_labels: dict[str, int] = {}
        for path in files:
            full_label = self.evaluation_plot_label(path)
            if full_label == "Maximum water level / flood depth":
                short_label = "Maximum water"
            elif full_label == "SFINCS vs NOAA — all gauges":
                short_label = "All gauges"
            elif " — " in full_label:
                short_label = full_label.split(" — ", 1)[1]
            else:
                short_label = full_label
            used_labels[short_label] = used_labels.get(short_label, 0) + 1
            if used_labels[short_label] > 1:
                short_label = f"{short_label} ({used_labels[short_label]})"
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            image = FittedPixmapLabel(f"Could not load {path.name}")
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                image.set_fitted_pixmap(pixmap)
            scroll.setWidget(image)
            scroll.setToolTip(str(path))
            self.evaluation_tabs.addTab(scroll, short_label)
            self.evaluation_tabs.setTabToolTip(self.evaluation_tabs.count() - 1, full_label)
            self.evaluation_single_plot_widgets.append(scroll)

    def refresh_evaluation_plots(self, select_preferred: bool = False) -> None:
        if not hasattr(self, "evaluation_plot_list"):
            return
        selected = self.evaluation_plot_list.currentItem()
        selected_path = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        files = self.evaluation_plot_files()
        self.rebuild_evaluation_single_plot_tabs(files)
        self.evaluation_plot_list.blockSignals(True); self.evaluation_plot_list.clear()
        selected_row = -1
        for row, path in enumerate(files):
            item = QListWidgetItem(self.evaluation_plot_label(path)); item.setToolTip(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path)); self.evaluation_plot_list.addItem(item)
            if str(path) == selected_path: selected_row = row
        self.evaluation_plot_list.blockSignals(False)
        self.evaluation_plot_status.setText(
            f"{len(files)} evaluation plot{'s' if len(files) != 1 else ''} — {self.results_dir('evaluation', 'figures')}"
            if files else "No plots yet. Run evaluation with map and gauge products selected.")
        if files and (select_preferred or selected_row >= 0):
            self.evaluation_plot_list.setCurrentRow(selected_row if selected_row >= 0 else 0)
        elif not files:
            self.evaluation_plot_image.clear(); self.evaluation_plot_image.setText("No evaluation plots are available yet.")

    def evaluation_plot_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None) -> None:
        if current is None:
            return
        path = Path(str(current.data(Qt.ItemDataRole.UserRole)))
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.evaluation_plot_image.clear(); self.evaluation_plot_image.setText(f"Could not load {path.name}")
            return
        self.evaluation_plot_image.set_fitted_pixmap(pixmap)

    def refresh_results(self) -> None:
        run = self.run_dir(); self.results_path.setText(str(run)); self.result_tree.clear()
        if not run.is_dir(): self.result_overview.setPlainText("Run directory does not exist yet."); return
        visible_suffixes = {".png", ".jpg", ".jpeg", ".bmp", ".csv", ".json"}
        if self.uses_organized_run_layout():
            roots = {
                "Simulation": self.results_dir("simulation"),
                "Evaluation": self.results_dir("evaluation"),
                "Attribution & Analysis": self.results_dir("attribution"),
                "Single-run Exposure": self.results_dir("exposure"),
            }
            categories = {
                name: [path for path in folder.rglob("*")
                       if path.is_file() and path.suffix.lower() in visible_suffixes]
                if folder.is_dir() else []
                for name, folder in roots.items()
            }
        else:
            roots = {
                "Simulation": run / "agent_reports",
                "Evaluation": run / "agent_plots",
                "Attribution & Analysis": run / "agent_analysis" / "results",
                "Single-run Exposure": run / "agent_impact",
            }
            categories = {
                name: [path for path in folder.glob("*")
                       if path.is_file() and path.suffix.lower() in visible_suffixes]
                if folder.is_dir() else []
                for name, folder in roots.items()
            }

        product_total = 0
        file_total = 0
        format_order = {".png": 0, ".jpg": 1, ".jpeg": 2, ".bmp": 3, ".csv": 4, ".json": 5}
        for category, files in categories.items():
            groups: dict[str, list[Path]] = {}
            for path in sorted(set(files), key=lambda item: (item.stem.lower(), item.suffix.lower())):
                groups.setdefault(path.stem.lower(), []).append(path)
            if not groups: continue
            parent = QTreeWidgetItem([category, ""]); self.result_tree.addTopLevelItem(parent)
            for stem, related in sorted(groups.items()):
                related.sort(key=lambda path: (format_order.get(path.suffix.lower(), 99), path.name.lower()))
                formats = " · ".join(path.suffix[1:].upper() for path in related)
                size = sum(path.stat().st_size for path in related)
                label = re.sub(r"_(?:250|300)dpi$", "", stem).replace("_", " ").title()
                child = QTreeWidgetItem([label, f"{formats} — {human_size(size)}"])
                child.setData(0, Qt.ItemDataRole.UserRole, str(related[0]))
                child.setData(
                    0, Qt.ItemDataRole.UserRole + 1,
                    json.dumps([str(path) for path in related]),
                )
                child.setToolTip(0, "\n".join(str(path.relative_to(run)) for path in related))
                parent.addChild(child)
                product_total += 1
                file_total += len(related)
            parent.setExpanded(True)
        self.result_overview.setHtml(
            f"<h2>{html.escape(run.name)}</h2>"
            f"<p>{product_total} display products ({file_total} files).</p>"
            "<p>Technical rasters, NetCDF files, caches, logs, configurations, "
            "and scenario workspaces are kept out of this view.</p>")

    def result_selected(self) -> None:
        items = self.result_tree.selectedItems()
        if not items or not items[0].data(0, Qt.ItemDataRole.UserRole): return
        encoded = items[0].data(0, Qt.ItemDataRole.UserRole + 1)
        try:
            paths = ([Path(value) for value in json.loads(encoded)] if encoded else
                     [Path(items[0].data(0, Qt.ItemDataRole.UserRole))])
        except (TypeError, ValueError, json.JSONDecodeError):
            paths = [Path(items[0].data(0, Qt.ItemDataRole.UserRole))]

        image_path = next((path for path in paths if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}), None)
        csv_path = next((path for path in paths if path.suffix.lower() == ".csv"), None)
        text_path = next((path for path in paths if path.suffix.lower() == ".json"), None)
        self.result_image.clear(); self.result_image.setText("No figure is available for this product.")
        self.result_table.clear(); self.result_table.setRowCount(0); self.result_table.setColumnCount(0)

        if image_path is not None:
            pixmap = QPixmap(str(image_path)); self.result_image.set_fitted_pixmap(pixmap)
        if csv_path is not None:
            try:
                with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.reader(stream))
                if rows:
                    columns = max(len(row) for row in rows)
                    self.result_table.setRowCount(min(5000, max(0, len(rows) - 1)))
                    self.result_table.setColumnCount(columns)
                    self.result_table.setHorizontalHeaderLabels(rows[0] + [""] * (columns - len(rows[0])))
                    for row_index, row in enumerate(rows[1:5001]):
                        for column_index, value in enumerate(row):
                            self.result_table.setItem(row_index, column_index, QTableWidgetItem(value))
                    self.result_table.resizeColumnsToContents()
            except Exception as exc:
                self.result_overview.setPlainText(str(exc)); self.result_tabs.setCurrentIndex(0); return
        if text_path is not None:
            self.result_overview.setPlainText(
                text_path.read_text(encoding="utf-8", errors="replace")[:2_000_000])
        else:
            listing = "<br>".join(html.escape(str(path)) for path in paths)
            self.result_overview.setHtml(
                f"<h3>{html.escape(items[0].text(0))}</h3><p>{listing}</p>")
        self.result_tabs.setCurrentIndex(
            1 if image_path is not None else 2 if csv_path is not None else 0)

    def open_results_folder(self) -> None:
        if self.run_dir().is_dir(): QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_dir())))

    def go_to(self, key: str) -> None:
        if key in self.page_keys: self.navigation.setCurrentRow(self.page_keys.index(key))

    def _page_changed(self, row: int) -> None:
        self.pages.setCurrentIndex(row)
        if 0 <= row < len(self.page_keys):
            key = self.page_keys[row]
            self.show_map_on(key)
            if key == "terrain":
                # Reload from disk after entering Terrain so previews created by
                # an external/previous run also appear without reopening YAML.
                QTimer.singleShot(0, self.load_prepared_dem)
            elif key == "surface":
                # Surface-only and full builds write a manifest of every raster
                # that can be inspected on this shared map.
                QTimer.singleShot(0, self.load_surface_previews)
            elif key == "forcing":
                # Existing prepared NetCDF forcing is converted to lightweight
                # AOI-clipped time frames once, then browsed locally.
                QTimer.singleShot(0, lambda: self.load_forcing_previews(automatic=True))
            elif key == "impact":
                QTimer.singleShot(0, self.refresh_analysis_status)
        if 0 <= row < len(self.page_keys) and self.page_keys[row] == "results": self.refresh_results()
        if 0 <= row < len(self.page_keys) and self.page_keys[row] == "sfincs": self.update_sfincs_preview()

    def _apply_style(self, theme: str = "Dark") -> None:
        if theme == "Light":
            self.is_light_theme = True
            self.setStyleSheet("""
            QMainWindow,QWidget{background:#f4f6f8;color:#1e2933} QToolBar{background:#ffffff;border-bottom:1px solid #c9d2da;spacing:6px;padding:5px}
            QLabel#brand{font-size:22px;font-weight:800;color:#17212b}
            QLabel#analysisState{background:#e5f2f8;color:#12384f;border:1px solid #9fc8dd;border-radius:4px;padding:10px;font-size:15px;font-weight:800}
            QListWidget#navigation{background:#e9eef2;border:0;padding-top:8px;font-size:13px} QListWidget#navigation::item{padding:11px 12px;margin:1px 5px;border-radius:4px} QListWidget#navigation::item:selected{background:#b9ddf2;color:#12384f}
            QGroupBox{border:1px solid #bcc7cf;border-radius:5px;margin-top:12px;padding:12px 8px 8px;font-weight:650} QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 5px;background:#f4f6f8}
            QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QDateTimeEdit,QTextEdit,QTableWidget,QTreeWidget,QTabWidget::pane{background:#ffffff;color:#1e2933;border:1px solid #aebbc5;border-radius:3px;padding:5px;selection-background-color:#a8d8f3;selection-color:#102a3b}
            QComboBox QAbstractItemView{background:#ffffff;color:#1e2933;selection-background-color:#a8d8f3;selection-color:#102a3b}
            QTabBar::tab{background:#e5ebef;color:#263640;border:1px solid #b7c2ca;padding:8px 15px} QTabBar::tab:selected{background:#b9ddf2;color:#12384f}
            QPushButton{background:#e4eaee;color:#1e2933;border:1px solid #9eacb7;border-radius:4px;padding:7px 12px} QPushButton:hover{background:#d6e0e7} QPushButton:disabled{color:#8b969e;background:#edf0f2} QPushButton#primary{background:#1976a8;color:white;border-color:#0f6694;font-weight:700}
            QDockWidget::title{background:#dfe6eb;color:#1e2933;padding:6px} QMainWindow::separator{background:#8aa0af;height:7px;width:7px} QMainWindow::separator:hover{background:#1976a8} QHeaderView::section{background:#dde5ea;color:#1e2933;padding:6px;border:1px solid #b2bec7} QLabel#mapFallback{background:#edf4f7;color:#425d70;border:1px solid #b5c6d1}
            QToolTip{background:#ffffff;color:#1e2933;border:1px solid #8c9aa5}
            """)
        elif theme == "System":
            self.setStyleSheet("")
            self.is_light_theme = self.palette().window().color().lightness() >= 128
        else:
            self.is_light_theme = False
            self.setStyleSheet("""
        QMainWindow,QWidget{background:#191b1d;color:#eef1f4} QToolBar{background:#202326;border-bottom:1px solid #34383c;spacing:6px;padding:5px}
        QLabel#brand{font-size:22px;font-weight:800;color:white}
        QLabel#analysisState{background:#233a48;color:#e9f6fd;border:1px solid #3e7898;border-radius:4px;padding:10px;font-size:15px;font-weight:800}
        QListWidget#navigation{background:#151719;border:0;padding-top:8px;font-size:13px} QListWidget#navigation::item{padding:11px 12px;margin:1px 5px;border-radius:4px} QListWidget#navigation::item:selected{background:#245273;color:white}
        QGroupBox{border:1px solid #353a3f;border-radius:5px;margin-top:12px;padding:12px 8px 8px;font-weight:650} QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 5px}
        QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QDateTimeEdit,QTextEdit,QTableWidget,QTreeWidget,QTabWidget::pane{background:#26292c;border:1px solid #41464b;border-radius:3px;padding:5px;selection-background-color:#28688f}
        QTabBar::tab{background:#222529;border:1px solid #3a3f44;padding:8px 15px} QTabBar::tab:selected{background:#315d78}
        QPushButton{background:#353a3e;border:1px solid #555d63;border-radius:4px;padding:7px 12px} QPushButton:hover{background:#454c51} QPushButton:disabled{color:#7b8084;background:#2c2f32} QPushButton#primary{background:#236b94;border-color:#3aa5e8;font-weight:700}
        QDockWidget::title{background:#25282b;padding:6px} QMainWindow::separator{background:#53616b;height:7px;width:7px} QMainWindow::separator:hover{background:#3aa5e8} QHeaderView::section{background:#303438;color:white;padding:6px;border:1px solid #454a4f} QLabel#mapFallback{background:#20272c;color:#9bb3c4;border:1px solid #3c4d58}
        """)
        if hasattr(self, "riva_anchor_dock"):
            panel_color = "#f4f6f8" if self.is_light_theme else "#191b1d"
            border_color = "#8aa0af" if self.is_light_theme else "#53616b"
            self.riva_anchor_dock.setStyleSheet(
                f"QDockWidget#rivaAnchorDock{{background:{panel_color};"
                f"border-left:1px solid {border_color}}}"
                f"QWidget#rivaAnchor{{background:{panel_color}}}"
            )
            self.riva_pet.set_theme(self.is_light_theme)
            self.riva_dock.set_theme(self.is_light_theme)
        map_theme = "Light" if self.is_light_theme else "Dark"
        if hasattr(self, "study_map"):
            self.study_map.set_theme(map_theme)
            self.event_map.set_theme(map_theme)
        if hasattr(self, "validity") and hasattr(self, "advanced_widgets"):
            self.validate_config(silent=True)

    def closeEvent(self, event: Any) -> None:
        if self.dirty and QMessageBox.question(self, "Unsaved configuration", "Close without saving YAML changes?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            event.ignore(); return
        if self.process is not None:
            if QMessageBox.question(self, "Operation running", "Cancel operation and close?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                event.ignore(); return
            self.process.kill()
        if self.sfincs_worker is not None:
            self.sfincs_worker.requestInterruption()
            if not self.sfincs_worker.wait(5000):
                QMessageBox.information(self, "SFINCS setup is finishing", "Wait for the SFINCS check or download to stop, then close AutoCF again.")
                event.ignore(); return
        if hasattr(self, "riva_dock"):
            if not self.riva_dock.shutdown():
                QMessageBox.information(
                    self, "Riva is finishing",
                    "Wait for Riva's local operation to stop, then close AutoCF again.")
                event.ignore(); return
        if hasattr(self, "riva_pet"):
            self.riva_pet.close()
        event.accept()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--runtime-worker":
        return host_runtime_main(sys.argv[2:], package_root())
    QApplication.setOrganizationName("AutoCF")
    QApplication.setApplicationName("AutoCF Desktop")
    app = QApplication(sys.argv)
    window = AutoCFWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
