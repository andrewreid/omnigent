"""
Claude Design project download tool.

Downloads all files from a claude.ai/design project via JSON-RPC to the
Anthropic Design MCP endpoint, preserving exact bytes and directory structure.
"""

import contextlib
import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omnigent_client import tool


def _read_bearer_token() -> str:
    """Read Claude.ai OAuth bearer token from credentials file."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {creds_path}\nRun `omnigent setup` to authenticate."
        )

    with open(creds_path) as f:
        creds = json.load(f)

    token = creds.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise ValueError("No accessToken found in credentials file")

    return token


def _jsonrpc_call(
    bearer: str, method: str, params: dict[str, Any] | None = None, request_id: int = 1
) -> Any:
    """Make a JSON-RPC call to the Anthropic Design MCP endpoint."""
    endpoint = "https://api.anthropic.com/v1/design/mcp"

    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.load(resp)

            if "error" in result:
                error_info = result["error"]
                # Redact error details to prevent leakage to model
                error_code = error_info.get("code", "unknown")
                raise RuntimeError(f"JSON-RPC error (code {error_code})")

            return result.get("result")
    except urllib.error.HTTPError as e:
        # Redact HTTP error bodies; `from None` drops the chained
        # HTTPError so its (possibly sensitive) body never surfaces.
        raise RuntimeError(f"HTTP {e.code} error") from None


def _strip_mcp_wrapper(content: str) -> str:
    """
    Strip ONLY the MCP transport wrapper tags.

    Design MCP wraps content in:
      <untrusted-project-content path="..." etag="...">
        [file content]
      </untrusted-project-content>
      (The body above is HTML-entity-escaped: ...)

    Strip the opening tag, closing tag, and trailing note. Preserve ALL file
    content including leading/trailing whitespace.
    """
    # Strip <untrusted-project-content> opening tag
    if content.startswith("<untrusted-project-content"):
        tag_end = content.find(">")
        if tag_end != -1:
            content = content[tag_end + 1 :]
            # Remove single newline after opening tag (wrapper artifact)
            if content.startswith("\n"):
                content = content[1:]

    # Strip closing tag + trailing note
    close_tag = "</untrusted-project-content>"
    close_pos = content.find(close_tag)
    if close_pos != -1:
        # Remove single newline before closing tag (wrapper artifact)
        if close_pos > 0 and content[close_pos - 1] == "\n":
            content = content[: close_pos - 1]
        else:
            content = content[:close_pos]

    return content


def _html_entity_decode(text: str) -> str:
    """
    Decode HTML entities in a SINGLE left-to-right pass.

    The MCP wrapper escapes file bytes once (``&`` -> ``&amp;``, ``<`` ->
    ``&lt;`` ...). A naive two-pass ``&amp;``-last scheme needs a magic
    sentinel to protect already-decoded ``&`` from re-scanning, and any file
    whose real content contains that sentinel string would raise — crashing
    the whole sync (round-4 BLOCKER 3).

    Instead we match each entity token with a regex and decode it exactly
    ONCE via ``html.unescape``. Because the callback output is never re-scanned,
    ``&amp;lt;`` correctly yields ``&lt;`` (not ``<``) and a bare ``&`` is left
    untouched — with no sentinel, so no possible content collision.
    """
    return _ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), text)


# Named (&amp;), decimal (&#39;) and hex (&#x2014;) HTML entity tokens.
_ENTITY_RE = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def _list_all_files(bearer: str, project_id: str, include_ds: bool) -> list[dict[str, Any]]:
    """Recursively list all files in the project, walking each directory."""
    all_files = []
    dirs_to_walk = [""]  # Start with root
    visited = set()  # Prevent cycles
    req_id = 100

    while dirs_to_walk:
        current_dir = dirs_to_walk.pop(0)

        # Skip if already visited (cycle detection)
        if current_dir in visited:
            continue
        visited.add(current_dir)

        args = {"project_id": project_id}
        if current_dir:
            args["path"] = current_dir

        result = _jsonrpc_call(
            bearer, "tools/call", {"name": "list_files", "arguments": args}, request_id=req_id
        )
        req_id += 1

        # Parse response — may be JSON-encoded text or structured response
        for item in result.get("content", []):
            # JSON-encoded text response
            if item.get("type") == "text":
                text_content = item.get("text", "")

                # Try parsing as JSON list
                try:
                    file_list = json.loads(text_content)
                except (json.JSONDecodeError, TypeError):
                    # Not JSON — check if it's an error message
                    if "error" in text_content.lower() or item.get("isError"):
                        continue
                    file_list = []

                # Handle both list and dict responses
                if isinstance(file_list, dict):
                    file_list = file_list.get("files", [])

                for entry in file_list:
                    path = entry.get("path", "")
                    file_type = entry.get("type", "")

                    if not path:
                        continue

                    # Skip _ds/ if requested
                    if not include_ds and (path == "_ds" or path.startswith("_ds/")):
                        continue

                    if file_type == "directory":
                        dirs_to_walk.append(path)
                    elif file_type == "file":
                        all_files.append(
                            {
                                "path": path,
                                "size": entry.get("size", 0),
                                "etag": entry.get("etag", ""),
                                "mimeType": "",
                            }
                        )
                continue

            # Fallback: resource format
            if item.get("type") == "resource":
                resource = item.get("resource", {})
                path = resource.get("uri", "").replace("file://", "")
                is_dir = resource.get("mimeType") == "application/x-directory"

                if not path:
                    continue

                if not include_ds and (path == "_ds" or path.startswith("_ds/")):
                    continue

                if is_dir:
                    dirs_to_walk.append(path)
                else:
                    all_files.append(
                        {
                            "path": path,
                            "size": resource.get("size", 0),
                            "etag": resource.get("annotations", {}).get("etag", ""),
                            "mimeType": resource.get("mimeType", ""),
                        }
                    )

    return all_files


def _read_file_content(
    bearer: str, project_id: str, path: str, request_id: int
) -> tuple[bytes | None, bool, str | None]:
    """
    Read file content via MCP.

    Returns: (content_bytes, is_binary, error_msg)
    - For text files: (content_bytes, False, None)
    - For binary files: (None, True, None)
    - For errors: (None, False, error_msg)
    """
    result = _jsonrpc_call(
        bearer,
        "tools/call",
        {"name": "read_file", "arguments": {"project_id": project_id, "path": path}},
        request_id=request_id,
    )

    # Check result-level error
    if result.get("isError"):
        error_content = str(result.get("content", "Unknown error"))
        # Check if it's a binary file error
        if "binary file" in error_content.lower() or "stored base64" in error_content.lower():
            return None, True, None
        return None, False, "Read error"

    # Collect all text blocks
    text_blocks = []
    is_binary_file = False
    error_msg = None

    for item in result.get("content", []):
        # Error response (check BEFORE text processing)
        if item.get("isError"):
            error_text = item.get("text", "Unknown error")
            # Binary file indicator
            if "binary file" in error_text.lower() or "stored base64" in error_text.lower():
                is_binary_file = True
                break
            error_msg = "Read error"
            break

        # Text content
        if item.get("type") == "text":
            text_blocks.append(item.get("text", ""))

    # Binary detection AFTER processing all blocks
    if is_binary_file:
        return None, True, None

    if error_msg:
        return None, False, error_msg

    if not text_blocks:
        return None, False, "No content in response"

    # Concatenate all text blocks
    full_content = "".join(text_blocks)

    # Strip wrapper + decode entities
    content = _strip_mcp_wrapper(full_content)
    content = _html_entity_decode(content)

    # Return as bytes for exact size validation
    return content.encode("utf-8"), False, None


def _is_path_safe(base_dir: Path, target_path: str) -> bool:
    """
    Cheap PRE-CHECK that target_path stays within base_dir.

    Rejects absolute paths and ``..`` escapes. This is only an early reject —
    it resolves paths at check time and therefore cannot defend against a
    symlink swapped in AFTER the check but BEFORE the write (TOCTOU). The real
    defense is the descriptor-relative, ``O_NOFOLLOW`` write in
    :func:`_write_file_contained`.
    """
    try:
        # Resolve both to absolute paths
        base_abs = base_dir.resolve()
        target_abs = (base_dir / target_path).resolve()

        # Check if target is within base
        return target_abs.is_relative_to(base_abs)
    except (ValueError, OSError):
        return False


class UnsafePathError(Exception):
    """Raised when a path component is (or became) a symlink at write time."""


def _write_file_contained(
    base_fd: int, rel_path: str, content_bytes: bytes, tmp_suffix: str
) -> None:
    """
    Write ``content_bytes`` to ``base_fd/<rel_path>`` WITHOUT ever following a
    symlink — the TOCTOU-safe write (round-4 BLOCKER 2).

    Every parent component is opened relative to the previous directory's file
    descriptor with ``O_DIRECTORY | O_NOFOLLOW``. If an attacker swaps any
    component for a symlink between the pre-check and this write, ``os.open``
    raises ``ELOOP`` and we abort that file with :class:`UnsafePathError`
    rather than writing through the link. The temp file is created with
    ``O_CREAT | O_EXCL | O_NOFOLLOW`` in the verified parent dir fd, its size
    is checked by fd, then it is atomically renamed into place — both operands
    anchored to the same verified directory fd — so the final bytes can only
    land inside ``base_fd``.

    :raises UnsafePathError: If any component is a symlink / not a directory.
    :raises OSError: On any other filesystem error.
    """
    parts = [p for p in rel_path.split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafePathError(f"empty path: {rel_path!r}")

    dir_parts, filename = parts[:-1], parts[-1]

    # Walk parents descriptor-relative; never follow a symlink.
    fds_to_close: list[int] = []
    cur_fd = base_fd
    try:
        for comp in dir_parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(comp, mode=0o755, dir_fd=cur_fd)
            try:
                next_fd = os.open(
                    comp,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=cur_fd,
                )
            except OSError as e:
                # ELOOP (symlink) or ENOTDIR → containment breach.
                raise UnsafePathError(
                    f"unsafe component {comp!r} in {rel_path!r}: {e.strerror}"
                ) from None
            fds_to_close.append(next_fd)
            cur_fd = next_fd

        tmp_name = f".design-sync-{filename}-{tmp_suffix}.tmp"

        # Create temp exclusively in the verified parent dir; O_NOFOLLOW so an
        # attacker-planted symlink at this name is rejected, O_EXCL so we never
        # clobber an existing entry.
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=cur_fd,
        )
        wrote_temp = True
        try:
            os.write(tmp_fd, content_bytes)
            written = os.fstat(tmp_fd).st_size
            os.close(tmp_fd)
            tmp_fd = -1
            if written != len(content_bytes):
                raise OSError(f"short write: {written} != {len(content_bytes)}")
            # Atomic rename anchored to the verified dir fd on BOTH sides.
            os.rename(tmp_name, filename, src_dir_fd=cur_fd, dst_dir_fd=cur_fd)
            wrote_temp = False
        finally:
            if tmp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            if wrote_temp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name, dir_fd=cur_fd)
    finally:
        for fd in fds_to_close:
            with contextlib.suppress(OSError):
                os.close(fd)


@tool
def design_sync(
    project_url: str, out_dir: str = ".design-mocks", include_ds: bool = True
) -> dict[str, Any]:
    """
    Download all text files from a Claude Design project to local directory.

    Downloads text files only (binaries are skipped and reported). Preserves
    exact bytes and directory structure. No automatic diff detection or
    stale-file removal — full tree rewrite each sync.

    Args:
        project_url: Full URL like https://claude.ai/design/p/<id>
        out_dir: Local output directory relative to cwd (default: .design-mocks)
        include_ds: Include Design System assets from _ds/ (default: True)

    Returns:
        Structured summary with:
        - files_written: list of {path, size_bytes, sha256, etag}
        - skipped_binaries: list of paths
        - skipped_unsafe: list of paths (traversal attempts)
        - failures: list of {path, error}
        - manifest_path: path to .design-sync-manifest.md
        - total_bytes: sum of written bytes
    """
    # Extract project ID from URL
    if "/p/" not in project_url:
        return {"error": "Invalid project URL (expected /p/<id>)"}

    project_id = project_url.split("/p/")[1].split("/")[0].split("?")[0]

    # Resolve cwd and validate out_dir containment
    cwd = Path.cwd().resolve()

    # Reject absolute out_dir
    if Path(out_dir).is_absolute():
        return {"error": f"out_dir must be relative (got absolute path: {out_dir})"}

    # Resolve out_dir and check containment (must stay strictly INSIDE cwd)
    out_path = (cwd / out_dir).resolve()
    try:
        rel = out_path.relative_to(cwd)
    except ValueError:
        return {"error": f"out_dir escapes cwd (tried: {out_dir})"}

    # Reject out_dir that IS the cwd / repo-root itself (e.g. '.', './',
    # '', 'foo/..'). Writing the mirror into the working tree root would
    # scatter downloaded files across the product tree. Require a distinct
    # sub-directory so the mirror is contained and gitignorable.
    if out_path == cwd or str(rel) == ".":
        return {
            "error": (
                "out_dir must be a sub-directory, not the working directory / "
                f"repo root itself (tried: {out_dir!r})"
            )
        }

    # Read bearer token
    try:
        bearer = _read_bearer_token()
    except Exception as e:  # noqa: BLE001 — tool must return a structured error, never crash the session
        return {"error": f"Failed to read bearer token: {e}"}

    # Initialize MCP session
    try:
        _jsonrpc_call(
            bearer,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "design-sync-agent", "version": "1.0.0"},
            },
            request_id=1,
        )
    except Exception as e:  # noqa: BLE001 — tool must return a structured error, never crash the session
        return {"error": f"MCP initialization failed: {e}"}

    # List all files
    try:
        files = _list_all_files(bearer, project_id, include_ds)
    except Exception as e:  # noqa: BLE001 — tool must return a structured error, never crash the session
        return {"error": f"Failed to list files: {e}"}

    # Prepare output directory
    out_path.mkdir(parents=True, exist_ok=True)

    # Open the base directory descriptor ONCE, refusing to follow a symlink at
    # out_dir itself. All per-file writes are anchored to this fd so nothing
    # can escape the mirror even if the tree is tampered with mid-sync.
    try:
        base_fd = os.open(str(out_path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        return {"error": f"out_dir is not a real directory (symlink?): {e.strerror}"}

    # Download each file
    files_written = []
    skipped_binaries = []
    skipped_unsafe = []
    failures = []
    total_bytes = 0
    req_id = 1000

    try:
        for file_info in files:
            path = file_info["path"]
            expected_size = file_info["size"]
            etag = file_info["etag"]

            # Cheap pre-check (early reject); real defense is the write below.
            if not _is_path_safe(out_path, path):
                skipped_unsafe.append(path)
                continue

            # Per-file guard: a single bad file NEVER aborts the whole sync
            # (round-4 BLOCKER 3). All read/decode/write errors are recorded.
            try:
                content_bytes, is_binary, error = _read_file_content(
                    bearer, project_id, path, req_id
                )
                req_id += 1

                if is_binary:
                    skipped_binaries.append(path)
                    continue

                if error:
                    failures.append({"path": path, "error": error})
                    continue

                if content_bytes is None:
                    failures.append({"path": path, "error": "No content returned"})
                    continue

                actual_size = len(content_bytes)

                # EXACT size check (no tolerance)
                if expected_size != actual_size:
                    # Retry once
                    content_bytes, is_binary, error = _read_file_content(
                        bearer, project_id, path, req_id
                    )
                    req_id += 1

                    if error or is_binary or content_bytes is None:
                        failures.append(
                            {
                                "path": path,
                                "error": (
                                    f"Size mismatch: expected {expected_size}, "
                                    f"got {actual_size} (retry failed)"
                                ),
                            }
                        )
                        continue

                    actual_size = len(content_bytes)

                    if expected_size != actual_size:
                        failures.append(
                            {
                                "path": path,
                                "error": (
                                    f"Size mismatch: expected {expected_size}, got {actual_size}"
                                ),
                            }
                        )
                        continue

                # TOCTOU-safe descriptor-relative write.
                _write_file_contained(base_fd, path, content_bytes, str(req_id))

                sha256 = hashlib.sha256(content_bytes).hexdigest()
                files_written.append(
                    {"path": path, "size_bytes": actual_size, "sha256": sha256, "etag": etag}
                )
                total_bytes += actual_size

            except UnsafePathError as e:
                # Symlink / containment breach detected AT WRITE TIME.
                skipped_unsafe.append(path)
                failures.append({"path": path, "error": f"Unsafe path: {e}"})
            except Exception as e:  # noqa: BLE001 — per-file failure recorded, never crashes the sync
                failures.append({"path": path, "error": f"Failed: {e}"})
    finally:
        with contextlib.suppress(OSError):
            os.close(base_fd)

    # Write sync manifest OUTSIDE mirrored namespace (sibling to out_dir, not inside it)
    timestamp = datetime.now(timezone.utc).isoformat()
    manifest_lines = [
        "# Design Sync Manifest\n",
        "\n",
        f"**Project:** {project_url}\n",
        f"**Project ID:** {project_id}\n",
        f"**Synced:** {timestamp}\n",
        f"**Include _ds/:** {include_ds}\n",
        "\n",
        "## Files\n",
        "\n",
        "| Path | Bytes | SHA256 | ETag | Status |\n",
        "|------|-------|--------|------|--------|\n",
    ]

    for file_info in files:
        path = file_info["path"]

        # Find status
        written = next((f for f in files_written if f["path"] == path), None)
        if written:
            status = "✓"
            size_display = written["size_bytes"]
            sha256_display = written["sha256"]
            etag_display = written["etag"]
        elif path in skipped_binaries:
            status = "binary"
            size_display = file_info["size"]
            sha256_display = "—"
            etag_display = file_info["etag"]
        elif path in skipped_unsafe:
            status = "unsafe"
            size_display = "—"
            sha256_display = "—"
            etag_display = "—"
        else:
            status = "FAILED"
            size_display = "—"
            sha256_display = "—"
            etag_display = file_info["etag"]

        # Escape markdown special chars
        path_escaped = path.replace("|", "\\|").replace("`", "\\`")
        sha256_escaped = str(sha256_display).replace("|", "\\|")
        etag_escaped = str(etag_display).replace("|", "\\|")

        manifest_lines.append(
            f"| `{path_escaped}` | {size_display} | `{sha256_escaped}` | "
            f"`{etag_escaped}` | {status} |\n"
        )

    manifest_path = cwd / ".design-sync-manifest.md"
    manifest_path.write_text("".join(manifest_lines))

    return {
        "project_url": project_url,
        "project_id": project_id,
        "out_dir": str(out_path),
        "files_written": files_written,
        "skipped_binaries": skipped_binaries,
        "skipped_unsafe": skipped_unsafe,
        "failures": failures,
        "manifest_path": str(manifest_path),
        "total_bytes": total_bytes,
        "timestamp": timestamp,
    }
