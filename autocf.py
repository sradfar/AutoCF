#!/usr/bin/env python3
"""
autocf.py

User-facing orchestrator for the SFINCS Agent.

The proven hydraulic implementation is retained as sfincs_hydraulic_core.py. AutoCF adds:
  - one canonical, non-duplicative configuration
  - global gauge preparation (NOAA -> GESLA-4.1 -> UHSLC/JASL)
  - production evaluation without SAR/remote-sensing extent scores
  - exact C/P/R factorial attribution
  - Overture building and WorldPop population exposure

Della split:
  login/internet : prepare, build, evaluate, analysis-prepare, analysis-report
  GPU/offline     : run, analysis-run
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from sfincs_config import (
    dump_resolved_canonical,
    read_config,
    resolved_output_dir,
    write_legacy_config,
)
from sfincs_global_validation import prepare_water_level_validation
from sfincs_attribution import prepare_overture_buildings, prepare_scenarios, run_scenarios, analyze


def log(msg: str) -> None:
    print(f"[AutoCF] {msg}", flush=True)


def require_hydraulic_environment() -> None:
    """Fail immediately if AutoCF was launched outside the SFINCS/HydroMT environment."""
    import importlib.util

    missing = []
    for module in ("hydromt", "hydromt_sfincs"):
        if importlib.util.find_spec(module) is None:
            missing.append(module)

    hydromt_exe = shutil.which("hydromt")

    if missing or hydromt_exe is None:
        raise RuntimeError(
            "AutoCF hydraulic commands require the SFINCS/HydroMT Python environment.\n"
            f"Current Python: {sys.executable}\n"
            f"hydromt executable: {hydromt_exe}\n"
            f"missing Python modules: {missing}\n\n"
            "On Princeton Della run:\n"
            "  module purge\n"
            "  module load anaconda3/2024.10\n"
            "  conda activate /scratch/gpfs/NLIN/sradfar/conda_envs/sfincs_agent"
        )


def _resolve_script(value: str, config_path: Path) -> str:
    p = Path(value).expanduser()
    if p.is_absolute() and p.exists():
        return str(p)
    if "/" in value:
        p = (config_path.parent / p).resolve()
        if p.exists():
            return str(p)
    local = (config_path.parent / value).resolve()
    if local.exists():
        return str(local)
    found = shutil.which(value)
    if found:
        return found
    raise FileNotFoundError(
        f"Required script/command not found: {value}. "
        f"Expected beside {config_path} or on PATH."
    )


def _prepared_station_file(cfg: Dict[str, Any]) -> Optional[Path]:
    p = resolved_output_dir(cfg) / "agent_validation" / "water_level" / "stations.geojson"
    return p if p.exists() else None


def write_internal_config(
    cfg: Dict[str, Any],
    canonical_path: Path,
    mode: str,
    stations: Optional[Path] = None,
) -> Path:
    run = resolved_output_dir(cfg)
    work = run / "agent_work"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "_autocf_resolved_legacy.yml"
    write_legacy_config(
        cfg,
        path,
        workflow_mode=mode,
        observation_locations=stations,
    )
    dump_resolved_canonical(cfg, work / "_autocf_resolved_canonical.yml")
    log(f"Internal hydraulic config: {path}")
    return path


def call_core(cfg: Dict[str, Any], canonical_path: Path, mode: str, stations: Optional[Path]) -> None:
    core = _resolve_script(str(cfg["runtime"].get("core_agent", "sfincs_hydraulic_core.py")), canonical_path)
    internal = write_internal_config(cfg, canonical_path, mode, stations)
    cmd = [sys.executable, "-u", core, str(internal)]
    log("Executing established hydraulic core:")
    log("  " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def prepare(cfg: Dict[str, Any], canonical_path: Path, exposure: bool = True) -> Optional[Path]:
    run = resolved_output_dir(cfg)
    run.mkdir(parents=True, exist_ok=True)

    log("Preparing global hydraulic validation stations/observations...")
    stations = prepare_water_level_validation(cfg)

    if exposure and bool(cfg.get("analysis", {}).get("exposure", {}).get("enabled", False)):
        try:
            p = prepare_overture_buildings(cfg, force=False)
            if p:
                log(f"Overture buildings cached: {p}")
        except Exception as exc:
            required = bool(
                (cfg.get("analysis", {}).get("exposure", {}).get("buildings", {}) or {}).get("required", False)
            )
            if required:
                raise
            log(f"Optional building exposure preparation skipped: {exc}")

    write_internal_config(cfg, canonical_path, "build_only", stations)
    scope = {
        "water_level_stations": str(stations) if stations else None,
        "remote_sensing_validation": False,
        "exposure_prepared": exposure,
    }
    (run / "agent_reports").mkdir(parents=True, exist_ok=True)
    (run / "agent_reports" / "autocf_prepare_scope.json").write_text(json.dumps(scope, indent=2))
    return stations


def cmd_build(cfg: Dict[str, Any], canonical_path: Path) -> None:
    stations = prepare(cfg, canonical_path, exposure=True)
    call_core(cfg, canonical_path, "build_only", stations)


def cmd_run(cfg: Dict[str, Any], canonical_path: Path) -> None:
    # GPU nodes have no internet. Never perform discovery here.
    stations = _prepared_station_file(cfg)
    if bool(cfg.get("validation", {}).get("water_level", {}).get("enabled", True)) and stations is None:
        log(
            "No prepared station file found. The hydraulic run can still proceed, "
            "but sfincs_his.nc will not contain automatic global validation points. "
            "Run `autocf.py ... prepare` on a login node first for a new case."
        )
    call_core(cfg, canonical_path, "run_direct_only", stations)


def cmd_evaluate(cfg: Dict[str, Any], canonical_path: Path, extra: list[str]) -> None:
    evaluator = _resolve_script(str(cfg["runtime"].get("evaluator", "sfincs_eval.py")), canonical_path)
    cmd = [sys.executable, "-u", evaluator, "--config", str(canonical_path), *extra]
    log("Running production hydraulic evaluator (no SAR/remote sensing)...")
    subprocess.run(cmd, check=True)


def cmd_analysis_prepare(cfg: Dict[str, Any], force: bool) -> None:
    if not (resolved_output_dir(cfg) / "sfincs_map.nc").exists():
        raise RuntimeError("Complete the parent/full SFINCS run before preparing factorial scenarios.")
    m = prepare_scenarios(cfg, force=force)
    log(f"Factorial scenario manifest: {m}")
    try:
        p = prepare_overture_buildings(cfg, force=force)
        if p:
            log(f"Overture buildings: {p}")
    except Exception as exc:
        required = bool(
            (cfg.get("analysis", {}).get("exposure", {}).get("buildings", {}) or {}).get("required", False)
        )
        if required:
            raise
        log(f"Optional Overture preparation skipped: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="AutoCF: automated SFINCS construction, validation, attribution, and exposure."
    )
    ap.add_argument("--config", default="sfincs_agent_subgrid_config.yml")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("prepare", help="LOGIN: prepare validation/exposure inputs, no hydraulic build.")
    sub.add_parser("build", help="LOGIN: prepare online inputs and build SFINCS.")
    sub.add_parser("run", help="GPU/OFFLINE: run the completed SFINCS build.")
    p = sub.add_parser("evaluate", help="LOGIN: hmax + gauges + HWM only.")
    p.add_argument("eval_args", nargs=argparse.REMAINDER)

    p = sub.add_parser("analysis-prepare", help="LOGIN: create factorial scenarios + Overture cache.")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("analysis-run", help="GPU/OFFLINE: run the seven counterfactual scenarios.")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("analysis-report", help="LOGIN: Shapley + amplification + exposure.")
    p.add_argument("--skip-population", action="store_true")
    sub.add_parser("show-config", help="Print resolved canonical config.")

    args = ap.parse_args()
    cpath = Path(args.config).expanduser().resolve()
    cfg = read_config(cpath)

    if args.command == "prepare":
        p = prepare(cfg, cpath, exposure=True)
        log(f"Prepared station file: {p}")
    elif args.command == "build":
        require_hydraulic_environment()
        cmd_build(cfg, cpath)
    elif args.command == "run":
        require_hydraulic_environment()
        cmd_run(cfg, cpath)
    elif args.command == "evaluate":
        require_hydraulic_environment()
        cmd_evaluate(cfg, cpath, list(args.eval_args))
    elif args.command == "analysis-prepare":
        cmd_analysis_prepare(cfg, force=args.force)
    elif args.command == "analysis-run":
        run_scenarios(cfg, force=args.force)
    elif args.command == "analysis-report":
        analyze(cfg, skip_population=args.skip_population)
    elif args.command == "show-config":
        import yaml
        obj = dict(cfg)
        obj.pop("_meta", None)
        print(yaml.safe_dump(obj, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
