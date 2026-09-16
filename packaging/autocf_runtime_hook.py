"""Runtime setup shared by the Linux and macOS PyInstaller bundles."""

from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("AUTOCF_VERSION", "1.0.0")

if getattr(sys, "frozen", False):
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    packaged_root = bundle_root / "autocf"
    if packaged_root.is_dir():
        os.environ["AUTOCF_PACKAGE_ROOT"] = str(packaged_root)

