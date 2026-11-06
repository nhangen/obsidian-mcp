"""Pack verification for the shell-capture-parser helper vendor.

Build artifacts are produced into temporary isolated directories for the
determinism checks and into the explicit vendor path for the packable
state. No release is installed and no packaging writes inside the
repository output tree.
"""

import hashlib
import json
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "packages/client"
VENDOR = CLIENT / "vendor/shell-capture-parser"
BUILDER = ROOT / "scripts/build-shell-capture-parser.py"
PACKER = ROOT / "scripts/pack-obsidian-mcp-client.py"
ARTIFACTS = (
    "shell-capture-parser-darwin-amd64",
    "shell-capture-parser-darwin-arm64",
    "shell-capture-parser-linux-amd64",
    "shell-capture-parser-linux-arm64",
)


def load_module(source: Path, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(source)
    exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
    return module


def build_to(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"builder failed: {result.returncode} {result.stdout} {result.stderr}"
        )


class HelperVendorStateTest(unittest.TestCase):
    def test_vendor_artifacts_and_provenance_exist(self):
        provenance = json.loads((VENDOR / "BUILD-PROVENANCE.json").read_bytes())
        self.assertEqual(provenance["formatVersion"], 1)
        self.assertEqual(provenance["module"]["version"], "v3.11.0")
        self.assertEqual(
            {entry["path"] for entry in provenance["build"]["platforms"]},
            set(ARTIFACTS),
        )
        for artifact in ARTIFACTS:
            contents = (VENDOR / artifact).read_bytes()
            pinned = next(
                entry
                for entry in provenance["build"]["platforms"]
                if entry["path"] == artifact
            )
            self.assertEqual(hashlib.sha256(contents).hexdigest(), pinned["sha256"])
            self.assertEqual(len(contents), pinned["size"])

    def test_local_artifact_classifies_the_reference_command(self):
        command = json.dumps(
            {"schema": "shell-capture-parser@1", "command": "git commit -m x"}
        )
        system = platform.system().lower()
        machine = platform.machine().lower()
        machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
        native = subprocess.run(
            [str(VENDOR / f"shell-capture-parser-{system}-{machine}")],
            input=command,
            capture_output=True,
            text=True,
        )
        self.assertEqual(native.returncode, 0, native.stderr)
        self.assertEqual(
            json.loads(native.stdout),
            {
                "schema": "shell-capture-parser@1",
                "status": "analyze",
                "flags": ["GIT_COMMIT_SIMPLE"],
            },
        )


class HelperBuildDeterminismTest(unittest.TestCase):
    def test_rebuilds_are_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="helper-build-first-") as first_dir:
            with tempfile.TemporaryDirectory(
                prefix="helper-build-second-"
            ) as second_dir:
                first = Path(first_dir)
                second = Path(second_dir)
                build_to(first / "out")
                build_to(second / "out")
                self.assertEqual(
                    (first / "out" / "BUILD-PROVENANCE.json").read_bytes(),
                    (second / "out" / "BUILD-PROVENANCE.json").read_bytes(),
                )
                for artifact in ARTIFACTS:
                    self.assertEqual(
                        (first / "out" / artifact).read_bytes(),
                        (second / "out" / artifact).read_bytes(),
                        artifact,
                    )
                    self.assertEqual(
                        (first / "out" / artifact).read_bytes(),
                        (VENDOR / artifact).read_bytes(),
                        f"vendored {artifact} is not the source's rebuild",
                    )
                    self.assertTrue(stat.S_IMODE(
                        (first / "out" / artifact).stat().st_mode
                    ) == 0o755)

    def test_built_artifact_licenses_are_present(self):
        with tempfile.TemporaryDirectory(prefix="helper-build-license-") as license_dir:
            directory = Path(license_dir)
            build_to(directory / "out")
            for license_name in ("LICENSE-mvdan-sh.txt", "LICENSE-go-runtime.txt"):
                contents = (directory / "out" / license_name).read_bytes()
                self.assertGreater(len(contents), 0, license_name)


class PackerHelperValidationTest(unittest.TestCase):
    def load_packer(self):
        return load_module(PACKER, "packer_under_test")

    def files_from_vendor(self, packer):
        files = {}
        for relative in packer.FILES:
            path = CLIENT / relative
            files[relative] = path.read_bytes()
        return files

    def test_helper_vendor_entries_allowlisted_as_executables(self):
        packer = self.load_packer()
        for artifact in ARTIFACTS:
            vendor = f"vendor/shell-capture-parser/{artifact}"
            self.assertIn(vendor, packer.FILES)
            self.assertIn(vendor, packer.EXECUTABLES)
        for license_name in ("LICENSE-go-runtime.txt", "LICENSE-mvdan-sh.txt"):
            self.assertIn(f"vendor/shell-capture-parser/{license_name}", packer.FILES)
            self.assertNotIn(
                f"vendor/shell-capture-parser/{license_name}",
                packer.EXECUTABLES,
            )

    def test_validation_accepts_the_built_vendor(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)
        packer.validate_helper_vendor(files)

    def test_rejects_stale_helper_source(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)
        provenance = json.loads(files["vendor/shell-capture-parser/BUILD-PROVENANCE.json"])
        provenance["source"]["main.go"] = "0" * 64
        files["vendor/shell-capture-parser/BUILD-PROVENANCE.json"] = (
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "stale|changed"):
            packer.validate_helper_vendor(files)

    def test_rejects_missing_artifact(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)
        del files["vendor/shell-capture-parser/shell-capture-parser-linux-arm64"]
        with self.assertRaisesRegex(ValueError, "missing helper artifact"):
            packer.validate_helper_vendor(files)

    def test_rejects_artifact_mismatch(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)
        path = "vendor/shell-capture-parser/shell-capture-parser-darwin-amd64"
        contents = bytearray(files[path])
        contents[20] ^= 0xFF
        files[path] = bytes(contents)
        with self.assertRaisesRegex(ValueError, "does not match its provenance"):
            packer.validate_helper_vendor(files)

    def test_rejects_platform_magic_mismatch(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)
        path = "vendor/shell-capture-parser/shell-capture-parser-linux-amd64"
        contents = packer.MACHO_MAGIC + files[path][len(packer.ELF_MAGIC):]
        files[path] = contents
        provenance = json.loads(files["vendor/shell-capture-parser/BUILD-PROVENANCE.json"])
        for entry in provenance["build"]["platforms"]:
            if entry["path"] == "shell-capture-parser-linux-amd64":
                entry["sha256"] = hashlib.sha256(contents).hexdigest()
                entry["size"] = len(contents)
        files["vendor/shell-capture-parser/BUILD-PROVENANCE.json"] = (
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "binary magic"):
            packer.validate_helper_vendor(files)

    def forged_provenance(self, files, change):
        key = "vendor/shell-capture-parser/BUILD-PROVENANCE.json"
        provenance = json.loads(files[key])
        change(provenance)
        files[key] = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()

    def test_rejects_wrong_architecture_in_a_platform_slot(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)
        path = "vendor/shell-capture-parser/shell-capture-parser-linux-arm64"
        contents = files["vendor/shell-capture-parser/shell-capture-parser-linux-amd64"]
        files[path] = contents

        def change(provenance):
            for entry in provenance["build"]["platforms"]:
                if entry["path"] == "shell-capture-parser-linux-arm64":
                    entry["sha256"] = hashlib.sha256(contents).hexdigest()
                    entry["size"] = len(contents)

        self.forged_provenance(files, change)
        with self.assertRaisesRegex(ValueError, "wrong CPU type"):
            packer.validate_helper_vendor(files)

    def test_rejects_mislabeled_platform_entry(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)

        def change(provenance):
            provenance["build"]["platforms"][0]["goarch"] = "arm64"

        self.forged_provenance(files, change)
        with self.assertRaisesRegex(ValueError, "mislabels platform"):
            packer.validate_helper_vendor(files)

    def test_rejects_forged_toolchain_and_go_sum(self):
        packer = self.load_packer()
        for field, value, message in (
            ("toolchain", {"goVersion": "go1.26.0"}, "does not pin go"),
            (
                "module",
                {
                    "name": packer.HELPER_MODULE,
                    "version": packer.HELPER_MODULE_VERSION,
                    "goSum": "forged",
                },
                "goSum does not match",
            ),
        ):
            files = self.files_from_vendor(packer)
            self.forged_provenance(files, lambda doc: doc.update({field: value}))
            with self.assertRaisesRegex(ValueError, message):
                packer.validate_helper_vendor(files)

    def test_rejects_symlink_sources(self):
        packer = self.load_packer()
        with tempfile.TemporaryDirectory(prefix="helper-pack-symlink-") as directory:
            target = Path(directory) / "target.txt"
            target.write_bytes(b"fixture")
            link = Path(directory) / "link.txt"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular file"):
                packer.regular_file_bytes(link, "fixture link")

    def test_release_inventory_carries_helper_files_with_modes(self):
        packer = self.load_packer()
        files = self.files_from_vendor(packer)
        manifest = json.loads((CLIENT / "manifest.json").read_bytes())
        manifest["pinnedServer"] = {
            "release": "fixture",
            "contractVersion": 1,
            "contractSha256": "0" * 64,
            "sourceCommit": "f" * 40,
        }
        files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
        files["release.json"] = packer.release_metadata(files)
        inventory = json.loads(files["release.json"])["files"]
        entries = {item["path"]: item for item in inventory}
        for artifact in ARTIFACTS:
            vendor = f"vendor/shell-capture-parser/{artifact}"
            self.assertEqual(entries[vendor]["mode"], 0o755)
            self.assertEqual(entries[vendor]["sha256"], hashlib.sha256(files[vendor]).hexdigest())
        self.assertEqual(entries["vendor/shell-capture-parser/BUILD-PROVENANCE.json"]["mode"], 0o644)


if __name__ == "__main__":
    unittest.main()
