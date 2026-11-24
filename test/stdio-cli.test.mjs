import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { EX_USAGE, parseCliArgs, shouldLaunchTour } from "../src/stdio.mjs";

const packageRoot = resolve(dirname(new URL(import.meta.url).pathname), "..");
const entrypoint = join(packageRoot, "src", "stdio.mjs");
let repositoryRoot = packageRoot;
try {
  repositoryRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], { cwd: packageRoot, encoding: "utf8" }).trim();
} catch {}
const packageManifest = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8"));

function startCliServer(args = [], extraEnv = {}) {
  const env = { ...process.env, MCP_REPOSITORY_ROOTS: repositoryRoot, ...extraEnv };
  delete env.OBSIDIAN_LOCAL_MD;
  if (extraEnv.OBSIDIAN_LOCAL_MD !== undefined) {
    env.OBSIDIAN_LOCAL_MD = extraEnv.OBSIDIAN_LOCAL_MD;
  }

  const child = spawn(process.execPath, [entrypoint, ...args], {
    cwd: tmpdir(),
    env,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  let childError;
  const messages = [];
  let messageWaiter;

  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    for (;;) {
      const newline = stdout.indexOf("\n");
      if (newline < 0) break;
      const line = stdout.slice(0, newline).trim();
      stdout = stdout.slice(newline + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch (error) {
        message = { parseFailure: error.message, line };
      }
      messages.push(message);
      messageWaiter?.();
      messageWaiter = undefined;
    }
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  child.on("error", (error) => {
    childError = error;
    messageWaiter?.();
    messageWaiter = undefined;
  });

  return {
    child,
    stderr: () => stderr,
    trailing: () => stdout,
    async request(message, id, timeoutMs = 4000) {
      child.stdin.write(`${JSON.stringify(message)}\n`);
      for (;;) {
        if (childError) throw childError;
        const response = messages.find((candidate) => candidate.id === id);
        if (response) return response;
        await Promise.race([
          new Promise((resolveWait) => {
            messageWaiter = resolveWait;
          }),
          new Promise((_, reject) => {
            setTimeout(() => reject(new Error(`timed out waiting for MCP response ${id}`)), timeoutMs);
          }),
        ]);
      }
    },
    notification(message) {
      child.stdin.write(`${JSON.stringify(message)}\n`);
    },
    messages,
  };
}

function runCliProcess(args = [], extraEnv = {}) {
  return new Promise((resolveRun) => {
    const env = { ...process.env, ...extraEnv };
    delete env.OBSIDIAN_LOCAL_MD;
    if (extraEnv.OBSIDIAN_LOCAL_MD !== undefined) {
      env.OBSIDIAN_LOCAL_MD = extraEnv.OBSIDIAN_LOCAL_MD;
    }

    const child = spawn(process.execPath, [entrypoint, ...args], {
      cwd: tmpdir(),
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("close", (code) => {
      resolveRun({ code, stdout, stderr });
    });
  });
}

test("parseCliArgs unit tests", () => {
  assert.deepEqual(parseCliArgs([]), { help: false, tour: false, serve: false, vault: undefined, dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["-h"]), { help: true });
  assert.deepEqual(parseCliArgs(["--help"]), { help: true });
  assert.deepEqual(parseCliArgs(["--tour"]), { help: false, tour: true, serve: false, vault: undefined, dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["--serve"]), { help: false, tour: false, serve: true, vault: undefined, dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["--stdio"]), { help: false, tour: false, serve: true, vault: undefined, dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["--vault", "/path/to/vault"]), { help: false, tour: false, serve: false, vault: "/path/to/vault", dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["-v", "/path/to/vault"]), { help: false, tour: false, serve: false, vault: "/path/to/vault", dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["--vault=/path/to/vault"]), { help: false, tour: false, serve: false, vault: "/path/to/vault", dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["-v=/path/to/vault"]), { help: false, tour: false, serve: false, vault: "/path/to/vault", dailyPath: undefined, repoRoots: [] });
  assert.deepEqual(parseCliArgs(["--vault", "/v", "--daily-path", "Custom/"]), { help: false, tour: false, serve: false, vault: "/v", dailyPath: "Custom/", repoRoots: [] });
  assert.deepEqual(parseCliArgs(["--vault", "/v", "--daily-path=Custom/"]), { help: false, tour: false, serve: false, vault: "/v", dailyPath: "Custom/", repoRoots: [] });
  assert.deepEqual(parseCliArgs(["--vault", "/v", "--repo-root", "/r1", "--repo-root", "/r2"]), {
    help: false,
    tour: false,
    serve: false,
    vault: "/v",
    dailyPath: undefined,
    repoRoots: ["/r1", "/r2"],
  });
  assert.throws(() => parseCliArgs(["--unknown"]), /unrecognized argument/);
  assert.throws(() => parseCliArgs(["--vault"]), /missing required argument for --vault/);
  assert.throws(() => parseCliArgs(["-v"]), /missing required argument for --vault/);
  assert.throws(() => parseCliArgs(["--vault", "-v"]), /missing required argument for --vault/);
  assert.throws(() => parseCliArgs(["--daily-path"]), /missing required argument for --daily-path/);
  assert.throws(() => parseCliArgs(["--repo-root"]), /missing required argument for --repo-root/);
});

test("shows usage and exits 0 on --help and -h", async () => {
  const helpLong = await runCliProcess(["--help"]);
  assert.equal(helpLong.code, 0);
  assert.match(helpLong.stdout, /Usage: obsidian-mcp \[options\]/);
  assert.match(helpLong.stdout, /--vault <path>/);
  assert.match(helpLong.stdout, /--daily-path <path>/);
  assert.match(helpLong.stdout, /--repo-root <path>/);

  const helpShort = await runCliProcess(["-h"]);
  assert.equal(helpShort.code, 0);
  assert.match(helpShort.stdout, /Usage: obsidian-mcp \[options\]/);
});

test("validates rejection of non-existent, relative, or file --vault paths with EX_USAGE (64)", async () => {
  assert.equal(EX_USAGE, 64);

  const relativeRun = await runCliProcess(["--vault", "relative/path/to/vault"]);
  assert.equal(relativeRun.code, 64);
  assert.match(relativeRun.stderr, /--vault must be an absolute path/);

  const nonExistentRun = await runCliProcess(["--vault", "/path/that/does/not/exist/at/all"]);
  assert.equal(nonExistentRun.code, 64);
  assert.match(nonExistentRun.stderr, /--vault path does not exist/);

  const fixture = await mkdtemp(join(tmpdir(), "mcp-test-file-"));
  const filePath = join(fixture, "not-a-dir.txt");
  await writeFile(filePath, "test");
  try {
    const fileRun = await runCliProcess(["--vault", filePath]);
    assert.equal(fileRun.code, 64);
    assert.match(fileRun.stderr, /--vault path is not a directory/);
  } finally {
    await rm(fixture, { recursive: true, force: true });
  }

  const missingArgRun = await runCliProcess(["--vault"]);
  assert.equal(missingArgRun.code, 64);
  assert.match(missingArgRun.stderr, /missing required argument for --vault/);

  const unrecognizedRun = await runCliProcess(["--unknown-flag"]);
  assert.equal(unrecognizedRun.code, 64);
  assert.match(unrecognizedRun.stderr, /unrecognized argument/);
});

test("validates rejection of invalid --daily-path and --repo-root with EX_USAGE (64)", async () => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-cli-vault-"));
  try {
    const absDaily = await runCliProcess(["--vault", vault, "--daily-path", "/absolute/daily"]);
    assert.equal(absDaily.code, 64);
    assert.match(absDaily.stderr, /--daily-path must remain within the configured vault/);

    const traversalDaily = await runCliProcess(["--vault", vault, "--daily-path", "../outside"]);
    assert.equal(traversalDaily.code, 64);
    assert.match(traversalDaily.stderr, /--daily-path must remain within the configured vault/);

    const newlineDaily = await runCliProcess(["--vault", vault, "--daily-path", "Daily\nTest"]);
    assert.equal(newlineDaily.code, 64);
    assert.match(newlineDaily.stderr, /--daily-path contains invalid characters/);

    const emptyDaily = await runCliProcess(["--vault", vault, "--daily-path", "///"]);
    assert.equal(emptyDaily.code, 64);
    assert.match(emptyDaily.stderr, /--daily-path must remain within the configured vault/);

    const relRepo = await runCliProcess(["--vault", vault, "--repo-root", "relative/repo"]);
    assert.equal(relRepo.code, 64);
    assert.match(relRepo.stderr, /--repo-root must be an absolute path/);

    const missingRepo = await runCliProcess(["--vault", vault, "--repo-root", "/nonexistent/repo/root"]);
    assert.equal(missingRepo.code, 64);
    assert.match(missingRepo.stderr, /--repo-root path does not exist/);
  } finally {
    await rm(vault, { recursive: true, force: true });
  }
});

test("server starts up and serves tools when started with --vault <scratch-vault-path>", async (t) => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-cli-scratch-"));
  await writeFile(join(vault, "welcome.md"), "# Welcome\nThis is a scratch note for testing CLI vault.\n");

  const server = startCliServer(["--vault", vault]);
  t.after(async () => {
    if (!server.child.killed) server.child.stdin.end();
    await Promise.race([
      once(server.child, "close").catch(() => {}),
      new Promise((resolveWait) => setTimeout(resolveWait, 200)),
    ]);
    await rm(vault, { recursive: true, force: true });
  });

  const initialized = await server.request(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-11-25",
        capabilities: {},
        clientInfo: { name: "stdio-cli-test", version: "1.0.0" },
      },
    },
    1,
  );
  assert.equal(initialized.jsonrpc, "2.0");
  assert.equal(initialized.id, 1);
  assert.equal(initialized.result?.serverInfo?.name, "claude-obsidian-mcp");
  assert.equal(initialized.result?.serverInfo?.version, packageManifest.version);
  server.notification({ jsonrpc: "2.0", method: "notifications/initialized" });

  const toolsResponse = await server.request(
    {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/list",
      params: {},
    },
    2,
  );
  assert.equal(toolsResponse.result?.isError, undefined);
  const toolNames = toolsResponse.result?.tools?.map((tool) => tool.name).sort();
  assert.deepEqual(toolNames, ["obsidian_commit_meta", "obsidian_find_notes"]);

  const searchResponse = await server.request(
    {
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: {
        name: "obsidian_find_notes",
        arguments: { query: "scratch" },
      },
    },
    3,
  );
  assert.equal(searchResponse.result?.isError, false);
  const matches = searchResponse.result?.structuredContent?.matches;
  assert.ok(Array.isArray(matches) && matches.length === 1);
  assert.equal(matches[0].path, "welcome.md");
  assert.match(matches[0].preview, /scratch note for testing CLI vault/);
});

test("server starts up with -v alias", async (t) => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-cli-alias-"));
  await writeFile(join(vault, "alias-note.md"), "# Alias Note\nFound via -v alias.\n");

  const server = startCliServer(["-v", vault]);
  t.after(async () => {
    if (!server.child.killed) server.child.stdin.end();
    await Promise.race([
      once(server.child, "close").catch(() => {}),
      new Promise((resolveWait) => setTimeout(resolveWait, 200)),
    ]);
    await rm(vault, { recursive: true, force: true });
  });

  const initialized = await server.request(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-11-25",
        capabilities: {},
        clientInfo: { name: "stdio-cli-test", version: "1.0.0" },
      },
    },
    1,
  );
  assert.equal(initialized.jsonrpc, "2.0");
  server.notification({ jsonrpc: "2.0", method: "notifications/initialized" });

  const searchResponse = await server.request(
    {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: {
        name: "obsidian_find_notes",
        arguments: { query: "alias" },
      },
    },
    2,
  );
  assert.equal(searchResponse.result?.isError, false);
  const matches = searchResponse.result?.structuredContent?.matches;
  assert.ok(Array.isArray(matches) && matches.length === 1);
  assert.equal(matches[0].path, "alias-note.md");
});

test("explicit CLI --vault overrides ambient configuration", async (t) => {
  const ambientFixture = await mkdtemp(join(tmpdir(), "mcp-ambient-"));
  const ambientVault = join(ambientFixture, "ambient-vault");
  const ambientConfig = join(ambientFixture, "obsidian.local.md");
  await mkdir(ambientVault);
  await writeFile(join(ambientVault, "ambient-note.md"), "# Ambient note\nFrom ambient vault\n");
  await writeFile(ambientConfig, `---\nvault_path: ${ambientVault}\ndaily_path: Daily/\n---\n`);

  const cliVault = await mkdtemp(join(tmpdir(), "mcp-cli-override-"));
  await writeFile(join(cliVault, "cli-note.md"), "# CLI Note\nFrom CLI vault\n");

  const server = startCliServer(["--vault", cliVault], { OBSIDIAN_LOCAL_MD: ambientConfig });
  t.after(async () => {
    if (!server.child.killed) server.child.stdin.end();
    await Promise.race([
      once(server.child, "close").catch(() => {}),
      new Promise((resolveWait) => setTimeout(resolveWait, 200)),
    ]);
    await rm(ambientFixture, { recursive: true, force: true });
    await rm(cliVault, { recursive: true, force: true });
  });

  await server.request(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "test", version: "1.0" } },
    },
    1,
  );
  server.notification({ jsonrpc: "2.0", method: "notifications/initialized" });

  const searchCli = await server.request(
    {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: { name: "obsidian_find_notes", arguments: { query: "cli" } },
    },
    2,
  );
  assert.equal(searchCli.result?.isError, false);
  const cliMatches = searchCli.result?.structuredContent?.matches;
  assert.equal(cliMatches.length, 1);
  assert.equal(cliMatches[0].path, "cli-note.md");

  const searchAmbient = await server.request(
    {
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: { name: "obsidian_find_notes", arguments: { query: "ambient" } },
    },
    3,
  );
  assert.equal(searchAmbient.result?.isError, false);
  const ambientMatches = searchAmbient.result?.structuredContent?.matches;
  assert.equal(ambientMatches.length, 0);
});

test("keeper save and daily append work against a vault configured purely via --vault", async (t) => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-cli-pure-write-"));

  const server = startCliServer(["--vault", vault], { MCP_STDIO_PROFILE: "write" });
  t.after(async () => {
    if (!server.child.killed) server.child.stdin.end();
    await Promise.race([
      once(server.child, "close").catch(() => {}),
      new Promise((resolveWait) => setTimeout(resolveWait, 200)),
    ]);
    await rm(vault, { recursive: true, force: true });
  });

  const initialized = await server.request(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "test", version: "1.0" } },
    },
    1,
  );
  assert.equal(initialized.jsonrpc, "2.0");
  server.notification({ jsonrpc: "2.0", method: "notifications/initialized" });

  const toolsResponse = await server.request(
    { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
    2,
  );
  const toolNames = toolsResponse.result?.tools?.map((tool) => tool.name).sort();
  assert.deepEqual(toolNames, ["obsidian_commit_meta", "obsidian_daily_append", "obsidian_find_notes", "obsidian_keeper_save"]);

  const saveResponse = await server.request(
    {
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: {
        name: "obsidian_keeper_save",
        arguments: {
          title: "CLI Save Note",
          body: "Written via pure CLI vault config",
          resolved: true,
          folder_hint: "Notes",
          idempotency_key: "cli-save-1",
          request_id: "req-save-1",
        },
      },
    },
    3,
  );
  assert.equal(saveResponse.result?.isError, false);
  const saveData = JSON.parse(saveResponse.result.content[0].text);
  assert.equal(saveData.status, "committed");
  assert.equal(saveData.path, "Notes/CLI Save Note.md");
  assert.equal(saveData.idempotency_key, "cli-save-1");

  const writtenNoteContent = await readFile(join(vault, "Notes", "CLI Save Note.md"), "utf8");
  assert.match(writtenNoteContent, /Written via pure CLI vault config/);

  const appendResponse = await server.request(
    {
      jsonrpc: "2.0",
      id: 4,
      method: "tools/call",
      params: {
        name: "obsidian_daily_append",
        arguments: {
          content: "Daily note entry from pure CLI",
          date: "2026-11-23",
          idempotency_key: "cli-daily-1",
        },
      },
    },
    4,
  );
  assert.equal(appendResponse.result?.isError, false);
  const appendData = JSON.parse(appendResponse.result.content[0].text);
  assert.equal(appendData.status, "committed");

  const writtenDailyContent = await readFile(join(vault, "Daily", "2026-11-23.md"), "utf8");
  assert.match(writtenDailyContent, /Daily note entry from pure CLI/);
});

test("daily append works with custom --daily-path configured via CLI", async (t) => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-cli-custom-daily-"));

  const server = startCliServer(["--vault", vault, "--daily-path", "Journal/"], { MCP_STDIO_PROFILE: "write" });
  t.after(async () => {
    if (!server.child.killed) server.child.stdin.end();
    await Promise.race([
      once(server.child, "close").catch(() => {}),
      new Promise((resolveWait) => setTimeout(resolveWait, 200)),
    ]);
    await rm(vault, { recursive: true, force: true });
  });

  await server.request(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "test", version: "1.0" } },
    },
    1,
  );
  server.notification({ jsonrpc: "2.0", method: "notifications/initialized" });

  const appendResponse = await server.request(
    {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: {
        name: "obsidian_daily_append",
        arguments: {
          content: "Journal entry via custom daily-path",
          date: "2026-11-23",
          idempotency_key: "custom-daily-1",
        },
      },
    },
    2,
  );
  assert.equal(appendResponse.result?.isError, false);
  const appendData = JSON.parse(appendResponse.result.content[0].text);
  assert.equal(appendData.status, "committed");

  const writtenJournal = await readFile(join(vault, "Journal", "2026-11-23.md"), "utf8");
  assert.match(writtenJournal, /Journal entry via custom daily-path/);
});

test("supports repeatable and platform-delimited --repo-root", async (t) => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-cli-repo-vault-"));
  const repo1 = await mkdtemp(join(tmpdir(), "mcp-repo1-"));
  const repo2 = await mkdtemp(join(tmpdir(), "mcp-repo2-"));

  const server = startCliServer(["--vault", vault, "--repo-root", `${repo1}:${repo2}`]);
  t.after(async () => {
    if (!server.child.killed) server.child.stdin.end();
    await Promise.race([
      once(server.child, "close").catch(() => {}),
      new Promise((resolveWait) => setTimeout(resolveWait, 200)),
    ]);
    await rm(vault, { recursive: true, force: true });
    await rm(repo1, { recursive: true, force: true });
    await rm(repo2, { recursive: true, force: true });
  });

  await server.request(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "test", version: "1.0" } },
    },
    1,
  );
  server.notification({ jsonrpc: "2.0", method: "notifications/initialized" });

  const commitMeta = await server.request(
    {
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: {
        name: "obsidian_commit_meta",
        arguments: { repository: repo1 },
      },
    },
    2,
  );
  // repo1 is not a git repo, so commit-meta returns PATH_INVALID or METADATA_INCOMPLETE, NOT "repository is outside the approved roots"
  assert.equal(commitMeta.result?.isError, true);
  const errorObj = JSON.parse(commitMeta.result.content[0].text);
  assert.notEqual(errorObj.detail, "repository is outside the approved roots");
});

test("synthetic configuration is cleaned up on server exit", async () => {
  const staging = await mkdtemp(join(tmpdir(), "mcp-staging-tmp-"));
  const vault = await mkdtemp(join(tmpdir(), "mcp-staging-vault-"));
  try {
    const server = startCliServer(["--vault", vault], { TMPDIR: staging });
    await server.request(
      {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "test", version: "1.0" } },
      },
      1,
    );
    server.notification({ jsonrpc: "2.0", method: "notifications/initialized" });

    const entriesWhileRunning = await readdir(staging);
    const mcpDirs = entriesWhileRunning.filter((name) => name.startsWith("obsidian-mcp-"));
    assert.equal(mcpDirs.length, 1);
    const createdConfig = join(staging, mcpDirs[0], "obsidian.local.md");
    assert.ok(existsSync(createdConfig));
    const configText = await readFile(createdConfig, "utf8");
    assert.match(configText, /vault_path:/);

    server.child.stdin.end();
    await once(server.child, "close");

    assert.ok(!existsSync(join(staging, mcpDirs[0])));
  } finally {
    await rm(staging, { recursive: true, force: true });
    await rm(vault, { recursive: true, force: true });
  }
});

test("shouldLaunchTour evaluates interactive seam conditions correctly", () => {
  // Piped non-TTY stdio (default MCP host spawn) never launches tour
  assert.equal(shouldLaunchTour({ isTTY: false }), false);
  assert.equal(shouldLaunchTour({ cli: {}, env: {}, isTTY: false }), false);

  // Pure interactive terminal without args launches tour
  assert.equal(shouldLaunchTour({ cli: {}, env: {}, isTTY: true }), true);

  // Passing --tour forces tour even on non-TTY
  assert.equal(shouldLaunchTour({ cli: { tour: true }, env: {}, isTTY: false }), true);
  assert.equal(shouldLaunchTour({ cli: { tour: true }, env: {}, isTTY: true }), true);

  // Passing --vault suppresses tour
  assert.equal(shouldLaunchTour({ cli: { vault: "/path" }, env: {}, isTTY: true }), false);

  // Passing --serve or --stdio suppresses tour
  assert.equal(shouldLaunchTour({ cli: { serve: true }, env: {}, isTTY: true }), false);

  // Setting MCP_SERVE_STDIO suppresses tour
  assert.equal(shouldLaunchTour({ cli: {}, env: { MCP_SERVE_STDIO: "1" }, isTTY: true }), false);

  // Setting OBSIDIAN_LOCAL_MD suppresses tour
  assert.equal(shouldLaunchTour({ cli: {}, env: { OBSIDIAN_LOCAL_MD: "/path" }, isTTY: true }), false);
});

test("a piped spawn with no arguments never writes the tour banner to stdout", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-cli-noargs-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));
  const env = { ...process.env, HOME: fixture, XDG_CONFIG_HOME: fixture, APPDATA: fixture };
  delete env.OBSIDIAN_LOCAL_MD;
  delete env.MCP_SERVE_STDIO;
  const child = spawn(process.execPath, [entrypoint], { cwd: fixture, env, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
  });
  child.stderr.resume();
  setTimeout(() => child.stdin.end(), 500);
  await Promise.race([
    once(child, "close"),
    new Promise((_, reject) => setTimeout(() => {
      child.kill();
      reject(new Error("timed out waiting for no-argument server to close"));
    }, 5000)),
  ]);
  assert.doesNotMatch(stdout, /Obsidian MCP Server Setup|Select a vault|Enter the absolute path/);
});

test("server starts up and serves tools when started with --serve", async (t) => {
  const vault = await mkdtemp(join(tmpdir(), "mcp-cli-serve-"));

  const server = startCliServer(["--serve", "--vault", vault]);
  t.after(async () => {
    if (!server.child.killed) server.child.stdin.end();
    await Promise.race([
      once(server.child, "close").catch(() => {}),
      new Promise((resolveWait) => setTimeout(resolveWait, 200)),
    ]);
    await rm(vault, { recursive: true, force: true });
  });

  const initResponse = await server.request(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "test", version: "1.0" } },
    },
    1,
  );
  assert.equal(initResponse.result.protocolVersion, "2025-11-25");
});

test("server launches tour banner when started with --tour", async (t) => {
  const fixture = await mkdtemp(join(tmpdir(), "mcp-cli-tour-"));
  t.after(() => rm(fixture, { recursive: true, force: true }));

  const child = spawn(process.execPath, [entrypoint, "--tour"], {
    cwd: fixture,
    env: {
      ...process.env,
      HOME: fixture,
      XDG_CONFIG_HOME: fixture,
      APPDATA: fixture,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });

  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    if (stdout.includes("Select a vault") || stdout.includes("Enter the absolute path")) {
      child.stdin.end();
    }
  });

  const [code] = await Promise.race([
    once(child, "close"),
    new Promise((_, reject) => setTimeout(() => {
      child.kill();
      reject(new Error("timed out waiting for tour CLI process to close"));
    }, 5000)),
  ]);

  assert.match(stdout, /Obsidian MCP Server Setup/);
  assert.notEqual(code, 0, "closing stdin mid-tour must not exit 0");
  assert.match(stderr, /cancelled/);
});

