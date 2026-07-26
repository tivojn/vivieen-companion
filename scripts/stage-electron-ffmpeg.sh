#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="7.1.5"
EXPECTED="de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f"
CACHE="${VIVIEEN_BUILD_CACHE:-$HOME/Library/Caches/vivieen-build}"
ARCHIVE="$CACHE/ffmpeg-$VERSION.tar.xz"
URL="https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz"
OUT_DIR="$ROOT/.electron-ffmpeg"
OUT="$OUT_DIR/ffmpeg"

checksum() {
  shasum -a 256 "$1" | awk '{print $1}'
}

if [[ -x "$OUT" && -s "$OUT_DIR/LICENSE.LGPLv2.1.txt" ]]; then
  LICENSE_OUTPUT="$("$OUT" -L 2>&1)"
  if "$OUT" -version 2>&1 | grep -F "ffmpeg version $VERSION" >/dev/null \
     && [[ $LICENSE_OUTPUT =~ GNU[[:space:]]+Lesser[[:space:]]+General[[:space:]]+Public[[:space:]]+License ]] \
     && "$OUT" -decoders 2>/dev/null | grep -E '^[[:space:]]*V[.A-Z]+[[:space:]]+png[[:space:]]' >/dev/null; then
    echo "staged LGPL FFmpeg $VERSION already verified"
    exit 0
  fi
fi

mkdir -p "$CACHE" "$OUT_DIR"
if [[ ! -f "$ARCHIVE" || "$(checksum "$ARCHIVE")" != "$EXPECTED" ]]; then
  TEMP="$(mktemp "${TMPDIR:-/tmp}/vivieen-ffmpeg.XXXXXX")"
  trap 'rm -f "$TEMP"' EXIT
  curl --fail --location --silent --show-error "$URL" --output "$TEMP"
  ACTUAL="$(checksum "$TEMP")"
  if [[ "$ACTUAL" != "$EXPECTED" ]]; then
    echo "FFmpeg source checksum mismatch: expected $EXPECTED, got $ACTUAL" >&2
    exit 1
  fi
  mv "$TEMP" "$ARCHIVE"
  trap - EXIT
fi

BUILD="$(mktemp -d "${TMPDIR:-/tmp}/vivieen-ffmpeg-build.XXXXXX")"
trap 'rm -rf "$BUILD"' EXIT
tar -xf "$ARCHIVE" -C "$BUILD"
cd "$BUILD/ffmpeg-$VERSION"

./configure \
  --prefix="$BUILD/install" \
  --arch=arm64 \
  --cc=/usr/bin/clang \
  --disable-autodetect \
  --disable-debug \
  --disable-doc \
  --disable-network \
  --disable-everything \
  --disable-ffplay \
  --disable-ffprobe \
  --enable-ffmpeg \
  --enable-avcodec \
  --enable-avfilter \
  --enable-avformat \
  --enable-swresample \
  --enable-swscale \
  --enable-videotoolbox \
  --enable-zlib \
  --enable-protocol=file \
  --enable-demuxer=aiff,flac,image2,matroska,mov,mp3,ogg,wav \
  --enable-muxer=mp4,wav \
  --enable-decoder=aac,alac,flac,mjpeg,mp3,opus,pcm_f32be,pcm_f32le,pcm_f64be,pcm_f64le,pcm_s8,pcm_s16be,pcm_s16le,pcm_s24be,pcm_s24le,pcm_s32be,pcm_s32le,pcm_u8,png,vorbis \
  --enable-encoder=h264_videotoolbox,pcm_s16le \
  --enable-parser=aac,h264,mpegaudio,opus,png,vorbis \
  --enable-filter=aresample,format,scale \
  --extra-cflags=-mmacosx-version-min=12.0 \
  --extra-ldflags=-mmacosx-version-min=12.0

make -j"$(sysctl -n hw.logicalcpu)"
make install
cp "$BUILD/install/bin/ffmpeg" "$OUT"
cp "$BUILD/ffmpeg-$VERSION/COPYING.LGPLv2.1" "$OUT_DIR/LICENSE.LGPLv2.1.txt"
strip -x "$OUT"
chmod 755 "$OUT"
xattr -cr "$OUT" 2>/dev/null || true

LICENSE_OUTPUT="$("$OUT" -L 2>&1)"
if [[ ! $LICENSE_OUTPUT =~ GNU[[:space:]]+Lesser[[:space:]]+General[[:space:]]+Public[[:space:]]+License ]]; then
  echo "refusing to stage an FFmpeg binary that is not LGPL" >&2
  exit 1
fi
if ! "$OUT" -decoders 2>/dev/null | grep -E '^[[:space:]]*V[.A-Z]+[[:space:]]+png[[:space:]]' >/dev/null; then
  echo "refusing to stage FFmpeg without PNG decoding" >&2
  exit 1
fi
if otool -L "$OUT" | grep -Eq '/opt/homebrew|/usr/local'; then
  echo "refusing to stage FFmpeg with non-system library dependencies" >&2
  exit 1
fi
printf 'Staged minimal LGPL FFmpeg %s (%s)\n' "$VERSION" "$(stat -f '%z bytes' "$OUT")"
