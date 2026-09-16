"""Release discovery and safe local storage for optional SFINCS software."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

GITHUB_LATEST_API = "https://api.github.com/repos/Deltares/SFINCS/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/Deltares/SFINCS/releases"
GITHUB_API_VERSION = "2022-11-28"
WINDOWS_DOWNLOAD_URL = "https://download.deltares.nl/en/sfincs/"
DOCKER_HUB_URL = "https://hub.docker.com/r/deltares/sfincs-cpu"
VERSION_PATTERN = re.compile(r"(?i)(?:\bv(?:ersion)?[\s_:-]*)?(\d+\.\d+\.\d+)")


class DownloadCancelled(RuntimeError):
    pass


def version_tuple(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = VERSION_PATTERN.search(value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def normalized_version(value: str | None) -> str | None:
    parsed = version_tuple(value)
    return ".".join(str(part) for part in parsed) if parsed else None


def is_newer(candidate: str | None, installed: str | None) -> bool:
    candidate_tuple, installed_tuple = version_tuple(candidate), version_tuple(installed)
    return bool(candidate_tuple and installed_tuple and candidate_tuple > installed_tuple)


def fetch_latest_release(timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(
        GITHUB_LATEST_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AutoCF-Desktop/2.1",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    tag = str(payload.get("tag_name") or "").strip()
    zip_url = str(payload.get("zipball_url") or "").strip()
    html_url = str(payload.get("html_url") or "").strip()
    if not tag or not zip_url or not html_url:
        raise RuntimeError("GitHub returned incomplete SFINCS release metadata.")
    return {
        "tag_name": tag,
        "version": normalized_version(tag),
        "name": str(payload.get("name") or tag),
        "published_at": str(payload.get("published_at") or ""),
        "html_url": html_url,
        "zipball_url": zip_url,
    }


def _walk_limited(root: Path, max_depth: int = 4):
    root_depth = len(root.parts)
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        if len(current_path.parts) - root_depth >= max_depth:
            directories[:] = []
        yield current_path, files


def inspect_installation(directory: Path) -> dict[str, Any]:
    root = directory.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Directory does not exist: {root}")

    metadata_path = root / "autocf-sfincs.json"
    metadata: dict[str, Any] = {}
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}

    executable_names = {"sfincs", "sfincs.exe", "sfincs-cpu", "sfincs-cpu.exe"}
    executables: list[Path] = []
    model_files: list[Path] = []
    version_texts = [str(metadata.get("tag_name") or ""), str(metadata.get("version") or ""), root.name]
    preferred_text_names = {
        "readme", "readme.md", "readme.rst", "version", "version.txt",
        "licensing conditions deltares freeware executable.txt",
    }
    for current, files in _walk_limited(root):
        for name in files:
            lower = name.lower()
            path = current / name
            if lower in executable_names:
                executables.append(path)
                version_texts.append(name)
            if lower == "sfincs.inp":
                model_files.append(path)
            if lower in preferred_text_names or ("license" in lower and path.suffix.lower() in {".txt", ".md", ".rst"}):
                try:
                    if path.stat().st_size <= 2_000_000:
                        version_texts.append(path.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass

    source_tree = (root / "source").is_dir() or (root / "src" / "sfincs_lib.f90").is_file()
    version = next((normalized_version(text) for text in version_texts if normalized_version(text)), None)
    kind = "runnable" if executables else "source" if source_tree else "model" if model_files else "unrecognized"
    return {
        "directory": str(root),
        "version": version,
        "kind": kind,
        "executables": [str(path) for path in sorted(executables)],
        "model_files": [str(path) for path in sorted(model_files)],
        "metadata": metadata,
    }


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "latest"


def _extract_github_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        members = [member for member in bundle.infolist() if not member.is_dir()]
        prefixes = {member.filename.split("/", 1)[0] for member in members if "/" in member.filename}
        prefix = next(iter(prefixes)) if len(prefixes) == 1 else ""
        for member in members:
            relative = member.filename
            if prefix and relative.startswith(prefix + "/"):
                relative = relative[len(prefix) + 1:]
            relative_path = Path(relative)
            if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
                raise RuntimeError("The GitHub archive contains an unsafe path.")
            target = (destination / relative_path).resolve()
            if destination_resolved not in target.parents:
                raise RuntimeError("The GitHub archive attempted to write outside the SFINCS folder.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def download_release_source(
    release: dict[str, Any],
    software_root: Path,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    timeout: int = 60,
) -> Path:
    tag = str(release.get("tag_name") or "").strip()
    url = str(release.get("zipball_url") or "").strip()
    if not tag or not url:
        raise ValueError("Release metadata does not contain a tag and source archive URL.")

    base = software_root.expanduser().resolve() / "SFINCS"
    base.mkdir(parents=True, exist_ok=True)
    target = base / _safe_name(tag)
    metadata_path = target / "autocf-sfincs.json"
    if metadata_path.is_file():
        return target

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AutoCF-Desktop/2.1",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    temp_parent = Path(tempfile.mkdtemp(prefix=".download-", dir=base))
    archive = temp_parent / "sfincs-source.zip"
    extracted = temp_parent / "source"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, archive.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            while True:
                if cancelled and cancelled():
                    raise DownloadCancelled("SFINCS download cancelled.")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
        _extract_github_zip(archive, extracted)
        metadata = {
            "provider": "Deltares/SFINCS on GitHub",
            "content": "source",
            "tag_name": tag,
            "version": release.get("version"),
            "release_name": release.get("name"),
            "published_at": release.get("published_at"),
            "release_url": release.get("html_url"),
        }
        (extracted / "autocf-sfincs.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        if target.exists():
            raise FileExistsError(f"SFINCS destination already exists: {target}")
        extracted.replace(target)
        return target
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)
