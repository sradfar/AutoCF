#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${AUTOCF_VERSION:-1.0.0}"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64) RELEASE_ARCH="arm64" ;;
  x86_64) RELEASE_ARCH="x64" ;;
  *) echo "Unsupported macOS architecture: $ARCH" >&2; exit 2 ;;
esac

cd "$ROOT"
python3 -m pip install --upgrade pip
python3 -m pip install -r packaging/requirements-build.txt

# Build a proper .icns from the existing AutoCF artwork.
ICONSET="$ROOT/packaging/AutoCF.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" logo/AutoCF1.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" logo/AutoCF1.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o packaging/AutoCF.icns

rm -rf build dist
export AUTOCF_VERSION="$VERSION"
export AUTOCF_TARGET_ARCH="$ARCH"
python3 -m PyInstaller --noconfirm --clean packaging/autocf_desktop.spec

APP="$ROOT/dist/AutoCF.app"
test -d "$APP"
find "$APP" -name 'QtWebEngineProcess*' -print -quit | grep -q .
find "$APP" -iname '*QtWebEngineCore*' -print -quit | grep -q .
test -d "$APP/Contents/Resources/autocf/app"
test -d "$APP/Contents/Resources/autocf/windows"

# PyInstaller signs collected Mach-O files. This final pass signs the bundle.
# Use '-' for a test build or a Developer ID Application identity for release.
IDENTITY="${AUTOCF_CODESIGN_IDENTITY:--}"
if [[ "$IDENTITY" == "-" ]]; then
  codesign --force --deep --sign - --entitlements packaging/macos-entitlements.plist "$APP"
else
  codesign --force --deep --options runtime --timestamp --sign "$IDENTITY" \
    --entitlements packaging/macos-entitlements.plist "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"

DMG="$ROOT/dist/AutoCF-${VERSION}-macOS-${RELEASE_ARCH}.dmg"
rm -f "$DMG"
hdiutil create -quiet -volname "AutoCF ${VERSION}" -srcfolder "$APP" -ov -format UDZO "$DMG"

# A release can be notarized non-interactively by GitHub Actions after an owner
# stores an Apple notary keychain profile on the runner.
if [[ -n "${APPLE_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$APPLE_NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  hdiutil create -quiet -volname "AutoCF ${VERSION}" -srcfolder "$APP" -ov -format UDZO "$DMG"
fi

shasum -a 256 "$DMG" > "$DMG.sha256"
echo "$DMG"

