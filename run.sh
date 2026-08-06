#!/usr/bin/env bash
# Launch DBC Viewer using its dedicated virtualenv.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec .venv/bin/python main.py "$@"
