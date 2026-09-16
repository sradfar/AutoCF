"""Fail closed when a macOS bundle exposes AutoCF source or developer data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath


FORBIDDEN_SUFFIXES = {".py", ".pyw", ".ipynb", ".spec", ".pdb"}
FORBIDDEN_PARTS = {
    ".git", ".github", ".codex-build", ".codex-build-macos-rc3",
    "__pycache__", "tests", "packaging",
}
PRIVATE_MARKERS = (
    b"c:\\users\\fmagh", b"c:/users/fmagh", b"/mnt/c/users/fmagh",
    b"onedrive - the university of alabama",
)
REQUIRED_SUFFIXES = {
    "app/sfincs_agent.pyc",
    "app/sfincs_eval.pyc",
    "windows/autocf_runtime.pyc",
    "windows/transformez_helper.pyc",
    "host/dockerfile.runtime",
    "host/transformez-helper.sh",
    "release-manifest.json",
}


def locate_payload(target: Path) -> Path:
    direct = target / "app"
    if direct.is_dir():
        return target
    candidates = list(target.rglob("Resources/autocf"))
    if len(candidates) != 1:
        raise RuntimeError(f"could not identify one AutoCF payload below {target}")
    return candidates[0]


def audit_directory(target: Path) -> dict[str, object]:
    payload = locate_payload(target)
    files = [path for path in payload.rglob("*") if path.is_file()]
    names = [path.relative_to(payload).as_posix() for path in files]
    errors: list[str] = []
    lowered = {name.lower() for name in names}
    for name in names:
        path = PurePosixPath(name)
        parts = {part.lower() for part in path.parts}
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"readable/developer source is packaged: {name}")
        if parts & FORBIDDEN_PARTS:
            errors.append(f"developer-only path is packaged: {name}")
    for required in REQUIRED_SUFFIXES:
        if not any(name.endswith(required) for name in lowered):
            errors.append(f"required sanitized payload is missing: {required}")
    for path, name in zip(files, names):
        if path.stat().st_size <= 25_000_000:
            data = path.read_bytes().lower()
            for marker in PRIVATE_MARKERS:
                if marker in data:
                    errors.append(
                        f"private developer path marker found in {name}: "
                        f"{marker.decode('ascii', 'replace')}"
                    )
    return {
        "target": str(target), "payload": str(payload),
        "files": len(files), "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    result = audit_directory(args.target.resolve())
    print(json.dumps(result, indent=2))
    if result["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
