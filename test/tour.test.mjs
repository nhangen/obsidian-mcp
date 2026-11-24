import assert from "node:assert/strict";
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { mkdir, mkdtemp, readdir, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough, Writable } from "node:stream";
import { test } from "node:test";
import { packageVersion } from "../src/version.mjs";
import {
  configureClaudeCodeViaCli,
  configureClaudeCodeViaFile,
  configureClaudeDesktop,
  countVaultNotes,
  detectClaudeCode,
  detectDailyNotesPath,
  findKnownVaults,
  resolveServerLauncher,
  runTour,
  upsertJsonMcpServerConfig,
} from "../src/tour.mjs";

test("findKnownVaults parses registry and sorts active vaults first", async (t) => {
  const rawFixture = await mkdtemp(join(tmpdir(), "mcp-tour-known-"));
  const fixture = realpathSync(rawFixture);
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const vaultActive = join(fixture, "VaultActive");
  const vaultRecent = join(fixture, "VaultRecent");
  const vaultOld = join(fixture, "VaultOld");
  const vaultMissing = join(fixture, "VaultMissing");

  await mkdir(vaultActive, { recursive: true });
  await mkdir(vaultRecent, { recursive: true });
  await mkdir(vaultOld, { recursive: true });

  const configPath = join(fixture, "obsidian.json");
  const registry = {
    vaults: {
      id1: { path: vaultOld, ts: 1000, open: false },
      id2: { path: vaultActive, ts: 2000, open: true },
      id3: { path: vaultMissing, ts: 5000, open: false },
      id4: { path: vaultRecent, ts: 3000, open: false },
    },
  };
  await writeFile(configPath, JSON.stringify(registry), "utf8");

  const detected = findKnownVaults(configPath);
  assert.equal(detected.length, 3);
  assert.equal(detected[0].path, vaultActive);
  assert.equal(detected[0].open, true);
  assert.equal(detected[1].path, vaultRecent);
  assert.equal(detected[2].path, vaultOld);
});

test("findKnownVaults returns empty array for absent or invalid config", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-invalid-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  assert.deepEqual(findKnownVaults(join(fixture, "absent.json")), []);

  const invalidJson = join(fixture, "invalid.json");
  await writeFile(invalidJson, "{ malformed", "utf8");
  assert.deepEqual(findKnownVaults(invalidJson), []);
});

test("detectDailyNotesPath detects custom folder, candidate folders, and default", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-daily-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  // 1. With daily-notes.json
  const vaultWithConfig = join(fixture, "vault-config");
  await mkdir(join(vaultWithConfig, ".obsidian"), { recursive: true });
  await writeFile(
    join(vaultWithConfig, ".obsidian", "daily-notes.json"),
    JSON.stringify({ folder: "Periodic/DailyNotes" }),
    "utf8",
  );
  assert.equal(detectDailyNotesPath(vaultWithConfig), "Periodic/DailyNotes/");

  // 2. With empty folder in daily-notes.json (falls back to default Daily/ per server directory contract)
  const vaultWithRoot = join(fixture, "vault-root");
  await mkdir(join(vaultWithRoot, ".obsidian"), { recursive: true });
  await writeFile(
    join(vaultWithRoot, ".obsidian", "daily-notes.json"),
    JSON.stringify({ folder: "" }),
    "utf8",
  );
  assert.equal(detectDailyNotesPath(vaultWithRoot), "Daily/");

  // 3. With candidate directory Journal
  const vaultWithJournal = join(fixture, "vault-journal");
  await mkdir(join(vaultWithJournal, "Journal"), { recursive: true });
  assert.equal(detectDailyNotesPath(vaultWithJournal), "Journal/");

  // 4. Fallback default
  const emptyVault = join(fixture, "vault-empty");
  await mkdir(emptyVault, { recursive: true });
  assert.equal(detectDailyNotesPath(emptyVault), "Daily/");
});

test("countVaultNotes recursively counts markdown notes and ignores metadata directories", async (t) => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-tour-count-"));
  t.after(() => rm(vault, { recursive: true, force: true }));

  await mkdir(join(vault, "Notes"), { recursive: true });
  await mkdir(join(vault, "Notes", "Sub"), { recursive: true });
  await mkdir(join(vault, ".obsidian"), { recursive: true });
  await mkdir(join(vault, ".git"), { recursive: true });

  await writeFile(join(vault, "root.md"), "# Root\n");
  await writeFile(join(vault, "Notes", "note1.md"), "# Note 1\n");
  await writeFile(join(vault, "Notes", "Sub", "note2.md"), "# Note 2\n");
  await writeFile(join(vault, "image.png"), "binary data");
  await writeFile(join(vault, ".obsidian", "workspace.json"), "{}");
  await writeFile(join(vault, ".git", "HEAD"), "ref: refs/heads/master");

  const result = await countVaultNotes(vault, 10);
  assert.equal(result.count, 3);
  assert.equal(result.hasMore, false);

  const capped = await countVaultNotes(vault, 2);
  assert.equal(capped.count, 2);
  assert.equal(capped.hasMore, true);
});

test("countVaultNotes throws when vault directory cannot be read", async () => {
  await assert.rejects(
    () => countVaultNotes("/path/does/not/exist/ever/12345"),
    /Cannot read vault directory/,
  );
});

test("upsertJsonMcpServerConfig creates absent file with 0600 mode and no backup", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-absent-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const configPath = join(fixture, "sub", "config.json");
  const serverEntry = { command: "node", args: ["server.js"] };

  const res = await upsertJsonMcpServerConfig(configPath, serverEntry);
  assert.equal(res.success, true);
  assert.equal(res.updated, true);

  const fileStats = await stat(configPath);
  assert.equal(fileStats.mode & 0o777, 0o600);

  const data = JSON.parse(await readFile(configPath, "utf8"));
  assert.deepEqual(data.mcpServers.obsidian, serverEntry);

  // No .bak was created because file didn't exist prior to upsert
  await assert.rejects(() => stat(`${configPath}.bak`));
});

test("upsertJsonMcpServerConfig preserves original .bak across multiple runs and keeps 0600 mode", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-repeat-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const configPath = join(fixture, "config.json");
  const original = { mcpServers: { initial: { command: "echo" } } };
  await writeFile(configPath, JSON.stringify(original), { mode: 0o600 });

  const entry1 = { command: "node", args: ["v1.js"] };
  await upsertJsonMcpServerConfig(configPath, entry1, { overwrite: true });

  const backup1 = JSON.parse(await readFile(`${configPath}.bak`, "utf8"));
  assert.deepEqual(backup1, original);

  // Second run with different entry
  const entry2 = { command: "node", args: ["v2.js"] };
  await upsertJsonMcpServerConfig(configPath, entry2, { overwrite: true });

  // Backup must still be the original pre-tour state, NOT entry1
  const backup2 = JSON.parse(await readFile(`${configPath}.bak`, "utf8"));
  assert.deepEqual(backup2, original);

  // Updated file has entry2
  const updated = JSON.parse(await readFile(configPath, "utf8"));
  assert.deepEqual(updated.mcpServers.obsidian, entry2);
  assert.deepEqual(updated.mcpServers.initial, original.mcpServers.initial);

  // File mode must still be 0600
  const fileStats = await stat(configPath);
  assert.equal(fileStats.mode & 0o777, 0o600);
});

test("upsertJsonMcpServerConfig rejects malformed JSON without modifying or wiping original file", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-malformed-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const configPath = join(fixture, "malformed.json");
  const corruptContent = "{\n  \"mcpServers\": { unclosed\n";
  await writeFile(configPath, corruptContent, "utf8");

  await assert.rejects(
    () => upsertJsonMcpServerConfig(configPath, { command: "node" }),
    /Configuration file contains invalid JSON.*malformed\.json/,
  );

  // File content is preserved exactly
  assert.equal(await readFile(configPath, "utf8"), corruptContent);
  // No backup was written
  await assert.rejects(() => stat(`${configPath}.bak`));
});

test("configureClaudeDesktop safely updates config using executable launcher", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-desktop-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const configPath = join(fixture, "claude_desktop_config.json");
  const existingConfig = {
    mcpServers: {
      other: { command: "node", args: ["other.js"] },
    },
    preferences: { theme: "dark" },
  };
  await writeFile(configPath, JSON.stringify(existingConfig, null, 2), "utf8");

  const vaultPath = join(fixture, "MyVault");
  await mkdir(vaultPath, { recursive: true });

  const res = await configureClaudeDesktop(vaultPath, "Journal/", configPath);
  assert.equal(res.success, true);

  const updated = JSON.parse(await readFile(configPath, "utf8"));
  assert.equal(updated.preferences.theme, "dark");
  assert.deepEqual(updated.mcpServers.other, existingConfig.mcpServers.other);
  assert.equal(updated.mcpServers.obsidian.command, process.execPath);
  assert.ok(updated.mcpServers.obsidian.args.includes("--vault"));
  assert.ok(updated.mcpServers.obsidian.args.includes(vaultPath));
  assert.ok(updated.mcpServers.obsidian.args.includes("--daily-path"));
  assert.ok(updated.mcpServers.obsidian.args.includes("Journal/"));
});

test("configureClaudeCodeViaFile safely updates config using executable launcher", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-claude-json-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const configPath = join(fixture, ".claude.json");
  const existing = { userSettings: { autoUpdate: true } };
  await writeFile(configPath, JSON.stringify(existing), "utf8");

  const vaultPath = join(fixture, "CodeVault");
  await mkdir(vaultPath, { recursive: true });

  const res = await configureClaudeCodeViaFile(vaultPath, "Daily/", configPath);
  assert.equal(res.success, true);
  assert.equal(res.method, "file");

  const updated = JSON.parse(await readFile(configPath, "utf8"));
  assert.equal(updated.userSettings.autoUpdate, true);
  assert.equal(updated.mcpServers.obsidian.command, process.execPath);
  assert.ok(updated.mcpServers.obsidian.args.includes("--vault"));
  assert.ok(updated.mcpServers.obsidian.args.includes(vaultPath));
});

test("runTour completes interactive walkthrough with isolated config overrides", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-e2e-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const vaultPath = join(fixture, "E2EVault");
  await mkdir(join(vaultPath, "Daily"), { recursive: true });
  await writeFile(join(vaultPath, "note.md"), "# Hello\n");

  const claudeCodeConfig = join(fixture, "isolated-claude.json");
  const claudeDesktopConfig = join(fixture, "isolated-desktop.json");

  const knownVaults = [
    { id: "1", path: vaultPath, open: true, ts: Date.now() },
  ];

  const inputStream = new PassThrough();
  let outputText = "";
  const outputStream = new Writable({
    write(chunk, encoding, callback) {
      const text = chunk.toString();
      outputText += text;
      if (text.includes("Select a vault")) {
        setImmediate(() => inputStream.write("\n"));
      } else if (text.includes("Daily notes directory")) {
        setImmediate(() => inputStream.write("\n"));
      } else if (text.includes("Select an option [1-4]")) {
        setImmediate(() => inputStream.write("3\n"));
      }
      callback();
    },
  });

  const outcome = await runTour({
    input: inputStream,
    output: outputStream,
    knownVaults,
    claudeCodeConfigPath: claudeCodeConfig,
    claudeDesktopConfigPath: claudeDesktopConfig,
    forceFileConfig: true,
    detectClaudeCode: () => false,
    version: "0.2.3",
  });

  assert.equal(outcome.vault, vaultPath);
  assert.equal(outcome.dailyPath, "Daily/");
  assert.equal(outcome.hostChoice, 3);
  assert.equal(outcome.results.length, 2);
  assert.equal(outcome.results[0].success, true);
  assert.equal(outcome.results[1].success, true);

  assert.match(outputText, /Obsidian MCP Server Setup/);
  assert.match(outputText, /Found Obsidian vault\(s\) on this machine/);
  assert.match(outputText, /Found 1 Markdown note\(s\) in vault/);
  assert.match(outputText, /Setup complete!/);

  // Both isolated config files were created
  for (const configPath of [claudeCodeConfig, claudeDesktopConfig]) {
    const { args } = JSON.parse(await readFile(configPath, "utf8")).mcpServers.obsidian;
    assert.deepEqual(args.slice(-4), ["--vault", vaultPath, "--daily-path", "Daily/"]);
  }
});

test("detectClaudeCode skips the CLI on Windows so args never pass through cmd.exe", () => {
  let spawned = false;
  const spawnStub = () => {
    spawned = true;
    return { status: 0 };
  };
  assert.equal(detectClaudeCode({ platform: "win32", spawn: spawnStub }), false);
  assert.equal(spawned, false);
});

test("detectClaudeCode spawns claude without a shell on POSIX", () => {
  let seen;
  const spawnStub = (cmd, args, opts) => {
    seen = { cmd, args, opts };
    return { status: 0 };
  };
  assert.equal(detectClaudeCode({ platform: "darwin", spawn: spawnStub }), true);
  assert.equal(seen.cmd, "claude");
  assert.notEqual(seen.opts.shell, true);
});

test("configureClaudeCodeViaCli treats only a missing binary as a spawn failure", async () => {
  const missing = await configureClaudeCodeViaCli("/vault", "Daily/", {
    spawn: () => ({ status: null, error: Object.assign(new Error("spawnSync claude ENOENT"), { code: "ENOENT" }) }),
  });
  assert.equal(missing.success, false);
  assert.equal(missing.spawnFailure, true);

  const timedOut = await configureClaudeCodeViaCli("/vault", "Daily/", {
    spawn: () => ({ status: null, error: Object.assign(new Error("spawnSync claude ETIMEDOUT"), { code: "ETIMEDOUT" }) }),
  });
  assert.equal(timedOut.success, false);
  assert.equal(timedOut.spawnFailure, false);
  assert.match(timedOut.error, /ETIMEDOUT/);
});

test("resolveServerLauncher runs an installed copy directly, including under .npm-global", async (t) => {
  const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-launcher-")));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const pkg = join(fixture, ".npm-global", "lib", "node_modules", "@nhangen", "obsidian-mcp");
  await mkdir(join(pkg, "dist"), { recursive: true });
  await writeFile(join(pkg, "dist", "stdio.mjs"), "");

  const launcher = resolveServerLauncher(join(pkg, "dist", "stdio.mjs"));
  assert.equal(launcher.command, process.execPath);
  assert.deepEqual(launcher.argsPrefix, [join(pkg, "dist", "stdio.mjs")]);
});

test("resolveServerLauncher pins the running version for npx caches and the fallback", () => {
  const pinned = ["-y", `@nhangen/obsidian-mcp@${packageVersion}`];
  const npxCache = resolveServerLauncher("/home/u/.npm/_npx/abc123/node_modules/@nhangen/obsidian-mcp/dist/stdio.mjs");
  assert.equal(npxCache.command, "npx");
  assert.deepEqual(npxCache.argsPrefix, pinned);

  const nowhere = resolveServerLauncher("/nonexistent/pkg/dist/stdio.mjs");
  assert.equal(nowhere.command, "npx");
  assert.deepEqual(nowhere.argsPrefix, pinned);
});

test("upsertJsonMcpServerConfig backs up a hand-tuned obsidian entry before replacing it", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-replace-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const configPath = join(fixture, "config.json");
  const tuned = { command: "node", args: ["/old/stdio.mjs"], env: { TOKEN: "keep-me" } };
  await writeFile(configPath, JSON.stringify({ mcpServers: { obsidian: tuned } }), { mode: 0o600 });
  await writeFile(`${configPath}.bak`, "{}", { mode: 0o600 });

  const entry = { command: "node", args: ["/new/stdio.mjs"] };
  const res = await upsertJsonMcpServerConfig(configPath, entry, { overwrite: true });
  assert.equal(res.existed, true);
  assert.equal(res.updated, true);
  assert.ok(res.replacedBackupPath, "a replaced entry needs its own backup");
  const saved = JSON.parse(await readFile(res.replacedBackupPath, "utf8"));
  assert.deepEqual(saved.mcpServers.obsidian, tuned);
  assert.equal((await stat(res.replacedBackupPath)).mode & 0o777, 0o600);
  assert.equal(await readFile(`${configPath}.bak`, "utf8"), "{}");

  const again = await upsertJsonMcpServerConfig(configPath, entry, { overwrite: true });
  assert.equal(again.existed, true);
  assert.equal(again.updated, false);
  const backups = (await readdir(fixture)).filter((name) => name.includes(".replaced-"));
  assert.equal(backups.length, 1, "an unchanged entry is not rewritten or re-backed-up");
});

test("runTour rejects when input closes before the tour finishes", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-eof-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const vaultPath = join(fixture, "Vault");
  await mkdir(vaultPath, { recursive: true });
  const claudeCodeConfigPath = join(fixture, "claude.json");

  const input = new PassThrough();
  const output = new Writable({
    write(chunk, encoding, callback) {
      const text = chunk.toString();
      if (text.includes("Select a vault")) setImmediate(() => input.write("\n"));
      if (text.includes("Daily notes directory")) setImmediate(() => input.end());
      callback();
    },
  });

  await assert.rejects(
    runTour({ input, output, knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }], claudeCodeConfigPath, detectClaudeCode: () => false }),
    /cancelled/,
  );
  assert.equal(existsSync(claudeCodeConfigPath), false);
});

function scriptedTour(answers) {
  const input = new PassThrough();
  let text = "";
  const output = new Writable({
    write(chunk, encoding, callback) {
      const piece = chunk.toString();
      text += piece;
      for (const [prompt, reply] of answers) {
        if (piece.includes(prompt)) {
          const next = Array.isArray(reply) ? reply.shift() : reply;
          if (next !== undefined) setImmediate(() => input.write(`${next}\n`));
          break;
        }
      }
      callback();
    },
  });
  return { input, output, text: () => text };
}

test("runTour reports a partial host failure as incomplete", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-partial-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const vaultPath = join(fixture, "Vault");
  await mkdir(vaultPath, { recursive: true });
  const claudeCodeConfigPath = join(fixture, "claude.json");
  const claudeDesktopConfigPath = join(fixture, "desktop.json");
  await writeFile(claudeCodeConfigPath, "{ truncated", "utf8");

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["Select an option", "3"]]);
  await assert.rejects(
    runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
      claudeCodeConfigPath,
      claudeDesktopConfigPath,
      detectClaudeCode: () => false,
    }),
    (err) => err.results?.length === 2 && err.results.some((r) => r.success) && err.results.some((r) => !r.success),
  );
  assert.doesNotMatch(io.text(), /Setup complete!/);
  assert.match(io.text(), /Setup incomplete: 1 of 2/);
  assert.ok(JSON.parse(await readFile(claudeDesktopConfigPath, "utf8")).mcpServers.obsidian);
  assert.equal(await readFile(claudeCodeConfigPath, "utf8"), "{ truncated");
});

test("runTour does not write the config file when claude mcp add rejects", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-cli-reject-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const vaultPath = join(fixture, "Vault");
  await mkdir(vaultPath, { recursive: true });
  const claudeCodeConfigPath = join(fixture, "claude.json");

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["Select an option", "1"]]);
  await assert.rejects(
    runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
      claudeCodeConfigPath,
      detectClaudeCode: () => true,
      configureClaudeCodeViaCli: async () => ({ success: false, method: "cli", error: "server obsidian already exists", spawnFailure: false }),
    }),
    /1 of 1/,
  );
  assert.equal(existsSync(claudeCodeConfigPath), false);
  assert.match(io.text(), /already exists/);
});

test("runTour falls back to the config file when the claude CLI cannot be spawned", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-cli-missing-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const vaultPath = join(fixture, "Vault");
  await mkdir(vaultPath, { recursive: true });
  const claudeCodeConfigPath = join(fixture, "claude.json");

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["Select an option", "1"]]);
  const outcome = await runTour({
    input: io.input,
    output: io.output,
    knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
    claudeCodeConfigPath,
    detectClaudeCode: () => true,
    configureClaudeCodeViaCli: async () => ({ success: false, method: "cli", error: "ENOENT", spawnFailure: true }),
  });
  assert.deepEqual(outcome.results, [{ host: "Claude Code", success: true, method: "file" }]);
  assert.ok(JSON.parse(await readFile(claudeCodeConfigPath, "utf8")).mcpServers.obsidian);
});

test("runTour re-prompts until the daily notes path stays inside the vault", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-daily-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const vaultPath = join(fixture, "Vault");
  await mkdir(vaultPath, { recursive: true });

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ["/Journal", "../outside", "Journal"]], ["Select an option", "4"]]);
  const outcome = await runTour({
    input: io.input,
    output: io.output,
    knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
    detectClaudeCode: () => false,
  });
  assert.equal(outcome.dailyPath, "Journal/");
  assert.equal(io.text().match(/Invalid daily notes path/g).length, 2);
});

test("detectDailyNotesPath ignores a daily-notes folder that escapes the vault", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-daily-escape-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  await mkdir(join(fixture, ".obsidian"), { recursive: true });
  await writeFile(join(fixture, ".obsidian", "daily-notes.json"), JSON.stringify({ folder: "../outside" }));
  assert.equal(detectDailyNotesPath(fixture), "Daily/");
});

test("upsertJsonMcpServerConfig rejects non-object configs and mcpServers without touching the file", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-shape-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const cases = [
    ["null", /must contain a JSON object/],
    ["[]", /must contain a JSON object/],
    [JSON.stringify({ mcpServers: "x" }), /mcpServers in .* must be an object/],
    [JSON.stringify({ mcpServers: null }), /mcpServers in .* must be an object/],
  ];
  for (const [index, [content, expected]] of cases.entries()) {
    const configPath = join(fixture, `config-${index}.json`);
    await writeFile(configPath, content, "utf8");
    await assert.rejects(upsertJsonMcpServerConfig(configPath, { command: "node", args: [] }, { overwrite: true }), expected);
    assert.equal(await readFile(configPath, "utf8"), content);
  }
});

test("runTour uses a preselected vault without prompting for one", async (t) => {
  const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-initial-")));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const vaultPath = join(fixture, "Vault");
  await mkdir(vaultPath, { recursive: true });

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["Select an option", "4"]]);
  const outcome = await runTour({
    input: io.input,
    output: io.output,
    initialVault: vaultPath,
    knownVaults: [{ id: "1", path: join(fixture, "Other"), open: true, ts: 0 }],
    detectClaudeCode: () => false,
  });
  assert.equal(outcome.vault, vaultPath);
  assert.doesNotMatch(io.text(), /Select a vault/);
});

test("runTour expands ~ and rejects relative custom vault paths", async (t) => {
  const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-custom-")));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  await mkdir(join(fixture, "Vault"), { recursive: true });

  const io = scriptedTour([["Enter the absolute path", ["Vault", "~/Vault"]], ["Daily notes directory", ""], ["Select an option", "4"]]);
  const outcome = await runTour({
    input: io.input,
    output: io.output,
    knownVaults: [],
    homeDir: fixture,
    detectClaudeCode: () => false,
  });
  assert.equal(outcome.vault, join(fixture, "Vault"));
  assert.match(io.text(), /must be absolute/);
});

test("runTour resolves a symlinked preselected vault to its real path", async (t) => {
  const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-initial-link-")));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  await mkdir(join(fixture, "Real"), { recursive: true });
  await symlink(join(fixture, "Real"), join(fixture, "Link"));

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["Select an option", "4"]]);
  const outcome = await runTour({
    input: io.input,
    output: io.output,
    initialVault: join(fixture, "Link"),
    knownVaults: [],
    detectClaudeCode: () => false,
  });
  assert.equal(outcome.vault, join(fixture, "Real"));
});

test("upsertJsonMcpServerConfig writes through a symlinked config instead of replacing the link", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-symlink-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const target = join(fixture, "dotfiles-claude.json");
  const link = join(fixture, ".claude.json");
  await writeFile(target, JSON.stringify({ keep: true }), { mode: 0o600 });
  await symlink(target, link);

  await upsertJsonMcpServerConfig(link, { command: "node", args: ["/x"] }, { overwrite: true });
  assert.equal(lstatSync(link).isSymbolicLink(), true);
  const written = JSON.parse(await readFile(target, "utf8"));
  assert.equal(written.keep, true);
  assert.deepEqual(written.mcpServers.obsidian, { command: "node", args: ["/x"] });
});
