## design-sync Agent

Download-only Claude Design sync agent. Downloads **text files only** from a `claude.ai/design` project to a local directory, preserving exact bytes and directory structure. Binaries are skipped and reported.

## Purpose

Pure read-only sync — never edits product code, runs gates, or opens PRs. Use to:
- Pull Design text artefacts into a consuming worktree for implementation reference
- Re-sync updated designs (no automatic diff detection — full tree rewrite each time)
- Mirror a Design project's text file tree locally

## Locked Recipe (DO NOT RE-LITIGATE)

This design is **proven** by 4 prior spikes + cross-vendor review rounds. The ONLY working harness:

### Why `executor.type: claude_sdk`?

- **claude-NATIVE children** drop user python tools (relay allowlist `_NATIVE_RELAY_BUILTIN_TOOLS`) AND do not forward spec MCP servers → native is OUT.
- **claude-SDK children** DO see `tools/python/*.py` (bridged to model as `mcp__omnigent__<fn>`) and run tools with NO elicitation → this is the winning harness.
- Declared directly as `executor: {type: claude_sdk, model: claude-haiku-4-5}` (no `os_env` block — a local python tool does not need one to run; adding `os_env` only registers `sys_os_shell/write/edit`, a live push/edit surface we explicitly do NOT want).

### Why inline JSON-RPC transport?

- The `claude-design` MCP has NO project→disk primitive:
  - `write_files.local_path` = not-implemented
  - `copy_files` = remote→remote only
  - `DesignSync` tool = upload, not download
- So download MUST be done in-process by our python tool via raw JSON-RPC to the SAME endpoint the MCP uses: `https://api.anthropic.com/v1/design/mcp`
- Bearer token read from `~/.claude/.credentials.json` (field `claudeAiOauth.accessToken`)
- Content is entity-decoded and wrapper-stripped in the python tool (not by the model)

### Download-only enforcement (accurate tool surface)

There is **no allowlist/denylist to trim framework builtins** — a `claude_sdk`
agent always gets ~19 always-on tools. So the surface is NOT "single tool only".
What actually holds the download-only guarantee:

- **No `os_env` block** → the `sys_os_read/write/edit/shell` OS tools never register.
  Verified: the assembled ToolManager builds exactly these 19 tools —
  `browser_click`, `browser_navigate`, `browser_screenshot`, `browser_snapshot`,
  `browser_type`, `design_sync`, `list_comments`, `load_skill`, `read_skill_file`,
  `sys_add_policy`, `sys_agent_download`, `sys_agent_get`, `sys_agent_list`,
  `sys_cancel_task`, `sys_policy_registry`, `sys_session_get_history`,
  `sys_session_get_info`, `sys_session_list`, `update_comment`.
- **`guardrails.policies.deny_all_mutations`** (a `type: function` policy built from
  `omnigent.policies.function.make_fixed_action_callable`, `action: deny`) hard-DENIES
  every mutation-capable builtin at the `tool_call` phase. Audited across all 19
  registered tools, that set is **7 tools**: `update_comment` (edits a comment's
  persisted state), `sys_add_policy` (creates a session policy), `sys_agent_download`
  (**writes bundle bytes to disk** — arbitrary write), `sys_cancel_task` (POSTs a
  stop/interrupt → mutates task lifecycle), `browser_navigate`, `browser_click`,
  `browser_type`. Plus the `sys_os_write/edit/shell` names (belt-and-suspenders —
  they aren't registered without an `os_env` block anyway).
- **`design_sync` is the ONLY mutation-capable tool** left reachable (it writes to a
  contained local `out_dir`). The remaining **11 builtins are read-only**:
  `browser_screenshot`, `browser_snapshot`, `list_comments`, `load_skill`,
  `read_skill_file`, `sys_policy_registry`, `sys_agent_get`, `sys_agent_list`,
  `sys_session_get_history`, `sys_session_get_info`, `sys_session_list`.
  (`sys_agent_download` and `sys_cancel_task` are NOT read-only — they mutate, and are
  in the denied set above. `tests/acceptance_gate.py` asserts deny on all 7 and abstain
  on all 12.)

## How to Launch

Via Omnigent runtime MCP tools in a consuming session.

**Caveat:** `sys_session_create` is NOT a generally-available tool — it is only
registered for callers whose own spec sets `spawn: true` (session-spawning
capability). From a caller without it, use whatever session-spawn path your host
exposes (or launch the agent through the server API). The snippet below is
illustrative of the shape, not a tool guaranteed to be present.

```python
# Create session for design-sync agent (requires spawn:true on the caller)
# Note: agent_id discovery depends on how agent is registered;
# may be "design-sync" or an "ag_<hash>" id
session = sys_session_create(
    agent_id="design-sync",  # or discovered ag_<hash> id
    title="Download Design Project 82bd9df4",
    message="Download https://claude.ai/design/p/82bd9df4-843a-4b69-bb20-941fde27b040 to .design-mocks"
)

# Session returns conversation_id
conversation_id = session["conversation_id"]

# Monitor or send follow-up via sys_session_send
result = sys_session_send(
    session_id=conversation_id,
    args={"input": "Additional instructions if needed"}
)
```

**Note**: `working_directory` inheritance from caller is environment-specific — verify cwd behavior in your deployment context.

## Inputs

The agent accepts natural language prompts referencing:
- **project_url** (required): Full URL like `https://claude.ai/design/p/<id>`
- **out_dir** (optional, default `.design-mocks`): Local output directory (relative to cwd, escaping rejected)
- **include_ds** (optional, default `true`): Include Design System assets from `_ds/`

Examples:
- `"Download https://claude.ai/design/p/82bd9df4-843a-4b69-bb20-941fde27b040"`
- `"Sync project https://claude.ai/design/p/abc123 to ./mocks without _ds"`

## Output

Text files written to `<out_dir>/` in the consuming worktree:
- **Text files**: exact bytes, HTML-entity-decoded, MCP wrapper stripped
- **Binary files**: skipped + reported (not downloaded)
- **Directory structure**: preserved verbatim (including spaces + literal `&` in filenames)
- **`_ds/` tree**: included by default (Design System tokens, styles, etc.)

### .design-sync-manifest.md

Written to **cwd** (sibling to out_dir, NOT inside mirrored tree) on every sync:
- Project URL + ID
- UTC timestamp
- Table of every file: path, actual byte size written, **full sha256**, **etag**, status

Status markers: `✓` (written), `binary` (skipped), `unsafe` (traversal attempt), `FAILED`

### Re-sync Semantics

On subsequent runs to the same `out_dir`:
- Idempotent overwrite (existing files replaced)
- **No automatic diff detection** or stale-file removal
- Full tree rewrite each time
- Manifest shows current sync state, not delta vs prior

## Gitignore

**IMPORTANT**: The consuming worktree should gitignore the output dir. Add to `.gitignore`:

```gitignore
# Claude Design sync (ephemeral local mirrors)
.design-mocks/
.design-sync-manifest.md
```

## Deployment Requirements (operator responsibility)

These are **runtime/deployment properties, not agent-config**. The `design_sync`
local python tool needs, at execution time:

1. **Network egress to `api.anthropic.com`** — it downloads via raw JSON-RPC to
   `https://api.anthropic.com/v1/design/mcp`. There is NO agent-config field that
   grants this: `spec.tools.sandbox` only accepts `container_image` /
   `docker_image` / `runtime` (a `type: none` there is silently ignored), and it
   governs the tool's execution sandbox, not the host network policy. If the host
   sandboxes local tools without egress, the download fails — that is the
   **operator's** environment to provision, not something this spec can assert.
2. **Read access to `~/.claude/.credentials.json`** — the tool reads
   `claudeAiOauth.accessToken` for the bearer. The host must run the tool as a
   principal that can read this file, with a non-expired token
   (`omnigent setup` refreshes it).

If either is missing the tool returns a structured error (no crash), but the
sync will not produce files. Verify both in your target deployment before relying
on the agent.

## Known Risks & Environment Dependencies

1. **Sandbox egress (HOST-DEPENDENT)**: The python tool makes outbound HTTPS to `api.anthropic.com`. On hosts with strict `bwrap`/`srt` sandboxing that denies egress, the tool will fail. This is a deployment property (see **Deployment Requirements** above), not an agent-config setting — `spec.tools.sandbox` does not control host egress.

2. **Bearer token expiry**: If `~/.claude/.credentials.json` has an expired `accessToken`, the tool fails with HTTP 401. Run `omnigent setup` to refresh. Token availability depends on authentication state.

3. **Byte-exact validation**: Tool validates `len(written_bytes) == size` from `list_files` EXACTLY (no tolerance). If MCP's `size` field is stale, sync fails with size mismatch error.

4. **Binary file detection**: Tool skips files where `read_file` response contains text "binary file" or "stored base64". If a binary file is served without these markers, it may be written but corrupted.

5. **HTML entity sentinel collision**: If file content contains the exact sentinel string used for entity decoding, tool raises ValueError. Extremely unlikely but possible.

6. **No incremental sync**: Every run rewrites the full tree. For large projects, this is wasteful but guarantees consistency.

7. **Model availability (DEPLOYMENT-DEPENDENT)**: `executor.model: claude-haiku-4-5` must be available to the configured provider. If model unavailable, agent load fails.

8. **Live full-session run is a DEPLOY-TIME gate**: Config-load + tool-build + policy-construction are verified offline (see below). A live, zero-elicitation full-session run under a real `claude_sdk` executor cannot be exercised from a build/review sub-agent (it can't spawn omnigent sessions) — it is the operator's final check at deploy time.

## Acceptance Gate Evidence

Run the tests under `tests/`. See the commit message for pasted output. They prove:
1. **Assembled agent loads + enforces** (`tests/acceptance_gate.py`) via the REAL loader:
   `parse()` → 0 validation errors; `ToolManager(spec, workdir)` builds 19 tools
   INCLUDING `design_sync`; `os_env` absent; `resolve_function_policy` constructs the
   `deny_all_mutations` guardrail without raising; and a deny/abstain matrix asserts the
   policy DENIES all 7 mutation-capable builtins (incl. `sys_agent_download` +
   `sys_cancel_task`) and ABSTAINS on `design_sync` + all 11 read-only builtins, with the
   classified set proven equal to the 19 registered tools.
2. **Transport/fidelity proof** (`tests/self_test_download.py`): byte-exact sha256
   over all text files, binaries skipped + reported, `_ds/` present, manifest with
   full sha + etag — importing the REAL production functions.
3. **TOCTOU symlink-escape regression** (`tests/test_toctou.py`): a mirror sub-dir is
   swapped for a symlink to an outside dir between the pre-check and the write; asserts
   NO bytes escape `out_dir`, the file is reported unsafe, and the sync still completes.
4. **Entity-sentinel collision regression** (`tests/test_sentinel.py`): content
   containing the former decode sentinel round-trips byte-exact and does not abort.
5. **Unit tests passing** (`tests/test_design_sync.py`, imports real production functions).
6. **Live zero-elicitation full-session run is a DEPLOY-TIME gate** — a build/review
   sub-agent cannot spawn omnigent sessions, so it is NOT claimed here; it is the
   operator's final check (see Known Risks #8).
