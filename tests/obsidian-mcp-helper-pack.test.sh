#!/usr/bin/env bash
# TEST-TYPE: regression
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tests/obsidian-mcp-helper-pack.test.py" || exit 1
