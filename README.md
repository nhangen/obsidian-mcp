# Obsidian MCP

Self-hosted Obsidian access over stdio or authenticated Streamable HTTP. The
packaged deterministic keeper performs every vault mutation. Neither a Claude
plugin nor the private llm-tools harness is required. The host running the server
needs filesystem access to the vault; remote clients do not.

This initial extraction imports only the tracked `packages/mcp-server` tree from
[`nhangen/claude-obsidian-plugin` at `76df3151104e37f409589fcda7ff3da65cc8c0d8`](https://github.com/nhangen/claude-obsidian-plugin/tree/76df3151104e37f409589fcda7ff3da65cc8c0d8/packages/mcp-server),
with fresh repository history and the original MIT notice. It preserves the
legacy package identity, launcher names, and protocol contract. Version `0.2.1`
prepares the first standalone release without reusing the imported `0.2.0` version.
It does not publish a new release, replace an existing deployment, or migrate
the optional plugin/client integrations. That cutover remains separate work.

## Portable clients

The standalone server release is [mcp-v0.2.1](https://github.com/nhangen/obsidian-mcp/releases/tag/mcp-v0.2.1).
The public portable client source is in [`packages/client`](packages/client/README.md),
including the pinned bridge, lifecycle installer and optional isolated native
capture integration. Neither requires the private llm-tools harness or a plugin.

From a trusted checkout, with Python 3.11+ and Node.js 22+:

```bash
python3 scripts/pack-obsidian-mcp-client.py --output /absolute/new/client-bundle
python3 /absolute/new/client-bundle/bin/obsidian-mcp-client-install \
  --bundle /absolute/new/client-bundle --release-sha256 <trusted-printed-digest>
```

The packer requires an output outside the repository and prints the release
inventory digest. Use the installer and digest from the trusted checkout or CI
artifact, not from an unauthenticated bundle. Host setup guides are under
[`integrations/codex`](integrations/codex/README.md),
[`integrations/claude`](integrations/claude/README.md) and
[`integrations/opencode`](integrations/opencode/README.md). Setup is explicit;
installation does not change credentials, hooks, host settings or writer ownership.
Automatic-capture host acceptance and client-release publication remain tracked in
[issue #2](https://github.com/nhangen/obsidian-mcp/issues/2). Fixture tests are not
evidence that an installed host delivered its hooks or persisted a real session.

## Build and configure

Use Node.js 20 or newer (CI pins 24.9.0), npm, Bash, Git, and standard Unix
utilities including awk, sed, find, grep, and tar. Windows hosts need a compatible
Unix environment such as WSL. Run from this repository's root:

```bash
git clone https://github.com/nhangen/obsidian-mcp.git
cd obsidian-mcp
npm ci
npm run build
npm test
OBSIDIAN_LOCAL_MD=/absolute/path/to/obsidian.local.md npm start
```

Create the referenced config outside the checkout, replacing the vault path and
taxonomy with existing folders in your own vault:

```markdown
---
vault_path: /absolute/path/to/vault
daily_path: Daily/
---

## Project Taxonomy

| Domain | Vault path | Precedence | Notes |
|--------|------------|------------|-------|
| Development | Projects/Development/ | 10 | Code and engineering |
```

Keep this config private and set `OBSIDIAN_LOCAL_MD` explicitly on every server
host. Set `MCP_REPOSITORY_ROOTS` explicitly if repository metadata is needed;
do not rely on source-checkout defaults. No scheduler or automatic session saver
is installed by these commands.

The TypeScript build emits the runnable package into `dist/` and copies the
canonical deterministic config and commit-metadata helpers into that package
output. The package exposes `claude-obsidian-mcp` for stdio and
`claude-obsidian-mcp-http` for authenticated Streamable HTTP. Both commands
resolve their implementation from the installed package, independent of the
caller's working directory.

An installed package can be configured in a stdio MCP client with the stable
launcher and explicit configuration boundaries:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "claude-obsidian-mcp",
      "env": {
        "OBSIDIAN_LOCAL_MD": "/absolute/path/to/obsidian.local.md",
        "MCP_REPOSITORY_ROOTS": "/absolute/path/to/repository"
      }
    }
  }
}
```

The manifest examples use `/absolute/path/to/` as an explicit placeholder. Replace
that prefix with the absolute path to the installed package before importing the
manifest; clients must not expand shell variables or rely on an inherited `PATH`.

The client invokes the package command rather than a file inside the package;
configuration and repository authorization remain explicit environment values.
Installed packages authorize no repository roots when `MCP_REPOSITORY_ROOTS`
is omitted. Multiple roots use the platform path delimiter.

The stdio server defaults to the read profile, exposing only vault and
repository read tools. Set `MCP_STDIO_PROFILE=write` explicitly as the
host-level policy authorization for trusted routine Obsidian writes. The write
profile provides the write tools. The supported client policy requires an
idempotency key for routine writes and does not require a per-write user
approval prompt. Unknown profile values fail closed, and requests outside the
configured path, scope, or recovery contract are rejected.

Authenticated Streamable HTTP uses stateful sessions at `/mcp` for the
compatibility revision `2025-11-25`:

```bash
MCP_HTTP_JWT_SECRET='<at least 32 bytes>' \
MCP_HTTP_JWT_ISSUER='https://issuer.example' \
MCP_HTTP_JWT_AUDIENCE='claude-obsidian' \
MCP_HTTP_ALLOWED_HOSTS='127.0.0.1,localhost' \
MCP_HTTP_ALLOWED_ORIGINS='https://client.example' \
npm run start:http
```

The HTTP server binds `127.0.0.1:3000` by default. Set `MCP_HTTP_BIND` and
`MCP_HTTP_PORT` explicitly when needed. Remote deployments must terminate TLS
in a trusted reverse proxy and restrict network access to the configured bind
address. Every request requires an HS256 bearer JWT with the exact configured
issuer and audience, a future `exp`, and a scope accepted by the requested
surface. Vault tools and resources require `vault:read`; repository metadata
requires `repo:read`; write tools require `vault:write`. `vault:admin` is
accepted for authentication but does not expose an additional MCP operation.
Query strings are rejected, so credentials cannot be supplied in URLs.

`MCP_HTTP_MAX_BODY_BYTES` and `MCP_HTTP_MAX_RESPONSE_BYTES` default to 1 MiB.
`MCP_HTTP_CONCURRENCY_LIMIT` defaults to 16, and
`MCP_HTTP_REQUEST_TIMEOUT_MS` defaults to 30000, and
`MCP_HTTP_SESSION_TTL_MS` defaults to 15 minutes. Host and Origin allowlists
are checked before authentication and before any MCP handler runs. POST carries
JSON-RPC, GET opens the session SSE stream, and DELETE closes the session. The
modern `2026-07-28` Streamable HTTP envelope remains planned because the pinned
stateful adapter does not classify modern envelopes; it is not advertised as
supported.

## Monitoring

The HTTP server writes one bounded `mcp-http-request` JSON event per request to
stderr. Events contain the HTTP status, duration, MCP method, tool name when
applicable, and sanitized JWT subject. They never contain bearer tokens,
request arguments, or vault content. `status` retains the legacy terminal status;
`terminal_status` makes that meaning explicit. `wire_status` is the status of
headers sent by the server, or null if no headers were sent (not proof of client
receipt). An SSE response can therefore have `wire_status: 200` and
`terminal_status: 408`. `terminal_reason` distinguishes `completed`, `http-error`,
`request-timeout`, `client-disconnect`, and other `request-aborted` outcomes.
`timeout_phase` is `body`, `transport`, or `response_stream` when a server deadline
fires, otherwise null. These fields diagnose the responsible boundary; they do
not identify the root cause of a slow tool or change deadlines/retry policy.
A Linux systemd deployment can run the packaged
`node monitor.mjs` command to perform an authenticated initialize handshake and
aggregate recent request counts, failures, tool calls, clients, stream counts,
and p95 request latency from the systemd journal. The p95 excludes long-lived
GET/SSE streams, which are reported separately. Set `MCP_MONITOR_URL`, the `MCP_HTTP_JWT_*` values,
and `MCP_MONITOR_EXPECTED_VERSION` in the monitor's environment. The command
writes an atomic state file and exits non-zero when the handshake, version
check, or journal read fails. This monitor requires `journalctl` and reads the
user service journal. Set `MCP_MONITOR_SERVICE` to the actual user unit name;
the default is `claude-obsidian-mcp.service`. It is not a macOS health checker
or a checker for the system-level service example below.

The server resolves `OBSIDIAN_LOCAL_MD` through the existing stable resolver.
The stdio entrypoint does not start an HTTP listener. In this
repository's source checkout, repository metadata access can default to the
checkout root. Always configure explicit roots for deployment. An
installed package never infers authorization from its install location or
working directory. Set the path-delimited `MCP_REPOSITORY_ROOTS` environment
variable to enable `obsidian_commit_meta` for explicit local repository roots.

The taxonomy resource returns only the project-taxonomy table. Tool arguments
are validated inside the handlers so invalid input uses the stable structured
error contract instead of leaking SDK validation text.

## Write contract

`obsidian_keeper_save` accepts `title`, `body`, required `resolved: true` and
`folder_hint`, plus optional `type`,
`links`, and `session_link_date` (links the note from that day's daily note; needs a valid `daily_path`), plus optional `idempotency_key` and `request_id`. `folder_hint` is
the caller-resolved vault-relative target folder; this MCP adapter does not run
librarian routing or deduplication. Streamable HTTP
writes require `idempotency_key`; local stdio calls retain keyless compatibility.
`obsidian_daily_append` accepts `content`, optional `section`, `date` or
`target_path` (an exact vault-relative `.md` path to append to instead of the daily note; mutually exclusive with `date`),
`skip_if_hash` (a 7-64 character hexadecimal commit hash that must appear in the submitted `section` heading), and
`init_content` (up to 65536 characters written as the note's initial content, such as frontmatter, only when the target note does not exist yet; ignored for an existing note), plus the same request fields. An exact heading hash skips; a strict abbreviation/full-hash relation returns `CONFLICT` without writing because the vault cannot prove commit identity. Daily writes use the configured
`daily_path`, and results report the vault-relative path actually written. MCP
writes that use `daily_path` (daily appends without `target_path`, and `session_link_date`) fail with `CONFIG_INVALID` when it is absent or invalid; the
adapter never silently substitutes `Daily/`.

Both tools return `status`, `request_id`, `idempotency_key`, `path`,
`affected_paths`, `warnings`, `recovery`, `error_code`, and `retryable`.
`committed` and `skipped` are successful. `conflict`, `partial`, and `failed`
return `isError=true`.

Commit-capture rollback is safe only after both the server/plugin and any copied
Git-hook runtime have this guard and emit full commit IDs. Pre-upgrade plugin
rollback remains unsupported.

Reuse an idempotency key only for the same payload. A repeated key and payload
returns `skipped`; a repeated key with different content returns
`IDEMPOTENCY_CONFLICT`. After a retryable timeout, cancellation, or partial
result, inspect `affected_paths` and `recovery`, then retry with the same key.
Keyless ambiguous failures, including partial outcomes, are not automatically
retryable. Their recovery action requires manual verification of
`affected_paths` before any retry.
If `recovery.required` remains true, follow its action before changing the key
or editing affected files manually.

Each write runs the keeper under a per-call-type subprocess cap:
`MCP_KEEPER_SAVE_TIMEOUT_MS` (default 25000) for `obsidian_keeper_save`, which
also reconciles the folder INDEX, `MCP_DAILY_APPEND_TIMEOUT_MS` (default 10000)
for `obsidian_daily_append`, and `MCP_COMMIT_META_TIMEOUT_MS` (default 5000)
for `obsidian_commit_meta`. Values are milliseconds between 100 and 600000; an
invalid value stops the server at startup. On the HTTP transport keep every
cap, plus the 500 ms the server allows a stopped subprocess to exit, below
`MCP_HTTP_REQUEST_TIMEOUT_MS`, whose maximum is 300000: a call that outlives
its HTTP request is cancelled with it and the client never sees the outcome.
The HTTP server prints an `mcp-http-warning` line at startup for each cap that
does not fit.

When a keeper save is stopped after the note itself was written (its cap, its
output limit, or the keeper exiting without a result), the result carries that
code (`SUBPROCESS_TIMEOUT`, for example) and its warnings and
`recovery.action` name the unfinished steps. `obsidian_keeper_save` does not
link a daily note, so those are `INDEX` and, for a keyed save, the idempotency
record. Do not write the note again. If the save had an idempotency key, the
result is `partial` with `retryable: true`: retry with that key, after raising
`MCP_KEEPER_SAVE_TIMEOUT_MS` when the cap was the cause (the same cap would
stop the retry at the same step). Without a key the result is `failed` with
`retryable: false`; finish the named steps by hand. A request that is
cancelled after the note was written gets the same outcome inside the server,
but MCP sends no response to a cancelled request.

The `ask_vault_librarian` prompt is a bounded read-only MCP workflow over the
taxonomy, librarian, pending, and search-preview surfaces. It does not invoke
the existing librarian agent, whose query path may refresh INDEX links. The
`summarize_session` prompt accepts the transcript as an explicit argument and
returns the provider instructions for the existing `SKIP`-or-Markdown summary
contract, including configured routing, taxonomy, scoring thresholds, and
keeper outcome boundaries. It does not run a provider or persist the result;
persistence remains in the client-side shell workflow. Both prompts are exposed
to clients with `vault:read`. The admin-only `reorganize_vault` workflow remains
outside the MCP MVP until a separate policy and write contract exists.

The machine-readable contract is [`contract.json`](contract.json). Its fixture
cases are in [`test/fixtures/contract-fixtures.json`](test/fixtures/contract-fixtures.json)
and are checked by `test/contract.test.mjs`.

Admin prompts and legacy HTTP+SSE remain explicitly gated in the contract. Routine
writes are released behind the configured policy, the `vault:write` scope, and
authenticated HTTP idempotency.

## Release and installation contract

GitHub Actions is the release builder. A pushed `mcp-vVERSION` tag must match the
private package version exactly. The workflow checks out that tag, installs the
locked dependencies, runs the package test suite, runs `npm pack`, and retains
the resulting tarball and release metadata as a CI artifact. Tag builds also
publish both files as GitHub Release assets. The package remains private and is
never published to the public npm registry.

The metadata is generated from the packed tarball. It records the source commit,
package name and version, SHA-256 of the packaged `contract.json`, SHA-256 of the
tarball, and the Node.js and npm versions used to build it. It intentionally has
no timestamp or runner-specific fields.

To reproduce and inspect a bundle locally, check out an exact `mcp-vVERSION` tag
or full commit SHA and run:

```bash
npm run release:bundle -- --revision <full-checked-out-commit-sha>
ls -l release/
```

Use the full checked-out commit SHA instead of the tag when preparing a bundle
before tagging. The command refuses branch names and refuses a revision that does
not resolve to the checked-out commit. It also requires a clean worktree so the
recorded commit identifies every packed file. It runs `npm ci`, a runtime dependency
audit that blocks high/critical advisories, the package tests,
and the same pack and metadata path used by CI.

For an existing versioned tag, use that exact `mcp-vVERSION` instead. Do not
reuse an old version for a different release. No standalone release exists merely
because this source tree was extracted.

An npm tarball contains a `package/` directory, not an immediately runnable
release root. Extract it into a new versioned directory, enter its `package/`
subdirectory, then install the frozen runtime dependencies:

```bash
cp dist/runtime-lock.json package-lock.json
npm ci --omit=dev --ignore-scripts
node dist/http.mjs
```

Set the required server environment before starting. Build dependencies are not
needed for the prebuilt tarball. Point service working directories and the
`current` symlink to that installed `package/` directory. The build copies the
canonical source `package-lock.json` to `dist/runtime-lock.json` because npm
omits root package-lock files from tarballs. Restoring that copy supports both
npm 11 and npm 12 without depending on shrinkwrap support. Source builds and
production installs use the same dependency versions and integrity hashes.
Dependency downloads still require npm registry access. Do not cut over a live service before
verifying the artifact checksum, contract, configuration and handshake.

## Self-Host Deployment and Service Management

Deployments can manage the Streamable HTTP server via `systemd` on Linux or `launchd` on macOS. Services run against the locally installed package artifact with explicit environment configuration and restricted file permissions.

### systemd service example (`/etc/systemd/system/obsidian-mcp.service`)

```ini
[Unit]
Description=Obsidian MCP Streamable HTTP Service
After=network.target

[Service]
Type=simple
User=obsidian
Group=obsidian
WorkingDirectory=/opt/obsidian-mcp/current
EnvironmentFile=/etc/obsidian-mcp/server.env
ExecStart=/usr/bin/node dist/http.mjs
Restart=always
RestartSec=5
LimitNOFILE=65536

# Security sandbox
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/log/obsidian-mcp /srv/obsidian-vault
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### Environment configuration (`/etc/obsidian-mcp/server.env`)

Generate a cryptographically secure 256-bit secret (64 hex characters / 32 bytes):
```bash
openssl rand -hex 32
```

Configure `/etc/obsidian-mcp/server.env` with your deployment values:

```ini
OBSIDIAN_LOCAL_MD=/etc/obsidian-mcp/obsidian.local.md
MCP_HTTP_BIND=127.0.0.1
MCP_HTTP_PORT=3000
# Generate a real secret with: openssl rand -hex 32
# Placeholder and example values will fail closed on startup.
MCP_HTTP_JWT_SECRET=<generate-secret-using-openssl-rand-hex-32>
MCP_HTTP_JWT_ISSUER=https://issuer.example
MCP_HTTP_JWT_AUDIENCE=claude-obsidian
MCP_HTTP_ALLOWED_HOSTS=127.0.0.1,localhost,mcp.example.com
MCP_HTTP_ALLOWED_ORIGINS=https://client.example
MCP_STDIO_PROFILE=write
```

File permissions must protect credentials (`chmod 0600 /etc/obsidian-mcp/server.env`).

### Reverse proxy TLS termination

Expose the HTTP service exclusively through a reverse proxy terminating TLS. For example, using Nginx:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:3000/mcp;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_read_timeout 300s;
    proxy_buffering off;
}
```

**Host header validation:** `MCP_HTTP_ALLOWED_HOSTS` validates the HTTP `Host` header received by the server. When Nginx forwards the incoming host via `proxy_set_header Host $host;`, ensure your public domain (e.g. `mcp.example.com`) is present in `MCP_HTTP_ALLOWED_HOSTS`. Alternatively, rewrite the upstream Host header to loopback (`proxy_set_header Host 127.0.0.1;`) if public hostname forwarding is not needed.

### Upgrades, Compatibility, and Safe Rollback

1. **Verification before cutover:**
   - Download the target release tarball and `<version>.release.json` metadata asset.
   - Verify the tarball checksum against the release metadata `artifact.sha256`.
   - Extract into a versioned release directory: `/opt/obsidian-mcp/releases/<version>`.
   - Verify the extracted `contract.json` matches `contract_sha256`.
   - Verify an authenticated MCP initialize handshake and its reported version with a configured MCP client. For a user systemd unit with journal access, `node monitor.mjs` also checks these and reports journal telemetry.
2. **Atomic switchover:**
   - Update the active symlink: `ln -sfn /opt/obsidian-mcp/releases/<version>/package /opt/obsidian-mcp/current`.
   - Restart the service: `systemctl restart obsidian-mcp`.
   - Repeat the authenticated initialize handshake and version check. Use `node monitor.mjs` only for a user systemd unit with accessible journal telemetry.
3. **Operational rollback:**
   - Retain the prior versioned release directory (e.g. `/opt/obsidian-mcp/releases/<prior-version>`).
   - If regressions or health check failures occur, point the `current` symlink back to the prior directory and restart:
     ```bash
     ln -sfn /opt/obsidian-mcp/releases/<prior-version>/package /opt/obsidian-mcp/current
     systemctl restart obsidian-mcp
     ```
   - Rollback is atomic and does not modify vault data, commit capture contracts, or existing historical notes.
