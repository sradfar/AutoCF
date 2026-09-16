#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${AUTOCF_VERSION:-1.0.0-rc3}"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64) RELEASE_ARCH="arm64" ;;
  x86_64) RELEASE_ARCH="x64" ;;
  *) echo "Unsupported macOS architecture: $ARCH" >&2; exit 2 ;;
esac

cd "$ROOT"
[[ "$(python3 -c 'import sys; print(sys.implementation.cache_tag)')" == "cpython-311" ]] || {
  echo "Sanitized macOS RC3 requires Python 3.11" >&2
  exit 2
}
python3 -m pip install --upgrade pip
python3 -m pip install -r packaging/requirements-build.txt

WORK="$ROOT/.codex-build-macos-rc3/$RELEASE_ARCH"
PAYLOAD="$WORK/payload"
PYI_WORK="$WORK/pyinstaller-work"
PYI_DIST="$WORK/pyinstaller-dist"
OUTPUT_ROOT="$ROOT/dist-macos-rc3"
DMG="$OUTPUT_ROOT/AutoCF-${VERSION}-macOS-${RELEASE_ARCH}.dmg"
[[ ! -e "$WORK" && ! -e "$DMG" && ! -e "$DMG.sha256" ]] || {
  echo "Refusing to overwrite an existing macOS RC3 output" >&2
  exit 2
}
mkdir -p "$WORK" "$OUTPUT_ROOT"

python3 packaging/prepare_macos_sanitized.py \
  --root "$ROOT" --destination "$PAYLOAD" --version "$VERSION" \
  --architecture "$RELEASE_ARCH"
python3 packaging/verify_macos_sanitized.py "$PAYLOAD"

ICONSET="$WORK/AutoCF.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" logo/AutoCF1.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" logo/AutoCF1.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o packaging/AutoCF.icns

export AUTOCF_VERSION="$VERSION"
export AUTOCF_TARGET_ARCH="$ARCH"
export AUTOCF_PAYLOAD_ROOT="$PAYLOAD"
python3 -m PyInstaller --noconfirm --clean \
  --workpath "$PYI_WORK" --distpath "$PYI_DIST" packaging/autocf_desktop.spec

APP="$PYI_DIST/AutoCF.app"
test -d "$APP"
find "$APP" -name 'QtWebEngineProcess*' -print -quit | grep -q .
find "$APP" -iname '*QtWebEngineCore*' -print -quit | grep -q .
python3 packaging/verify_macos_sanitized.py "$APP"

IDENTITY="${AUTOCF_CODESIGN_IDENTITY:--}"
if [[ "$IDENTITY" == "-" ]]; then
  codesign --force --deep --sign - --entitlements packaging/macos-entitlements.plist "$APP"
else
  codesign --force --deep --options runtime --timestamp --sign "$IDENTITY" \
    --entitlements packaging/macos-entitlements.plist "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"

hdiutil create -quiet -volname "AutoCF ${VERSION}" -srcfolder "$APP" -ov -format UDZO "$DMG"
if [[ -n "${APPLE_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$APPLE_NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  hdiutil create -quiet -volname "AutoCF ${VERSION}" -srcfolder "$APP" -ov -format UDZO "$DMG"
fi

shasum -a 256 "$DMG" > "$DMG.sha256"
python3 packaging/verify_release.py "$DMG" "AutoCF-${VERSION}-macOS-${RELEASE_ARCH}"
echo "$DMG"
