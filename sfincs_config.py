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


DEFAULT_TOPOBATHY = {
    "target_vertical_datum": "MSL",
    "buffer_km": 10,
    "primary": {
        "source": "NOAA_CUDEM_1_9_ARCSEC",
        "vrt_url": (
            "https://coast.noaa.gov/htdata/raster2/elevation/"
            "NCEI_ninth_Topobathy_2014_8483/"
            "NCEI_ninth_Topobathy_2014_m8483_EPSG-4269.vrt"
        ),
        "output_resolution_m": 3,
    },
    "gapfill": {
        "source": "GEBCO_2024_15_ARCSEC",
        "cog_url": (
            "https://s3.us-west-2.amazonaws.com/"
            "us-west-2.opendata.source.coop/alexgleith/gebco-2024/GEBCO_2024.tif"
        ),
        "output_resolution_m": 500,
    },
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
    "search_radius_km": 150.0,
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
        pp = Path(str(parent)).expanduser()
        if not pp.is_absolute():
            pp = path.parent / pp
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
    s = os.path.expandvars(os.path.expanduser(str(value)))
    p = Path(s)
    if not p.is_absolute():
        p = config_dir / p
    return str(p.resolve())


def normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize paths and derive output/cache paths without duplicating YAML settings."""
    cfg = copy.deepcopy(cfg)
    meta = cfg.setdefault("_meta", {})
    cdir = Path(meta.get("config_dir", ".")).expanduser().resolve()

    project = cfg.setdefault("project", {})
    run_name = str(project.get("run_name") or project.get("name") or "sfincs_case")
    workspace = project.get("workspace_root")
    if workspace is not None:
        workspace_path = Path(_expand(workspace, cdir))
    else:
        # If output_dir is explicit, use its parent hierarchy only for derived
        # analysis paths. Otherwise default to the config directory.
        workspace_path = cdir

    output_raw = project.get("output_dir", "auto")
    if output_raw in (None, "", "auto"):
        output = workspace_path / "agent_runs" / run_name
    else:
        output = Path(_expand(output_raw, cdir))

    cache_raw = project.get("cache_dir", "auto")
    if cache_raw in (None, "", "auto"):
        cache = workspace_path / "agent_cache"
    else:
        cache = Path(_expand(cache_raw, cdir))

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
    geometry["path"] = _expand(geometry["path"], cdir)
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
    model["topobathy"]["target_vertical_datum"] = str(model["vertical_datum"])
    model["roughness"] = deep_merge(DEFAULT_ROUGHNESS, model.get("roughness", {}))
    model["subgrid"] = deep_merge(DEFAULT_SUBGRID, model.get("subgrid", {}))
    model.setdefault("infiltration", {"enabled": False})
    cfg["model"] = model

    forcing = cfg.setdefault("forcing", {})
    forcing.setdefault("precipitation", {"source": "era5"})
    forcing.setdefault("wind", {"source": "era5"})
    forcing.setdefault("pressure", {"source": "era5"})
    forcing.setdefault("ocean", {"source": "noaa_coops"})
    forcing.setdefault("river", {"source": "usgs_auto"})
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
    analysis.setdefault("enabled", False)
    analysis.setdefault("attribution", {})
    analysis.setdefault("exposure", {})
    cfg["analysis"] = analysis

    runtime = cfg.setdefault("runtime", {})
    runtime.setdefault("core_agent", "sfincs_agent.py")
    runtime.setdefault("evaluator", "sfincs_eval.py")
    runtime.setdefault("attribution_script", "sfincs_attribution.py")
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


def target_vertical_datum(cfg: Dict[str, Any]) -> str:
    return str(cfg["model"].get("vertical_datum", cfg["model"]["topobathy"].get("target_vertical_datum", "MSL")))


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
    ocean.setdefault("interval_minutes", 6)
    ocean.setdefault("max_station_distance_km", 150)
    ocean.setdefault("max_missing_fraction", 0.10)
    forcing["ocean_boundary"] = ocean

    river.setdefault("parameter_cd", "00060")
    river.setdefault("max_gage_distance_km", 50)
    river.setdefault("max_missing_fraction", 0.25)
    river.setdefault("unit_mult", 0.028316846592)
    river.setdefault("interval_minutes", 15)
    river.setdefault("max_gages", 12)
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
