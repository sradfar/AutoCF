# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform AutoCF desktop bundle.

The Windows release remains a one-file executable. Linux is an onedir bundle,
and macOS is a real .app bundle. Linux/macOS embed the scientific/container
support tree under ``autocf`` so the app is not tied to the developer machine.
"""

import os
import platform
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


# PyInstaller defines SPECPATH as the directory containing this spec file.
# The spec lives in <repository>/packaging, so its parent is the repository.
root = Path(SPECPATH).resolve().parent
payload_root = Path(os.environ.get("AUTOCF_PAYLOAD_ROOT", str(root))).resolve()
system = platform.system().lower()
version = os.environ.get("AUTOCF_VERSION", "1.0.0")
product = "AutoCF-Desktop"
entry_script = root / "windows" / "autocf_desktop_v2.py"
if not entry_script.is_file():
    raise FileNotFoundError(f"AutoCF entry script not found: {entry_script}")

datas = []
binaries = []
hiddenimports = []

# On Windows/Linux, preserve the broad collection used by the tested edition2
# package. On macOS, collecting the whole PySide6 distribution also captures
# Qt development applications (Assistant.app, Designer.app, and tools). Those
# unrelated nested apps break hardened-runtime signing. PyInstaller's PySide6
# module hooks below collect the required frameworks, plugins, translations,
# WebEngine helper, and Chromium resources without the development tools.
if system != "darwin":
    qt_datas, qt_binaries, qt_hidden = collect_all("PySide6")
    datas += qt_datas
    binaries += qt_binaries
    hiddenimports += qt_hidden

# The UI imports these modules lazily on some code paths.
hiddenimports += [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PIL.Image",
    "ruamel.yaml",
    "ruamel.yaml.clib",
    "yaml",
]

# Native Linux/macOS downloads contain everything required by AutoCF's host
# launcher and Docker-backed scientific runtime. Cache, runs, and build output
# are deliberately not packaged.
if system in {"darwin", "linux"}:
    for directory in ("app", "windows", "host", "vendor", "data", "examples", "logo"):
        source = payload_root / directory
        if source.is_dir():
            datas.append((str(source), f"autocf/{directory}"))
    for filename in (
        "README.md", "RELEASE-NOTES.txt", "START-HERE.txt", "release-manifest.json",
    ):
        source = payload_root / filename
        if source.is_file():
            datas.append((str(source), "autocf"))

a = Analysis(
    [str(entry_script)],
    pathex=[str(root / "windows"), str(root / "app")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "packaging" / "autocf_runtime_hook.py")],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Fail during packaging, not on a user's machine, if WebEngine was omitted.
bundle_names = "\n".join(str(item[0]).lower() for item in (a.binaries + a.datas))
for required in ("qtwebengine", "qtwebengineprocess", "qtwebchannel"):
    if required not in bundle_names:
        raise RuntimeError(f"Incomplete Qt bundle: missing {required}")

pyz = PYZ(a.pure)
icon_ico = str(root / "logo" / "AutoCF1.ico")
icon_icns = root / "packaging" / "AutoCF.icns"

if system == "windows":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=product,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        icon=icon_ico,
        version=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=product,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        target_arch=os.environ.get("AUTOCF_TARGET_ARCH") or None,
        codesign_identity=os.environ.get("AUTOCF_CODESIGN_IDENTITY") or None,
        entitlements_file=(str(root / "packaging" / "macos-entitlements.plist") if system == "darwin" else None),
    )
    collected = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=product,
    )
    if system == "darwin":
        app = BUNDLE(
            collected,
            name="AutoCF.app",
            icon=str(icon_icns) if icon_icns.is_file() else None,
            bundle_identifier="org.autocf.desktop",
            version=version,
            info_plist={
                "CFBundleDisplayName": "AutoCF",
                "CFBundleName": "AutoCF",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
                "LSMinimumSystemVersion": "12.0",
                "NSHighResolutionCapable": True,
                "NSRequiresAquaSystemAppearance": False,
            },
        )

