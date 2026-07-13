"""
Claude Design project download tool.

Downloads all files from a claude.ai/design project via JSON-RPC to the
Anthropic Design MCP endpoint, preserving exact bytes and directory structure.
"""

import json
import os
import urllib.request
import urllib.error
import html
import hashlib
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

from omnigent_client import tool


def _read_bearer_token() -> str:
    """Read Claude.ai OAuth bearer token from credentials file."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Credentials file not found: {creds_path}\n"
            "Run `omnigent setup` to authenticate."
        )

    with open(creds_path) as f:
        creds = json.load(f)

    token = creds.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise ValueError("No accessToken found in credentials file")

    return token


def _jsonrpc_call(
    bearer: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    request_id: int = 1
) -> Any:
    """Make a JSON-RPC call to the Anthropic Design MCP endpoint."""
    endpoint = "https://api.anthropic.com/v1/design/mcp"

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {}
    }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.load(resp)

            if "error" in result:
                error_info = result['error']
                # Redact error details to prevent leakage to model
                error_code = error_info.get('code', 'unknown')
                raise RuntimeError(f"JSON-RPC error (code {error_code})")

            return result.get("result")
    except urllib.error.HTTPError as e:
        # Redact HTTP error bodies
        raise RuntimeError(f"HTTP {e.code} error")


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
            content = content[tag_end + 1:]
            # Remove single newline after opening tag (wrapper artifact)
            if content.startswith("\n"):
                content = content[1:]

    # Strip closing tag + trailing note
    close_tag = "</untrusted-project-content>"
    close_pos = content.find(close_tag)
    if close_pos != -1:
        # Remove single newline before closing tag (wrapper artifact)
        if close_pos > 0 and content[close_pos - 1] == "\n":
            content = content[:close_pos - 1]
        else:
            content = content[:close_pos]

    return content


def _html_entity_decode(text: str) -> str:
    """Decode HTML entities, with &amp; decoded LAST to avoid double-decode."""
    # Guard against sentinel collision (unlikely but possible)
    sentinel = "\x00AMPERSAND_PLACEHOLDER_e4b9c2a1\x00"
    if sentinel in text:
        raise ValueError("Content contains entity-decode sentinel (collision)")

    # First pass: decode everything except &amp;
    text = text.replace("&amp;", sentinel)
    text = html.unescape(text)
    # Second pass: decode &amp;
    text = text.replace(sentinel, "&")
    return text


def _list_all_files(bearer: str, project_id: str, include_ds: bool) -> List[Dict[str, Any]]:
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
            bearer,
            "tools/call",
            {
                "name": "list_files",
                "arguments": args
            },
            request_id=req_id
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
                        all_files.append({
                            "path": path,
                            "size": entry.get("size", 0),
                            "etag": entry.get("etag", ""),
                            "mimeType": ""
                        })
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
                    all_files.append({
                        "path": path,
                        "size": resource.get("size", 0),
                        "etag": resource.get("annotations", {}).get("etag", ""),
                        "mimeType": resource.get("mimeType", "")
                    })

    return all_files


def _read_file_content(
    bearer: str,
    project_id: str,
    path: str,
    request_id: int
) -> Tuple[Optional[bytes], bool, Optional[str]]:
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
        {
            "name": "read_file",
            "arguments": {"project_id": project_id, "path": path}
        },
        request_id=request_id
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
    Check if target_path is safe (stays within base_dir).

    Rejects: absolute paths, .., symlinks that escape, etc.
    """
    try:
        # Resolve both to absolute paths
        base_abs = base_dir.resolve()
        target_abs = (base_dir / target_path).resolve()

        # Check if target is within base
        return target_abs.is_relative_to(base_abs)
    except (ValueError, OSError):
        return False


@tool
def design_sync(
    project_url: str,
    out_dir: str = ".design-mocks",
    include_ds: bool = True
) -> Dict[str, Any]:
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

    # Resolve out_dir and check containment
    out_path = (cwd / out_dir).resolve()
    try:
        out_path.relative_to(cwd)
    except ValueError:
        return {"error": f"out_dir escapes cwd (tried: {out_dir})"}

    # Read bearer token
    try:
        bearer = _read_bearer_token()
    except Exception as e:
        return {"error": f"Failed to read bearer token: {e}"}

    # Initialize MCP session
    try:
        _jsonrpc_call(
            bearer,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "design-sync-agent", "version": "1.0.0"}
            },
            request_id=1
        )
    except Exception as e:
        return {"error": f"MCP initialization failed: {e}"}

    # List all files
    try:
        files = _list_all_files(bearer, project_id, include_ds)
    except Exception as e:
        return {"error": f"Failed to list files: {e}"}

    # Prepare output directory
    out_path.mkdir(parents=True, exist_ok=True)

    # Download each file
    files_written = []
    skipped_binaries = []
    skipped_unsafe = []
    failures = []
    total_bytes = 0
    req_id = 1000

    for file_info in files:
        path = file_info["path"]
        expected_size = file_info["size"]
        etag = file_info["etag"]

        # Path containment check
        if not _is_path_safe(out_path, path):
            skipped_unsafe.append(path)
            continue

        # Read file content
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
                failures.append({
                    "path": path,
                    "error": f"Size mismatch: expected {expected_size}, got {actual_size} (retry failed)"
                })
                continue

            actual_size = len(content_bytes)

            if expected_size != actual_size:
                failures.append({
                    "path": path,
                    "error": f"Size mismatch: expected {expected_size}, got {actual_size}"
                })
                continue

        # Validate then write (unique temp → verify → atomic rename)
        file_path = out_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Use unique temp file in same directory
        temp_fd, temp_path_str = tempfile.mkstemp(
            suffix=".tmp",
            prefix=f".design-sync-{file_path.name}-",
            dir=file_path.parent
        )
        temp_path = Path(temp_path_str)

        try:
            # Write to temp
            os.write(temp_fd, content_bytes)
            os.close(temp_fd)

            # Verify size
            if temp_path.stat().st_size != actual_size:
                raise RuntimeError(f"Write verification failed: expected {actual_size} bytes")

            # Atomic rename
            temp_path.rename(file_path)

            sha256 = hashlib.sha256(content_bytes).hexdigest()
            files_written.append({
                "path": path,
                "size_bytes": actual_size,
                "sha256": sha256,
                "etag": etag
            })
            total_bytes += actual_size

        except Exception as e:
            # Cleanup on failure
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except:
                pass
            failures.append({"path": path, "error": f"Write failed: {e}"})
            continue

    # Write sync manifest OUTSIDE mirrored namespace (sibling to out_dir, not inside it)
    timestamp = datetime.now(timezone.utc).isoformat()
    manifest_lines = [
        f"# Design Sync Manifest\n",
        f"\n",
        f"**Project:** {project_url}\n",
        f"**Project ID:** {project_id}\n",
        f"**Synced:** {timestamp}\n",
        f"**Include _ds/:** {include_ds}\n",
        f"\n",
        f"## Files\n",
        f"\n",
        f"| Path | Bytes | SHA256 | ETag | Status |\n",
        f"|------|-------|--------|------|--------|\n",
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
            f"| `{path_escaped}` | {size_display} | `{sha256_escaped}` | `{etag_escaped}` | {status} |\n"
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
        "timestamp": timestamp
    }
