#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${AUTOCF_VERSION:-1.0.0}"
ARCH="$(uname -m)"
[[ "$ARCH" == "x86_64" ]] || { echo "Linux x64 build requires x86_64" >&2; exit 2; }

cd "$ROOT"
python3 -m pip install --upgrade pip
python3 -m pip install -r packaging/requirements-build.txt
rm -rf build dist
export AUTOCF_VERSION="$VERSION"
python3 -m PyInstaller --noconfirm --clean packaging/autocf_desktop.spec

BUNDLE="$ROOT/dist/AutoCF-Desktop"
test -x "$BUNDLE/AutoCF-Desktop"
find "$BUNDLE" -name 'QtWebEngineProcess*' -print -quit | grep -q .
find "$BUNDLE" -iname '*QtWebEngineCore*' -print -quit | grep -q .
test -d "$BUNDLE/_internal/autocf/app"
test -d "$BUNDLE/_internal/autocf/windows"

OUT_DIR="$ROOT/dist/AutoCF-${VERSION}-Linux-x64"
rm -rf "$OUT_DIR"
mv "$BUNDLE" "$OUT_DIR"

# Passing means the GUI remained alive long enough to enter its event loop.
set +e
QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 timeout 20s "$OUT_DIR/AutoCF-Desktop"
STATUS=$?
set -e
if [[ $STATUS -ne 0 && $STATUS -ne 124 ]]; then
  echo "AutoCF Linux launch smoke test failed: exit $STATUS" >&2
  exit "$STATUS"
fi

tar -C "$ROOT/dist" -czf "$ROOT/dist/AutoCF-${VERSION}-Linux-x64.tar.gz" "AutoCF-${VERSION}-Linux-x64"
sha256sum "$ROOT/dist/AutoCF-${VERSION}-Linux-x64.tar.gz" > "$ROOT/dist/AutoCF-${VERSION}-Linux-x64.tar.gz.sha256"

