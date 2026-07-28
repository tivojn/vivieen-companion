#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/electron/native/person_cutout.swift"
OUTPUT_DIR="$ROOT/.electron-native"
BINARY="$OUTPUT_DIR/person-cutout"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Skipping Vision cutout build outside macOS."
  exit 0
fi

mkdir -p "$OUTPUT_DIR"
/usr/bin/xcrun swiftc \
  "$SOURCE" \
  -O \
  -target arm64-apple-macosx14.0 \
  -framework AppKit \
  -framework CoreImage \
  -framework Vision \
  -o "$BINARY"
/bin/chmod 755 "$BINARY"
/usr/bin/codesign --force --sign - --identifier com.vivieen.companion.cutout "$BINARY" >/dev/null

echo "$BINARY"
