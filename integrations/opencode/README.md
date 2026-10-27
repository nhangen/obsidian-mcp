# Obsidian MCP for OpenCode

Connect OpenCode to Obsidian MCP for vault exploration, structured metadata inspection, and guarded writes.

## Prerequisites

- **OpenCode**: Any release supporting JSON-RPC Model Context Protocol (MCP) servers.
- **Node.js**: 22 or newer for Streamable HTTP read bridge / launchers; Node 20+ for stdio.
- **Python**: 3.11 or newer for this client bundle.

## Configuration (`opencode.json` / `opencode.jsonc`)

Add the `obsidian` and `obsidian-write` MCP servers to your OpenCode configuration:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "obsidian": {
      "type": "local",
      "command": ["/prefix/current/bin/obsidian-mcp-read"],
      "environment": {
        "OBSIDIAN_MCP_READ_ENDPOINT": "https://mcp.internal.example/mcp",
        "OBSIDIAN_MCP_READ_CREDENTIAL_PROVIDER": "env",
        "OBSIDIAN_MCP_READ_TOKEN_ENV": "MY_OBSIDIAN_READ_TOKEN",
        "MY_OBSIDIAN_READ_TOKEN": "JWT_TOKEN_WITH_VAULT_READ"
      }
    },
    "obsidian-write": {
      "type": "local",
      "command": ["/prefix/current/bin/obsidian-mcp-write"],
      "environment": {
        "OBSIDIAN_MCP_WRITE_ENDPOINT": "https://mcp.internal.example/mcp",
        "OBSIDIAN_MCP_WRITE_CREDENTIAL_PROVIDER": "env",
        "OBSIDIAN_MCP_WRITE_TOKEN_ENV": "MY_OBSIDIAN_WRITE_TOKEN",
        "MY_OBSIDIAN_WRITE_TOKEN": "JWT_TOKEN_WITH_VAULT_WRITE"
      }
    }
  }
}
```

## Scoped Host Authorization

Routine writes (`obsidian_keeper_save`, `obsidian_daily_append`) require host-level write policy to avoid constant interactive approval prompts. In `opencode.jsonc`:

```jsonc
{
  "permission": {
    "obsidian-write_*": "allow"
  }
}
```

This grants authorization specifically to the `obsidian-write` server's declared tools. The read server requires no special permissions beyond normal tool execution.

## Supported Workflows

- **Bounded Search**: Call `obsidian_find_notes` with `{ "query": "..." }` to find up to 5 matching note snippets.
- **Published Resources**: Read `obsidian://taxonomy`, `obsidian://daily/{date}`, `obsidian://librarian`, and `obsidian://pending`.
- **Commit Metadata**: Read sanitized git commit information via `obsidian_commit_meta`.
- **Guarded Note Writes**: Save notes with `obsidian_keeper_save` using an idempotency key.
- **Daily Append**: Append sections with `obsidian_daily_append`.
- **Prompts**: `ask_vault_librarian` and `summarize_session` provide read-only librarian guidance and session summary frontmatter.

The skill definition at `integrations/opencode/skills/obsidian-mcp/SKILL.md` can be placed in your OpenCode instructions or agent skills directory.

## Capture Status and Explicit Limits

> [!IMPORTANT]
> **This bundle has not established automatic hook-based capture acceptance for OpenCode.**
> No OpenCode automatic-capture integration is shipped or enabled by this client bundle. Do not infer support from the presence of MCP connections or from another host's hooks.
> Manual agent workflows (requesting the agent to search the vault, summarize notes, or save a note via MCP tools) are fully supported.
