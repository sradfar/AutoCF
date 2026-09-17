"""Runtime setup shared by the Linux and macOS PyInstaller bundles."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import certifi


os.environ.setdefault("AUTOCF_VERSION", "1.0.0")

# urllib/ssl in a frozen macOS application may otherwise look for a certificate
# file from the build machine or an unconfigured system Python installation.
# Force all standard Python HTTPS clients to the CA bundle shipped in the app.
ca_bundle = Path(certifi.where()).resolve()
if not ca_bundle.is_file():
    raise RuntimeError(f"Bundled TLS certificate store is missing: {ca_bundle}")
os.environ["SSL_CERT_FILE"] = str(ca_bundle)
os.environ["REQUESTS_CA_BUNDLE"] = str(ca_bundle)

if getattr(sys, "frozen", False):
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    packaged_root = bundle_root / "autocf"
    if packaged_root.is_dir():
        os.environ["AUTOCF_PACKAGE_ROOT"] = str(packaged_root)
