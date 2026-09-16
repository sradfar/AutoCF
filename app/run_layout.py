"""Versioned filesystem layout for AutoCF run artifacts.

Layout version 2 separates machine-facing model/data files from curated result
products.  Configurations without an explicit layout version retain the legacy
paths so existing runs remain readable and resumable.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


CURRENT_LAYOUT_VERSION = 2


def _kind_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".svg"}:
        return "figures"
    if suffix == ".csv":
        return "tables"
    if suffix in {".json", ".yml", ".yaml", ".txt"}:
        return "reports"
    if suffix in {".tif", ".tiff", ".vrt"}:
        return "rasters"
    if suffix in {".geojson", ".gpkg", ".shp"}:
        return "vectors"
    if suffix in {".nc", ".netcdf"}:
        return "netcdf"
    if suffix in {".parquet", ".feather"}:
        return "tables"
    if suffix in {".log"}:
        return "logs"
    return "files"


@dataclass(frozen=True)
class RunLayout:
    root: Path
    version: int

    @property
    def organized(self) -> bool:
        return self.version >= CURRENT_LAYOUT_VERSION

    @property
    def model(self) -> Path:
        return self.root / "data" / "model" / "main" if self.organized else self.root

    @property
    def metadata(self) -> Path:
        return self.root / "metadata" if self.organized else self.root

    @property
    def state(self) -> Path:
        return self.metadata / "run_state.json" if self.organized else self.root / "agent_state.json"

    @property
    def work(self) -> Path:
        return self.root / "work" / "hydromt" if self.organized else self.root / "agent_work"

    @property
    def validation(self) -> Path:
        return (
            self.root / "data" / "validation" / "water_level"
            if self.organized else self.root / "agent_validation" / "water_level"
        )

    @property
    def previews(self) -> Path:
        return self.root / "data" / "previews" if self.organized else self.root / "agent_data"

    @property
    def forcing_previews(self) -> Path:
        return (
            self.previews / "forcing"
            if self.organized else self.root / "agent_data" / "forcing_previews"
        )

    @property
    def analysis(self) -> Path:
        return self.root / "data" / "attribution" if self.organized else self.root / "agent_analysis"

    @property
    def scenarios(self) -> Path:
        return self.analysis / "scenarios"

    @property
    def exposure_source(self) -> Path:
        return (
            self.root / "data" / "exposure" / "source"
            if self.organized else self.root / "agent_impact" / "source"
        )

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def data_path(self, section: str, name: str) -> Path:
        candidate = Path(name)
        if not self.organized:
            if section == "geospatial":
                return self.root / "agent_gis" / candidate
            if section == "validation":
                return self.validation / candidate
            return self.root / "agent_data" / candidate
        return self.root / "data" / section / _kind_for(candidate) / candidate

    def result_path(self, section: str, name: str) -> Path:
        candidate = Path(name)
        if not self.organized:
            legacy = {
                "evaluation": self.root / "agent_plots",
                "attribution": self.root / "agent_analysis" / "results",
                "exposure": self.root / "agent_impact",
                "simulation": self.root / "agent_reports",
            }
            return legacy[section] / candidate
        return self.root / "results" / section / _kind_for(candidate) / candidate

    def result_section(self, section: str) -> Path:
        if not self.organized:
            return self.result_path(section, "placeholder").parent
        return self.root / "results" / section

    def ensure_base_directories(self) -> None:
        directories = [
            self.root,
            self.model,
            self.metadata,
            self.work,
            self.validation,
            self.forcing_previews,
        ]
        if self.organized:
            directories.extend([
                self.root / "data",
                self.root / "results",
                self.logs / "build",
                self.logs / "simulation",
                self.logs / "analysis",
            ])
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)


def layout_version(cfg: Mapping[str, Any]) -> int:
    project = cfg.get("project", {}) if hasattr(cfg, "get") else {}
    try:
        return int(project.get("layout_version", 1))
    except (TypeError, ValueError):
        return 1


def run_layout(cfg: Mapping[str, Any]) -> RunLayout:
    project = cfg.get("project", {}) if hasattr(cfg, "get") else {}
    root = Path(str(project["output_dir"])).expanduser().resolve()
    return RunLayout(root=root, version=layout_version(cfg))


def refresh_artifact_catalog(layout: RunLayout) -> Path | None:
    """Rebuild the run's result catalog without indexing working data."""
    if not layout.organized:
        return None
    results = layout.root / "results"
    entries = []
    if results.is_dir():
        for path in sorted(results.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(layout.root)
            parts = relative.parts
            section = parts[1] if len(parts) > 1 else "other"
            kind = parts[2] if len(parts) > 2 else _kind_for(path)
            entries.append({
                "id": f"{section}:{path.stem}",
                "title": path.stem.replace("_", " ").title(),
                "section": section,
                "kind": kind,
                "format": path.suffix.lower().lstrip("."),
                "path": relative.as_posix(),
                "display": path.suffix.lower() in {
                    ".png", ".jpg", ".jpeg", ".bmp", ".csv", ".json",
                },
            })
    layout.metadata.mkdir(parents=True, exist_ok=True)
    target = layout.metadata / "artifact_catalog.json"
    target.write_text(json.dumps({
        "layout_version": layout.version,
        "products": entries,
    }, indent=2), encoding="utf-8")
    return target
