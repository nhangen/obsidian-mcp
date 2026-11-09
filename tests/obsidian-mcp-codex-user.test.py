#!/usr/bin/env python3
import json
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import unittest


fixture = runpy.run_path(str(Path(__file__).with_name("obsidian-mcp-native.test.py")))
NativeCaptureFixture = fixture["NativeCaptureFixture"]


class CodexUserCaptureTest(NativeCaptureFixture):
    test_pending_capture_allows_next_commit_without_promoting_old_evidence = (
        fixture["NativeCaptureTest"].test_pending_capture_allows_next_commit_without_promoting_old_evidence
    )
    test_capture_proof_failure_preserves_git_exit_and_never_executes_twice = (
        fixture["NativeCaptureTest"].test_capture_proof_failure_preserves_git_exit_and_never_executes_twice
    )
    test_post_git_persistence_failure_preserves_exit_and_refuses_reexecution = (
        fixture["NativeCaptureTest"].test_post_git_persistence_failure_preserves_exit_and_refuses_reexecution
    )

    def event(self, name, **extra):
        return {**super().event(name), **extra}

    def setUp(self):
        super().setUp()
        runtime = self.directory / "runtime-home"
        runtime.mkdir()
        self.environment.update(HOME=str(runtime), PYTHONDONTWRITEBYTECODE="1")
        for kind in ("CONFIG", "DATA", "STATE", "CACHE"):
            self.environment["XDG_" + kind + "_HOME"] = str(runtime / kind.lower())

    def configure(self, *extra):
        result = self.cli(
            "attach",
            "--host",
            "codex",
            "--scope",
            "user",
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

    def start(self, **extra):
        self.configure()
        self.hook("UserPromptSubmit", prompt="Meaningful Codex work", **extra)
        paths = self.state_paths()
        self.assertEqual(len(paths), 1)
        return paths[0]

    def session_arguments(self, path):
        plan = json.loads(path.read_bytes())["session_save"]
        return dict(
            title=plan["title"],
            folder_hint=plan["folder"],
            body="Fixture facts",
            resolved=True,
            session_link_date=plan["date"],
            idempotency_key=plan["idempotency_key"],
        )

    def receipt(self, path):
        plan = json.loads(path.read_bytes())["session_save"]
        return dict(
            status="committed",
            request_id="fixture",
            idempotency_key=plan["idempotency_key"],
            path=plan["path"],
            affected_paths=[plan["path"], plan["index"], plan["daily"]],
            warnings=[],
            recovery={"required": False, "action": ""},
            error_code=None,
            retryable=False,
        )

    def save(self, path, **extra):
        event = dict(
            tool_name="mcp__obsidian_write__obsidian_keeper_save",
            tool_use_id="session-write",
            tool_input=self.session_arguments(path),
            **extra,
        )
        self.assertEqual(self.hook("PreToolUse", **event), {})
        self.hook(
            "PostToolUse",
            **event,
            tool_response={"structuredContent": self.receipt(path)},
        )
        self.assertEqual(json.loads(path.read_bytes())["disposition"], "saved")

    def drain(self):
        result = self.cli("drain-status", "--home", str(self.home))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def crash_publication(self, phase, event):
        program = """
import os, json, runpy, sys
from pathlib import Path
script, home, phase = sys.argv[1:]
replace = os.replace
def interrupted(source, target):
    target = Path(target)
    wanted = (
        (phase == 'descriptor' and target.name == 'capture-profile.json' and target.parent.parent.name == 'provisioning')
        or (phase == 'state' and target.parent.name == 'capture-state' and target.suffix == '.json')
        or (phase in ('binding', 'enrolled') and target.parent.name == 'bindings'
            and json.loads(Path(source).read_bytes())['enrolled'] == (phase == 'enrolled'))
        or (phase == 'continuation' and target.parent.name == 'bindings'
            and 'continuationCapture' in json.loads(Path(source).read_bytes()))
    )
    if wanted:
        os._exit(73)
    return replace(source, target)
os.replace = interrupted
sys.argv = [script, 'hook', '--home', home]
runpy.run_path(script, run_name='__main__')
"""
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                program,
                str(self.bundle / "bin/obsidian-mcp-native"),
                str(self.home),
                phase,
            ],
            input=json.dumps(event),
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 73, result.stderr + result.stdout)

    def test_native_turns_duplicates_and_matching_receipts(self):
        for turn in ("turn-1", "bcefc5b2-19b6-4436-a477-14141b178ddb"):
            with self.subTest(turn=turn):
                self.home = self.directory / turn
                path = self.start(turn_id=turn)
                original = path.read_bytes()
                self.assertEqual(json.loads(original)["turn"], turn)
                self.assertNotIn("claude_transcript", json.loads(original))
                self.hook(
                    "UserPromptSubmit", turn_id=turn, prompt="Meaningful Codex work"
                )
                self.assertEqual(path.read_bytes(), original)
                self.hook("SessionStart", turn_id=None)
                self.assertEqual(path.read_bytes(), original)
                self.save(path, turn_id=turn)
                saved = path.read_bytes()
                self.hook(
                    "UserPromptSubmit", turn_id=turn, prompt="Meaningful Codex work"
                )
                self.assertEqual(path.read_bytes(), saved)
                self.assertTrue(self.drain()["quiescent"])
                self.hook("UserPromptSubmit", turn_id="turn-next", prompt="Next work")
                self.assertEqual(json.loads(path.read_bytes())["turn"], "turn-next")
                self.assertEqual(
                    json.loads(path.read_bytes())["disposition"], "pending"
                )

    def test_pending_new_prompt_retains_payload_and_allows_original_receipt(self):
        path = self.start()
        event = dict(
            tool_name="mcp__obsidian_write__obsidian_keeper_save",
            tool_use_id="inflight",
            tool_input=self.session_arguments(path),
        )
        self.hook("PreToolUse", **event)
        original = path.read_bytes()
        output = self.hook("UserPromptSubmit", turn_id="turn-2", prompt="Next work")
        self.assertNotIn("decision", output)
        self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", json.dumps(output))
        self.assertEqual(path.read_bytes(), original)
        self.hook(
            "PostToolUse",
            **event,
            tool_response={"structuredContent": self.receipt(path)},
        )
        self.assertEqual(json.loads(path.read_bytes())["disposition"], "saved")
        self.assertTrue(self.drain()["quiescent"])

    def test_alias_call_payload_and_receipt_mismatches_never_acknowledge_success(self):
        path = self.start()
        arguments = self.session_arguments(path)
        event = dict(
            tool_name="mcp__obsidian_write__obsidian_keeper_save",
            tool_use_id="write",
            tool_input=arguments,
        )
        for tool in (
            "mcp__obsidian-write__obsidian_keeper_save",
            "obsidian_keeper_save",
        ):
            before = path.read_bytes()
            self.hook(
                "PostToolUse",
                **dict(event, tool_name=tool),
                tool_response={"structuredContent": self.receipt(path)},
            )
            self.assertEqual(path.read_bytes(), before)
        self.hook("PreToolUse", **event)
        before = path.read_bytes()
        self.hook(
            "PostToolUse",
            **dict(event, tool_use_id="wrong-call"),
            tool_response={"structuredContent": self.receipt(path)},
        )
        self.assertEqual(path.read_bytes(), before)
        self.hook(
            "PostToolUse",
            **dict(event, tool_input=dict(arguments, body="Changed")),
            tool_response={"structuredContent": self.receipt(path)},
        )
        self.assertEqual(json.loads(path.read_bytes())["disposition"], "pending")
        for receipt in (
            {"structuredContent": dict(self.receipt(path), path="Other.md")},
            self.receipt(path),
            [{"type": "text", "text": json.dumps(self.receipt(path))}],
            {"structuredContent": self.receipt(path), "isError": True},
        ):
            self.hook("PostToolUse", **event, tool_response=receipt)
            state = json.loads(path.read_bytes())
            self.assertEqual(state["disposition"], "pending")
            self.assertTrue(state["session_recovery"])
        self.assertFalse(self.drain()["quiescent"])

    def test_workspace_binding_git_subdirectories_and_directory_session_only(self):
        nested = self.project / "nested"
        nested.mkdir()
        alias = self.directory / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        path = self.start(cwd=str(alias))
        first = path.read_bytes()
        self.hook(
            "UserPromptSubmit", cwd=str(self.project), prompt="Meaningful Codex work"
        )
        self.assertEqual(path.read_bytes(), first)
        other = self.directory / "other"
        other.mkdir()
        subprocess.run(
            ["git", "-C", str(other), "init"],
            env=self.environment,
            capture_output=True,
            check=True,
        )
        refused = self.hook(
            "UserPromptSubmit", cwd=str(other), turn_id="turn-2", prompt="Move"
        )
        self.assertIn("SESSION_WORKSPACE_CHANGED", json.dumps(refused))
        self.assertEqual(path.read_bytes(), first)
        self.hook(
            "UserPromptSubmit",
            cwd=str(other),
            session_id="independent",
            prompt="Other work",
        )
        plain = self.directory / "plain"
        plain.mkdir()
        output = self.hook(
            "UserPromptSubmit",
            cwd=str(plain),
            session_id="plain",
            prompt="Session work",
        )
        self.assertIn("session-only", json.dumps(output))
        profiles = [
            json.loads(p.read_bytes())
            for p in (self.home / "workspaces").glob("*/capture-profile.json")
        ]
        self.assertEqual(
            {p["project"] for p in profiles},
            {str(self.project), str(other), str(plain)},
        )
        self.assertEqual(len(profiles), 3)
        plain_path = next(
            (self.home / "workspaces").glob(
                "*/capture-state/" + self.session_key("plain") + ".json"
            )
        )
        self.save(plain_path, session_id="plain", cwd=str(plain))
        before = plain_path.read_bytes()
        denied = self.hook(
            "PreToolUse",
            cwd=str(plain),
            session_id="plain",
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_input={"skip_if_hash": "f" * 40},
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        rejected = self.cli(
            "prepare",
            "--home",
            str(self.home),
            "--session",
            plain_path.stem,
            "--commit",
            "f" * 64,
            payload={"context": ["No Git"]},
        )
        self.assertIn("DIRECTORY_SESSION_ONLY", rejected.stderr)
        self.assertEqual(plain_path.read_bytes(), before)

    def test_interrupted_enrollment_reconciles_native_turn_without_transcript(self):
        for phase in ("descriptor", "binding", "state", "enrolled"):
            with self.subTest(phase=phase):
                self.home = self.directory / phase
                self.configure()
                event = self.event(
                    "UserPromptSubmit",
                    prompt="Recover original",
                    turn_id="native-turn-uuid",
                )
                self.crash_publication(phase, event)
                self.assertTrue(list(self.home.rglob(".capture-*")))
                self.assertFalse(self.drain()["complete"])
                self.hook(
                    "UserPromptSubmit",
                    prompt="Recover original",
                    turn_id="native-turn-uuid",
                )
                (path,) = self.state_paths()
                self.assertEqual(
                    json.loads(path.read_bytes())["turn"], "native-turn-uuid"
                )
                self.assertEqual(list(self.home.rglob(".capture-*")), [])
                self.save(path, turn_id="native-turn-uuid")
                self.assertTrue(self.drain()["quiescent"])

    def continuation_fixture(self):
        path = self.start()
        stopped = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(stopped["decision"], "block")
        frozen = path.read_bytes()
        self.hook("UserPromptSubmit", turn_id="turn-new", prompt="New work")
        self.assertEqual(path.read_bytes(), frozen)
        event = self.event(
            "UserPromptSubmit",
            turn_id="continuation-turn",
            prompt="OBSIDIAN_MCP_CONTINUE:turn-1\nComplete original capture",
        )
        return path, frozen, event

    def test_interrupted_continuation_retains_original_native_identity(self):
        path, frozen, event = self.continuation_fixture()
        self.crash_publication("continuation", event)
        self.hook("SessionStart", turn_id=None)
        self.assertEqual(path.read_bytes(), frozen)
        self.assertEqual(list(self.home.rglob(".capture-*")), [])
        binding = json.loads(
            (self.home / "bindings" / (path.stem + ".json")).read_bytes()
        )
        self.assertFalse(binding["paused"])
        self.assertRegex(binding["continuationCapture"], "^[0-9a-f]{64}$")
        self.save(path, turn_id="continuation-turn")
        self.assertTrue(self.drain()["quiescent"])

    def test_tampered_continuation_state_or_binding_keeps_evidence_and_refuses_recovery(
        self,
    ):
        for damage in ("turn", "transcript", "plan", "session", "python", "binding"):
            with self.subTest(damage=damage):
                self.home = self.directory / damage
                path, _, event = self.continuation_fixture()
                self.crash_publication("continuation", event)
                (candidate,) = self.home.rglob(".capture-*")
                state = json.loads(path.read_bytes())
                if damage == "binding":
                    value = json.loads(candidate.read_bytes())
                    value["project"] = str(self.directory / "other")
                    candidate.write_text(json.dumps(value))
                else:
                    if damage == "plan":
                        state["session_save"]["folder"] = "Other"
                    elif damage == "transcript":
                        state["claude_transcript"] = "f" * 64
                    else:
                        state[damage] = "tampered"
                    path.write_text(json.dumps(state))
                frozen = path.read_bytes()
                binding = self.home / "bindings" / (path.stem + ".json")
                before_binding = binding.read_bytes()
                output = self.hook("SessionStart", turn_id=None)
                self.assertNotIn("obsidian_keeper_save", json.dumps(output))
                self.assertEqual(path.read_bytes(), frozen)
                self.assertEqual(binding.read_bytes(), before_binding)
                self.assertTrue(candidate.exists())

    def test_subdirectory_commit_execution_preserves_prepare_requirement_and_codex_output(
        self,
    ):
        nested = self.project / "nested"
        nested.mkdir()
        path = self.start(cwd=str(nested))
        (nested / "file.txt").write_text("fixture\n")
        self.git("add", "nested/file.txt")
        hooks = self.directory / "empty-hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        event = dict(
            cwd=str(nested),
            tool_name="Bash",
            tool_use_id="commit-call",
            tool_input={"command": "git commit -m fixture -- file.txt"},
        )
        rewritten = self.hook("PreToolUse", **event)
        self.assertEqual(rewritten["hookSpecificOutput"]["permissionDecision"], "allow")
        command = rewritten["hookSpecificOutput"]["updatedInput"]["command"]
        executed = subprocess.run(
            shlex.split(command),
            env=self.environment,
            cwd=nested,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.hook("PostToolUse", **event, tool_response=executed.stdout)
        state = json.loads(path.read_bytes())
        (commit,) = state["commits"]
        self.assertEqual(
            state["commits"][commit]["metadata"]["hash"],
            self.git("rev-parse", "HEAD").stdout.strip(),
        )
        consumer = runpy.run_path(str(self.bundle / "bin/obsidian-mcp-capture-commit"))
        _, arguments = consumer["capture_arguments"](
            state["commits"][commit]["metadata"], ["Fixture fact"]
        )
        frozen = path.read_bytes()
        advisory = self.hook("UserPromptSubmit", turn_id="next-turn", prompt="New work")
        self.assertIn("PREVIOUS_CAPTURE_UNRESOLVED", json.dumps(advisory))
        self.assertEqual(path.read_bytes(), frozen)
        self.hook(
            "PreToolUse",
            tool_name="mcp__obsidian_write__obsidian_daily_append",
            tool_input=arguments,
        )
        self.assertEqual(path.read_bytes(), frozen)
        prepared = self.prepare(path, commit)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        arguments = json.loads(prepared.stdout)["arguments"]
        self.append_event(arguments)
        self.assertEqual(
            json.loads(path.read_bytes())["commits"][commit]["status"], "committed"
        )
        self.save(path)
        self.assertTrue(self.drain()["quiescent"])

    def test_tampered_archived_capture_refuses_restore_and_fresh_enrollment(self):
        path = self.start()
        self.save(path)
        self.hook("UserPromptSubmit", session_id="other-session", prompt="Other work")
        directory = self.home / "archive" / path.stem
        index = json.loads((directory / "index.json").read_bytes())
        archive = directory / (index["generation"] + ".json")
        archived = json.loads(archive.read_bytes())
        self.assertEqual(archived["state"]["turn"], "turn-1")
        archived["state"]["turn"] = "tampered"
        archive.write_text(json.dumps(archived))
        frozen = archive.read_bytes()
        output = self.hook("UserPromptSubmit", turn_id="new-turn", prompt="Resume")
        self.assertIn("RETENTION_ARCHIVE_INVALID", json.dumps(output))
        self.assertEqual(archive.read_bytes(), frozen)
        self.assertFalse(path.exists())
        self.assertEqual(
            self.cli("drain-status", "--home", str(self.home)).returncode, 64
        )

    def test_codex_project_attach_and_isolated_user_or_directory_create_remain_unsupported(
        self,
    ):
        for operation, options in (
            ("attach", ["--scope", "project", "--project", str(self.project)]),
            ("create", ["--scope", "user"]),
            ("create", ["--project-mode", "directory", "--project", str(self.project)]),
        ):
            with self.subTest(operation=operation, options=options):
                result = self.cli(
                    operation,
                    "--host",
                    "codex",
                    "--home",
                    str(self.home),
                    "--folder",
                    "Sessions",
                    "--release-sha256",
                    self.digest,
                    *options,
                )
                self.assertEqual(result.returncode, 64)
                self.assertFalse(self.home.exists())

    def test_attached_discriminator_and_child_ownership_cannot_be_reinterpreted_as_isolated(
        self,
    ):
        path = self.start()
        descriptor = self.home / "capture-profile.json"
        before = descriptor.read_bytes()
        for changes in (
            {"profileMode": "isolated"},
            {"files": {"AGENTS.md": "f" * 64}},
            {"config": {}},
            {"scope": "project"},
        ):
            value = dict(json.loads(before), **changes)
            descriptor.write_text(json.dumps(value))
            result = self.cli("profile-status", "--home", str(self.home))
            self.assertEqual(result.returncode, 64)
        descriptor.write_bytes(before)
        child = path.parent.parent
        for operation in ("run", "disable"):
            result = self.cli(operation, "--home", str(child))
            self.assertEqual(result.returncode, 64)
            self.assertIn(
                "ATTACHED_PROFILE_" + operation.upper() + "_UNSUPPORTED", result.stderr
            )

    def test_attached_user_capabilities_and_lifecycle_leave_host_files_unchanged(self):
        ordinary = self.directory / "ordinary-codex"
        ordinary.mkdir()
        (ordinary / "config.toml").write_text('model = "fixture"\n')
        (ordinary / "hooks.json").write_text('{"hooks":{}}\n')
        (ordinary / "AGENTS.md").write_text("Ordinary instructions\n")
        (self.project / "AGENTS.md").write_text("Workspace instructions\n")
        self.environment["CODEX_HOME"] = str(ordinary)
        before = {
            p: p.read_bytes() for p in (*ordinary.iterdir(), self.project / "AGENTS.md")
        }
        capabilities = self.cli("capabilities", "--release-sha256", self.digest)
        self.assertEqual(capabilities.returncode, 0, capabilities.stderr)
        result = json.loads(capabilities.stdout)
        self.assertEqual(result["captureScopes"]["codex"], ["project", "user"])
        self.assertEqual(result["projectModes"]["codex"], ["git"])
        self.assertEqual(result["attachedCaptureScopes"]["codex"], ["user"])
        self.assertEqual(result["userWorkspaceModes"]["codex"], ["git", "directory"])
        self.assertEqual(result["promptIdentities"]["codex"], "native-turn-id")
        self.configure()
        descriptor = json.loads((self.home / "capture-profile.json").read_bytes())
        self.assertEqual(descriptor["profileMode"], "attached")
        self.assertEqual(descriptor["files"], {})
        self.assertEqual(descriptor["host"], "codex")
        self.assertNotIn("config", descriptor)
        self.assertNotIn("project", descriptor)
        status = self.cli("profile-status", "--home", str(self.home))
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["profileMode"], "attached")
        for operation, error in (
            ("run", "ATTACHED_PROFILE_RUN_UNSUPPORTED"),
            ("disable", "ATTACHED_PROFILE_DISABLE_UNSUPPORTED"),
        ):
            refused = self.cli(operation, "--home", str(self.home))
            self.assertEqual(refused.returncode, 64, refused.stderr)
            self.assertIn(error, refused.stderr)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(
            set(p.name for p in self.home.iterdir()),
            {"capture-profile.json", "capture-state", "workspaces", "bindings"},
        )
        self.hook("UserPromptSubmit", prompt="Capture with existing host configuration")
        (path,) = self.state_paths()
        self.save(path)
        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
