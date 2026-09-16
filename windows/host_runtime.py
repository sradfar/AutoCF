"""Run the bundled AutoCF Linux environment through Docker or Podman."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


IMAGE = "autocf/runtime:2"
GPU_IMAGE = "autocf/runtime-gpu:2"


def normalize_compute_backend(value: object) -> str:
    compute = str(value or "auto").strip().lower().replace("nvidia", "gpu")
    return compute if compute in {"auto", "cpu", "gpu"} else "auto"


def image_is_available(engine: str, image: str) -> bool:
    return subprocess.run(
        [engine, "image", "inspect", image],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def resolve_container_compute(
    engine: str, requested: object, *, host_system: str | None = None,
) -> tuple[str, list[str], str]:
    """Select only a locally installed and explicitly validated GPU image."""
    compute = normalize_compute_backend(requested)
    system = (host_system or platform.system()).lower()
    gpu_available = system != "darwin" and image_is_available(engine, GPU_IMAGE)
    if compute == "gpu" and not gpu_available:
        if system == "darwin":
            raise RuntimeError(
                "SFINCS GPU execution is not supported on macOS; select CPU or Automatic."
            )
        raise RuntimeError(
            "A validated AutoCF SFINCS GPU image is not installed. "
            "Select CPU/Automatic or install a validated GPU payload."
        )
    if compute == "gpu" or (compute == "auto" and gpu_available):
        return GPU_IMAGE, ["--gpus", "all"], "NVIDIA GPU container"
    note = "CPU container"
    if compute == "auto":
        note += " (no validated GPU image detected)"
    return IMAGE, [], note


def parse_options(arguments: list[str]) -> dict[str, str | bool]:
    options: dict[str, str | bool] = {}
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if not token.startswith("-"):
            index += 1
            continue
        key = token.lstrip("-").lower()
        if index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
            options[key] = arguments[index + 1]
            index += 2
        else:
            options[key] = True
            index += 1
    return options


def container_engine() -> str | None:
    return shutil.which("docker") or shutil.which("podman")


def run_checked(command: list[str]) -> int:
    print("$ " + " ".join(command), flush=True)
    return subprocess.run(command).returncode


def platform_arguments() -> list[str]:
    machine = platform.machine().lower()
    return ["--platform", "linux/amd64"] if machine in {"arm64", "aarch64"} else []


def status(root: Path, compute: object = "auto") -> int:
    print("=" * 68)
    print(" AutoCF host status")
    print("=" * 68)
    print(f"Platform         : {platform.system()} {platform.machine()}")
    print(f"Package          : {root}")
    engine = container_engine()
    if engine is None:
        print("Container engine : NOT INSTALLED")
        print("Use Install / repair runtime in the AutoCF UI.")
        return 0
    print(f"Container engine : {Path(engine).name}")
    if subprocess.run([engine, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        print("Engine status    : NOT RUNNING")
        print("Start Docker Desktop or Podman Desktop, then check again.")
        return 0
    if not image_is_available(engine, IMAGE):
        print("AutoCF runtime   : NOT INSTALLED")
        return 0
    print("AutoCF runtime   : INSTALLED")
    try:
        _image, _gpu_args, backend = resolve_container_compute(engine, compute)
    except RuntimeError as exc:
        backend = f"UNAVAILABLE - {exc}"
    print(f"SFINCS backend   : {backend}")
    return 0


def install(root: Path) -> int:
    engine = container_engine()
    if engine is None:
        print("No supported container engine was found.")
        if platform.system() == "Darwin":
            print("Install Docker Desktop or Podman Desktop, start it, then select Install again.")
        else:
            print("Install Docker Engine or Podman with your system software manager, then select Install again.")
        return 2
    if subprocess.run([engine, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        print("The container engine is installed but not running. Start it and select Install again.")
        return 2
    dockerfile = root / "host" / "Dockerfile.runtime"
    required = [
        dockerfile,
        root / "windows" / "runtime" / "autocf-wsl-rootfs.tar.gz",
        root / "windows" / "runtime" / "sfincs-cpu-rootfs.tar.gz",
        root / "windows" / "runtime" / "sfincs-cpu-launcher.sh",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print("Missing runtime payloads:")
        print("\n".join(f"  {path}" for path in missing))
        return 1
    command = [engine, "build", *platform_arguments(), "--file", str(dockerfile), "--tag", IMAGE, str(root)]
    code = run_checked(command)
    if code == 0:
        print("AutoCF runtime installation: PASS")
    return code


def container_path(path: Path, root: Path) -> tuple[str, list[str]]:
    path = path.expanduser().resolve()
    try:
        relative = path.relative_to(root)
        return "/workspace/" + relative.as_posix(), []
    except ValueError:
        return "/project/" + path.name, ["-v", f"{path.parent}:/project"]


def mirrored_container_target(path: Path, system: str | None = None) -> str:
    """Return the container path used when preserving an absolute host path.

    AutoCF YAML written by the desktop contains native absolute paths.  On
    Linux and macOS those paths can be mounted at the same location.  The
    runtime launcher normalizes Windows drive paths to WSL-style ``/mnt/c``
    paths, so Docker Desktop mirrors them there as well.
    """
    resolved = path.expanduser().resolve()
    host_system = (system or platform.system()).lower()
    if host_system == "windows":
        drive = resolved.drive
        if len(drive) != 2 or drive[1] != ":":
            raise ValueError(f"AutoCF Docker requires a local drive path: {resolved}")
        relative = resolved.relative_to(resolved.anchor).as_posix()
        suffix = f"/{relative}" if relative and relative != "." else ""
        return f"/mnt/{drive[0].lower()}{suffix}"
    return resolved.as_posix()


def mirrored_mounts(paths: list[Path], system: str | None = None) -> list[str]:
    """Mount distinct host trees at paths understood by the YAML runtime."""
    roots: list[Path] = []
    for path in sorted(
        {candidate.expanduser().resolve() for candidate in paths},
        key=lambda candidate: len(str(candidate)),
    ):
        if any(path == parent or parent in path.parents for parent in roots):
            continue
        roots.append(path)
    arguments: list[str] = []
    for path in roots:
        target = mirrored_container_target(path, system)
        arguments += ["-v", f"{path}:{target}"]
    return arguments


def execute(root: Path, options: dict[str, str | bool]) -> int:
    engine = container_engine()
    if engine is None:
        print("The managed container runtime is unavailable. Select Install / repair runtime.")
        return 2
    config_value = options.get("config")
    if not isinstance(config_value, str):
        print("A configuration file is required.")
        return 2
    config_path = Path(config_value).expanduser().resolve()
    config, extra_mount = container_path(config_path, root)
    action = str(options.get("action", "run")).lower()
    cpu_threads = str(options.get("cputhreads", "0"))
    if cpu_threads == "0":
        cpu_threads = str(os.cpu_count() or 1)
    proj_network = "ON" if action == "impact" else "OFF"
    try:
        runtime_image, gpu_arguments, compute_note = resolve_container_compute(
            engine, options.get("computebackend", "auto"),
        )
    except RuntimeError as exc:
        print(f"SFINCS compute selection failed: {exc}")
        return 2
    print(f"SFINCS compute   : {compute_note}")
    environment = [
        "-e", "PYTHONPATH=/workspace/app:/workspace/vendor",
        *([] if action == "forcingpreview" else [
            "-e", "AUTOCF_PACKAGE_ROOT=/workspace"]),
        "-e", "SFINCS_EXECUTABLE=/usr/local/bin/sfincs-cpu",
        "-e", "PYTHONNOUSERSITE=1",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "XDG_CACHE_HOME=/tmp/autocf-cache",
        "-e", "MPLCONFIGDIR=/tmp/autocf-mpl", "-e", f"PROJ_NETWORK={proj_network}",
        "-e", "HDF5_USE_FILE_LOCKING=FALSE",
        "-e", f"OMP_NUM_THREADS={cpu_threads}", "-e", "OMP_DYNAMIC=FALSE",
    ]
    if runtime_image == GPU_IMAGE:
        environment[environment.index("SFINCS_EXECUTABLE=/usr/local/bin/sfincs-cpu")] = (
            "SFINCS_EXECUTABLE=/usr/local/bin/sfincs-gpu"
        )
    if action == "run" and runtime_image == IMAGE:
        environment += ["-e", "AUTOCF_TRANSFORMEZ_EXECUTABLE=/usr/local/bin/transformez-helper"]
    mirrored = mirrored_mounts([root, config_path.parent])
    cds_api_rc = Path(os.environ.get("CDSAPI_RC", str(Path.home() / ".cdsapirc"))).expanduser()
    cds_mount: list[str] = []
    if cds_api_rc.is_file():
        cds_mount = ["-v", f"{cds_api_rc.resolve()}:/root/.cdsapirc:ro"]
    command = [
        engine, "run", "--rm", *platform_arguments(), *gpu_arguments,
        "-v", f"{root}:/workspace",
        *extra_mount, *mirrored, *cds_mount, "-w", "/workspace", *environment, IMAGE,
    ]
    command[-1] = runtime_image
    if action == "run":
        command += [
            "/usr/local/bin/python", "-u", "/workspace/windows/autocf_runtime.py",
            "--app", "/workspace/app/sfincs_agent.py", "--config", config,
            "--mode", str(options.get("mode", "full")),
        ]
    elif action == "evaluate":
        command += [
            "/usr/local/bin/python", "-u", "/workspace/app/sfincs_eval.py",
            "--config", config, "--tolerance-min", str(options.get("evaluationtoleranceminutes", "4")),
            "--hmax-vmax", str(options.get("evaluationhmaxvmax", "3")),
        ]
        if options.get("skipevaluationhmax"): command.append("--skip-hmax")
        if options.get("skipevaluationobservations"): command.append("--skip-observations")
    elif action == "impact":
        command += [
            "/usr/local/bin/python", "-u", "/workspace/app/sfincs_impact.py",
            "--config", config, "--thresholds-m", str(options.get("impactthresholds", "0.15,0.3,0.6,1.0,2.0")),
            "--analysis-resolution-m", str(options.get("impactanalysisresolution", "30")),
        ]
        if options.get("skipimpactbuildings"): command.append("--skip-buildings")
        if options.get("skipimpactroads"): command.append("--skip-roads")
        if options.get("skipimpactpopulation"): command.append("--skip-population")
        if options.get("forceimpactdownloads"): command.append("--force-downloads")
    elif action == "forcingpreview":
        command += [
            "/usr/local/bin/python", "-u", "/workspace/app/sfincs_forcing_preview.py",
            "--config", config,
        ]
    elif action in {
        "analysisprepare", "analysisprepareexposure", "analysisrun", "analysisreport",
    }:
        analysis_command = {
            "analysisprepare": "prepare",
            "analysisprepareexposure": "prepare-exposure",
            "analysisrun": "run",
            "analysisreport": "report",
        }[action]
        command += [
            "/usr/local/bin/python", "-u", "/workspace/app/sfincs_attribution.py",
            "--config", config, analysis_command,
        ]
        if options.get("forceanalysis") and analysis_command in {"prepare", "prepare-exposure", "run"}:
            command.append("--force")
        if options.get("skipanalysispopulation") and analysis_command == "report":
            command.append("--skip-population")
    else:
        print(f"Unsupported AutoCF action: {action}")
        return 2
    return run_checked(command)


def main(arguments: list[str] | None = None, root: Path | None = None) -> int:
    arguments = list(arguments if arguments is not None else sys.argv[1:])
    options = parse_options(arguments)
    package_root = (root or Path(__file__).resolve().parent.parent).resolve()
    action = str(options.get("action", "status")).lower()
    if action == "status": return status(package_root, options.get("computebackend", "auto"))
    if action == "install": return install(package_root)
    if action == "unsupported":
        print(f"AutoCF does not have a runtime backend for {platform.system()}.")
        return 2
    return execute(package_root, options)


if __name__ == "__main__":
    raise SystemExit(main())
