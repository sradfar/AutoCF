"""Vertical-datum metadata and auditable raster harmonization.

On Windows, AutoCF prefers its packaged Transformez helper, which runs in a
separate Python 3.12+ process and therefore cannot alter the validated Python
3.11 HydroMT/SFINCS runtime.  The older overlap-derived constant offset remains
a clearly marked approximate fallback so an inaccessible helper or uncovered
conversion never makes an otherwise valid desktop run impossible.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np


SOURCE_VERTICAL_DATUM: Dict[str, Dict[str, str]] = {
    "noaa_cudem_1_9_arcsec": {
        "datum": "NAVD88", "confidence": "authoritative",
        "note": "NOAA NCEI Ninth topobathymetric DEM (CONUS) is distributed in NAVD88.",
        "surface": "topobathy",
    },
    "usgs_3dep": {
        "datum": "NAVD88", "confidence": "authoritative",
        "note": "USGS 3DEP CONUS elevation products use NAVD88.",
        "surface": "land",
    },
    "usgs_3dep_1_3_arcsec": {
        "datum": "NAVD88", "confidence": "authoritative",
        "note": "USGS 3DEP CONUS elevation products use NAVD88.",
        "surface": "land",
    },
    "gedtm30": {
        "datum": "EGM2008", "confidence": "authoritative",
        "note": "GEDTM30 elevations reference the EGM2008 geoid (EPSG:3855).",
        "surface": "land",
    },
    "copernicus_glo_30": {
        "datum": "EGM2008", "confidence": "authoritative",
        "note": "Copernicus DEM GLO-30 elevations reference the EGM2008 geoid.",
        "surface": "land",
    },
    "gebco_2024_15_arcsec": {
        "datum": "MSL", "confidence": "assumed",
        "note": "GEBCO is treated as mean-sea-level referenced; its compiled sources may vary locally.",
        "surface": "topobathy",
    },
}


def normalize_vertical_datum(value: Any) -> str:
    text = (
        str(value or "").strip().upper().replace("_", "").replace("-", "").replace(":", "")
    )
    aliases = {
        "NAVD": "NAVD88", "NAVD88": "NAVD88",
        "EGM2008": "EGM2008", "EPSG3855": "EGM2008",
        "MSL": "MSL", "LMSL": "MSL", "MEANSEALEVEL": "MSL",
        "EGM96": "EGM96", "EPSG5773": "EGM96",
    }
    return aliases.get(text, str(value or "UNKNOWN").strip().upper() or "UNKNOWN")


def source_vertical_datum(section: Dict[str, Any]) -> str:
    source = str(section.get("source", "")).strip().lower()
    registered = SOURCE_VERTICAL_DATUM.get(source, {}).get("datum")
    if registered:
        return normalize_vertical_datum(registered)
    explicit = section.get("vertical_datum") or section.get("native_vertical_datum")
    return normalize_vertical_datum(explicit or "UNKNOWN")


def vertical_datums_equivalent(left: Any, right: Any) -> bool:
    return normalize_vertical_datum(left) == normalize_vertical_datum(right)


def datum_unit_label(value: Any) -> str:
    return f"m+{normalize_vertical_datum(value)}"


def source_datum_metadata(section: Dict[str, Any]) -> Dict[str, str]:
    source = str(section.get("source", "")).strip().lower()
    registered = SOURCE_VERTICAL_DATUM.get(source, {})
    return {
        "source": source,
        "native_vertical_datum": source_vertical_datum(section),
        "confidence": str(registered.get("confidence", "user-declared" if section.get("vertical_datum") else "unknown")),
        "note": str(registered.get("note", "User-provided sources require an explicit native vertical datum.")),
        "surface": str(section.get("surface") or registered.get("surface", "user-declared")),
    }


TRANSFORMEZ_DATUMS = {
    "NAVD88": "5703",
    "EGM2008": "3855",
    "EGM96": "5773",
    "MSL": "msl",
}


def transformez_executable() -> Optional[str]:
    """Return the isolated Transformez helper or a compatible CLI."""
    override = str(os.environ.get("AUTOCF_TRANSFORMEZ_EXECUTABLE", "")).strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which("transformez")


def transformez_status() -> Dict[str, Any]:
    executable = transformez_executable()
    status: Dict[str, Any] = {
        "available": bool(executable),
        "executable": executable,
        "role": "isolated rigorous grid-based vertical transformation",
    }
    if not executable:
        status["note"] = (
            "The isolated Transformez helper is not accessible from this execution "
            "backend; automatic mode will use the local overlap alignment fallback "
            "when source and target datums differ."
        )
        return status
    if str(os.environ.get("AUTOCF_TRANSFORMEZ_EXECUTABLE", "")).strip():
        # A PyInstaller one-file helper validates itself during the actual
        # transformation.  Starting its large geospatial payload only to ask
        # for a version would add roughly 30 seconds to every terrain run.
        status["version"] = "packaged-isolated-helper"
        status["note"] = "Packaged helper found; conversion errors still trigger the audited fallback."
        return status
    try:
        result = subprocess.run(
            [executable, "--version"], check=False, capture_output=True, text=True, timeout=15,
        )
        version = (result.stdout or result.stderr).strip()
        if version:
            status["version"] = version.splitlines()[-1]
        status["available"] = result.returncode == 0
        if result.returncode != 0:
            status["note"] = f"Version check failed with exit code {result.returncode}."
    except Exception as exc:
        status["available"] = False
        status["note"] = f"Transformez could not be started: {exc}"
    return status


def transform_gapfill_with_transformez(
    gapfill_path: Path,
    output_path: Path,
    *,
    source_datum: str,
    target_datum: str,
    cache_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Apply the isolated Transformez raster engine and validate its output."""
    import rasterio

    executable = transformez_executable()
    if not executable:
        raise RuntimeError("The isolated Transformez helper is not accessible.")
    source = normalize_vertical_datum(source_datum)
    target = normalize_vertical_datum(target_datum)
    try:
        datum_in = TRANSFORMEZ_DATUMS[source]
        datum_out = TRANSFORMEZ_DATUMS[target]
    except KeyError as exc:
        raise RuntimeError(
            f"Transformez mapping is not configured for {source} -> {target}."
        ) from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    env = os.environ.copy()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        env["XDG_CACHE_HOME"] = str(cache_dir)
        # The Windows helper receives WSL-mounted paths and translates them to
        # native Windows paths.  A dedicated variable avoids coupling its
        # Python 3.12+ cache to the Python 3.11 runtime's XDG cache.
        env["AUTOCF_TRANSFORMEZ_CACHE"] = str(cache_dir)
    command = [
        executable, "raster", str(gapfill_path), "-I", datum_in, "-O", datum_out,
        "--in-units", "m", "--out-units", "m", "--decay-pixels", "0",
        "--out", str(output_path),
    ]
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=3600, env=env,
    )
    if result.returncode != 0 or not output_path.is_file():
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()
        if len(detail) > 800:
            detail = detail[-800:]
        raise RuntimeError(
            f"Transformez failed for {source} -> {target} (exit {result.returncode}): {detail}"
        )
    with rasterio.open(output_path) as transformed:
        if transformed.count < 1 or transformed.width < 1 or transformed.height < 1:
            raise RuntimeError("Transformez produced an empty raster.")
    with rasterio.open(output_path, "r+") as transformed:
        transformed.update_tags(
            AUTOCF_VERTICAL_DATUM=target,
            AUTOCF_SOURCE_VERTICAL_DATUM=source,
            AUTOCF_DATUM_METHOD="transformez-grid",
        )
    return {
        "method": "transformez-grid",
        "engine": "Transformez",
        "source_datum": source,
        "target_datum": target,
        "output": str(output_path),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "qualification": "Grid-based vertical transformation; no constant-offset approximation.",
        "command_summary": f"transformez raster -I {datum_in} -O {datum_out} --decay-pixels 0",
    }


def harmonize_gapfill_to_target(
    primary_path: Path,
    gapfill_path: Path,
    output_path: Path,
    *,
    target_datum: str,
    primary_datum: str,
    gapfill_datum: str,
    engine: str = "auto",
    configured_offset_m: Optional[float] = None,
    cache_dir: Optional[Path] = None,
    min_samples: int = 64,
) -> Dict[str, Any]:
    """Use rigorous conversion when available, with an auditable fallback."""
    requested = str(engine or "auto").strip().lower().replace("-", "_")
    if requested not in {"auto", "transformez", "local_alignment"}:
        raise RuntimeError(
            "topobathy.datum_engine must be auto, transformez, or local_alignment."
        )
    source = normalize_vertical_datum(gapfill_datum)
    target = normalize_vertical_datum(target_datum)
    if vertical_datums_equivalent(source, target):
        report = align_gapfill_to_primary(
            primary_path, gapfill_path, output_path,
            target_datum=target, primary_datum=primary_datum, gapfill_datum=source,
            configured_offset_m=configured_offset_m, min_samples=min_samples,
        )
        report["engine_requested"] = requested
        return report

    if configured_offset_m is None and requested in {"auto", "transformez"}:
        try:
            report = transform_gapfill_with_transformez(
                gapfill_path, output_path, source_datum=source, target_datum=target,
                cache_dir=cache_dir,
            )
            report["engine_requested"] = requested
            return report
        except Exception as exc:
            if requested == "transformez":
                raise
            rigorous_error = str(exc)
    else:
        rigorous_error = (
            "A user-configured constant offset takes precedence."
            if configured_offset_m is not None else "Local alignment was explicitly selected."
        )

    report = align_gapfill_to_primary(
        primary_path, gapfill_path, output_path,
        target_datum=target, primary_datum=primary_datum, gapfill_datum=source,
        configured_offset_m=configured_offset_m, min_samples=min_samples,
    )
    report.update({
        "engine_requested": requested,
        "fallback_from": "Transformez" if requested == "auto" else None,
        "fallback_reason": rigorous_error,
        "quality": "approximate",
    })
    return report


def source_temporal_metadata(section: Dict[str, Any], event_start: Any = None) -> Dict[str, Any]:
    """Report whether a user-declared source date predates the modeled event."""
    raw = str(section.get("acquisition_date") or "").strip()
    result: Dict[str, Any] = {
        "acquisition_date": raw or None,
        "compatibility": "unknown",
        "note": "No acquisition date was supplied; product editions are not treated as acquisition dates.",
    }
    if not raw or not event_start:
        return result
    try:
        acquired = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        event = datetime.fromisoformat(str(event_start).replace("Z", "+00:00"))
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=timezone.utc)
        if event.tzinfo is None:
            event = event.replace(tzinfo=timezone.utc)
    except ValueError:
        result["compatibility"] = "invalid-date"
        result["note"] = "Use ISO format YYYY-MM-DD for acquisition_date."
        return result
    if acquired <= event:
        result["compatibility"] = "pre-event"
        result["note"] = "The declared acquisition date is on or before the modeled event."
    else:
        result["compatibility"] = "post-event"
        result["note"] = "The declared acquisition date is after the modeled event; this represents modern terrain."
    return result


def write_source_provenance_raster(
    primary_path: Path, gapfill_path: Optional[Path], output_path: Path,
    additional_sources=None,
) -> Dict[str, Any]:
    """Write a model-resolution map of which source supplies each valid cell."""
    import rasterio
    from rasterio.warp import Resampling, reproject

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(primary_path) as primary:
        primary_valid = ~primary.read(1, masked=True).mask
        if np.isscalar(primary_valid):
            primary_valid = np.full((primary.height, primary.width), bool(primary_valid))
        gap_valid = np.zeros((primary.height, primary.width), dtype="uint8")
        if gapfill_path is not None:
            with rasterio.open(gapfill_path) as gap:
                source_valid = (~gap.read(1, masked=True).mask).astype("uint8")
                if np.isscalar(source_valid):
                    source_valid = np.full((gap.height, gap.width), int(source_valid), dtype="uint8")
                reproject(
                    source=source_valid, destination=gap_valid,
                    src_transform=gap.transform, src_crs=gap.crs,
                    dst_transform=primary.transform, dst_crs=primary.crs,
                    src_nodata=0, dst_nodata=0, resampling=Resampling.nearest,
                )
        provenance = np.zeros((primary.height, primary.width), dtype="uint32")
        provenance[gap_valid > 0] = 2
        provenance[np.asarray(primary_valid, dtype=bool)] = 1
        codes = {"0": "no-data", "1": "Source 1", "2": "Source 2"}
        for code, extra_path in additional_sources or []:
            with rasterio.open(extra_path) as extra:
                source_valid = (~np.ma.getmaskarray(extra.read(1, masked=True))).astype("uint8")
                valid = np.zeros(provenance.shape, dtype="uint8")
                reproject(source=source_valid, destination=valid,
                          src_transform=extra.transform, src_crs=extra.crs,
                          dst_transform=primary.transform, dst_crs=primary.crs,
                          src_nodata=0, dst_nodata=0, resampling=Resampling.nearest)
                provenance[(provenance == 0) & (valid > 0)] = code
            codes[str(code)] = f"Source {code}"
        profile = primary.profile.copy()
        profile.update(dtype="uint32", nodata=0, count=1, compress="deflate", predictor=1)
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(provenance, 1)
            dst.update_tags(
                AUTOCF_PROVENANCE_CODES=",".join(f"{code}={label}" for code, label in codes.items()),
                AUTOCF_SOURCE_PRIORITY="Lower source number wins; later sources fill only NoData gaps",
            )
    total = int(provenance.size)
    counts = {code: int(np.count_nonzero(provenance == int(code))) for code in codes}
    return {
        "raster": str(output_path),
        "codes": codes,
        "cell_count": total,
        "counts": counts,
        "fractions": {key: value / total if total else 0.0 for key, value in counts.items()},
        "resolution": "model grid",
    }


def align_gapfill_to_primary(
    primary_path: Path,
    gapfill_path: Path,
    output_path: Path,
    *,
    target_datum: str,
    primary_datum: str,
    gapfill_datum: str,
    configured_offset_m: Optional[float] = None,
    min_samples: int = 64,
) -> Dict[str, Any]:
    """Align a clipped gap filler by a robust overlap-derived constant offset.

    This is deliberately described as local alignment, not as a rigorous VDatum
    transformation. It is fast enough for desktop use because calculations take
    place on the coarser gap-filler grid.
    """
    import rasterio
    from rasterio.warp import Resampling, reproject

    primary_datum = normalize_vertical_datum(primary_datum)
    gapfill_datum = normalize_vertical_datum(gapfill_datum)
    target_datum = normalize_vertical_datum(target_datum)
    if primary_datum != target_datum:
        raise RuntimeError(
            f"Primary topobathy is {primary_datum}, but the model target is {target_datum}. "
            "Choose the primary source's native datum or provide a pre-converted primary raster."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if gapfill_datum == target_datum:
        if gapfill_path.resolve() != output_path.resolve():
            shutil.copy2(gapfill_path, output_path)
        return {
            "method": "none-required", "offset_m": 0.0, "sample_count": 0,
            "primary_datum": primary_datum, "source_datum": gapfill_datum,
            "target_datum": target_datum, "output": str(output_path),
        }

    if "UNKNOWN" in {primary_datum, gapfill_datum, target_datum}:
        raise RuntimeError(
            "A native vertical datum is missing. Select it for each user raster before terrain preparation."
        )

    with rasterio.open(gapfill_path) as gap, rasterio.open(primary_path) as primary:
        gap_raw = gap.read(1, masked=True).astype("float64")
        gap_values = np.asarray(gap_raw.filled(np.nan)) * float(gap.scales[0]) + float(gap.offsets[0])
        primary_on_gap = np.full((gap.height, gap.width), np.nan, dtype="float64")
        reproject(
            source=rasterio.band(primary, 1), destination=primary_on_gap,
            src_transform=primary.transform, src_crs=primary.crs,
            src_nodata=primary.nodata, dst_transform=gap.transform, dst_crs=gap.crs,
            dst_nodata=np.nan, resampling=Resampling.average,
        )
        primary_on_gap = primary_on_gap * float(primary.scales[0]) + float(primary.offsets[0])
        valid = np.isfinite(gap_values) & np.isfinite(primary_on_gap)
        differences = primary_on_gap[valid] - gap_values[valid]
        if configured_offset_m is None:
            if differences.size < int(min_samples):
                raise RuntimeError(
                    f"Cannot align {gapfill_datum} gap-fill elevations to {target_datum}: only "
                    f"{differences.size} overlapping valid cells were found (minimum {min_samples}). "
                    "Provide topobathy.gapfill.vertical_datum_offset_m or a pre-converted raster."
                )
            q05, q95 = np.nanpercentile(differences, [5.0, 95.0])
            central = differences[(differences >= q05) & (differences <= q95)]
            offset = float(np.nanmedian(central))
            method = "local-overlap-median"
        else:
            q05 = q95 = math.nan
            central = differences
            offset = float(configured_offset_m)
            method = "configured-constant-offset"
        aligned = gap_values + offset
        profile = gap.profile.copy()
        profile.update(dtype="float32", nodata=-9999.0, compress="deflate", predictor=3)
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(np.where(np.isfinite(aligned), aligned, -9999.0).astype("float32"), 1)
            dst.scales = (1.0,)
            dst.offsets = (0.0,)
            dst.update_tags(
                AUTOCF_VERTICAL_DATUM=target_datum,
                AUTOCF_SOURCE_VERTICAL_DATUM=gapfill_datum,
                AUTOCF_DATUM_METHOD=method,
                AUTOCF_VERTICAL_OFFSET_M=f"{offset:.6f}",
            )
    median = float(np.nanmedian(differences)) if differences.size else None
    mad = float(np.nanmedian(np.abs(differences - median))) if differences.size else None
    p05 = float(q05) if math.isfinite(float(q05)) else None
    p95 = float(q95) if math.isfinite(float(q95)) else None
    return {
        "method": method, "offset_m": offset, "sample_count": int(differences.size),
        "difference_median_m": median, "difference_mad_m": mad,
        "difference_p05_m": p05, "difference_p95_m": p95,
        "primary_datum": primary_datum, "source_datum": gapfill_datum,
        "target_datum": target_datum, "output": str(output_path),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "qualification": "Local empirical alignment; not a NOAA VDatum transformation.",
    }


def write_datum_provenance(path: Path, payload: Dict[str, Any]) -> Path:
    def json_safe(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
            return None
        return value

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, allow_nan=False), encoding="utf-8")
    return path
