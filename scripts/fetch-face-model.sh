#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_DIR="$ROOT/models"
STAGE_DIR="$ROOT/.electron-models"
MODEL="$MODEL_DIR/face_landmarker.task"
STAGED="$STAGE_DIR/face_landmarker.task"
URL="https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
EXPECTED="64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"

checksum() {
  shasum -a 256 "$1" | awk '{print $1}'
}

mkdir -p "$MODEL_DIR" "$STAGE_DIR"
if [[ ! -f "$MODEL" || "$(checksum "$MODEL")" != "$EXPECTED" ]]; then
  TEMP="$(mktemp "${TMPDIR:-/tmp}/vivieen-face-model.XXXXXX")"
  trap 'rm -f "$TEMP"' EXIT
  curl --fail --location --silent --show-error "$URL" --output "$TEMP"
  ACTUAL="$(checksum "$TEMP")"
  if [[ "$ACTUAL" != "$EXPECTED" ]]; then
    echo "face model checksum mismatch: expected $EXPECTED, got $ACTUAL" >&2
    exit 1
  fi
  mv "$TEMP" "$MODEL"
  trap - EXIT
fi
cp "$MODEL" "$STAGED"
LICENSE_SOURCE="$(find "$ROOT/.venv/lib/python3.12/site-packages" -path '*/mediapipe-*.dist-info/licenses/LICENSE' -type f -print -quit 2>/dev/null || true)"
if [[ -n "$LICENSE_SOURCE" ]]; then
  cp "$LICENSE_SOURCE" "$STAGE_DIR/LICENSE.Apache-2.0.txt"
else
  echo "MediaPipe license file is missing; run backend setup before packaging" >&2
  exit 1
fi
printf 'Face model verified: %s\n' "$EXPECTED"
