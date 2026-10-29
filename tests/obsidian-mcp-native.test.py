#!/usr/bin/env python3
from contextlib import contextmanager
import hashlib
import json
import os
import re
import runpy
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class NativeCaptureFixture(unittest.TestCase):
    expected_rewrite_decision = "allow"
    initialize_git = True

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.bundle = cls.root / "client"
        subprocess.run(
            [
                "python3",
                str(ROOT / "scripts/pack-obsidian-mcp-client.py"),
                "--output",
                str(cls.bundle),
            ],
            check=True,
            capture_output=True,
        )
        cls.digest = hashlib.sha256(
            (cls.bundle / "release.json").read_bytes()
        ).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.root)
        self.directory = Path(self.temp.name)
        self.project = self.directory / "project"
        self.project.mkdir()
        self.home = self.directory / "codex"
        self.environment = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        if self.initialize_git:
            self.git("init", "-b", "native-fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.project), *args],
            env=self.environment,
            text=True,
            capture_output=True,
            check=True,
        )

    def cli(self, *args, payload=None):
        return subprocess.run(
            [str(self.bundle / "bin/obsidian-mcp-native"), *args],
            input=json.dumps(payload) if payload is not None else "",
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def configure(self, **extra):
        result = self.cli(
            "create",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Projects/Development/fixture/native",
            "--release-sha256",
            extra.get("digest", self.digest),
            *(["--project-mode", extra["mode"]] if "mode" in extra else []),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def event(self, name, **extra):
        return dict(
            session_id="session-1",
            turn_id="turn-1",
            cwd=str(self.project),
            hook_event_name=name,
            **extra,
        )

    def session_key(self, name):
        return hashlib.sha256(name.encode()).hexdigest()

    def state_paths(self):
        return [
            path
            for path in (self.home / "capture-state").glob("*.json")
            if path.name != "active.json"
        ]

    def hook(self, name, **extra):
        if extra.get("tool_name", "").startswith("mcp__"):
            extra.setdefault("tool_use_id", "fixture-mcp-call")
        result = self.cli(
            "hook", "--home", str(self.home), payload=self.event(name, **extra)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout or "{}")

    def pending_commit(self):
        args = dict(
            tool_name="Bash",
            tool_use_id="commit-call",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = self.hook("PreToolUse", **args)
        (self.project / "file.txt").write_text("fixture\n")
        self.git("add", "file.txt")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.hook("PostToolUse", **args, tool_response=executed.stdout)
        path = next(iter(self.state_paths()))
        state = json.loads(path.read_text())
        return path, state, next(iter(state["commits"]))

    def execute_rewritten(self, rewritten):
        self.assertEqual(
            rewritten["hookSpecificOutput"].get("permissionDecision"),
            self.expected_rewrite_decision,
        )
        return subprocess.run(
            shlex.split(rewritten["hookSpecificOutput"]["updatedInput"]["command"]),
            env=self.environment,
            cwd=self.project,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def load_native(self):
        script = self.bundle / "bin/obsidian-mcp-native"
        module = types.ModuleType("native_under_test")
        module.__file__ = str(script)
        exec(compile(script.read_text(), str(script), "exec"), module.__dict__)
        return module

    def prepare(self, path, commit, payload=None):
        return self.cli(
            "prepare",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--commit",
            commit,
            payload=payload
            or {
                "context": [
                    "Implemented fixture change",
                    "Verified production entry point",
                ]
            },
        )

    def append_event(self, arguments, status="committed", call="append-1"):
        target = arguments["target_path"]
        partial = status == "partial"
        receipt = dict(
            status=status,
            request_id="fixture-request",
            idempotency_key=arguments["idempotency_key"],
            path=target,
            affected_paths=[target],
            warnings=[],
            recovery={
                "required": partial,
                "action": "inspect fixture" if partial else "",
            },
            error_code="PARTIAL" if partial else None,
            retryable=partial,
        )
        event = dict(
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_use_id=call,
            tool_input=arguments,
        )
        before = self.hook("PreToolUse", **event)
        self.assertNotIn("permissionDecision", before.get("hookSpecificOutput", {}))
        return self.hook(
            "PostToolUse",
            **event,
            tool_response={"structuredContent": receipt, "isError": partial},
        )


class NativeCaptureTest(NativeCaptureFixture):
    def test_codex_direct_append_still_requires_cli_preparation(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, state, commit = self.pending_commit()
        consumer = runpy.run_path(str(self.bundle / "bin/obsidian-mcp-capture-commit"))
        _, arguments = consumer["capture_arguments"](
            state["commits"][commit]["metadata"], ["A fact"]
        )
        before = path.read_bytes()
        self.assertEqual(
            self.hook(
                "PreToolUse",
                tool_name="mcp__obsidian_write__obsidian_daily_append",
                tool_input=arguments,
            ),
            {},
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("native_arguments", json.loads(before)["commits"][commit])

    def assert_transient_history_retained(self, staged):
        self.git(
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-m",
            "baseline",
        )
        self.configure()
        self.hook("UserPromptSubmit", prompt="Exercise no-op effect verification")
        path = next(iter(self.state_paths()))
        self.assertEqual(
            self.cli(
                "skip",
                "--home",
                str(self.home),
                "--session",
                path.stem,
                "--turn",
                json.loads(path.read_text())["turn"],
                "--reason",
                "user_requested_no_capture",
            ).returncode,
            0,
        )
        self.assertTrue(
            json.loads(self.cli("drain-status", "--home", str(self.home)).stdout)[
                "quiescent"
            ]
        )
        if staged:
            (self.project / "change.txt").write_text("staged for a dry run\n")
            self.git("add", "change.txt")
        event = dict(
            tool_name="Bash",
            tool_use_id="transient-history",
            tool_input={"command": "git commit --dry-run"},
        )
        rewritten = self.hook("PreToolUse", **event)
        self.git(
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-m",
            "external transient",
        )
        self.git("reset", "--soft", "HEAD~")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 64, executed.stderr)
        self.hook("PostToolUse", **event, tool_response=executed.stdout)
        state = json.loads(path.read_text())
        self.assertTrue(state["snapshots"])
        self.assertFalse(state["commits"])
        (snapshot,) = state["snapshots"].values()
        self.assertEqual(snapshot["exit_code"], 0 if staged else 1)
        self.assertIn("capture_error", snapshot)
        self.assertFalse(
            json.loads(self.cli("drain-status", "--home", str(self.home)).stdout)[
                "quiescent"
            ]
        )
        self.assertEqual(self.execute_rewritten(rewritten).returncode, 64)

    def test_successful_dry_run_cannot_clear_transient_history(self):
        self.assert_transient_history_retained(staged=True)

    def test_failed_dry_run_cannot_clear_transient_history(self):
        self.assert_transient_history_retained(staged=False)

    def test_post_without_executor_cannot_discard_changed_git_effects(self):
        self.git(
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-m",
            "baseline",
        )
        self.configure()
        self.hook("UserPromptSubmit", prompt="Observe an executor that did not run")
        path = next(iter(self.state_paths()))
        event = dict(
            tool_name="Bash",
            tool_use_id="unstarted-executor",
            tool_input={"command": "git commit --dry-run"},
        )
        self.hook("PreToolUse", **event)
        self.git(
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-m",
            "outside executor",
        )
        output = self.hook("PostToolUse", **event, tool_response="not executor output")
        self.assertIn("unresolved", output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(json.loads(path.read_text())["snapshots"])
        self.assertFalse(json.loads(path.read_text())["commits"])

    def test_new_prompt_cannot_erase_uncaptured_resolution_evidence(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Original work")
        path, snapshot = self.live_snapshot()
        resolved = self.cli(
            "resolve-snapshot",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--snapshot",
            snapshot,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        skipped = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--turn",
            "turn-1",
            "--reason",
            "user_requested_no_capture",
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        before = path.read_bytes()
        next_prompt = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                self.event("UserPromptSubmit", prompt="Another task"), turn_id="turn-2"
            ),
        )
        self.assertEqual(next_prompt.returncode, 64)
        self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", next_prompt.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(
            json.loads(self.cli("drain-status", "--home", str(self.home)).stdout)[
                "quiescent"
            ]
        )

    def test_native_commit_preparation_receipt_replay_and_changed_payload(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, state, commit = self.pending_commit()
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        arguments = json.loads(prepared.stdout)["arguments"]
        pending = state["commits"][commit]
        self.assertEqual(arguments["skip_if_hash"], pending["event"]["commit"])
        self.assertEqual(
            arguments["target_path"],
            "Projects/Development/local/project/" + pending["metadata"]["date"] + ".md",
        )
        self.append_event(arguments)
        self.assertEqual(
            json.loads(self.prepare(path, commit).stdout)["status"], "skipped"
        )
        changed = self.prepare(path, commit, {"context": ["Different payload"]})
        self.assertEqual(changed.returncode, 64)
        self.assertIn("COMMIT_PAYLOAD_CHANGED", changed.stderr)

    def test_run_rejects_config_project_and_profile_overrides(self):
        self.configure()
        for option in (
            "-cfeatures.hooks=false",
            "-C/private/tmp",
            "-pother",
            "--config=features.hooks=false",
            "--cd=/private/tmp",
            "--profile=other",
        ):
            with self.subTest(option=option):
                result = self.cli("run", "--home", str(self.home), option)
                self.assertEqual(result.returncode, 64)
                self.assertIn("PROFILE_OVERRIDE_REFUSED", result.stderr)

    def test_run_isolates_profile_home_xdg_cwd_and_forwards_arguments(self):
        self.configure()
        fake_bin = self.directory / "bin"
        fake_bin.mkdir()
        output = self.directory / "codex-observed.json"
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['NATIVE_FIXTURE_OUTPUT']).write_text(json.dumps({"
            "'cwd': os.getcwd(), 'argv': sys.argv[1:], 'environment': {"
            "key: os.environ[key] for key in ('HOME', 'CODEX_HOME', "
            "'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')}}))\n"
        )
        codex.chmod(0o700)
        self.environment.update(
            PATH=str(fake_bin) + os.pathsep + self.environment["PATH"],
            NATIVE_FIXTURE_OUTPUT=str(output),
        )
        result = self.cli(
            "run", "--home", str(self.home), "exec", "--json", "fixture prompt"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(output.read_text())
        self.assertEqual(observed["cwd"], str(self.project))
        self.assertEqual(observed["argv"], ["exec", "--json", "fixture prompt"])
        runtime = self.home / "runtime-home"
        self.assertEqual(
            observed["environment"],
            {
                "HOME": str(runtime),
                "CODEX_HOME": str(self.home),
                "XDG_CONFIG_HOME": str(runtime / ".config"),
                "XDG_DATA_HOME": str(runtime / ".local/share"),
                "XDG_STATE_HOME": str(runtime / ".local/state"),
                "XDG_CACHE_HOME": str(runtime / ".cache"),
            },
        )

    def test_native_trust_records_are_allowed_but_capture_config_drift_is_rejected(
        self,
    ):
        self.configure()
        config = self.home / "config.toml"
        original = config.read_text()
        config.write_text(
            original
            + '\n[tui]\nscreen_reader_detection_done = true\n[projects."/fixture"]\ntrust_level = "trusted"\n[hooks.state."fixture-hook"]\ntrusted_hash = "sha256:fixture"\n'
        )
        self.hook("UserPromptSubmit", prompt="work")
        config.write_text(config.read_text().replace("hooks = true", "hooks = false"))
        result = self.cli("status", "--home", str(self.home))
        self.assertEqual(result.returncode, 64)
        self.assertIn("PROFILE_DRIFT", result.stderr)

    def test_partial_commit_outcome_stays_visible_and_blocks_automatic_retry(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _state, commit = self.pending_commit()
        arguments = json.loads(self.prepare(path, commit).stdout)["arguments"]
        self.append_event(arguments, "partial")
        pending = json.loads(path.read_text())["commits"][commit]
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(pending["recovery"]["recovery"]["required"])
        denied = self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_use_id="unreviewed",
            tool_input=arguments,
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.prepare(path, commit).returncode, 64)

    def test_reprepare_preserves_full_arguments_across_branch_identity_changes(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _state, commit = self.pending_commit()
        before = json.loads(self.prepare(path, commit).stdout)
        self.git("branch", "-m", "renamed-fixture")
        self.git(
            "remote",
            "add",
            "origin",
            "git@github.com:different-owner/different-project.git",
        )
        after = json.loads(self.prepare(path, commit).stdout)
        self.assertEqual(before, after)

    def test_two_reflog_transitions_are_not_attributed_to_one_tool_call(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        args = dict(
            tool_name="Bash",
            tool_use_id="two-commits",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = self.hook("PreToolUse", **args)
        for number in range(2):
            (self.project / "file.txt").write_text(str(number))
            self.git("add", "file.txt")
            if number == 0:
                self.git("commit", "-m", "fixture")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 64)
        result = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=self.event("PostToolUse", **args, tool_response={"exit_code": 0}),
        )
        self.assertEqual(result.returncode, 0)
        state = json.loads(next(iter(self.state_paths())).read_text())
        self.assertEqual(state["commits"], {})

    def test_new_prompt_cannot_discard_unresolved_previous_turn(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        result = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                self.event("UserPromptSubmit", prompt="next"), turn_id="turn-2"
            ),
        )
        self.assertEqual(result.returncode, 64)
        state = json.loads(next(iter(self.state_paths())).read_text())
        self.assertEqual(state["turn"], "turn-1")

    def test_save_after_policy_skip_keeps_inflight_and_partial_work_unresolved(self):
        for outcome in ("inflight", "partial"):
            with self.subTest(outcome=outcome):
                self.home = self.directory / outcome
                self.configure()
                self.hook("UserPromptSubmit", prompt="work")
                path = next(iter(self.state_paths()))
                state = json.loads(path.read_text())
                skipped = self.cli(
                    "skip",
                    "--home",
                    str(self.home),
                    "--session",
                    path.stem,
                    "--turn",
                    "turn-1",
                    "--reason",
                    "no_meaningful_work",
                )
                self.assertEqual(skipped.returncode, 0, skipped.stderr)
                plan = state["session_save"]
                arguments = dict(
                    title=plan["title"],
                    body="Later meaningful work",
                    folder_hint=plan["folder"],
                    session_link_date=plan["date"],
                    idempotency_key=plan["idempotency_key"],
                    resolved=True,
                )
                event = dict(
                    tool_name="mcp__obsidian_write__obsidian_keeper_save",
                    tool_input=arguments,
                    tool_use_id="later-save",
                )
                self.assertEqual(self.hook("PreToolUse", **event), {})
                if outcome == "partial":
                    self.hook(
                        "PostToolUse",
                        **event,
                        tool_response={
                            "structuredContent": {
                                "status": "partial",
                                "error_code": "PARTIAL",
                            }
                        },
                    )
                self.assertEqual(
                    self.hook("Stop", stop_hook_active=False)["decision"], "block"
                )
                next_turn = self.cli(
                    "hook",
                    "--home",
                    str(self.home),
                    payload=dict(
                        self.event("UserPromptSubmit", prompt="next"), turn_id="turn-2"
                    ),
                )
                self.assertEqual(next_turn.returncode, 64)
                self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", next_turn.stderr)
                remaining = json.loads(path.read_text())
                self.assertEqual(remaining["turn"], "turn-1")
                self.assertEqual(remaining["session_request"], arguments)
                self.assertEqual(remaining["disposition"], "pending")
                self.assertNotIn("skip_reason", remaining)

    def test_profile_trust_ownership_and_rollback(self):
        self.configure()
        config = (self.home / "config.toml").read_text()
        self.assertIn('default_tools_approval_mode = "auto"', config)
        self.assertNotIn("plugin", config)
        hooks = json.loads((self.home / "hooks.json").read_text())["hooks"]
        for event in ("PreToolUse", "PostToolUse"):
            for tool in ("obsidian_keeper_save", "obsidian_daily_append"):
                self.assertIsNotNone(
                    re.fullmatch(
                        hooks[event][0]["matcher"], "mcp__obsidian_write__" + tool
                    )
                )
        self.assertEqual(
            self.cli(
                "create",
                "--home",
                str(self.home),
                "--project",
                str(self.project),
                "--folder",
                "Projects/Development/fixture/native",
                "--release-sha256",
                self.digest,
            ).returncode,
            64,
        )
        (self.home / "auth.json").write_text("fixture credentials")
        result = self.cli("disable", "--home", str(self.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / "hooks.json").exists())
        self.assertEqual((self.home / "auth.json").read_text(), "fixture credentials")
        self.assertEqual(
            self.cli(
                "hook",
                "--home",
                str(self.home),
                payload=self.event("Stop", stop_hook_active=False),
            ).returncode,
            64,
        )

    def test_wrong_digest_and_inherited_capture_are_rejected_without_profile(self):
        result = self.cli(
            "create",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Projects/Development/fixture/native",
            "--release-sha256",
            "0" * 64,
        )
        self.assertEqual(result.returncode, 64)
        self.assertFalse(self.home.exists())
        (self.project / ".codex").mkdir()
        result = self.cli(
            "create",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Projects/Development/fixture/native",
            "--release-sha256",
            self.digest,
        )
        self.assertEqual(result.returncode, 64)
        self.assertFalse(self.home.exists())

    def test_managed_release_supports_native_create_and_verifies_installed_trust(self):
        prefix = self.directory / "managed-client"
        installed = subprocess.run(
            [
                str(self.bundle / "bin/obsidian-mcp-client-install"),
                "--prefix",
                str(prefix),
                "--bundle",
                str(self.bundle),
                "--release-sha256",
                self.digest,
            ],
            env=self.environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.bundle = prefix / "current"
        wrong = self.cli(
            "create",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Projects/fixture",
            "--release-sha256",
            "0" * 64,
        )
        self.assertEqual(wrong.returncode, 64)
        self.assertIn("BUNDLE_UNTRUSTED", wrong.stderr)
        self.assertFalse(self.home.exists())
        self.configure()
        self.hook("UserPromptSubmit", prompt="installed capture")
        installed_readme = self.bundle / "README.md"
        installed_readme.chmod(0o600)
        installed_readme.write_text(installed_readme.read_text() + "tampered")
        rejected = self.cli("status", "--home", str(self.home))
        self.assertEqual(rejected.returncode, 64)
        self.assertIn("BUNDLE_UNTRUSTED", rejected.stderr)

    def test_profile_drift_blocks_hook_and_disable(self):
        self.configure()
        hooks = self.home / "hooks.json"
        hooks.write_text(hooks.read_text() + " ")
        self.assertEqual(self.cli("disable", "--home", str(self.home)).returncode, 64)
        self.assertTrue(hooks.exists())
        self.assertEqual(
            self.cli(
                "hook",
                "--home",
                str(self.home),
                payload=self.event("UserPromptSubmit", prompt="work"),
            ).returncode,
            64,
        )

    def test_stop_requires_receipt_and_never_promotes_attempt_to_success(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Review a design")
        first = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(first["decision"], "block")
        second = self.hook("Stop", stop_hook_active=True)
        self.assertIn("unresolved", second["systemMessage"])
        self.assertNotIn("decision", second)

    def test_session_receipt_requires_matching_key_path_and_backlinks(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Review a design")
        state = json.loads(next(iter(self.state_paths())).read_text())
        expected = state["session_save"]
        receipt = dict(
            status="committed",
            request_id="fixture-request",
            path=expected["path"],
            idempotency_key=expected["idempotency_key"],
            affected_paths=[expected["path"], expected["index"], expected["daily"]],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
            warnings=[],
        )
        args = dict(
            title=expected["title"],
            body="Meaningful planning",
            resolved=True,
            folder_hint=expected["folder"],
            session_link_date=expected["date"],
            idempotency_key=expected["idempotency_key"],
        )
        self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian_write__obsidian_keeper_save",
            tool_input=args,
        )
        for patch in (
            {"idempotency_key": "wrong"},
            {"path": "Projects/wrong-note.md"},
            {"affected_paths": [expected["path"]]},
            {"recovery": {"required": True}},
            {"status": "conflict"},
        ):
            self.hook(
                "PostToolUse",
                tool_name="mcp__obsidian_write__obsidian_keeper_save",
                tool_input=args,
                tool_response={"structuredContent": dict(receipt, **patch)},
            )
            self.assertIn(
                "unresolved", self.hook("Stop", stop_hook_active=True)["systemMessage"]
            )
        self.hook(
            "PostToolUse",
            tool_name="mcp__obsidian_write__obsidian_keeper_save",
            tool_input=args,
            tool_response={"structuredContent": receipt, "isError": False},
        )
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                self.event("UserPromptSubmit", prompt="More work"), turn_id="turn-2"
            ),
        )
        self.assertEqual(self.hook("Stop", stop_hook_active=False)["decision"], "block")

    def test_policy_skip_is_explicit_and_turn_bound(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="ok")
        state_file = next(iter(self.state_paths()))
        state = json.loads(state_file.read_text())
        key = state_file.stem
        wrong = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            key,
            "--turn",
            "wrong",
            "--reason",
            "no_meaningful_work",
        )
        self.assertEqual(wrong.returncode, 64)
        result = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            key,
            "--turn",
            state["turn"],
            "--reason",
            "no_meaningful_work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(state_file.read_text())["disposition"], "skipped_by_policy"
        )
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})

    def test_real_git_event_requires_matching_snapshot_and_single_commit_transition(
        self,
    ):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Implement a change")
        command = shlex.join(
            ["git", "-C", str(self.project), "commit", "-m", "fixture commit"]
        )
        args = dict(
            tool_name="Bash", tool_use_id="commit-1", tool_input={"command": command}
        )
        rewritten = self.hook("PreToolUse", **args)
        (self.project / "file.txt").write_text("first\n")
        self.git("add", "file.txt")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        result = self.hook("PostToolUse", **args, tool_response=executed.stdout)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("--commit", context)
        self.assertNotIn("captured successfully", context)
        state = json.loads(next(iter(self.state_paths())).read_text())
        self.assertEqual(len(state["commits"]), 1)
        pending = next(iter(state["commits"].values()))
        self.assertEqual(
            pending["event"]["commit"], self.git("rev-parse", "HEAD").stdout.strip()
        )
        self.assertEqual(pending["status"], "pending")
        replay = self.execute_rewritten(rewritten)
        self.assertEqual(replay.returncode, 64)
        self.hook("PostToolUse", **args, tool_response={"exit_code": 0})
        self.assertEqual(
            len(json.loads(next(iter(self.state_paths())).read_text())["commits"]),
            1,
        )

    def test_executor_failure_and_interrupted_execution_cannot_promote_raw_output(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        args = dict(
            tool_name="Bash",
            tool_use_id="no-changes",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = self.hook("PreToolUse", **args)
        failed = self.execute_rewritten(rewritten)
        self.assertNotEqual(failed.returncode, 0)
        self.hook("PostToolUse", **args, tool_response="Process exited with code 0")
        path = next(iter(self.state_paths()))
        self.assertEqual(json.loads(path.read_text())["commits"], {})
        rewritten = self.hook("PreToolUse", **dict(args, tool_use_id="interrupted"))
        state = json.loads(path.read_text())
        snapshot = next(iter(state["snapshots"].values()))
        snapshot["execution_started"] = True
        path.write_text(json.dumps(state))
        refused = self.execute_rewritten(rewritten)
        self.assertEqual(refused.returncode, 64)
        self.assertIn("COMMIT_EXECUTION_UNRESOLVED", refused.stderr)
        result = self.hook(
            "PostToolUse",
            **dict(args, tool_use_id="interrupted"),
            tool_response="Process exited with code 0",
        )
        self.assertIn(
            "outcome unavailable", result["hookSpecificOutput"]["additionalContext"]
        )
        self.assertTrue(json.loads(path.read_text())["snapshots"])

    def test_hooks_and_executor_pin_the_creator_python_when_shell_path_differs(self):
        self.configure()
        bad_bin = self.directory / "bin"
        bad_bin.mkdir()
        python = bad_bin / "python3"
        python.write_text("#!/bin/sh\nexit 77\n")
        python.chmod(0o755)
        self.environment["PATH"] = str(bad_bin) + os.pathsep + os.environ["PATH"]
        hooks = json.loads((self.home / "hooks.json").read_text())["hooks"]
        started = subprocess.run(
            shlex.split(hooks["UserPromptSubmit"][0]["hooks"][0]["command"]),
            input=json.dumps(self.event("UserPromptSubmit", prompt="work")),
            env=self.environment,
            cwd=self.project,
            text=True,
            capture_output=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        args = dict(
            tool_name="Bash",
            tool_use_id="pinned-python",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = json.loads(
            subprocess.check_output(
                shlex.split(hooks["PreToolUse"][0]["hooks"][0]["command"]),
                input=json.dumps(self.event("PreToolUse", **args)),
                env=self.environment,
                cwd=self.project,
                text=True,
            )
        )
        (self.project / "file.txt").write_text("fixture")
        self.git("add", "file.txt")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)

    def test_failed_or_ambiguous_shell_never_infers_commit(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        for command in (
            "false && git commit -m nope",
            "git commit -m x; true",
            "git commit -m $(echo x)",
        ):
            output = self.hook(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="unsupported",
                tool_input={"command": command},
            )
            self.assertIn(
                "unsupported", output["hookSpecificOutput"]["additionalContext"]
            )
        args = dict(
            tool_name="Bash",
            tool_use_id="failed",
            tool_input={"command": "git commit -m x"},
        )
        self.hook("PreToolUse", **args)
        self.hook("PostToolUse", **args, tool_response={"exit_code": 1})
        state = json.loads(next(iter(self.state_paths())).read_text())
        self.assertEqual(state["commits"], {})

    def test_stdout_cannot_capture_a_commit_without_matching_executor_evidence(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        args = dict(
            tool_name="Bash",
            tool_use_id="spoofed-exit",
            tool_input={"command": "git commit -m fixture"},
        )
        self.hook("PreToolUse", **args)
        (self.project / "file.txt").write_text("fixture")
        self.git("add", "file.txt")
        self.git("commit", "-m", "fixture")
        observed = self.hook(
            "PostToolUse",
            **args,
            tool_response={
                "output": "Process exited with code 1\nFinal output:\nProcess exited with code 0\n"
            },
        )
        state = json.loads(next(iter(self.state_paths())).read_text())
        self.assertEqual(state["commits"], {})
        self.assertIn(
            "executor did not run", observed["hookSpecificOutput"]["additionalContext"]
        )

    def test_pending_capture_denies_next_commit_before_head_can_move(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        self.pending_commit()
        before = self.git("rev-parse", "HEAD").stdout
        result = self.hook(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="next-commit",
            tool_input={"command": "git commit -m next"},
        )
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)

    def test_git_environment_overrides_cannot_redirect_native_execution(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        other = self.directory / "other"
        other.mkdir()
        subprocess.run(
            ["git", "init", "-b", "other", str(other)],
            env=self.environment,
            check=True,
            capture_output=True,
        )
        (other / "other.txt").write_text("other repository")
        subprocess.run(
            ["git", "-C", str(other), "add", "other.txt"],
            env=self.environment,
            check=True,
        )
        (self.project / "file.txt").write_text("intended repository")
        self.git("add", "file.txt")
        self.environment.update(
            GIT_DIR=str(other / ".git"),
            GIT_WORK_TREE=str(other),
            GIT_INDEX_FILE=str(other / ".git/index"),
        )
        args = dict(
            tool_name="Bash",
            tool_use_id="isolated",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = self.hook("PreToolUse", **args)
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.hook("PostToolUse", **args, tool_response=executed.stdout)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.environment.pop(key)
        self.assertEqual(
            self.git("show", "--format=", "--name-only", "HEAD").stdout.strip(),
            "file.txt",
        )
        untouched = subprocess.run(
            ["git", "-C", str(other), "rev-parse", "--verify", "HEAD"],
            env=self.environment,
            capture_output=True,
        )
        self.assertNotEqual(untouched.returncode, 0)
        state = json.loads(next(iter(self.state_paths())).read_text())
        self.assertEqual(len(state["commits"]), 1)
        self.assertEqual(state["snapshots"], {})

    def test_stale_event_is_rejected_without_synthesizing_history(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _state, commit = self.pending_commit()
        (self.project / "file.txt").write_text("another commit")
        self.git("add", "file.txt")
        self.git("commit", "-m", "external fixture commit")
        failure = self.prepare(path, commit)
        self.assertEqual(failure.returncode, 64)
        self.assertIn("COMMIT_EVENT_REJECTED", failure.stderr)
        pending = json.loads(path.read_text())["commits"][commit]
        self.assertEqual(pending["status"], "pending")
        self.assertNotIn("native_arguments", pending)

    def test_sibling_commit_waits_until_pending_capture_is_resolved_by_owner(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        path, _state, commit = self.pending_commit()
        second_prompt = dict(
            self.event("UserPromptSubmit", prompt="session B"), session_id="session-2"
        )
        second_key = self.session_key("session-2")
        started = self.cli("hook", "--home", str(self.home), payload=second_prompt)
        self.assertEqual(started.returncode, 0, started.stderr)
        commit_event = dict(
            self.event(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="second-commit",
                tool_input={"command": "git commit -m sibling"},
            ),
            session_id="session-2",
        )
        before = self.cli("hook", "--home", str(self.home), payload=commit_event)
        self.assertEqual(before.returncode, 0, before.stderr)
        decision = json.loads(before.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn(path.stem, decision["permissionDecisionReason"])
        self.assertIn(second_key, decision["permissionDecisionReason"])
        status = self.cli("status", "--home", str(self.home), "--session", path.stem)
        self.assertEqual(json.loads(status.stdout)["gate"]["session"], path.stem)
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        arguments = json.loads(prepared.stdout)["arguments"]
        self.append_event(arguments)
        released = self.cli("status", "--home", str(self.home), "--session", path.stem)
        self.assertIsNone(json.loads(released.stdout)["gate"])
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "committed"
        )
        allowed = self.cli("hook", "--home", str(self.home), payload=commit_event)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("updatedInput", json.loads(allowed.stdout)["hookSpecificOutput"])
        (self.project / "sibling.txt").write_text("sibling session change\n")
        self.git("add", "sibling.txt")
        executed = self.execute_rewritten(json.loads(allowed.stdout))
        self.assertEqual(executed.returncode, 0, executed.stderr)
        after = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                commit_event,
                hook_event_name="PostToolUse",
                tool_response=executed.stdout,
            ),
        )
        self.assertEqual(after.returncode, 0, after.stderr)
        second_path = next(p for p in self.state_paths() if p != path)
        second_commit = next(iter(json.loads(second_path.read_text())["commits"]))
        prepared_second = self.prepare(second_path, second_commit)
        self.assertEqual(prepared_second.returncode, 0, prepared_second.stderr)
        self.assertEqual(
            json.loads(prepared_second.stdout)["arguments"]["idempotency_key"],
            json.loads(second_path.read_text())["commits"][second_commit][
                "native_arguments"
            ]["idempotency_key"],
        )

    def test_takeover_marks_interrupted_owner_inspectable_and_recoverable(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        path, _state, commit = self.pending_commit()
        head = self.git("rev-parse", "HEAD").stdout.strip()
        second_prompt = dict(
            self.event("UserPromptSubmit", prompt="session B"), session_id="session-2"
        )
        second_key = self.session_key("session-2")
        started = self.cli("hook", "--home", str(self.home), payload=second_prompt)
        self.assertEqual(started.returncode, 0, started.stderr)
        commit_event = dict(
            self.event(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="second-commit",
                tool_input={"command": "git commit -m sibling"},
            ),
            session_id="session-2",
        )
        denied = self.cli("hook", "--home", str(self.home), payload=commit_event)
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        unsigned = self.cli(
            "takeover", "--home", str(self.home), "--session", second_key
        )
        self.assertEqual(unsigned.returncode, 64)
        self.assertIn("INTERRUPTED_VERIFICATION_REQUIRED", unsigned.stderr)
        recovered = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            second_key,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["owner"], path.stem)
        public = self.cli("status", "--home", str(self.home), "--session", path.stem)
        public_state = json.loads(public.stdout)
        self.assertIsNone(public_state["gate"])
        self.assertEqual(public_state["commits"][commit]["status"], "not_captured")
        self.assertEqual(public_state["commits"][commit]["event"]["commit"], head)
        resolution = public_state["snapshot_resolutions"][0]
        self.assertEqual(resolution["status"], "not_captured")
        self.assertEqual(resolution["taken_over_by"], second_key)
        self.assertEqual(resolution["commits"], [commit])
        blocked = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            second_key,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(blocked.returncode, 64)
        self.assertIn("INTERRUPTED_OWNERSHIP_REQUIRED", blocked.stderr)
        allowed = self.cli("hook", "--home", str(self.home), payload=commit_event)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        (self.project / "sibling.txt").write_text("rescued session change\n")
        self.git("add", "sibling.txt")
        executed = self.execute_rewritten(json.loads(allowed.stdout))
        self.assertEqual(executed.returncode, 0, executed.stderr)
        after = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                commit_event,
                hook_event_name="PostToolUse",
                tool_response=executed.stdout,
            ),
        )
        self.assertEqual(after.returncode, 0, after.stderr)
        second_path = next(p for p in self.state_paths() if p != path)
        second_commit = next(iter(json.loads(second_path.read_text())["commits"]))
        prepared_second = self.prepare(second_path, second_commit)
        self.assertEqual(prepared_second.returncode, 0, prepared_second.stderr)
        stale_owner = self.prepare(path, commit)
        self.assertEqual(stale_owner.returncode, 0, stale_owner.stderr)
        self.assertEqual(
            json.loads(stale_owner.stdout), {"status": "skipped", "path": None}
        )

    def sibling_commit_event(self):
        started = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                self.event("UserPromptSubmit", prompt="session B"),
                session_id="session-2",
            ),
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        return dict(
            self.event(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="second-commit",
                tool_input={"command": "git commit -m sibling"},
            ),
            session_id="session-2",
        )

    def test_losing_second_claim_rolls_back_its_snapshot_and_denies(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        path, _state, _commit = self.pending_commit()
        commit_event = self.sibling_commit_event()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        real = native.active_request
        calls = []

        def unowned_at_first_check(directory):
            calls.append(directory)
            return None if len(calls) == 1 else real(directory)

        with mock.patch.object(native, "active_request", unowned_at_first_check):
            decision = native.hook(home, profile, commit_event)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            path.stem, decision["hookSpecificOutput"]["permissionDecisionReason"]
        )
        second_path = next(p for p in self.state_paths() if p != path)
        self.assertEqual(json.loads(second_path.read_text())["snapshots"], {})

    def test_disable_removes_the_gate_file(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        self.pending_commit()
        gate = self.home / "capture-state" / "active.json"
        self.assertTrue(gate.exists())
        self.assertEqual(self.cli("disable", "--home", str(self.home)).returncode, 0)
        self.assertFalse(gate.exists())

    def test_corrupt_gate_file_keeps_the_commit_event_and_takeover_clears_it(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        args = dict(
            tool_name="Bash",
            tool_use_id="commit-call",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = self.hook("PreToolUse", **args)
        (self.project / "file.txt").write_text("fixture\n")
        self.git("add", "file.txt")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        gate = self.home / "capture-state" / "active.json"
        gate.write_text("not json")
        after = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=self.event("PostToolUse", **args, tool_response=executed.stdout),
        )
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn("ACTIVE_STATE_INVALID", after.stdout)
        path = next(iter(self.state_paths()))
        state = json.loads(path.read_text())
        self.assertEqual(state["snapshots"], {})
        self.assertEqual([c["status"] for c in state["commits"].values()], ["pending"])
        unsigned = self.cli(
            "takeover", "--home", str(self.home), "--session", path.stem
        )
        self.assertEqual(unsigned.returncode, 64)
        self.assertTrue(gate.exists())
        cleared = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(json.loads(cleared.stdout)["status"], "cleared_invalid_gate")
        self.assertFalse(gate.exists())

    def test_takeover_aborts_when_the_owner_releases_before_its_state_is_locked(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        path, _state, commit = self.pending_commit()
        second_key = self.session_key("session-2")
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        gate = self.home / "capture-state" / "active.json"
        real = native.state_lock

        @contextmanager
        def owner_releases(directory, key):
            gate.unlink()
            with real(directory, key) as held:
                yield held

        arguments = types.SimpleNamespace(
            verification="command-stopped-and-history-inspected", session=second_key
        )
        with mock.patch.object(native, "state_lock", owner_releases):
            with self.assertRaises(native.NativeError) as raised:
                native.takeover(home, profile, arguments)
        self.assertEqual(str(raised.exception), "INTERRUPTED_OWNERSHIP_CHANGED")
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "pending"
        )

    def test_owner_reclaim_keeps_the_original_since(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, state, _commit = self.pending_commit()
        gate = self.home / "capture-state" / "active.json"
        record = json.loads(gate.read_text())
        record["since"] = 1
        gate.write_text(json.dumps(record))
        native = self.load_native()
        home, _profile = native.load_profile(self.home)
        self.assertIsNone(native.claim_gate(home, path.stem, state))
        self.assertEqual(json.loads(gate.read_text())["since"], 1)

    def test_partial_commit_can_retry_only_original_payload_after_explicit_inspection(
        self,
    ):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _state, commit = self.pending_commit()
        marker = "PRIVATE-COMMIT-CONTEXT-6291"
        prepared = self.prepare(path, commit, {"context": [marker]})
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        arguments = json.loads(prepared.stdout)["arguments"]

        def assert_status_redacted():
            before = path.read_bytes()
            public = self.cli(
                "status", "--home", str(self.home), "--session", path.stem
            )
            self.assertEqual(public.returncode, 0, public.stderr)
            self.assertNotIn(marker, public.stdout)
            self.assertNotIn(
                "native_arguments", json.loads(public.stdout)["commits"][commit]
            )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                json.loads(before)["commits"][commit]["native_arguments"], arguments
            )

        assert_status_redacted()
        self.append_event(arguments, "partial")
        assert_status_redacted()
        pending = json.loads(path.read_text())["commits"][commit]
        retry_args = (
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--commit",
            commit,
            "--payload-sha256",
            pending["payload_digest"],
        )
        self.assertEqual(self.cli(*retry_args).returncode, 64)
        result = self.cli(*retry_args, "--verification", "authoritative-path-inspected")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["arguments"], arguments)
        self.append_event(arguments, call="reviewed-append")
        assert_status_redacted()
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "committed"
        )

    def test_session_partial_preserves_payload_and_blocks_changed_or_unreviewed_retry(
        self,
    ):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path = next(iter(self.state_paths()))
        state = json.loads(path.read_text())
        plan = state["session_save"]
        args = dict(
            title=plan["title"],
            folder_hint=plan["folder"],
            body="Exact planning context",
            resolved=True,
            session_link_date=plan["date"],
            idempotency_key=plan["idempotency_key"],
        )
        event = dict(
            tool_name="mcp__obsidian_write__obsidian_keeper_save", tool_input=args
        )
        self.hook("PreToolUse", **event)
        self.hook(
            "PostToolUse",
            **event,
            tool_response={
                "isError": True,
                "structuredContent": {
                    "status": "partial",
                    "error_code": "PARTIAL",
                    "recovery": {"required": True},
                },
            },
        )
        state = json.loads(path.read_text())
        self.assertEqual(state["session_request"], args)
        self.assertEqual(state["session_outcome"]["status"], "partial")
        denied = self.hook("PreToolUse", **event)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        result = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--payload-sha256",
            state["session_payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(json.loads(result.stdout), args)
        changed = self.hook(
            "PreToolUse",
            tool_name=event["tool_name"],
            tool_input=dict(args, body="Changed"),
        )
        self.assertEqual(changed["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.hook("PreToolUse", **event), {})
        public = self.cli("status", "--home", str(self.home), "--session", path.stem)
        self.assertNotIn("Exact planning context", public.stdout)
        skip = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--turn",
            state["turn"],
            "--reason",
            "no_meaningful_work",
        )
        self.assertEqual(skip.returncode, 64)
        next_prompt = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                self.event("UserPromptSubmit", prompt="new"), turn_id="turn-2"
            ),
        )
        self.assertEqual(next_prompt.returncode, 64)
        self.assertEqual(json.loads(path.read_text())["session_request"], args)

    def test_interleaved_pre_events_deny_second_call_and_canceled_snapshot_is_resolvable(
        self,
    ):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        args = dict(
            tool_name="Bash",
            tool_use_id="first",
            tool_input={"command": "git commit -m first"},
        )
        rewritten = self.hook("PreToolUse", **args)
        self.assertIn("updatedInput", rewritten["hookSpecificOutput"])
        self.assertEqual(self.hook("PreToolUse", **args), rewritten)
        denied = self.hook(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="second",
            tool_input={"command": "git commit -m second"},
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        path = next(iter(self.state_paths()))
        snapshot = next(iter(json.loads(path.read_text())["snapshots"]))
        result = self.cli(
            "resolve-snapshot",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--snapshot",
            snapshot,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(path.read_text())
        self.assertEqual(state["snapshot_resolutions"][0]["status"], "not_captured")
        self.assertEqual(state["snapshots"], {})
        self.assertIn(
            "updatedInput",
            self.hook("PreToolUse", **dict(args, tool_use_id="third"))[
                "hookSpecificOutput"
            ],
        )

    def commit_args(self, call):
        return dict(
            tool_name="Bash",
            tool_use_id=call,
            tool_input={"command": "git commit -m fixture"},
        )

    def live_snapshot(self, call="commit-call"):
        rewritten = self.hook("PreToolUse", **self.commit_args(call))
        self.assertIn("updatedInput", rewritten["hookSpecificOutput"])
        path = next(iter(self.state_paths()))
        return path, next(iter(json.loads(path.read_text())["snapshots"]))

    def test_state_file_stem_matches_derived_session_key(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _snapshot = self.live_snapshot()
        self.assertEqual(path.stem, self.session_key("session-1"))

    def resolve(self, key, snapshot):
        return self.cli(
            "resolve-snapshot",
            "--home",
            str(self.home),
            "--session",
            key,
            "--snapshot",
            snapshot,
            "--verification",
            "command-stopped-and-history-inspected",
        )

    def test_resolve_snapshot_releases_the_gate(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        key = self.session_key("session-1")
        _path, snapshot = self.live_snapshot()
        self.assertTrue((self.home / "capture-state" / "active.json").exists())
        resolved = self.resolve(key, snapshot)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        result = json.loads(resolved.stdout)
        self.assertEqual(result["status"], "not_captured")
        self.assertNotIn("note", result)
        public = json.loads(
            self.cli("status", "--home", str(self.home), "--session", key).stdout
        )
        self.assertIsNone(public["gate"])
        self.assertEqual(public["snapshots"], {})

    def test_resolve_snapshot_warns_instead_of_failing_on_invalid_gate(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        key = self.session_key("session-1")
        _path, snapshot = self.live_snapshot()
        (self.home / "capture-state" / "active.json").write_text("not json")
        resolved = self.resolve(key, snapshot)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        result = json.loads(resolved.stdout)
        self.assertEqual(result["status"], "not_captured")
        self.assertIn("ACTIVE_STATE_INVALID", result["note"])

    def test_dormant_executor_pops_snapshot_and_releases_gate(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        first_key = self.session_key("session-1")
        path, _snapshot = self.live_snapshot()
        gate = self.home / "capture-state" / "active.json"
        self.assertTrue(gate.exists())
        after = self.hook("PostToolUse", **self.commit_args("commit-call"))
        self.assertIn(
            "Commit executor did not run",
            after["hookSpecificOutput"]["additionalContext"],
        )
        public = json.loads(
            self.cli("status", "--home", str(self.home), "--session", first_key).stdout
        )
        self.assertIsNone(public["gate"])
        self.assertEqual(public["snapshots"], {})
        self.assertEqual(json.loads(path.read_text())["snapshots"], {})

    def test_takeover_carries_live_snapshots(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        self.git("commit", "--allow-empty", "-m", "base")
        path, snapshot = self.live_snapshot()
        head = self.git("rev-parse", "HEAD").stdout.strip()
        second_key = self.session_key("session-2")
        started = self.cli(
            "hook",
            "--home",
            str(self.home),
            payload=dict(
                self.event("UserPromptSubmit", prompt="session B"),
                session_id="session-2",
            ),
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        recovered = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            second_key,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        result = json.loads(recovered.stdout)
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["owner"], path.stem)
        self.assertEqual(result["snapshots"], [snapshot])
        self.assertEqual(result["observed_head"], head)
        self.assertFalse(result["head_unavailable"])
        public = json.loads(
            self.cli("status", "--home", str(self.home), "--session", path.stem).stdout
        )
        record = public["snapshot_resolutions"][0]
        self.assertEqual(list(record["snapshots"]), [snapshot])
        self.assertEqual(public["snapshots"], {})

    def test_takeover_of_owner_without_pending_work_is_distinct(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        self.pending_commit()
        path = next(iter(self.state_paths()))
        owner_state = json.loads(path.read_text())
        owner_state["commits"] = {}
        owner_state["snapshots"] = {}
        path.write_text(json.dumps(owner_state))
        second_key = self.session_key("session-2")
        recovered = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            second_key,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        result = json.loads(recovered.stdout)
        self.assertEqual(result["status"], "recovered_no_pending")
        self.assertEqual(result["commits"], [])
        self.assertNotIn("snapshots", result)
        self.assertIsNone(
            json.loads(
                self.cli(
                    "status", "--home", str(self.home), "--session", second_key
                ).stdout
            )["gate"]
        )

    def test_gate_lock_blocks_across_processes(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, snapshot = self.live_snapshot()
        second_key = self.session_key("session-2")
        lock = self.home / "capture-state" / "active.lock"
        self.assertTrue(lock.exists())
        holder = subprocess.Popen(
            [
                "python3",
                "-c",
                "import fcntl,os,sys,time\n"
                "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR)\n"
                "fcntl.flock(fd,fcntl.LOCK_EX)\n"
                "print('HELD',flush=True)\n"
                "time.sleep(1.5)\n"
                "fcntl.flock(fd,fcntl.LOCK_UN)\n",
                str(lock),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = holder.stdout.readline()
            self.assertEqual(line.strip(), "HELD")
            started = time.monotonic()
            takeover = self.cli(
                "takeover",
                "--home",
                str(self.home),
                "--session",
                second_key,
                "--verification",
                "command-stopped-and-history-inspected",
            )
            elapsed = time.monotonic() - started
            self.assertEqual(takeover.returncode, 0, takeover.stderr)
            result = json.loads(takeover.stdout)
            self.assertEqual(result["status"], "recovered")
            self.assertEqual(result["snapshots"], [snapshot])
            self.assertGreaterEqual(elapsed, 1.2)
            self.assertEqual(holder.wait(timeout=10), 0)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()

    def test_daily_append_on_invalid_gate_warns_instead_of_failing(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _state, commit = self.pending_commit()
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        arguments = json.loads(prepared.stdout)["arguments"]
        gate = self.home / "capture-state" / "active.json"
        gate.write_text("not json")
        before = self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_use_id="append-1",
            tool_input=arguments,
        )
        self.assertNotIn("permissionDecision", before.get("hookSpecificOutput", {}))
        after = self.hook(
            "PostToolUse",
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_use_id="append-1",
            tool_input=arguments,
            tool_response={
                "structuredContent": dict(
                    status="committed",
                    request_id="fixture-request",
                    idempotency_key=arguments["idempotency_key"],
                    path=arguments["target_path"],
                    affected_paths=[arguments["target_path"]],
                    warnings=[],
                    recovery={"required": False, "action": ""},
                    error_code=None,
                    retryable=False,
                ),
                "isError": False,
            },
        )
        self.assertIn(
            "ACTIVE_STATE_INVALID", after["hookSpecificOutput"]["additionalContext"]
        )
        state = json.loads(path.read_text())
        self.assertEqual(state["commits"][commit]["status"], "committed")

    def foreign_gate(self):
        gate = self.home / "capture-state" / "active.json"
        gate.write_text(
            json.dumps(
                dict(
                    schemaVersion=1,
                    session=self.session_key("session-2"),
                    reason="commit_capture",
                    since=1,
                )
            )
        )

    def test_hook_surfaces_foreign_gate_owner_note(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _state, commit = self.pending_commit()
        arguments = json.loads(self.prepare(path, commit).stdout)["arguments"]
        self.foreign_gate()
        before = self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_use_id="append-1",
            tool_input=arguments,
        )
        note = before["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Unexpected capture-gate owner", note)
        self.assertIn(self.session_key("session-2"), note)

    def test_gate_note_is_kept_when_the_receipt_does_not_match(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, _state, commit = self.pending_commit()
        arguments = json.loads(self.prepare(path, commit).stdout)["arguments"]
        append = dict(
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_input=arguments,
        )
        self.hook("PreToolUse", tool_use_id="append-1", **append)
        (self.home / "capture-state" / "active.json").write_text("not json")
        after = self.hook(
            "PostToolUse",
            tool_use_id="append-2",
            tool_response={"isError": True},
            **append,
        )
        text = after["hookSpecificOutput"]["additionalContext"]
        self.assertIn("did not match", text)
        self.assertIn("ACTIVE_STATE_INVALID", text)

    def test_takeover_without_a_head_records_null_and_marks_it_unavailable(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        path, _snapshot = self.live_snapshot()
        second_key = self.session_key("session-2")
        recovered = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            second_key,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        result = json.loads(recovered.stdout)
        self.assertEqual(result["status"], "recovered")
        self.assertIsNone(result["observed_head"])
        self.assertTrue(result["head_unavailable"])
        record = json.loads(path.read_text())["snapshot_resolutions"][0]
        self.assertIsNone(record["observed_head"])

    def test_resolve_snapshot_without_a_head_records_null(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, snapshot = self.live_snapshot()
        resolved = self.cli(
            "resolve-snapshot",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--snapshot",
            snapshot,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        record = json.loads(path.read_text())["snapshot_resolutions"][0]
        self.assertIsNone(record["observed_head"])

    def test_takeover_of_owner_with_missing_state_file_is_not_reported_clean(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        path, _snapshot = self.live_snapshot()
        path.unlink()
        recovered = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            self.session_key("session-2"),
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        result = json.loads(recovered.stdout)
        self.assertEqual(result["status"], "recovered_owner_state_missing")
        self.assertEqual(result["owner"], path.stem)

    def test_takeover_rejects_a_malformed_session_key(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="session A")
        self.live_snapshot()
        recovered = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            "not-hex",
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertNotEqual(recovered.returncode, 0)
        self.assertIn("SESSION_KEY_INVALID", recovered.stderr)


class ClaudePluginCaptureTest(NativeCaptureFixture):
    expected_rewrite_decision = None

    def setUp(self):
        super().setUp()
        self.transcript = self.directory / "conversation.jsonl"
        self.transcript.write_text('{"type":"system","uuid":"start"}\n')
        (self.project / ".claude").mkdir()
        (self.project / ".claude/settings.json").write_text(
            '{"hooks":{},"custom":true}\n'
        )
        (self.project / "CLAUDE.md").write_text("Keep project policy\n")

    def configure(self, **extra):
        return self.cli(
            "attach",
            "--host",
            "claude-plugin",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Projects/Development/fixture/native",
            "--daily-folder",
            extra.get("daily", "Journal"),
            "--release-sha256",
            extra.get("digest", self.digest),
            *(["--project-mode", extra["mode"]] if "mode" in extra else []),
        )

    def event(self, name, **extra):
        return {
            **dict(
                session_id="session-1",
                cwd=str(self.project),
                transcript_path=str(self.transcript),
                hook_event_name=name,
            ),
            **extra,
        }

    def start(self):
        result = self.configure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.hook("UserPromptSubmit", prompt="Implement the change")
        return next(iter(self.state_paths()))

    def commit_arguments(self, path, items=None, *, output=None):
        if output is None:
            output = self.hook("SessionStart", source="resume")
        guidance = output["hookSpecificOutput"]["additionalContext"]
        encoded = next(
            line.removeprefix("Commit capture arguments: ")
            for line in guidance.split("\n")
            if line.startswith("Commit capture arguments: ")
        )
        arguments = json.loads(encoded)
        self.assertNotIn("pipe JSON", guidance)
        if items is not None:
            branch = arguments["content"].split("\n")[0]
            arguments["content"] = "\n".join([branch, *("- " + x for x in items)])
        return arguments

    def commit_event(self, arguments, call="direct-commit-write"):
        return dict(
            tool_name="mcp__obsidian-write__obsidian_daily_append",
            tool_use_id=call,
            tool_input=arguments,
        )

    def commit_receipt(self, arguments):
        return dict(
            status="committed",
            request_id="direct-fixture",
            idempotency_key=arguments["idempotency_key"],
            path=arguments["target_path"],
            affected_paths=[arguments["target_path"]],
            warnings=[],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
        )

    def test_direct_commit_prepares_and_settles_without_shell_prepare(self):
        path = self.start()
        _, state, commit = self.pending_commit()
        items = ["Implemented fixture change", "Verified Git effects", "Kept MCP scope"]
        arguments = self.commit_arguments(path, items)
        metadata = state["commits"][commit]["metadata"]
        self.assertEqual(arguments["skip_if_hash"], metadata["hash"])
        self.assertEqual(
            arguments["section"], f"## {metadata['time']} - {metadata['hash']}"
        )
        self.assertEqual(
            arguments["target_path"],
            f"Projects/Development/local/project/{metadata['date']}.md",
        )
        expected_key = hashlib.sha256(
            ("local/project\0" + metadata["hash"]).encode()
        ).hexdigest()
        self.assertEqual(arguments["idempotency_key"], "commit-capture:" + expected_key)
        event = self.commit_event(arguments)
        self.assertEqual(self.hook("PreToolUse", **event), {})
        pending = json.loads(path.read_text())["commits"][commit]
        self.assertEqual(pending["native_arguments"], arguments)
        self.assertEqual(pending["request"], {"context": items})
        self.assertEqual(pending["in_flight"], event["tool_use_id"])
        self.assertEqual(pending["status"], "pending")
        before = path.read_bytes()
        self.assertEqual(self.hook("PreToolUse", **event), {})
        self.assertEqual(path.read_bytes(), before)
        self.hook(
            "PostToolUse",
            **event,
            tool_response=json.dumps(self.commit_receipt(arguments)),
        )
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "committed"
        )
        self.save(path)
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])

    def test_direct_commit_rejects_noncanonical_arguments_without_preparing(self):
        path = self.start()
        _, _, commit = self.pending_commit()
        arguments = self.commit_arguments(
            path, ["First fact", "Second fact", "Third fact"]
        )
        branch = arguments["content"].split("\n")[0]
        changes = [
            {"target_path": "Other/forged.md"},
            {"skip_if_hash": arguments["skip_if_hash"][:8]},
            {"section": "## forged"},
            {"init_content": "forged frontmatter"},
            {"date": "2026-01-01"},
            {"request_id": "extra-field"},
            {"content": '- Branch: "forged"\n- A fact'},
            {"content": branch + "\nnot a bullet"},
            {"content": branch + "\n- "},
            {"content": branch + "\n- fact\n"},
            {"content": branch + "\n- fact\r"},
            {"content": branch + "\n- fact\tmore"},
            {"content": branch + "\n- fact\x7f"},
            {"content": branch + "\n- \ud800"},
            {"content": branch + "\n- " + "é" * 2049},
            {"content": branch + "\n- fact" * 9},
            {"content": None},
            {"content": self.commit_arguments(path)["content"]},
        ]
        before = path.read_bytes()
        for change in changes:
            with self.subTest(
                fields=list(change), content=repr(change.get("content"))[:80]
            ):
                denied = self.hook(
                    "PreToolUse", **self.commit_event(dict(arguments, **change))
                )
                self.assertEqual(
                    denied["hookSpecificOutput"]["permissionDecision"], "deny"
                )
                self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(
            "native_arguments", json.loads(path.read_text())["commits"][commit]
        )
        manual = dict(arguments, idempotency_key="manual-other-key")
        self.assertEqual(self.hook("PreToolUse", **self.commit_event(manual)), {})
        self.assertEqual(path.read_bytes(), before)

    def test_direct_commit_post_cannot_prepare_or_settle_without_matching_pre(self):
        path = self.start()
        _, _, commit = self.pending_commit()
        arguments = self.commit_arguments(path, ["A fact"])
        event = self.commit_event(arguments)
        before = path.read_bytes()
        self.hook("PostToolUse", **event, tool_response=self.commit_receipt(arguments))
        self.assertEqual(path.read_bytes(), before)
        self.hook("PreToolUse", **event)
        prepared = path.read_bytes()
        self.hook(
            "PostToolUse",
            **self.commit_event(arguments, "orphan"),
            tool_response=self.commit_receipt(arguments),
        )
        self.assertEqual(path.read_bytes(), prepared)
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "pending"
        )

    def test_direct_commit_retry_preserves_arguments_and_requires_recovery_review(self):
        path = self.start()
        _, _, commit = self.pending_commit()
        arguments = self.commit_arguments(path, ["Original fact"])
        event = self.commit_event(arguments)
        self.hook("PreToolUse", **event)
        self.git("branch", "-m", "renamed-after-preparation")
        self.git("remote", "add", "origin", "git@github.com:changed/identity.git")
        self.assertEqual(self.commit_arguments(path), arguments)
        for alternate in (
            dict(arguments, content=arguments["content"] + "\n- Changed"),
            arguments,
        ):
            denied = self.hook(
                "PreToolUse", **self.commit_event(alternate, "overlapping")
            )
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.hook("PostToolUseFailure", **event, error="fixture failure")
        before = path.read_bytes()
        self.assertEqual(self.commit_arguments(path), arguments)
        denied = self.hook("PreToolUse", **self.commit_event(arguments, "unreviewed"))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(path.read_bytes(), before)
        pending = json.loads(before)["commits"][commit]
        retry = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--commit",
            commit,
            "--payload-sha256",
            pending["payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["arguments"], arguments)
        event = self.commit_event(arguments, "reviewed")
        self.assertEqual(self.hook("PreToolUse", **event), {})
        self.hook("PostToolUse", **event, tool_response=self.commit_receipt(arguments))
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "committed"
        )

    def test_direct_commit_revalidates_head_before_preparation(self):
        path = self.start()
        self.pending_commit()
        arguments = self.commit_arguments(path, ["A fact"])
        self.git(
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-m",
            "external",
        )
        before = path.read_bytes()
        denied = self.hook("PreToolUse", **self.commit_event(arguments))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "COMMIT_EVENT_REJECTED",
            denied["hookSpecificOutput"]["permissionDecisionReason"],
        )
        self.assertEqual(path.read_bytes(), before)

    def test_direct_commit_accepts_existing_context_byte_and_item_limits(self):
        path = self.start()
        _, _, commit = self.pending_commit()
        items = ["é" * 2046 + "\u2028x"] + ["x" * 4096] * 7
        arguments = self.commit_arguments(path, items)
        self.assertEqual(self.hook("PreToolUse", **self.commit_event(arguments)), {})
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["request"],
            {"context": items},
        )

    def test_direct_commit_ambiguous_identity_denies_without_preparation(self):
        path = self.start()
        _, state, commit = self.pending_commit()
        arguments = self.commit_arguments(path, ["A fact"])
        state["commits"]["0" * 64] = dict(state["commits"][commit])
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        denied = self.hook("PreToolUse", **self.commit_event(arguments))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "COMMIT_EVENT_AMBIGUOUS",
            denied["hookSpecificOutput"]["permissionDecisionReason"],
        )
        self.assertEqual(path.read_bytes(), before)

    def test_direct_commit_persistence_failure_denies_before_write(self):
        path = self.start()
        self.pending_commit()
        arguments = self.commit_arguments(path, ["A fact"])
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        before = path.read_bytes()
        with mock.patch.object(
            native, "atomic_write", side_effect=OSError("fixture failure")
        ):
            denied = native.hook(
                home, profile, self.event("PreToolUse", **self.commit_event(arguments))
            )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.hook("PreToolUse", **self.commit_event(arguments)), {})

    def test_direct_commit_guidance_preserves_tool_like_metadata_and_context(self):
        path = self.start()
        branch = "codex/mcp__obsidian_write__obsidian_keeper_save"
        self.git("branch", "-m", branch)
        self.pending_commit()
        arguments = self.commit_arguments(
            path, ["Used mcp__obsidian_write__obsidian_daily_append literally"]
        )
        self.assertEqual(
            arguments["content"].split("\n")[0], "- Branch: " + json.dumps(branch)
        )
        self.assertEqual(self.hook("PreToolUse", **self.commit_event(arguments)), {})
        self.assertEqual(self.commit_arguments(path), arguments)

    def test_direct_commit_producer_timeout_denies_without_preparing(self):
        path = self.start()
        self.pending_commit()
        arguments = self.commit_arguments(path, ["A fact"])
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        consumer = runpy.run_path(str(self.bundle / "bin/obsidian-mcp-capture-commit"))
        run_path = native.runpy.run_path
        processes = []

        def load_runtime(script):
            return (
                consumer
                if Path(script).name == "obsidian-mcp-capture-commit"
                else run_path(script)
            )

        def stalled_producer(*args, **kwargs):
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs
            )
            processes.append(process)
            return process

        isolated_subprocess = types.SimpleNamespace(**vars(subprocess))
        isolated_subprocess.Popen = stalled_producer
        consumer["run_producer"].__globals__["subprocess"] = isolated_subprocess
        before = path.read_bytes()
        started = time.monotonic()
        try:
            with mock.patch.object(native.runpy, "run_path", side_effect=load_runtime):
                denied = native.hook(
                    home,
                    profile,
                    self.event("PreToolUse", **self.commit_event(arguments)),
                )
            self.assertLess(time.monotonic() - started, 7)
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual(path.read_bytes(), before)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        self.assertEqual(self.hook("PreToolUse", **self.commit_event(arguments)), {})

    def test_subdirectory_prompt_and_commit_preserve_relative_pathspec(self):
        child = self.project / "src"
        child.mkdir()
        for path in (self.project / "relative.txt", child / "relative.txt"):
            path.write_text("before\n")
        self.git("add", "relative.txt", "src/relative.txt")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-m", "initial fixture")
        attached = self.configure()
        self.assertEqual(attached.returncode, 0, attached.stderr)
        guidance = self.hook(
            "UserPromptSubmit", cwd=str(child), prompt="Capture work from src"
        )
        self.assertIn("obsidian_keeper_save", json.dumps(guidance))
        for path in (self.project / "relative.txt", child / "relative.txt"):
            path.write_text("after\n")
        self.git("add", "relative.txt", "src/relative.txt")
        event = dict(
            cwd=str(child),
            tool_name="Bash",
            tool_use_id="subdir-commit",
            tool_input={"command": "git commit -m fixture relative.txt"},
        )
        rewritten = self.hook("PreToolUse", **event)
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.hook("PostToolUse", **event, tool_response=executed.stdout)
        self.assertEqual(
            self.git("show", "--format=", "--name-only", "HEAD").stdout.strip(),
            "src/relative.txt",
        )
        self.assertEqual(
            self.git("diff", "--cached", "--name-only").stdout.strip(), "relative.txt"
        )
        path = next(iter(self.state_paths()))
        (pending,) = json.loads(path.read_text())["commits"].values()
        self.assertEqual(pending["metadata"]["files"], ["src/relative.txt"])

    def test_nested_foreign_repository_is_not_the_registered_project(self):
        self.configure()
        child = self.project / "nested"
        child.mkdir()
        self.git("-C", str(child), "init")
        output = self.hook(
            "UserPromptSubmit", cwd=str(child), prompt="Foreign nested repository"
        )
        self.assertNotIn("decision", output)
        self.assertIn("PROJECT_MISMATCH", json.dumps(output))
        self.assertFalse(self.state_paths())

    def test_failure_envelope_keeps_original_claude_event_name(self):
        path = self.start()
        arguments = self.session_arguments(path)
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="failure-envelope",
            tool_input=arguments,
        )
        self.hook("PreToolUse", **event)
        output = self.hook(
            "PostToolUseFailure",
            **dict(event, tool_use_id="orphan-failure"),
            error="Transport interrupted",
        )
        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure"
        )
        self.assertEqual(
            json.loads(path.read_text())["session_in_flight"], "failure-envelope"
        )

    def session_arguments(self, path):
        plan = json.loads(path.read_text())["session_save"]
        return dict(
            title=plan["title"],
            folder_hint=plan["folder"],
            body="Private session context",
            resolved=True,
            session_link_date=plan["date"],
            idempotency_key=plan["idempotency_key"],
        )

    def save(
        self,
        path,
        call="session-write",
        status="committed",
        array=False,
        bare=False,
        receipt_changes=None,
    ):
        args = self.session_arguments(path)
        plan = json.loads(path.read_text())["session_save"]
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id=call,
            tool_input=args,
        )
        self.assertEqual(self.hook("PreToolUse", **event), {})
        receipt = dict(
            status=status,
            request_id="fixture",
            idempotency_key=args["idempotency_key"],
            path=plan["path"],
            affected_paths=[plan["path"], plan["index"], plan["daily"]],
            warnings=[],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
        )
        receipt.update(receipt_changes or {})
        result = (
            [{"type": "text", "text": json.dumps(receipt)}]
            if array
            else {"structuredContent": receipt}
        )
        if bare:
            result = json.dumps(receipt)
        return self.hook("PostToolUse", **event, tool_response=result)

    def test_actual_claude_json_receipt_string_settles_capture_without_stop_loop(self):
        path = self.start()
        self.save(path, bare=True)
        state = json.loads(path.read_text())
        self.assertEqual(state["disposition"], "saved")
        self.assertFalse(state["session_recovery"])
        self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])

    def test_bare_host_receipt_still_requires_exact_path_and_schema(self):
        path = self.start()
        self.save(path, bare=True, receipt_changes={"path": "Other/forged.md"})
        state = json.loads(path.read_text())
        self.assertEqual(state["disposition"], "pending")
        self.assertTrue(state["session_recovery"])
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])

    def test_actual_claude_bare_commit_receipt_uses_shared_strict_validator(self):
        path = self.start()
        _path, _state, commit = self.pending_commit()
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        args = json.loads(prepared.stdout)["arguments"]
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_daily_append",
            tool_use_id="bare-commit",
            tool_input=args,
        )
        self.hook("PreToolUse", **event)
        receipt = dict(
            status="committed",
            request_id="fixture-bare",
            idempotency_key=args["idempotency_key"],
            path=args["target_path"],
            affected_paths=[args["target_path"]],
            warnings=[],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
        )
        self.hook("PostToolUse", **event, tool_response=json.dumps(receipt))
        pending = json.loads(path.read_text())["commits"][commit]
        self.assertEqual(pending["status"], "committed")
        self.assertFalse(pending.get("recovery"))

    def drain(self):
        return self.cli("drain-status", "--home", str(self.home))

    def test_capabilities_and_attach_preserve_host_files_and_verify_release(self):
        result = self.cli("capabilities", "--release-sha256", self.digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude-plugin", json.loads(result.stdout)["hosts"])
        self.assertEqual(json.loads(result.stdout)["apiVersion"], 1)
        self.assertEqual(
            json.loads(result.stdout)["projectModes"],
            {"claude-plugin": ["git", "directory"], "codex": ["git"]},
        )
        original = (self.project / ".claude/settings.json").read_bytes()
        result = self.configure(digest="0" * 64)
        self.assertEqual(result.returncode, 64)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.configure().returncode, 0)
        self.assertEqual(
            (self.project / ".claude/settings.json").read_bytes(), original
        )
        self.assertEqual(
            (self.project / "CLAUDE.md").read_text(), "Keep project policy\n"
        )
        self.assertEqual(
            set(p.name for p in self.home.iterdir()),
            {"capture-profile.json", "capture-state"},
        )
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        profile = self.cli("profile-status", "--home", str(self.home))
        self.assertEqual(profile.returncode, 0, profile.stderr)
        self.assertEqual(json.loads(profile.stdout)["daily_folder"], "Journal")
        self.assertEqual(json.loads(profile.stdout)["host"], "claude-plugin")
        self.assertEqual(self.configure().returncode, 64)

    def test_prompt_occurrence_duplicate_resume_and_repeated_text(self):
        path = self.start()
        before = path.read_bytes()
        self.hook("UserPromptSubmit", prompt="Implement the change")
        self.assertEqual(path.read_bytes(), before)
        resumed = self.hook("SessionStart", source="resume")
        self.assertIn("mcp__obsidian-write__obsidian_keeper_save", json.dumps(resumed))
        self.assertEqual(path.read_bytes(), before)
        self.save(path, array=True)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["disposition"], "saved")
        self.assertTrue(saved["session_save"]["daily"].startswith("Journal/"))
        self.hook("UserPromptSubmit", prompt="Implement the change")
        self.assertEqual(json.loads(path.read_text())["disposition"], "saved")
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"user","uuid":"next"}\n'
        )
        self.hook("UserPromptSubmit", prompt="Implement the change")
        next_state = json.loads(path.read_text())
        self.assertNotEqual(next_state["turn"], saved["turn"])
        self.assertNotEqual(
            next_state["session_save"]["idempotency_key"],
            saved["session_save"]["idempotency_key"],
        )
        self.assertEqual(next_state["disposition"], "pending")
        self.assertNotIn("Implement the change", path.read_text())
        self.assertNotIn(str(self.transcript), path.read_text())

    def test_legacy_descriptor_without_project_mode_keeps_git_validation(self):
        self.assertEqual(self.configure().returncode, 0)
        path = self.home / "capture-profile.json"
        profile = json.loads(path.read_text())
        profile.pop("projectMode", None)
        path.write_text(json.dumps(profile))
        result = self.cli("profile-status", "--home", str(self.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["projectMode"], "git")
        nested = self.project / "nested"
        nested.mkdir()
        subprocess.run(
            ["git", "-C", str(nested), "init"],
            env=self.environment,
            check=True,
            capture_output=True,
        )
        result = self.hook("UserPromptSubmit", cwd=str(nested), prompt="Nested work")
        self.assertNotIn("decision", result)
        self.assertIn("PROJECT_MISMATCH", json.dumps(result))
        self.assertFalse(self.state_paths())

    def test_failed_write_is_unresolved_and_retry_retains_exact_payload(self):
        path = self.start()
        args = self.session_arguments(path)
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="write-failure",
            tool_input=args,
        )
        self.hook("PreToolUse", **event)
        output = self.hook(
            "PostToolUseFailure",
            **event,
            error="SECRET-PROVIDER-ERROR",
            is_interrupt=True,
        )
        state = json.loads(path.read_text())
        self.assertTrue(state["session_recovery"])
        self.assertEqual(state["disposition"], "pending")
        self.assertNotIn("SECRET-PROVIDER-ERROR", path.read_text() + json.dumps(output))
        self.assertEqual(
            self.hook("PreToolUse", **event)["hookSpecificOutput"][
                "permissionDecision"
            ],
            "deny",
        )
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])
        result = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--payload-sha256",
            state["session_payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), args)
        changed = self.hook(
            "PreToolUse", **dict(event, tool_input=dict(args, body="Changed"))
        )
        self.assertEqual(changed["hookSpecificOutput"]["permissionDecision"], "deny")
        self.save(path, call="retry-write")
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])

    def test_stop_is_bounded_and_orphan_hook_cannot_create_turn(self):
        self.assertEqual(self.configure().returncode, 0)
        orphan = self.hook(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="orphan",
            tool_input={"command": "git commit -m fixture"},
        )
        self.assertNotIn("permissionDecision", orphan.get("hookSpecificOutput", {}))
        self.assertFalse(self.state_paths())
        self.hook("UserPromptSubmit", prompt="Work")
        first = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(first["decision"], "block")
        self.assertNotIn("decision", self.hook("Stop", stop_hook_active=True))
        self.assertNotIn("decision", self.hook("Stop", stop_hook_active=False))

    def test_generic_hook_errors_are_advisory_except_protected_mcp_writes(self):
        self.assertEqual(self.configure().returncode, 0)
        self.hook("UserPromptSubmit", prompt="Original work")
        path = self.state_paths()[0]
        before = path.read_bytes()
        prompt = self.hook("UserPromptSubmit", session_id="", prompt="New work")
        self.assertNotIn("decision", prompt)
        self.assertIn("EVENT_ID_INVALID", json.dumps(prompt))
        for active in (False, True):
            output = self.hook("Stop", session_id="", stop_hook_active=active)
            self.assertNotIn("decision", output)
            self.assertIn("EVENT_ID_INVALID", json.dumps(output))
        shell = self.hook(
            "PreToolUse", session_id="", tool_name="Bash", tool_input={"command": "pwd"}
        )
        self.assertNotIn("permissionDecision", shell.get("hookSpecificOutput", {}))
        for prefix in ("mcp__obsidian-write__", "mcp__obsidian_write__"):
            for tool in ("obsidian_keeper_save", "obsidian_daily_append"):
                output = self.hook(
                    "PreToolUse", session_id="", tool_name=prefix + tool, tool_input={}
                )
                self.assertEqual(
                    output["hookSpecificOutput"]["permissionDecision"], "deny"
                )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.hook("Stop", stop_hook_active=False)["decision"], "block")
        self.assertNotIn("decision", self.hook("Stop", stop_hook_active=False))

    def test_commit_executor_and_failure_receipts_use_shared_canonical_plan(self):
        path = self.start()
        _path, _state, commit = self.pending_commit()
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        args = json.loads(prepared.stdout)["arguments"]
        self.assertEqual(
            args["skip_if_hash"], self.git("rev-parse", "HEAD").stdout.strip()
        )
        self.assertTrue(args["target_path"].startswith("Projects/Development/"))
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_daily_append",
            tool_use_id="append",
            tool_input=args,
        )
        self.hook("PreToolUse", **event)
        self.hook("PostToolUseFailure", **event, error="connection lost")
        pending = json.loads(path.read_text())["commits"][commit]
        self.assertTrue(pending["recovery"])
        self.assertEqual(pending["status"], "pending")
        retry = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--commit",
            commit,
            "--payload-sha256",
            pending["payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(json.loads(retry.stdout)["arguments"], args)

    def test_drain_checks_all_sessions_and_rejects_corruption_or_missing_owner(self):
        path = self.start()
        self.save(path)
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])
        self.hook("UserPromptSubmit", session_id="second", prompt="Other work")
        result = json.loads(self.drain().stdout)
        self.assertFalse(result["quiescent"])
        self.assertEqual(result["session_count"], 2)
        self.assertNotIn("Private session context", json.dumps(result))
        second = next(p for p in self.state_paths() if p != path)
        second.write_text("{}")
        self.assertEqual(self.drain().returncode, 64)
        second.unlink()
        gate = self.home / "capture-state/active.json"
        gate.write_text(
            json.dumps(
                dict(
                    schemaVersion=1, session="0" * 64, reason="commit_capture", since=1
                )
            )
        )
        gate.chmod(0o600)
        self.assertEqual(self.drain().returncode, 64)
        gate.write_text("invalid")
        self.assertEqual(self.drain().returncode, 64)

    def test_manual_writes_do_not_acknowledge_or_block_automatic_capture(self):
        path = self.start()
        before = path.read_bytes()
        for tool, args in (
            (
                "obsidian_keeper_save",
                dict(
                    title="Manual",
                    folder_hint="Notes",
                    body="Manual content",
                    resolved=True,
                    idempotency_key="manual-note",
                ),
            ),
            (
                "obsidian_daily_append",
                dict(
                    content="Manual daily entry",
                    date="2026-01-01",
                    idempotency_key="manual-daily",
                ),
            ),
        ):
            event = dict(
                tool_name="mcp__obsidian-write__" + tool,
                tool_use_id=tool,
                tool_input=args,
            )
            self.assertEqual(self.hook("PreToolUse", **event), {})
            self.assertEqual(
                self.hook(
                    "PostToolUse",
                    **event,
                    tool_response={"structuredContent": {"status": "committed"}},
                ),
                {},
            )
            self.assertEqual(path.read_bytes(), before)
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])

    def test_drain_rejects_busy_gate_and_state_without_deadlock(self):
        path = self.start()
        self.save(path)
        self.drain()
        for lock in (
            self.home / "capture-state/active.lock",
            path.with_suffix(".lock"),
        ):
            holder = subprocess.Popen(
                [
                    "python3",
                    "-c",
                    "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); sys.stdin.read(1)",
                    str(lock),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                busy = self.drain()
                self.assertEqual(busy.returncode, 64, busy.stdout)
            finally:
                holder.communicate("x", timeout=10)
            self.assertTrue(json.loads(self.drain().stdout)["quiescent"])

    def test_drain_detects_snapshot_and_recovery_even_in_another_settled_session(self):
        path = self.start()
        self.save(path)
        before = path.read_bytes()
        state = json.loads(before)
        state["session_outcome"]["recovery"]["required"] = True
        path.write_text(json.dumps(state))
        self.assertEqual(self.drain().returncode, 64)
        state = json.loads(before)
        for flag in ("session_recovery", "session_in_flight"):
            state[flag] = True
            path.write_text(json.dumps(state))
            self.assertFalse(json.loads(self.drain().stdout)["quiescent"])
            state.pop(flag)
        path.write_bytes(before)
        event = dict(
            tool_name="Bash",
            tool_use_id="snapshot",
            tool_input={"command": "git commit -m fixture"},
        )
        self.hook("PreToolUse", **event)
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])

    def test_orphan_receipt_wrong_alias_and_unknown_prompt_cannot_promote_capture(self):
        path = self.start()
        args = self.session_arguments(path)
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="unpaired",
            tool_input=args,
        )
        self.hook(
            "PostToolUse",
            **event,
            tool_response={"structuredContent": {"status": "committed"}},
        )
        self.assertEqual(json.loads(path.read_text())["disposition"], "pending")
        wrong = self.hook(
            "PreToolUse",
            **dict(event, tool_name="mcp__obsidian_write__obsidian_keeper_save"),
        )
        self.assertEqual(wrong["hookSpecificOutput"]["permissionDecision"], "deny")
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"user","uuid":"new"}\n'
        )
        denied = self.hook("UserPromptSubmit", prompt="Another prompt")
        self.assertNotIn("decision", denied)
        self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", json.dumps(denied))
        self.assertEqual(
            json.loads(path.read_text())["session_save"]["idempotency_key"],
            args["idempotency_key"],
        )

    def test_first_prompt_without_transcript_and_resume_identity_change(self):
        self.transcript.unlink()
        path = self.start()
        self.transcript.write_text('{"type":"user","uuid":"first"}\n')
        self.hook("SessionStart", source="resume")
        before = path.read_bytes()
        other = self.directory / "other.jsonl"
        other.write_text("{}\n")
        output = self.hook("SessionStart", source="resume", transcript_path=str(other))
        self.assertIn("TRANSCRIPT_IDENTITY_CHANGED", json.dumps(output))
        self.assertEqual(path.read_bytes(), before)

    def test_host_commit_failure_preserves_executor_and_never_claims_capture(self):
        path = self.start()
        event = dict(
            tool_name="Bash",
            tool_use_id="commit-failure",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = self.hook("PreToolUse", **event)
        (self.project / "file.txt").write_text("fixture\n")
        self.git("add", "file.txt")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.hook(
            "PostToolUseFailure", **event, error="host interrupted", is_interrupt=True
        )
        state = json.loads(path.read_text())
        self.assertTrue(state["snapshots"])
        self.assertFalse(state["commits"])
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])

    def test_installed_release_verification_is_required_on_every_profile_load(self):
        prefix = self.directory / "managed-client"
        installed = subprocess.run(
            [
                str(self.bundle / "bin/obsidian-mcp-client-install"),
                "--prefix",
                str(prefix),
                "--bundle",
                str(self.bundle),
                "--release-sha256",
                self.digest,
            ],
            env=self.environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.bundle = prefix / "current"
        self.start()
        self.assertEqual(
            self.cli("profile-status", "--home", str(self.home)).returncode, 0
        )
        installed_readme = self.bundle / "README.md"
        installed_readme.chmod(0o600)
        installed_readme.write_text("tampered")
        self.assertEqual(
            self.cli("profile-status", "--home", str(self.home)).returncode, 64
        )
        self.assertEqual(self.drain().returncode, 64)

    def test_daily_folder_validation_and_external_lifecycle(self):
        self.assertEqual(self.configure(daily="../Journal").returncode, 64)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.configure(daily="Daily").returncode, 0)
        disabled = self.cli("disable", "--home", str(self.home))
        self.assertEqual(disabled.returncode, 64)
        self.assertEqual(self.cli("run", "--home", str(self.home)).returncode, 64)
        self.assertEqual(
            self.cli("profile-status", "--home", str(self.home)).returncode, 0
        )

    @unittest.skipUnless(
        os.environ.get("OBSIDIAN_MCP_TEST_PLUGIN_ROOT"),
        "optional public plugin checkout not supplied",
    )
    def test_plugin_handoff_delegates_and_observes_session_receipt(self):
        plugin = Path(os.environ["OBSIDIAN_MCP_TEST_PLUGIN_ROOT"]).resolve(strict=True)
        owner = plugin / "scripts/lib/capture-owner.py"
        dispatcher = plugin / "scripts/capture-hook.sh"
        self.assertTrue(owner.is_file())
        self.assertTrue(dispatcher.is_file())
        fixture_home = self.directory / "home"
        fixture_home.mkdir()
        self.environment.update(
            HOME=str(fixture_home),
            XDG_STATE_HOME=str(self.directory / "state"),
            XDG_CONFIG_HOME=str(self.directory / "config"),
            XDG_CACHE_HOME=str(self.directory / "cache"),
        )
        prefix = self.directory / "installed-client"
        installed = subprocess.run(
            [
                str(self.bundle / "bin/obsidian-mcp-client-install"),
                "--prefix",
                str(prefix),
                "--bundle",
                str(self.bundle),
                "--release-sha256",
                self.digest,
            ],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.bundle = prefix / "current"
        attached = self.configure()
        self.assertEqual(attached.returncode, 0, attached.stderr)

        def handoff(target):
            return subprocess.run(
                [
                    "python3",
                    str(owner),
                    "handoff",
                    "--owner",
                    target,
                    "--profile",
                    str(self.home),
                    "--project",
                    str(self.project),
                    "--verification",
                    "legacy-writers-stopped-and-history-inspected",
                ],
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

        def dispatch(name, **extra):
            result = subprocess.run(
                ["bash", str(dispatcher), name],
                input=json.dumps(self.event(name, **extra)),
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        switched = handoff("mcp")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        self.assertEqual(json.loads(switched.stdout)["owner"], "mcp")
        guidance = dispatch(
            "UserPromptSubmit", prompt="Capture a paired integration fixture"
        )
        self.assertIn("mcp__obsidian-write__obsidian_keeper_save", json.dumps(guidance))
        path = next(iter(self.state_paths()))
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])
        args = self.session_arguments(path)
        plan = json.loads(path.read_text())["session_save"]
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="paired-save",
            tool_input=args,
        )
        self.assertEqual(dispatch("PreToolUse", **event), {})
        receipt = dict(
            status="committed",
            request_id="paired-fixture",
            idempotency_key=args["idempotency_key"],
            path=plan["path"],
            affected_paths=[plan["path"], plan["index"], plan["daily"]],
            warnings=[],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
        )
        self.assertEqual(
            dispatch(
                "PostToolUse", **event, tool_response={"structuredContent": receipt}
            ),
            {},
        )
        self.assertEqual(json.loads(path.read_text())["disposition"], "saved")
        commit_call = dict(
            tool_name="Bash",
            tool_use_id="paired-commit",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = dispatch("PreToolUse", **commit_call)
        (self.project / "file.txt").write_text("paired change\n")
        self.git("add", "file.txt")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        guidance = dispatch("PostToolUse", **commit_call, tool_response=executed.stdout)
        commit_args = self.commit_arguments(
            path,
            ["Paired fixture commit", "Verified dispatcher", "Kept immutable request"],
            output=guidance,
        )
        commit_write = self.commit_event(commit_args, "paired-commit-write")
        self.assertEqual(dispatch("PreToolUse", **commit_write), {})
        (pending,) = json.loads(path.read_text())["commits"].values()
        self.assertEqual(pending["native_arguments"], commit_args)
        dispatch(
            "PostToolUse",
            **commit_write,
            tool_response=self.commit_receipt(commit_args),
        )
        (pending,) = json.loads(path.read_text())["commits"].values()
        self.assertEqual(pending["status"], "committed")
        self.assertEqual(dispatch("Stop", stop_hook_active=False), {})
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])
        descriptor = self.home / "capture-profile.json"
        original = descriptor.read_bytes()
        damaged = json.loads(original)
        damaged["releaseSha256"] = "0" * 64
        descriptor.write_text(json.dumps(damaged))
        try:
            denied = dispatch(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="bad-release",
                tool_input={"command": "git status"},
            )
            self.assertNotIn("permissionDecision", denied.get("hookSpecificOutput", {}))
            denied = dispatch(
                "PreToolUse",
                tool_name="mcp__obsidian-write__obsidian_keeper_save",
                tool_use_id="bad-release-write",
                tool_input=args,
            )
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertNotIn("decision", dispatch("Stop", stop_hook_active=False))
            self.assertNotIn("decision", dispatch("Stop", stop_hook_active=True))
        finally:
            descriptor.write_bytes(original)
        rolled_back = handoff("disabled")
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertEqual(json.loads(rolled_back.stdout)["owner"], "disabled")
        self.assertEqual(
            self.cli("profile-status", "--home", str(self.home)).returncode, 0
        )

    def test_default_daily_folder_and_empty_profile_drain(self):
        attached = self.cli(
            "attach",
            "--host",
            "claude-plugin",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Notes",
            "--release-sha256",
            self.digest,
        )
        self.assertEqual(attached.returncode, 0, attached.stderr)
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])
        self.hook("UserPromptSubmit", prompt="Work")
        path = next(iter(self.state_paths()))
        self.assertTrue(
            json.loads(path.read_text())["session_save"]["daily"].startswith("Daily/")
        )

    def test_stop_failure_and_continuation_never_invent_a_prompt_occurrence(self):
        path = self.start()
        before = path.read_bytes()
        failure = self.hook(
            "StopFailure", error="rate_limit", error_details="PRIVATE-DETAILS"
        )
        self.assertNotIn("decision", failure)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("PRIVATE-DETAILS", json.dumps(failure))
        stopped = self.hook("Stop", stop_hook_active=False)
        turn = json.loads(path.read_text())["turn"]
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"system","uuid":"continuation"}\n'
        )
        self.hook("UserPromptSubmit", prompt=stopped["reason"])
        self.assertEqual(json.loads(path.read_text())["turn"], turn)
        self.assertNotIn("decision", self.hook("Stop", stop_hook_active=False))

    def test_takeover_does_not_make_uncaptured_commit_losslessly_drained(self):
        path = self.start()
        self.pending_commit()
        self.save(path)
        self.hook("UserPromptSubmit", session_id="other", prompt="Other")
        other = next(p for p in self.state_paths() if p != path)
        taken = self.cli(
            "takeover",
            "--home",
            str(self.home),
            "--session",
            other.stem,
            "--verification",
            "command-stopped-and-history-inspected",
        )
        self.assertEqual(taken.returncode, 0, taken.stderr)
        self.assertEqual(json.loads(taken.stdout)["status"], "recovered")
        skipped = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            other.stem,
            "--turn",
            json.loads(other.read_text())["turn"],
            "--reason",
            "no_meaningful_work",
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        result = json.loads(self.drain().stdout)
        self.assertIsNone(result["active_request"])
        self.assertFalse(result["quiescent"])
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"user","uuid":"later"}\n'
        )
        output = self.hook("UserPromptSubmit", prompt="Later")
        self.assertNotIn("decision", output)
        self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", json.dumps(output))

    def test_failure_before_executor_start_preserves_snapshot(self):
        path = self.start()
        event = dict(
            tool_name="Bash",
            tool_use_id="failed-launch",
            tool_input={"command": "git commit -m fixture"},
        )
        self.hook("PreToolUse", **event)
        self.hook("PostToolUseFailure", **event, error="could not launch executor")
        self.assertTrue(json.loads(path.read_text())["snapshots"])
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])

    def test_rewritten_command_preserves_profile_path_and_permission_flow(self):
        self.home = self.directory / "mcp__obsidian_write__obsidian_keeper_save"
        self.start()
        self.pending_commit()

    def test_invalid_profile_and_noncanonical_daily_receipt_fail_closed(self):
        path = self.start()
        args = self.session_arguments(path)
        plan = json.loads(path.read_text())["session_save"]
        event = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="bad-daily",
            tool_input=args,
        )
        self.hook("PreToolUse", **event)
        receipt = dict(
            status="committed",
            request_id="fixture",
            idempotency_key=args["idempotency_key"],
            path=plan["path"],
            affected_paths=[
                plan["path"],
                plan["index"],
                "Daily/" + plan["date"] + ".md",
            ],
            warnings=[],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
        )
        self.hook(
            "PostToolUse",
            **event,
            tool_response=json.dumps({"structuredContent": receipt}),
        )
        state = json.loads(path.read_text())
        self.assertEqual(state["disposition"], "pending")
        self.assertTrue(state["session_recovery"])
        (self.home / "capture-profile.json").write_text("[]")
        invalid = self.cli("profile-status", "--home", str(self.home))
        self.assertEqual(invalid.returncode, 64)
        self.assertNotIn("Traceback", invalid.stderr)


class DirectoryCaptureTest(NativeCaptureFixture):
    initialize_git = False
    session_arguments = ClaudePluginCaptureTest.session_arguments
    save = ClaudePluginCaptureTest.save
    drain = ClaudePluginCaptureTest.drain
    test_generic_hook_errors_are_advisory_except_protected_mcp_writes = (
        ClaudePluginCaptureTest.test_generic_hook_errors_are_advisory_except_protected_mcp_writes
    )

    def setUp(self):
        super().setUp()
        self.transcript = self.directory / "conversation.jsonl"
        self.transcript.write_text('{"type":"system","uuid":"start"}\n')
        self.cwd = self.project
        self.session = "directory-session"
        self.git_log = self.directory / "unexpected-git"

    def configure(self, **extra):
        extra.setdefault("mode", "directory")
        return ClaudePluginCaptureTest.configure(self, **extra)

    def event(self, name, **extra):
        extra.setdefault("cwd", str(self.cwd))
        extra.setdefault("session_id", self.session)
        return ClaudePluginCaptureTest.event(self, name, **extra)

    def forbid_git(self):
        directory = self.directory / "no-git"
        directory.mkdir()
        binary = directory / "git"
        binary.write_text(
            '#!/bin/sh\nprintf attempted > "$FORBIDDEN_GIT_LOG"\nexit 97\n'
        )
        binary.chmod(0o700)
        self.environment.update(
            PATH=str(directory) + os.pathsep + self.environment["PATH"],
            FORBIDDEN_GIT_LOG=str(self.git_log),
        )

    def profile_snapshot(self):
        return {
            str(path.relative_to(self.home)): (
                path.read_bytes() if path.is_file() else None
            )
            for path in self.home.rglob("*")
        }

    def test_directory_session_and_nested_git_are_session_only_without_git_calls(self):
        subdirectory = self.project / "notes"
        nested = self.project / "repository"
        subdirectory.mkdir()
        nested.mkdir()
        subprocess.run(
            ["git", "-C", str(nested), "init"],
            env=self.environment,
            check=True,
            capture_output=True,
        )
        original = {
            str(path.relative_to(self.project)): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file()
        }
        self.forbid_git()
        attached = self.configure()
        self.assertEqual(attached.returncode, 0, attached.stderr)
        profile = json.loads((self.home / "capture-profile.json").read_text())
        self.assertEqual(profile["projectMode"], "directory")
        self.assertEqual(profile["project"], str(self.project))
        status = self.cli("profile-status", "--home", str(self.home))
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["projectMode"], "directory")
        for index, cwd in enumerate((self.project, subdirectory, nested)):
            with self.subTest(cwd=cwd):
                self.cwd, self.session = cwd, "directory-" + str(index)
                self.hook("SessionStart", source="startup")
                self.hook(
                    "UserPromptSubmit", prompt="Capture meaningful directory work"
                )
                path = (
                    self.home
                    / "capture-state"
                    / (self.session_key(self.session) + ".json")
                )
                self.assertTrue(path.is_file())
                self.assertEqual(
                    self.hook(
                        "PreToolUse",
                        tool_name="Bash",
                        tool_use_id="read-only",
                        tool_input={"command": "git status"},
                    ),
                    {},
                )
                self.hook(
                    "PostToolUseFailure",
                    tool_name="Bash",
                    tool_use_id="read-only",
                    tool_input={"command": "git status"},
                    error="not a checkout",
                )
                self.assertEqual(
                    self.hook("Stop", stop_hook_active=False)["decision"], "block"
                )
                self.save(path, bare=True)
                self.hook("SessionStart", source="resume")
                self.hook("StopFailure")
                self.assertEqual(self.hook("Stop", stop_hook_active=False), {})
                state = json.loads(path.read_text())
                self.assertEqual(state["disposition"], "saved")
                self.assertEqual(state["commits"], {})
                self.assertEqual(state["snapshots"], {})
                self.assertNotIn("metadata", state)
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])
        self.assertFalse(self.git_log.exists())
        self.assertFalse((self.project / ".git").exists())
        self.assertEqual(
            {
                str(path.relative_to(self.project)): path.read_bytes()
                for path in self.project.rglob("*")
                if path.is_file()
            },
            original,
        )

    def test_directory_cwd_rejects_sibling_prefix_and_symlink_escape_before_state(self):
        outside = self.directory / "project-sibling"
        outside.mkdir()
        escape = self.project / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        loop = self.project / "loop"
        loop.symlink_to(loop, target_is_directory=True)
        self.forbid_git()
        self.assertEqual(self.configure().returncode, 0)
        before = self.profile_snapshot()
        for cwd in (outside, escape, loop, self.project / "missing", Path("relative")):
            with self.subTest(cwd=cwd):
                result = self.hook(
                    "UserPromptSubmit", cwd=str(cwd), prompt="Outside work"
                )
                self.assertNotIn("decision", result)
                self.assertIn("PROJECT_MISMATCH", json.dumps(result))
                self.assertEqual(self.profile_snapshot(), before)
        refusal = self.hook(
            "PreToolUse",
            cwd=str(loop),
            tool_name="Bash",
            tool_input={"command": "printf fixture"},
        )
        self.assertNotIn("permissionDecision", refusal.get("hookSpecificOutput", {}))
        self.assertIn("PROJECT_MISMATCH", json.dumps(refusal))
        self.assertEqual(self.profile_snapshot(), before)
        self.assertFalse(self.git_log.exists())

    def test_looped_project_and_profile_paths_refuse_without_traceback_or_state(self):
        loop = self.project / "loop"
        loop.symlink_to(loop, target_is_directory=True)
        self.forbid_git()
        project = self.project
        self.project = loop
        refused = self.configure()
        self.assertEqual(refused.returncode, 64, refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertFalse(self.home.exists())
        self.project = project
        self.assertEqual(self.configure().returncode, 0)
        descriptor = self.home / "capture-profile.json"
        profile = json.loads(descriptor.read_text())
        descriptor.write_text(json.dumps(dict(profile, project=str(loop))))
        before = self.profile_snapshot()
        for operation in ("profile-status", "hook", "drain-status"):
            with self.subTest(operation=operation):
                result = self.cli(
                    operation,
                    "--home",
                    str(self.home),
                    payload=self.event("UserPromptSubmit", prompt="Must refuse"),
                )
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.profile_snapshot(), before)
        result = self.cli("profile-status", "--home", str(loop / "profile"))
        self.assertEqual(result.returncode, 64, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.profile_snapshot(), before)
        descriptor.write_text(json.dumps(profile))
        self.hook("SessionStart", source="startup")
        before = self.profile_snapshot()
        result = self.hook(
            "UserPromptSubmit",
            transcript_path=str(loop / "transcript.jsonl"),
            prompt="Must refuse looped transcript",
        )
        self.assertNotIn("decision", result)
        self.assertEqual(self.profile_snapshot(), before)
        self.assertEqual(self.state_paths(), [])
        self.assertFalse(self.git_log.exists())

    def test_directory_partial_session_requires_review_and_reuses_the_original_request(
        self,
    ):
        self.forbid_git()
        self.assertEqual(self.configure().returncode, 0)
        self.hook("UserPromptSubmit", prompt="Retain failed session work")
        path = next(iter(self.state_paths()))
        self.save(
            path,
            status="partial",
            receipt_changes={
                "recovery": {"required": True, "action": "inspect"},
                "error_code": "PARTIAL",
                "retryable": True,
            },
        )
        state = json.loads(path.read_text())
        self.assertEqual(state["disposition"], "pending")
        self.assertTrue(state["session_recovery"])
        self.assertFalse(json.loads(self.drain().stdout)["quiescent"])
        original = state["session_request"]
        changed = dict(original, body="Changed payload")
        refusal = self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="changed",
            tool_input=changed,
        )
        self.assertEqual(refusal["hookSpecificOutput"]["permissionDecision"], "deny")
        retry = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--payload-sha256",
            state["session_payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout), original)
        self.save(path, call="reviewed-retry")
        self.assertEqual(json.loads(path.read_text())["session_request"], original)
        self.assertTrue(json.loads(self.drain().stdout)["quiescent"])
        self.assertFalse(self.git_log.exists())

    def test_directory_shell_events_pass_unchanged_without_commit_capture_or_git(self):
        self.forbid_git()
        self.assertEqual(self.configure().returncode, 0)
        self.hook("UserPromptSubmit", prompt="Review directory shell activity")
        before = self.profile_snapshot()
        for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            for command in (
                "git log --grep commit",
                "grep commit git",
                'printf "%s\\n" git commit',
                "git commit -m fixture",
                "rtk git -C nested commit -m fixture",
                "env git commit -m fixture",
                "command git commit -m fixture",
                'bash -lc "git commit -m fixture"',
                "git commit; printf done",
                "python3 -c 'print(\"git commit\")'",
                "printf 'unterminated",
            ):
                with self.subTest(event=name, command=command):
                    result = self.cli(
                        "hook",
                        "--home",
                        str(self.home),
                        payload=self.event(
                            name,
                            tool_name="Bash",
                            tool_use_id="shell-command",
                            tool_input={"command": command},
                            tool_response="ignored",
                        ),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {})
                    self.assertEqual(self.profile_snapshot(), before)
        state = json.loads(next(iter(self.state_paths())).read_text())
        self.assertEqual(state["commits"], {})
        self.assertEqual(state["snapshots"], {})
        self.assertNotIn("metadata", state)
        self.assertFalse(self.git_log.exists())

    def test_directory_commit_capture_cli_and_mcp_refuse_before_state_or_git(
        self,
    ):
        self.forbid_git()
        self.assertEqual(self.configure().returncode, 0)
        before = self.profile_snapshot()
        for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            for tool in (
                "mcp__obsidian-write__obsidian_daily_append",
                "mcp__obsidian_write__obsidian_daily_append",
            ):
                for arguments in (
                    dict(skip_if_hash="a" * 40),
                    dict(idempotency_key="commit-capture:forged"),
                ):
                    with self.subTest(event=name, tool=tool, arguments=arguments):
                        result = self.hook(
                            name,
                            tool_name=tool,
                            tool_use_id="commit-append",
                            tool_input=arguments,
                            tool_response="ignored",
                        )
                        self.assertIn("DIRECTORY_SESSION_ONLY", json.dumps(result))
                        if name == "PreToolUse":
                            self.assertEqual(
                                result["hookSpecificOutput"]["permissionDecision"],
                                "deny",
                            )
                        self.assertEqual(self.profile_snapshot(), before)
        for operation, options in (
            ("execute-commit", ["--snapshot", "a" * 64]),
            ("prepare", ["--commit", "b" * 64]),
            ("retry", ["--commit", "b" * 64]),
            ("resolve-snapshot", ["--snapshot", "a" * 64]),
            ("takeover", []),
        ):
            with self.subTest(operation=operation):
                result = self.cli(
                    operation,
                    "--home",
                    str(self.home),
                    "--session",
                    "c" * 64,
                    *options,
                    payload={"context": ["must not be processed"]},
                )
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("DIRECTORY_SESSION_ONLY", result.stderr)
                self.assertEqual(self.profile_snapshot(), before)
        self.assertFalse(self.git_log.exists())

    def test_malformed_modes_fail_closed_at_attach_and_load_without_git(self):
        self.forbid_git()
        for mode in ("", "Directory", "unknown"):
            with self.subTest(mode=mode):
                result = self.configure(mode=mode)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertFalse(self.home.exists())
        self.assertEqual(self.configure().returncode, 0)
        path = self.home / "capture-profile.json"
        original = json.loads(path.read_text())
        for mode in (None, False, 1, [], {}, "", "unknown"):
            with self.subTest(mode=mode):
                path.write_text(json.dumps(dict(original, projectMode=mode)))
                before = self.profile_snapshot()
                for operation in ("profile-status", "hook", "drain-status"):
                    result = self.cli(
                        operation,
                        "--home",
                        str(self.home),
                        payload=self.event("UserPromptSubmit", prompt="Invalid mode"),
                    )
                    self.assertEqual(result.returncode, 64, result.stderr)
                    self.assertIn("PROJECT_MODE_INVALID", result.stderr)
                    self.assertEqual(self.profile_snapshot(), before)
        self.assertFalse(self.git_log.exists())

    def test_directory_mode_is_explicit_and_codex_create_refuses_before_changes(self):
        self.forbid_git()
        created = self.cli(
            "create",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Projects/Fixture",
            "--release-sha256",
            self.digest,
            "--project-mode",
            "directory",
        )
        self.assertEqual(created.returncode, 64, created.stderr)
        self.assertIn("PROJECT_MODE_HOST_UNSUPPORTED", created.stderr)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.git_log.exists())
        default = ClaudePluginCaptureTest.configure(self)
        self.assertEqual(default.returncode, 64, default.stderr)
        self.assertFalse(self.home.exists())


class UserCaptureTest(NativeCaptureFixture):
    expected_rewrite_decision = None
    event = ClaudePluginCaptureTest.event
    session_arguments = ClaudePluginCaptureTest.session_arguments
    save = ClaudePluginCaptureTest.save

    def test_busy_executor_does_not_block_other_workspace_commit_or_admission(self):
        self.configure()
        commands = []
        roots = [self.project, self.directory / "other-git"]
        for number, root in enumerate(roots):
            if number:
                root.mkdir()
                subprocess.run(
                    ["git", "-C", str(root), "init"],
                    env=self.environment,
                    check=True,
                    capture_output=True,
                )
            self.hook(
                "UserPromptSubmit",
                cwd=str(root),
                session_id=f"git-{number}",
                prompt="Work",
            )
            (root / "change").write_text("fixture")
            subprocess.run(
                ["git", "-C", str(root), "add", "change"],
                env=self.environment,
                check=True,
            )
            output = self.hook(
                "PreToolUse",
                cwd=str(root),
                session_id=f"git-{number}",
                tool_name="Bash",
                tool_use_id=f"commit-{number}",
                tool_input={"command": "git commit -m fixture"},
            )
            commands.append(
                shlex.split(output["hookSpecificOutput"]["updatedInput"]["command"])
            )
        entered, release = self.directory / "entered", self.directory / "release"
        hook = roots[0] / ".git/hooks/pre-commit"
        hook.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nimport time\n"
            f"Path({str(entered)!r}).touch()\nend=time.monotonic()+15\n"
            f"while not Path({str(release)!r}).exists() and time.monotonic()<end: time.sleep(.02)\n"
        )
        hook.chmod(0o700)
        with subprocess.Popen(
            commands[0],
            cwd=roots[0],
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as first:
            try:
                deadline = time.monotonic() + 10
                while (
                    not entered.exists()
                    and first.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertTrue(entered.exists(), "executor never entered Git hook")
                second = subprocess.run(
                    commands[1],
                    cwd=roots[1],
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                fresh = self.directory / "fresh-directory"
                fresh.mkdir()
                replay = subprocess.run(
                    commands[0],
                    cwd=roots[0],
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                native = self.load_native()
                home, profile = native.load_profile(self.home)
                with mock.patch.object(native, "MAX_DRAIN_SESSIONS", 2):
                    at_capacity = native.hook(
                        home,
                        profile,
                        self.event(
                            "UserPromptSubmit",
                            cwd=str(fresh),
                            session_id="fresh",
                            prompt="Independent work",
                        ),
                    )
                self.assertIn("DRAIN_SESSION_LIMIT", json.dumps(at_capacity))
                admission = self.hook(
                    "UserPromptSubmit",
                    cwd=str(fresh),
                    session_id="fresh",
                    prompt="Independent work",
                )
                held = first.poll() is None
            finally:
                release.touch()
                stdout, stderr = first.communicate(timeout=15)
        self.assertTrue(held, "isolation checks did not overlap running Git")
        self.assertEqual(first.returncode, 0, stderr + stdout)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(replay.returncode, 64, replay.stdout)
        self.assertIn("obsidian_keeper_save", json.dumps(admission))

    def pause_next_prompt(self):
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"user","uuid":"next"}\n'
        )
        output = self.hook("UserPromptSubmit", prompt="Next ordinary prompt")
        self.assertNotIn("decision", output)
        self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", json.dumps(output))

    def test_paused_session_accepts_only_frozen_reviewed_retry_and_matching_receipt(
        self,
    ):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Original")
        path = self.state_paths()[0]
        self.save(
            path,
            status="partial",
            receipt_changes={
                "recovery": {"required": True, "action": "inspect"},
                "error_code": "PARTIAL",
                "retryable": True,
            },
        )
        original = path.read_bytes()
        self.pause_next_prompt()
        self.assertEqual(path.read_bytes(), original)
        args = self.session_arguments(path)
        call = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="reviewed",
            tool_input=args,
        )
        self.assertEqual(
            self.hook("PreToolUse", **call)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        state = json.loads(original)
        retry = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--payload-sha256",
            state["session_payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout), args)
        changed = self.hook(
            "PreToolUse", **dict(call, tool_input=dict(args, body="New work"))
        )
        self.assertEqual(changed["hookSpecificOutput"]["permissionDecision"], "deny")
        self.save(path, call="reviewed")
        self.assertEqual(json.loads(path.read_text())["disposition"], "saved")
        self.hook("UserPromptSubmit", prompt="Next ordinary prompt")
        self.assertNotEqual(json.loads(path.read_text())["turn"], state["turn"])

    def test_paused_session_observes_exact_late_receipt_without_new_capture(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Original")
        path = self.state_paths()[0]
        args = self.session_arguments(path)
        plan = json.loads(path.read_text())["session_save"]
        call = dict(
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="late",
            tool_input=args,
        )
        self.assertEqual(self.hook("PreToolUse", **call), {})
        self.pause_next_prompt()
        receipt = dict(
            status="committed",
            request_id="late-fixture",
            idempotency_key=args["idempotency_key"],
            path=plan["path"],
            affected_paths=[plan["path"], plan["index"], plan["daily"]],
            warnings=[],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
        )
        self.hook("PostToolUse", **call, tool_response={"structuredContent": receipt})
        self.assertEqual(json.loads(path.read_text())["disposition"], "saved")

    def test_paused_commit_accepts_frozen_reviewed_retry_and_receipt(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Original")
        path, _, commit = self.pending_commit()
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        arguments = json.loads(prepared.stdout)["arguments"]
        call = dict(
            tool_name="mcp__obsidian-write__obsidian_daily_append",
            tool_use_id="original-write",
            tool_input=arguments,
        )
        self.assertEqual(self.hook("PreToolUse", **call), {})
        partial = ClaudePluginCaptureTest.commit_receipt(self, arguments)
        partial.update(
            status="partial",
            error_code="PARTIAL",
            recovery={"required": True, "action": "inspect"},
            retryable=True,
        )
        self.hook(
            "PostToolUse",
            **call,
            tool_response={"structuredContent": partial, "isError": True},
        )
        self.pause_next_prompt()
        state = json.loads(path.read_text())
        retried = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--commit",
            commit,
            "--payload-sha256",
            state["commits"][commit]["payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["arguments"], arguments)
        retry_call = dict(call, tool_use_id="reviewed-write")
        self.assertEqual(self.hook("PreToolUse", **retry_call), {})
        self.hook(
            "PostToolUse",
            **retry_call,
            tool_response={
                "structuredContent": ClaudePluginCaptureTest.commit_receipt(
                    self, arguments
                )
            },
        )
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "committed"
        )

    def test_removed_terminal_workspace_can_be_inspected_but_not_executed(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Original")
        path = self.state_paths()[0]
        turn = json.loads(path.read_text())["turn"]
        skipped = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--turn",
            turn,
            "--reason",
            "no_meaningful_work",
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.project.rename(self.directory / "retired-project")
        self.assertTrue(self.drain()["quiescent"])
        status = self.cli("status", "--home", str(self.home), "--session", path.stem)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["disposition"], "skipped_by_policy")
        child_status = self.cli("profile-status", "--home", str(path.parents[1]))
        self.assertEqual(child_status.returncode, 0, child_status.stderr)
        execute = self.cli(
            "execute-commit",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--snapshot",
            "0" * 64,
        )
        self.assertEqual(execute.returncode, 64)

    def test_interrupted_child_publication_recovers_without_partial_live_child(self):
        self.configure()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        original_write = native.atomic_write

        def fail_descriptor(path, value):
            if path.name == "capture-profile.json":
                raise OSError("fixture interrupted publication")
            return original_write(path, value)

        with mock.patch.object(native, "atomic_write", side_effect=fail_descriptor):
            output = native.hook(
                home, profile, self.event("UserPromptSubmit", prompt="Original")
            )
        self.assertNotIn("decision", output)
        self.assertEqual(list((self.home / "workspaces").iterdir()), [])
        self.hook("UserPromptSubmit", prompt="Original")
        self.assertEqual(len(self.state_paths()), 1)

    def test_child_rename_interruption_reopens_and_ambiguous_staging_is_retained(self):
        self.configure()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        real_rename = native.os.rename

        def fail_publication(source, target):
            if Path(source).parent.name == "provisioning":
                raise OSError("fixture interrupted rename")
            return real_rename(source, target)

        with mock.patch.object(native.os, "rename", side_effect=fail_publication):
            native.hook(
                home, profile, self.event("UserPromptSubmit", prompt="Original")
            )
        self.assertEqual(list((home / "workspaces").iterdir()), [])
        stage = next((home / "provisioning").iterdir())
        evidence = stage / "capture-state" / ("a" * 64 + ".json")
        evidence.write_text("preserved ambiguous fixture")
        evidence.chmod(0o600)
        denied = self.hook("UserPromptSubmit", prompt="Original")
        self.assertIn("CHILD_PROVISIONING_AMBIGUOUS", json.dumps(denied))
        self.assertEqual(evidence.read_text(), "preserved ambiguous fixture")
        self.assertEqual(self.cli("drain-status", "--home", str(home)).returncode, 64)
        evidence.rename(self.directory / "preserved-fixture-evidence")
        self.hook("UserPromptSubmit", prompt="Original")
        self.assertEqual(len(self.state_paths()), 1)

    def terminal_session(self, session="session-1"):
        self.hook("UserPromptSubmit", session_id=session, prompt="Original")
        path = next(
            (self.home / "workspaces").glob(
                "*/capture-state/" + self.session_key(session) + ".json"
            )
        )
        state = json.loads(path.read_text())
        result = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--turn",
            state["turn"],
            "--reason",
            "no_meaningful_work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    def test_missing_or_corrupt_intermediate_generation_refuses_every_retained_path(
        self,
    ):
        for damage in ("missing", "corrupt", "cross-session", "cycle"):
            with self.subTest(damage=damage):
                self.home = self.directory / damage
                self.configure()
                for number in range(3):
                    self.transcript.write_text(
                        self.transcript.read_text()
                        + json.dumps({"type": "user", "uuid": f"{damage}-{number}"})
                        + "\n"
                    )
                    path = self.terminal_session()
                    self.hook(
                        "UserPromptSubmit", session_id=f"other-{number}", prompt="Other"
                    )
                directory = self.home / "archive" / path.stem
                current = json.loads((directory / "index.json").read_text())[
                    "generation"
                ]
                latest = json.loads((directory / (current + ".json")).read_text())
                middle = directory / (latest["binding"]["archiveGeneration"] + ".json")
                if damage == "missing":
                    middle.unlink()
                elif damage == "corrupt":
                    middle.write_text("{")
                else:
                    record = json.loads(middle.read_text())
                    if damage == "cross-session":
                        record["binding"]["session"] = self.session_key(
                            "another-session"
                        )
                    else:
                        record["binding"]["archiveGeneration"] = middle.stem
                    middle.write_text(json.dumps(record))
                before = {str(p): p.read_bytes() for p in self.home.rglob("*.json")}
                for operation in ("status", "request", "drain-status"):
                    arguments = (
                        [] if operation == "drain-status" else ["--session", path.stem]
                    )
                    refused = self.cli(operation, "--home", str(self.home), *arguments)
                    self.assertEqual(refused.returncode, 64, operation + refused.stdout)
                resumed = self.hook("SessionStart", source="resume")
                self.assertNotIn("obsidian_keeper_save", json.dumps(resumed))
                self.assertFalse(path.exists())
                self.assertEqual(
                    {str(p): p.read_bytes() for p in self.home.rglob("*.json")}, before
                )

    def test_session_start_does_not_enroll_or_consume_admission(self):
        self.configure()
        for session in ("startup-one", "startup-two"):
            result = self.hook("SessionStart", session_id=session, source="startup")
            self.assertIn("UserPromptSubmit", json.dumps(result))
        self.assertEqual(list((self.home / "bindings").iterdir()), [])
        self.assertEqual(list((self.home / "workspaces").iterdir()), [])

    def test_near_limit_saved_payload_archives_and_restores_without_truncation(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Work")
        path = self.state_paths()[0]
        self.save(path)
        native = self.load_native()
        baseline = json.loads(path.read_text())
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"user","uuid":"large"}\n'
        )
        self.hook("UserPromptSubmit", prompt="Large work")
        arguments = self.session_arguments(path)
        arguments["body"] = "x" * (
            native.LIMIT - 64 - len(native.encode(baseline)) + len(arguments["body"])
        )
        with mock.patch.object(self, "session_arguments", return_value=arguments):
            self.save(path)
        original = json.loads(path.read_text())
        self.assertEqual(original["disposition"], "saved")
        self.assertGreater(len(native.encode(original)), native.LIMIT - 256)
        self.assertLessEqual(len(path.read_bytes()), native.LIMIT)
        fresh = self.directory / "independent"
        fresh.mkdir()
        admitted = self.hook(
            "UserPromptSubmit",
            cwd=str(fresh),
            session_id="independent",
            prompt="Other work",
        )
        self.assertIn("obsidian_keeper_save", json.dumps(admitted))
        self.assertFalse(path.exists())
        state = self.cli("status", "--home", str(self.home), "--session", path.stem)
        self.assertEqual(state.returncode, 0, state.stderr)
        self.assertEqual(json.loads(state.stdout)["disposition"], "saved")
        self.hook("SessionStart", source="resume")
        self.assertEqual(json.loads(path.read_text()), original)

    def test_retention_bound_covers_two_accepted_unicode_components_but_not_unbounded_data(
        self,
    ):
        native = self.load_native()
        component = self.directory / "accepted-component.json"
        raw = ('{"text":"' + "é" * 131000 + '"}').encode()
        native.atomic_write(component, raw)
        accepted = json.loads(native.private_read(component))
        journal = self.directory / "retention-envelope.json"
        body = native.encode(
            {
                "schemaVersion": 1,
                "record": {"binding": accepted, "state": accepted},
                "before": {"generation": "f" * 64, "location": "hot"},
                "operation": "archive",
            }
        )
        self.assertGreater(len(body), 2 * 262144)
        with self.assertRaisesRegex(native.NativeError, "STATE_TOO_LARGE"):
            native.atomic_write(journal, body)
        native.atomic_write(journal, body, limit=native.RETENTION_LIMIT)
        self.assertEqual(
            json.loads(native.private_read(journal, limit=native.RETENTION_LIMIT))[
                "record"
            ]["state"]["text"],
            "é" * 131000,
        )
        with self.assertRaisesRegex(native.NativeError, "STATE_TOO_LARGE"):
            native.private_read(journal)
        with self.assertRaisesRegex(native.NativeError, "STATE_TOO_LARGE"):
            native.atomic_write(
                journal,
                b"x" * (native.RETENTION_LIMIT + 1),
                limit=native.RETENTION_LIMIT,
            )

    def test_terminal_history_exceeds_256_and_exact_session_resumes(self):
        self.configure()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        for number in range(258):
            session = "history-" + str(number)
            result = native.hook(
                home,
                profile,
                self.event("UserPromptSubmit", session_id=session, prompt="History"),
            )
            self.assertIn("obsidian_keeper_save", json.dumps(result), (number, result))
            key = self.session_key(session)
            path = next((home / "workspaces").glob("*/capture-state/" + key + ".json"))
            state = json.loads(path.read_text())
            arguments = dict(
                title=state["session_save"]["title"],
                folder_hint=state["session_save"]["folder"],
                body="History fixture",
                resolved=True,
                session_link_date=state["session_save"]["date"],
                idempotency_key=state["session_save"]["idempotency_key"],
            )
            call = dict(
                tool_name="mcp__obsidian-write__obsidian_keeper_save",
                tool_use_id="save",
                tool_input=arguments,
            )
            self.assertEqual(
                native.hook(
                    home, profile, self.event("PreToolUse", session_id=session, **call)
                ),
                {},
            )
            plan = state["session_save"]
            receipt = dict(
                status="committed",
                request_id="history-fixture",
                idempotency_key=arguments["idempotency_key"],
                path=plan["path"],
                affected_paths=[plan["path"], plan["index"], plan["daily"]],
                warnings=[],
                recovery={"required": False, "action": ""},
                error_code=None,
                retryable=False,
            )
            native.hook(
                home,
                profile,
                self.event(
                    "PostToolUse",
                    session_id=session,
                    **call,
                    tool_response={"structuredContent": receipt},
                ),
            )
        drained = self.drain()
        self.assertTrue(drained["quiescent"])
        self.assertEqual(drained["session_count"], 258)
        self.assertGreaterEqual(drained["archived_session_count"], 257)
        self.assertTrue(drained["complete"])
        old = self.cli(
            "status", "--home", str(home), "--session", self.session_key("history-0")
        )
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(json.loads(old.stdout)["disposition"], "saved")
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"user","uuid":"resume"}\n'
        )
        resumed = self.hook(
            "UserPromptSubmit", session_id="history-0", prompt="Resumed work"
        )
        self.assertIn("obsidian_keeper_save", json.dumps(resumed))
        self.assertFalse(self.drain()["quiescent"])

    def test_archive_interruption_reopens_idempotently_without_unlinking_locks(self):
        self.configure()
        path = self.terminal_session()
        original = path.read_bytes()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        real_write = native.atomic_write

        def interrupt_index(target, body, **options):
            if target.name == "index.json":
                raise OSError("fixture interrupted archive")
            return real_write(target, body, **options)

        with mock.patch.object(native, "atomic_write", side_effect=interrupt_index):
            native.hook(
                home,
                profile,
                self.event("UserPromptSubmit", session_id="second", prompt="New work"),
            )
        self.assertTrue((home / "retention-journal.json").is_file())
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(path.with_suffix(".lock").exists())
        output = self.hook("UserPromptSubmit", session_id="second", prompt="New work")
        self.assertIn("obsidian_keeper_save", json.dumps(output))
        self.assertFalse(path.exists())
        self.assertTrue(path.with_suffix(".lock").exists())
        old = self.cli("status", "--home", str(home), "--session", path.stem)
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(json.loads(old.stdout)["turn"], json.loads(original)["turn"])

    def test_abrupt_retention_publication_exit_cleans_only_known_temporary_bytes(self):
        for phase in (
            "archive-record",
            "archive-index",
            "archive-locator",
            "restore-state",
            "restore-binding",
            "restore-index",
            "ambiguous",
        ):
            with self.subTest(phase=phase):
                self.home = self.directory / phase
                self.configure()
                path = self.terminal_session()
                original = path.read_bytes()
                restore = phase.startswith("restore")
                if restore:
                    self.hook("UserPromptSubmit", session_id="other", prompt="Other")
                target_session = "session-1" if restore else "other"
                program = """
import json, os, sys, types
from pathlib import Path
script, profile, phase, session, event = sys.argv[1:]
module = types.ModuleType("crash_fixture")
module.__file__ = script
exec(compile(Path(script).read_text(), script, "exec"), module.__dict__)
original = module.os.replace
def crash(source, target):
    target = Path(target)
    wanted = (
        (phase in ("archive-index", "restore-index", "ambiguous") and target.name == "index.json")
        or (phase == "archive-record" and target.parent.name == session and target.name != "index.json")
        or (phase == "archive-locator" and target.parent.name == "retained-bindings")
        or (phase == "restore-state" and target.parent.name == "capture-state" and target.stem == session)
        or (phase == "restore-binding" and target.parent.name == "bindings")
    )
    if wanted:
        os._exit(73)
    return original(source, target)
module.os.replace = crash
home, descriptor = module.load_profile(profile)
module.hook(home, descriptor, json.loads(event))
"""
                crashed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        program,
                        str(self.bundle / "bin/obsidian-mcp-native"),
                        str(self.home),
                        phase,
                        path.stem,
                        json.dumps(
                            self.event(
                                "SessionStart" if restore else "UserPromptSubmit",
                                session_id=target_session,
                                prompt="Other",
                                source="resume",
                            )
                        ),
                    ],
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(crashed.returncode, 73, crashed.stderr)
                leftovers = list(self.home.rglob(".capture-*"))
                self.assertTrue(leftovers)
                self.assertTrue((self.home / "retention-journal.json").exists())
                if phase == "ambiguous":
                    leftovers[0].write_bytes(b"preserve ambiguous fixture")
                resumed = self.hook(
                    "SessionStart", session_id=target_session, source="resume"
                )
                if phase == "ambiguous":
                    self.assertEqual(
                        leftovers[0].read_bytes(), b"preserve ambiguous fixture"
                    )
                    self.assertTrue((self.home / "retention-journal.json").exists())
                    self.assertFalse(self.drain()["quiescent"])
                else:
                    self.assertEqual(
                        list(self.home.rglob(".capture-*")), [], json.dumps(resumed)
                    )
                    self.assertFalse((self.home / "retention-journal.json").exists())
                    inspected = self.cli(
                        "status", "--home", str(self.home), "--session", path.stem
                    )
                    self.assertEqual(inspected.returncode, 0, inspected.stderr)
                    self.assertEqual(
                        json.loads(inspected.stdout)["turn"],
                        json.loads(original)["turn"],
                    )
                    self.assertTrue(self.drain()["complete"])

    def test_archive_journal_conflict_and_missing_archive_never_fresh_enroll(self):
        self.configure()
        path = self.terminal_session()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        real_write = native.atomic_write

        def interrupt_index(target, body, **options):
            if target.name == "index.json":
                raise OSError("fixture interrupted archive")
            return real_write(target, body, **options)

        with mock.patch.object(native, "atomic_write", side_effect=interrupt_index):
            native.hook(
                home,
                profile,
                self.event("UserPromptSubmit", session_id="second", prompt="New work"),
            )
        changed = json.loads(path.read_text())
        changed["turn"] += "conflict"
        path.write_text(json.dumps(changed))
        output = self.hook("UserPromptSubmit", session_id="second", prompt="New work")
        self.assertNotIn("decision", output)
        self.assertIn("RETENTION_CONFLICT", json.dumps(output))
        self.assertEqual(json.loads(path.read_text()), changed)
        drain = self.cli("drain-status", "--home", str(home))
        self.assertTrue(
            drain.returncode != 0 or json.loads(drain.stdout)["quiescent"] is False
        )

    def setUp(self):
        super().setUp()
        self.transcript = self.directory / "conversation.jsonl"
        self.transcript.write_text('{"type":"system","uuid":"start"}\n')

    def test_archive_and_restore_interrupted_move_boundaries_reopen(self):
        for phase in (
            "archive-index",
            "archive-binding",
            "archive-state",
            "restore-state",
            "restore-binding",
            "restore-index",
        ):
            with self.subTest(phase=phase):
                self.home = self.directory / phase
                self.configure()
                path = self.terminal_session()
                original = json.loads(path.read_text())
                native = self.load_native()
                home, profile = native.load_profile(self.home)
                if phase.startswith("restore"):
                    self.hook(
                        "UserPromptSubmit", session_id="second", prompt="Other work"
                    )
                real_write, real_unlink = native.atomic_write, Path.unlink
                interrupted = []

                def write_boundary(target, body, **options):
                    real_write(target, body, **options)
                    wanted = (
                        (phase.endswith("index") and target.name == "index.json")
                        or (phase == "restore-state" and target == path)
                        or (
                            phase == "restore-binding"
                            and target == home / "bindings" / (path.stem + ".json")
                        )
                    )
                    if wanted and not interrupted:
                        interrupted.append(True)
                        raise OSError("fixture interrupted after publication")

                def unlink_boundary(target, *args, **kwargs):
                    real_unlink(target, *args, **kwargs)
                    wanted = (phase == "archive-state" and target == path) or (
                        phase == "archive-binding"
                        and target == home / "bindings" / (path.stem + ".json")
                    )
                    if wanted and not interrupted:
                        interrupted.append(True)
                        raise OSError("fixture interrupted after unlink")

                target_session = (
                    "session-1" if phase.startswith("restore") else "second"
                )
                with mock.patch.object(
                    native, "atomic_write", side_effect=write_boundary
                ), mock.patch.object(Path, "unlink", new=unlink_boundary):
                    native.hook(
                        home,
                        profile,
                        self.event(
                            "UserPromptSubmit",
                            session_id=target_session,
                            prompt="Resumed work",
                        ),
                    )
                self.assertTrue(interrupted)
                self.assertTrue((home / "retention-journal.json").exists())
                incomplete = self.drain()
                self.assertFalse(incomplete["quiescent"])
                self.assertFalse(incomplete["complete"])
                reopened = self.hook(
                    "UserPromptSubmit", session_id=target_session, prompt="Resumed work"
                )
                self.assertIn("obsidian_keeper_save", json.dumps(reopened))
                self.assertFalse((home / "retention-journal.json").exists())
                self.assertTrue(path.with_suffix(".lock").exists())
                snapshots = [
                    json.loads(p.read_text())["state"]
                    for p in (home / "archive" / path.stem).glob("*.json")
                    if p.name != "index.json"
                ]
                self.assertIn(original, snapshots)

    def test_archived_missing_evidence_and_conflicting_hot_copy_never_fall_back(self):
        for failure in ("missing-generation", "missing-directory", "conflicting-hot"):
            with self.subTest(failure=failure):
                self.home = self.directory / failure
                self.configure()
                path = self.terminal_session()
                self.hook("UserPromptSubmit", session_id="second", prompt="Other work")
                directory = self.home / "archive" / path.stem
                index = json.loads((directory / "index.json").read_text())
                generation = directory / (index["generation"] + ".json")
                if failure == "missing-generation":
                    generation.unlink()
                elif failure == "missing-directory":
                    directory.rename(self.home / "preserved-archive")
                else:
                    record = json.loads(generation.read_text())
                    binding = self.home / "bindings" / (path.stem + ".json")
                    binding.write_text(json.dumps(record["binding"]))
                    binding.chmod(0o600)
                output = self.hook("UserPromptSubmit", prompt="Resume old work")
                self.assertNotIn("decision", output)
                self.assertNotIn("obsidian_keeper_save", json.dumps(output))
                self.assertFalse(path.exists())
                self.assertEqual(
                    self.cli("drain-status", "--home", str(self.home)).returncode, 64
                )

    def test_historical_locks_are_not_admission_but_busy_locks_refuse_drain(self):
        self.configure()
        path = self.terminal_session()
        self.hook("UserPromptSubmit", session_id="second", prompt="Other work")
        child = path.parents[1]
        for number in range(270):
            lock = (
                child
                / "capture-state"
                / (self.session_key("unused-" + str(number)) + ".lock")
            )
            lock.touch(mode=0o600)
        self.assertFalse(self.drain()["quiescent"])
        native = self.load_native()
        with native.state_lock(child, path.stem):
            self.assertEqual(
                self.cli("drain-status", "--home", str(self.home)).returncode, 64
            )
        with native.gate_lock(child):
            self.assertEqual(
                self.cli("drain-status", "--home", str(self.home)).returncode, 64
            )
        self.assertTrue(path.with_suffix(".lock").exists())

    def test_retired_workspaces_do_not_consume_active_admission(self):
        self.configure()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        with mock.patch.object(native, "MAX_DRAIN_SESSIONS", 2):
            for number in range(5):
                root = self.directory / ("workspace-" + str(number))
                root.mkdir()
                session = "workspace-session-" + str(number)
                result = native.hook(
                    home,
                    profile,
                    self.event(
                        "UserPromptSubmit",
                        cwd=str(root),
                        session_id=session,
                        prompt="Work",
                    ),
                )
                self.assertIn("obsidian_keeper_save", json.dumps(result))
                key = self.session_key(session)
                path = next(
                    (home / "workspaces").glob("*/capture-state/" + key + ".json")
                )
                state = json.loads(path.read_text())
                skipped = self.cli(
                    "skip",
                    "--home",
                    str(home),
                    "--session",
                    key,
                    "--turn",
                    state["turn"],
                    "--reason",
                    "no_meaningful_work",
                )
                self.assertEqual(skipped.returncode, 0, skipped.stderr)
                root.rename(root.with_name(root.name + "-retired"))
            drained = native.user_drain_status(home, profile)
            self.assertTrue(drained["quiescent"])
            self.assertTrue(drained["complete"])
            self.assertEqual(drained["workspace_count"], 5)
            self.assertEqual(drained["session_count"], 5)
            for number in range(3):
                result = native.hook(
                    home,
                    profile,
                    self.event(
                        "UserPromptSubmit",
                        session_id="active-" + str(number),
                        prompt="Active work",
                    ),
                )
                self.assertEqual(
                    "DRAIN_SESSION_LIMIT" in json.dumps(result), number == 2
                )
        self.assertFalse(self.drain()["quiescent"])

    @unittest.skipUnless(
        os.environ.get("OBSIDIAN_MCP_TEST_LONG_HISTORY"),
        "optional production-threshold CLI history acceptance",
    )
    def test_cli_accepts_257_retired_workspaces_without_new_parent(self):
        self.configure()
        for number in range(257):
            root = self.directory / ("workspace-" + str(number))
            root.mkdir()
            session = "workspace-session-" + str(number)
            output = self.hook(
                "UserPromptSubmit", cwd=str(root), session_id=session, prompt="Work"
            )
            self.assertIn("obsidian_keeper_save", json.dumps(output), number)
            key = self.session_key(session)
            path = next(
                (self.home / "workspaces").glob("*/capture-state/" + key + ".json")
            )
            state = json.loads(path.read_text())
            skipped = self.cli(
                "skip",
                "--home",
                str(self.home),
                "--session",
                key,
                "--turn",
                state["turn"],
                "--reason",
                "no_meaningful_work",
            )
            self.assertEqual(skipped.returncode, 0, skipped.stderr)
            root.rename(root.with_name(root.name + "-retired"))
        result = self.drain()
        self.assertTrue(result["complete"])
        self.assertTrue(result["quiescent"])
        self.assertEqual(result["workspace_count"], 257)
        self.assertEqual(result["session_count"], 257)

    def configure(self, *extra):
        result = self.cli(
            "attach",
            "--scope",
            "user",
            "--host",
            "claude-plugin",
            "--home",
            str(self.home),
            "--folder",
            "Sessions",
            "--daily-folder",
            "Journal",
            "--release-sha256",
            self.digest,
            *extra,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def state_paths(self):
        return list(
            (self.home / "workspaces").glob(
                "*/capture-state/" + self.session_key("session-1") + ".json"
            )
        )

    def drain(self):
        result = self.cli("drain-status", "--home", str(self.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_user_attach_schema_capabilities_and_no_workspace_files(self):
        before = set(self.project.rglob("*"))
        self.configure()
        profile = json.loads((self.home / "capture-profile.json").read_text())
        self.assertEqual(profile["scope"], "user")
        self.assertNotIn("project", profile)
        self.assertNotIn("projectMode", profile)
        self.assertEqual(profile["files"], {})
        status = self.cli("profile-status", "--home", str(self.home))
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["scope"], "user")
        self.assertIsNone(json.loads(status.stdout).get("project"))
        self.assertNotIn("projectMode", json.loads(status.stdout))
        capabilities = self.cli("capabilities", "--release-sha256", self.digest)
        self.assertEqual(
            json.loads(capabilities.stdout)["captureScopes"],
            {
                "claude-plugin": ["project", "user"],
                "codex": ["project"],
            },
        )
        self.assertTrue(self.drain()["quiescent"])
        self.hook("UserPromptSubmit", prompt="work")
        self.assertEqual(set(self.project.rglob("*")), before)
        for path in self.home.rglob("*"):
            self.assertEqual(
                path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600
            )
        self.assertFalse((self.home / "capture-state/active.json").exists())

    def test_new_workspaces_git_subdirectories_symlinks_and_nongit(self):
        self.configure()
        nested = self.project / "nested"
        nested.mkdir()
        alias = self.directory / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        self.hook("UserPromptSubmit", cwd=str(alias), prompt="first")
        self.hook(
            "UserPromptSubmit",
            cwd=str(self.project),
            session_id="second",
            prompt="second",
        )
        plain = self.directory / "plain"
        plain.mkdir()
        output = self.hook(
            "UserPromptSubmit", cwd=str(plain), session_id="plain", prompt="third"
        )
        self.assertIn("session-only", json.dumps(output))
        children = list((self.home / "workspaces").iterdir())
        self.assertEqual(len(children), 2)
        profiles = [
            json.loads((p / "capture-profile.json").read_text()) for p in children
        ]
        self.assertEqual(
            {p["project"] for p in profiles}, {str(self.project), str(plain)}
        )
        self.assertEqual({p["projectMode"] for p in profiles}, {"git", "directory"})
        status = self.drain()
        self.assertFalse(status["quiescent"])
        self.assertEqual(status["session_count"], 3)
        self.assertEqual(status["workspace_count"], 2)
        self.assertFalse(list(plain.iterdir()))

    def test_session_binding_never_moves_pending_work_to_another_workspace(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="original")
        path = self.state_paths()[0]
        before = path.read_bytes()
        other = self.directory / "other"
        other.mkdir()
        advisory = self.hook("UserPromptSubmit", cwd=str(other), prompt="next")
        self.assertNotIn("decision", advisory)
        self.assertIn("SESSION_WORKSPACE_CHANGED", json.dumps(advisory))
        self.assertEqual(path.read_bytes(), before)
        shell = self.hook(
            "PreToolUse",
            cwd=str(other),
            tool_name="Bash",
            tool_use_id="safe",
            tool_input={"command": "pwd"},
        )
        self.assertNotIn("permissionDecision", shell.get("hookSpecificOutput", {}))
        denied = self.hook(
            "PreToolUse",
            cwd=str(other),
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="unsafe",
            tool_input={},
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(list((self.home / "workspaces").iterdir())), 1)

    def test_unresolved_previous_turn_is_retained_and_next_prompt_is_advisory(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="original")
        path = self.state_paths()[0]
        before = path.read_bytes()
        self.transcript.write_text(
            self.transcript.read_text() + '{"type":"user","uuid":"next"}\n'
        )
        output = self.hook("UserPromptSubmit", prompt="new ordinary prompt")
        self.assertNotIn("decision", output)
        self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", json.dumps(output))
        self.assertEqual(path.read_bytes(), before)
        arguments = self.session_arguments(path)
        denied = self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="save",
            tool_input=arguments,
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(path.read_bytes(), before)
        stopped = self.hook("Stop", stop_hook_active=False)
        self.assertNotIn("decision", stopped)
        self.assertFalse(self.drain()["quiescent"])
        old = json.loads(before)
        skipped = self.cli(
            "skip",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--turn",
            old["turn"],
            "--reason",
            "no_meaningful_work",
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)
        self.hook("UserPromptSubmit", prompt="new ordinary prompt")
        self.assertNotEqual(json.loads(path.read_text())["turn"], old["turn"])

    def test_children_reuse_commit_engine_and_parent_cli_routes_bound_session(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path, state, commit = self.pending_commit()
        self.assertEqual(state["commits"][commit]["status"], "pending")
        self.assertEqual(self.prepare(path, commit).returncode, 0)
        status = self.cli("status", "--home", str(self.home), "--session", path.stem)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["gate"]["session"], path.stem)
        self.assertFalse((self.home / "capture-state/active.json").exists())
        self.assertFalse(self.drain()["quiescent"])

    def test_tampered_child_or_binding_fails_closed_and_drain_refuses(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        child = self.state_paths()[0].parents[1]
        descriptor = child / "capture-profile.json"
        original = descriptor.read_bytes()
        profile = json.loads(original)
        profile["folder"] = "Other"
        descriptor.write_text(json.dumps(profile))
        result = self.cli("drain-status", "--home", str(self.home))
        self.assertEqual(result.returncode, 64)
        direct = self.cli("profile-status", "--home", str(child))
        self.assertEqual(direct.returncode, 64)
        profile = json.loads(original)
        profile.pop("parentHome")
        descriptor.write_text(json.dumps(profile))
        self.assertEqual(
            self.cli("profile-status", "--home", str(child)).returncode, 64
        )
        descriptor.write_bytes(original)
        binding = self.home / "bindings" / (self.session_key("session-1") + ".json")
        record = json.loads(binding.read_text())
        record["workspace"] = "0" * 64
        binding.write_text(json.dumps(record))
        self.assertEqual(
            self.cli("drain-status", "--home", str(self.home)).returncode, 64
        )
        denied = self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian-write__obsidian_keeper_save",
            tool_use_id="bad",
            tool_input={},
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_primitive_tool_input_denies_protected_writes_without_blocking_bash(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        before = self.state_paths()[0].read_bytes()
        for malformed in (None, [], 1, "bad"):
            with self.subTest(malformed=malformed):
                denied = self.hook(
                    "PreToolUse",
                    tool_name="mcp__obsidian-write__obsidian_keeper_save",
                    tool_use_id="bad",
                    tool_input=malformed,
                )
                self.assertEqual(
                    denied["hookSpecificOutput"]["permissionDecision"], "deny"
                )
                shell = self.hook(
                    "PreToolUse",
                    tool_name="Bash",
                    tool_use_id="bad",
                    tool_input=malformed,
                )
                self.assertNotIn(
                    "permissionDecision", shell.get("hookSpecificOutput", {})
                )
        self.assertEqual(self.state_paths()[0].read_bytes(), before)

    def test_user_scope_rejects_project_arguments_and_codex_and_limits_active_work(
        self,
    ):
        rejected = self.cli(
            "attach",
            "--host",
            "claude-plugin",
            "--scope",
            "user",
            "--home",
            str(self.home),
            "--project",
            str(self.project),
            "--folder",
            "Sessions",
            "--release-sha256",
            self.digest,
        )
        self.assertEqual(rejected.returncode, 64)
        self.assertFalse(self.home.exists())
        rejected = self.cli(
            "create",
            "--scope",
            "user",
            "--home",
            str(self.home),
            "--folder",
            "Sessions",
            "--release-sha256",
            self.digest,
        )
        self.assertEqual(rejected.returncode, 64)
        self.assertFalse(self.home.exists())
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        with mock.patch.object(native, "MAX_DRAIN_SESSIONS", 1):
            output = native.hook(
                home,
                profile,
                self.event("UserPromptSubmit", session_id="extra", prompt="work"),
            )
        self.assertNotIn("decision", output)
        self.assertIn("DRAIN_SESSION_LIMIT", json.dumps(output))
        self.assertEqual(len(list((self.home / "bindings").iterdir())), 1)

    def test_missing_enrolled_state_or_unknown_parent_state_is_not_drained(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        path = self.state_paths()[0]
        original = path.read_bytes()
        path.unlink()
        self.assertEqual(
            self.cli("drain-status", "--home", str(self.home)).returncode, 64
        )
        path.write_bytes(original)
        path.chmod(0o600)
        unexpected = self.home / "capture-state" / "unknown.json"
        unexpected.write_text("{}")
        unexpected.chmod(0o600)
        self.assertEqual(
            self.cli("drain-status", "--home", str(self.home)).returncode, 64
        )

    def test_missing_git_uses_session_only_and_commit_writes_refuse(self):
        self.configure()
        native = self.load_native()
        home, profile = native.load_profile(self.home)
        with mock.patch.object(native, "git", side_effect=FileNotFoundError):
            output = native.hook(
                home, profile, self.event("UserPromptSubmit", prompt="work")
            )
            shell = native.hook(
                home,
                profile,
                self.event(
                    "PreToolUse",
                    tool_name="Bash",
                    tool_use_id="ordinary",
                    tool_input={"command": "pwd"},
                ),
            )
            denied = native.hook(
                home,
                profile,
                self.event(
                    "PreToolUse",
                    tool_name="mcp__obsidian-write__obsidian_daily_append",
                    tool_use_id="fake",
                    tool_input={
                        "skip_if_hash": "a" * 40,
                        "idempotency_key": "commit-capture:fake",
                    },
                ),
            )
        self.assertIn("session-only", json.dumps(output))
        path = self.state_paths()[0]
        self.assertEqual(json.loads(path.read_text())["commits"], {})
        self.assertNotIn("permissionDecision", shell.get("hookSpecificOutput", {}))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("DIRECTORY_SESSION_ONLY", json.dumps(denied))

    def test_two_git_workspaces_have_independent_pending_commit_gates(self):
        self.configure()
        other = self.directory / "other-git"
        other.mkdir()
        initialized = subprocess.run(
            ["git", "-C", str(other), "init"],
            env=self.environment,
            text=True,
            capture_output=True,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        for session, cwd in (("first", self.project), ("second", other)):
            self.hook(
                "UserPromptSubmit", session_id=session, cwd=str(cwd), prompt="work"
            )
            rewritten = self.hook(
                "PreToolUse",
                session_id=session,
                cwd=str(cwd),
                tool_name="Bash",
                tool_use_id="commit",
                tool_input={"command": "git commit -m fixture"},
            )
            self.assertIn("updatedInput", rewritten["hookSpecificOutput"])
        gates = list((self.home / "workspaces").glob("*/capture-state/active.json"))
        self.assertEqual(len(gates), 2)
        self.assertEqual(
            {json.loads(p.read_text())["session"] for p in gates},
            {self.session_key("first"), self.session_key("second")},
        )
        self.assertFalse((self.home / "capture-state/active.json").exists())
        self.assertFalse(self.drain()["quiescent"])

    def test_bound_and_unbound_busy_hooks_do_not_block_prompts_or_unrelated_bash(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        native = self.load_native()
        with native.gate_lock(self.home):
            prompt = self.hook("UserPromptSubmit", prompt="more work")
            self.assertNotIn("decision", prompt)
            shell = self.hook(
                "PreToolUse",
                tool_name="Bash",
                tool_use_id="pwd",
                tool_input={"command": "pwd"},
            )
            self.assertNotIn("permissionDecision", shell.get("hookSpecificOutput", {}))
            self.assertEqual(
                self.cli("drain-status", "--home", str(self.home)).returncode, 64
            )
        (self.home / "bindings" / (self.session_key("session-1") + ".json")).unlink()
        output = self.hook("Stop", session_id="unbound", stop_hook_active=False)
        self.assertNotIn("decision", output)
        self.assertEqual(
            self.cli("drain-status", "--home", str(self.home)).returncode, 64
        )

    def test_symlinked_child_and_binding_and_unknown_scopes_refuse(self):
        self.configure()
        self.hook("UserPromptSubmit", prompt="work")
        binding = self.home / "bindings" / (self.session_key("session-1") + ".json")
        real = self.home / "real-binding"
        binding.rename(real)
        binding.symlink_to(real)
        self.assertEqual(
            self.cli("drain-status", "--home", str(self.home)).returncode, 64
        )
        binding.unlink()
        real.rename(binding)
        child = self.state_paths()[0].parents[1]
        backup = self.home / "real-child"
        child.rename(backup)
        child.symlink_to(backup, target_is_directory=True)
        self.assertEqual(
            self.cli("drain-status", "--home", str(self.home)).returncode, 64
        )
        child.unlink()
        backup.rename(child)
        descriptor = self.home / "capture-profile.json"
        profile = json.loads(descriptor.read_text())
        profile["scope"] = "typo"
        descriptor.write_text(json.dumps(profile))
        self.assertEqual(
            self.cli("profile-status", "--home", str(self.home)).returncode, 64
        )

    @unittest.skipUnless(
        os.environ.get("OBSIDIAN_MCP_TEST_PLUGIN_ROOT"),
        "optional public plugin checkout not supplied",
    )
    def test_user_plugin_handoff_enrolls_new_git_and_nongit_and_drains_receipts(self):
        plugin = Path(os.environ["OBSIDIAN_MCP_TEST_PLUGIN_ROOT"]).resolve(strict=True)
        fixture_home = self.directory / "home"
        fixture_home.mkdir()
        self.environment.update(
            HOME=str(fixture_home),
            XDG_STATE_HOME=str(self.directory / "state"),
            XDG_CONFIG_HOME=str(self.directory / "config"),
            XDG_CACHE_HOME=str(self.directory / "cache"),
        )
        prefix = self.directory / "installed-client"
        installed = subprocess.run(
            [
                str(self.bundle / "bin/obsidian-mcp-client-install"),
                "--prefix",
                str(prefix),
                "--bundle",
                str(self.bundle),
                "--release-sha256",
                self.digest,
            ],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.bundle = prefix / "current"
        self.configure()
        ownership_path = (
            Path(self.environment["XDG_STATE_HOME"])
            / "claude-obsidian/capture-ownership/owner.json"
        )
        self.assertFalse(ownership_path.exists())
        switched = subprocess.run(
            [
                "python3",
                str(plugin / "scripts/lib/capture-owner.py"),
                "handoff",
                "--owner",
                "mcp",
                "--scope",
                "user",
                "--profile",
                str(self.home),
                "--verification",
                "legacy-writers-stopped-and-history-inspected",
            ],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(switched.returncode, 0, switched.stderr)
        ownership = json.loads(ownership_path.read_text())
        self.assertEqual(ownership["default_profile"], str(self.home))
        self.assertEqual(ownership["profiles"], {})
        plain = self.directory / "unregistered-notes"
        plain.mkdir()
        for session, cwd in (
            ("new-git-session", self.project),
            ("new-directory-session", plain),
        ):

            def dispatch(name, **extra):
                result = subprocess.run(
                    ["bash", str(plugin / "scripts/capture-hook.sh"), name],
                    input=json.dumps(
                        self.event(name, session_id=session, cwd=str(cwd), **extra)
                    ),
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout or "{}")

            before = set(cwd.rglob("*"))
            prompt = dispatch(
                "UserPromptSubmit", prompt="Capture newly opened workspace"
            )
            self.assertIn(
                "mcp__obsidian-write__obsidian_keeper_save", json.dumps(prompt)
            )
            paths = list(
                (self.home / "workspaces").glob(
                    "*/capture-state/" + self.session_key(session) + ".json"
                )
            )
            self.assertEqual(len(paths), 1)
            path = paths[0]
            self.assertFalse(self.drain()["quiescent"])
            args = self.session_arguments(path)
            plan = json.loads(path.read_text())["session_save"]
            call = dict(
                tool_name="mcp__obsidian-write__obsidian_keeper_save",
                tool_use_id="paired-save",
                tool_input=args,
            )
            self.assertEqual(dispatch("PreToolUse", **call), {})
            receipt = dict(
                status="committed",
                request_id="paired-user-fixture",
                idempotency_key=args["idempotency_key"],
                path=plan["path"],
                affected_paths=[plan["path"], plan["index"], plan["daily"]],
                warnings=[],
                recovery={"required": False, "action": ""},
                error_code=None,
                retryable=False,
            )
            self.assertEqual(
                dispatch("PostToolUse", **call, tool_response=json.dumps(receipt)), {}
            )
            self.assertEqual(json.loads(path.read_text())["disposition"], "saved")
            self.assertEqual(dispatch("Stop", stop_hook_active=False), {})
            self.assertEqual(set(cwd.rglob("*")), before)
        drained = self.drain()
        self.assertTrue(drained["quiescent"])
        self.assertEqual(drained["workspace_count"], 2)
        self.assertEqual(drained["session_count"], 2)
        self.assertEqual(json.loads(ownership_path.read_text()), ownership)


class CommitRoutesTest(NativeCaptureFixture):
    expected_rewrite_decision = None
    configure = UserCaptureTest.configure
    commit_arguments = ClaudePluginCaptureTest.commit_arguments
    commit_event = ClaudePluginCaptureTest.commit_event
    commit_receipt = ClaudePluginCaptureTest.commit_receipt

    def event(self, name, **extra):
        return ClaudePluginCaptureTest.event(
            self,
            name,
            **dict(session_id=getattr(self, "current_session", "session-1"), **extra),
        )

    def state_paths(self):
        key = self.session_key(getattr(self, "current_session", "session-1"))
        return list((self.home / "workspaces").glob("*/capture-state/" + key + ".json"))

    def setUp(self):
        super().setUp()
        self.transcript = self.directory / "conversation.jsonl"
        self.transcript.write_text('{"type":"system","uuid":"start"}\n')
        self.routes = {
            "business/engine": "Business/engine/commits/{date}-engine.md",
            "business/other": "Business/other/{date}.md",
        }
        self.policy = self.directory / "routes.json"
        self.policy.write_text(json.dumps(self.routes))
        self.policy.chmod(0o600)

    def start(self, routed=True):
        self.git("remote", "add", "origin", "git@github.com:business/engine.git")
        self.configure(*(["--commit-routes", str(self.policy)] if routed else []))
        self.hook("UserPromptSubmit", prompt="work")
        return self.pending_commit()

    def change_snapshot(self, routes):
        paths = [self.home / "capture-profile.json"]
        paths.extend((self.home / "workspaces").glob("*/capture-profile.json"))
        for path in paths:
            profile = json.loads(path.read_text())
            profile["commitRoutes"] = routes
            path.write_text(json.dumps(profile))

    def test_shared_selection_exact_routes_nonmatch_and_canonical_compatibility(self):
        consumer = runpy.run_path(str(self.bundle / "bin/obsidian-mcp-capture-commit"))
        metadata = dict(
            org_repo="business/engine",
            date="2026-10-29",
            hash="a" * 40,
            branch="fixture",
            time="10:30",
        )
        canonical, original = consumer["capture_arguments"](metadata, ["fact"])
        self.assertEqual(
            canonical, "Projects/Development/business/engine/2026-10-29.md"
        )
        self.assertEqual(consumer["capture_target"](metadata, {}), canonical)
        for repo, expected in (
            ("business/engine", "Business/engine/commits/2026-10-29-engine.md"),
            ("business/other", "Business/other/2026-10-29.md"),
            (
                "business/engine-extra",
                "Projects/Development/business/engine-extra/2026-10-29.md",
            ),
        ):
            with self.subTest(repo=repo):
                selected = consumer["capture_target"](
                    dict(metadata, org_repo=repo), self.routes
                )
                self.assertEqual(selected, expected)
        target, routed = consumer["capture_arguments"](
            metadata, ["fact"], routes=self.routes
        )
        self.assertEqual(target, "Business/engine/commits/2026-10-29-engine.md")
        self.assertEqual(routed, dict(original, target_path=target))

    def test_attach_snapshots_once_and_new_roots_inherit_without_enrollment(self):
        self.configure("--commit-routes", str(self.policy))
        self.policy.write_text("invalid changed external policy")
        for name, prefix, suffix in (
            ("engine", "Business/engine/commits/", "-engine.md"),
            ("other", "Business/other/", ".md"),
            ("engine-extra", "Projects/Development/business/engine-extra/", ".md"),
        ):
            self.project = self.directory / name
            self.project.mkdir()
            self.current_session = name
            self.git("init", "-b", "native-fixture")
            self.git(
                "remote", "add", "origin", "git@github.com:business/" + name + ".git"
            )
            self.hook("UserPromptSubmit", prompt="work")
            path, state, commit = self.pending_commit()
            expected = prefix + state["commits"][commit]["metadata"]["date"] + suffix
            prepared = self.prepare(path, commit)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            self.assertEqual(
                json.loads(prepared.stdout)["arguments"]["target_path"], expected
            )
        profiles = [
            json.loads(p.read_text())
            for p in (self.home / "workspaces").glob("*/capture-profile.json")
        ]
        self.assertEqual(len(profiles), 3)
        self.assertTrue(all(p["commitRoutes"] == self.routes for p in profiles))
        self.assertNotIn(
            str(self.policy), (self.home / "capture-profile.json").read_text()
        )

    def test_empty_map_readonly_file_preserves_canonical_target(self):
        self.policy.write_text("{}")
        self.policy.chmod(0o400)
        path, state, commit = self.start()
        expected = (
            "Projects/Development/business/engine/"
            + state["commits"][commit]["metadata"]["date"]
            + ".md"
        )
        self.assertEqual(state["commits"][commit]["capture_path"], expected)
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(
            json.loads(prepared.stdout)["arguments"]["target_path"], expected
        )

    def test_observation_freezes_before_context_and_retry_preserves_same_key_body(self):
        path, state, commit = self.start()
        pending = state["commits"][commit]
        expected = (
            "Business/engine/commits/" + pending["metadata"]["date"] + "-engine.md"
        )
        self.assertEqual(pending["capture_path"], expected)
        self.assertNotIn("native_arguments", pending)
        self.change_snapshot({"business/engine": "Changed/{date}.md"})
        arguments = self.commit_arguments(path, ["Original fact"])
        self.assertEqual(arguments["target_path"], expected)
        event = self.commit_event(arguments)
        self.assertEqual(self.hook("PreToolUse", **event), {})
        denied = self.hook(
            "PreToolUse",
            **self.commit_event(
                dict(arguments, content=arguments["content"] + "\n- Changed fact")
            ),
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.hook("PostToolUseFailure", **event, error="fixture failure")
        pending = json.loads(path.read_text())["commits"][commit]
        retry = self.cli(
            "retry",
            "--home",
            str(self.home),
            "--session",
            path.stem,
            "--commit",
            commit,
            "--payload-sha256",
            pending["payload_digest"],
            "--verification",
            "authoritative-path-inspected",
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["arguments"], arguments)
        event = self.commit_event(arguments, "reviewed")
        self.assertEqual(self.hook("PreToolUse", **event), {})
        self.hook("PostToolUse", **event, tool_response=self.commit_receipt(arguments))
        self.assertEqual(
            json.loads(path.read_text())["commits"][commit]["status"], "committed"
        )

    def test_executor_observation_survives_policy_change_before_post_hook(self):
        self.git("remote", "add", "origin", "git@github.com:business/engine.git")
        self.configure("--commit-routes", str(self.policy))
        self.hook("UserPromptSubmit", prompt="work")
        args = dict(
            tool_name="Bash",
            tool_use_id="commit-call",
            tool_input={"command": "git commit -m fixture"},
        )
        rewritten = self.hook("PreToolUse", **args)
        (self.project / "file.txt").write_text("fixture\n")
        self.git("add", "file.txt")
        executed = self.execute_rewritten(rewritten)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.change_snapshot({"business/engine": "Changed/{date}.md"})
        self.hook("PostToolUse", **args, tool_response=executed.stdout)
        path = self.state_paths()[0]
        pending = next(iter(json.loads(path.read_text())["commits"].values()))
        self.assertEqual(
            pending["capture_path"],
            "Business/engine/commits/" + pending["metadata"]["date"] + "-engine.md",
        )

    def test_legacy_unprepared_conflicting_policy_requires_reconciliation(self):
        path, state, commit = self.start(routed=False)
        state["commits"][commit].pop("capture_path", None)
        path.write_text(json.dumps(state))
        self.change_snapshot(self.routes)
        before = path.read_bytes()
        result = self.prepare(path, commit)
        self.assertEqual(result.returncode, 64)
        self.assertIn("COMMIT_ROUTE_RECONCILIATION_REQUIRED", result.stderr)
        self.assertEqual(path.read_bytes(), before)
        advisory = self.hook("SessionStart", source="resume")
        self.assertIn("COMMIT_ROUTE_RECONCILIATION_REQUIRED", json.dumps(advisory))
        self.assertNotIn("Commit capture arguments:", json.dumps(advisory))
        self.change_snapshot({})
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertTrue(
            json.loads(prepared.stdout)["arguments"]["target_path"].startswith(
                "Projects/Development/business/engine/"
            )
        )

    def test_legacy_prepared_exact_arguments_survive_changed_policy(self):
        path, state, commit = self.start(routed=False)
        first = self.prepare(path, commit)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = json.loads(first.stdout)
        state = json.loads(path.read_text())
        state["commits"][commit]["path"] = "Historical/exact.md"
        state["commits"][commit]["native_arguments"][
            "target_path"
        ] = "Historical/exact.md"
        path.write_text(json.dumps(state))
        self.change_snapshot(self.routes)
        again = self.prepare(path, commit)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(
            json.loads(again.stdout),
            dict(
                original,
                arguments=dict(
                    original["arguments"], target_path="Historical/exact.md"
                ),
            ),
        )

    def test_invalid_policy_fails_before_any_attach_mutation(self):
        cases = [
            "[]",
            "null",
            "",
            '{"business/engine":"A/{date}.md","business/engine":"B/{date}.md"}',
            '{"business/engine":1}',
            '{"business/*":"A/{date}.md"}',
            '{"business//engine":"A/{date}.md"}',
            '{"business/engine":"../{date}.md"}',
            '{"business/engine":"/A/{date}.md"}',
            '{"business/engine":"A//{date}.md"}',
            '{"business/engine":"A/{repo}.md"}',
            '{"business/engine":"A/{date.__class__}.md"}',
            '{"business/engine":"A/{date:10}.md"}',
            '{"business/engine":"C:/{date}.md"}',
            '{"business/engine":" A/{date}.md"}',
            '{"business/engine":"A/./{date}.md"}',
            '{"business/engine":"A/{date}.txt"}',
            '{"business/engine":"A/.md"}',
            '{"business/engine":"A/\\u0000{date}.md"}',
            '{"business/engine":"A/\\\\{date}.md"}',
            '{"business/engine":"A/\\ud800{date}.md"}',
            " " * 65537,
            "{bad json",
            json.dumps({"business/engine": "a" * 1024 + ".md"}),
            json.dumps({"business/repo" + str(i): "A/{date}.md" for i in range(129)}),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:90]):
                self.policy.write_text(raw)
                self.assert_attach_rejected(self.policy)
        self.policy.write_text(json.dumps(self.routes))
        for mode in (0o644, 0o660):
            self.policy.chmod(mode)
            self.assert_attach_rejected(self.policy)
        self.policy.chmod(0o600)
        alias = self.directory / "alias.json"
        alias.symlink_to(self.policy)
        self.assert_attach_rejected(alias)
        alias.unlink()
        os.link(self.policy, alias)
        self.assert_attach_rejected(self.policy)
        alias.unlink()
        self.assert_attach_rejected(self.directory)
        self.assert_attach_rejected(self.directory / "missing.json")
        parent_alias = self.directory / "linked-parent"
        parent_alias.symlink_to(self.directory, target_is_directory=True)
        self.assert_attach_rejected(parent_alias / "routes.json")
        fifo = self.directory / "routes.fifo"
        os.mkfifo(fifo, 0o600)
        self.assert_attach_rejected(fifo)

    def test_route_file_requires_invoking_owner(self):
        consumer = runpy.run_path(str(self.bundle / "bin/obsidian-mcp-capture-commit"))
        with mock.patch.object(consumer["os"], "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(consumer["CaptureError"]):
                consumer["read_commit_routes"](str(self.policy))

    def assert_attach_rejected(self, policy):
        result = self.cli(
            "attach",
            "--scope",
            "user",
            "--host",
            "claude-plugin",
            "--home",
            str(self.home),
            "--folder",
            "Sessions",
            "--release-sha256",
            self.digest,
            "--commit-routes",
            str(policy),
        )
        self.assertEqual(result.returncode, 64, result.stdout)
        self.assertFalse(self.home.exists())

    def test_route_flag_rejected_for_unsupported_operations(self):
        for operation in ("create", "capabilities", "run", "profile-status"):
            result = self.cli(
                operation,
                "--commit-routes",
                str(self.policy),
                "--home",
                str(self.home),
                "--project",
                str(self.project),
                "--folder",
                "Sessions",
                "--release-sha256",
                self.digest,
            )
            self.assertEqual(result.returncode, 64)
            self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
