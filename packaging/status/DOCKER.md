# AutoCF 1.0.0 Docker runtime

Status: **native CI image build and runtime smoke tests verified; published**.

Pull image: `docker pull ghcr.io/sradfar/autocf-runtime:1.0.0`

The Ubuntu job built the tested edition2 runtime and passed smoke tests for
Python, native SFINCS startup, and the vertical-datum helper before publishing
it to GHCR. Docker is headless, so Qt is intentionally not installed.

