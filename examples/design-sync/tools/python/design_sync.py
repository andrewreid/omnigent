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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

# Import the @tool decorator from omnigent
try:
    from omnigent import tool
except ImportError:
    # Fallback if running standalone
    def tool(func):
        return func


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
    params: Optional[Dict[str, Any]] = None
) -> Any:
    """Make a JSON-RPC call to the Anthropic Design MCP endpoint."""
    endpoint = "https://api.anthropic.com/v1/design/mcp"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
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
                raise RuntimeError(f"JSON-RPC error: {result['error']}")

            return result.get("result")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}")


def _strip_wrapper_and_header(content: str) -> str:
    """
    Strip XML wrapper tags and trailing metadata note.

    Design MCP wraps content in:
      <untrusted-project-content path="..." etag="...">
        [file content]
      </untrusted-project-content>
      (The body above is HTML-entity-escaped: ...)

    Strip the opening tag, closing tag, and trailing note.
    """
    # Strip <untrusted-project-content> opening tag
    if content.startswith("<untrusted-project-content"):
        tag_end = content.find(">")
        if tag_end != -1:
            content = content[tag_end + 1:]

    # Strip closing tag + trailing note
    # Look for the closing tag, then remove everything from there
    close_tag = "</untrusted-project-content>"
    close_pos = content.find(close_tag)
    if close_pos != -1:
        content = content[:close_pos]

    # Strip leading/trailing whitespace from the unwrapped content
    content = content.strip()

    # Strip GENERATED/NOTE header (first line if it's a comment)
    lines = content.splitlines(keepends=True)
    if lines:
        first = lines[0].strip()
        # Check for various comment formats
        is_header = (
            (first.startswith("<!--") or first.startswith("//")) and
            ("GENERATED" in first or "NOTE" in first or "do not edit" in first.lower())
        )
        if is_header:
            content = "".join(lines[1:])

    # Final trim
    return content.strip()


def _html_entity_decode(text: str) -> str:
    """Decode HTML entities, with &amp; decoded LAST to avoid double-decode."""
    # First pass: decode everything except &amp;
    text = text.replace("&amp;", "\x00AMPERSAND\x00")
    text = html.unescape(text)
    # Second pass: decode &amp;
    text = text.replace("\x00AMPERSAND\x00", "&")
    return text


def _list_all_files(bearer: str, project_id: str, include_ds: bool) -> List[Dict[str, Any]]:
    """Recursively list all files in the project, walking each directory."""
    all_files = []
    dirs_to_walk = [""]  # Start with root

    while dirs_to_walk:
        current_dir = dirs_to_walk.pop(0)

        args = {"project_id": project_id}
        if current_dir:
            args["path"] = current_dir

        result = _jsonrpc_call(
            bearer,
            "tools/call",
            {
                "name": "list_files",
                "arguments": args
            }
        )

        # Parse response — may be resource list or JSON-encoded text
        for item in result.get("content", []):
            # Check for JSON-encoded text response
            if item.get("type") == "text":
                file_list = json.loads(item.get("text", "[]"))
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

            # Fallback: resource format (if MCP changes response structure)
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


def _read_file_content(bearer: str, project_id: str, path: str) -> Tuple[Optional[str], bool, Optional[str]]:
    """
    Read file content via MCP.

    Returns: (content, is_binary, error_msg)
    - For text files: (content_str, False, None)
    - For binary files: (None, True, None)
    - For errors: (None, False, error_msg)
    """
    result = _jsonrpc_call(
        bearer,
        "tools/call",
        {
            "name": "read_file",
            "arguments": {"project_id": project_id, "path": path}
        }
    )

    for item in result.get("content", []):
        if item.get("type") == "text":
            content = item.get("text", "")

            # Check if this is a binary file error message
            if "binary file" in content.lower() or "stored base64" in content.lower():
                return None, True, None

            # Strip wrapper + GENERATED header + decode entities
            content = _strip_wrapper_and_header(content)
            content = _html_entity_decode(content)
            return content, False, None

        # Binary file (thumbnail returned instead)
        if ".thumbnail" in str(item):
            return None, True, None

        # Error response
        if item.get("isError"):
            return None, False, item.get("text", "Unknown error")

    return None, False, "No content in response"


@tool
def design_sync(
    project_url: str,
    out_dir: str = ".design-mocks",
    include_ds: bool = True
) -> Dict[str, Any]:
    """
    Download all files from a Claude Design project to local directory.

    Args:
        project_url: Full URL like https://claude.ai/design/p/<id>
        out_dir: Local output directory (default: .design-mocks)
        include_ds: Include Design System assets from _ds/ (default: True)

    Returns:
        Structured summary with:
        - files_written: list of {path, size_bytes}
        - skipped_binaries: list of paths
        - failures: list of {path, error}
        - mapping_path: path to MAPPING.md
        - total_bytes: sum of written bytes
    """
    # Extract project ID from URL
    if "/p/" not in project_url:
        return {"error": "Invalid project URL (expected /p/<id>)"}

    project_id = project_url.split("/p/")[1].split("/")[0].split("?")[0]

    # Read bearer token
    try:
        bearer = _read_bearer_token()
    except Exception as e:
        return {"error": f"Failed to read bearer token: {e}"}

    # Initialize MCP session (no 'initialized' notification needed for JSON-RPC)
    try:
        _jsonrpc_call(
            bearer,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "design-sync-agent", "version": "1.0.0"}
            }
        )
    except Exception as e:
        return {"error": f"MCP initialization failed: {e}"}

    # List all files
    try:
        files = _list_all_files(bearer, project_id, include_ds)
    except Exception as e:
        return {"error": f"Failed to list files: {e}"}

    # Prepare output directory
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Download each file
    files_written = []
    skipped_binaries = []
    failures = []
    total_bytes = 0

    for file_info in files:
        path = file_info["path"]
        expected_size = file_info["size"]

        # Read file content
        content, is_binary, error = _read_file_content(bearer, project_id, path)

        if is_binary:
            skipped_binaries.append(path)
            continue

        if error:
            failures.append({"path": path, "error": error})
            continue

        if content is None:
            failures.append({"path": path, "error": "No content returned"})
            continue

        # Write to disk
        file_path = out_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        content_bytes = content.encode("utf-8")
        file_path.write_bytes(content_bytes)

        actual_size = len(content_bytes)

        # Size mismatch check (lenient: MCP's size may include wrapper overhead)
        # Only fail if delta > 200 bytes (wrapper is ~170-180 bytes, off-by-one is normal)
        size_delta = abs(actual_size - expected_size)
        if expected_size > 0 and size_delta > 200:
            # Retry once for large discrepancies
            content, is_binary, error = _read_file_content(bearer, project_id, path)
            if error or is_binary or content is None:
                failures.append({
                    "path": path,
                    "error": f"Size mismatch: expected {expected_size}, got {actual_size} (retry failed)"
                })
                continue

            content_bytes = content.encode("utf-8")
            file_path.write_bytes(content_bytes)
            actual_size = len(content_bytes)
            size_delta = abs(actual_size - expected_size)

            if size_delta > 200:
                failures.append({
                    "path": path,
                    "error": f"Size mismatch: expected {expected_size}, got {actual_size}"
                })
                continue

        files_written.append({"path": path, "size_bytes": actual_size})
        total_bytes += actual_size

    # Write MAPPING.md
    timestamp = datetime.now(timezone.utc).isoformat()
    mapping_lines = [
        f"# Design Sync Mapping\n",
        f"\n",
        f"**Project:** {project_url}\n",
        f"**Project ID:** {project_id}\n",
        f"**Synced:** {timestamp}\n",
        f"**Include _ds/:** {include_ds}\n",
        f"\n",
        f"## Files\n",
        f"\n",
        f"| Path | Size (bytes) | ETag |\n",
        f"|------|--------------|------|\n",
    ]

    for file_info in files:
        path = file_info["path"]
        size = file_info["size"]
        etag = file_info["etag"]

        # Status marker
        if path in skipped_binaries:
            status = "(binary, skipped)"
        elif any(f["path"] == path for f in failures):
            status = "(FAILED)"
        else:
            status = ""

        mapping_lines.append(f"| `{path}` {status} | {size} | `{etag}` |\n")

    mapping_path = out_path / "MAPPING.md"
    mapping_path.write_text("".join(mapping_lines))

    return {
        "project_url": project_url,
        "project_id": project_id,
        "out_dir": str(out_path),
        "files_written": files_written,
        "skipped_binaries": skipped_binaries,
        "failures": failures,
        "mapping_path": str(mapping_path),
        "total_bytes": total_bytes,
        "timestamp": timestamp
    }
