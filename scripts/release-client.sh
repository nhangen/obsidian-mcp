#!/usr/bin/env bash
set -euo pipefail

# Pack the portable client bundle and install it locally.
# Usage: npm run release:client
# This keeps the locally-installed client in sync with the current catalog/server version.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/obsidian-mcp-client.XXXXXX")
trap 'rm -rf "${OUTPUT_DIR}"' EXIT

echo "==> Packing client bundle to ${OUTPUT_DIR}"
PACK_OUTPUT=$(python3 "${REPO_ROOT}/scripts/pack-obsidian-mcp-client.py" --output "${OUTPUT_DIR}")
echo "${PACK_OUTPUT}"

# The pack script prints the sha256 on the last line
RELEASE_SHA256=$(echo "${PACK_OUTPUT}" | grep -oE '[0-9a-f]{64}' | tail -1)
if [ -z "${RELEASE_SHA256}" ]; then
  echo "ERROR: could not extract release sha256 from pack output" >&2
  exit 1
fi

echo "==> Installing client (release-sha256: ${RELEASE_SHA256})"
python3 "${OUTPUT_DIR}/bin/obsidian-mcp-client-install" \
  --bundle "${OUTPUT_DIR}" \
  --release-sha256 "${RELEASE_SHA256}"

# Verify installed manifest
INSTALLED_MANIFEST="$HOME/.local/share/obsidian-mcp-client/current/manifest.json"
echo "==> Installed manifest pinnedServer:"
python3 -c "import json; m=json.load(open('${INSTALLED_MANIFEST}')); print(json.dumps(m.get('pinnedServer',{}), indent=2))"

echo "==> Done."
