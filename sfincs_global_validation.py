#!/usr/bin/env python3
"""
sfincs_global_validation.py

Global water-level validation for AutoCF.

Production validation intentionally excludes SAR/remote-sensing flood-extent
products. Water-level validation uses only stations inside/on the SFINCS domain and follows:
  1) NOAA CO-OPS where available (absolute datum when requested), then
  2) GESLA-4.1 where available, then
  3) UHSLC/JASL Research Quality hourly data where available, then
  4) UHSLC Fast Delivery hourly data as a global fallback, or
  5) a user-provided file.

UHSLC water levels are referenced to station zero. When their datum is not the
same as the SFINCS vertical datum, AutoCF estimates ONE constant datum offset
from a configured quiet/alignment window near the beginning of the simulation,
freezes it, excludes that window from event scores by default, and evaluates
the event thereafter. No event-time fitting is performed.
"""
from __future__ import annotations

import io
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from pyproj import Geod
from shapely.geometry import Point
from shapely.ops import unary_union

from sfincs_config import read_config, resolved_output_dir, target_vertical_datum

NOAA_MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
NOAA_DATA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
UHSLC_ERDDAP_ROOT = "https://uhslc.soest.hawaii.edu/erddap/tabledap"
GESLA_DATASET = "global_hourly_gesla"

GEOD = Geod(ellps="WGS84")


def log(msg: str) -> None:
    print(f"[global-validation] {msg}", flush=True)


def parse_time(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _domain_geometry(cfg: Dict[str, Any]):
    gdf = gpd.read_file(cfg["geometry"]["path"])
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    if "role" in gdf.columns:
        sel = gdf[gdf["role"].astype(str).str.lower().eq("domain")]
        if not sel.empty:
            return unary_union(sel.geometry)
    polys = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    if polys.empty:
        raise RuntimeError("No Polygon/MultiPolygon domain found in geometry.path")
    return unary_union(polys.geometry)


def _distance_to_domain_km(lon: float, lat: float, domain) -> float:
    # Nearest-point distance in a locally estimated UTM CRS gives stable ranking.
    gs = gpd.GeoSeries([domain, Point(lon, lat)], crs=4326)
    try:
        utm = gs.estimate_utm_crs()
        p = gs.to_crs(utm)
        return float(p.iloc[1].distance(p.iloc[0]) / 1000.0)
    except Exception:
        c = domain.centroid
        _, _, d = GEOD.inv(float(c.x), float(c.y), float(lon), float(lat))
        return float(d / 1000.0)


def _safe_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "station"


def _station_is_inside_domain(row: Any, domain) -> bool:
    """Return True only when a validation station is covered by the SFINCS domain.

    ``covers`` intentionally includes points exactly on the polygon boundary.
    Automatic provider search must never expand validation outside the modeled
    domain. This rule is separate from boundary-forcing station selection.
    """
    try:
        geom = row.get("geometry") if hasattr(row, "get") else None
    except Exception:
        geom = None
    try:
        if geom is not None and not geom.is_empty:
            point = geom
        else:
            point = Point(float(row["lon"]), float(row["lat"]))
        return bool(domain.covers(point))
    except Exception:
        return False


def _filter_stations_inside_domain(candidates: pd.DataFrame, domain) -> pd.DataFrame:
    """Filter provider candidates to stations physically inside/on the domain."""
    if candidates.empty:
        return candidates.copy()
    mask = candidates.apply(lambda row: _station_is_inside_domain(row, domain), axis=1)
    return candidates.loc[np.asarray(mask, dtype=bool)].copy()


def discover_noaa(domain, cfg: Dict[str, Any]) -> pd.DataFrame:
    r = requests.get(NOAA_MDAPI, params={"type": "waterlevels"}, timeout=90)
    r.raise_for_status()
    stations = r.json().get("stations", []) or []
    rows = []
    for st in stations:
        try:
            lon = float(st.get("lng", st.get("lon")))
            lat = float(st.get("lat"))
            sid = str(st.get("id"))
        except Exception:
            continue
        if not sid:
            continue
        rows.append(
            {
                "provider": "noaa_coops",
                "station_id": sid,
                "name": str(st.get("name", sid)),
                "country": "United States",
                "lon": lon,
                "lat": lat,
                "datum": str((cfg.get("validation", {}).get("water_level", {}).get("noaa", {}) or {}).get("datum", target_vertical_datum(cfg))),
                "distance_km": _distance_to_domain_km(lon, lat, domain),
            }
        )
    return pd.DataFrame(rows).sort_values("distance_km") if rows else pd.DataFrame()


def _uhslc_station_table(dataset: str = "global_hourly_fast") -> pd.DataFrame:
    # Keep a small essential metadata query that works across both the
    # Research Quality (global_hourly_rqds) and Fast Delivery datasets.
    essential = [
        "latitude", "longitude", "station_name", "station_country",
        "record_id", "reference_datum",
    ]
    optional = ["uhslc_id", "gloss_id", "ssc_id", "last_rq_date"]

    def request(variables: List[str]) -> pd.DataFrame:
        url = f"{UHSLC_ERDDAP_ROOT}/{dataset}.csv?" + ",".join(variables) + "&distinct()"
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text))

    # Some ERDDAP datasets omit one of the optional metadata variables. Try
    # the richer query first, then fall back to the common core.
    try:
        df = request(essential + optional)
    except Exception:
        df = request(essential)
        for col in optional:
            if col not in df.columns:
                df[col] = np.nan

    # ERDDAP CSV commonly inserts a units row after the header; numeric coercion
    # removes it without special-casing server versions.
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["record_id"] = pd.to_numeric(df["record_id"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude", "record_id"]).copy()
    df["longitude"] = np.where(df["longitude"] > 180.0, df["longitude"] - 360.0, df["longitude"])
    return df


def discover_uhslc(domain, cfg: Dict[str, Any], dataset: str = "global_hourly_fast") -> pd.DataFrame:
    src = _uhslc_station_table(dataset)
    rows = []
    for _, st in src.iterrows():
        lon = float(st["longitude"])
        lat = float(st["latitude"])
        rid = str(int(st["record_id"]))
        rows.append(
            {
                "provider": "uhslc",
                "dataset": dataset,
                "station_id": rid,
                "name": str(st.get("station_name", rid)),
                "country": str(st.get("station_country", "")),
                "lon": lon,
                "lat": lat,
                "datum": str(st.get("reference_datum", "station zero")),
                "uhslc_id": None if pd.isna(st.get("uhslc_id")) else str(st.get("uhslc_id")),
                "gloss_id": None if pd.isna(st.get("gloss_id")) else str(st.get("gloss_id")),
                "ssc_id": None if pd.isna(st.get("ssc_id")) else str(st.get("ssc_id")).strip(),
                "last_rq_date": None if pd.isna(st.get("last_rq_date")) else str(st.get("last_rq_date")),
                "distance_km": _distance_to_domain_km(lon, lat, domain),
            }
        )
    return pd.DataFrame(rows).sort_values("distance_km") if rows else pd.DataFrame()


def discover_gesla(domain, cfg: Dict[str, Any]) -> pd.DataFrame:
    variables = [
        "latitude", "longitude", "station_name", "agency_id",
        "station_code", "station_country_code", "record_id",
    ]
    url = f"{UHSLC_ERDDAP_ROOT}/{GESLA_DATASET}.csv?" + ",".join(variables) + "&distinct()"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude", "record_id"]).copy()
    df["longitude"] = np.where(df["longitude"] > 180.0, df["longitude"] - 360.0, df["longitude"])
    rows = []
    for _, st in df.iterrows():
        lon, lat = float(st["longitude"]), float(st["latitude"])
        rid = str(st["record_id"]).strip()
        if not rid or rid.lower() == "nan":
            continue
        rows.append({
            "provider": "gesla",
            "dataset": GESLA_DATASET,
            "station_id": rid,
            "name": str(st.get("station_name", rid)),
            "country": str(st.get("station_country_code", "")),
            "agency_id": str(st.get("agency_id", "")),
            "station_code": str(st.get("station_code", "")),
            # GESLA-4.1 ERDDAP declares datum information as unspecified.
            # Therefore automatic evaluation is datum-neutral.
            "datum": "unspecified",
            "lon": lon,
            "lat": lat,
            "distance_km": _distance_to_domain_km(lon, lat, domain),
        })
    return pd.DataFrame(rows).sort_values("distance_km") if rows else pd.DataFrame()


def fetch_gesla(record_id: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    variables = "time,sea_level,flag1,flag2"
    rid = quote(json.dumps(str(record_id)), safe="")
    s = quote(json.dumps(start.strftime("%Y-%m-%dT%H:%M:%SZ")), safe=":-TZ")
    e = quote(json.dumps(end.strftime("%Y-%m-%dT%H:%M:%SZ")), safe=":-TZ")
    query = f"{variables}&record_id={rid}&time>={s}&time<={e}"
    url = f"{UHSLC_ERDDAP_ROOT}/{GESLA_DATASET}.csv?" + query
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        raise RuntimeError(f"GESLA record_id={record_id}: empty response")
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df["sea_level"] = pd.to_numeric(df["sea_level"], errors="coerce")
    if "flag2" in df.columns:
        df["flag2"] = pd.to_numeric(df["flag2"], errors="coerce")
        df = df[df["flag2"].eq(1)]
    if "flag1" in df.columns:
        df["flag1"] = pd.to_numeric(df["flag1"], errors="coerce")
    df = df.dropna(subset=["time", "sea_level"]).copy()
    df = df[df["sea_level"] > -90.0]
    if df.empty:
        raise RuntimeError(f"GESLA record_id={record_id}: no quality-usable data")
    out = pd.DataFrame({"time": df["time"], "observed_zs_m": df["sea_level"].astype(float)})
    if "flag1" in df.columns:
        out["quality_flag"] = df["flag1"].to_numpy()
    return out


def fetch_noaa(station_id: str, start: pd.Timestamp, end: pd.Timestamp, cfg: Dict[str, Any]) -> pd.DataFrame:
    ncfg = (cfg.get("validation", {}).get("water_level", {}).get("noaa", {}) or cfg.get("noaa", {}) or {})
    params = {
        "begin_date": start.strftime("%Y%m%d %H:%M"),
        "end_date": end.strftime("%Y%m%d %H:%M"),
        "station": station_id,
        "product": ncfg.get("product", "water_level"),
        "datum": ncfg.get("datum", target_vertical_datum(cfg)),
        "time_zone": ncfg.get("time_zone", "gmt"),
        "units": ncfg.get("units", "metric"),
        "format": "json",
        "application": "autocf",
    }
    r = requests.get(NOAA_DATA, params=params, timeout=120)
    r.raise_for_status()
    js = r.json()
    if "error" in js:
        raise RuntimeError(f"NOAA {station_id}: {js['error']}")
    data = js.get("data", []) or []
    rows = []
    for item in data:
        try:
            rows.append((pd.Timestamp(item["t"], tz="UTC"), float(item["v"])))
        except Exception:
            continue
    if not rows:
        raise RuntimeError(f"NOAA {station_id}: no usable observations in requested window")
    return pd.DataFrame(rows, columns=["time", "observed_zs_m"])


def fetch_uhslc(record_id: str, start: pd.Timestamp, end: pd.Timestamp, dataset: str = "global_hourly_fast") -> pd.DataFrame:
    # ERDDAP query values use quoted ISO timestamps.
    variables = "time,sea_level,quality"
    s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    e = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = (
        f"{variables}"
        f"&record_id={int(record_id)}"
        f'&time>="{s}"'
        f'&time<="{e}"'
    )
    url = f"{UHSLC_ERDDAP_ROOT}/{dataset}.csv?" + query
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        raise RuntimeError(f"UHSLC record_id={record_id}: empty response")
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df["sea_level"] = pd.to_numeric(df["sea_level"], errors="coerce")
    if "quality" in df:
        df["quality"] = pd.to_numeric(df["quality"], errors="coerce")
    df = df.dropna(subset=["time", "sea_level"]).copy()
    if df.empty:
        raise RuntimeError(f"UHSLC record_id={record_id}: no usable data")
    # Official UHSLC sea_level units are millimeters.
    out = pd.DataFrame(
        {
            "time": df["time"],
            "observed_zs_m": df["sea_level"].astype(float) / 1000.0,
        }
    )
    if "quality" in df:
        out["quality"] = df["quality"].to_numpy()
    return out


def _event_data_available(row: pd.Series, start: pd.Timestamp, end: pd.Timestamp, cfg: Dict[str, Any]) -> pd.DataFrame:
    provider = str(row["provider"])
    sid = str(row["station_id"])
    if provider == "noaa_coops":
        return fetch_noaa(sid, start, end, cfg)
    if provider == "gesla":
        return fetch_gesla(sid, start, end)
    if provider.startswith("uhslc"):
        default_dataset = "global_hourly_rqds" if provider == "uhslc_rq" else "global_hourly_fast"
        return fetch_uhslc(sid, start, end, str(row.get("dataset", default_dataset)))
    raise RuntimeError(f"Unsupported automatic provider {provider}")


def _observation_coverage_fraction(
    row: pd.Series,
    obs: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cfg: Dict[str, Any],
) -> float:
    provider = str(row.get("provider", "")).lower()
    wlcfg = cfg.get("validation", {}).get("water_level", {}) or {}
    if provider == "noaa_coops":
        minutes = float((wlcfg.get("noaa", {}) or {}).get("interval_minutes", 6))
    elif provider == "gesla" or provider.startswith("uhslc"):
        minutes = 60.0
    else:
        # User files may be irregular; only require the configured minimum count.
        return 1.0
    span_min = max(0.0, (end - start).total_seconds() / 60.0)
    expected = max(1, int(math.floor(span_min / minutes)) + 1)
    n = int(pd.DatetimeIndex(pd.to_datetime(obs["time"], utc=True, errors="coerce")).dropna().nunique())
    return float(min(1.0, n / expected))


def prepare_water_level_validation(cfg: Dict[str, Any]) -> Optional[Path]:
    """Discover event-valid stations and cache their observations on the login node."""
    cfg = dict(cfg)
    vcfg = cfg.get("validation", {}).get("water_level", {}) or {}
    if not bool(vcfg.get("enabled", True)):
        log("water-level validation disabled")
        return None

    run = resolved_output_dir(cfg)
    outdir = run / "agent_validation" / "water_level"
    outdir.mkdir(parents=True, exist_ok=True)

    # Once a model is complete, its point_zs columns are tied to the observation
    # point ordering used at build time. Reuse that exact station file rather
    # than silently changing the station ranking during a later evaluation.
    existing_station_file = outdir / "stations.geojson"
    if (
        bool(vcfg.get("reuse_existing_model_stations", True))
        and (run / "sfincs_his.nc").exists()
        and existing_station_file.exists()
    ):
        log(
            f"reusing completed-model station order: {existing_station_file} "
            "(point_zs column mapping preserved; evaluation still excludes out-of-domain stations)"
        )
        return existing_station_file

    if str(vcfg.get("provider", "auto")).lower() == "file":
        loc = Path(str(vcfg["locations"])).expanduser().resolve()
        ts = Path(str(vcfg["timeseries"])).expanduser().resolve()
        if not loc.exists():
            raise FileNotFoundError(loc)
        if not ts.exists():
            raise FileNotFoundError(ts)
        target = outdir / "stations.geojson"
        g = gpd.read_file(loc)
        if g.crs is None:
            g = g.set_crs(4326)
        g = g.to_crs(4326).reset_index(drop=True)
        # Preserve a user station identifier, but guarantee HydroMT-compatible
        # integer IDs for the observation-point file.
        sid_col = next((c for c in ("provider_station_id", "station_id", "id", "name") if c in g.columns), None)
        if sid_col is None:
            g["provider_station_id"] = [str(i + 1) for i in range(len(g))]
        else:
            g["provider_station_id"] = g[sid_col].astype(str)
        g["station_id"] = g["provider_station_id"].astype(str)
        g["provider"] = "file"
        g["ID"] = np.arange(1, len(g) + 1)
        if "datum" not in g.columns:
            g["datum"] = str(vcfg.get("datum", ""))
        g.to_file(target, driver="GeoJSON")
        obs_target = outdir / "user_observations.csv"
        pd.read_csv(ts).to_csv(obs_target, index=False)
        manifest = {
            "status": "ready",
            "mode": "user_file",
            "stations_file": str(target),
            "timeseries_file": str(obs_target),
            "n_stations": int(len(g)),
        }
        (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return target

    start = parse_time(cfg["simulation"]["start"])
    end = parse_time(cfg["simulation"]["end"])

    # For an already-completed model, preserve the observation-point ordering
    # that was actually used to generate sfincs_his.nc. This prevents a new
    # station-discovery ranking from being compared against old point_zs columns.
    legacy_obs = run / "agent_gis" / "noaa_validation_obs.geojson"
    if (
        bool(vcfg.get("reuse_existing_model_stations", True))
        and (run / "sfincs_his.nc").exists()
        and legacy_obs.exists()
    ):
        old = gpd.read_file(legacy_obs)
        if old.crs is None:
            old = old.set_crs(4326)
        old = old.to_crs(4326).reset_index(drop=True)
        rows = []
        for i, rr in old.iterrows():
            sid = None
            for col in old.columns:
                if col == "geometry":
                    continue
                m = re.search(r"\b(\d{7})\b", str(rr[col]))
                if m:
                    sid = m.group(1)
                    break
            if sid is None:
                continue
            try:
                obs = fetch_noaa(sid, start, end, cfg)
            except Exception:
                continue
            obs_path = outdir / f"obs_noaa_coops_{sid}.csv"
            obs.to_csv(obs_path, index=False)
            rows.append({
                "ID": i + 1,
                "provider": "noaa_coops",
                "station_id": sid,
                "provider_station_id": sid,
                "name": str(rr.get("name", rr.get("station", sid))),
                "country": "United States",
                "lon": float(rr.geometry.x),
                "lat": float(rr.geometry.y),
                "datum": str((vcfg.get("noaa", {}) or {}).get("datum", target_vertical_datum(cfg))),
                "distance_km": np.nan,
                "observation_path": str(obs_path),
                "n_observations": int(len(obs)),
                "geometry": rr.geometry,
            })
        if rows:
            gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)
            station_file = outdir / "stations.geojson"
            gdf.to_file(station_file, driver="GeoJSON")
            manifest = {
                "status": "ready",
                "mode": "reused_existing_model_station_order",
                "stations_file": str(station_file),
                "stations": [{k: v for k, v in r.items() if k != "geometry"} for r in rows],
                "note": "Existing SFINCS observation-point ordering was preserved.",
            }
            (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
            log(f"reused {len(rows)} NOAA station(s) from completed model observation order")
            return station_file

    domain = _domain_geometry(cfg)
    # Validation has a strict spatial eligibility rule: a station must lie
    # inside or exactly on the SFINCS domain polygon. ``search_radius_km`` is
    # retained in old YAML files for backward compatibility but is NOT used
    # for validation station eligibility. Boundary-forcing selection is separate.
    max_stations = int(vcfg.get("max_stations", 12))
    min_stations = int(vcfg.get("min_stations", 1))
    candidate_limit = int(vcfg.get("candidate_limit_per_provider", 30))

    provider = str(vcfg.get("provider", "auto")).lower()
    if provider == "auto":
        order = [str(x).lower() for x in vcfg.get("provider_order", ["noaa_coops", "gesla", "uhslc_rq", "uhslc_fast"])]
    else:
        order = [provider]

    selected_rows: List[Dict[str, Any]] = []
    cache_rows: List[Dict[str, Any]] = []
    provider_errors: Dict[str, str] = {}

    for prov in order:
        try:
            if prov in {"noaa", "noaa_coops", "coops"}:
                candidates = discover_noaa(domain, cfg)
                canonical = "noaa_coops"
            elif prov in {"gesla", "gesla4", "gesla4.1"}:
                candidates = discover_gesla(domain, cfg)
                canonical = "gesla"
            elif prov in {"uhslc", "jasl", "uhslc_fast", "uhslc_rq"}:
                if prov in {"jasl", "uhslc_rq"}:
                    dataset = "global_hourly_rqds"
                    canonical = "uhslc_rq"
                elif prov == "uhslc_fast":
                    dataset = "global_hourly_fast"
                    canonical = "uhslc_fast"
                else:
                    dataset = str((vcfg.get("uhslc", {}) or {}).get("dataset", "global_hourly_fast"))
                    canonical = "uhslc"
                candidates = discover_uhslc(domain, cfg, dataset=dataset)
                candidates["provider"] = canonical
                candidates["dataset"] = dataset
            else:
                raise RuntimeError(f"Unknown water-level provider: {prov}")

            if candidates.empty:
                provider_errors[canonical] = "provider returned no station metadata"
                continue

            n_provider = int(len(candidates))
            candidates = _filter_stations_inside_domain(candidates, domain)
            log(
                f"{canonical}: {len(candidates)}/{n_provider} station candidate(s) "
                "inside/on SFINCS domain"
            )
            if candidates.empty:
                provider_errors[canonical] = "no stations inside/on SFINCS domain"
                continue

            # Preserve the existing candidate-limit and max-stations behavior;
            # only the spatial eligibility rule changed.
            candidates = candidates.head(candidate_limit)
            accepted: List[Dict[str, Any]] = []
            for _, row in candidates.iterrows():
                if len(accepted) >= max_stations:
                    break
                try:
                    obs = _event_data_available(row, start, end, cfg)
                    if len(obs) < int(vcfg.get("min_observations", 4)):
                        continue
                    coverage = _observation_coverage_fraction(row, obs, start, end, cfg)
                    max_missing = float(vcfg.get("max_missing_fraction", 0.20))
                    if coverage < 1.0 - max_missing:
                        raise RuntimeError(
                            f"event coverage {coverage:.1%} is below required {1.0-max_missing:.1%}"
                        )
                    sid = str(row["station_id"])
                    obs_path = outdir / f"obs_{canonical}_{_safe_id(sid)}.csv"
                    obs.to_csv(obs_path, index=False)
                    rec = row.to_dict()
                    rec["provider"] = canonical
                    rec["observation_path"] = str(obs_path)
                    rec["n_observations"] = int(len(obs))
                    rec["coverage_fraction"] = float(coverage)
                    accepted.append(rec)
                except Exception as exc:
                    cache_rows.append(
                        {
                            "provider": canonical,
                            "station_id": str(row.get("station_id")),
                            "status": "rejected",
                            "reason": str(exc),
                        }
                    )
            if len(accepted) >= min_stations:
                selected_rows = accepted
                log(f"selected {len(accepted)} {canonical} station(s) with event data")
                break
            provider_errors[canonical] = f"only {len(accepted)} event-valid station(s), need {min_stations}"
        except Exception as exc:
            provider_errors[prov] = str(exc)

    if not selected_rows:
        manifest = {
            "status": "no_stations",
            "provider_errors": provider_errors,
            "spatial_rule": "inside_or_on_sfincs_domain",
            "simulation_start": start.isoformat(),
            "simulation_end": end.isoformat(),
        }
        (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if bool(vcfg.get("required", False)):
            raise RuntimeError(f"No automatic water-level validation stations found: {provider_errors}")
        log(f"no automatic event-valid water-level stations found; continuing: {provider_errors}")
        return None

    gdf = gpd.GeoDataFrame(
        selected_rows,
        geometry=[Point(float(r["lon"]), float(r["lat"])) for r in selected_rows],
        crs=4326,
    )
    # Integer ID is required by HydroMT's established point-observation path;
    # provider_station_id preserves the true identifier for fetching/evaluation.
    gdf["ID"] = np.arange(1, len(gdf) + 1)
    gdf["provider_station_id"] = gdf["station_id"].astype(str)
    station_file = outdir / "stations.geojson"
    gdf.to_file(station_file, driver="GeoJSON")

    manifest = {
        "status": "ready",
        "stations_file": str(station_file),
        "simulation_start": start.isoformat(),
        "simulation_end": end.isoformat(),
        "spatial_rule": "inside_or_on_sfincs_domain",
        "provider_order": order,
        "provider_errors": provider_errors,
        "stations": [
            {
                k: (None if pd.isna(v) else v)
                for k, v in r.items()
                if k != "geometry"
            }
            for r in selected_rows
        ],
        "note": (
            "Only stations inside/on the SFINCS domain are eligible for water-level validation. "
            "Remote-sensing flood-extent products are intentionally not used. "
            "GESLA datum information is unspecified and UHSLC sea levels are relative "
            "to station zero; both therefore use datum-neutral event alignment unless "
            "an absolute datum match is explicitly known."
        ),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    if cache_rows:
        pd.DataFrame(cache_rows).to_csv(outdir / "rejected_candidates.csv", index=False)
    return station_file


def _load_cached_obs(run: Path, row: pd.Series, cfg: Dict[str, Any], station_index: int = 0) -> pd.DataFrame:
    path = row.get("observation_path")
    if isinstance(path, str) and Path(path).exists():
        df = pd.read_csv(path)
    else:
        provider = str(row.get("provider", ""))
        sid = str(row.get("provider_station_id", row.get("station_id", "")))
        start = parse_time(cfg["simulation"]["start"])
        end = parse_time(cfg["simulation"]["end"])
        if provider == "noaa_coops":
            df = fetch_noaa(sid, start, end, cfg)
        elif provider == "gesla":
            df = fetch_gesla(sid, start, end)
        elif provider.startswith("uhslc"):
            dataset = str(row.get("dataset", "global_hourly_rqds" if provider == "uhslc_rq" else "global_hourly_fast"))
            df = fetch_uhslc(sid, start, end, dataset=dataset)
        elif provider == "file":
            wlcfg = cfg.get("validation", {}).get("water_level", {}) or {}
            ts = run / "agent_validation" / "water_level" / "user_observations.csv"
            if not ts.exists():
                ts = Path(str(wlcfg.get("timeseries", ""))).expanduser()
            if not ts.exists():
                raise FileNotFoundError(f"User water-level timeseries not found: {ts}")
            raw = pd.read_csv(ts)
            time_col = str(wlcfg.get("time_column", "time"))
            if time_col not in raw.columns:
                time_col = str(raw.columns[0])
            configured = row.get("value_column")
            candidates = [
                str(configured) if configured is not None and not pd.isna(configured) and str(configured) != "" else None,
                sid,
                str(row.get("station_id", "")),
                str(row.get("ID", station_index + 1)),
                str(station_index + 1),
            ]
            value_col = next((c for c in candidates if c and c in raw.columns and c != time_col), None)
            if value_col is None:
                non_time = [c for c in raw.columns if c != time_col]
                if len(non_time) == 1 and station_index == 0:
                    value_col = non_time[0]
                else:
                    raise RuntimeError(
                        f"Cannot match user observation column for station {sid!r}. "
                        f"Available columns={list(raw.columns)}"
                    )
            df = raw[[time_col, value_col]].rename(
                columns={time_col: "time", value_col: "observed_zs_m"}
            )
        else:
            raise RuntimeError(f"No observation source for provider={provider}")
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    if "observed_zs_m" not in df.columns:
        raise RuntimeError(f"Observation source for provider={provider} lacks observed_zs_m")
    df["observed_zs_m"] = pd.to_numeric(df["observed_zs_m"], errors="coerce")
    return df.dropna(subset=["time", "observed_zs_m"]).sort_values("time")


def _metric_dict(model: np.ndarray, obs: np.ndarray) -> Dict[str, float]:
    d = np.asarray(model, float) - np.asarray(obs, float)
    n = int(np.isfinite(d).sum())
    if n == 0:
        return {"n": 0}
    mask = np.isfinite(model) & np.isfinite(obs)
    m = np.asarray(model, float)[mask]
    o = np.asarray(obs, float)[mask]
    d = m - o
    corr = float(np.corrcoef(m, o)[0, 1]) if len(m) > 1 and np.nanstd(m) > 0 and np.nanstd(o) > 0 else float("nan")
    return {
        "n": int(len(m)),
        "rmse_m": float(np.sqrt(np.mean(d * d))),
        "mae_m": float(np.mean(np.abs(d))),
        "bias_m": float(np.mean(d)),
        "correlation": corr,
    }


def compare_model_to_water_levels(
    cfg: Dict[str, Any],
    run: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    tolerance_minutes: Optional[float] = None,
) -> pd.DataFrame:
    """Compare SFINCS point_zs to cached NOAA/UHSLC/user observations."""
    run = Path(run or resolved_output_dir(cfg)).resolve()
    out_dir = Path(out_dir or (run / "agent_plots"))
    out_dir.mkdir(parents=True, exist_ok=True)

    wlcfg = cfg.get("validation", {}).get("water_level", {}) or {}
    if not bool(wlcfg.get("enabled", True)):
        return pd.DataFrame()

    stations = run / "agent_validation" / "water_level" / "stations.geojson"
    if not stations.exists():
        log("water-level evaluation skipped: no prepared global station file")
        return pd.DataFrame()

    his_path = run / "sfincs_his.nc"
    if not his_path.exists():
        raise FileNotFoundError(his_path)

    import xarray as xr
    his = xr.open_dataset(his_path)
    try:
        if "point_zs" not in his:
            raise RuntimeError(f"point_zs not found in {his_path}")
        gdf = gpd.read_file(stations).reset_index(drop=True)
        sim_time = pd.DatetimeIndex(pd.to_datetime(his["time"].values, utc=True))
        sim_zs = np.asarray(his["point_zs"].values, dtype=float)
        if sim_zs.ndim != 2:
            raise RuntimeError(f"Unexpected point_zs shape {sim_zs.shape}")
        if sim_zs.shape[0] != len(sim_time) and sim_zs.shape[1] == len(sim_time):
            sim_zs = sim_zs.T
        if sim_zs.shape[0] != len(sim_time):
            raise RuntimeError("Cannot align point_zs with history time axis")

        domain = _domain_geometry(cfg)
        eligible_station_indices = [
            k for k, row in gdf.iterrows()
            if k < sim_zs.shape[1] and _station_is_inside_domain(row, domain)
        ]
        n_outside = min(len(gdf), sim_zs.shape[1]) - len(eligible_station_indices)
        if n_outside > 0:
            log(
                f"water-level evaluation: excluding {n_outside} station(s) outside "
                "the SFINCS domain while preserving original point_zs column mapping"
            )
        if not eligible_station_indices:
            log("water-level evaluation skipped: no prepared stations inside/on SFINCS domain")
            return pd.DataFrame()

        start = parse_time(cfg["simulation"]["start"])
        align = wlcfg.get("datum_alignment_window_hours", [0, 12])
        h0, h1 = float(align[0]), float(align[1])
        align_start = start + pd.Timedelta(hours=h0)
        align_end = start + pd.Timedelta(hours=h1)
        exclude_align = bool(wlcfg.get("exclude_alignment_window_from_scores", True))
        score_start = align_end if exclude_align else start

        summary: List[Dict[str, Any]] = []
        series_for_plot = []

        for k in eligible_station_indices:
            row = gdf.iloc[k]
            obs = _load_cached_obs(run, row, cfg, station_index=k)
            sim = pd.DataFrame({"time": sim_time, "sfincs_zs_m": sim_zs[:, k]})
            sim = sim.sort_values("time")
            obs = obs.sort_values("time")

            provider = str(row.get("provider", "unknown"))
            if tolerance_minutes is None:
                if str(wlcfg.get("time_tolerance_minutes", "auto")).lower() == "auto":
                    tol = 31.0 if (provider == "gesla" or provider.startswith("uhslc")) else 4.0
                else:
                    tol = float(wlcfg.get("time_tolerance_minutes"))
            else:
                tol = float(tolerance_minutes)

            merged = pd.merge_asof(
                sim,
                obs[["time", "observed_zs_m"]],
                on="time",
                direction="nearest",
                tolerance=pd.Timedelta(minutes=tol),
            ).dropna(subset=["sfincs_zs_m", "observed_zs_m"])

            if merged.empty:
                continue

            requested_mode = str(wlcfg.get("datum_mode", "auto")).lower()
            station_datum = str(row.get("datum", ""))
            target_datum = target_vertical_datum(cfg)
            if requested_mode == "auto":
                absolute = provider in {"noaa_coops", "file"} and station_datum.strip().upper() == target_datum.strip().upper()
                datum_mode = "absolute" if absolute else "datum_neutral"
            else:
                datum_mode = requested_mode

            offset = 0.0
            align_n = 0
            if datum_mode in {"datum_neutral", "aligned", "offset"}:
                a = merged[(merged["time"] >= align_start) & (merged["time"] <= align_end)]
                align_n = int(len(a))
                if align_n < int(wlcfg.get("min_alignment_pairs", 3)):
                    raise RuntimeError(
                        f"Station {row.get('provider_station_id', row.get('station_id'))}: "
                        f"only {align_n} matched pairs in datum alignment window "
                        f"{align_start}..{align_end}"
                    )
                offset = float(np.nanmedian(a["observed_zs_m"] - a["sfincs_zs_m"]))
            elif datum_mode != "absolute":
                raise RuntimeError(f"Unsupported datum_mode={datum_mode!r}")

            merged["sfincs_aligned_m"] = merged["sfincs_zs_m"] + offset
            scored = merged[merged["time"] >= score_start].copy()
            if scored.empty:
                continue

            met = _metric_dict(scored["sfincs_aligned_m"].to_numpy(), scored["observed_zs_m"].to_numpy())

            # Datum-neutral anomaly metrics use each series' frozen alignment-window
            # median. These remain interpretable even when absolute datum is unknown.
            a = merged[(merged["time"] >= align_start) & (merged["time"] <= align_end)]
            if len(a):
                m0 = float(np.nanmedian(a["sfincs_aligned_m"]))
                o0 = float(np.nanmedian(a["observed_zs_m"]))
            else:
                m0 = float(scored["sfincs_aligned_m"].iloc[0])
                o0 = float(scored["observed_zs_m"].iloc[0])
            manom = scored["sfincs_aligned_m"].to_numpy() - m0
            oanom = scored["observed_zs_m"].to_numpy() - o0
            amet = _metric_dict(manom, oanom)

            im = int(np.nanargmax(scored["sfincs_aligned_m"].to_numpy()))
            io = int(np.nanargmax(scored["observed_zs_m"].to_numpy()))
            mt = pd.Timestamp(scored["time"].iloc[im])
            ot = pd.Timestamp(scored["time"].iloc[io])

            rec: Dict[str, Any] = {
                "provider": provider,
                "station_id": str(row.get("provider_station_id", row.get("station_id", row.get("ID", k + 1)))),
                "station_name": str(row.get("name", "")),
                "distance_km": float(row.get("distance_km", np.nan)),
                "station_datum": station_datum,
                "target_datum": target_datum,
                "datum_mode": datum_mode,
                "datum_offset_m": float(offset),
                "alignment_pairs": align_n,
                **met,
                "anomaly_rmse_m": amet.get("rmse_m", np.nan),
                "anomaly_mae_m": amet.get("mae_m", np.nan),
                "anomaly_bias_m": amet.get("bias_m", np.nan),
                "anomaly_correlation": amet.get("correlation", np.nan),
                "model_peak_m": float(scored["sfincs_aligned_m"].iloc[im]),
                "observed_peak_m": float(scored["observed_zs_m"].iloc[io]),
                "peak_error_m": float(scored["sfincs_aligned_m"].iloc[im] - scored["observed_zs_m"].iloc[io]),
                "peak_timing_error_hours": float((mt - ot).total_seconds() / 3600.0),
                "model_amplitude_m": float(scored["sfincs_aligned_m"].max() - scored["sfincs_aligned_m"].min()),
                "observed_amplitude_m": float(scored["observed_zs_m"].max() - scored["observed_zs_m"].min()),
            }
            rec["amplitude_error_m"] = rec["model_amplitude_m"] - rec["observed_amplitude_m"]
            summary.append(rec)
            series_for_plot.append((rec, merged, score_start))

        df = pd.DataFrame(summary)
        csv = out_dir / "sfincs_water_level_validation.csv"
        df.to_csv(csv, index=False)
        (out_dir / "sfincs_water_level_validation.json").write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8"
        )

        if series_for_plot:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            n = len(series_for_plot)
            ncols = 2 if n > 1 else 1
            nrows = int(math.ceil(n / ncols))
            fig, axes = plt.subplots(nrows, ncols, figsize=(7.2 * ncols, 3.6 * nrows), squeeze=False)
            for ax, (rec, merged, score_start) in zip(axes.ravel(), series_for_plot):
                t = pd.DatetimeIndex(merged["time"]).tz_convert(None)
                ax.plot(t, merged["sfincs_aligned_m"], label="SFINCS", linewidth=1.4)
                ax.plot(t, merged["observed_zs_m"], label=str(rec["provider"]).upper(), linewidth=1.1)
                ax.axvline(score_start.tz_convert(None), linestyle="--", linewidth=0.8, alpha=0.6)
                ax.set_title(
                    f"{rec['station_name']} | RMSE={rec.get('rmse_m', np.nan):.3f} m | "
                    f"peak Δ={rec['peak_error_m']:+.3f} m"
                )
                ax.set_ylabel("Water level [m]")
                ax.grid(alpha=0.2)
                ax.legend(fontsize=8)
            for ax in axes.ravel()[len(series_for_plot):]:
                ax.axis("off")
            fig.suptitle("SFINCS Global Water-Level Validation")
            fig.autofmt_xdate(rotation=25)
            fig.tight_layout(rect=[0, 0, 1, 0.97])
            fig.savefig(out_dir / "sfincs_water_level_validation.png", dpi=300, bbox_inches="tight")
            plt.close(fig)

        return df
    finally:
        his.close()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sfincs_agent_config.yml")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    args = ap.parse_args()
    cfg = read_config(Path(args.config))
    if args.prepare:
        p = prepare_water_level_validation(cfg)
        print(p or "NO_STATIONS")
    if args.evaluate:
        print(compare_model_to_water_levels(cfg).to_string(index=False))
    if not args.prepare and not args.evaluate:
        ap.error("choose --prepare and/or --evaluate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
