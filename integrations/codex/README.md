# Obsidian MCP for OpenAI Codex

Connect OpenAI Codex CLI to Obsidian MCP for vault search, resource browsing, metadata extraction, and guarded writes.

The configuration keys below follow the [official MCP configuration reference](https://learn.chatgpt.com/docs/extend/mcp).
The imported native integration is an opt-in candidate; fixture tests do not
establish hook delivery or automatic-capture acceptance on your installed build.

## Prerequisites

- **Codex CLI**: 0.163.0 or newer for native hook-based capture; 0.135.0 or newer for skill-driven capture.
- **Node.js**: 22 or newer for Streamable HTTP read bridge / launchers; Node 20+ for stdio.
- **Python**: 3.11 or newer for native capture execution and metadata parsing.

## Connection Setup (`config.toml`)

In `~/.codex/config.toml` (or your isolated profile's `CODEX_HOME/config.toml`):

```toml
[mcp_servers.obsidian]
command = "/prefix/current/bin/obsidian-mcp-read"
default_tools_approval_mode = "auto"

[mcp_servers.obsidian.env]
OBSIDIAN_MCP_READ_ENDPOINT = "https://mcp.internal.example/mcp"
OBSIDIAN_MCP_READ_CREDENTIAL_PROVIDER = "env"
OBSIDIAN_MCP_READ_TOKEN_ENV = "MY_OBSIDIAN_READ_TOKEN"
MY_OBSIDIAN_READ_TOKEN = "JWT_TOKEN_WITH_VAULT_READ"

[mcp_servers."obsidian-write"]
command = "/prefix/current/bin/obsidian-mcp-write"
default_tools_approval_mode = "auto"

[mcp_servers."obsidian-write".env]
OBSIDIAN_MCP_WRITE_ENDPOINT = "https://mcp.internal.example/mcp"
OBSIDIAN_MCP_WRITE_CREDENTIAL_PROVIDER = "env"
OBSIDIAN_MCP_WRITE_TOKEN_ENV = "MY_OBSIDIAN_WRITE_TOKEN"
MY_OBSIDIAN_WRITE_TOKEN = "JWT_TOKEN_WITH_VAULT_WRITE"
```

## Scoped Host Authorization

Setting `default_tools_approval_mode = "auto"` inside `[mcp_servers."obsidian-write"]` authorizes the server's routine write tools (`obsidian_keeper_save` and `obsidian_daily_append`) without repetitive per-call confirmation prompts.

Other servers and unconfigured write tools retain standard approval prompts.

## Integration Modes

### 1. Isolated Native Capture (`obsidian-mcp-native`)

For automated session and commit capture without relying on the Claude plugin:

1. Create a fresh, disposable profile with the client installer's native tool:
   ```bash
   python3 /prefix/current/bin/obsidian-mcp-native create \
     --home ~/.codex/profiles/obsidian-capture \
     --project /path/to/clean/project \
     --folder "Vault/Folder/Hint" \
     --release-sha256 <bundle-release-sha256>
   ```
   Creation outputs a JSON result with `status: "created"` and `launch_command`:
   ```json
   {
     "status": "created",
     "home": "/path/to/.codex/profiles/obsidian-capture",
     "writer": "mcp",
     "scope": "isolated-profile",
     "launch_command": "python3 /prefix/current/bin/obsidian-mcp-native run --home /path/to/.codex/profiles/obsidian-capture",
     "release_id": "..."
   }
   ```
2. Launch Codex using the `launch_command` returned above (any additional options are forwarded directly to `codex`):
   ```bash
   python3 /prefix/current/bin/obsidian-mcp-native run --home ~/.codex/profiles/obsidian-capture
   ```
3. **Execution Flow**:
   - `UserPromptSubmit` supplies a turn-specific session save identity.
   - `PreToolUse` intercepts single `git commit` commands and rewrites them through a local bundled executor.
   - The executor executes git in an isolated subprocess, records the actual return code and reflog transition.
   - `PostToolUse` verifies the execution receipt and outputs a pending commit ID.
   - The agent prepares context and calls `obsidian_daily_append` via MCP.
   - Only a matching `status: "committed"` or `status: "skipped"` receipt confirms capture.
4. **Failure & Concurrency Handling**:
   - Replay skips duplicate appends idempotently.
   - Changed context conflicts locally and blocks automatic re-writes.
   - A pending event locks commit capture for that session; other sessions in the profile are denied until resolved.
   - Bounded recovery: `obsidian-mcp-native takeover` safely releases an interrupted owner's lock without reconstructing git history.

### 2. Attached user-wide runtime state

An integrating plugin can use `attach --host codex --scope user` to route new
workspaces through one private runtime profile. Git roots and subdirectories
support commit capture; non-Git workspaces are session-only. It writes neither
normal Codex configuration nor workspace instructions. Require the runtime's
`captureScopes["codex"]` and `attachedCaptureScopes["codex"]` capabilities to
include `"user"` before using it. Directory support is reported separately in
`userWorkspaceModes["codex"]`; isolated creation remains Git-only.

See the [attached API](../../packages/client/README.md#attached-user-wide-codex-capture-api)
for creation and lifecycle boundaries. This API does not install hooks, approve
them, or activate capture. Plugin-managed normal-home activation, duplicate-safe
managed mode, and the administrator installer remain separate work. Do not use
the isolated `run` or `disable` commands on attached state.

### 3. Skill-Driven Capture (`obsidian-mcp` skill)

If running Codex in an existing profile without native hooks:
- Register the servers as shown above.
- Add `integrations/codex/skills/obsidian-mcp/SKILL.md` to your skill path.
- The model invokes `obsidian_find_notes`, `obsidian_commit_meta`, and opt-in write tools on explicit user request.
- No hooks or background watchers run; writes are explicitly agent-invoked.

## Administrator-managed deployment preview

Codex [managed hooks](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)
are trusted by administrator policy, including local Unix system requirements
at `/etc/codex/requirements.toml`; MDM is not the only delivery route. This moves
authorization to the administrator. Managed handlers cannot be disabled in the
user hook browser. The administrator must protect the program, interpreter,
dependencies, and distribution paths, not only the policy file.

`bin/obsidian-mcp-managed-export` prepares an offline proposal from a trusted
packed runtime, an explicit deployment plan, and optional snapshots of existing
policy and local hooks. Its default is a non-mutating preview. `--output` writes
only to a new export directory, never to the proposed system destinations.

The output is **not activation-ready**. It does not provide a managed dispatcher
implementation, merge or install policy, elevate privileges, copy credentials,
change hook trust, or migrate a writer. A policy fragment is not a complete
replacement for an existing requirements file and must not be blindly appended
to it. Existing policy bytes remain a separate snapshot for administrator review.

Before a managed deployment can serve normal users, finish these gates:

- Normal-home, user-wide Codex capture and automatic workspace routing through
  the plugin, rather than manual enrollment of each workspace.
- An explicit managed activation mode that suppresses only the corresponding
  owned local handlers, preserving unrelated hooks and retained pending work.
- Protected executable code, interpreter, dependencies, and pointers verified
  on the target host, with a dispatcher that ignores other accounts and scopes.
- Reviewed policy merging, scoped install, upgrade, uninstall, rollback, and
  actual installed-host verification.

Do not set `allow_managed_hooks_only` or force hooks on globally just to install
Obsidian. Those controls affect other integrations. Conflicting managed
directories, ownership, or policy require resolution before deployment.

For ordinary user-local hooks, Codex still requires trust. In installed CLI
0.166.0, `/hooks` opens the hook browser, but `/hooks approve <id>` is treated as
ordinary model input. The startup review offers **Trust all and continue**;
that includes unrelated pending hooks, not only Obsidian. Do not automate that
choice or present it as a scoped Obsidian approval command.

This preview does not install into the current host. Local command-hook support
also does not establish cloud-orchestrated capture support.

## Diagnostics & Troubleshooting

- Inspect status and pending events without vault access:
   ```bash
   /prefix/current/bin/obsidian-mcp-native status --home <profile> --session <key>
   ```
- Inspect original request payload for retry:
   ```bash
   /prefix/current/bin/obsidian-mcp-native request --home <profile> --session <key>
   ```
- Cleanly disable an isolated native profile only:
  ```bash
  /prefix/current/bin/obsidian-mcp-native disable --home <profile>
  ```
  Disable removes owned isolated-profile config/hooks while retaining capture
  evidence. Attached state rejects this command; its integrating plugin owns
  admission and rollback.
