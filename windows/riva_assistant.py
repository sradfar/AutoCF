"""Riva: a local, evidence-grounded research assistant for AutoCF.

Riva uses deterministic retrieval and result extraction first.  When the
optional Qwen3.5-0.8B runtime is installed, llama.cpp turns that evidence into
a concise conversational answer.  No project data leaves the computer.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import html
import json
import math
import os
import re
import secrets
import shutil
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QDockWidget, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QUrl

from riva_knowledge import (
    CASE_STUDIES, FAQS, PAGE_ANSWERS, REFERENCES, RULE_DATABASE, RULE_DATABASE_ERROR,
)


MODEL_NAME = "Qwen3.5-0.8B-Q4_0.gguf"
MODEL_URL = (
    "https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF/resolve/main/"
    "Qwen3.5-0.8B-Q4_0.gguf?download=true"
)
MODEL_MIN_BYTES = 500_000_000
MODEL_SHA256 = "57d1997790d1744fba5b40a7317df71ea5e2acee28c47e78f0cce39c0703f8cf"
LLAMA_RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=12"
SFINCS_MANUAL_URL = "https://sfincs.readthedocs.io/en/latest/"
SFINCS_PARAMETERS_URL = "https://sfincs.readthedocs.io/en/latest/parameters.html"
HYDROMT_SFINCS_URL = "https://deltares.github.io/hydromt_sfincs/latest/"
RIVA_PORT = 8942


@dataclass(frozen=True)
class Evidence:
    title: str
    source: str
    text: str


AUTOCF_GUIDE = [
    Evidence(
        "AutoCF workflow",
        "AutoCF built-in guide",
        "AutoCF follows a guided sequence: Project, Event & Time, Study Area, Terrain, "
        "Roughness & Infiltration, Forcing, SFINCS Model, Build & Run, Evaluation, "
        "Attribution & Analysis, and Results. A saved YAML represents the model "
        "configuration; the output directory contains the built and simulated model.",
    ),
    Evidence(
        "Attribution workspace preparation",
        "AutoCF built-in guide",
        "Prepare analysis workspace creates or refreshes the seven coastal, precipitation, "
        "and river counterfactual input folders. It does not run SFINCS. A completed "
        "scenario containing sfincs_map.nc is preserved unless Recreate existing prepared "
        "counterfactual folders is explicitly selected.",
    ),
    Evidence(
        "Counterfactual simulations",
        "AutoCF built-in guide",
        "Run missing counterfactual simulations checks all seven prepared scenarios and "
        "runs only those without sfincs_map.nc. The parent CPR simulation is reused as the "
        "eighth, full scenario. Completed scenarios do not need to be rerun.",
    ),
    Evidence(
        "Single-run analysis",
        "AutoCF built-in guide",
        "The Single-run tool analyzes exposure for the completed parent flood map only. "
        "It can report buildings, roads, and population but does not compute Shapley driver "
        "attribution or factorial interactions.",
    ),
    Evidence(
        "Absolute and attributed impacts",
        "AutoCF built-in guide",
        "Headline impacts use absolute depth in the full CPR scenario. Shapley attribution "
        "allocates the incremental change f(FULL)-f(NONE) among coastal, precipitation, and "
        "river drivers. These quantities must remain separately labelled.",
    ),
    Evidence(
        "Flood duration",
        "AutoCF built-in guide",
        "Flood-duration products integrate time above each selected depth threshold from "
        "the saved time-varying SFINCS depths. Duration attribution uses the existing eight "
        "factorial scenarios and requires no additional SFINCS simulations.",
    ),
    Evidence(
        "Driver Impact Shift (DIS)",
        "AutoCF built-in guide",
        "DIS is the percentage-point difference between a driver's share of an impact "
        "metric and its share of flooded area. Positive DIS means the driver contributes "
        "more to the impact than its flooded-area share; negative DIS means less.",
    ),
    Evidence(
        "CDC SVI burden",
        "AutoCF built-in guide",
        "The SVI burden summary intersects affected population or exposure with CDC/ATSDR "
        "Social Vulnerability Index census-tract rankings. It is an equity summary, not a "
        "causal vulnerability model.",
    ),
    Evidence(
        "Evaluation metrics",
        "AutoCF built-in guide",
        "Evaluation compares modeled and observed water levels. RMSE and MAE measure error "
        "magnitude, bias shows systematic over- or underprediction, correlation measures "
        "co-variation, peak error compares peak levels, and peak timing error compares when "
        "the peaks occur. Datum and observation coverage must be checked before interpretation.",
    ),
    Evidence(
        "Riva scope and safeguards",
        "AutoCF built-in guide",
        "Riva explains AutoCF, retrieves SFINCS and HydroMT-SFINCS documentation, and "
        "summarizes existing evaluation and attribution files. Riva does not edit YAML, "
        "launch simulations, or replace engineering review. Numerical statements are built "
        "from saved AutoCF outputs before the language model is called.",
    ),
]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]{2,}", text.lower())


def _normalized_question(text: str) -> str:
    return " ".join(_tokens(text))


def match_autocf_faq(question: str, page: str = "") -> Evidence | None:
    """Return a curated answer only when an AutoCF intent is a strong match."""
    normalized = _normalized_question(question)
    query_tokens = set(_tokens(question))
    best: tuple[float, dict[str, Any]] | None = None
    for faq in FAQS:
        aliases = tuple(_normalized_question(alias) for alias in faq["aliases"])
        if normalized in aliases:
            score = 100.0
        elif any(alias and alias in normalized for alias in aliases):
            score = 80.0
        else:
            keywords = set(faq["keywords"])
            overlap = len(query_tokens & keywords)
            score = float(overlap * 4)
            if keywords and keywords <= query_tokens:
                score += 8.0
            if page and page in faq.get("pages", ()):
                score += 1.5
        if (
            best is None or score > best[0]
            or (score == best[0] and faq.get("source") and not best[1].get("source"))
        ):
            best = (score, faq)
    if best is None or best[0] < 8.0:
        return None
    faq = best[1]
    return Evidence(
        str(faq["title"]), str(faq.get("source", "AutoCF built-in guide")),
        str(faq["answer"]),
    )


LOCAL_EVIDENCE_TERMS = {
    "best", "buffer", "calibrate", "calibration", "case", "evidence",
    "example", "local", "manning", "near", "nearby", "paper", "roughness",
    "study", "subgrid", "validation",
}


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + (
        math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return radius_km * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def nearby_case_studies(question: str, bounds: Any, limit: int = 2) -> list[Evidence]:
    """Return relevant indexed papers close to the active study-area centre."""
    query_terms = set(_tokens(question))
    if not query_terms.intersection(LOCAL_EVIDENCE_TERMS):
        return []
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return []
    try:
        west, south, east, north = map(float, bounds)
    except (TypeError, ValueError):
        return []
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        return []
    lon, lat = (west + east) / 2.0, (south + north) / 2.0
    candidates: list[tuple[int, float, dict[str, Any]]] = []
    for case in CASE_STUDIES:
        center = case.get("center", ())
        if not isinstance(center, list) or len(center) != 2:
            continue
        distance = _haversine_km(lon, lat, float(center[0]), float(center[1]))
        if distance > float(case.get("radius_km", 0.0)):
            continue
        topic_overlap = len(query_terms.intersection(_tokens(" ".join(case.get("topics", ())))))
        candidates.append((-topic_overlap, distance, case))
    candidates.sort(key=lambda item: (item[0], item[1]))
    output: list[Evidence] = []
    for _negative_overlap, distance, case in candidates[:limit]:
        reference_ids = list(map(str, case.get("references", ())))
        cited = [REFERENCES[item] for item in reference_ids if item in REFERENCES]
        if not cited:
            continue
        citations = "; ".join(
            f"{ref['citation']} ({ref['url']})" for ref in cited
        )
        text = (
            f"Indexed case about {distance:,.0f} km from the study-area centre: "
            f"{case['summary']} Evidence: {citations}"
        )
        output.append(Evidence(
            f"Nearby case: {case['name']}", str(cited[0]["url"]), text,
        ))
    return output


SEARCH_STOPWORDS = {
    "about", "and", "are", "can", "default", "does", "explain", "for",
    "from", "how", "its", "parameter", "please", "the", "this", "what",
    "when", "where", "which", "with",
}


def _clean_rst(text: str) -> str:
    text = re.sub(r"\.\.\s+[a-zA-Z0-9_-]+::[^\n]*", " ", text)
    text = re.sub(r":([a-zA-Z0-9_-]+):", r"\1: ", text)
    text = re.sub(r"`([^`<>]+)\s*<[^>]+>`_", r"\1", text)
    text = re.sub(r"[*|]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _chunks(text: str, limit: int = 1500) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 35]
    output: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 <= limit:
            current = f"{current}\n\n{paragraph}".strip()
        else:
            if current:
                output.append(current)
            if len(paragraph) <= limit:
                current = paragraph
            else:
                output.extend(paragraph[i:i + limit] for i in range(0, len(paragraph), limit))
                current = ""
    if current:
        output.append(current)
    return output


class RivaKnowledge:
    """Small in-memory lexical index over bundled manuals and AutoCF guidance."""

    def __init__(self, package_root: Path) -> None:
        self.root = package_root
        self.entries: list[Evidence] = list(AUTOCF_GUIDE)
        self._load_sfincs_manual()
        self._load_hydromt_api()

    def _load_sfincs_manual(self) -> None:
        docs = self.root / "windows" / "cpu-source" / "SFINCS" / "docs"
        if not docs.is_dir():
            return
        preferred = (
            "parameters.rst", "input.rst", "input_forcing.rst",
            "input_structures.rst", "output.rst", "overview.rst",
        )
        for name in preferred:
            path = docs / name
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            if name == "parameters.rst":
                lines = raw.splitlines()
                for index, line in enumerate(lines):
                    candidate = line.strip().split(" - ", 1)[0].strip()
                    if not re.fullmatch(r"[a-z][a-z0-9_]*", candidate):
                        continue
                    if index + 1 >= len(lines) or not lines[index + 1].lstrip().startswith(":description:"):
                        continue
                    block = [line]
                    for following in lines[index + 1:index + 9]:
                        stripped = following.lstrip()
                        if block and stripped and not stripped.startswith(":"):
                            break
                        block.append(following)
                    self.entries.append(Evidence(
                        f"SFINCS parameter {candidate}", SFINCS_PARAMETERS_URL,
                        _clean_rst("\n".join(block)),
                    ))
            clean = _clean_rst(raw)
            for index, chunk in enumerate(_chunks(clean), 1):
                url = SFINCS_PARAMETERS_URL if name == "parameters.rst" else SFINCS_MANUAL_URL
                self.entries.append(Evidence(f"SFINCS {path.stem} ({index})", url, chunk))

    def _load_hydromt_api(self) -> None:
        package = self.root / "vendor" / "hydromt_sfincs"
        if not package.is_dir():
            return
        for path in package.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                doc = ast.get_docstring(node, clean=True)
                if not doc or (not node.name.startswith("setup") and len(doc) < 120):
                    continue
                signature = node.name
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names = [arg.arg for arg in node.args.args if arg.arg not in {"self", "cls"}]
                    signature += "(" + ", ".join(names[:12]) + (", …" if len(names) > 12 else "") + ")"
                self.entries.append(Evidence(
                    f"HydroMT-SFINCS {signature}", HYDROMT_SFINCS_URL,
                    _clean_rst(doc)[:3500],
                ))

    def search(self, query: str, limit: int = 6) -> list[Evidence]:
        terms = [term for term in _tokens(query) if term not in SEARCH_STOPWORDS]
        if not terms:
            return []
        phrase = query.strip().lower()
        scored: list[tuple[float, Evidence]] = []
        hydromt_requested = (
            any(
                term in phrase
                for term in (
                    "hydromt", "setup_", "function", "method", "api", "merge",
                    "dataarray", "data array",
                )
            )
            or "_" in phrase
        )
        for entry in self.entries:
            if entry.title.startswith("HydroMT-SFINCS") and not hydromt_requested:
                continue
            title = entry.title.lower()
            body_tokens = _tokens(entry.text)
            counts: dict[str, int] = {}
            for token in body_tokens:
                counts[token] = counts.get(token, 0) + 1
            score = 0.0
            for term in set(terms):
                score += min(counts.get(term, 0), 5)
                if term in title:
                    score += 5.0
                if term in {"sfincs", "hydromt", "autocf"}:
                    continue
                if counts.get(term, 0) and "sfincs" in title:
                    score += 3.0
            if phrase and phrase in entry.text.lower():
                score += 12.0
            # Exact sfincs.inp parameter names deserve a strong match.
            for candidate in re.findall(r"\b[a-z][a-z0-9_]{2,}\b", phrase):
                if re.search(rf"(?<![a-z0-9_]){re.escape(candidate)}(?![a-z0-9_])", title):
                    score += 8.0
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _, entry in scored[:limit]]


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    except OSError:
        return []


def _result_file(run_dir: Path, section: str, kind: str, name: str) -> Path:
    organized = run_dir / "results" / section / kind / name
    legacy_roots = {
        "evaluation": run_dir / "agent_plots",
        "attribution": run_dir / "agent_analysis" / "results",
        "exposure": run_dir / "agent_impact",
        "simulation": run_dir / "agent_reports",
    }
    legacy = legacy_roots[section] / name
    return organized if organized.is_file() or not legacy.is_file() else legacy


def _words_limited(text: str, limit: int = 120) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]).rstrip(" ,;:") + "…"


def _evidence_fallback(item: Evidence | None) -> str:
    if item is None:
        return "I could not find a reliable answer in the available AutoCF documentation."
    compact = re.sub(r"\s+", " ", item.text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", compact)
    excerpt = " ".join(sentences[:3])
    if item.title.startswith("HydroMT-SFINCS"):
        excerpt = f"The closest HydroMT-SFINCS entry is {item.title.removeprefix('HydroMT-SFINCS ')}. " + excerpt
    return _words_limited(excerpt, 90)


def evaluation_summary(run_dir: Path) -> tuple[str, list[Evidence]]:
    path = _result_file(
        run_dir, "evaluation", "reports", "sfincs_water_level_validation.json"
    )
    records = _read_json(path)
    if not isinstance(records, list) or not records:
        return (
            "No station-level evaluation JSON was found for the active model. Run Evaluation "
            "with observations enabled, then ask Riva again.", []
        )
    valid = [row for row in records if isinstance(row, dict)]
    rmse = [_safe_number(row.get("rmse_m")) for row in valid]
    mae = [_safe_number(row.get("mae_m")) for row in valid]
    bias = [_safe_number(row.get("bias_m")) for row in valid]
    corr = [_safe_number(row.get("correlation")) for row in valid]
    rmse_values = [v for v in rmse if v is not None]
    mae_values = [v for v in mae if v is not None]
    bias_values = [v for v in bias if v is not None]
    corr_values = [v for v in corr if v is not None]
    lines = [f"Evaluation file: {path.name}", f"Stations evaluated: {len(valid)}"]
    if rmse_values:
        lines.append(f"Mean station RMSE: {statistics.fmean(rmse_values):.3f} m")
        candidates = [(value, row) for value, row in zip(rmse, valid) if value is not None]
        best = min(candidates, key=lambda item: item[0])
        worst = max(candidates, key=lambda item: item[0])
        lines.append(f"Lowest RMSE: {best[1].get('station_name', best[1].get('station_id', 'station'))} ({best[0]:.3f} m)")
        lines.append(f"Highest RMSE: {worst[1].get('station_name', worst[1].get('station_id', 'station'))} ({worst[0]:.3f} m)")
    if mae_values:
        lines.append(f"Mean station MAE: {statistics.fmean(mae_values):.3f} m")
    if bias_values:
        lines.append(f"Mean station bias: {statistics.fmean(bias_values):+.3f} m")
    if corr_values:
        lines.append(f"Mean station correlation: {statistics.fmean(corr_values):.3f}")
    lines.append("Interpret these metrics together with datum consistency, observation coverage, and station plots.")
    evidence = Evidence("Active model evaluation results", str(path), "\n".join(lines))
    return "\n".join(lines), [evidence]


def evaluation_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]] | None:
    """Answer natural station-comparison questions from saved evaluation values."""
    lower = question.lower()
    comparison = {
        "which", "worse", "worst", "best", "highest", "lowest", "largest",
        "smallest", "mean", "average", "overpredict", "underpredict", "perform",
        "result", "summary",
    }
    topics = (
        "gauge", "station", "hwm", "high water", "rmse", "mae", "bias", "correlation", "peak",
        "evaluation", "validation",
    )
    if not any(term in lower for term in topics):
        return None
    if not any(term in lower for term in comparison):
        return None
    if "hwm" in lower or "high water" in lower:
        hwm_path = _result_file(
            run_dir, "evaluation", "reports", "sfincs_hwm_validation_summary.json"
        )
        summary = _read_json(hwm_path)
        if not isinstance(summary, dict):
            return "No high-water-mark validation summary was found.", []
        rmse = _safe_number(summary.get("rmse_m"))
        mae = _safe_number(summary.get("mae_m"))
        bias = _safe_number(summary.get("bias_m"))
        values = [f"{summary.get('n_metric_eligible', 0)} eligible marks"]
        if rmse is not None:
            values.append(f"RMSE {rmse:.3f} m")
        if mae is not None:
            values.append(f"MAE {mae:.3f} m")
        if bias is not None:
            values.append(f"bias {bias:+.3f} m")
        answer = "High-water-mark evaluation: " + "; ".join(values) + "."
        return answer, [Evidence("High-water-mark evaluation results", str(hwm_path), answer)]
    path = _result_file(
        run_dir, "evaluation", "reports", "sfincs_water_level_validation.json"
    )
    records = _read_json(path)
    rows = [row for row in records if isinstance(row, dict)] if isinstance(records, list) else []
    if not rows:
        answer = (
            "No station-level evaluation results were found for the active model. Run "
            "Evaluation with observations enabled, then ask again."
        )
        return answer, []

    def station(row: dict[str, Any]) -> str:
        return str(row.get("station_name") or row.get("station_id") or "Unnamed station")

    metric = "rmse_m"
    label = "RMSE"
    unit = "m"
    if "mae" in lower:
        metric, label = "mae_m", "MAE"
    elif "correlation" in lower:
        metric, label, unit = "correlation", "correlation", ""
    elif "timing" in lower:
        metric, label, unit = "peak_timing_error_hours", "peak timing error", "h"
    elif "peak" in lower:
        metric, label = "peak_error_m", "peak error"
    elif "bias" in lower or "overpredict" in lower or "underpredict" in lower:
        metric, label = "bias_m", "bias"

    values = [(value, row) for row in rows if (value := _safe_number(row.get(metric))) is not None]
    if not values:
        answer = f"The evaluation file contains no valid {label} values."
        return answer, [Evidence("Active model evaluation results", str(path), answer)]

    if "mean" in lower or "average" in lower:
        value = statistics.fmean(item[0] for item in values)
        answer = f"Mean station {label}: {value:+.3f}{(' ' + unit) if unit else ''} across {len(values)} stations."
        if metric == "bias_m":
            answer += " Positive means average overprediction; negative means underprediction."
    else:
        wants_best = "best" in lower or "lowest" in lower or "smallest" in lower
        if metric == "correlation":
            chosen = max(values, key=lambda item: item[0]) if wants_best else min(values, key=lambda item: item[0])
        elif metric in {"bias_m", "peak_error_m", "peak_timing_error_hours"}:
            if "overpredict" in lower:
                chosen = max(values, key=lambda item: item[0])
            elif "underpredict" in lower:
                chosen = min(values, key=lambda item: item[0])
            else:
                chosen = min(values, key=lambda item: abs(item[0])) if wants_best else max(values, key=lambda item: abs(item[0]))
        else:
            chosen = min(values, key=lambda item: item[0]) if wants_best else max(values, key=lambda item: item[0])
        value, row = chosen
        direction = "best" if wants_best else "worst"
        answer = f"By {label}, {station(row)} is the {direction} station: {value:+.3f}{(' ' + unit) if unit else ''}."
        rmse = _safe_number(row.get("rmse_m"))
        bias = _safe_number(row.get("bias_m"))
        pairs = row.get("n")
        details = []
        if metric != "rmse_m" and rmse is not None:
            details.append(f"RMSE {rmse:.3f} m")
        if metric != "bias_m" and bias is not None:
            details.append(f"bias {bias:+.3f} m")
        if pairs not in (None, ""):
            details.append(f"{pairs} matched pairs")
        if details:
            answer += " " + "; ".join(details) + "."
    answer += " Check datum mode and the station plot before changing the model."
    answer = _words_limited(answer)
    return answer, [Evidence("Active model evaluation results", str(path), answer)]


def attribution_summary(run_dir: Path) -> tuple[str, list[Evidence]]:
    absolute_path = _result_file(
        run_dir, "attribution", "reports", "absolute_impact_report.json"
    )
    summary_path = _result_file(
        run_dir, "attribution", "reports", "attribution_summary.json"
    )
    absolute = _read_json(absolute_path)
    summary = _read_json(summary_path)
    if not isinstance(absolute, dict) and not isinstance(summary, dict):
        return (
            "No completed attribution report was found for the active model. Generate the "
            "Attribution & Analysis report, then ask Riva again.", []
        )
    lines: list[str] = []
    evidence: list[Evidence] = []
    if isinstance(absolute, dict):
        rows = absolute.get("rows", [])
        primary = next((row for row in rows if isinstance(row, dict) and row.get("threshold_role") == "primary"), None)
        if primary is None:
            primary = next((row for row in rows if isinstance(row, dict)), None)
        lines.append(
            f"Headline scenario: {absolute.get('headline_scenario', 'FULL')}; "
            f"baseline: {absolute.get('baseline_scenario', 'NONE')}."
        )
        if isinstance(primary, dict):
            threshold = _safe_number(primary.get("depth_threshold_m"))
            if threshold is not None:
                lines.append(f"At the primary {threshold:g} m threshold:")
            metrics = (
                ("Full flooded area", "full_flooded_area_km2", "km²", ".2f"),
                ("Full buildings exposed", "full_buildings_exposed", "", ".0f"),
                ("Full population exposed", "full_population_exposed", "people", ".0f"),
                ("Population affected", "full_population_affected_pct", "%", ".2f"),
            )
            for label, key, unit, fmt in metrics:
                value = _safe_number(primary.get(key))
                if value is not None:
                    spacer = " " if unit and unit != "%" else ""
                    lines.append(f"- {label}: {format(value, fmt)}{spacer}{unit}")
        evidence.append(Evidence("Absolute impact report", str(absolute_path), "\n".join(lines)))
    if isinstance(summary, dict):
        closure = summary.get("shapley_closure", {})
        if isinstance(closure, dict):
            status = "passes" if closure.get("passes_1e-5_m") else "does not pass"
            maximum = _safe_number(closure.get("max_abs_m"))
            suffix = f"; maximum absolute residual {maximum:.3g} m" if maximum is not None else ""
            lines.append(f"Shapley numerical closure {status} the 1e-5 m check{suffix}.")
        svi = summary.get("cdc_svi_burden", {})
        if isinstance(svi, dict):
            lines.append(f"CDC SVI burden: {svi.get('status', 'reported')}.")
        duration = summary.get("flood_duration", {})
        if isinstance(duration, dict) and duration.get("enabled"):
            lines.append("Flood-duration and duration-attribution products are included.")
        dis_path = _result_file(
            run_dir, "attribution", "tables", "driver_impact_shift.csv"
        )
        if dis_path.is_file():
            try:
                with dis_path.open("r", encoding="utf-8-sig", newline="") as stream:
                    dis_rows = list(csv.DictReader(stream))
                lines.append(f"Driver Impact Shift table: {len(dis_rows)} driver/metric/threshold rows.")
            except OSError:
                pass
        evidence.append(Evidence("Attribution summary", str(summary_path), json.dumps(summary, indent=2)[:7000]))
    lines.append("Headline totals are absolute FULL-scenario impacts; Shapley values allocate FULL minus NONE.")
    return "\n".join(lines), evidence


def _requested_threshold(question: str) -> float | None:
    match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:m|meter|metre)\b", question.lower())
    return float(match.group(1)) if match else None


def _threshold_rows(rows: list[dict[str, str]], question: str) -> tuple[list[dict[str, str]], float | None]:
    requested = _requested_threshold(question)
    available = sorted({
        value for row in rows
        if (value := _safe_number(row.get("depth_threshold_m"))) is not None
    })
    if not available:
        return rows, None
    if requested is None:
        primary = [
            value for row in rows if row.get("threshold_role") == "primary"
            and (value := _safe_number(row.get("depth_threshold_m"))) is not None
        ]
        chosen = min(primary) if primary else min(available)
    else:
        chosen = min(available, key=lambda value: abs(value - requested))
    selected = [
        row for row in rows
        if (value := _safe_number(row.get("depth_threshold_m"))) is not None
        and math.isclose(value, chosen, rel_tol=0.0, abs_tol=1e-9)
    ]
    return selected, chosen


def _attribution_threshold_rows(
    rows: list[dict[str, str]], question: str, run_dir: Path
) -> tuple[list[dict[str, str]], float | None]:
    """Use the requested threshold, or the report's lowest primary threshold."""
    if _requested_threshold(question) is not None or any(
        row.get("threshold_role") == "primary" for row in rows
    ):
        return _threshold_rows(rows, question)
    summary_path = _result_file(
        run_dir, "attribution", "reports", "attribution_summary.json"
    )
    summary = _read_json(summary_path)
    primary = summary.get("primary_depth_thresholds_m", []) if isinstance(summary, dict) else []
    primary_values = [
        value for item in primary if (value := _safe_number(item)) is not None
    ]
    available = sorted({
        value for row in rows
        if (value := _safe_number(row.get("depth_threshold_m"))) is not None
    })
    matches = [
        value for value in available
        if any(math.isclose(value, item, rel_tol=0.0, abs_tol=1e-9) for item in primary_values)
    ]
    if not matches:
        return _threshold_rows(rows, question)
    chosen = min(matches)
    selected = [
        row for row in rows
        if (value := _safe_number(row.get("depth_threshold_m"))) is not None
        and math.isclose(value, chosen, rel_tol=0.0, abs_tol=1e-9)
    ]
    return selected, chosen


def _driver_label(value: Any) -> str:
    return str(value or "unknown").replace("_", " ").title()


def _shapley_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]]:
    path = _result_file(
        run_dir, "attribution", "tables", "shapley_hazard_exposure.csv"
    )
    rows = _read_csv(path)
    selected, threshold = _threshold_rows(rows, question)
    if not selected:
        answer = "No Shapley hazard/exposure table was found for the active model."
        return answer, []
    lower = question.lower()
    requested_metrics = []
    if "flood" in lower or "hazard" in lower or "area" in lower:
        requested_metrics.append(("flooded_area_km2", "flooded area", "km2"))
    if "building" in lower:
        requested_metrics.append(("buildings_exposed", "building exposure", "buildings"))
    if "population" in lower or "people" in lower:
        requested_metrics.append(("population_exposed", "population exposure", "people"))
    if "impact" in lower and not any(
        metric in {"buildings_exposed", "population_exposed"}
        for metric, _, _ in requested_metrics
    ):
        requested_metrics.extend([
            ("buildings_exposed", "building exposure", "buildings"),
            ("population_exposed", "population exposure", "people"),
        ])
    if not requested_metrics:
        requested_metrics = [
            ("flooded_area_km2", "flooded area", "km2"),
            ("buildings_exposed", "building exposure", "buildings"),
            ("population_exposed", "population exposure", "people"),
        ]
    findings = []
    for metric, label, unit in requested_metrics:
        candidates = []
        for row in selected:
            if row.get("metric") != metric:
                continue
            value = _safe_number(row.get("shapley_value"))
            share = _safe_number(row.get("fraction_of_full_minus_none"))
            if value is not None:
                candidates.append((value, share, row))
        if not candidates:
            continue
        value, share, row = max(candidates, key=lambda item: item[0])
        share_text = f" ({100.0 * share:.1f}% of FULL minus NONE)" if share is not None else ""
        findings.append(
            f"{_driver_label(row.get('driver'))} leads {label}: {value:,.2f} {unit}{share_text}"
        )
    if not findings:
        answer = "The attribution table has no valid values for the requested metric."
    else:
        prefix = f"At the {threshold:g} m threshold, " if threshold is not None else ""
        answer = prefix + "; ".join(findings) + "."
        if len(findings) > 1:
            answer += " The leading driver can differ between hazard and consequence metrics."
    answer = _words_limited(answer)
    return answer, [Evidence("Shapley hazard and exposure results", str(path), answer)]


def _dis_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]]:
    path = _result_file(run_dir, "attribution", "tables", "driver_impact_shift.csv")
    rows = [row for row in _read_csv(path) if row.get("status") == "ok"]
    selected, threshold = _attribution_threshold_rows(rows, question, run_dir)
    if not selected:
        answer = "No valid Driver Impact Shift rows were found for the active model."
        return answer, []
    lower = question.lower()
    if "population" in lower or "people" in lower:
        selected = [row for row in selected if "population" in row.get("impact_metric", "")]
    elif "building" in lower:
        selected = [row for row in selected if "building" in row.get("impact_metric", "")]
    values = []
    for row in selected:
        value = _safe_number(row.get("dis_percentage_points"))
        if value is not None:
            values.append((value, row))
    if not values:
        answer = "DIS is unavailable for the requested metric or threshold."
        return answer, [Evidence("Driver Impact Shift results", str(path), answer)]
    high = max(values, key=lambda item: item[0])
    low = min(values, key=lambda item: item[0])
    prefix = f"At {threshold:g} m, " if threshold is not None else ""
    answer = (
        prefix + f"{_driver_label(high[1].get('driver'))} has the highest DIS for "
        f"{str(high[1].get('impact_metric', 'the requested impact')).replace('_', ' ')} "
        f"({high[0]:+.1f} percentage points), so it is most consequence-heavy relative "
        f"to its flood footprint. {_driver_label(low[1].get('driver'))} has the lowest "
        f"DIS ({low[0]:+.1f} points). DIS means impact share minus flooded-area share."
    )
    return _words_limited(answer), [Evidence("Driver Impact Shift results", str(path), answer)]


def _svi_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]]:
    path = _result_file(
        run_dir, "attribution", "reports", "cdc_svi_burden_summary.json"
    )
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return "No CDC SVI burden report was found for the active model.", []
    status = str(payload.get("status", "unknown"))
    rows = [row for row in payload.get("rows", []) if isinstance(row, dict)]
    selected, threshold = _threshold_rows(rows, question)
    values = []
    for row in selected:
        value = _safe_number(row.get("burden_concentration_ratio"))
        if value is not None:
            values.append((value, row))
    if status != "complete" or not values:
        detail = str(payload.get("message", "No valid quartile rows are available."))
        answer = f"CDC SVI burden status: {status}. {detail}"
        return _words_limited(answer), [Evidence("CDC SVI burden results", str(path), answer)]
    value, row = max(values, key=lambda item: item[0])
    coverage = _safe_number(row.get("svi_population_coverage_pct"))
    prefix = f"At {threshold:g} m, " if threshold is not None else ""
    answer = (
        prefix + f"{row.get('svi_quartile', 'the leading quartile')} has the highest burden "
        f"concentration ratio ({value:.2f}). Values above 1 indicate disproportionate burden."
    )
    if coverage is not None:
        answer += f" SVI coverage is {coverage:.1f}% of affected population."
    answer += " This is an equity overlay, not a causal vulnerability result."
    return _words_limited(answer), [Evidence("CDC SVI burden results", str(path), answer)]


def _duration_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]]:
    path = _result_file(
        run_dir, "attribution", "tables", "shapley_duration_metrics.csv"
    )
    rows = _read_csv(path)
    selected, threshold = _attribution_threshold_rows(rows, question, run_dir)
    lower = question.lower()
    metric = "building_hours" if "building" in lower else "flooded_area_hours_km2_h"
    values = []
    for row in selected:
        if row.get("metric") != metric:
            continue
        value = _safe_number(row.get("shapley_value"))
        share = _safe_number(row.get("fraction_of_full_minus_none"))
        if value is not None:
            values.append((value, share, row))
    if not values:
        return "No duration-attribution values were found for the requested metric.", []
    value, share, row = max(values, key=lambda item: item[0])
    metric_label = "building-hours" if metric == "building_hours" else "flooded area-hours"
    prefix = f"At {threshold:g} m, " if threshold is not None else ""
    answer = prefix + f"{_driver_label(row.get('driver'))} leads {metric_label}: {value:,.2f}"
    if share is not None:
        answer += f" ({100.0 * share:.1f}% of FULL minus NONE)"
    answer += ". Duration is computed from saved time-varying depths; no extra scenarios are run."
    return _words_limited(answer), [Evidence("Duration attribution results", str(path), answer)]


def _factorial_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]]:
    lower = question.lower()
    interaction = "interaction" in lower or "synergy" in lower or "antagon" in lower
    name = (
        "factorial_interactions_hazard_exposure.csv" if interaction
        else "compound_amplification_hazard_exposure.csv"
    )
    path = _result_file(run_dir, "attribution", "tables", name)
    selected, threshold = _threshold_rows(_read_csv(path), question)
    metric = "flooded_area_km2"
    if "building" in lower:
        metric = "buildings_exposed"
    elif "population" in lower or "people" in lower:
        metric = "population_exposed"
    selected = [row for row in selected if row.get("metric") == metric]
    key = "value" if interaction else "compound_amplification"
    values = [(value, row) for row in selected if (value := _safe_number(row.get(key))) is not None]
    if not values:
        return "No matching factorial result was found for the active model.", []
    value, row = max(values, key=lambda item: abs(item[0]))
    prefix = f"At {threshold:g} m" if threshold is not None else "For this result"
    if interaction:
        sign = "synergistic" if value > 0 else "antagonistic" if value < 0 else "neutral"
        answer = f"{prefix}, the largest interaction is {row.get('term')}: {value:,.2f}. It is {sign}."
    else:
        relative = _safe_number(row.get("relative_to_full"))
        answer = f"{prefix}, compound amplification is {value:,.2f} for {metric.replace('_', ' ')}"
        if relative is not None:
            answer += f" ({100.0 * relative:.1f}% of FULL)"
        answer += "."
    return answer, [Evidence("Factorial attribution results", str(path), answer)]


def attribution_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]] | None:
    """Interpret saved attribution tables with deterministic calculations."""
    lower = question.lower()
    topics = (
        "attribution", "driver", "shapley", "hazard", "impact", "dis", "dsi",
        "svi", "vulnerability", "duration", "interaction", "amplification",
        "coastal", "precipitation", "river",
    )
    requests = (
        "which", "most", "highest", "lowest", "largest", "dominant", "leader",
        "result", "summary", "active", "interpret", "total", "pass", "quality",
        "how many", "absolute", "overall",
    )
    if not any(term in lower for term in topics):
        return None
    if not any(term in lower for term in requests):
        return None

    if "dis" in lower or "dsi" in lower or "impact shift" in lower:
        return _dis_answer(question, run_dir)
    if "svi" in lower or "vulnerability" in lower or "equity" in lower:
        return _svi_answer(question, run_dir)
    if "duration" in lower or "persistent" in lower:
        return _duration_answer(question, run_dir)
    if "interaction" in lower or "synergy" in lower or "antagon" in lower:
        return _factorial_answer(question, run_dir)
    if "amplification" in lower:
        return _factorial_answer(question, run_dir)
    if "total" in lower or "how many" in lower or "absolute" in lower or "overall" in lower:
        return attribution_summary(run_dir)
    if "summary" in lower or "quality" in lower or "closure" in lower or "pass" in lower:
        return attribution_summary(run_dir)
    return _shapley_answer(question, run_dir)


def exposure_answer(question: str, run_dir: Path) -> tuple[str, list[Evidence]] | None:
    lower = question.lower()
    if not any(term in lower for term in ("single run", "single-run", "road", "parent flood map")):
        return None
    path = _result_file(run_dir, "exposure", "reports", "impact_report.json")
    payload = _read_json(path)
    rows = payload.get("summary", []) if isinstance(payload, dict) else []
    rows = [row for row in rows if isinstance(row, dict)]
    selected, threshold = _threshold_rows(rows, question)
    if not selected:
        return "No single-run exposure report was found for the active model.", []
    row = selected[0]
    values = []
    for key, label, unit in (
        ("flood_area_km2", "flood area", "km2"),
        ("impacted_buildings", "buildings", ""),
        ("flooded_road_km", "flooded road", "km"),
        ("impacted_population", "population", "people"),
    ):
        value = _safe_number(row.get(key))
        if value is not None:
            values.append(f"{label} {value:,.2f}{(' ' + unit) if unit else ''}")
    prefix = f"At {threshold:g} m, " if threshold is not None else ""
    answer = prefix + "; ".join(values) + "."
    errors = payload.get("provider_errors", {}) if isinstance(payload, dict) else {}
    if errors:
        answer += " Some providers reported warnings; review impact_report.json."
    return _words_limited(answer), [Evidence("Single-run exposure results", str(path), answer)]


class RivaBackend:
    """Own the optional llama.cpp process and its local Qwen model."""

    def __init__(self, package_root: Path) -> None:
        self.root = package_root
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        self.user_dir = local / "AutoCF" / "Riva"
        self.process: subprocess.Popen[bytes] | None = None
        self.api_key = secrets.token_urlsafe(24)

    def _candidate_roots(self) -> list[Path]:
        return [self.root / "Software" / "Riva", self.user_dir]

    def model_path(self) -> Path | None:
        for folder in self._candidate_roots():
            path = folder / "models" / MODEL_NAME
            if path.is_file() and path.stat().st_size >= MODEL_MIN_BYTES:
                return path
        return None

    def server_path(self) -> Path | None:
        for folder in self._candidate_roots():
            for path in (folder / "llama" / "llama-server.exe", folder / "llama-server.exe"):
                if path.is_file():
                    return path
        system = shutil.which("llama-server") or shutil.which("llama-server.exe")
        return Path(system) if system else None

    def is_installed(self) -> bool:
        return self.model_path() is not None and self.server_path() is not None

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{RIVA_PORT}/health", timeout=1.5) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _ensure_server(self) -> None:
        if self._healthy():
            if self.process is not None and self.process.poll() is None:
                return
            raise RuntimeError("Riva's localhost port is already in use by another process.")
        model = self.model_path()
        server = self.server_path()
        if model is None or server is None:
            raise RuntimeError("Riva local AI is not installed.")
        self.stop()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        command = [
            str(server), "-m", str(model), "-c", "4096", "-t", str(threads),
            "-np", "1", "--host", "127.0.0.1", "--port", str(RIVA_PORT),
            "--api-key", self.api_key, "--no-webui", "--log-disable",
        ]
        self.process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=creationflags,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("The local llama.cpp engine stopped while loading Qwen.")
            if self._healthy():
                return
            time.sleep(0.25)
        self.stop()
        raise RuntimeError("Qwen did not finish loading within 45 seconds.")

    def ask(self, question: str, evidence: list[Evidence], exact_results: str = "") -> str:
        self._ensure_server()
        sources = []
        for index, item in enumerate(evidence, 1):
            source_line = (
                f"\nExternal source: {item.source}"
                if item.source.startswith(("http://", "https://")) else ""
            )
            sources.append(f"[S{index}] {item.title}{source_line}\n{item.text}")
        if exact_results:
            sources.insert(0, "EXACT AUTOCF RESULT SUMMARY (do not alter values):\n" + exact_results)
        system = (
            "You are Riva, the local research assistant inside AutoCF. The EVIDENCE supplied "
            "by AutoCF is authoritative. Never challenge, correct, reinterpret, or add facts "
            "to it. Answer only by clearly restating and explaining the supplied evidence. "
            "Never invent a parameter range, unit, default, result, or file state. Cite every "
            "factual paragraph with [S1], [S2], etc. If a requested fact is absent, say it is "
            "not in the evidence. Keep the answer concise and professional. Distinguish "
            "SFINCS, HydroMT-SFINCS, and AutoCF. Use plain text, not Markdown."
        )
        payload = json.dumps({
            "model": "riva-qwen3.5-0.8b",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"/no_think\nQuestion: {question}\n\nEVIDENCE:\n" + "\n\n".join(sources)},
            ],
            "temperature": 0.0,
            "max_tokens": 220,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{RIVA_PORT}/v1/chat/completions", data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }, method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("The local model returned an empty response.")
        answer = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        answer = answer.replace("**", "")
        if evidence and not re.search(r"\[S\d+\]", answer):
            answer += " [S1]"
        return answer


class RivaAskWorker(QThread):
    completed = Signal(str, object)
    failed = Signal(str, object)

    def __init__(self, backend: RivaBackend, question: str, evidence: list[Evidence], exact: str) -> None:
        super().__init__()
        self.backend, self.question = backend, question
        self.evidence, self.exact = evidence, exact

    def run(self) -> None:
        try:
            self.completed.emit(self.backend.ask(self.question, self.evidence, self.exact), self.evidence)
        except Exception as exc:  # surfaced in the Riva panel
            self.failed.emit(str(exc), self.evidence)


class RivaInstallWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, destination: Path) -> None:
        super().__init__()
        self.destination = destination

    def _download(self, url: str, destination: Path, low: int, high: int, label: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(1, 6):
            if self.isInterruptionRequested():
                raise RuntimeError("Riva installation was cancelled.")
            existing = temporary.stat().st_size if temporary.is_file() else 0
            headers = {"User-Agent": "AutoCF-Riva/1.0"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    if existing and response.status != 206:
                        existing = 0
                        temporary.unlink(missing_ok=True)
                    total_header = response.headers.get("Content-Range") or response.headers.get("Content-Length")
                    total_match = re.search(r"/(\d+)$", total_header or "")
                    total = int(total_match.group(1)) if total_match else existing + int(response.headers.get("Content-Length", "0") or 0)
                    mode = "ab" if existing else "wb"
                    received = existing
                    with temporary.open(mode) as stream:
                        while True:
                            if self.isInterruptionRequested():
                                raise RuntimeError("Riva installation was cancelled.")
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            stream.write(block)
                            received += len(block)
                            fraction = received / total if total else 0
                            self.progress.emit(int(low + (high - low) * fraction), label)
                last_error = None
                break
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                self.progress.emit(
                    max(low, self.progress_value(temporary, low, high)),
                    f"{label} connection interrupted; resuming ({attempt}/5)…",
                )
                time.sleep(min(2 * attempt, 8))
        if last_error is not None:
            raise last_error
        temporary.replace(destination)

    @staticmethod
    def progress_value(temporary: Path, low: int, high: int) -> int:
        # The precise total is learned from the next HTTP response. This keeps
        # the progress bar stable between retry attempts without guessing 100%.
        return low if not temporary.is_file() else min(high - 1, low + 1)

    def _engine_url(self) -> str:
        request = urllib.request.Request(LLAMA_RELEASES_URL, headers={"User-Agent": "AutoCF-Riva/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            releases = json.loads(response.read().decode("utf-8"))
        for release in releases:
            if release.get("draft"):
                continue
            for asset in release.get("assets", []):
                name = str(asset.get("name", ""))
                if re.fullmatch(r"llama-b\d+-bin-win-cpu-x64\.zip", name):
                    return str(asset["browser_download_url"])
        raise RuntimeError("No Windows x64 CPU llama.cpp package was found in recent releases.")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def run(self) -> None:
        try:
            model = self.destination / "models" / MODEL_NAME
            engine_dir = self.destination / "llama"
            server = engine_dir / "llama-server.exe"
            if not model.is_file() or model.stat().st_size < MODEL_MIN_BYTES:
                self.progress.emit(1, "Downloading Qwen3.5-0.8B (563 MB)…")
                self._download(MODEL_URL, model, 1, 88, "Downloading Qwen3.5-0.8B…")
            if model.stat().st_size < MODEL_MIN_BYTES:
                raise RuntimeError("The Qwen download is incomplete.")
            self.progress.emit(88, "Verifying the Qwen model…")
            if self._sha256(model) != MODEL_SHA256:
                model.unlink(missing_ok=True)
                raise RuntimeError("The Qwen model checksum did not match the approved file; it was removed.")
            if not server.is_file():
                self.progress.emit(89, "Downloading the lightweight llama.cpp engine…")
                engine_url = self._engine_url()
                archive = self.destination / "llama-win-cpu-x64.zip"
                self._download(engine_url, archive, 89, 97, "Downloading llama.cpp…")
                engine_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive) as package:
                    for member in package.infolist():
                        name = Path(member.filename).name
                        if not name or Path(name).suffix.lower() not in {".exe", ".dll"}:
                            continue
                        with package.open(member) as source, (engine_dir / name).open("wb") as target:
                            shutil.copyfileobj(source, target)
                archive.unlink(missing_ok=True)
            if not server.is_file():
                raise RuntimeError("llama-server.exe was not found after extraction.")
            notice = self.destination / "NOTICE.txt"
            notice.write_text(
                "Riva local AI components\n\n"
                "Model: Qwen3.5-0.8B Q4_0 GGUF (Apache-2.0)\n"
                "https://huggingface.co/ggml-org/Qwen3.5-0.8B-GGUF\n\n"
                f"Model SHA-256: {MODEL_SHA256}\n\n"
                "Inference: llama.cpp\nhttps://github.com/ggml-org/llama.cpp\n",
                encoding="utf-8",
            )
            self.progress.emit(100, "Riva local AI is ready.")
            self.completed.emit(str(self.destination))
        except Exception as exc:
            self.failed.emit(str(exc))


class RivaAssistantDock(QDockWidget):
    """Dockable BSOD-blue assistant UI used by the AutoCF desktop window."""

    activityChanged = Signal(str)

    def __init__(
        self,
        parent: QWidget,
        package_root: Path,
        run_dir_provider: Callable[[], Path],
        context_provider: Callable[[], dict[str, Any]],
        busy_provider: Callable[[], bool],
    ) -> None:
        super().__init__("Riva - Research Assistant", parent)
        self.setObjectName("RivaDock")
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.root = package_root
        self.run_dir_provider = run_dir_provider
        self.context_provider = context_provider
        self.busy_provider = busy_provider
        self.knowledge = RivaKnowledge(package_root)
        self.backend = RivaBackend(package_root)
        self.ask_worker: RivaAskWorker | None = None
        self.install_worker: RivaInstallWorker | None = None
        self._question_count = 0
        self._build()
        self._refresh_status()
        self._welcome()

    def _build(self) -> None:
        panel = QWidget()
        panel.setObjectName("rivaPanel")
        panel.setMinimumWidth(350)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)

        header = QWidget(); header.setObjectName("rivaHeader")
        header_layout = QHBoxLayout(header); header_layout.setContentsMargins(8, 8, 8, 8)
        self.icon = QLabel(); self.icon.setObjectName("rivaIcon"); self.icon.setFixedSize(86, 86)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_path = self.root / "logo" / "Riva.png"
        if icon_path.is_file():
            pixmap = QPixmap(str(icon_path))
            self.icon.setPixmap(pixmap.scaled(82, 82, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        title_box = QVBoxLayout()
        title = QLabel("Riva"); title.setObjectName("rivaTitle")
        subtitle = QLabel("AutoCF Research Assistant"); subtitle.setObjectName("rivaSubtitle")
        self.status = QLabel("Checking local AI…"); self.status.setObjectName("rivaStatus")
        title_box.addWidget(title); title_box.addWidget(subtitle); title_box.addWidget(self.status)
        header_layout.addWidget(self.icon); header_layout.addLayout(title_box, 1)
        layout.addWidget(header)

        quick_prompts = (
            ("How to start", "What is the first step to run a new AutoCF model?"),
            ("Explain page", "Explain the purpose and workflow of the current AutoCF page."),
            ("Worst gauge", "Which gauge has the worst RMSE in the active model?"),
            ("Leading driver", "Which driver is most hazardous and impactful?"),
            ("Explain DIS", "Interpret Driver Impact Shift for the active model."),
            ("Results", "Summarize the active model attribution results."),
        )
        for start in (0, 3):
            quick = QHBoxLayout()
            for label, prompt in quick_prompts[start:start + 3]:
                button = QPushButton(label); button.setObjectName("rivaQuick")
                button.clicked.connect(lambda _checked=False, text=prompt: self.submit(text))
                quick.addWidget(button)
            layout.addLayout(quick)

        self.transcript = QTextBrowser()
        self.transcript.setObjectName("rivaTranscript")
        self.transcript.setOpenExternalLinks(True)
        self.transcript.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.transcript, 1)

        input_row = QHBoxLayout()
        self.input = QLineEdit(); self.input.setObjectName("rivaInput")
        self.input.setPlaceholderText("Ask about AutoCF or a SFINCS parameter…")
        self.input.returnPressed.connect(self.submit)
        self.send = QPushButton("Ask Riva"); self.send.setObjectName("rivaSend")
        self.send.clicked.connect(self.submit)
        input_row.addWidget(self.input, 1); input_row.addWidget(self.send)
        layout.addLayout(input_row)

        self.install_progress = QProgressBar(); self.install_progress.setVisible(False)
        self.install_progress.setRange(0, 100); layout.addWidget(self.install_progress)
        self.install = QPushButton("Install local AI - Qwen3.5-0.8B (563 MB)")
        self.install.setObjectName("rivaInstall"); self.install.clicked.connect(self.install_local_ai)
        layout.addWidget(self.install)

        privacy = QLabel("Local-only • Evidence grounded • Does not run or modify models")
        privacy.setObjectName("rivaPrivacy"); privacy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(privacy)
        self.setWidget(panel)
        self.set_theme(False)

    def set_theme(self, is_light: bool) -> None:
        if is_light:
            panel, header, surface = "#f4f6f8", "#e9eef2", "#ffffff"
            text, muted, border = "#1e2933", "#425d70", "#c9d2da"
            button, hover, disabled = "#e4eaee", "#d6e0e7", "#edf0f2"
            primary, primary_border = "#1976a8", "#0f6694"
        else:
            panel, header, surface = "#191b1d", "#25282b", "#202326"
            text, muted, border = "#eef1f4", "#9bb3c4", "#454a4f"
            button, hover, disabled = "#353a3e", "#454c51", "#2c2f32"
            primary, primary_border = "#236b94", "#3aa5e8"
        widget = self.widget()
        if widget is None:
            return
        widget.setStyleSheet(f"""
            QWidget#rivaPanel{{background:{panel};color:{text}}}
            QWidget#rivaHeader{{background:{header};border:1px solid {border};border-radius:6px}}
            QWidget#rivaPanel QLabel{{background:transparent;border:none;color:{text}}}
            QLabel#rivaTitle{{font-size:22px;font-weight:800;color:{text}}}
            QLabel#rivaSubtitle{{font-size:12px;font-weight:650;color:{text}}}
            QLabel#rivaStatus{{font-size:11px;color:{muted}}}
            QTextBrowser#rivaTranscript{{background:{surface};color:{text};border:1px solid {border};border-radius:4px;padding:8px;selection-background-color:{primary}}}
            QLineEdit#rivaInput{{background:{surface};color:{text};border:1px solid {border};border-radius:4px;padding:8px}}
            QPushButton#rivaSend{{background:{primary};color:white;border:1px solid {primary_border};border-radius:4px;padding:8px 12px;font-weight:700}}
            QPushButton#rivaQuick,QPushButton#rivaInstall{{background:{button};color:{text};border:1px solid {border};border-radius:4px;padding:7px}}
            QPushButton#rivaQuick:hover,QPushButton#rivaInstall:hover{{background:{hover}}}
            QPushButton:disabled{{background:{disabled};color:{muted}}}
            QLabel#rivaPrivacy{{color:{muted};font-size:10px;padding:3px}}
            QProgressBar{{background:{surface};color:{text};border:1px solid {border};border-radius:4px;text-align:center}}
            QProgressBar::chunk{{background:{primary}}}
        """)

    def _welcome(self) -> None:
        self.transcript.setHtml(
            "<div><b>Riva</b><br>Ask me about AutoCF, SFINCS parameters, "
            "HydroMT-SFINCS setup methods, or the active model’s saved results.</div>"
        )

    def _refresh_status(self) -> None:
        installed = self.backend.is_installed()
        rules = len(RULE_DATABASE.get("questions", ()))
        if RULE_DATABASE_ERROR:
            status = "Evidence database unavailable; original guidance active"
        else:
            status = f"Evidence mode: {rules} rules, local AI optional"
        self.status.setText("Qwen3.5-0.8B ready locally" if installed else status)
        self.install.setVisible(not installed)

    def _append(self, speaker: str, text: str, sources: list[Evidence] | None = None) -> None:
        body = html.escape(text)
        body = re.sub(
            r"(https?://[^\s&lt;&gt;()]+)",
            lambda match: (
                f"<a style='color:#7dd8ff' href='{match.group(1)}'>{match.group(1)}</a>"
            ),
            body,
        ).replace("\n", "<br>")
        block = f"<p><b>{html.escape(speaker)}</b><br>{body}</p>"
        if sources:
            unique: list[Evidence] = []
            for item in sources:
                if item.source not in {entry.source for entry in unique}:
                    unique.append(item)
            links = []
            for item in unique[:6]:
                if not item.source.startswith(("http://", "https://")):
                    continue
                source = html.escape(item.source, quote=True)
                label = html.escape(item.title)
                links.append(f"<a style='color:#7dd8ff' href='{source}'>{label}</a>")
            if links:
                block += "<p style='font-size:10px;color:#a9dfff'><b>Sources:</b> " + " • ".join(links) + "</p>"
        self.transcript.append(block)
        self.transcript.verticalScrollBar().setValue(self.transcript.verticalScrollBar().maximum())

    def _query_parts(self, question: str) -> tuple[list[Evidence], str]:
        context = self.context_provider()
        page = str(context.get("page", ""))
        normalized = _normalized_question(question)
        exact_faq = any(
            normalized == _normalized_question(alias)
            for faq in FAQS for alias in faq["aliases"]
        )
        faq = match_autocf_faq(question, page)

        def grounded_faq_answer(item: Evidence) -> tuple[list[Evidence], str]:
            nearby = nearby_case_studies(question, context.get("study_bounds"))
            if not nearby:
                return [item], item.text
            nearby_text = "\n".join(entry.text for entry in nearby)
            return [item, *nearby], item.text + "\n\nNearby study-area evidence:\n" + nearby_text

        if exact_faq and faq is not None:
            return grounded_faq_answer(faq)
        result = evaluation_answer(question, self.run_dir_provider())
        if result is None:
            result = exposure_answer(question, self.run_dir_provider())
        if result is None:
            result = attribution_answer(question, self.run_dir_provider())
        if result is not None:
            answer, evidence = result
            return evidence, answer
        lower = question.lower()
        if "explain page" in lower or "current page" in lower:
            answer = PAGE_ANSWERS.get(page)
            if answer:
                evidence = Evidence("AutoCF page guide", "AutoCF built-in guide", answer)
                return [evidence], answer
        if faq is not None:
            return grounded_faq_answer(faq)
        lower = question.lower()
        expanded = question
        if "current autocf page" in lower or "current page" in lower:
            expanded += " AutoCF page " + page
        docs = self.knowledge.search(expanded)
        return docs[:6], ""

    def submit(self, preset: str | bool = False) -> None:
        question = preset if isinstance(preset, str) else self.input.text().strip()
        if not question:
            return
        self.activityChanged.emit("thinking")
        if self._question_count:
            self.transcript.append(
                "<hr style='border:0;border-top:1px solid #39a9ff;margin:12px 0'>"
            )
        self._question_count += 1
        self.input.clear(); self._append("You", question)
        evidence, exact = self._query_parts(question)
        if exact:
            # Result files are summarized deterministically. Qwen is never
            # allowed to transcribe or recalculate scientific values.
            self._append("Riva", exact, evidence)
            self.activityChanged.emit("responding")
            return
        if self.busy_provider():
            answer = _evidence_fallback(evidence[0] if evidence else None) if evidence else (
                "AutoCF is running a job. Riva has paused the local language model to avoid competing for CPU and memory."
            )
            answer += "\n\nThe local language model remains paused while the AutoCF job is active."
            self._append("Riva", answer, evidence)
            self.activityChanged.emit("responding")
            return
        if not evidence and not exact:
            self._append(
                "Riva",
                "I could not find enough evidence in the bundled AutoCF, SFINCS, or HydroMT-SFINCS material. Try naming the exact parameter or workflow.",
            )
            self.activityChanged.emit("responding")
            return
        if not self.backend.is_installed():
            answer = _evidence_fallback(evidence[0] if evidence else None)
            answer += "\n\nQwen is optional; install it only if you want synthesized documentation answers."
            self._append("Riva", answer, evidence)
            self.activityChanged.emit("responding")
            return
        self.input.setEnabled(False); self.send.setEnabled(False)
        self.status.setText("Riva is reading the evidence…")
        self.ask_worker = RivaAskWorker(self.backend, question, evidence, exact)
        self.ask_worker.completed.connect(self._answer_ready)
        self.ask_worker.failed.connect(self._answer_failed)
        self.ask_worker.start()

    def _answer_ready(self, answer: str, evidence: list[Evidence]) -> None:
        answer = _words_limited(answer, 140)
        self._append("Riva", answer, evidence)
        self.activityChanged.emit("responding")
        self.input.setEnabled(True); self.send.setEnabled(True); self._refresh_status(); self.input.setFocus()

    def _answer_failed(self, message: str, evidence: list[Evidence]) -> None:
        fallback = _evidence_fallback(evidence[0] if evidence else None)
        self._append("Riva", fallback + f"\n\nLocal AI note: {message}", evidence)
        self.activityChanged.emit("responding")
        self.input.setEnabled(True); self.send.setEnabled(True); self._refresh_status()

    def install_local_ai(self) -> None:
        if self.busy_provider():
            QMessageBox.information(self, "AutoCF is busy", "Wait for the active AutoCF job to finish before installing Riva local AI.")
            return
        answer = QMessageBox.question(
            self, "Install Riva local AI",
            "Download Qwen3.5-0.8B Q4_0 (563 MB) and the Windows CPU llama.cpp engine?\n\n"
            "Files are stored under your local AutoCF application-data folder. No API key is used.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.activityChanged.emit("working")
        self.install.setEnabled(False); self.install_progress.setVisible(True); self.install_progress.setValue(0)
        self.install_worker = RivaInstallWorker(self.backend.user_dir)
        self.install_worker.progress.connect(self._install_progress)
        self.install_worker.completed.connect(self._install_complete)
        self.install_worker.failed.connect(self._install_failed)
        self.install_worker.start()

    def _install_progress(self, value: int, label: str) -> None:
        self.install_progress.setValue(value); self.status.setText(label)

    def _install_complete(self, folder: str) -> None:
        self.install_progress.setVisible(False); self.install.setEnabled(True); self._refresh_status()
        self._append("Riva", "Local Qwen and llama.cpp are installed and ready.")
        self.activityChanged.emit("responding")

    def _install_failed(self, message: str) -> None:
        self.install_progress.setVisible(False); self.install.setEnabled(True); self._refresh_status()
        self.activityChanged.emit("responding")
        QMessageBox.warning(self, "Riva installation failed", message)

    def shutdown(self) -> bool:
        if self.install_worker is not None and self.install_worker.isRunning():
            self.install_worker.requestInterruption()
        self.backend.stop()
        workers = [worker for worker in (self.ask_worker, self.install_worker) if worker is not None and worker.isRunning()]
        return all(worker.wait(5000) for worker in workers)
