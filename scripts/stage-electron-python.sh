#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
OUT="$ROOT/.electron-python-runtime"

if [[ ! -x "$VENV_PY" ]]; then
  echo "missing $VENV_PY; run scripts/setup-electron-backend.sh first" >&2
  exit 1
fi

REAL_PY="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$VENV_PY")"
RUNTIME="$(cd "$(dirname "$REAL_PY")/.." && pwd)"
if [[ ! -f "$RUNTIME/lib/libpython3.12.dylib" ]]; then
  echo "the selected Python is not the relocatable 3.12 runtime expected by this build" >&2
  exit 1
fi

rm -rf "$OUT"
ditto "$RUNTIME" "$OUT"
rm -rf "$OUT/lib/python3.12/site-packages"
find "$OUT" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$OUT" -type f \( -name '*.pyc' -o -name '.DS_Store' \) -delete
rm -rf "$OUT/lib/python3.12/test" "$OUT/lib/python3.12/idlelib/idle_test"
mkdir -p "$OUT/lib/python3.12/site-packages"
xattr -cr "$OUT" 2>/dev/null || true

ORIGINAL_LIB="$RUNTIME/lib/libpython3.12.dylib"
STAGED_LIB="$OUT/lib/libpython3.12.dylib"
install_name_tool -id '@rpath/libpython3.12.dylib' "$STAGED_LIB"

while IFS= read -r candidate; do
  if ! file "$candidate" | grep -q 'Mach-O'; then
    continue
  fi
  changed=false
  while IFS= read -r dependency; do
    if [[ "$dependency" == "$ORIGINAL_LIB" ]]; then
      install_name_tool -change "$dependency" \
        '@executable_path/../lib/libpython3.12.dylib' "$candidate"
      changed=true
    elif [[ "$dependency" == "$RUNTIME"/* ]]; then
      echo "unsupported absolute Python runtime dependency: $candidate -> $dependency" >&2
      exit 1
    fi
  done < <(otool -L "$candidate" | awk 'NR > 1 {print $1}')
  if [[ "$candidate" == "$STAGED_LIB" || "$changed" == true ]]; then
    codesign --force --sign - "$candidate" >/dev/null
  fi
done < <(find "$OUT" -type f)

STAGE_RUNTIME="$RUNTIME" STAGE_OUT="$OUT" python3 - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["STAGE_OUT"])
source = os.environ["STAGE_RUNTIME"].encode()
replacement = b"/opt/vivieen-python"
remaining = []
for path in root.rglob("*"):
    if not path.is_file():
        continue
    data = path.read_bytes()
    if source not in data:
        continue
    if b"\0" in data:
        remaining.append(str(path))
        continue
    path.write_bytes(data.replace(source, replacement))
if remaining:
    raise SystemExit("private runtime prefix remains in binary files: " + ", ".join(remaining))
PY

if find "$OUT" -type f -print0 | xargs -0 grep -a -l -F "$RUNTIME" | grep .; then
  echo "private runtime prefix remains in staged Python" >&2
  exit 1
fi

"$OUT/bin/python" -B -c 'import ssl, sys; assert sys.version_info[:2] == (3, 12)'
echo "staged portable Python runtime"
