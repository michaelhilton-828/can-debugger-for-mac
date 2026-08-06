#!/usr/bin/env bash
# Rebuild DBC Viewer from the current source and reinstall it over the
# copy in /Applications. Run this after pulling/making code changes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

APP_NAME="DBC Viewer"
INSTALLED_APP="/Applications/${APP_NAME}.app"
BUILT_APP="dist/${APP_NAME}.app"

echo "==> Syncing dependencies..."
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/pip show pyinstaller >/dev/null 2>&1 || .venv/bin/pip install --quiet pyinstaller

echo "==> Quitting running instance (if any)..."
osascript -e "tell application \"${APP_NAME}\" to quit" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
    pgrep -f "${INSTALLED_APP}/Contents/MacOS/${APP_NAME}" >/dev/null 2>&1 || break
    sleep 1
done
pkill -f "${INSTALLED_APP}/Contents/MacOS/${APP_NAME}" >/dev/null 2>&1 || true

echo "==> Building..."
.venv/bin/pyinstaller --noconfirm "${APP_NAME}.spec"

echo "==> Installing to ${INSTALLED_APP}..."
rm -rf "${INSTALLED_APP}"
cp -R "${BUILT_APP}" "${INSTALLED_APP}"

echo "==> Done. Launching updated app..."
open "${INSTALLED_APP}"
