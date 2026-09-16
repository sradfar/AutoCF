"""Platform-aware command selection for the AutoCF desktop application."""
from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformRuntime:
    """Select the packaged runtime backend without changing scientific actions."""

    root: Path
    system: str = ""
    backend: str = ""
    compute: str = "auto"

    def __post_init__(self) -> None:
        object.__setattr__(self, "system", (self.system or platform.system()).lower())
        requested = self.backend.strip().lower()
        if self.system == "windows":
            aliases = {
                "": "wsl",
                "wsl": "wsl",
                "wsl2": "wsl",
                "docker": "docker",
                "container": "docker",
                "podman": "docker",
            }
            object.__setattr__(self, "backend", aliases.get(requested, "wsl"))
        else:
            object.__setattr__(self, "backend", "container")
        compute = self.compute.strip().lower().replace("nvidia", "gpu")
        if compute not in {"auto", "cpu", "gpu"}:
            compute = "auto"
        if self.system == "darwin" and compute == "gpu":
            compute = "cpu"
        object.__setattr__(self, "compute", compute)

    @property
    def uses_container(self) -> bool:
        return self.system in {"linux", "darwin"} or (
            self.system == "windows" and self.backend == "docker"
        )

    @property
    def platform_name(self) -> str:
        return {"windows": "Windows", "linux": "Linux", "darwin": "macOS"}.get(
            self.system, platform.system() or self.system
        )

    @property
    def backend_name(self) -> str:
        if self.system == "windows" and self.backend == "wsl":
            return "Managed WSL2 runtime"
        if self.uses_container:
            if self.system == "windows":
                return "Docker Desktop runtime"
            return "Managed Docker/Podman runtime"
        return "Unsupported platform"

    @property
    def compute_name(self) -> str:
        return {
            "auto": "Automatic (validated GPU when available)",
            "cpu": "CPU (OpenMP)",
            "gpu": "NVIDIA GPU (validated SFINCS payload required)",
        }[self.compute]

    def with_compute(self, arguments: list[str]) -> list[str]:
        """Add the compute preference understood by every managed backend."""
        lowered = {item.lower() for item in arguments}
        if "-computebackend" in lowered or "--computebackend" in lowered:
            return list(arguments)
        return [*arguments, "-ComputeBackend", self.compute]

    @property
    def supports_restart(self) -> bool:
        return self.system == "windows" and self.backend == "wsl"

    def command(self, arguments: list[str]) -> list[str]:
        arguments = self.with_compute(arguments)
        if self.system == "windows" and self.backend == "wsl":
            backend = self.root / "windows" / "AutoCF-Windows.ps1"
            return [
                "powershell.exe", "-NoLogo", "-NoProfile",
                "-ExecutionPolicy", "Bypass", "-File", str(backend), *arguments,
            ]

        if not self.uses_container:
            return self.worker_command(["-Action", "Unsupported"])
        return self.worker_command(arguments)

    def install_command(self) -> list[str]:
        if self.system == "windows" and self.backend == "wsl":
            return [
                "cmd.exe", "/d", "/c", str(self.root / "Install-AutoCF.cmd"),
                "--non-interactive",
            ]
        return self.command(["-Action", "Install"])

    def worker_command(self, arguments: list[str]) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--runtime-worker", *arguments]
        desktop = Path(__file__).with_name("autocf_desktop_v2.py")
        return [sys.executable, str(desktop), "--runtime-worker", *arguments]

    def restart_command(self) -> list[str] | None:
        if self.supports_restart:
            return ["shutdown.exe", "/r", "/t", "0"]
        return None
