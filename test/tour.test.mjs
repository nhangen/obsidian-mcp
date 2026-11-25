import assert from "node:assert/strict";
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { mkdir, mkdtemp, readdir, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough, Writable } from "node:stream";
import { test } from "node:test";
import { packageVersion } from "../src/version.mjs";
import {
  configureClaudeCodeViaCli,
  configureClaudeCodeViaFile,
  configureClaudeDesktop,
  removeClaudeCodeViaCli,
  countVaultNotes,
  detectClaudeCode,
  detectDailyNotesPath,
  findKnownVaults,
  getClaudeCodeConfigPath,
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

  const vaultWithJournal = join(fixture, "vault-journal");
  await mkdir(join(vaultWithJournal, "Journal"), { recursive: true });
  assert.equal(detectDailyNotesPath(vaultWithJournal), "Journal/");

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

  const entry2 = { command: "node", args: ["v2.js"] };
  await upsertJsonMcpServerConfig(configPath, entry2, { overwrite: true });

  // Backup must still be the original pre-tour state, NOT entry1
  const backup2 = JSON.parse(await readFile(`${configPath}.bak`, "utf8"));
  assert.deepEqual(backup2, original);

  const updated = JSON.parse(await readFile(configPath, "utf8"));
  assert.deepEqual(updated.mcpServers.obsidian, entry2);
  assert.deepEqual(updated.mcpServers.initial, original.mcpServers.initial);

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

  assert.equal(await readFile(configPath, "utf8"), corruptContent);
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
      } else if (text.includes("save notes")) {
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

  for (const configPath of [claudeCodeConfig, claudeDesktopConfig]) {
    const { args } = JSON.parse(await readFile(configPath, "utf8")).mcpServers.obsidian;
    assert.deepEqual(args.slice(-5), ["--vault", vaultPath, "--daily-path", "Daily/", "--write"]);
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

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "3"]]);
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

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "1"]]);
  await assert.rejects(
    runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
      claudeCodeConfigPath,
      detectClaudeCode: () => true,
      configureClaudeCodeViaCli: async () => ({ success: false, method: "cli", error: "managed settings forbid this server", spawnFailure: false }),
    }),
    /1 of 1/,
  );
  assert.equal(existsSync(claudeCodeConfigPath), false);
  assert.match(io.text(), /managed settings forbid/);
  assert.doesNotMatch(io.text(), /Replace the existing/);
});

test("runTour falls back to the config file when the claude CLI cannot be spawned", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-cli-missing-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const vaultPath = join(fixture, "Vault");
  await mkdir(vaultPath, { recursive: true });
  const claudeCodeConfigPath = join(fixture, "claude.json");

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "1"]]);
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

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ["/Journal", "../outside", "Journal"]], ["save notes", ""], ["Select an option", "4"]]);
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

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "4"]]);
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

  const io = scriptedTour([["Vault directory", ["Vault", "~/Vault"]], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "4"]]);
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

  const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "4"]]);
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

test("resolveServerLauncher runs the bundle in place for the portable client layout", async (t) => {
  const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-portable-")));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const bundle = join(fixture, "client", "server", "stdio.mjs");
  await mkdir(join(fixture, "client", "server"), { recursive: true });
  await writeFile(bundle, "");

  const launcher = resolveServerLauncher(bundle);
  assert.equal(launcher.command, process.execPath);
  assert.deepEqual(launcher.argsPrefix, [bundle]);
});

for (const [label, replies, writable] of [
  ["Enter", [""], true],
  ["yes", ["y"], true],
  ["no", ["n"], false],
  ["an unrecognized answer, then no", ["maybe", "no"], false],
]) {
  test(`runTour write choice: ${label}`, { timeout: 15_000 }, async (t) => {
    const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-write-")));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    const vaultPath = join(fixture, "Vault");
    await mkdir(vaultPath, { recursive: true });
    const claudeCodeConfigPath = join(fixture, "claude.json");
    const claudeDesktopConfigPath = join(fixture, "desktop.json");
    const asked = replies.length;

    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", [...replies]], ["Select an option", "3"]]);
    const outcome = await runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
      claudeCodeConfigPath,
      claudeDesktopConfigPath,
      detectClaudeCode: () => false,
    });

    assert.equal(outcome.write, writable);
    assert.equal(io.text().match(/save notes and add to your daily note\?/g).length, asked);
    for (const configPath of [claudeCodeConfigPath, claudeDesktopConfigPath]) {
      const { args } = JSON.parse(await readFile(configPath, "utf8")).mcpServers.obsidian;
      assert.equal(args.at(-1), writable ? "--write" : "--read", configPath);
    }
    assert.equal(/Writing is off/.test(io.text()), !writable);
  });
}

for (const [reply, replaced] of [["", true], ["n", false]]) {
  test(`runTour existing Claude Code entry, replace answer ${JSON.stringify(reply)}`, { timeout: 15_000 }, async (t) => {
    const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-cli-replace-"));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    const vaultPath = join(fixture, "Vault");
    await mkdir(vaultPath, { recursive: true });
    const claudeCodeConfigPath = join(fixture, "claude.json");
    const original = JSON.stringify({ mcpServers: { obsidian: { command: "old" } } });
    await writeFile(claudeCodeConfigPath, original);
    const calls = [];

    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "1"], ["Replace the existing", reply]]);
    const run = runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
      claudeCodeConfigPath,
      detectClaudeCode: () => true,
      configureClaudeCodeViaCli: async (vault, daily, options) => {
        calls.push(["add", options]);
        return calls.length === 1
          ? { success: false, method: "cli", error: "MCP server obsidian already exists in user config", spawnFailure: false }
          : { success: true, method: "cli" };
      },
      removeClaudeCodeViaCli: async () => {
        calls.push(["remove"]);
        return { success: true };
      },
    });

    if (replaced) {
      const outcome = await run;
      assert.deepEqual(calls, [["add", { write: true }], ["remove"], ["add", { write: true }]]);
      assert.deepEqual(outcome.results, [{ host: "Claude Code", success: true, method: "cli" }]);
      const backup = io.text().match(/previous config is saved at (.+)/)[1].trim();
      assert.equal(await readFile(backup, "utf8"), original);
    } else {
      await assert.rejects(run, /1 of 1/);
      assert.deepEqual(calls, [["add", { write: true }]]);
      assert.match(io.text(), /claude mcp remove obsidian -s user/);
    }
  });
}

for (const [label, removeResult, expected] of [
  ["remove fails", { success: false, error: "remove broke" }, /Could not remove the old entry: remove broke/],
  ["the second add fails", { success: true }, /The old "obsidian" entry was removed and the new one was not added\. To restore it, .*\.bak/],
]) {
  test(`runTour reports failure when ${label}`, { timeout: 15_000 }, async (t) => {
    const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-cli-replace-fail-"));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    const vaultPath = join(fixture, "Vault");
    await mkdir(vaultPath, { recursive: true });
    const claudeCodeConfigPath = join(fixture, "claude.json");
    await writeFile(claudeCodeConfigPath, JSON.stringify({ mcpServers: { obsidian: { command: "old" } } }));

    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "1"], ["Replace the existing", ""]]);
    let calls = 0;
    await assert.rejects(runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
      claudeCodeConfigPath,
      detectClaudeCode: () => true,
      configureClaudeCodeViaCli: async () => ({ success: false, method: "cli", error: calls++ ? "invalid server config" : "server obsidian already exists", spawnFailure: false }),
      removeClaudeCodeViaCli: async () => removeResult,
    }), /1 of 1/);
    assert.match(io.text(), expected);
    assert.equal(/was removed and the new one was not added/.test(io.text()), removeResult.success);
  });
}

test("removeClaudeCodeViaCli removes only the user-scope entry", async () => {
  let seen;
  const result = await removeClaudeCodeViaCli({ spawn: (cmd, args) => { seen = [cmd, ...args]; return { status: 0 }; } });
  assert.deepEqual(seen, ["claude", "mcp", "remove", "obsidian", "-s", "user"]);
  assert.equal(result.success, true);
  const failed = await removeClaudeCodeViaCli({ spawn: () => ({ status: 1, stderr: "nope\n" }) });
  assert.deepEqual(failed, { success: false, error: "nope" });
});

for (const [reply, writable] of [["", true], ["n", false]]) {
  test(`runTour snippet carries the write choice ${JSON.stringify(reply)}`, { timeout: 15_000 }, async (t) => {
    const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-snippet-")));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    const vaultPath = join(fixture, "Vault");
    await mkdir(vaultPath, { recursive: true });
    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", reply], ["Select an option", "4"]]);
    await runTour({ input: io.input, output: io.output, knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }], detectClaudeCode: () => false });
    const text = io.text();
    const snippet = JSON.parse(text.slice(text.indexOf("{", text.indexOf("Configuration snippet")), text.indexOf("Verifying vault notes")));
    assert.equal(snippet.mcpServers.obsidian.args.at(-1), writable ? "--write" : "--read");
  });
}

for (const [reply, writable] of [["", true], ["n", false]]) {
  test(`runTour names other entries that can override the write choice ${JSON.stringify(reply)}`, { timeout: 15_000 }, async (t) => {
    const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-other-entries-"));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    const vaultPath = join(fixture, "Vault");
    await mkdir(vaultPath, { recursive: true });
    const claudeCodeConfigPath = join(fixture, "claude.json");
    await writeFile(claudeCodeConfigPath, JSON.stringify({
      mcpServers: { "obsidian-write": { command: "old" } },
      projects: {
        "/work/shadowed": { mcpServers: { obsidian: { command: "old" } } },
        "/work/writer": { mcpServers: { "obsidian-write": { command: "old" } } },
        "/work/clean": { mcpServers: {} },
      },
    }));
    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", reply], ["Select an option", "1"]]);
    await runTour({ input: io.input, output: io.output, knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }], claudeCodeConfigPath, detectClaudeCode: () => false });
    assert.match(io.text(), /\/work\/shadowed has its own "obsidian" entry/);
    assert.doesNotMatch(io.text(), /\/work\/clean/);
    assert.equal(/"obsidian-write" entry .* can still write/.test(io.text()), !writable);
    assert.equal(/\/work\/writer has an "obsidian-write" entry/.test(io.text()), !writable);
  });
}

test("configureClaudeCodeViaCli appends --write only when asked", async () => {
  for (const write of [true, false]) {
    let seen;
    const spawnStub = (cmd, args) => {
      seen = args;
      return { status: 0 };
    };
    await configureClaudeCodeViaCli("/vault", "Daily/", { spawn: spawnStub, write });
    assert.deepEqual(seen.slice(0, 6), ["mcp", "add", "-s", "user", "obsidian", "--"]);
    assert.equal(seen.at(-1), write ? "--write" : "--read");
  }
});

for (const [label, content] of [
  ["the config file is missing", undefined],
  ["the config file has no obsidian entry", JSON.stringify({ mcpServers: {} })],
  ["the config file is not JSON", "{ truncated"],
]) {
  test(`runTour keeps the old entry when it cannot be backed up because ${label}`, { timeout: 15_000 }, async (t) => {
    const fixture = await mkdtemp(join(tmpdir(), "mcp-tour-no-backup-"));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    const vaultPath = join(fixture, "Vault");
    await mkdir(vaultPath, { recursive: true });
    const claudeCodeConfigPath = join(fixture, "claude.json");
    if (content !== undefined) await writeFile(claudeCodeConfigPath, content);
    let removed = false;

    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "1"], ["Replace the existing", ""]]);
    await assert.rejects(runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: vaultPath, open: true, ts: 0 }],
      claudeCodeConfigPath,
      detectClaudeCode: () => true,
      configureClaudeCodeViaCli: async () => ({ success: false, method: "cli", error: "server obsidian already exists", spawnFailure: false }),
      removeClaudeCodeViaCli: async () => { removed = true; return { success: true }; },
    }), /1 of 1/);
    assert.equal(removed, false);
    assert.match(io.text(), /Kept the old entry, because it could not be backed up/);
    assert.match(io.text(), /claude mcp remove obsidian -s user/);
  });
}

test("getClaudeCodeConfigPath follows CLAUDE_CONFIG_DIR", (t) => {
  const previous = process.env.CLAUDE_CONFIG_DIR;
  t.after(() => {
    if (previous === undefined) delete process.env.CLAUDE_CONFIG_DIR;
    else process.env.CLAUDE_CONFIG_DIR = previous;
  });
  process.env.CLAUDE_CONFIG_DIR = "/custom/claude";
  assert.equal(getClaudeCodeConfigPath(), join("/custom/claude", ".claude.json"));
  delete process.env.CLAUDE_CONFIG_DIR;
  assert.equal(getClaudeCodeConfigPath(), join(homedir(), ".claude.json"));
});

for (const { label, existing, cwdIsVault, expected, willCreate } of [
  { label: "the current folder when it is a vault", existing: ["Documents/Obsidian"], cwdIsVault: true, expected: "work", willCreate: false },
  { label: "~/Documents/Obsidian when it exists", existing: ["Documents/Obsidian", "Obsidian"], expected: "Documents/Obsidian", willCreate: false },
  { label: "~/Obsidian when that is the one that exists", existing: ["Obsidian"], expected: "Obsidian", willCreate: false },
  { label: "a new ~/Documents/Obsidian when nothing exists", existing: [], expected: "Documents/Obsidian", willCreate: true },
]) {
  test(`runTour accepts Enter at the vault path prompt: ${label}`, { timeout: 15_000 }, async (t) => {
    const home = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-default-")));
    t.after(() => rm(home, { recursive: true, force: true }));
    const cwd = join(home, "work");
    await mkdir(cwd, { recursive: true });
    if (cwdIsVault) await mkdir(join(cwd, ".obsidian"));
    for (const dir of existing) await mkdir(join(home, dir), { recursive: true });

    const io = scriptedTour([["Vault directory", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "4"]]);
    const result = await runTour({ input: io.input, output: io.output, knownVaults: [], homeDir: home, cwd });

    assert.equal(result.vault, join(home, expected));
    assert.equal(lstatSync(join(home, expected)).isDirectory(), true);
    assert.equal(io.text().includes(`Vault directory (default: ${join(home, expected)}${willCreate ? ", will be created" : ""}): `), true);
  });
}

test("runTour creates no folder when a vault path is typed instead of accepted", { timeout: 15_000 }, async (t) => {
  const home = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-typed-")));
  t.after(() => rm(home, { recursive: true, force: true }));
  await mkdir(join(home, "Mine"));

  const io = scriptedTour([["Vault directory", "~/Mine"], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "4"]]);
  const result = await runTour({ input: io.input, output: io.output, knownVaults: [], homeDir: home, cwd: home });

  assert.equal(result.vault, join(home, "Mine"));
  assert.equal(existsSync(join(home, "Documents")), false);
});

test("runTour asks again when the default vault folder cannot be created", { timeout: 15_000 }, async (t) => {
  const home = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-blocked-")));
  t.after(() => rm(home, { recursive: true, force: true }));
  await mkdir(join(home, "Documents"));
  await writeFile(join(home, "Documents", "Obsidian"), "a file, not a folder");
  await mkdir(join(home, "Mine"));

  const io = scriptedTour([["Vault directory", ["", "~/Mine"]], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "4"]]);
  const result = await runTour({ input: io.input, output: io.output, knownVaults: [], homeDir: home, cwd: home });

  assert.equal(result.vault, join(home, "Mine"));
  assert.match(io.text(), /Could not create .*Documents\/Obsidian: .*EEXIST/);
});

test("runTour still accepts Enter after a mistyped vault path", { timeout: 15_000 }, async (t) => {
  const home = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-mistyped-")));
  t.after(() => rm(home, { recursive: true, force: true }));

  const io = scriptedTour([["Vault directory", ["~/Missing", ""]], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", "4"]]);
  const result = await runTour({ input: io.input, output: io.output, knownVaults: [], homeDir: home, cwd: home });

  assert.equal(result.vault, join(home, "Documents", "Obsidian"));
  assert.match(io.text(), /Directory does not exist: ~\/Missing/);
  assert.equal(existsSync(join(home, "Missing")), false);
});

for (const [choice, hasCode, hasDesktop] of [["1", true, false], ["2", false, true], ["3", true, true], ["4", false, false]]) {
  test(`runTour ends with next steps and no server command for host choice ${choice}`, { timeout: 15_000 }, async (t) => {
    const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-next-")));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    await mkdir(join(fixture, "Vault"));

    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", choice]]);
    await runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: join(fixture, "Vault"), open: true, ts: 0 }],
      claudeCodeConfigPath: join(fixture, "claude.json"),
      claudeDesktopConfigPath: join(fixture, "desktop.json"),
      detectClaudeCode: () => false,
    });

    const ending = io.text().slice(io.text().indexOf("Setup complete!"));
    assert.equal(/stdio|--vault/.test(ending), false, ending);
    assert.equal(/start `claude` and ask/.test(ending), hasCode);
    assert.equal(/Quit Claude Desktop and open it again/.test(ending), hasDesktop);
    assert.equal(/`claude` command line is set up separately/.test(ending), choice === "2");
    assert.equal(/nothing to start yourself/.test(ending), hasCode || hasDesktop);
    assert.equal(/Add the snippet above to your app's settings, then restart the app/.test(ending), choice === "4");
  });
}

for (const { code, desktop, expected, found } of [
  { code: true, desktop: true, expected: 3, found: "Found: Claude Code, Claude Desktop" },
  { code: true, desktop: false, expected: 1, found: "Found: Claude Code" },
  { code: false, desktop: true, expected: 2, found: "Found: Claude Desktop" },
  { code: false, desktop: false, expected: 4, found: "Found no Claude app" },
]) {
  test(`runTour Enter connects what it detects: code=${code} desktop=${desktop}`, { timeout: 15_000 }, async (t) => {
    const fixture = realpathSync(await mkdtemp(join(tmpdir(), "mcp-tour-detect-")));
    t.after(() => rm(fixture, { recursive: true, force: true }));
    await mkdir(join(fixture, "Vault"));
    const claudeCodeConfigPath = join(fixture, "claude.json");
    const claudeDesktopConfigPath = join(fixture, "DesktopApp", "claude_desktop_config.json");
    if (desktop) await mkdir(join(fixture, "DesktopApp"));
    let cliAdds = 0;

    const io = scriptedTour([["Select a vault", ""], ["Daily notes directory", ""], ["save notes", ""], ["Select an option", ""]]);
    const outcome = await runTour({
      input: io.input,
      output: io.output,
      knownVaults: [{ id: "1", path: join(fixture, "Vault"), open: true, ts: 0 }],
      claudeCodeConfigPath,
      claudeDesktopConfigPath,
      detectClaudeCode: () => code,
      configureClaudeCodeViaCli: async () => { cliAdds += 1; return { success: true, method: "cli" }; },
    });

    assert.equal(outcome.hostChoice, expected);
    assert.equal(io.text().includes(`${found}\n`) || io.text().includes(`${found}.`), true, io.text());
    assert.equal(io.text().includes(`Select an option [1-4] (default: ${expected}): `), true);
    assert.equal(cliAdds, code ? 1 : 0);
    assert.equal(existsSync(claudeDesktopConfigPath), desktop);
    assert.equal(existsSync(claudeCodeConfigPath), false);
  });
}
