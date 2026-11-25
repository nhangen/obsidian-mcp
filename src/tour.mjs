import { spawnSync } from "node:child_process";
import { constants, existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { copyFile, mkdir, readdir, readFile, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import * as readline from "node:readline/promises";
import { fileURLToPath } from "node:url";
import { validateDailyPath } from "./stdio.mjs";
import { packageVersion } from "./version.mjs";

export function getObsidianConfigPath() {
  if (process.platform === "darwin") {
    return join(homedir(), "Library", "Application Support", "obsidian", "obsidian.json");
  }
  if (process.platform === "win32") {
    return process.env.APPDATA
      ? join(process.env.APPDATA, "obsidian", "obsidian.json")
      : join(homedir(), "AppData", "Roaming", "obsidian", "obsidian.json");
  }
  return process.env.XDG_CONFIG_HOME
    ? join(process.env.XDG_CONFIG_HOME, "obsidian", "obsidian.json")
    : join(homedir(), ".config", "obsidian", "obsidian.json");
}

export function findKnownVaults(configPath = getObsidianConfigPath(), logger = () => {}) {
  try {
    if (!existsSync(configPath)) return [];
    let content;
    try {
      content = readFileSync(configPath, "utf8");
    } catch (err) {
      logger(`Warning: could not read Obsidian registry at ${configPath}: ${err.message}`);
      return [];
    }
    let data;
    try {
      data = JSON.parse(content);
    } catch (err) {
      logger(`Warning: Obsidian registry at ${configPath} contains malformed JSON: ${err.message}`);
      return [];
    }
    if (!data || typeof data.vaults !== "object" || data.vaults === null) return [];
    const vaults = [];
    for (const [id, entry] of Object.entries(data.vaults)) {
      if (entry && typeof entry.path === "string" && existsSync(entry.path)) {
        try {
          if (statSync(entry.path).isDirectory()) {
            vaults.push({
              id,
              path: realpathSync(entry.path),
              open: Boolean(entry.open),
              ts: typeof entry.ts === "number" ? entry.ts : 0,
            });
          }
        } catch {}
      }
    }
    vaults.sort((a, b) => {
      if (a.open && !b.open) return -1;
      if (!a.open && b.open) return 1;
      return b.ts - a.ts;
    });
    return vaults;
  } catch {
    return [];
  }
}

function isDirectory(path) {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

export function suggestVaultPath(home, cwd) {
  if (isDirectory(join(cwd, ".obsidian"))) return { path: resolve(cwd), exists: true };
  const candidates = [join(home, "Documents", "Obsidian"), join(home, "Obsidian")];
  const found = candidates.find(isDirectory);
  return found ? { path: found, exists: true } : { path: candidates[0], exists: false };
}

export function detectDailyNotesPath(vaultPath) {
  try {
    const dailyNotesConfig = join(vaultPath, ".obsidian", "daily-notes.json");
    if (existsSync(dailyNotesConfig)) {
      const data = JSON.parse(readFileSync(dailyNotesConfig, "utf8"));
      if (typeof data.folder === "string" && data.folder.trim() !== "") {
        try {
          return validateDailyPath(data.folder, vaultPath);
        } catch {}
      }
    }
  } catch {}

  for (const candidate of ["Daily", "Journal", "Dailies", "Notes"]) {
    try {
      const p = join(vaultPath, candidate);
      if (existsSync(p) && statSync(p).isDirectory()) {
        return `${candidate}/`;
      }
    } catch {}
  }
  return "Daily/";
}

export async function countVaultNotes(vaultPath, maxCount = 500) {
  let count = 0;
  let hasMore = false;
  const ignored = new Set([".obsidian", ".git", "node_modules", ".trash"]);

  let rootEntries;
  try {
    rootEntries = await readdir(vaultPath, { withFileTypes: true });
  } catch (err) {
    throw new Error(`Cannot read vault directory ${vaultPath}: ${err.message}`);
  }

  async function scan(dir, entries) {
    if (count >= maxCount) {
      hasMore = true;
      return;
    }
    for (const entry of entries) {
      if (count >= maxCount) {
        hasMore = true;
        return;
      }
      if (entry.isDirectory()) {
        if (!ignored.has(entry.name)) {
          let subEntries;
          try {
            subEntries = await readdir(join(dir, entry.name), { withFileTypes: true });
            await scan(join(dir, entry.name), subEntries);
          } catch {}
        }
      } else if (entry.isFile() && entry.name.endsWith(".md")) {
        count++;
      }
    }
  }

  await scan(vaultPath, rootEntries);
  return { count, hasMore };
}

export function resolveServerLauncher(currentFile = fileURLToPath(import.meta.url)) {
  const npx = { command: "npx", argsPrefix: ["-y", `@nhangen/obsidian-mcp@${packageVersion}`] };
  // An npx cache directory can be pruned at any time, so a config that points
  // into it would break later; re-resolve through npx at the same version.
  if (currentFile.split(/[\\/]/).includes("_npx")) return npx;
  const packageDir = resolve(dirname(currentFile), "..");
  const candidates = [join(packageDir, "dist", "stdio.mjs"), join(packageDir, "src", "stdio.mjs")];
  // The portable client ships the bundle as server/stdio.mjs, outside any dist/.
  if (basename(currentFile) === "stdio.mjs") candidates.unshift(currentFile);
  for (const candidate of candidates) {
    try {
      if (existsSync(candidate)) {
        return { command: process.execPath, argsPrefix: [realpathSync(candidate)] };
      }
    } catch {}
  }
  return npx;
}

export async function upsertJsonMcpServerConfig(requestedPath, serverEntry, options = {}) {
  // rename() would replace a symlink (e.g. into a dotfiles repo) with a plain
  // file, so write to the file the link points at.
  let configPath = requestedPath;
  try {
    configPath = await realpath(requestedPath);
  } catch {}
  let config = {};
  let content;
  let originalMode = 0o600;

  if (existsSync(configPath)) {
    try {
      const stats = await stat(configPath);
      originalMode = stats.mode & 0o777;
    } catch {}

    try {
      content = await readFile(configPath, "utf8");
    } catch (err) {
      throw new Error(`Failed to read configuration file: ${configPath}: ${err.message}`);
    }

    try {
      config = JSON.parse(content);
    } catch (err) {
      throw new Error(`Configuration file contains invalid JSON: ${configPath}: ${err.message}`);
    }

    if (!config || typeof config !== "object" || Array.isArray(config)) {
      throw new Error(`Configuration file must contain a JSON object: ${configPath}`);
    }

    const backupPath = `${configPath}.bak`;
    if (!existsSync(backupPath)) {
      try {
        await copyFile(configPath, backupPath);
      } catch (err) {
        throw new Error(`Failed to create backup at ${backupPath}: ${err.message}`);
      }
    }
  }

  if (config.mcpServers !== undefined && (typeof config.mcpServers !== "object" || config.mcpServers === null || Array.isArray(config.mcpServers))) {
    throw new Error(`mcpServers in ${configPath} must be an object`);
  }

  config.mcpServers = config.mcpServers || {};
  const previous = config.mcpServers.obsidian;
  const existed = previous !== undefined;
  if (existed && (!options.overwrite || JSON.stringify(previous) === JSON.stringify(serverEntry))) {
    return {
      success: true,
      configPath: requestedPath,
      existed: true,
      updated: false,
    };
  }

  // The .bak keeps the pre-tour original, so a later replacement of an entry
  // the user edited since then needs a copy of its own.
  let replacedBackupPath;
  if (existed) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    replacedBackupPath = `${configPath}.replaced-${stamp}.bak`;
    try {
      await writeFile(replacedBackupPath, content, { encoding: "utf8", mode: originalMode, flag: "wx" });
    } catch (err) {
      throw new Error(`Failed to back up the existing obsidian entry at ${replacedBackupPath}: ${err.message}`);
    }
  }

  config.mcpServers.obsidian = serverEntry;

  await mkdir(dirname(configPath), { recursive: true });
  const tmpFile = join(dirname(configPath), `.${basename(configPath)}.${Date.now()}.${Math.random().toString(36).slice(2)}.tmp`);
  try {
    await writeFile(tmpFile, `${JSON.stringify(config, null, 2)}\n`, {
      encoding: "utf8",
      mode: originalMode,
    });
    await rename(tmpFile, configPath);
  } catch (err) {
    try {
      await rm(tmpFile, { force: true });
    } catch {}
    throw err;
  }

  return {
    success: true,
    configPath: requestedPath,
    existed,
    updated: true,
    replacedBackupPath,
  };
}

export function getClaudeDesktopConfigPath() {
  if (process.platform === "darwin") {
    return join(homedir(), "Library", "Application Support", "Claude", "claude_desktop_config.json");
  }
  if (process.platform === "win32") {
    return process.env.APPDATA
      ? join(process.env.APPDATA, "Claude", "claude_desktop_config.json")
      : join(homedir(), "AppData", "Roaming", "Claude", "claude_desktop_config.json");
  }
  return process.env.XDG_CONFIG_HOME
    ? join(process.env.XDG_CONFIG_HOME, "Claude", "claude_desktop_config.json")
    : join(homedir(), ".config", "Claude", "claude_desktop_config.json");
}

function serverArgs(launcher, vaultPath, dailyPath, write) {
  return [...launcher.argsPrefix, "--vault", vaultPath, "--daily-path", dailyPath, write ? "--write" : "--read"];
}

export async function removeClaudeCodeViaCli({ spawn = spawnSync } = {}) {
  const res = spawn("claude", ["mcp", "remove", "obsidian", "-s", "user"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 10_000,
  });
  if (res.status === 0) return { success: true };
  return { success: false, error: (res.stderr || res.stdout || res.error?.message || `exit code ${res.status}`).trim() };
}

// Throws unless the saved copy holds the entry that is about to be removed.
async function backupBeforeReplace(configPath) {
  if (!JSON.parse(await readFile(configPath, "utf8"))?.mcpServers?.obsidian) {
    throw new Error(`${configPath} has no "obsidian" entry`);
  }
  const backupPath = `${configPath}.replaced-${new Date().toISOString().replace(/[:.]/g, "-")}.bak`;
  await copyFile(configPath, backupPath, constants.COPYFILE_EXCL);
  return backupPath;
}

// Entries the tour does not manage can still decide whether a session can write.
export function otherEntryWarnings(configPath, write) {
  let config;
  try {
    config = JSON.parse(readFileSync(configPath, "utf8"));
  } catch (err) {
    return err.code === "ENOENT" ? [] : [`Could not check ${configPath} for other Obsidian entries: ${err.message}`];
  }
  const warnings = [];
  for (const [project, settings] of Object.entries(config?.projects ?? {})) {
    if (settings?.mcpServers?.obsidian) {
      warnings.push(`${project} has its own "obsidian" entry, which wins there. To remove it, run \`claude mcp remove obsidian -s local\` in that folder.`);
    }
  }
  for (const [project, settings] of Object.entries(config?.projects ?? {})) {
    if (!write && settings?.mcpServers?.["obsidian-write"]) {
      warnings.push(`${project} has an "obsidian-write" entry that can still write to your vault. Remove it if you want writing off.`);
    }
  }
  if (!write && config?.mcpServers?.["obsidian-write"]) {
    warnings.push(`A separate "obsidian-write" entry in ${configPath} can still write to your vault. Remove it if you want writing off.`);
  }
  return warnings;
}

export async function configureClaudeDesktop(vaultPath, dailyPath = "Daily/", configPath = getClaudeDesktopConfigPath(), options = {}) {
  const launcher = resolveServerLauncher();
  const serverEntry = {
    command: launcher.command,
    args: serverArgs(launcher, vaultPath, dailyPath, options.write),
  };
  return upsertJsonMcpServerConfig(configPath, serverEntry, options);
}

export function detectClaudeCode({ platform = process.platform, spawn = spawnSync } = {}) {
  // npm installs claude.cmd on Windows, which spawnSync can only reach through
  // cmd.exe, and cmd.exe re-splits unquoted paths such as C:\Program Files.
  // The file writer is the safe path there.
  if (platform === "win32") return false;
  try {
    const res = spawn("claude", ["--version"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 2000,
    });
    return res.status === 0;
  } catch {
    return false;
  }
}

export function getClaudeCodeConfigPath() {
  return join(process.env.CLAUDE_CONFIG_DIR || homedir(), ".claude.json");
}

export async function configureClaudeCodeViaCli(vaultPath, dailyPath = "Daily/", { spawn = spawnSync, write = false } = {}) {
  const launcher = resolveServerLauncher();
  const args = [
    "mcp",
    "add",
    "-s",
    "user",
    "obsidian",
    "--",
    launcher.command,
    ...serverArgs(launcher, vaultPath, dailyPath, write),
  ];
  let res;
  try {
    res = spawn("claude", args, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 10_000,
    });
  } catch (err) {
    return { success: false, method: "cli", error: err.message, spawnFailure: true };
  }
  if (res.status === 0) {
    return { success: true, method: "cli" };
  }
  const errorMsg = (res.stderr || res.stdout || res.error?.message || `exit code ${res.status}`).trim();
  return { success: false, method: "cli", error: errorMsg, spawnFailure: res.error?.code === "ENOENT" };
}

export async function configureClaudeCodeViaFile(vaultPath, dailyPath = "Daily/", configPath = getClaudeCodeConfigPath(), options = {}) {
  const launcher = resolveServerLauncher();
  const serverEntry = {
    command: launcher.command,
    args: serverArgs(launcher, vaultPath, dailyPath, options.write),
  };
  const result = await upsertJsonMcpServerConfig(configPath, serverEntry, options);
  return { ...result, method: "file" };
}

function cancelledError() {
  return new Error("setup cancelled: input closed before the tour finished");
}

export async function runTour(options = {}) {
  const input = options.input || process.stdin;
  const output = options.output || process.stdout;
  const version = options.version || packageVersion;
  const rl = readline.createInterface({ input, output });
  // readline/promises leaves a pending question unsettled when input closes,
  // so without this the process drains its event loop and exits 0.
  const closed = new AbortController();
  rl.once("close", () => closed.abort());
  const ask = async (query) => {
    if (closed.signal.aborted) throw cancelledError();
    try {
      return await rl.question(query, { signal: closed.signal });
    } catch (err) {
      if (closed.signal.aborted) throw cancelledError();
      throw err;
    }
  };

  const print = (msg = "") => output.write(`${msg}\n`);

  try {
    print();
    print("  ___  _         _     _ _              __  __  ____ ____  ");
    print(" / _ \\| |__  ___(_) __| (_) __ _ _ __  |  \\/  |/ ___|  _ \\ ");
    print("| | | | '_ \\/ __| |/ _` | |/ _` | '_ \\ | |\\/| | |   | |_) |");
    print("| |_| | |_) \\__ \\ | (_| | | (_| | | | || |  | | |___|  __/ ");
    print(" \\___/|_.__/|___/_|\\__,_|_|\\__,_|_| |_||_|  |_|\\____|_|    ");
    print();
    print(`Obsidian MCP Server Setup (v${version})`);
    print("Connect your Obsidian vault to Claude Code, Claude Desktop, and AI agents.");
    print();

    const home = options.homeDir || homedir();
    let selectedVault = "";
    if (options.initialVault) {
      try {
        const initial = resolve(options.initialVault);
        if (statSync(initial).isDirectory()) selectedVault = realpathSync(initial);
      } catch {}
      if (!selectedVault) print(`Provided initial vault is invalid: ${options.initialVault}`);
    }

    if (!selectedVault) {
      const knownVaults = options.knownVaults || findKnownVaults(options.obsidianConfigPath, print);
      if (knownVaults.length > 0) {
        print("Found Obsidian vault(s) on this machine:");
        knownVaults.forEach((v, idx) => {
          const marker = v.open ? " (currently active)" : "";
          print(`  [${idx + 1}] ${v.path}${marker}`);
        });
        print(`  [${knownVaults.length + 1}] Specify a custom vault directory`);
        print();

        while (!selectedVault) {
          const answer = (await ask(`Select a vault [1-${knownVaults.length + 1}] (default: 1): `)).trim();
          const choice = answer === "" ? 1 : parseInt(answer, 10);
          if (!isNaN(choice) && choice >= 1 && choice <= knownVaults.length) {
            selectedVault = knownVaults[choice - 1].path;
          } else if (choice === knownVaults.length + 1) {
            break;
          } else {
            print(`Invalid choice. Enter a number between 1 and ${knownVaults.length + 1}.`);
          }
        }
      }
    }

    while (!selectedVault) {
      const suggestion = suggestVaultPath(home, options.cwd || process.cwd());
      const note = suggestion.exists ? "" : ", will be created";
      const typed = (await ask(`Vault directory (default: ${suggestion.path}${note}): `)).trim();
      if (!typed && !suggestion.exists) {
        try {
          await mkdir(suggestion.path, { recursive: true });
        } catch (err) {
          throw new Error(`Could not create ${suggestion.path}: ${err.message}`);
        }
      }
      const customPath = typed || suggestion.path;
      const expanded = customPath === "~" || customPath.startsWith("~/") ? join(home, customPath.slice(1)) : customPath;
      if (!isAbsolute(expanded)) {
        print(`Vault path must be absolute (or start with ~/): ${customPath}`);
        continue;
      }
      const resolved = resolve(expanded);
      if (!existsSync(resolved)) {
        print(`Directory does not exist: ${customPath}`);
        continue;
      }
      try {
        if (!statSync(resolved).isDirectory()) {
          print(`Path is not a directory: ${customPath}`);
          continue;
        }
        selectedVault = realpathSync(resolved);
      } catch (err) {
        print(`Unable to access path: ${err.message}`);
      }
    }

    print(`Selected vault: ${selectedVault}`);
    print();

    let selectedDaily = "";
    const detectedDaily = options.initialDailyPath
      ? options.initialDailyPath
      : detectDailyNotesPath(selectedVault);

    while (!selectedDaily) {
      const dailyPrompt = (await ask(`Daily notes directory (default: ${detectedDaily || "Daily/"}): `)).trim();
      const candidate = dailyPrompt || detectedDaily || "Daily/";
      try {
        selectedDaily = validateDailyPath(candidate, selectedVault);
      } catch (err) {
        print(`Invalid daily notes path: ${err.message}`);
      }
    }

    print(`Daily notes path: ${selectedDaily}`);
    print();

    let write;
    while (write === undefined) {
      const reply = (await ask("Let your AI app save notes and add to your daily note? [Y/n]: ")).trim().toLowerCase();
      if (["", "y", "yes"].includes(reply)) write = true;
      else if (["n", "no"].includes(reply)) write = false;
      else print("Answer y or n.");
    }
    print(write ? "Writing is on." : 'Writing is off. To turn it on later, run the tour again or change "--read" to "--write" in the server arguments.');
    print();

    print("AI Host Configuration:");
    print("  [1] Configure Claude Code");
    print("  [2] Configure Claude Desktop");
    print("  [3] Configure both Claude Code and Claude Desktop");
    print("  [4] Skip automatic configuration (display snippet only)");
    print();

    let hostChoice = 0;
    while (!hostChoice) {
      const choiceStr = (await ask("Select an option [1-4] (default: 1): ")).trim();
      const choice = choiceStr === "" ? 1 : parseInt(choiceStr, 10);
      if (choice >= 1 && choice <= 4) {
        hostChoice = choice;
      } else {
        print("Invalid choice. Enter a number between 1 and 4.");
      }
    }

    print();
    const hasClaudeCli = options.detectClaudeCode ? options.detectClaudeCode() : detectClaudeCode();
    const claudeCodeConfigPath = options.claudeCodeConfigPath || getClaudeCodeConfigPath();
    const claudeDesktopConfigPath = options.claudeDesktopConfigPath || getClaudeDesktopConfigPath();
    const results = [];

    if (hostChoice === 1 || hostChoice === 3) {
      let codeConfigured = false;
      if (hasClaudeCli && !options.forceFileConfig) {
        print("Configuring Claude Code via `claude mcp add`...");
        const addViaCli = () => (options.configureClaudeCodeViaCli || configureClaudeCodeViaCli)(selectedVault, selectedDaily, { write });
        let cliRes = await addViaCli();
        if (!cliRes.success && /already exists/i.test(cliRes.error)) {
          const reply = (await ask('Replace the existing "obsidian" entry? [Y/n]: ')).trim().toLowerCase();
          if (["", "y", "yes"].includes(reply)) {
            let backupPath;
            try {
              backupPath = await backupBeforeReplace(claudeCodeConfigPath);
            } catch (err) {
              print(`Kept the old entry, because it could not be backed up: ${err.message}`);
            }
            if (backupPath) {
              print(`  The previous config is saved at ${backupPath}`);
              const removed = await (options.removeClaudeCodeViaCli || removeClaudeCodeViaCli)();
              if (removed.success) {
                cliRes = await addViaCli();
                if (!cliRes.success) {
                  print(`The old "obsidian" entry was removed and the new one was not added. To restore it, copy mcpServers.obsidian from ${backupPath} into ${claudeCodeConfigPath}.`);
                }
              } else {
                print(`Could not remove the old entry: ${removed.error}`);
              }
            }
          }
        }
        if (cliRes.success) {
          print("✓ Claude Code configured successfully via CLI!");
          codeConfigured = true;
          results.push({ host: "Claude Code", success: true, method: "cli" });
        } else if (cliRes.spawnFailure) {
          print(`CLI add unavailable (${cliRes.error}). Trying direct config write...`);
        } else {
          print(`CLI add reported: ${cliRes.error}.`);
          if (/already exists/i.test(cliRes.error)) print("To replace it yourself, run `claude mcp remove obsidian -s user`, then run this tour again.");
          results.push({ host: "Claude Code", success: false, method: "cli", error: cliRes.error });
          codeConfigured = true;
        }
      }

      if (!codeConfigured) {
        try {
          print(`Configuring Claude Code in ${claudeCodeConfigPath}...`);
          const fileRes = await configureClaudeCodeViaFile(selectedVault, selectedDaily, claudeCodeConfigPath, { overwrite: true, write });
          print(fileRes.updated ? `✓ Updated ${fileRes.configPath}` : `✓ Already configured in ${fileRes.configPath}`);
          if (fileRes.replacedBackupPath) print(`  Replaced an existing "obsidian" entry; the previous config is saved at ${fileRes.replacedBackupPath}`);
          results.push({ host: "Claude Code", success: true, method: "file" });
        } catch (err) {
          print(`❌ Failed to configure Claude Code: ${err.message}`);
          results.push({ host: "Claude Code", success: false, error: err.message });
        }
      }
    }

    if (hostChoice === 2 || hostChoice === 3) {
      try {
        print(`Configuring Claude Desktop in ${claudeDesktopConfigPath}...`);
        const desktopRes = await configureClaudeDesktop(selectedVault, selectedDaily, claudeDesktopConfigPath, { overwrite: true, write });
        print(desktopRes.updated ? `✓ Updated ${desktopRes.configPath}` : `✓ Already configured in ${desktopRes.configPath}`);
        if (desktopRes.replacedBackupPath) print(`  Replaced an existing "obsidian" entry; the previous config is saved at ${desktopRes.replacedBackupPath}`);
        results.push({ host: "Claude Desktop", success: true });
      } catch (err) {
        print(`❌ Failed to configure Claude Desktop: ${err.message}`);
        results.push({ host: "Claude Desktop", success: false, error: err.message });
      }
    }

    const checkedPaths = [
      ...(hostChoice === 1 || hostChoice === 3 ? [claudeCodeConfigPath] : []),
      ...(hostChoice === 2 || hostChoice === 3 ? [claudeDesktopConfigPath] : []),
    ];
    for (const configPath of checkedPaths) {
      for (const warning of otherEntryWarnings(configPath, write)) print(`⚠️ ${warning}`);
    }

    if (hostChoice === 4) {
      const launcher = resolveServerLauncher();
      print("Configuration snippet for Claude Desktop or Claude Code:");
      print(JSON.stringify({
        mcpServers: {
          obsidian: {
            command: launcher.command,
            args: serverArgs(launcher, selectedVault, selectedDaily, write),
          },
        },
      }, null, 2));
    }

    print();
    print("Verifying vault notes...");
    try {
      const { count, hasMore } = await countVaultNotes(selectedVault);
      if (count === 0) {
        print(`⚠️ Warning: No Markdown (.md) notes found in ${selectedVault}.`);
      } else {
        print(`✓ Found ${count}${hasMore ? "+" : ""} Markdown note(s) in vault.`);
      }
    } catch (err) {
      print(`⚠️ Vault verification warning: ${err.message}`);
    }
    print();

    const failed = results.filter((r) => !r.success);
    if (failed.length > 0) {
      for (const r of results) {
        print(`  ${r.success ? "✓" : "❌"} ${r.host}${r.success ? "" : `: ${r.error}`}`);
      }
      print(`❌ Setup incomplete: ${failed.length} of ${results.length} requested host configuration(s) failed.`);
      print();
      const err = new Error(`${failed.length} of ${results.length} requested host configuration(s) failed`);
      err.results = results;
      throw err;
    }

    print("Setup complete!");
    print();
    const configured = (host) => results.some((r) => r.host === host);
    if (configured("Claude Desktop")) {
      print("Next: Quit Claude Desktop and open it again.");
      if (!configured("Claude Code")) {
        print("The `claude` command line is set up separately. To add it, run this setup again and choose 1.");
      }
      print();
    }
    if (configured("Claude Code")) {
      print("Next: start `claude` and ask:");
      print('  "Search my Obsidian notes for project roadmap"');
      print('  "What did I write in my daily note today?"');
      if (write) print('  "Add a line to my daily note saying setup works"');
      print();
    }
    print(results.length > 0
      ? "There is nothing to start yourself. Your AI app runs the server when it needs it."
      : "Next: Add the snippet above to your app's settings, then restart the app.");
    print();

    return {
      vault: selectedVault,
      dailyPath: selectedDaily,
      write,
      hostChoice,
      results,
    };
  } finally {
    rl.close();
  }
}
