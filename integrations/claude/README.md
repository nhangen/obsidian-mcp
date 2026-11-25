# Obsidian MCP for Claude Code

Connect Claude Code to Obsidian MCP over stdio or authenticated Streamable HTTP without the Claude plugin. Stdio runs on the machine that holds the vault. Streamable HTTP connects to a remote server, so the client machine needs no vault.

## Prerequisites

- **Claude Code CLI**: A build supporting MCP; verify it against your installed version.
- **Node.js**: 22 or newer for Streamable HTTP read bridge / launchers; Node 20+ for stdio.
- **Python**: 3.11+ for the portable client bundle only. The npm stdio server doesn't need it.

## Connection Setup

### 1. Stdio (Local Server)

When the vault is on the same machine as Claude Code, run the setup tour from the
npm package:

```bash
npx -y @nhangen/obsidian-mcp
```

To configure it by hand, add one `obsidian` entry. Use `--write` to allow saving,
or `--read` to allow search only:

```bash
claude mcp add -s user obsidian -- npx -y @nhangen/obsidian-mcp --vault /absolute/path/to/vault --daily-path Daily/ --write
```

You don't need a separate `obsidian-write` entry for stdio. If an earlier setup
left one, it can still write even when `obsidian` uses `--read`, so remove it if
you want writing off. See the
[package README](../../README.md#install-from-npm) for updating and uninstalling.

### 2. Streamable HTTP (Remote Server)

For remote server deployments (e.g., dedicated server, Tailscale tailnet), install the client bundle with `python3 bin/obsidian-mcp-client-install` and configure the launchers:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "/prefix/current/bin/obsidian-mcp-read",
      "env": {
        "OBSIDIAN_MCP_READ_ENDPOINT": "https://mcp.internal.example/mcp",
        "OBSIDIAN_MCP_READ_CREDENTIAL_PROVIDER": "env",
        "OBSIDIAN_MCP_READ_TOKEN_ENV": "MY_OBSIDIAN_READ_TOKEN",
        "MY_OBSIDIAN_READ_TOKEN": "JWT_TOKEN_WITH_VAULT_READ"
      }
    },
    "obsidian-write": {
      "command": "/prefix/current/bin/obsidian-mcp-write",
      "env": {
        "OBSIDIAN_MCP_WRITE_ENDPOINT": "https://mcp.internal.example/mcp",
        "OBSIDIAN_MCP_WRITE_CREDENTIAL_PROVIDER": "env",
        "OBSIDIAN_MCP_WRITE_TOKEN_ENV": "MY_OBSIDIAN_WRITE_TOKEN",
        "MY_OBSIDIAN_WRITE_TOKEN": "JWT_TOKEN_WITH_VAULT_WRITE"
      }
    }
  }
}
```

`OBSIDIAN_MCP_<PROFILE>_CREDENTIAL_PROVIDER` accepts `env`, `file`, or `keychain` (macOS). The launchers pass tokens to the child bridge without creating credential temporary files or placing tokens in command arguments. Protect any configuration or credential file containing a token.

## Scoped Host Authorization

Automatic MCP writes require scoped host policy rather than repetitive manual confirmation. In `~/.claude/settings.json` or project configuration:

```json
{
  "permissions": {
    "allow": [
      "mcp__obsidian-write__*"
    ]
  }
}
```

This rule permits `obsidian_keeper_save` and `obsidian_daily_append` under the authenticated write profile. Requests outside the MCP contract remain rejected. With the single stdio entry, allow `mcp__obsidian__obsidian_keeper_save` and `mcp__obsidian__obsidian_daily_append` instead.

## Workflows & Capabilities

| Workflow | Tool / Resource | Access | Description |
| --- | --- | --- | --- |
| Bounded read | `obsidian_find_notes` | Read | Scans vault notes for query; returns top 5 bounded snippets. |
| Resource read | `obsidian://*` | Read | Accesses published resources (`taxonomy`, `daily/{date}`, `librarian`, `pending`). |
| Commit metadata | `obsidian_commit_meta` | Read | Emits sanitized commit info for an allowlisted repository. |
| Guarded note save | `obsidian_keeper_save` | Write | Saves a structured note into an authorized taxonomy folder; requires `idempotency_key`. |
| Daily append | `obsidian_daily_append` | Write | Appends a section to the daily note or exact target note; supports `--skip-if-hash`. |
| Librarian prompt | `ask_vault_librarian` | Read | Generates read-only guidance from vault indexes without modifying files. |
| Session summary | `summarize_session` | Read | Generates client-side summary frontmatter; does not write to vault. |

## Automatic Capture vs. Standalone Usage

- The **Claude plugin** is one optional integration providing automatic session capture and git post-commit hooks within Claude Code sessions.
- In standalone client mode, Claude Code uses the MCP tools listed above directly or through the provided `integrations/claude/skills/obsidian-mcp/SKILL.md` skill.
- Automatic hook-based capture requires either the Claude plugin hooks or an explicit host execution event; silent fallback or guessed git histories are forbidden.

## Explicit non-Git session capture

The optional plugin can attach a canonical directory using the public native
runtime's `attach --host claude-plugin --project-mode directory` operation.
First verify the installed release and require `apiVersion: 1`, `claude-plugin`
in `hosts`, and `directory` in `projectModes["claude-plugin"]` from `capabilities`.
See the [attached-profile contract](../../packages/client/README.md#explicit-directory-session-profiles)
for the complete command and immutable release requirements.

Directory profiles save sessions without probing Git or changing workspace files.
Only the registered root and its contained working directories are eligible;
sibling paths and escaping symlinks fail closed. A nested Git repository remains
session-only under that directory profile. Shell commands run normally under host
permissions, without automatic commit capture, metadata, snapshots, or rewrites;
directory mode is not a shell sandbox. Explicit native commit capture operations
and commit-targeted MCP appends refuse before creating capture state. A separately
attached, more specific Git profile is required for commit capture. Git is the default mode,
including for older descriptors without `projectMode`; malformed modes refuse.

The plugin must reject duplicate canonical registrations, select the most specific
explicit root, and attach new or unmapped workspaces deliberately. Install/verify
the supporting release and attach a fresh pinned profile before the separate global
writer handoff. No operation here authorizes changing credentials, initializing a
repository, or falling back to a local vault writer. Session receipt, idempotency,
recovery, and scoped MCP authorization remain unchanged. This does not add native
non-Git Codex coverage or complete whole-host acceptance. Public issues
[#8](https://github.com/nhangen/obsidian-mcp/issues/8),
[#2](https://github.com/nhangen/obsidian-mcp/issues/2), and
[plugin #254](https://github.com/nhangen/claude-obsidian-plugin/issues/254)
retain their release and installed-host acceptance gates.

## Diagnostics & Limits

- **Errors**: Returned with stable structured error codes (`INVALID_INPUT`, `READ_FAILED`, `CONFLICT`, `PARTIAL`, `WRITE_FAILED`, `SUBPROCESS_TIMEOUT`).
- **Connection vs. Operation**: Server connection alone is not proof of write success. Inspect structured MCP receipts (`status: "committed"` or `status: "skipped"`).
- **Timeouts**: Keeper save defaults to 25000ms, daily append to 10000ms and commit metadata to 5000ms. HTTP requests default to 30000ms.
