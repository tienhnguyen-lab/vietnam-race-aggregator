#!/bin/bash
# Wrapper called by launchd — ensures correct Python env and working directory.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/opt/anaconda3/bin/python3"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"
exec "$PYTHON" main.py sync >> "$LOG_DIR/sync.log" 2>&1
