#!/usr/bin/env bash
# Run the whole test suite:  tests/run_tests.sh
#   PYTHON=/path/to/python tests/run_tests.sh   to use a specific interpreter (needs Pillow)
#   SKIP_PARITY=1 tests/run_tests.sh            to skip the JavaScript comparison (needs node)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; else PYTHON=python3; fi
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

step() { printf '\n== %s\n' "$*"; }

step "Generating test books"
"$PYTHON" "$HERE/make_test_books.py" "$TMP/books" >/dev/null

# The generated set includes one deliberate non-EPUB, so a good run exits 2 and reports exactly that.
convert() {
  local out="$1"; shift
  set +e
  "$PYTHON" "$ROOT/epub_optimize.py" "$TMP/books" -o "$TMP/$out" -j 2 "$@" >"$TMP/$out.log" 2>&1
  local rc=$?
  set -e
  if [[ $rc -ne 2 ]] || ! grep -q "FAILED not an epub.epub" "$TMP/$out.log" \
     || [[ $(grep -c "FAILED" "$TMP/$out.log") -ne 1 ]] \
     || ! grep -q "kept broken.png unchanged" "$TMP/$out.log"; then
    cat "$TMP/$out.log"; echo "unexpected converter result (exit $rc) for: $*"; exit 1
  fi
  echo "converted ($out): $(grep '^Done:' "$TMP/$out.log")"
}

step "Converting"
convert x3
convert x4 -d x4
convert x3cache --crosspoint-cache
convert x4cache -d x4 --crosspoint-cache
convert custom --size 600x900 --true-grayscale --suffix _opt

step "Re-running skips finished books"
set +e
"$PYTHON" "$ROOT/epub_optimize.py" "$TMP/books" -o "$TMP/x3" -j 2 >"$TMP/rerun.log" 2>&1
set -e
grep -q "Skipping 2 book(s)" "$TMP/rerun.log" || { cat "$TMP/rerun.log"; echo "rerun did not skip"; exit 1; }
echo ok

step "Verifying output structure"
"$PYTHON" "$HERE/verify_epub.py" --max 528x792 "$TMP"/x3/*.epub "$TMP"/x3cache/*.epub
"$PYTHON" "$HERE/verify_epub.py" --max 480x800 "$TMP"/x4/*.epub "$TMP"/x4cache/*.epub
"$PYTHON" "$HERE/verify_epub.py" --max 600x900 "$TMP"/custom/*_opt.epub

step "Checking images fill the X3 width"
"$PYTHON" - "$TMP" <<'EOF'
import io, sys, zipfile
from PIL import Image
z = zipfile.ZipFile(sys.argv[1] + "/x3/Book B (EPUB2).epub")
w = Image.open(io.BytesIO(z.read("images/map.jpg"))).size
assert w == (528, 704), w
z = zipfile.ZipFile(sys.argv[1] + "/x4/Book B (EPUB2).epub")
w = Image.open(io.BytesIO(z.read("images/map.jpg"))).size
assert w == (480, 640), w
print("ok")
EOF

if [[ -n "${SKIP_PARITY:-}" ]]; then
  echo; echo "Skipping JavaScript parity (SKIP_PARITY set)"
elif ! command -v node >/dev/null; then
  echo; echo "Skipping JavaScript parity (node not installed)"
else
  step "Comparing CrossPoint cache with the site's JavaScript"
  [[ -d "$HERE/ref/node_modules" ]] || (cd "$HERE/ref" && npm install --silent --no-audit --no-fund)
  "$PYTHON" "$HERE/parity.py" "$TMP/books" "$TMP/x4cache"
fi

printf '\nAll tests passed.\n'
