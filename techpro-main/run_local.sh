#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 scripts/build_web_data.py --skip-collect
echo "Open http://localhost:8000"
python3 -m http.server 8000 --directory web
