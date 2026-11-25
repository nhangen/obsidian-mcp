#!/usr/bin/env bash
# TEST-TYPE: regression
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)" || exit 1
test -n "$tmp" && test -d "$tmp" || exit 1
trap 'rm -rf "$tmp"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

mkdir -p "$tmp/repo/scripts" "$tmp/home" "$tmp/work"
cp "$ROOT/scripts/release-client.sh" "$tmp/repo/scripts/"
cat > "$tmp/repo/scripts/pack-obsidian-mcp-client.py" <<'PY'
import json, os, sys
from pathlib import Path
assert sys.argv[1] == "--output" and len(sys.argv) == 3, sys.argv
output = Path(sys.argv[2])
if output.exists():
    sys.exit("output exists but has incorrect directory permissions")
(output / "bin").mkdir(parents=True)
pinned = {} if os.environ.get("FAKE_NO_PIN") == "1" else {"pinnedServer": {"release": "mcp-v9.9.9"}}
(output / "manifest.json").write_text(json.dumps(pinned))
(output / "bin/obsidian-mcp-client-install").write_text('''
import json, os, sys
from pathlib import Path
assert sys.argv[1::2] == ["--bundle", "--release-sha256"], sys.argv
assert sys.argv[4] == "a" * 64, sys.argv
current = Path(os.environ["HOME"]) / ".local/share/obsidian-mcp-client/current"
current.mkdir(parents=True, exist_ok=True)
release = os.environ.get("FAKE_INSTALLED_RELEASE", "mcp-v9.9.9")
(current / "manifest.json").write_text(json.dumps({"pinnedServer": {"release": release}} if release else {}))
''')
print("unrelated digest " + "b" * 64)
if os.environ.get("FAKE_NO_SHA") != "1":
    print("obsidian-mcp-client: release.json sha256 " + "a" * 64)
    print("unrelated digest " + "c" * 64)
PY

run() { env HOME="$tmp/home" TMPDIR="$tmp/work" "$@" bash "$tmp/repo/scripts/release-client.sh"; }
no_leftovers() { [ -z "$(ls -A "$tmp/work")" ] || fail "$1 left a temp directory"; }

run FAKE_UNUSED=1 > "$tmp/out" 2>&1 || fail "matching install should succeed: $(cat "$tmp/out")"
no_leftovers "success"

for installed in mcp-v0.0.1 ""; do
  if run FAKE_INSTALLED_RELEASE="$installed" > "$tmp/out" 2>&1; then
    fail "installed pinnedServer '$installed' should fail verification"
  fi
  grep -q 'does not match' "$tmp/out" || fail "mismatch reason missing: $(cat "$tmp/out")"
  no_leftovers "mismatch"
done

if run FAKE_NO_PIN=1 FAKE_INSTALLED_RELEASE= > "$tmp/out" 2>&1; then
  fail "a bundle without pinnedServer should fail verification"
fi
grep -q 'does not match' "$tmp/out" || fail "missing-pin reason missing: $(cat "$tmp/out")"
no_leftovers "missing pin"

if run FAKE_NO_SHA=1 > "$tmp/out" 2>&1; then fail "missing sha line should fail"; fi
grep -q 'could not extract release sha256' "$tmp/out" || fail "sha reason missing: $(cat "$tmp/out")"
no_leftovers "missing sha"

echo "PASS"
