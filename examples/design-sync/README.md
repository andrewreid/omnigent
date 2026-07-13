## design-sync Agent

Download-only Claude Design sync agent. Downloads **text files only** from a `claude.ai/design` project to a local directory, preserving exact bytes and directory structure. Binaries are skipped and reported.

## Purpose

Pure read-only sync — never edits product code, runs gates, or opens PRs. Use to:
- Pull Design text artefacts into a consuming worktree for implementation reference
- Re-sync updated designs (no automatic diff detection — full tree rewrite each time)
- Mirror a Design project's text file tree locally

## Locked Recipe (DO NOT RE-LITIGATE)

This design is **proven** by 4 prior spikes + 2 cross-vendor review rounds. The ONLY working harness:

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
- Content is entity-decoded and wrapper-stripped in the python tool (not by the model)

### Why permission_mode: bypassPermissions?

- `permission_mode` OMITTED resolves to `auto`
- Set it EXPLICITLY to `bypassPermissions` to avoid elicitation prompts when invoked via managed Claude settings (claude.ai context)
- Headless/API-key runs may ignore this field based on caller's permission config

### Why tools.sandbox: none?

- Local python tools run under `spec.tools.sandbox`, NOT `os_env.sandbox`
- `tools.sandbox: none` allows the python tool to make outbound HTTPS to `api.anthropic.com` and read `~/.claude/.credentials.json`
- **Host-dependent**: on srt-enabled hosts with strict sandboxing, egress may still be denied — test with your sandbox config

### Download-only structural enforcement

- Agent has NO shell/git/gh tool access
- Hard DENY policies for push/PR/edit actions (not ASK)
- Exposed tool surface is exactly `{design_sync}` — no OS/shell/file-edit capability

## How to Launch

Via Omnigent runtime MCP tools (available in consuming sessions):

```python
# Create session for design-sync agent
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

## Known Risks & Environment Dependencies

1. **Sandbox egress (HOST-DEPENDENT)**: The python tool makes outbound HTTPS to `api.anthropic.com`. On hosts with strict `bwrap`/`srt` sandboxing that denies egress even with `tools.sandbox: none`, the tool will fail. Test with your sandbox config; adjust `tools.sandbox` if needed.

2. **Bearer token expiry**: If `~/.claude/.credentials.json` has an expired `accessToken`, the tool fails with HTTP 401. Run `omnigent setup` to refresh. Token availability depends on authentication state.

3. **Byte-exact validation**: Tool validates `len(written_bytes) == size` from `list_files` EXACTLY (no tolerance). If MCP's `size` field is stale, sync fails with size mismatch error.

4. **Binary file detection**: Tool skips files where `read_file` response contains text "binary file" or "stored base64". If a binary file is served without these markers, it may be written but corrupted.

5. **HTML entity sentinel collision**: If file content contains the exact sentinel string used for entity decoding, tool raises ValueError. Extremely unlikely but possible.

6. **No incremental sync**: Every run rewrites the full tree. For large projects, this is wasteful but guarantees consistency.

7. **Model availability (DEPLOYMENT-DEPENDENT)**: `executor.model: claude-haiku-4-5` must be available to the configured provider. If model unavailable, agent load fails.

8. **Permission mode (CONTEXT-DEPENDENT)**: `bypassPermissions` only applies when invoked via managed Claude settings (claude.ai context). Headless/API-key runs may ignore this field based on caller's permission config.

## Acceptance Gate Evidence

See commit message for full evidence of:
1. Agent loads via real omnigent local-tool loader (exposed tools == {design_sync}, no LocalToolLoadError)
2. Transport/fidelity proof (exact byte sha256 match, binaries skipped, _ds/ present, manifest with full sha+etag)
3. Unit tests passing (imports real production functions)
