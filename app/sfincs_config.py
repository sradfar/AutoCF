#!/usr/bin/env python3
"""
sfincs_config.py

Canonical configuration layer for AutoCF/SFINCS Agent.

The user edits only the canonical YAML. This module expands it into the
legacy/internal structure expected by the proven sfincs_agent.py hydraulic core.
Backend URLs, unit conversions, and compatibility defaults live here rather than
being repeated in case YAML files.

The canonical config supports `extends:` inheritance, so a subgrid overlay only
contains values that differ from the regular/base case.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from vertical_datum import normalize_vertical_datum, source_vertical_datum


DEFAULT_TOPOBATHY = {
    "target_vertical_datum": "NAVD88",
    "datum_policy": "align_gapfill",
    "datum_engine": "auto",
    "buffer_km": 10,
    "primary": {
        "source": "NOAA_CUDEM_1_9_ARCSEC",
        "output_resolution_m": 3,
        "vertical_datum": "NAVD88",
    },
    "gapfill": {
        "source": "GEBCO_2024_15_ARCSEC",
        "cog_url": (
            "https://s3.us-west-2.amazonaws.com/"
            "us-west-2.opendata.source.coop/alexgleith/gebco-2024/GEBCO_2024.tif"
        ),
        "output_resolution_m": 500,
        "vertical_datum": "MSL",
    },
}

BUILTIN_PRIMARY_TOPOBATHY_URLS = {
    "usgs_3dep": (
        "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
        "USGS_Seamless_DEM_13.vrt"
    ),
    "usgs_3dep_1_3_arcsec": (
        "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
        "USGS_Seamless_DEM_13.vrt"
    ),
    "gebco_2024_15_arcsec": DEFAULT_TOPOBATHY["gapfill"]["cog_url"],
    "gedtm30": (
        "https://s3.opengeohub.org/global/edtm/"
        "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v20250611.tif"
    ),
    "copernicus_glo_30": (
        "https://opentopography.s3.sdsc.edu/raster/COP30/COP30_hh.vrt"
    ),
}

DEFAULT_ROUGHNESS = {
    "source": "ESA_WorldCover_2021_v200",
    "resolution_m": 10,
    "tile_url_template": (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
        "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
    ),
    "buffer_km": 10,
    "manning_mapping": "default_esa_worldcover",
}

DEFAULT_ATMOSPHERE = {
    "source": "ERA5",
    "cache_subdir": "era5",
    "cds_dataset": "reanalysis-era5-single-levels",
    "spatial_buffer_degrees": 0.5,
    "variables": {
        "wind_u": {
            "era5_name": "10m_u_component_of_wind",
            "raw_short_name": "u10",
            "final_name": "wind_u",
            "unit_mult": 1.0,
            "unit_add": 0.0,
            "valid_min": -100.0,
            "valid_max": 100.0,
        },
        "wind_v": {
            "era5_name": "10m_v_component_of_wind",
            "raw_short_name": "v10",
            "final_name": "wind_v",
            "unit_mult": 1.0,
            "unit_add": 0.0,
            "valid_min": -100.0,
            "valid_max": 100.0,
        },
        "pressure": {
            "era5_name": "mean_sea_level_pressure",
            "raw_short_name": "msl",
            "final_name": "press",
            "unit_mult": 1.0,
            "unit_add": 0.0,
            "valid_min": 85000.0,
            "valid_max": 110000.0,
        },
        "precipitation": {
            "era5_name": "total_precipitation",
            "raw_short_name": "tp",
            "final_name": "precip",
            "unit_mult": 1000.0,
            "unit_add": 0.0,
            "valid_min": 0.0,
            "valid_max": 500.0,
        },
    },
    "precip_cumulative_input": True,
}

DEFAULT_AORC = {
    "bucket": "noaa-nws-aorc-v1-1-1km",
    "cache_subdir": "aorc",
    "spatial_buffer_degrees": 0.5,
}

DEFAULT_HYDROMT = {
    "use_hydromt_cli": True,
    "build_verbosity": "-vv",
    "active_zmin_m": -50.0,
    "active_zmax_m": 20.0,
    "boundary_include_buffer_m": 600.0,
    "boundary_zmax_m": 5.0,
    "boundary_point_spacing_m": 5000.0,
}

DEFAULT_SFINCS = {
    "executable": os.environ.get("SFINCS_EXECUTABLE", "sfincs"),
    "input_file": "sfincs.inp",
    "dtout_seconds": 3600,
    "dthisout_seconds": 360,
    "dtmaxout_seconds": 3600,
    "outputformat": "net",
    "inputformat": "bin",
    "additional_parameters": {
        "advection": 1,
        "alpha": 0.5,
        "huthresh": 0.05,
        "theta": 1.0,
        "qinf": 0.0,
        "viscosity": 1,
        "storecumprcp": 1,
        "storevelmax": 1,
    },
}

DEFAULT_SUBGRID = {
    "nlevels": 10,
    "nr_subgrid_pixels": 20,
    "nrmax": 2000,
    "buffer_cells": 0,
    "huthresh": 0.01,
    "manning_land": 0.04,
    "manning_sea": 0.02,
    "rgh_lev_land": 0.0,
    "landcover_output_resolution_m": 10.0,
    "write_dep_tif": True,
    "write_man_tif": True,
    "q_table_option": 2,
    "weight_option": "min",
}

DEFAULT_WATER_LEVEL_VALIDATION = {
    "enabled": True,
    "provider": "auto",
    "provider_order": ["noaa_coops", "gesla", "uhslc_rq", "uhslc_fast"],
    "station_selection": "strict_inside_domain",
    "max_stations": 12,
    "min_stations": 1,
    "candidate_limit_per_provider": 30,
    "min_observations": 4,
    "max_missing_fraction": 0.20,
    "datum_mode": "auto",
    "datum_alignment_window_hours": [0, 12],
    "min_alignment_pairs": 3,
    "exclude_alignment_window_from_scores": True,
    "time_tolerance_minutes": "auto",
    "reuse_existing_model_stations": True,
    "noaa": {
        "product": "water_level",
        "units": "metric",
        "time_zone": "gmt",
        "interval_minutes": 6,
    },
    "uhslc": {"dataset": "global_hourly_fast"},
}

DEFAULT_HWM_VALIDATION = {
    "enabled": True,
    "provider": "usgs_stn",
    "domain_buffer_km": 5,
    "accepted_qualities": ["excellent", "good", "fair"],
    "datum_policy": "require_match",
    "vertical_datum_offset_m": None,
}



def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _portable_path_text(value: Any, posix: Optional[bool] = None) -> str:
    """Translate a Windows absolute path when a project is evaluated in WSL."""
    text = os.path.expandvars(os.path.expanduser(str(value)))
    if posix is None:
        posix = os.name != "nt"
    if posix:
        text = text.replace("\\", "/")
        if len(text) >= 3 and text[0].isalpha() and text[1:3] == ":/":
            text = f"/mnt/{text[0].lower()}/{text[3:]}"
    return text


def _config_path(value: Any, config_dir: Path) -> Path:
    candidate = Path(_portable_path_text(value)).expanduser()
    if not candidate.is_absolute():
        candidate = config_dir / candidate
    return candidate.resolve()


def _read_config_raw(path: Path, _stack: Optional[List[Path]] = None) -> Dict[str, Any]:
    path = Path(path).expanduser().resolve()
    stack = list(_stack or [])
    if path in stack:
        chain = " -> ".join(str(p) for p in stack + [path])
        raise RuntimeError(f"Circular YAML inheritance detected: {chain}")
    if not path.exists():
        raise FileNotFoundError(path)

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Top-level YAML must be a mapping: {path}")

    parent = raw.pop("extends", None)
    if parent:
        # A Windows-authored project commonly contains C:\... here but every
        # backend/preview script executes inside WSL. Resolve inheritance with
        # the same cross-platform conversion used for all other config paths.
        pp = _config_path(parent, path.parent)
        cfg = deep_merge(_read_config_raw(pp, stack + [path]), raw)
    else:
        cfg = raw
    return cfg


def read_config(path: Path, _stack: Optional[List[Path]] = None) -> Dict[str, Any]:
    """Read inheritance first, then derive paths exactly once."""
    path = Path(path).expanduser().resolve()
    cfg = _read_config_raw(path, _stack)
    cfg.setdefault("_meta", {})["config_path"] = str(path)
    cfg["_meta"]["config_dir"] = str(path.parent)
    return normalize_config(cfg)

def _expand(value: Any, config_dir: Path) -> str:
    return str(_config_path(value, config_dir))


def normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize paths and derive output/cache paths without duplicating YAML settings."""
    cfg = copy.deepcopy(cfg)
    meta = cfg.setdefault("_meta", {})
    cdir = Path(meta.get("config_dir", ".")).expanduser().resolve()
    legacy_schema = any(key in cfg for key in ("geojson", "mesh", "topobathy", "landcover"))
    # A relative path belongs to the project configuration that contains it.
    # Never reinterpret project paths relative to the installed AutoCF package
    # or the process working directory. This keeps copied projects portable on
    # Windows, WSL, containers, and other users' systems.
    path_base = cdir

    # Windows v2 projects use the proven hydraulic-core schema.  Add the HPC
    # v2 canonical aliases without removing any legacy values; evaluation and
    # attribution can then run the exact same scientific implementation.
    if legacy_schema:
        old_geo = cfg.get("geojson", {}) or {}
        if old_geo.get("path"):
            cfg.setdefault("geometry", {}).setdefault("path", old_geo["path"])
        mesh = cfg.get("mesh", {}) or {}
        model_alias = cfg.setdefault("model", {})
        for old_key, new_key in (("dx_m", "dx_m"), ("crs", "crs"), ("rotated", "rotated")):
            if old_key in mesh:
                model_alias.setdefault(new_key, mesh[old_key])
        model_alias.setdefault("topobathy", copy.deepcopy(cfg.get("topobathy", {}) or {}))
        model_alias.setdefault("roughness", copy.deepcopy(cfg.get("landcover", {}) or {}))
        model_alias.setdefault("subgrid", copy.deepcopy(cfg.get("subgrid", {}) or {}))
        model_alias.setdefault("infiltration", copy.deepcopy(cfg.get("infiltration", {}) or {}))
        topo_datum = (cfg.get("topobathy", {}) or {}).get("target_vertical_datum")
        if topo_datum is not None:
            model_alias.setdefault("vertical_datum", topo_datum)
        forcing_alias = cfg.setdefault("forcing", {})
        if "ocean" not in forcing_alias and forcing_alias.get("ocean_boundary") is not None:
            forcing_alias["ocean"] = copy.deepcopy(forcing_alias["ocean_boundary"])
        validation_alias = cfg.setdefault("validation", {})
        noaa_validation = cfg.get("noaa_validation", {}) or {}
        evaluation_obs = (cfg.get("evaluation", {}) or {}).get("observations", {}) or {}
        water_level = validation_alias.setdefault("water_level", {})
        if noaa_validation:
            water_level.setdefault("enabled", bool(noaa_validation.get("enabled", True)))
            water_level.setdefault("provider", "auto")
            water_level.setdefault("max_stations", int(noaa_validation.get("max_stations", 12)))
            water_level.setdefault("max_missing_fraction", float(noaa_validation.get("max_missing_fraction", 0.20)))
            water_level.setdefault("noaa", {
                key: noaa_validation[key]
                for key in ("product", "datum", "units", "time_zone", "interval_minutes")
                if key in noaa_validation
            })
        if str(evaluation_obs.get("source", "")).lower() == "file":
            water_level.update({
                "enabled": True,
                "provider": "file",
                "locations": evaluation_obs.get("locations"),
                "timeseries": evaluation_obs.get("timeseries"),
            })
        external = cfg.get("external_validation", {}) or {}
        if external.get("high_water_marks"):
            validation_alias.setdefault("hwm", copy.deepcopy(external["high_water_marks"]))

    project = cfg.setdefault("project", {})
    run_name = str(project.get("run_name") or project.get("name") or "sfincs_case")
    workspace = project.get("workspace_root")
    if workspace is not None:
        workspace_path = Path(_expand(workspace, cdir))
    else:
        # If output_dir is explicit, use its parent hierarchy only for derived
        # analysis paths. Otherwise default to the config directory.
        workspace_path = path_base

    output_raw = project.get("output_dir", "auto")
    if output_raw in (None, "", "auto"):
        output = workspace_path / "runs" / run_name
    else:
        output = Path(_expand(output_raw, path_base))

    cache_raw = project.get("cache_dir", "auto")
    if cache_raw in (None, "", "auto"):
        cache = workspace_path / "cache"
    else:
        cache = Path(_expand(cache_raw, path_base))

    project["run_name"] = run_name
    project["workspace_root"] = str(workspace_path)
    project["output_dir"] = str(output)
    project["cache_dir"] = str(cache)
    project.setdefault("overwrite", False)
    cfg["project"] = project

    geometry = cfg.setdefault("geometry", {})
    if "path" not in geometry:
        # backward-compatible canonical alias
        old = cfg.get("geojson", {})
        if old.get("path"):
            geometry["path"] = old["path"]
    if not geometry.get("path"):
        raise RuntimeError("geometry.path is required.")
    geometry["path"] = _expand(geometry["path"], path_base)
    cfg["geometry"] = geometry

    model = cfg.setdefault("model", {})
    model.setdefault("mode", "regular")
    model.setdefault("dx_m", 200)
    model.setdefault("crs", "utm")
    model.setdefault("rotated", False)

    # One canonical vertical datum. Topobathy, NOAA ocean forcing, gauge
    # validation, and HWM comparison inherit from this unless explicitly
    # overridden for an advanced case. Refuse conflicting duplicate settings.
    topo_user = copy.deepcopy(model.get("topobathy", {}) or {})
    topo_datum = topo_user.get("target_vertical_datum")
    if "vertical_datum" not in model:
        model["vertical_datum"] = topo_datum or DEFAULT_TOPOBATHY["target_vertical_datum"]
    elif topo_datum is not None and str(topo_datum).upper() != str(model["vertical_datum"]).upper():
        raise RuntimeError(
            "Conflicting model.vertical_datum and model.topobathy.target_vertical_datum. "
            "Define the vertical datum once at model.vertical_datum."
        )
    topo_user.pop("target_vertical_datum", None)
    model["topobathy"] = deep_merge(DEFAULT_TOPOBATHY, topo_user)
    # A default URL belongs to its default source, not to the config field in
    # isolation. Re-resolve it whenever a source is changed or an older GUI
    # config omitted the URL.
    primary_user = topo_user.get("primary", {}) or {}
    primary = model["topobathy"]["primary"]
    primary_source = str(primary.get("source", "")).strip().lower()
    if primary_source in {"noaa_cudem_1_3_arcsec", "us_auto_topobathy_10m"}:
        primary["source"] = "NOAA_CUDEM_1_9_ARCSEC"
        primary["output_resolution_m"] = 3
        primary_source = "noaa_cudem_1_9_arcsec"
    if not primary_user.get("vrt_url"):
        primary_url = BUILTIN_PRIMARY_TOPOBATHY_URLS.get(primary_source)
        if primary_url:
            primary["vrt_url"] = primary_url
        else:
            primary.pop("vrt_url", None)
    if primary_source == "noaa_cudem_1_9_arcsec":
        primary.pop("vrt_url", None)
    gapfill_user = topo_user.get("gapfill", {}) or {}
    gapfill = model["topobathy"]["gapfill"]
    gapfill_source = str(gapfill.get("source", "")).strip().lower()
    if not gapfill_user.get("cog_url"):
        gapfill_url = BUILTIN_PRIMARY_TOPOBATHY_URLS.get(gapfill_source)
        if gapfill_url:
            gapfill["cog_url"] = gapfill_url
        elif gapfill_source != "none":
            gapfill.pop("cog_url", None)
    model["vertical_datum"] = normalize_vertical_datum(model["vertical_datum"])
    model["topobathy"]["target_vertical_datum"] = str(model["vertical_datum"])
    model["topobathy"].setdefault("datum_policy", "align_gapfill")
    datum_engine = str(model["topobathy"].setdefault("datum_engine", "auto")).lower()
    if datum_engine not in {"auto", "transformez", "local_alignment"}:
        raise RuntimeError(
            "model.topobathy.datum_engine must be auto, transformez, or local_alignment."
        )
    for source_role in ("primary", "gapfill"):
        source_cfg = model["topobathy"].get(source_role, {}) or {}
        if source_role == "gapfill" and str(source_cfg.get("source", "")).lower() == "none":
            continue
        native = source_vertical_datum(source_cfg)
        if native == "UNKNOWN":
            raise RuntimeError(
                f"model.topobathy.{source_role}.vertical_datum is required for a user or unknown elevation source."
            )
        source_cfg["vertical_datum"] = native
        model["topobathy"][source_role] = source_cfg
    additional = model["topobathy"].setdefault("additional_sources", [])
    if not isinstance(additional, list):
        raise RuntimeError("topobathy.additional_sources must be an ordered list.")
    for index, source_cfg in enumerate(additional, 3):
        if not isinstance(source_cfg, dict):
            raise RuntimeError(f"Terrain Source {index} must be a mapping.")
        source = str(source_cfg.get("source", "")).strip().lower()
        if source == "none":
            continue
        if not source:
            raise RuntimeError(f"Terrain Source {index}: source is required.")
        native = source_vertical_datum(source_cfg)
        if native == "UNKNOWN":
            raise RuntimeError(f"Terrain Source {index}: native vertical datum is required.")
        source_cfg["vertical_datum"] = native
        if source_cfg.get("path"):
            source_cfg["path"] = _expand(source_cfg["path"], path_base)
        if not source_cfg.get("cog_url") and not source_cfg.get("vrt_url"):
            url = BUILTIN_PRIMARY_TOPOBATHY_URLS.get(source)
            if url:
                source_cfg["cog_url"] = url
        resolution = float(source_cfg.get("output_resolution_m", 30))
        if not 0 < resolution <= 10000:
            raise RuntimeError(f"Terrain Source {index}: preparation resolution must be in (0, 10000] m.")
    model["roughness"] = deep_merge(DEFAULT_ROUGHNESS, model.get("roughness", {}))
    model["subgrid"] = deep_merge(DEFAULT_SUBGRID, model.get("subgrid", {}))
    model.setdefault("infiltration", {"enabled": False})
    cfg["model"] = model

    forcing = cfg.setdefault("forcing", {})
    forcing.setdefault("precipitation", {"source": "era5"})
    forcing.setdefault("wind", {"source": "era5"})
    forcing.setdefault("pressure", {"source": "era5"})
    forcing.setdefault("ocean", {"source": "noaa_coops"})
    # GloFAS supplies the global default. Existing projects that explicitly use
    # USGS keep that setting unchanged.
    forcing.setdefault("river", {"source": "glofas_auto"})
    cfg["forcing"] = forcing

    validation = cfg.setdefault("validation", {})
    validation.setdefault("strict", False)
    validation["water_level"] = deep_merge(
        DEFAULT_WATER_LEVEL_VALIDATION, validation.get("water_level", {}) or {}
    )
    validation["hwm"] = deep_merge(
        DEFAULT_HWM_VALIDATION, validation.get("hwm", {}) or {}
    )
    cfg["validation"] = validation

    analysis = cfg.setdefault("analysis", {})
    legacy_impact = cfg.get("impact", {}) or {} if legacy_schema else {}
    analysis.setdefault("enabled", bool(legacy_impact))
    attribution = analysis.setdefault("attribution", {})
    if legacy_impact.get("flood_depth_thresholds_m") is not None:
        attribution.setdefault(
            "depth_thresholds_m",
            copy.deepcopy(legacy_impact["flood_depth_thresholds_m"]),
        )
    attribution.setdefault("primary_min_depth_m", 0.10)
    exposure = analysis.setdefault("exposure", {})
    legacy_buildings = legacy_impact.get("buildings", {}) or {}
    legacy_population = legacy_impact.get("population", {}) or {}
    exposure.setdefault(
        "enabled",
        bool(legacy_buildings.get("enabled", True) or legacy_population.get("enabled", True))
        if legacy_impact else False,
    )
    exposure["buildings"] = deep_merge(
        {"enabled": True, "source": "overture"},
        exposure.get("buildings", legacy_buildings) or {},
    )
    exposure["population"] = deep_merge(
        {"enabled": True, "source": "worldpop"},
        exposure.get("population", legacy_population) or {},
    )
    if str(exposure["buildings"].get("source", "")).lower() == "overture_maps":
        exposure["buildings"]["source"] = "overture"
    if str(exposure["population"].get("source", "")).lower() == "worldpop_api_v2":
        exposure["population"]["source"] = "worldpop"
    if str(exposure["population"].get("year", "")).lower() == "simulation":
        exposure["population"]["year"] = "auto"
    if "geometry_simplify_m" in exposure["population"]:
        exposure["population"].setdefault(
            "polygon_simplify_m", exposure["population"]["geometry_simplify_m"]
        )
    if "timeout_seconds" in exposure["population"]:
        exposure["population"].setdefault(
            "task_timeout_seconds", exposure["population"]["timeout_seconds"]
        )
    exposure.setdefault("depth_definition", "excess_over_none")
    analysis["land_mask"] = deep_merge(
        {
            "enabled": True,
            "worldcover_water_classes": [80],
            "min_land_fraction": 0.5,
        },
        analysis.get("land_mask", {}) or {},
    )
    cfg["analysis"] = analysis

    runtime = cfg.setdefault("runtime", {})
    runtime.setdefault("core_agent", "sfincs_agent.py")
    runtime.setdefault("evaluator", "sfincs_eval.py")
    runtime.setdefault("attribution_script", "sfincs_attribution.py")
    runtime["sfincs"] = deep_merge(
        DEFAULT_SFINCS,
        runtime.get("sfincs", cfg.get("sfincs", {})) or {},
    )
    for key in ("core_agent", "evaluator", "attribution_script"):
        # Commands on PATH are allowed. Relative filenames are resolved to
        # config directory only when they contain a path separator or exist.
        val = str(runtime[key])
        expanded = os.path.expandvars(os.path.expanduser(val))
        p = Path(expanded)
        if p.is_absolute():
            runtime[key] = str(p)
        elif "/" in expanded or (cdir / p).exists():
            runtime[key] = str((cdir / p).resolve())
        else:
            runtime[key] = expanded
    cfg["runtime"] = runtime
    return cfg


def resolved_output_dir(cfg: Dict[str, Any]) -> Path:
    return Path(cfg["project"]["output_dir"]).expanduser().resolve()


def resolved_model_dir(cfg: Dict[str, Any]) -> Path:
    """Return the SFINCS working directory for either run-layout version."""
    from run_layout import run_layout

    return run_layout(cfg).model


def target_vertical_datum(cfg: Dict[str, Any]) -> str:
    return normalize_vertical_datum(
        cfg["model"].get("vertical_datum", cfg["model"]["topobathy"].get("target_vertical_datum", "NAVD88"))
    )


def _legacy_infiltration(model: Dict[str, Any]) -> Dict[str, Any]:
    obj = copy.deepcopy(model.get("infiltration", {}) or {})
    obj.setdefault("enabled", False)
    return obj


def build_legacy_config(
    cfg: Dict[str, Any],
    *,
    workflow_mode: str,
    observation_locations: Optional[Path] = None,
) -> Dict[str, Any]:
    """Expand canonical config to the stable hydraulic-core schema.

    This is intentionally internal. It centralizes all duplicate NOAA/USGS/
    atmosphere backend settings so the user never defines them twice.
    """
    cfg = normalize_config(cfg)
    model = cfg["model"]
    forcing = copy.deepcopy(cfg["forcing"])
    ocean = copy.deepcopy(forcing.pop("ocean", {}) or {})
    river = copy.deepcopy(forcing.get("river", {}) or {})
    forcing["ocean_boundary"] = ocean

    # Provider-specific canonical aliases.
    ocean_source = str(ocean.get("source", "noaa_coops")).lower()
    ocean.setdefault("datum", target_vertical_datum(cfg))
    ocean.setdefault("units", "metric")
    ocean.setdefault("time_zone", "gmt")
    ocean.setdefault("interval_minutes", 60 if ocean_source in {"gtsm", "gtsm_v3"} else 6)
    ocean.setdefault("max_station_distance_km", 150)
    ocean.setdefault("max_missing_fraction", 0.10)
    forcing["ocean_boundary"] = ocean

    river_source = str(river.get("source", "glofas_auto")).lower()
    river.setdefault("parameter_cd", "00060")
    river.setdefault("max_gage_distance_km", 50)
    river.setdefault("max_missing_fraction", 0.25)
    river.setdefault("unit_mult", 0.028316846592)
    river.setdefault("interval_minutes", 60 if river_source in {"glofas", "glofas_auto"} else 15)
    river.setdefault("max_gages", 12)
    river.setdefault("max_sources", river["max_gages"])
    river.setdefault("min_upstream_area_km2", 500.0)
    river.setdefault("system_version", "version_4_0")
    river.setdefault("product_type", "consolidated")
    forcing["river"] = river

    vcfg = cfg.get("validation", {})
    wlcfg = copy.deepcopy(vcfg.get("water_level", {}) or {})
    if not bool(wlcfg.get("enabled", True)) or observation_locations is None:
        eval_obs = {"source": "none"}
    else:
        eval_obs = {
            "source": "file",
            "locations": str(Path(observation_locations).resolve()),
        }

    hwm = copy.deepcopy(vcfg.get("hwm", {}) or {})
    hwm_enabled = bool(hwm.pop("enabled", True))
    hwm_provider = str(hwm.pop("provider", "usgs_stn"))
    hwm_legacy = {
        "enabled": hwm_enabled,
        "source": hwm_provider,
        "domain_buffer_km": hwm.pop("domain_buffer_km", 5),
        "accepted_qualities": hwm.pop("accepted_qualities", ["excellent", "good", "fair"]),
        "datum_policy": hwm.pop("datum_policy", "require_match"),
        "vertical_datum_offset_m": hwm.pop("vertical_datum_offset_m", None),
        **hwm,
    }

    sfincs_cfg = deep_merge(DEFAULT_SFINCS, cfg.get("runtime", {}).get("sfincs", {}))
    if cfg.get("sfincs"):
        sfincs_cfg = deep_merge(sfincs_cfg, cfg["sfincs"])

    legacy = {
        "project": {
            "name": cfg["project"]["run_name"],
            "output_dir": cfg["project"]["output_dir"],
            "cache_dir": cfg["project"]["cache_dir"],
            "overwrite": bool(cfg["project"].get("overwrite", False)),
        },
        "geojson": {"path": cfg["geometry"]["path"]},
        "workflow": {
            "mode": workflow_mode,
            "require_internet_for_build": workflow_mode in {"build_only", "build_run_direct", "validation_only"},
        },
        "simulation": copy.deepcopy(cfg["simulation"]),
        "model": {"mode": str(model.get("mode", "regular")).lower()},
        "mesh": {
            "type": "regular",
            "dx_m": float(model.get("dx_m", 200)),
            "crs": model.get("crs", "utm"),
            "rotated": bool(model.get("rotated", False)),
            "subgrid": str(model.get("mode", "regular")).lower() == "subgrid",
        },
        "topobathy": copy.deepcopy(model["topobathy"]),
        "landcover": copy.deepcopy(model["roughness"]),
        "subgrid": copy.deepcopy(model["subgrid"]),
        "infiltration": _legacy_infiltration(model),
        "forcing": forcing,
        "atmosphere": copy.deepcopy(DEFAULT_ATMOSPHERE),
        "aorc": copy.deepcopy(DEFAULT_AORC),
        # Legacy backend aliases are derived, never user-authored.
        "noaa_boundary": {
            "source": "NOAA_COOPS",
            "product": ocean.get("product", "water_level"),
            "datum": ocean["datum"],
            "units": ocean["units"],
            "time_zone": ocean["time_zone"],
            "interval_minutes": ocean["interval_minutes"],
            "max_station_distance_km": ocean["max_station_distance_km"],
            "max_missing_fraction": ocean["max_missing_fraction"],
            "min_valid_fraction": float(ocean.get("min_valid_fraction", 0.90)),
        },
        "evaluation": {"observations": eval_obs},
        "noaa_validation": {
            "enabled": False,  # canonical global station file is used instead
            "source": "NOAA_COOPS",
            "product": wlcfg.get("noaa", {}).get("product", "water_level"),
            "datum": wlcfg.get("noaa", {}).get("datum", target_vertical_datum(cfg)),
            "units": wlcfg.get("noaa", {}).get("units", "metric"),
            "time_zone": wlcfg.get("noaa", {}).get("time_zone", "gmt"),
            "interval_minutes": 6,
            "station_selection": "all_inside_domain",
            "max_missing_fraction": float(wlcfg.get("max_missing_fraction", 0.20)),
        },
        "usgs_rivers": {
            "enabled": str(river.get("source", "none")).lower() in {"usgs_auto", "usgs", "usgs_sites"},
            "source": "USGS_NWIS",
            "parameter_cd": river["parameter_cd"],
            "max_gage_distance_km": river["max_gage_distance_km"],
            "max_missing_fraction": river["max_missing_fraction"],
            "unit_mult": river["unit_mult"],
            "interval_minutes": river["interval_minutes"],
            "max_gages": river["max_gages"],
        },
        "external_validation": {
            "strict": bool(vcfg.get("strict", False)),
            "high_water_marks": hwm_legacy,
            # Deliberately no OPERA, GFM, SAR, or Google Flood Hub entries.
        },
        "hydromt": copy.deepcopy(DEFAULT_HYDROMT),
        "sfincs": sfincs_cfg,
        "postprocess": {
            "hmax_variable_candidates": ["hmax", "zsmax", "max_water_depth"],
            "make_hmax_png": True,
            "make_hmax_tif": True,
        },
    }

    # Preserve any explicit backend overrides in an advanced section without
    # exposing duplicate normal-user settings.
    backend = cfg.get("backend", {}) or {}
    for key in ("atmosphere", "aorc", "hydromt"):
        if key in backend:
            legacy[key] = deep_merge(legacy[key], backend[key])
    if "sfincs" in backend:
        legacy["sfincs"] = deep_merge(legacy["sfincs"], backend["sfincs"])

    # Keep the source canonical config for provenance only.
    legacy["_autocf"] = {
        "schema_version": int(cfg.get("schema_version", 2)),
        "canonical_config": cfg.get("_meta", {}).get("config_path"),
        "remote_sensing_validation": "disabled_by_design",
    }
    return legacy


def write_legacy_config(
    cfg: Dict[str, Any],
    path: Path,
    *,
    workflow_mode: str,
    observation_locations: Optional[Path] = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = build_legacy_config(
        cfg,
        workflow_mode=workflow_mode,
        observation_locations=observation_locations,
    )
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")
    return path


def dump_resolved_canonical(cfg: Dict[str, Any], path: Path) -> Path:
    out = copy.deepcopy(cfg)
    out.pop("_meta", None)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(out, sort_keys=False), encoding="utf-8")
    return path


def config_signature(cfg: Dict[str, Any]) -> str:
    import hashlib
    obj = copy.deepcopy(cfg)
    obj.pop("_meta", None)
    raw = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]
