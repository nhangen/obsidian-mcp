#!/usr/bin/env python3
"""Offline deployment-export fixtures, never host policy installation."""

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "packages/client/bin/obsidian-mcp-managed-export"


class ManagedExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.bundle = cls.root / "client"
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts/pack-obsidian-mcp-client.py"),
                        "--output", str(cls.bundle)], check=True, capture_output=True)
        cls.release = json.loads((cls.bundle / "release.json").read_bytes())
        cls.sha = hashlib.sha256((cls.bundle / "release.json").read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.plan = dict(schemaVersion=1, host="codex", scope="project", targetUid=501,
                         project=str(self.directory / "project"),
                         profileHome=str(self.directory / "profile"),
                         managedDir="/etc/codex/hooks/obsidian",
                         bundlePath="/opt/obsidian-mcp/" + self.release["releaseId"],
                         pythonPath="/usr/bin/python3", pythonVersion="3.11")
        self.plan_path = self.private("plan.json", json.dumps(self.plan).encode())

    def private(self, name, body):
        path = self.directory / name
        path.write_bytes(body)
        path.chmod(0o600)
        return path

    def cli(self, *extra, plan=None, bundle=None, sha=None, guard=None):
        if plan is not None:
            self.plan_path.write_text(json.dumps(plan))
        executable = self.bundle / "bin/obsidian-mcp-managed-export"
        prefix = [sys.executable, "-B", str(executable)]
        if guard:
            prefix = [sys.executable, "-B", "-c", guard, str(executable)]
        return subprocess.run([*prefix, "--bundle",
                               str(bundle or self.bundle), "--release-sha256", sha or self.sha,
                               "--plan", str(self.plan_path), *map(str, extra)],
                              cwd=self.directory, text=True, capture_output=True, timeout=30,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def rejected(self, result, code):
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(code, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_help_describes_export_not_activation(self):
        result = subprocess.run(
            [sys.executable, "-B", str(EXPORT), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--output", result.stdout)
        self.assertIn("export", result.stdout.lower())

    def test_preview_is_canonical_nonmutating_and_omits_input_paths(self):
        before = sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob("*"))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        self.assertIs(preview["activationReady"], False)
        self.assertEqual(preview["status"], "proposal-only")
        self.assertEqual(len(preview["blockers"]), 4)
        self.assertEqual(result.stdout, json.dumps(preview, sort_keys=True, separators=(",", ":")) + "\n")
        self.assertNotIn(str(self.directory), result.stdout)
        self.assertEqual(before, sorted(str(p.relative_to(self.directory)) for p in self.directory.rglob("*")))
        self.assertFalse(Path(self.plan["profileHome"]).exists())

    def test_create_only_export_inventory_and_proposal_events(self):
        output = self.directory / "export"
        result = self.cli("--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in output.iterdir()},
                         {"requirements.fragment.toml", "dispatcher-config.json", "ownership-manifest.json"})
        manifest = json.loads((output / "ownership-manifest.json").read_bytes())
        self.assertFalse(manifest["activationReady"])
        self.assertFalse(manifest["policyMerged"])
        self.assertFalse(manifest["dispatcherImplemented"])
        self.assertEqual(manifest["releaseSha256"], self.sha)
        self.assertEqual(manifest["releaseId"], self.release["releaseId"])
        self.assertEqual(manifest["releaseInventory"], self.release["files"])
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir()))
        for item in manifest["artifacts"]:
            data = (output / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
            self.assertEqual(len(data), item["size"])
        proposal = tomllib.loads((output / "requirements.fragment.toml").read_text())
        self.assertNotIn("hooks", proposal)
        self.assertFalse(proposal["proposal"]["deployable"])
        events = proposal["proposal"]["events"]
        self.assertEqual([x["event"] for x in events], ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"])
        self.assertEqual(events[1]["matcher"], "^(Bash|mcp__obsidian_write__obsidian_keeper_save|mcp__obsidian_write__obsidian_daily_append)$")
        config = json.loads((output / "dispatcher-config.json").read_bytes())
        self.assertEqual(config["plan"], self.plan)
        self.assertFalse(config["executable"])
        first = {p.name: p.read_bytes() for p in output.iterdir()}
        self.rejected(self.cli("--output", output), "OUTPUT_INVALID")
        self.assertEqual(first, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_policy_snapshot_exact_preserved_without_preview_disclosure(self):
        data = b'# SECRET_POLICY_CANARY\r\n[model]\r\nname = "custom"\r\n[hooks]\r\nother = true\r\n'
        policy = self.private("policy.toml", data)
        output = self.directory / "export"
        result = self.cli("--existing-policy", policy, "--output", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(policy.read_bytes(), data)
        self.assertEqual((output / "existing-policy.toml").read_bytes(), data)
        self.assertNotIn("SECRET_POLICY_CANARY", result.stdout + result.stderr)

    def test_policy_conflicts_and_unknown_ownership_fail_closed(self):
        for body, code in [(b'[hooks]\nmanaged_dir = "/other"\n', "MANAGED_DIR_CONFLICT"),
                           (b'[hooks]\nmanaged_dir = "/etc/codex/hooks/obsidian"\n', "OWNERSHIP_UNKNOWN"),
                           (b'[hooks.Stop]\ncommand = "obsidian-mcp-native hook"\n', "OWNED_HANDLER_CONFLICT")]:
            with self.subTest(code=code):
                policy = self.private("policy.toml", body)
                self.rejected(self.cli("--existing-policy", policy), code)
                self.assertEqual(policy.read_bytes(), body)

    def test_local_hook_duplicates_and_unrelated_hooks(self):
        def inventory(command):
            return json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}).encode()
        local = self.private("local.json", inventory("notify-user"))
        self.assertEqual(self.cli("--local-hooks", local).returncode, 0)
        local.write_bytes(inventory("/opt/client/bin/obsidian-mcp-native hook --home /user"))
        self.rejected(self.cli("--local-hooks", local), "LOCAL_CAPTURE_DUPLICATE")
        local.write_bytes(b'{"invented":true}')
        self.rejected(self.cli("--local-hooks", local), "LOCAL_HOOKS_INVALID")

    def test_plan_validation_scope_uid_interpreter_and_paths(self):
        for key, value in [("schemaVersion", True), ("host", "other"), ("scope", "user"),
                           ("targetUid", 0), ("targetUid", -1), ("targetUid", True),
                           ("pythonVersion", "3.10"), ("pythonVersion", "3.11;exec"),
                           ("pythonPath", "/home/user/python3"), ("managedDir", "/tmp/hooks"),
                           ("bundlePath", "/opt/obsidian-mcp/wrong"), ("project", "relative"),
                           ("project", "/home/user/../project"), ("project", "/"),
                           ("profileHome", "/tmp/a\nSECRET_INPUT_CANARY"),
                           ("credential", "SECRET_INPUT_CANARY")]:
            with self.subTest(key=key, value=value):
                self.rejected(self.cli(plan=dict(self.plan, **{key: value})), "PLAN_INVALID")
        self.assertNotIn("SECRET_INPUT_CANARY", self.cli(plan=dict(self.plan, credential="SECRET_INPUT_CANARY")).stderr)

    def test_private_regular_bounded_inputs_and_fixed_diagnostics(self):
        self.plan_path.chmod(0o644)
        self.rejected(self.cli(), "INPUT_INVALID")
        self.plan_path.chmod(0o600)
        self.plan_path.write_bytes(b" " * 262145)
        self.rejected(self.cli(), "INPUT_INVALID")
        self.plan_path.write_bytes(b'{"SECRET_INPUT_CANARY":')
        result = self.cli()
        self.rejected(result, "INPUT_INVALID")
        self.assertNotIn("SECRET_INPUT_CANARY", result.stderr)
        self.plan_path.unlink()
        self.plan_path.symlink_to(self.bundle / "manifest.json")
        self.rejected(self.cli(), "INPUT_INVALID")

    def test_duplicate_fields_and_nonfinite_json_rejected(self):
        for data in (b'{"host":"codex","host":"other"}', b'{"targetUid":NaN}'):
            self.plan_path.write_bytes(data)
            self.rejected(self.cli(), "INPUT_INVALID")

    def test_hardlinked_and_fifo_inputs_fail_without_blocking(self):
        link = self.directory / "hardlink"
        os.link(self.plan_path, link)
        self.rejected(self.cli(), "INPUT_INVALID")
        link.unlink()
        self.plan_path.unlink()
        os.mkfifo(self.plan_path)
        self.rejected(self.cli(), "INPUT_INVALID")

    def test_preview_never_executes_or_reads_credentials(self):
        secret = self.private("credential", b"SECRET_CREDENTIAL_CANARY")
        guard = (
            "import os,runpy,sys\n"
            f"os.environ['OBSIDIAN_MCP_READ_TOKEN_FILE'] = {str(secret)!r}\n"
            f"os.environ['OBSIDIAN_MCP_WRITE_TOKEN_FILE'] = {str(secret)!r}\n"
            "def audit(event,args):\n"
            "    if event in ('subprocess.Popen','os.system','os.exec','os.posix_spawn'): raise AssertionError('execution forbidden')\n"
            "    if event in ('os.mkdir','os.rename','os.remove','os.chmod'): raise AssertionError('mutation forbidden')\n"
            "    if event == 'open' and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT): raise AssertionError('write forbidden')\n"
            f"    if event == 'open' and args[0] == {str(secret)!r}: raise AssertionError('credential read forbidden')\n"
            "sys.addaudithook(audit)\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0],run_name='__main__')\n"
        )
        result = self.cli(guard=guard)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET_CREDENTIAL_CANARY", result.stdout + result.stderr)

    def test_partial_output_retained_with_fixed_actionable_diagnostic(self):
        output = self.directory / "partial"
        guard = (
            "import os,runpy,sys\n"
            "def fail_sync(_fd): raise OSError('SECRET_FAILURE_CANARY')\n"
            "os.fsync = fail_sync\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0],run_name='__main__')\n"
        )
        result = self.cli("--output", output, guard=guard)
        self.rejected(result, "OUTPUT_PARTIAL")
        self.assertNotIn("SECRET_FAILURE_CANARY", result.stderr)
        self.assertIn("--output", result.stderr)
        self.assertTrue(output.is_dir())
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        self.rejected(self.cli("--output", output), "OUTPUT_INVALID")
        self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_bundle_digest_tamper_and_supplied_installer_never_executed(self):
        self.rejected(self.cli(sha="0" * 64), "BUNDLE_UNTRUSTED")
        bundle = self.directory / "tampered"
        shutil.copytree(self.bundle, bundle)
        marker = self.directory / "EXECUTED"
        (bundle / "bin/obsidian-mcp-client-install").write_text(f'open({str(marker)!r}, "w").write("executed")\n')
        self.rejected(self.cli(bundle=bundle), "BUNDLE_UNTRUSTED")
        self.assertFalse(marker.exists())

    def test_even_pinned_supplied_installer_is_data_not_executed(self):
        bundle = self.directory / "repinned"
        shutil.copytree(self.bundle, bundle)
        marker = self.directory / "EXECUTED"
        (bundle / "bin/obsidian-mcp-client-install").write_text(f'open({str(marker)!r}, "w").write("executed")\n')
        packer = runpy.run_path(str(ROOT / "scripts/pack-obsidian-mcp-client.py"))
        payload = {p.relative_to(bundle).as_posix(): p.read_bytes()
                   for p in bundle.rglob("*") if p.is_file() and p.name != "release.json"}
        metadata = packer["release_metadata"](payload)
        (bundle / "release.json").write_bytes(metadata)
        plan = dict(self.plan, bundlePath="/opt/obsidian-mcp/" + json.loads(metadata)["releaseId"])
        result = self.cli(bundle=bundle, sha=hashlib.sha256(metadata).hexdigest(), plan=plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_unsafe_outputs_do_not_write(self):
        link = self.directory / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        for output in [ROOT / "export-should-not-exist", self.bundle / "new", link / "export", self.directory / "missing" / "export"]:
            with self.subTest(output=output):
                self.rejected(self.cli("--output", output), "OUTPUT_INVALID")
                self.assertFalse(output.exists())

    def test_source_alias_cannot_export_inside_verified_bundle(self):
        source = self.directory / "source"
        shutil.copytree(self.bundle, source)
        walk = self.directory / "walk"
        walk.mkdir()
        alias = walk / ".." / "source"
        before = {p.relative_to(source).as_posix(): p.read_bytes()
                  for p in source.rglob("*") if p.is_file()}
        self.rejected(self.cli("--output", source / "export", bundle=alias), "OUTPUT_INVALID")
        self.assertFalse((source / "export").exists())
        self.assertEqual(before, {p.relative_to(source).as_posix(): p.read_bytes()
                                  for p in source.rglob("*") if p.is_file()})
        self.assertEqual(self.cli(bundle=source).returncode, 0)

    def test_outputs_refuse_unsafe_parents_checkouts_and_existing_leaves(self):
        unsafe = self.directory / "unsafe"
        unsafe.mkdir()
        unsafe.chmod(0o777)
        checkout = self.directory / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: fixture")
        existing = self.private("existing", b"retained")
        link = self.directory / "existing-link"
        link.symlink_to(existing)
        for output in [unsafe / "new", checkout / "new", existing, link]:
            with self.subTest(output=output):
                self.rejected(self.cli("--output", output), "OUTPUT_INVALID")
        self.assertEqual(existing.read_bytes(), b"retained")
        self.assertTrue(link.is_symlink())
        self.assertFalse((unsafe / "new").exists())
        self.assertFalse((checkout / "new").exists())

    def test_cli_errors_never_reflect_arbitrary_arguments(self):
        result = self.cli("--SECRET_CLI_CANARY")
        self.rejected(result, "CLI_INVALID")
        self.assertNotIn("SECRET_CLI_CANARY", result.stderr)


if __name__ == "__main__":
    unittest.main()
