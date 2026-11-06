#!/usr/bin/env python3
"""Copy the allowlisted portable Obsidian MCP client bundle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packages/client"
CATALOG = ROOT / "packages/client/catalog.json"
CONTRACT = ROOT / "packages/client/contract.json"
EXIT_FAILURE = 64
FILES = (
    "README.md",
    "manifest.json",
    "bin/obsidian-mcp-read",
    "bin/obsidian-mcp-write",
    "bin/obsidian-mcp-commit-event",
    "bin/obsidian-mcp-capture-commit",
    "bin/obsidian-mcp-client-install",
    "bin/obsidian-mcp-native",
    "bin/obsidian-mcp-managed-export",
    "hosts/codex-capture.md",
    "lib/launch-profile.sh",
    "lib/verify-read-bridge.py",
    "examples/profiles.env.example",
    "vendor/shell-capture-parser/BUILD-PROVENANCE.json",
    "vendor/shell-capture-parser/LICENSE-go-runtime.txt",
    "vendor/shell-capture-parser/LICENSE-mvdan-sh.txt",
    "vendor/shell-capture-parser/shell-capture-parser-darwin-amd64",
    "vendor/shell-capture-parser/shell-capture-parser-darwin-arm64",
    "vendor/shell-capture-parser/shell-capture-parser-linux-amd64",
    "vendor/shell-capture-parser/shell-capture-parser-linux-arm64",
)
EXECUTABLES = {
    "bin/obsidian-mcp-read",
    "bin/obsidian-mcp-write",
    "bin/obsidian-mcp-commit-event",
    "bin/obsidian-mcp-capture-commit",
    "bin/obsidian-mcp-client-install",
    "bin/obsidian-mcp-native",
    "bin/obsidian-mcp-managed-export",
    "lib/launch-profile.sh",
    "vendor/mcp-remote-read/xdg-open",
    "vendor/mcp-remote-read/mcp-remote-read.mjs",
    "vendor/shell-capture-parser/shell-capture-parser-darwin-amd64",
    "vendor/shell-capture-parser/shell-capture-parser-darwin-arm64",
    "vendor/shell-capture-parser/shell-capture-parser-linux-amd64",
    "vendor/shell-capture-parser/shell-capture-parser-linux-arm64",
}
HELPER_VENDOR = "vendor/shell-capture-parser"
HELPER_SOURCE = ROOT / "helpers/shell-capture-parser"
HELPER_PROVENANCE = f"{HELPER_VENDOR}/BUILD-PROVENANCE.json"
HELPER_MODULE = "mvdan.cc/sh/v3"
HELPER_MODULE_VERSION = "v3.11.0"
HELPER_SOURCE_FILES = ("main.go", "main_test.go", "go.mod", "go.sum")
HELPER_PLATFORMS = (
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("linux", "amd64"),
    ("linux", "arm64"),
)
MACHO_MAGIC = b"\xcf\xfa\xed\xfe"
ELF_MAGIC = b"\x7fELF"


def fail(message: str) -> None:
    raise ValueError(message)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        fail(message)


def regular_file_bytes(path: Path, label: str) -> bytes:
    try:
        item = path.lstat()
    except OSError as error:
        fail(f"cannot inspect {label}: {error.strerror}")
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        fail(f"{label} is not a private regular file")
    return path.read_bytes()


def server_reference(catalog: dict, contract_bytes: bytes) -> dict[str, str]:
    contract = json.loads(contract_bytes)
    contract_pin = catalog.get("contract", {})
    release = catalog.get("release", {})
    actual_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    if contract_pin.get("sha256") != actual_sha256:
        fail("catalog contract hash does not match contract bytes")
    if contract_pin.get("contractVersion") != contract.get("contractVersion"):
        fail("catalog contract version does not match contract")
    if release.get("sourceCommit") != contract_pin.get("commit"):
        fail("catalog release source commit does not match contract pin")
    if not isinstance(release.get("tag"), str) or not release["tag"]:
        fail("catalog release tag is missing")
    return {
        "release": release["tag"],
        "contractVersion": contract_pin["contractVersion"],
        "contractSha256": actual_sha256,
        "sourceCommit": contract_pin["commit"],
    }


def validate_helper_vendor(files: dict[str, bytes]) -> dict:
    """Validate vendored shell-capture-parser artifacts against live helper
    sources before packing, rejecting stale sources, missing artifacts,
    symlinks (by refusal of non-regular-nlink-1 bytes), and mismatches."""
    if HELPER_PROVENANCE not in files:
        fail(f"bundle is missing {HELPER_PROVENANCE}")
    provenance = json.loads(files[HELPER_PROVENANCE])
    if provenance.get("formatVersion") != 1:
        fail("helper provenance formatVersion is missing")
    if provenance.get("helper") != "shell-capture-parser":
        fail("helper provenance names a different helper")
    module = provenance.get("module", {})
    if (
        module.get("name") != HELPER_MODULE
        or module.get("version") != HELPER_MODULE_VERSION
    ):
        fail(
            f"helper provenance does not pin {HELPER_MODULE} "
            f"{HELPER_MODULE_VERSION}"
        )
    source = provenance.get("source")
    if not isinstance(source, dict) or set(source) != set(HELPER_SOURCE_FILES):
        fail("helper provenance does not carry the pinned helper sources")
    for relative, expected in sorted(source.items()):
        helper_file = HELPER_SOURCE / relative
        for parent in helper_file.absolute().parents:
            if ROOT not in parent.absolute().parents and parent.absolute() != ROOT.absolute():
                continue
            parent_item = parent.lstat()
            if stat.S_ISLNK(parent_item.st_mode):
                fail(f"helper source traverses a symbolic-link ancestor: {parent}")
            if not stat.S_ISDIR(parent_item.st_mode):
                fail(f"helper source traverses a non-directory ancestor: {parent}")
        actual = hashlib.sha256(
            regular_file_bytes(helper_file, f"helper source {relative}")
        ).hexdigest()
        if actual != expected:
            fail(
                f"helper source has changed since the pinned build: "
                f"{relative}; rebuild the helper before packing"
            )
    artifacts = provenance.get("build", {}).get("platforms")
    if not isinstance(artifacts, list):
        fail("helper provenance does not list build platforms")
    listed = {}
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("path"), str)
            or not isinstance(artifact.get("goos"), str)
            or not isinstance(artifact.get("goarch"), str)
        ):
            fail("helper provenance carries a malformed platform entry")
        listed[f"{HELPER_VENDOR}/{artifact['path']}"] = artifact
    expected_paths = {
        f"{HELPER_VENDOR}/shell-capture-parser-{goos}-{goarch}"
        for goos, goarch in HELPER_PLATFORMS
    }
    if set(listed) != expected_paths:
        fail("helper provenance platform artifacts do not match the allowlist")
    for platform_path, artifact in sorted(listed.items()):
        contents = files.get(platform_path)
        if contents is None:
            fail(f"bundle is missing helper artifact {platform_path}")
        if hashlib.sha256(contents).hexdigest() != artifact.get("sha256"):
            fail(f"helper artifact does not match its provenance: {platform_path}")
        if len(contents) != artifact.get("size"):
            fail(f"helper artifact size does not match provenance: {platform_path}")
        expected_magic = ELF_MAGIC if artifact["goos"] == "linux" else MACHO_MAGIC
        if not contents.startswith(expected_magic):
            fail(
                f"helper artifact does not carry the "
                f"{artifact['goos']} binary magic: {platform_path}"
            )
    licenses = {
        f"{HELPER_VENDOR}/{item.get('path')}": item
        for item in provenance.get("licenses", [])
    }
    expected_licenses = {
        f"{HELPER_VENDOR}/LICENSE-go-runtime.txt",
        f"{HELPER_VENDOR}/LICENSE-mvdan-sh.txt",
    }
    if set(licenses) != expected_licenses:
        fail("helper provenance does not list exactly the bundled licenses")
    for relative in sorted(expected_licenses):
        contents = files.get(relative)
        if contents is None:
            fail(f"bundle is missing helper license {relative}")
        if hashlib.sha256(contents).hexdigest() != licenses[relative].get("sha256"):
            fail(f"helper license does not match provenance: {relative}")
    return provenance


def source_files(catalog_path: Path, contract_path: Path) -> dict[str, bytes]:
    contract_bytes = regular_file_bytes(contract_path, "contract input")
    catalog = json.loads(regular_file_bytes(catalog_path, "catalog input"))
    files: dict[str, bytes] = {}
    for relative in FILES:
        path = SOURCE / relative
        files[relative] = regular_file_bytes(path, f"bundle source {relative}")
    manifest = json.loads(files["manifest.json"])
    verifier_path = SOURCE / "lib/verify-read-bridge.py"
    spec = importlib.util.spec_from_file_location("obsidian_read_bridge", verifier_path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    bridge_pin = manifest.get("readBridge")
    if bridge_pin != catalog.get("server", {}).get("readHttpBridge"):
        fail("catalog read bridge pin does not match bundle")
    verified_vendor = verifier.verified_files(SOURCE, bridge_pin)
    for relative, contents in verified_vendor.items():
        if (
            regular_file_bytes(SOURCE / relative, f"bundle source {relative}")
            != contents
        ):
            fail(f"bundle source changed during verification: {relative}")
    files.update(verified_vendor)
    manifest["pinnedServer"] = server_reference(catalog, contract_bytes)
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    files["contract.json"] = contract_bytes
    files["LICENSE"] = regular_file_bytes(ROOT / "LICENSE", "project license input")
    validate_helper_vendor(files)
    return files


def release_metadata(files: dict[str, bytes]) -> bytes:
    inventory = [
        {
            "mode": 0o755 if relative in EXECUTABLES else 0o644,
            "path": relative,
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size": len(contents),
        }
        for relative, contents in sorted(files.items())
    ]
    inventory_bytes = json.dumps(
        inventory, sort_keys=True, separators=(",", ":")
    ).encode()
    release_id = hashlib.sha256(inventory_bytes).hexdigest()
    manifest = json.loads(files["manifest.json"])
    capture = manifest.get("commitCapture", {})
    capture_defaults = {
        "automaticHistoricalBackfill": capture.get("automaticHistoricalBackfill"),
        "defaultActivated": capture.get("defaultActivated"),
    }
    if any(value is not False for value in capture_defaults.values()):
        fail("manifest commit capture defaults must be literal false")
    metadata = {
        "formatVersion": 1,
        "releaseId": release_id,
        "inventorySha256": release_id,
        "files": inventory,
        "pinnedServer": manifest["pinnedServer"],
        "readBridge": {
            "provenanceSha256": manifest["readBridge"]["provenanceSha256"],
        },
        "captureDefaults": capture_defaults,
    }
    return (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()


def validate_destination(output: Path) -> Path:
    resolved = output.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        fail("output must be outside the repository")
    return resolved


def existing_bundle_matches(output: Path, files: dict[str, bytes]) -> bool:
    if not output.exists():
        return False
    if not output.is_dir() or output.is_symlink():
        fail("output exists but is not a directory")
    if stat.S_IMODE(output.stat().st_mode) != 0o755:
        fail("output exists but has incorrect directory permissions")
    for relative, contents in files.items():
        path = output / relative
        try:
            item = path.lstat()
        except OSError:
            fail("output exists but differs from this bundle")
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or path.read_bytes() != contents
        ):
            fail("output exists but differs from this bundle")
        expected_mode = 0o755 if relative in EXECUTABLES else 0o644
        if stat.S_IMODE(path.stat().st_mode) != expected_mode:
            fail("output exists but has incorrect permissions")
    expected_entries = set(files)
    for relative in files:
        expected_entries.update(
            parent.as_posix()
            for parent in Path(relative).parents
            if parent != Path(".")
        )
    actual = set()
    for path in output.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            fail("output exists but contains unsafe entries")
        if path.is_dir() and stat.S_IMODE(path.stat().st_mode) != 0o755:
            fail("output exists but has incorrect directory permissions")
        actual.add(path.relative_to(output).as_posix())
    if actual != expected_entries:
        fail("output exists but contains unallowlisted files")
    return True


def write_bundle(output: Path, files: dict[str, bytes]) -> None:
    output.mkdir(parents=True, exist_ok=False)
    try:
        output.chmod(0o755)
        for relative, contents in files.items():
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            path.chmod(0o755 if relative in EXECUTABLES else 0o644)
        for path in output.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
    except OSError as error:
        fail(
            f"could not finish bundle at {output}: {error}; "
            f"incomplete bundle retained; remove {output} before retrying"
        )


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    args = parser.parse_args()
    files = source_files(args.catalog.resolve(), args.contract.resolve())
    files["release.json"] = release_metadata(files)
    release_sha256 = hashlib.sha256(files["release.json"]).hexdigest()
    output = validate_destination(args.output)
    if existing_bundle_matches(output, files):
        print(f"obsidian-mcp-client: up to date at {output}")
        print(f"obsidian-mcp-client: release.json sha256 {release_sha256}")
        return 0
    write_bundle(output, files)
    print(f"obsidian-mcp-client: packed {len(files)} files at {output}")
    print(f"obsidian-mcp-client: release.json sha256 {release_sha256}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"obsidian-mcp-client: {error}", file=sys.stderr)
        raise SystemExit(EXIT_FAILURE)
