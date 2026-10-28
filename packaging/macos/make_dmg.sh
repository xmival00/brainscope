#!/usr/bin/env bash
# Wraps dist/brainscope.app into dist/brainscope-<version>-macos-<arch>.dmg
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(python -c 'import brainscope; print(brainscope.__version__)')"
DMG="$ROOT/dist/brainscope-${VERSION}-macos-$(uname -m).dmg"
STAGE="$ROOT/build/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$ROOT/dist/brainscope.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "brainscope ${VERSION}" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo "built $DMG"
