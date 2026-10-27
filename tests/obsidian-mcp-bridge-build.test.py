import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class BridgeBuildTest(unittest.TestCase):
    def load_builder(self):
        source = ROOT / "packages/client/vendor/mcp-remote-read/build.py"
        module = types.ModuleType("bridge_build_under_test")
        module.__file__ = str(source)
        exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)
        return module

    def test_dependency_audit_failure_stops_before_bundle_output(self):
        module = self.load_builder()
        with tempfile.TemporaryDirectory(prefix="bridge-build-audit-") as directory:
            fixture = Path(directory)
            artifact = fixture / "source.tgz"
            artifact.write_bytes(b"isolated upstream fixture")
            for name in ("build-package.json", "build-package-lock.json"):
                (fixture / name).write_text("{}\n")
            commands = fixture / "commands.jsonl"
            npm = fixture / "npm"
            npm.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['BRIDGE_AUDIT_COMMANDS'], 'a') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "sys.exit(42 if sys.argv[1] == 'audit' else 0)\n"
            )
            npm.chmod(0o755)
            patch = fixture / "patch"
            patch.write_text("#!/bin/sh\nexit 0\n")
            patch.chmod(0o755)
            output = fixture / "output"
            with (
                mock.patch.object(module, "ROOT", fixture),
                mock.patch.object(module, "UPSTREAM_SHA256", hashlib.sha256(artifact.read_bytes()).hexdigest()),
                mock.patch.object(module, "extract_sources"),
                mock.patch.dict(os.environ, {"PATH": f"{fixture}:{os.environ['PATH']}", "BRIDGE_AUDIT_COMMANDS": str(commands)}),
            ):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    module.build(artifact, output, fixture / "cache")
            self.assertEqual(raised.exception.returncode, 42)
            self.assertEqual(raised.exception.cmd, ["npm", "audit", "--omit=dev", "--json"])
            self.assertEqual([json.loads(line) for line in commands.read_text().splitlines()], [
                ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                ["audit", "--omit=dev", "--json"],
            ])
            self.assertFalse(output.exists())

    def test_audit_requires_a_successful_well_formed_zero_vulnerability_report(self):
        module = self.load_builder()
        zero = {name: 0 for name in ("info", "low", "moderate", "high", "critical", "total")}
        cases = [
            ("malformed", "not-json", 0, ValueError),
            ("missing-counts", "{}", 0, ValueError),
            ("vulnerable", json.dumps({"metadata": {"vulnerabilities": {**zero, "high": 1, "total": 1}}}), 0, ValueError),
            ("network-failure", "{}", 42, subprocess.CalledProcessError),
            ("zero", json.dumps({"metadata": {"vulnerabilities": zero}}), 0, None),
        ]
        with tempfile.TemporaryDirectory(prefix="bridge-audit-report-") as directory:
            fixture = Path(directory)
            npm = fixture / "npm"
            npm.write_text("#!/usr/bin/env python3\nimport os,sys\nprint(os.environ['BRIDGE_REPORT'])\nsys.exit(int(os.environ['BRIDGE_CODE']))\n")
            npm.chmod(0o755)
            for name, report, code, error in cases:
                with self.subTest(name=name):
                    environment = {**os.environ, "PATH": f"{fixture}:{os.environ['PATH']}", "BRIDGE_REPORT": report, "BRIDGE_CODE": str(code)}
                    if error is None:
                        module.audit_dependencies(fixture, environment)
                    else:
                        with self.assertRaises(error):
                            module.audit_dependencies(fixture, environment)

    def test_license_inventory_keeps_nested_dependency_identity(self):
        module = self.load_builder()
        with tempfile.TemporaryDirectory(prefix="bridge-license-") as directory:
            node_modules = Path(directory)
            for path, name, version, license_text in [
                ("send", "send", "1.0.0", "OUTER LICENSE"),
                ("send/node_modules/ms", "ms", "2.0.0", "NESTED LICENSE"),
            ]:
                package = node_modules / path
                package.mkdir(parents=True)
                (package / "package.json").write_text(json.dumps({"name": name, "version": version, "license": "MIT"}))
                (package / "LICENSE").write_text(license_text)
            result = module.build_licenses(node_modules, {"inputs": {"node_modules/send/index.js": {}, "node_modules/send/node_modules/ms/index.js": {}}})
            self.assertIn("ms@2.0.0 (MIT)", result)
            self.assertIn("NESTED LICENSE", result)

    def test_missing_notice_fails_instead_of_substituting_license_expression(self):
        module = self.load_builder()
        with tempfile.TemporaryDirectory(prefix="bridge-missing-license-") as directory:
            root = Path(directory)
            package = root / "unlicensed"
            package.mkdir()
            (package / "package.json").write_text(json.dumps({"name": "unlicensed", "version": "1.0.0", "license": "MIT"}))
            with self.assertRaisesRegex(ValueError, "license notice"):
                module.build_licenses(root, {"inputs": {"node_modules/unlicensed/index.js": {}}})

    def test_embedded_and_supplemental_notices_are_included(self):
        module = self.load_builder()
        with tempfile.TemporaryDirectory(prefix="bridge-notices-") as directory:
            root = Path(directory)
            for name, version in [("cookie-signature", "1.0.7"), ("strict-url-sanitise", "0.0.1")]:
                package = root / name
                package.mkdir()
                (package / "package.json").write_text(json.dumps({"name": name, "version": version, "license": "MIT"}))
            (root / "cookie-signature/Readme.md").write_text("# Package\n\n## License\n\nCopyright upstream fixture\nPermission is hereby granted\n")
            result = module.build_licenses(root, {"inputs": {"node_modules/cookie-signature/index.js": {}, "node_modules/strict-url-sanitise/index.js": {}}})
            self.assertIn("Copyright upstream fixture", result)
            self.assertIn("Glen Maddern", result)
            self.assertIn("Permission is hereby granted", result)
            self.assertNotIn("License expression:", result)


if __name__ == "__main__":
    unittest.main()
