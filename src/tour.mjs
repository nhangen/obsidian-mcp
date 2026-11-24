import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { copyFile, mkdir, readdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
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
  for (const candidate of [join(packageDir, "dist", "stdio.mjs"), join(packageDir, "src", "stdio.mjs")]) {
    try {
      if (existsSync(candidate)) {
        return { command: process.execPath, argsPrefix: [realpathSync(candidate)] };
      }
    } catch {}
  }
  return npx;
}

export async function upsertJsonMcpServerConfig(configPath, serverEntry, options = {}) {
  let config = {};
  let originalMode = 0o600;

  if (existsSync(configPath)) {
    try {
      const stats = await stat(configPath);
      originalMode = stats.mode & 0o777;
    } catch {}

    let content;
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
  if (config.mcpServers.obsidian && !options.overwrite) {
    return {
      success: true,
      configPath,
      existed: true,
      updated: false,
    };
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
    configPath,
    existed: false,
    updated: true,
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

export async function configureClaudeDesktop(vaultPath, dailyPath = "Daily/", configPath = getClaudeDesktopConfigPath(), options = {}) {
  const launcher = resolveServerLauncher();
  const serverEntry = {
    command: launcher.command,
    args: [
      ...launcher.argsPrefix,
      "--vault",
      vaultPath,
      "--daily-path",
      dailyPath,
    ],
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
  return join(homedir(), ".claude.json");
}

export async function configureClaudeCodeViaCli(vaultPath, dailyPath = "Daily/", { spawn = spawnSync } = {}) {
  const launcher = resolveServerLauncher();
  const args = [
    "mcp",
    "add",
    "-s",
    "user",
    "obsidian",
    "--",
    launcher.command,
    ...launcher.argsPrefix,
    "--vault",
    vaultPath,
    "--daily-path",
    dailyPath,
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
    args: [
      ...launcher.argsPrefix,
      "--vault",
      vaultPath,
      "--daily-path",
      dailyPath,
    ],
  };
  const result = await upsertJsonMcpServerConfig(configPath, serverEntry, options);
  return { ...result, method: "file" };
}

export async function runTour(options = {}) {
  const input = options.input || process.stdin;
  const output = options.output || process.stdout;
  const version = options.version || packageVersion;
  const rl = readline.createInterface({ input, output });

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

    let selectedVault = options.initialVault ? resolve(options.initialVault) : "";
    if (selectedVault && (!existsSync(selectedVault) || !statSync(selectedVault).isDirectory())) {
      print(`Provided initial vault is invalid: ${options.initialVault}`);
      selectedVault = "";
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
          const answer = (await rl.question(`Select a vault [1-${knownVaults.length + 1}] (default: 1): `)).trim();
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
      const customPath = (await rl.question("Enter the absolute path to your Obsidian vault directory: ")).trim();
      if (!customPath) continue;
      const resolved = resolve(customPath);
      if (!isAbsolute(resolved) || !existsSync(resolved)) {
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
      const dailyPrompt = (await rl.question(`Daily notes directory (default: ${detectedDaily || "Daily/"}): `)).trim();
      const candidate = dailyPrompt || detectedDaily || "Daily/";
      try {
        selectedDaily = validateDailyPath(candidate, selectedVault);
      } catch (err) {
        print(`Invalid daily notes path: ${err.message}`);
      }
    }

    print(`Daily notes path: ${selectedDaily}`);
    print();

    print("AI Host Configuration:");
    print("  [1] Configure Claude Code");
    print("  [2] Configure Claude Desktop");
    print("  [3] Configure both Claude Code and Claude Desktop");
    print("  [4] Skip automatic configuration (display snippet only)");
    print();

    let hostChoice = 0;
    while (!hostChoice) {
      const choiceStr = (await rl.question("Select an option [1-4] (default: 1): ")).trim();
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
        const cliRes = options.configureClaudeCodeViaCli
          ? await options.configureClaudeCodeViaCli(selectedVault, selectedDaily)
          : await configureClaudeCodeViaCli(selectedVault, selectedDaily);
        if (cliRes.success) {
          print("✓ Claude Code configured successfully via CLI!");
          codeConfigured = true;
          results.push({ host: "Claude Code", success: true, method: "cli" });
        } else if (cliRes.spawnFailure) {
          print(`CLI add unavailable (${cliRes.error}). Trying direct config write...`);
        } else {
          print(`CLI add reported: ${cliRes.error}.`);
          results.push({ host: "Claude Code", success: false, method: "cli", error: cliRes.error });
          codeConfigured = true;
        }
      }

      if (!codeConfigured) {
        try {
          print(`Configuring Claude Code in ${claudeCodeConfigPath}...`);
          const fileRes = await configureClaudeCodeViaFile(selectedVault, selectedDaily, claudeCodeConfigPath, { overwrite: true });
          print(`✓ Updated ${fileRes.configPath}`);
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
        const desktopRes = await configureClaudeDesktop(selectedVault, selectedDaily, claudeDesktopConfigPath, { overwrite: true });
        print(`✓ Updated ${desktopRes.configPath}`);
        results.push({ host: "Claude Desktop", success: true });
      } catch (err) {
        print(`❌ Failed to configure Claude Desktop: ${err.message}`);
        results.push({ host: "Claude Desktop", success: false, error: err.message });
      }
    }

    if (hostChoice === 4) {
      const launcher = resolveServerLauncher();
      print("Configuration snippet for Claude Desktop or Claude Code:");
      print(JSON.stringify({
        mcpServers: {
          obsidian: {
            command: launcher.command,
            args: [...launcher.argsPrefix, "--vault", selectedVault, "--daily-path", selectedDaily],
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

    if (results.length > 0 && results.every((r) => !r.success)) {
      print("❌ Setup incomplete: all requested host configurations failed.");
      print();
      const err = new Error("all requested host configurations failed");
      err.results = results;
      throw err;
    }

    print("Setup complete!");
    print();
    print("To test with Claude Code, launch `claude` and ask:");
    print('  "Search my Obsidian notes for project roadmap"');
    print('  "What did I write in my daily note today?"');
    print();
    const runLauncher = resolveServerLauncher();
    const runCommand = [runLauncher.command, ...runLauncher.argsPrefix, "--vault", selectedVault, "--daily-path", selectedDaily]
      .map((part) => (/\s/.test(part) ? `"${part}"` : part))
      .join(" ");
    print("To run the MCP server directly in stdio mode:");
    print(`  ${runCommand}`);
    print();

    return {
      vault: selectedVault,
      dailyPath: selectedDaily,
      configuredHosts: hostChoice,
      results,
    };
  } finally {
    rl.close();
  }
}
