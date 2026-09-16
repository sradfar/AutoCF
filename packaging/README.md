# AutoCF 1.0.0 release builds

This directory produces six distinct release artifacts from the tested
`edition2` source. Copying the Windows directory is not considered a build.
Every workflow job must create, smoke-test, size-check, and hash its artifact.

## Native deliverables

- Windows: `AutoCF-1.0.0-Windows-x64.zip`
- macOS Apple Silicon: `AutoCF-1.0.0-macOS-arm64.dmg`
- macOS Intel: `AutoCF-1.0.0-macOS-x64.dmg`
- Linux: `AutoCF-1.0.0-Linux-x64.tar.gz`
- Docker: `ghcr.io/OWNER/autocf-runtime:1.0.0`
- Offline Docker image: `AutoCF-Runtime-1.0.0-amd64.tar.zst`

## Why the Qt builds differ

PySide6 includes platform-native Qt binaries. A Windows `.exe` cannot be
converted into a Mac or Linux application. The GitHub matrix installs PySide6
on each target OS and PyInstaller collects that OS's Qt libraries, plugins,
WebEngine helper, translations, and Chromium resource files.

The macOS build is a true `AutoCF.app` bundle inside a DMG. It verifies the
QtWebEngine process/framework, applies WebEngine JIT entitlements, signs the
bundle, and optionally notarizes it when Apple credentials are configured.
The Linux build is a self-contained onedir Qt bundle and is launched under an
offscreen display as a smoke test. Docker is headless and intentionally does
not contain Qt.

## One-time GitHub bootstrap

The two managed root files are too large for normal Git objects. After creating
the GitHub repository and authenticating `gh`, upload them once as a release
asset:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/publish_runtime_assets.ps1 `
  -Repository OWNER/REPOSITORY
```

The release workflow downloads and validates that pinned payload. GitHub
release assets allow individual files up to 2 GiB, so the runtime archive is
within the supported limit.

## Apple signing

Unsigned/ad-hoc DMGs are suitable for tester builds. A normal public Mac
release needs repository secrets for a Developer ID Application certificate
and Apple notarization credentials. The publisher signs once in CI; end users
do not sign anything and do not need the publisher's computer or data.

