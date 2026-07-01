#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="清华云盘自助备份"

cd "$PROJECT_ROOT"

python3 -m pip install --upgrade pyinstaller

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  --distpath "$PROJECT_ROOT/dist" \
  --workpath "$PROJECT_ROOT/build" \
  --specpath "$PROJECT_ROOT/build" \
  --paths "$PROJECT_ROOT/src" \
  --hidden-import tkinter \
  --hidden-import tkinter.ttk \
  "$PROJECT_ROOT/scripts/pyinstaller_entry.py"

echo ""
echo "macOS app:"
echo "$PROJECT_ROOT/dist/$APP_NAME.app"
