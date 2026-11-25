#!/usr/bin/env bash
set -euo pipefail

# Usage: npm run release:client
# Installs the server release pinned in the catalog, not whatever server is running.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/obsidian-mcp-client.XXXXXX")
trap 'rm -rf "${WORK_DIR}"' EXIT
# The packer refuses a pre-created directory with mktemp's mode, so let it create this one.
OUTPUT_DIR="${WORK_DIR}/bundle"

echo "==> Packing client bundle to ${OUTPUT_DIR}"
PACK_OUTPUT=$(python3 "${REPO_ROOT}/scripts/pack-obsidian-mcp-client.py" --output "${OUTPUT_DIR}")
echo "${PACK_OUTPUT}"

RELEASE_SHA256=$(echo "${PACK_OUTPUT}" | sed -n 's/^obsidian-mcp-client: release\.json sha256 \([0-9a-f]\{64\}\)$/\1/p')
if [ -z "${RELEASE_SHA256}" ]; then
  echo "ERROR: could not extract release sha256 from pack output" >&2
  exit 1
fi

echo "==> Installing client (release-sha256: ${RELEASE_SHA256})"
python3 "${OUTPUT_DIR}/bin/obsidian-mcp-client-install" \
  --bundle "${OUTPUT_DIR}" \
  --release-sha256 "${RELEASE_SHA256}"

INSTALLED_MANIFEST="$HOME/.local/share/obsidian-mcp-client/current/manifest.json"
echo "==> Verifying installed pinnedServer"
python3 - "${OUTPUT_DIR}/manifest.json" "${INSTALLED_MANIFEST}" <<'PY'
import json
import sys

packed, installed = (json.load(open(path)).get("pinnedServer") for path in sys.argv[1:])
if not packed or packed != installed:
    sys.exit(f"ERROR: installed pinnedServer {installed} does not match packed {packed}")
print(json.dumps(installed, indent=2))
PY

echo "==> Done."
