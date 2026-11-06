#!/usr/bin/env python3
"""Deterministic development build for the pinned shell capture parser.

Builds the Go helper only; nothing is executed, installed, or downloaded.
Each supported platform artifact is written to the explicit bundle vendor
path with CGO disabled and trimming paths, followed by generated public
build provenance. Supplying --output stages into an isolated directory
instead of the vendor path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "helpers/shell-capture-parser"
DEFAULT_OUTPUT = ROOT / "packages/client/vendor/shell-capture-parser"
PLATFORMS = (
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("linux", "amd64"),
    ("linux", "arm64"),
)
MODULE = "mvdan.cc/sh/v3"
MODULE_VERSION = "v3.11.0"
SOURCE_FILES = ("main.go", "main_test.go", "go.mod", "go.sum")
EXPECTED_GOVERSION = "go1.27.3"
LABEL_PATTERN = re.compile(r"^[a-z0-9]{1,32}$")
LICENSE_TARGETS = (
    ("LICENSE-mvdan-sh.txt", "mvdan"),
    ("LICENSE-go-runtime.txt", "go"),
)

MACHO_MAGIC = b"\xcf\xfa\xed\xfe"
ELF_MAGIC = b"\x7fELF"


def fail(message: str) -> None:
    raise ValueError(message)


def sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def regular_file(path: Path, label: str) -> bytes:
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        fail(f"{label} is not a private regular file")
    return path.read_bytes()


def go_setting(name: str) -> str:
    result = subprocess.run(
        ["go", "env", name], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def go_major_minor(version: str) -> tuple[int, int] | None:
    # go1.27.3 -> (1, 27)
    if not version.startswith("go"):
        return None
    body = version[2:]
    parts = body.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1][:2].isdigit():
        return None
    return int(parts[0]), int(parts[1][:2])


def toolchain_license_bytes(version: str):
    goroot = Path(go_setting("GOROOT"))
    # Homebrew layouts keep the cell LICENSE one level above the gocr
    # GOROOT root; canonical installs keep it at the GOROOT root.
    candidates = (
        goroot / "LICENSE",
        goroot.parent / "LICENSE",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes(), str(candidate.relative_to(candidate.parents[-2]))
        if candidate == candidates[0]:
            continue
    parts = go_major_minor(version)
    if parts is None:
        fail(f"cannot determine the Go toolchain license for {version}")
    github = f"https://raw.githubusercontent.com/golang/go/go{version}/LICENSE"
    fail(
        "cannot read the Go toolchain license file; no LICENSE found in "
        f"{goroot} or {goroot.parent}; for a pinned toolchain build use the "
        "release tarball LICENSE file for "
        f"{github}"
    )


def module_license_bytes() -> bytes:
    candidates = (
        Path.home() / f"go/pkg/mod/{MODULE}@{MODULE_VERSION}/LICENSE",
        Path(go_setting("GOMODCACHE")) / f"{MODULE}@{MODULE_VERSION}/LICENSE",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes()
    fail(
        f"cannot read the {MODULE}@{MODULE_VERSION} license; expected it in "
        "the module cache from the pinned dependency download"
    )


def build_platform(goos: str, goarch: str, output: Path) -> bytes:
    artifact = output / f"shell-capture-parser-{goos}-{goarch}"
    environment = controlled_build_environment()
    environment.update(
        {
            "CGO_ENABLED": "0",
            "GOOS": goos,
            "GOARCH": goarch,
        }
    )
    subprocess.run(
        [
            "go",
            "build",
            "-trimpath",
            "-mod=readonly",
            "-buildvcs=false",
            "-o",
            str(artifact),
            ".",
        ],
        cwd=HELPER,
        env=environment,
        check=True,
        capture_output=True,
    )
    return artifact.read_bytes()


def controlled_build_environment():
    """Pinned offline toolchain settings: no auto toolchain download, no
    network proxying, no workspace replacement, and no inherited GOFLAGS;
    every setting is set explicitly for the build only."""
    environment = dict(os.environ)
    environment.pop("GOFLAGS", None)
    environment.update(
        {
            "GOTOOLCHAIN": "local",
            "GOPROXY": "off",
            "GOWORK": "off",
        }
    )
    return environment


def reject_symlink_ancestors(path: Path, label: str) -> None:
    """Reject any symbolic-link ancestor on an input path."""
    for parent in path.absolute().parents:
        item = parent.lstat()
        if stat.S_ISLNK(item.st_mode):
            fail(f"{label} traverses a symbolic-link ancestor: {parent}")
        if not stat.S_ISDIR(item.st_mode):
            fail(f"{label} traverses a non-directory ancestor: {parent}")


def regular_directory_chain(path: Path, label: str) -> None:
    """Reject symlinked ancestors and inputs escaped from the repo root."""
    reject_symlink_ancestors(path, label)
    if ROOT not in path.absolute().parents:
        fail(f"{label} does not live under the repository root")


def verify_dependency_checksums() -> None:
    """Verify the pinned dependency against its checksums with the offline
    toolchain before building; go mod verify recomputes cached content."""
    result = subprocess.run(
        ["go", "mod", "verify"],
        cwd=HELPER,
        env=controlled_build_environment(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or "all modules verified" not in result.stdout:
        fail(f"dependency verification failed: {result.returncode}")


def verify_build_provenance(artifact: Path, goos: str, goarch: str) -> None:
    """Sanity-check the binary's recorded module under the pinned pin and
    platform strings so build provenance matches the dependency pin."""
    text = subprocess.run(
        ["go", "version", "-m", str(artifact)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if f"dep\t{MODULE}\t{MODULE_VERSION}\th1:" not in text:
        fail(
            f"built artifact does not record {MODULE}@{MODULE_VERSION} "
            "in its build provenance"
        )
    if "GOOS=" + goos not in text or "GOARCH=" + goarch not in text:
        fail(f"built artifact platform labels do not match {goos}/{goarch}")


def validate_platform_entries(entries) -> list:
    seen = set()
    checked = []
    for goos, goarch in entries:
        for label in (goos, goarch):
            if not isinstance(label, str) or not LABEL_PATTERN.match(label):
                fail(f"invalid platform label {label!r}")
        pair = (goos, goarch)
        if pair in seen:
            fail(f"duplicate platform {goos}/{goarch}")
        seen.add(pair)
        checked.append(pair)
    if len(checked) != len(PLATFORMS) or tuple(checked) != PLATFORMS:
        fail("platform artifacts do not cover exactly the supported set")
    return checked


def magic_for(goos: str, contents: bytes) -> None:
    expected = ELF_MAGIC if goos == "linux" else MACHO_MAGIC
    if not contents.startswith(expected):
        fail(f"built artifact for {goos} does not carry the {goos} binary magic")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        fail(f"build output {output} exists; remove it before rebuilding")
    regular_directory_chain(HELPER, "helper source")
    output.mkdir(parents=True)
    go_version = go_setting("GOVERSION")
    if go_version != EXPECTED_GOVERSION:
        fail(f"unexpected Go toolchain version {go_version}")
    module_cache = Path(go_setting("GOMODCACHE"))
    reject_symlink_ancestors(module_cache / f"{MODULE}@{MODULE_VERSION}", "module cache")
    if (ROOT / "go.work").is_file() or (ROOT / "helpers/shell-capture-parser/go.work").is_file():
        fail("a go workspace file is present; workspace replacements are rejected")
    validate_platform_entries(PLATFORMS)
    verify_dependency_checksums()
    source_hashes = {}
    for relative in SOURCE_FILES:
        contents = regular_file(HELPER / relative, f"helper source {relative}")
        source_hashes[relative] = sha256_bytes(contents)
    artifacts = []
    for goos, goarch in PLATFORMS:
        contents = build_platform(goos, goarch, output)
        magic_for(goos, contents)
        artifact_path = output / f"shell-capture-parser-{goos}-{goarch}"
        verify_build_provenance(artifact_path, goos, goarch)
        artifacts.append(
            {
                "goos": goos,
                "goarch": goarch,
                "path": f"shell-capture-parser-{goos}-{goarch}",
                "sha256": sha256_bytes(contents),
                "size": len(contents),
                "mode": "0755",
            }
        )
    licenses = []
    for target, source in LICENSE_TARGETS:
        if source == "mvdan":
            contents = module_license_bytes()
        else:
            contents, _ = toolchain_license_bytes(go_version)
        (output / target).write_bytes(contents)
        (output / target).chmod(0o644)
        licenses.append({"path": target, "sha256": sha256_bytes(contents)})
    go_sum_line = None
    for line in (HELPER / "go.sum").read_text().splitlines():
        if line.startswith(MODULE + " " + MODULE_VERSION + " "):
            go_sum_line = line
            break
    if go_sum_line is None:
        fail(f"go.sum does not pin {MODULE} {MODULE_VERSION}")
    provenance = {
        "formatVersion": 1,
        "helper": "shell-capture-parser",
        "module": {
            "name": MODULE,
            "version": MODULE_VERSION,
            "goSum": go_sum_line,
            "license": "LICENSE-mvdan-sh.txt",
        },
        "toolchain": {
            "goVersion": go_version,
            "license": "LICENSE-go-runtime.txt",
        },
        "build": {
            "flags": ["CGO_ENABLED=0", "-trimpath", "-buildvcs=false", "-mod=readonly"],
            "offline": ["GOTOOLCHAIN=local", "GOPROXY=off", "GOWORK=off"],
            "dependencyVerification": "go mod verify",
            "platforms": artifacts,
        },
        "source": source_hashes,
        "licenses": licenses,
    }
    (output / "BUILD-PROVENANCE.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    )
    (output / "BUILD-PROVENANCE.json").chmod(0o644)
    print(f"shell-capture-parser: built {len(artifacts)} artifacts at {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"shell-capture-parser: {error}", file=sys.stderr)
        raise SystemExit(64)
