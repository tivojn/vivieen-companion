#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/electron/native/enconvo_audio_tap.swift"
OUTPUT_DIR="$ROOT/.electron-native"
BINARY="$OUTPUT_DIR/enconvo-audio-tap"
LEGACY_APP="$OUTPUT_DIR/VivieenAudioTap.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Skipping Core Audio tap build outside macOS."
  exit 0
fi

mkdir -p "$OUTPUT_DIR"
if [[ -d "$LEGACY_APP" ]]; then
  /bin/rm -rf "$LEGACY_APP"
fi

/usr/bin/xcrun swiftc \
  "$SOURCE" \
  -O \
  -parse-as-library \
  -target arm64-apple-macosx14.2 \
  -framework AppKit \
  -framework AudioToolbox \
  -framework AVFAudio \
  -o "$BINARY"
/bin/chmod 755 "$BINARY"
/usr/bin/codesign --force --sign - --identifier com.vivieen.companion "$BINARY" >/dev/null

if [[ "${1:-}" == "--check" ]]; then
  OUTPUT="$($BINARY --self-test)"
  [[ "$OUTPUT" == *'"ok":true'* ]]
fi

echo "$BINARY"
