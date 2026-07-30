#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/electron/native/key_tap.swift"
OUTPUT_DIR="$ROOT/.electron-native"
BINARY="$OUTPUT_DIR/key-tap"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Skipping key tap build outside macOS."
  exit 0
fi

if [[ "${1:-}" == "--check" ]]; then
  if [[ -x "$BINARY" && "$BINARY" -nt "$SOURCE" ]]; then
    echo "key-tap binary is current."
    exit 0
  fi
  echo "key-tap binary is missing or stale; run npm run build:key-tap." >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
/usr/bin/xcrun swiftc \
  "$SOURCE" \
  -O \
  -parse-as-library \
  -target arm64-apple-macosx14.2 \
  -framework AppKit \
  -o "$BINARY"
/bin/chmod 755 "$BINARY"
/usr/bin/codesign --force --sign - --identifier com.vivieen.companion "$BINARY" >/dev/null
echo "Built $BINARY"
