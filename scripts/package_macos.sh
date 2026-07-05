#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="清华云盘自助备份"

cd "$PROJECT_ROOT"

python3 -m pip install --upgrade pyinstaller
python3 -m pip install -e "$PROJECT_ROOT"

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  --distpath "$PROJECT_ROOT/dist" \
  --workpath "$PROJECT_ROOT/build" \
  --specpath "$PROJECT_ROOT/build" \
  --paths "$PROJECT_ROOT/src" \
  --hidden-import tsinghua_cloud_backup.app \
  --hidden-import tsinghua_cloud_backup.web_console \
  "$PROJECT_ROOT/scripts/pyinstaller_entry.py"

ZIP_PATH="$PROJECT_ROOT/dist/$APP_NAME-macos.zip"
rm -f "$ZIP_PATH"
ditto -c -k --sequesterRsrc --keepParent "$PROJECT_ROOT/dist/$APP_NAME.app" "$ZIP_PATH"

echo ""
echo "macOS app:"
echo "$PROJECT_ROOT/dist/$APP_NAME.app"
echo ""
echo "macOS zip package:"
echo "$ZIP_PATH"
