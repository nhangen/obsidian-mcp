import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough, Writable } from "node:stream";
import { test } from "node:test";
import {
  configureClaudeCodeViaCli,
  configureClaudeCodeViaFile,
  configureClaudeDesktop,
  countVaultNotes,
  detectClaudeCode,
  detectDailyNotesPath,
  findKnownVaults,
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
    version: "0.2.3",
  });

  assert.equal(outcome.vault, vaultPath);
  assert.equal(outcome.dailyPath, "Daily/");
  assert.equal(outcome.configuredHosts, 3);
  assert.equal(outcome.results.length, 2);
  assert.equal(outcome.results[0].success, true);
  assert.equal(outcome.results[1].success, true);

  assert.match(outputText, /Obsidian MCP Server Setup/);
  assert.match(outputText, /Found Obsidian vault\(s\) on this machine/);
  assert.match(outputText, /Found 1 Markdown note\(s\) in vault/);
  assert.match(outputText, /Setup complete!/);

  // Both isolated config files were created
  const codeCfg = JSON.parse(await readFile(claudeCodeConfig, "utf8"));
  assert.ok(codeCfg.mcpServers.obsidian);
  const desktopCfg = JSON.parse(await readFile(claudeDesktopConfig, "utf8"));
  assert.ok(desktopCfg.mcpServers.obsidian);
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
