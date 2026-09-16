"""Self-contained Transformez bridge for AutoCF's Python 3.11 runtime.

This program is frozen with Python 3.12+ and invoked as a separate process.
It deliberately shares no Python packages with the HydroMT/SFINCS runtime.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


HELPER_VERSION = "1.0"


def prepare_embedded_tools() -> None:
    """Expose bundled NOAA command-line tools before Transformez is imported."""
    roots = []
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        roots.append(Path(bundled_root))
    roots.append(Path(sys.executable).resolve().parent)
    prefixes = [str(root) for root in roots if root.is_dir()]
    if prefixes:
        os.environ["PATH"] = os.pathsep.join(prefixes + [os.environ.get("PATH", "")])


def native_path(value: str) -> Path:
    """Translate a mounted WSL path to the Windows path seen by this helper."""
    text = str(value).strip()
    match = re.match(r"^/mnt/([A-Za-z])/(.*)$", text)
    if match:
        tail = match.group(2).replace("/", "\\")
        return Path(f"{match.group(1).upper()}:\\{tail}")
    return Path(text).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Transformez-Helper",
        description="Isolated vertical-datum raster conversion for AutoCF.",
    )
    parser.add_argument("--version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    raster = subparsers.add_parser("raster")
    raster.add_argument("input_file")
    raster.add_argument("-I", "--input-datum", required=True)
    raster.add_argument("-O", "--output-datum", required=True)
    raster.add_argument("-o", "--out", required=True)
    raster.add_argument("--in-units", choices=("auto", "m", "ft", "us-ft"), default="m")
    raster.add_argument("--out-units", choices=("auto", "m", "ft", "us-ft"), default="m")
    raster.add_argument("--decay-pixels", type=int, default=0)
    return parser


def transform_raster(args: argparse.Namespace) -> int:
    prepare_embedded_tools()
    import transformez

    source = native_path(args.input_file).resolve()
    output = native_path(args.out).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input raster does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache_value = str(os.environ.get("AUTOCF_TRANSFORMEZ_CACHE", "")).strip()
    cache = native_path(cache_value).resolve() if cache_value else output.parent / "transformez_cache"
    cache.mkdir(parents=True, exist_ok=True)
    result = transformez.transform_raster(
        input_raster=str(source),
        datum_in=str(args.input_datum),
        datum_out=str(args.output_datum),
        decay_pixels=int(args.decay_pixels),
        output_raster=str(output),
        cache_dir=str(cache),
        z_unit_in=str(args.in_units),
        z_unit_out=str(args.out_units),
        save_shift=False,
        verbose=False,
    )
    produced = native_path(str(result)).resolve() if result else output
    if produced != output and produced.is_file() and not output.is_file():
        produced.replace(output)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("Transformez did not produce the requested output raster.")
    print(output)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        try:
            prepare_embedded_tools()
            import transformez
            version = getattr(transformez, "__version__", "unknown")
        except Exception as exc:
            print(f"Transformez-Helper {HELPER_VERSION}; Transformez unavailable: {exc}")
            return 1
        print(f"Transformez-Helper {HELPER_VERSION}; Transformez {version}")
        return 0
    if args.command == "raster":
        try:
            return transform_raster(args)
        except Exception as exc:
            print(f"Transformez conversion failed: {exc}", file=sys.stderr)
            return 2
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
