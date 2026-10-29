# Obsidian MCP portable client

Imported from the tracked public client at
`nhangen/claude-obsidian-plugin@76df3151104e37f409589fcda7ff3da65cc8c0d8`.
This copy pins the independently published server `mcp-v0.2.1`; it does not
activate or replace any existing host integration. The upstream source and
auth/session patch are retained; Express and Undici dependencies are patched,
rebuilt and repinned with regenerated notices and provenance. The rebuild gate
requires a valid zero-vulnerability audit report. This is source/fixture delivery toward issue #2, not proof of
automatic-capture acceptance on every host or a published client release.

This plugin-free client bundle connects to the catalog-pinned server release through separate read and write Streamable HTTP profiles. It does not require an Obsidian vault mount or an installed Obsidian plugin on the client.

The profile `requiredScopes` describe which token each profile needs. They do not limit a credential themselves; the server enforces the actual request scopes.

Configure each profile explicitly with environment variables. `OBSIDIAN_MCP_<PROFILE>_CREDENTIAL_PROVIDER` accepts `env`, `file`, or `keychain`; see [`examples/profiles.env.example`](examples/profiles.env.example). The selected token is exported only to the child bridge process as a profile-specific authorization variable. It is never written to a temporary file or passed as a command-line argument. File credentials must be owned by the invoking user and mode `0400` or `0600`.

`https` endpoints are accepted by default. Loopback `http` requires `OBSIDIAN_MCP_ALLOW_HTTP=1`; a `.ts.net` HTTP endpoint additionally requires `OBSIDIAN_MCP_ALLOW_TAILNET_HTTP=1`. URL userinfo and control characters are rejected. `Origin` is derived from the endpoint authority unless `OBSIDIAN_MCP_<PROFILE>_ORIGIN` supplies a valid explicit origin.

Use a host's documented native Streamable HTTP MCP registration when it supports that transport. This bundle intentionally does not invent host-specific configuration or approval syntax. Both the read and write launchers verify the bundled `mcp-remote@0.14.3` runtime before executing it directly with Node.js. Its dedicated preauthenticated HTTP mode requires the launcher's explicit bearer header, skips OAuth discovery and compatibility probing, and fails closed on authorization errors without starting an OAuth flow. Only `obsidian_find_notes` retries once after an expired-session HTTP 404, sharing recovery across concurrent reads. Session-expiry recovery strictly excludes writes, authentication failures and timeouts, preserving write idempotency. Neither launcher delegates to `npx` or unverified external npm packages. Missing or mismatched artifact bytes fail closed without an npm-cache fallback. The upstream license, source hashes and exact patch are included under `vendor/mcp-remote-read`.

The bridge runtime is self-contained: it needs no installed `node_modules` or npm cache. Its pinned build lock, rebuild script, patch, licenses, and Linux `xdg-open` helper are included and verified with the bundle. The write launcher executes this verified runtime directly via Node.js with profile-scoped write credentials. The bundle requires Python 3.11+ and Node.js 22 or newer; pack an immutable output with `python3 scripts/pack-obsidian-mcp-client.py --output <directory>`. A different bundle needs a different directory; packing never overwrites an existing mismatched bundle.

The packer emits `release.json` and prints its SHA-256. Obtain both the standalone
`bin/obsidian-mcp-client-install` bootstrap and that expected digest from a trusted
build or an outer authenticated release. The installer checks the exact bounded
metadata bytes against the externally supplied digest before parsing them, then
checks every payload path, mode, digest, and release identity. The copy of the
installer listed in its own inventory is diagnostic evidence after trust has been
established; it cannot authenticate itself.

Client-owned failures use exit code `64` consistently in the installer, packer,
commit-event producer, commit-capture consumer, and read/write launchers. This
includes invalid CLI arguments; `--help` on the installer and packer remains a
successful exit. A launched third-party bridge retains its own exit status.
If packing fails after creating the output directory, the error names the
incomplete directory to inspect and remove before retrying; it is not overwritten
or silently deleted.

Install with `python3 bin/obsidian-mcp-client-install --bundle <bundle>
--release-sha256 <digest>`. `--dry-run` makes no managed-directory changes. Releases
are immutable and retained under `<prefix>/releases/<release-id>`; `current`,
`previous`, and `disabled` are the only completed-state pointers. Upgrades and
`--rollback` keep the client enabled or disabled as it was; `--disable` and
`--enable` switch it. A locked,
fsynced transition journal recognizes only its complete before, after, and canonical
partial pointer maps. Unknown state fails closed. Reinstalling the same verified
release does not replace its files. No operation changes host configuration,
credentials, hooks, vault content, server state, or writer ownership.

`--prefix` accepts absolute paths or paths relative to the current working
directory, normalizing `~`, `.` and `..` before applying the same safety checks.
Dry runs print one canonical JSON object with `after`, `before`, `dryRun`, and
`operation`, with release IDs or `null` in the three slot fields. An interrupted
release copy cannot activate a partial release. Abandoned `.release-*` staging
directories are not reused; a cleanup failure reports the exact path without
hiding the original failure. Existing incomplete or tampered `releases/<id>`
directories fail closed instead of being overwritten.

Configure a supported MCP host to launch `<prefix>/current/bin/obsidian-mcp-read`
and `obsidian-mcp-write`, as shown in the public `integrations/` guides. No home
wrapper, private harness or plugin is needed. Installing this bundle never changes
an existing host's routing or writer ownership.

The bundle includes a commit-capture consumer. The default pinned release
`mcp-v0.2.1` provides exact-target append and the exact/prefix hash guard, so an
explicit invocation can pass the local capability gate. The consumer still requires
the live server version and discovered schema to match before writing; the released
contract alone does not change writer ownership.

## Local commit-event metadata

`bin/obsidian-mcp-commit-event` requires a POSIX host (macOS, Linux or WSL), Python
3 and Git. It reads one JSON event
from stdin after a host has observed a successful commit:

```json
{"schemaVersion":1,"event":"git.commit","outcome":"success","repository":"/absolute/repository/root","commit":"0123456789012345678901234567890123456789"}
```

The host must supply the full commit hash from that event, not a revision expression.
The producer requires that commit to remain HEAD in the exact repository root.
Missing, failed, stale or malformed events return a nonzero exit with a bounded
diagnostic and no metadata. An event is a trusted host assertion, not proof the
producer executed the commit. Do not invoke it on failed commands or infer an event
from an arbitrary existing HEAD.

Input is capped at 64 KiB with a five-second read deadline. Each read-only Git
command has a five-second deadline and capped output; a stalled command's process
group is terminated. Limits fail explicitly rather than truncating metadata.

The JSON result contains the full hash, current branch, changed files, sanitized
`org_repo` and `repo_name`, branch ticket, and commit-local date/time. Detached HEAD
has an empty branch; a repository without origin uses `local/<repository-name>`.
Transport credentials and URL query/fragment data never enter repository identity.
No vault path, plugin configuration, author identity or commit subject is emitted.

This is a read-only metadata producer, not a capture hook or a vault writer. It
does not install hooks, change MCP routing, or report a commit as captured. Hosts
without a successful-event channel must report that capability as unavailable.

## Commit capture consumer

`bin/obsidian-mcp-capture-commit` accepts the same five event fields plus a
required `context` array of one to eight single-line, client-authored summary
items. It invokes the bundled event producer locally, then appends through the
bundled write bridge only after both the bundled contract and live discovered
`obsidian_daily_append` schema expose `target_path`, `skip_if_hash`, `section`,
and `idempotency_key`. The client requests `2025-11-25` only when the pinned
matrix approves that implemented initialize revision for both stdio and Streamable
HTTP with non-planned status. The initialize response must return that exact
protocol, an initialized tools capability object, and the server version pinned by
the manifest before the client sends any initialized notification or tool request.

The default target is
`Projects/Development/<org>/<repo>/<date>.md` for every repository. This public
consumer contains no employer-specific path overrides. Do not migrate an existing
custom-routing workflow without explicitly reconciling its target and writer
ownership first.
The section includes commit-local time and the full hash; the body includes the
branch and supplied context. A deterministic idempotency key is derived from the
sanitized repository identity and full commit hash, so changed context under the
same commit is a server conflict rather than a second write.

The consumer reports JSON with `status: committed` only after validating a
non-error, non-recoverable committed outcome at the exact target. An exact replay
is reported as `status: skipped`. Partial, conflicted, contradictory, mismatched,
or unknown outcomes exit nonzero without claiming capture. Validated partial,
conflict, and failed outcomes emit a bounded JSON failure envelope containing the
stable idempotency key, canonical path, affected paths, status, error code,
retryability, recovery requirement, and fixed client guidance. An unknown result
after dispatch is `ambiguous` and requires authoritative-path verification before
retrying with the same key. Arbitrary server strings, bridge stderr, and request
content are never copied into diagnostics.
An idempotency-key conflict directs the operator to verify the existing idempotency
record. A heading-identity conflict instead directs the operator to verify the note's
full commit identity.

The bridge operation budget defaults to 120 seconds across initialization, tool
discovery, and the remote append. `OBSIDIAN_MCP_CAPTURE_BRIDGE_TIMEOUT_SECONDS`
may set an integer from 1 through 300 for isolated environments. Invalid values
fail before the bridge starts. The producer and bridge pipes retain independent
size and deadline bounds. Successful shutdown closes stdin and waits through the
grace period before reaping any surviving process-group descendants.

This CLI does not install a Git hook, claim writer ownership, change host routing,
or fall back to the plugin keeper.

The consumer remains default-unactivated. On an upgraded server, exact full hashes
skip and a strict short/full prefix relation blocks with `CONFLICT`; no arbitrary
historical alias is treated as equivalent and the bundle performs no historical
backfill. Old server releases and rollback to a pre-upgrade writer are unsupported
for this migration. The default pin is `mcp-v0.2.1`, but
`legacyHashInteroperability` remains `unproven` and capture remains disabled by
default until the live canary and writer-ownership migration complete.

## Native capture in an isolated Codex profile

The bundle includes a first-host integration for Codex native hooks. It requires
a POSIX client, Python 3.11+, a Codex build supporting UserPromptSubmit,
PreToolUse/PostToolUse and Stop hooks, a new CODEX_HOME, and a clean Git project
with no inherited `.codex`, `.claude`, `.opencode`, `.agents`, AGENTS.md or CLAUDE.md.
Codex 0.163.0 exposes these hooks. This is an opt-in profile, not migration of an
existing host or completion of the other-host matrix.

Configure the read/write endpoint and credential-reference environment variables
from `examples/profiles.env.example`, then create the profile using the trusted
packed bundle's external release.json digest:

```sh
<bundle>/bin/obsidian-mcp-native create --home /absolute/new-codex-home \
  --project /absolute/clean-git-project --folder Projects/Development/example/project \
  --release-sha256 <trusted-digest>
<bundle>/bin/obsidian-mcp-native run --home /absolute/new-codex-home
```

Authenticate Codex in that isolated profile separately. No provider credentials
are copied from another profile. The launcher fixes CODEX_HOME and cwd; config
registers only the two MCP launchers, native capture hooks, and scoped automatic
MCP approval. Endpoint credential values are not serialized into profile files.
Creation returns `launch_command` with the absolute creator Python interpreter.
Use that command when the shell PATH differs; hooks, executor and capture guidance
pin the same Python 3.11+ interpreter. Recreate the profile if it is removed.
Only selected environment variable names are forwarded to the MCP children.
On macOS the isolated HOME has no default keychain search list. Supply credentials
through the existing environment provider or an absolute private credential file.
The launcher also isolates HOME and XDG directories under `runtime-home`, so
personal `.agents/skills` and user configuration are not silently inherited.
Open `/hooks` in that profile and review/trust the exact hook definitions before
using capture. Untrusted hooks are skipped by Codex; profile creation is not hook
activation. No hook-trust bypass is installed. The workspace-write sandbox grants
the local helper write access only to this profile's capture-state directory.
Codex may add UI, project-trust and hook-trust records to its config. The helper
accepts those host records while requiring the capture configuration to match.

UserPromptSubmit establishes a turn-bound session-save identity. The current host
model supplies the meaningful summary and invokes the native MCP save tool.
PostToolUse validates its successful receipt, including note, INDEX and daily
paths. An explicit turn-bound `skip` records `skipped_by_policy`, not a saved note.
Stop requests one continuation without a receipt; a repeated Stop reports
unresolved capture instead of looping or claiming success. Pending previous work
cannot be silently discarded by the next user prompt.

For commits, run one `git -C <project> commit ...` command, optionally with `rtk`
or `rtk proxy`; stage and test in separate tool calls. The paired hooks require a
matching call ID/command, a successful executor result, and exactly one local reflog
commit transition. Compound commands, substitutions, redirects and environment
overrides are explicitly unsupported. No event means no inferred historical tip.
The existing producer sanitizes metadata; `prepare` takes the pending event ID and
model-supplied context, then reuses the consumer's canonical argument builder.
The model calls the native MCP append tool with those exact prepared arguments.
Codex sends raw shell output to PostToolUse without an exit envelope. PreToolUse
therefore rewrites eligible commits through the bundled local executor, retaining
the original validated arguments. The executor records its actual Git exit code
and verified transition privately before PostToolUse consumes them; printed text
cannot substitute for that record. Normal sandbox approval applies to the rewritten
command. The hook permits and rewrites only supported commits; Codex still applies
its configured sandbox and normal execution-approval policy. Interrupted execution
remains unresolved for explicit inspection, and a
completed executor record cannot execute Git a second time.
Pre/Post hooks require matching call IDs, arguments and validated receipts.
No second shell-based network or approval path is introduced.
Commit capture is serialized at project scope. The session that prepares an
eligible commit owns the capture window: while one of its executor snapshots or
pending commit events is unresolved, the hooks deny other sessions in the same
profile new commit tool calls instead of letting them advance HEAD and strand
the earlier capture. External Git processes outside the hooks bypass this gate
and will still leave the earlier capture unresolved. `status --home <profile>
--session <key>` reports `gate` naming the owning session when denial applies.
An interrupted owner is recoverable: `takeover --home <profile> --session
<your-key> --verification command-stopped-and-history-inspected` records the
owner's snapshots and pending commits as `not_captured` evidence, releases the
gate, and never claims a successful capture or reconstructs git history. An
owner with nothing pending reports `recovered_no_pending` instead of
`recovered`, an owner whose state file is missing reports
`recovered_owner_state_missing` (nothing was inspected), and a failed
`git rev-parse` marks the record's `observed_head` null with `head_unavailable`
in the result. A write the owner had already dispatched may still land later; the rescued event stays
marked unresolved, which under-reports rather than over-reports capture.
If `capture-state/active.json` is corrupt (`ACTIVE_STATE_INVALID`), the same `takeover`
command deletes it and reports `cleared_invalid_gate`; the commit event is already
recorded in the session state by then. `takeover` also aborts with
`INTERRUPTED_OWNERSHIP_CHANGED` if the owner released the gate while it waited.
If `daily-append` or `resolve-snapshot` finish their local write and then find
the gate corrupt or owned by another session, they report it (`note` in the
result, one context line in the hook) without raising, so a completed capture
is not reported as a failed operation.
Cross-session repair details and follow-ups remain tracked for separate multi-session workflows.
The full commit must still be HEAD. Replays preserve the original payload digest;
changed context conflicts locally. Partial/unknown outcomes remain visible and
block automatic retry until manually inspected. `retry --session <key> --commit
<event-id> --payload-sha256 <digest-from-status> --verification
authoritative-path-inspected` records that explicit verification and returns the
original native append arguments; omit `--commit` for original session-save
arguments. The helper does not claim to have performed the authoritative
inspection. `request --session <key>` explicitly prints the preserved session
payload for exact retry; ordinary status omits saved content. A changed payload is
never a recovery action. Do not review/retry a native call that is still running.
For a denied/canceled commit tool with no Post event, first confirm the tool stopped
and inspect local history, then use `resolve-snapshot --session <key> --snapshot
<id> --verification command-stopped-and-history-inspected`. This retains explicit
`not_captured` evidence and never reconstructs a missing successful event.

Use `status --home <profile> --session <key-from-hook>` to inspect bounded local
evidence without a vault mount. `disable --home <profile>` verifies the owned files
and removes only the profile's config, hooks and instructions, retaining auth and
capture evidence. Drift refuses disable. Existing profiles/plugins, Git hooks,
vault content and running sessions are never changed. Restart or close a running
profile before disabling it; an already-running host has loaded its hook settings.
General profile upgrade/migration and an all-host writer handoff remain separate
acceptance gates. The existing managed client installer still owns bundle lifecycle.

## Attached Claude plugin capture API

The public runtime also supports an explicit `claude-plugin` host mode. This is
source support for an optional plugin dispatcher, not installation, hook
registration, writer activation, or a completed migration. Existing Codex creation
and isolation remain unchanged. No second transport or provider process is added.

Before using this API, verify the public release using the installer instructions
above. The dispatcher must require a successful capabilities response with
`apiVersion: 1` and `claude-plugin` in `hosts`; an older release or any nonzero exit
is unsupported, with no local writer fallback. Directory attachment additionally
requires `projectModes["claude-plugin"]` to contain `"directory"`. The additive
capability map is `{"claude-plugin":["git","directory"],"codex":["git"]}`;
it does not advertise non-Git Codex native profiles:

```sh
python3 <verified-bundle>/bin/obsidian-mcp-native capabilities \
  --release-sha256 <trusted-release-json-sha256>
python3 <verified-bundle>/bin/obsidian-mcp-native attach --host claude-plugin \
  --home /absolute/new-private-project-profile --project /absolute/git-root \
  --folder Projects/Development/example/project --daily-folder Journal \
  --release-sha256 <trusted-release-json-sha256>
```

`attach` requires a nonexistent profile path beneath an existing canonical parent.
It creates a 0700 directory, a 0600 `capture-profile.json`, and a 0700
`capture-state` directory. The descriptor contains `host: "claude-plugin"`,
`active: true`, canonical project and bundle paths, the pinned release digest,
creator Python, session folder and daily folder. It accepts projects with existing
host settings and instructions, and writes none of those files. `--daily-folder`
defaults to `Daily`; both folders must be normalized vault-relative directories.
The supplied daily folder must match the server configuration: receipts naming a
different daily path remain unresolved rather than relaxing verification.
Every profile load verifies the same release bytes and installed trust record as
the isolated runtime. A profile remains pinned to its immutable release even if
the installer's `current` pointer later changes.

### Explicit directory session profiles

`attach --project-mode git` remains the default. Descriptors without `projectMode`
retain Git validation. An explicit `--project-mode directory` attaches an existing
absolute directory for Claude session capture without initializing Git or changing
workspace files:

```sh
python3 <verified-bundle>/bin/obsidian-mcp-native attach --host claude-plugin \
  --home /absolute/new-directory-profile --project /absolute/workspace \
  --project-mode directory --folder Projects/Research/workspace \
  --daily-folder Daily --release-sha256 <trusted-release-json-sha256>
```

The descriptor persists `projectMode: "directory"` and the canonical directory
identity. Hook working directories must exist within that root by path components;
sibling prefixes and symlinks escaping the root refuse capture. Ordinary session
events, session receipts, recovery review, and drain perform no Git operation.
Session idempotency, exact receipt/backlink validation, unresolved-write retention,
and bounded Stop continuation are unchanged. Unknown, empty, or non-string modes
fail closed. `create --project-mode directory` refuses before creating an isolated
Codex profile.

Directory profiles are session-only even when a nested directory contains Git.
All shell commands run normally under the host's permissions, including direct,
wrapped, or opaque commit commands; the capture runtime does not parse or rewrite
them, create commit snapshots/gates, reconstruct metadata, or acknowledge a commit.
Directory mode is not a shell sandbox. Explicit native commit capture operations
and commit-targeted append events still refuse before creating capture state.
Ordinary tools and manual appends with unrelated keys do not settle automatic
capture. Use a separately attached Git profile for commit capture; the integrating
plugin must choose the
most specific explicit canonical root and reject duplicate registrations. Without
a user-scope fallback, an unmapped workspace must be attached explicitly.

Install and verify the supporting release, check its mode capabilities, and attach
a fresh profile before the plugin's separately managed ownership handoff. Existing
profiles retain their immutable release pins; editing an old descriptor does not
upgrade its runtime. Source fixtures do not establish installed-host acceptance.

Dispatcher commands (JSON stdout, nonzero exit on invalid profile/state):

| Command | Contract |
| --- | --- |
| `profile-status --home <profile>` | Validates the descriptor and release; returns `status: "validated"`, `scope`, `host`, `bundle`, `folder`, `daily_folder`, `release_id`, `release_sha256`, `active`, and `apiVersion`. Project scope also returns `project` and `projectMode`. Does not require a session ID. |
| `hook --home <profile>` | Reads the full Claude hook JSON from stdin; emits host hook JSON. The dispatcher performs no turn normalization. |
| `drain-status --home <profile>` | Inspects **all** persisted sessions; returns `status: "inspected"`, `quiescent`, `session_count`, hashed session IDs with unresolved reason codes, and `active_request`. Emits no saved body or transcript. |

The plugin must close its ownership/admission barrier **before** draining and keep
it closed through owner or profile remapping. Drain is an observation, not a
writer lease. It acquires the gate and every discovered session lock without
waiting, refuses contention, corrupt state, missing active-owner state, unknown
state files and, for standalone project profiles, more than 256 sessions. User
parents use the streamed retained-state inspection described below. Drain never
repairs or deletes evidence.
`quiescent: true` requires no active request, pending session disposition,
in-flight/recovery state, snapshots, pending/uncaptured commits or unresolved
snapshot-resolution evidence. A nonzero exit is never permission to hand off.
`takeover` and `resolve-snapshot` retain uncaptured evidence and cannot substitute
for lossless draining. `disable` and `run` are unsupported for attached profiles;
the plugin owns writer admission and rollback separately. No registration or
ownership change is performed by any of these API calls.

### User scope without workspace registration

An integrating Claude plugin can configure a single fallback session folder and
daily folder, then delegate events from newly opened workspaces without attaching
each project. First require the additive capability map
`captureScopes={"claude-plugin":["project","user"],"codex":["project"]}`.
Profiles without `scope` retain `project` behavior; Codex remains project-only.

```sh
python3 <verified-bundle>/bin/obsidian-mcp-native attach --host claude-plugin \
  --scope user --home /absolute/new-private-user-profile \
  --folder Sessions --daily-folder Journal \
  --release-sha256 <trusted-release-json-sha256>
```

The parent descriptor has `scope: "user"`, the existing host, Python, bundle,
immutable release pin, `folder`, `dailyFolder`, `files: {}`, and `active: true`.
It omits `project` and `projectMode`; those CLI arguments are rejected for user
attachment. `active` validates the runtime descriptor, not writer ownership.
Attachment creates only private configuration and never activates the plugin's
user owner. The plugin manages activation and exclusions separately.
`profile-status` validates the parent without a project and returns `scope:
"user"`, omitting `project` and `projectMode`.

The runtime creates ordinary project descriptors under the parent's private
`workspaces/<workspace-hash>` directory. A Git workspace uses its canonical Git
root, including subdirectory or symlink entry; otherwise the canonical event cwd
gets directory session capture. Missing Git produces a session-only diagnostic,
never a commit event or receipt. No configuration, hook, or state is written into
either workspace. Child descriptors inherit the parent's folders and immutable
release pin and are checked against it on every load. Direct child CLI operations
also validate the parent. All directories are mode 0700; state files are 0600.

`bindings/<session-hash>.json` durably binds a session to its first workspace.
A session cannot rebind to another workspace, including while a commit or session
write is pending. Each child keeps its own existing commit gate; there is no
shared parent `active.json`. Parent `status`, `request`, `prepare`, `retry`, and
other session operations accept the existing hashed `--session` and resolve its
bound child. Hook recovery guidance also identifies the child directly.

Parent `drain-status` inspects every child and binding under a nonblocking parent
lock. It retains the existing `quiescent`, `session_count`, and `sessions` fields,
adds `complete`, `workspace_count`, `hot_session_count`, `archived_session_count`,
and `unresolved_session_count`. It streams all retained records one bounded record
at a time, returns at most 256 unresolved-session details and workspace summaries,
and marks truncated details separately from complete inspection. History has no
lifetime session or workspace admission limit. Busy locks, missing or tampered
evidence, invalid bindings, and pending work prevent a clean drain. An unfinished
retention journal returns `complete: false, quiescent: false`; a timeout or failure
is never permission to hand off. Inspection time grows with retained history, but
memory and response details remain bounded. The plugin must keep its admission
barrier closed during drain and handoff.

On valid prompts, verified terminal sessions move automatically out of hot
admission accounting. Only active or unresolved work consumes the 256-session
budget. New capture pauses advisably at that active-work limit; resolving existing
work releases capacity without new configuration. SessionStart inspects an
existing session but never creates enrollment; the first valid prompt enrolls it.

Terminal binding/state pairs are preserved in private immutable digest-addressed
records under `archive/<session-hash>`, with a current location/generation index
and a durable retained-binding locator. A fsynced `retention-journal.json` freezes
the transition. Complete archive publication precedes removal of hot binding and
state files. Next admission or a mutating session operation reconciles an
interrupted transition only when identities, generations, and payloads match;
conflicts fail closed. State and gate lock files are never moved or deleted.
Historical empty locks consume no admission capacity, but busy locks refuse drain.
Admission counts a busy session conservatively as active without refusing unrelated
workspaces below the active-work limit. Wrapped Git execution pins its child/session
state before releasing the parent lock, so independent workspace commits can proceed.

Resuming an archived session restores only its exact validated binding and state;
missing or conflicting evidence never falls back to fresh enrollment. Earlier
archive generations remain retained. Read-only status, request, profile validation,
and drain validate descriptors, parent identity, release pins, and retained state
without requiring a retired workspace to exist. Execution still checks the live
project. Child creation stages its complete private descriptor and empty state
directory outside `workspaces` before atomic publication; unambiguous interrupted
staging resumes, while conflicting leftovers remain preserved for inspection.

User-scope prompt failures emit advisory context and allow the ordinary prompt to
proceed. An unresolved previous turn remains unchanged; the binding pauses new
capture for that session instead of erasing the old turn or attributing new work
to it. Unrelated Bash commands proceed and Stop reports advisory context. Frozen
requests and matching late receipts still reach the existing validators: partial
or ambiguous writes need the existing authoritative `retry` review, and changed
payloads or unprepared new captures remain denied. Resolve the original state,
then submit a new prompt to resume capture. This retains one pending turn rather than adding a
multi-turn queue. Enrolled, unpaused pending work keeps the existing bounded Stop
recovery behavior. Missing enrollment never creates a Stop recovery loop.

Register `UserPromptSubmit`, `SessionStart` (including resume), `Stop`,
`StopFailure`, and paired `PreToolUse`, `PostToolUse`, `PostToolUseFailure` handlers.
Tool matchers must cover `Bash` and the exact names
`mcp__obsidian-write__obsidian_keeper_save` and
`mcp__obsidian-write__obsidian_daily_append`. Pass the original payload unchanged.
The runtime follows the [official Claude hook schemas](https://code.claude.com/docs/en/hooks):
prompt events supply no turn ID; tool failures supply `error`/`is_interrupt`
rather than a successful receipt. Configure host-level hook failures to refuse
execution where supported; a missing executable or unverified profile cannot
establish capture ownership merely by printing instructions.

Prompt occurrence identity hashes the session, canonical transcript identity,
file identity, byte watermark, bounded tail digest and prompt digest. Duplicate
deliveries at the same boundary reuse the turn; the same text at a later boundary
gets a new turn. Missing first-prompt transcript files use an empty watermark.
Resume reuses persisted state without inventing a turn or replaying history.
Changed transcript identity, orphan tool/Stop events and unresolved previous
turns refuse capture advancement. A changed boundary cannot prove a late delivery
was a duplicate, so unresolved work prevents a new capture occurrence. User-scope
prompts proceed with the advisory pause described above. State stores hashes, not
prompt text, transcript paths or raw transcript copies. Subagent events are
explicitly unsupported and cannot be attributed to the main session.

Session/commit planning, Git execution, canonical commit targets and receipt
validation use the shared engine. In Git profiles, Bash commits retain the isolated mode's bounded
single-command syntax. Repository subdirectories retain their execution directory
and relative pathspecs while metadata is attributed to the canonical Git root;
nested foreign repositories are refused. MCP result objects, JSON-encoded bare
receipts emitted by Claude, JSON-encoded wrappers and content-block arrays are
normalized into the same strict receipt validator. A matching failed
write retains its exact request/key and requires inspection plus the existing
`retry --verification authoritative-path-inspected` flow. Raw error text is not
persisted. A failed host tool cannot establish a successful commit event.
Claude commit rewrites omit an `allow` permission decision, so the rewritten
command still goes through the host's configured permission flow.
For attached Claude, pending-commit guidance supplies canonical
`obsidian_daily_append` arguments with three context placeholders. Replace those
bullets with factual context and call MCP directly; no Bash `prepare` command or
stdin helper permission is needed. The Pre hook validates the complete arguments,
revalidates the recorded Git event with a three-second producer budget, and
persists the immutable request and call identity before the write. The producer
budget leaves time for cleanup within the plugin dispatcher's eight-second limit.
The Branch line, target, section, full hash and idempotency key must match the
recorded plan; extra fields, including `init_content`, are rejected. Context uses
the existing limits: 1-8 single-line bullets, at most 4096 UTF-8 bytes each and
32768 bytes total. Literal placeholder bullets are rejected. Subsequent guidance
repeats the exact prepared arguments, including after branch or remote changes.
Only a paired, strictly validated successful receipt settles the commit.
Normal MCP tool authorization still applies; the observer emits no permission
allow override. Existing denial and recovery policies remain in force. Isolated
Codex capture retains its CLI `prepare` workflow and producer timeout.
Manual MCP writes with unrelated keys are observed without blocking or settling
automatic capture; matching automatic keys still require exact arguments.
Stop requests at most one continuation per occurrence, then reports unresolved
work. StopFailure retains state and never requests more provider work.

Fixture tests exercise the packed/installed CLI with temporary Git repositories,
Claude event fixtures and simulated MCP receipts. They do not establish installed
Claude execution, real provider usage/billing, live server recovery, plugin
writer handoff, release, or cross-host acceptance.
To additionally exercise the current plugin dispatcher and ownership barrier,
set `OBSIDIAN_MCP_TEST_PLUGIN_ROOT` to a public plugin checkout when running
`tests/obsidian-mcp-native.test.py`. The optional pair fixture installs the trusted
packed client, attaches a temporary Git project, hands ownership to MCP, executes
a fixture Git commit, observes simulated session and direct commit MCP receipts
through plugin hooks without CLI preparation, verifies drain, and hands
ownership to disabled. All state stays in temporary directories; no real server,
provider, user configuration or running writer is involved.
The additional user-scope pair fixture attaches one parent, activates the plugin's
user owner, opens fresh Git and nonGit directories without enrollment, observes
exact simulated session receipts through the real dispatcher, and verifies both
saved children and an aggregate clean drain.

The source fixture suite also covers 258 terminal sessions, exact archive resume,
interrupted archive/restore publication, digest conflicts, removed workspaces,
and retained busy locks. To run the longer production-threshold CLI acceptance
for 257 resolved, removed workspaces under one parent:

```sh
PYTHONDONTWRITEBYTECODE=1 OBSIDIAN_MCP_TEST_LONG_HISTORY=1 \
  python3 tests/obsidian-mcp-native.test.py \
  UserCaptureTest.test_cli_accepts_257_retired_workspaces_without_new_parent
```
