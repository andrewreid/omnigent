# design-sync Agent

Download-only Claude Design artefact-sync agent. Downloads text + binary files from a `claude.ai/design` project to a local directory, preserving exact bytes, directory structure, and Design System assets (`_ds/`).

## Purpose

Pure read-only sync — never edits product code, runs gates, or opens PRs. Use to:
- Pull Design artefacts into a consuming worktree for implementation reference
- Re-sync updated designs (no automatic diff detection — full tree rewrite each time)
- Mirror a Design project's file tree locally

## Locked Recipe (DO NOT RE-LITIGATE)

This design is **proven** by 4 prior spikes + cross-vendor review. The ONLY working harness on this branch:

### Why claude-SDK only?

- **claude-NATIVE children** drop user python tools (relay allowlist `_NATIVE_RELAY_BUILTIN_TOOLS`) AND do not forward spec MCP servers → native is OUT.
- **claude-SDK children** DO see `tools/python/*.py` (bridged to model as `mcp__omnigent__<fn>`) and run tools with NO elicitation under bypass → this is the winning harness.

### Why inline JSON-RPC transport?

- The `claude-design` MCP has NO project→disk primitive:
  - `write_files.local_path` = not-implemented
  - `copy_files` = remote→remote only
  - `DesignSync` tool = upload, not download
- So download MUST be done in-process by our python tool via raw JSON-RPC to the SAME endpoint the MCP uses: `https://api.anthropic.com/v1/design/mcp`
- Bearer token read from `~/.claude/.credentials.json` (field `claudeAiOauth.accessToken`)
- Content is entity-decoded and wrapper-stripped — bytes never touch the model (no token overhead, byte-exact fidelity)

### Why permission_mode: bypassPermissions?

- `permission_mode` OMITTED resolves to `auto` (a wrong code comment at `workflow.py:1249` claims bypass)
- Set it EXPLICITLY to `bypassPermissions` to avoid elicitation prompts (only applies when invoked via managed Claude settings; headless/API-key runs ignore it)

### Why no `tools:` block or MCP server block?

- Tool auto-discovery from `tools/python/*.py` with `@tool` decorator is the ONLY working primitive for local python tools on this branch
- `type: function` + inline `callable:` is NOT parsed
- Transport is in-process → no MCP server block needed

## How to Launch

Via Omnigent runtime MCP tools (available in consuming sessions):

```python
# Create session for design-sync agent
session = sys_session_create(
    agent_id="design-sync",
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

Note: `working_directory` is set by the calling session's cwd (not a parameter to sys_session_create). The agent's cwd is inherited from caller.

## Inputs

The agent accepts natural language prompts referencing:
- **project_url** (required): Full URL like `https://claude.ai/design/p/<id>`
- **out_dir** (optional, default `.design-mocks`): Local output directory
- **include_ds** (optional, default `true`): Include Design System assets from `_ds/`

Examples:
- `"Download https://claude.ai/design/p/82bd9df4-843a-4b69-bb20-941fde27b040"`
- `"Sync project https://claude.ai/design/p/abc123 to ./mocks without _ds"`

## Output

All files written to `<out_dir>/` in the consuming worktree:
- Text files: exact bytes, HTML-entity-decoded, MCP wrapper stripped
- Binary files: skipped + reported (thumbnails not downloaded)
- Directory structure: preserved verbatim (including spaces + literal `&` in filenames)
- `_ds/` tree: included by default (Design System tokens, styles, etc.)

### sync-manifest.md

Written to `<out_dir>/sync-manifest.md` on every sync (non-colliding name):
- Project URL + ID
- UTC timestamp
- Table of every file: path, actual byte size written, sha256 prefix, status

### Re-sync Semantics

On subsequent runs to the same `out_dir`:
- Idempotent overwrite (existing files replaced)
- No automatic diff detection or stale-file removal
- Full tree rewrite each time

## Gitignore

**IMPORTANT**: The consuming worktree MUST gitignore the output dir. Add to `.gitignore`:

```gitignore
# Claude Design artefact sync (ephemeral local mirrors)
.design-mocks/
```

The agent itself just writes there — it's the consuming session's responsibility to ignore the output.

## Known Residual Risks & Environment Dependencies

1. **Sandbox egress**: The tool makes outbound HTTPS to `api.anthropic.com`. On hosts with strict `bwrap`/`srt` sandboxing that denies egress, the agent must run unsandboxed (`sandbox: type: none` in `config.yaml` — already set). This is host-dependent; test with your sandbox config.

2. **Bearer token expiry**: If `~/.claude/.credentials.json` has an expired `accessToken`, the tool fails with HTTP 401. Run `omnigent setup` to refresh. Token availability depends on authentication state.

3. **Byte-exact validation**: Tool validates `len(written_bytes) == size` from `list_files` EXACTLY (no tolerance). If MCP's `size` field is stale or includes encoding overhead, sync fails with size mismatch error.

4. **Binary file detection**: Tool skips files where `read_file` response contains text "binary file" or "stored base64", or returns `.thumbnail`. If a binary file is incorrectly served as text without these markers, it will be written but may be corrupted.

5. **HTML entity double-decode**: Tool decodes `&amp;` LAST to avoid double-decode. If upstream changes entity-encoding, this may break.

6. **No incremental sync**: Every run rewrites the full tree. For large projects, this is wasteful but guarantees consistency.

7. **Model availability**: `executor.model: claude-haiku-4-5` is required and must be available to the configured provider. If model unavailable, agent load fails.

8. **Permission mode**: `bypassPermissions` only applies when invoked via managed Claude settings (claude.ai context). Headless/API-key runs ignore this field and may elicit approval prompts based on caller's permission config.

## Acceptance Gate Evidence

See commit message for full evidence of:
1. Agent loads with tool registered (not no-op fallback)
2. Clean-state end-to-end run via sys_session_create on test project (zero elicitations, byte-exact, sync-manifest.md correct)
3. Unit tests passing (wrapper stripping, entity decode, path safety)
