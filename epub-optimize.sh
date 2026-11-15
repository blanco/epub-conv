#!/usr/bin/env bash
# epub-optimize.sh — convert every EPUB in a folder for a 480x800 e-ink reader.
#
#   ./epub-optimize.sh ~/Books                      -> ~/Books/optimized/
#   ./epub-optimize.sh ~/Books --crosspoint-cache   -> also pre-build the CrossPoint cache
#   ./epub-optimize.sh --help                       -> all options
#
# First run creates a private Python environment (.venv next to this script)
# and installs Pillow into it; nothing is installed system-wide.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
PY="$VENV/bin/python"

if [[ $# -eq 0 ]]; then
  echo "usage: $(basename "$0") <folder-of-epubs> [options]   (--help for options)"
  exit 1
fi

if [[ ! -x "$PY" ]] || ! "$PY" -c "import PIL" 2>/dev/null; then
  command -v python3 >/dev/null || { echo "python3 not found. Install it with: xcode-select --install  (or brew install python)"; exit 1; }
  echo "First run: setting up $VENV with Pillow…"
  python3 -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet Pillow
fi

if [[ -z "${MOZJPEG_CJPEG:-}" ]] && [[ ! -x /opt/homebrew/opt/mozjpeg/bin/cjpeg ]] && [[ ! -x /usr/local/opt/mozjpeg/bin/cjpeg ]]; then
  echo "Tip: 'brew install mozjpeg' makes the images ~15% smaller (same quality). Using Pillow for now."
fi

exec "$PY" "$DIR/epub_optimize.py" "$@"
