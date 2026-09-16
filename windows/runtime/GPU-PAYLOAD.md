# Optional SFINCS GPU payload

AutoCF never labels an unverified executable as GPU-ready. The Windows WSL
backend enables NVIDIA GPU execution only when all three files below are
present in this directory and the installed WSL2 environment can see NVIDIA
hardware:

- `sfincs-gpu-rootfs.tar.gz`
- `sfincs-gpu-launcher.sh`
- `sfincs-gpu-manifest.json`

The manifest must identify the SFINCS version, CUDA runtime, payload SHA-256,
source/build provenance, supported NVIDIA driver range, and the regression
suite used to compare its numerical output with the bundled CPU solver.

These files are intentionally absent from this release. The current official
SFINCS distribution does not provide a supported GPU package suitable for
bundling. Automatic compute selection therefore uses the validated CPU/OpenMP
runtime. Choosing NVIDIA GPU explicitly gives a clear error instead of silently
running an unknown binary.
