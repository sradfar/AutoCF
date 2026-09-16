# AutoCF 1.0.0 Docker runtime

Status: **image build configured; GitHub Docker build not yet executed**.

The Ubuntu job builds `ghcr.io/OWNER/autocf-runtime:1.0.0` from the tested
edition2 runtime and smoke-tests Python, SFINCS, and the vertical-datum helper.
Docker is headless, so Qt is intentionally not installed in this image.

