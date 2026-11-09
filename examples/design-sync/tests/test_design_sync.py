#!/usr/bin/env python3
"""Tests for design_sync tool (imports real production functions)."""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import traceback
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest

# Import production functions
sys.path.insert(0, str(Path(__file__).parent.parent / "tools/python"))

# Stub omnigent_client for import
sys.modules["omnigent_client"] = type(sys)("omnigent_client")
sys.modules["omnigent_client"].tool = lambda f: f

import design_sync as ds  # noqa: E402
from design_sync import (  # noqa: E402 — sys.path + omnigent_client stub must precede import
    _html_entity_decode,
    _is_path_safe,
    _list_all_files,
    _strip_mcp_wrapper,
    _write_file_contained,
)


@pytest.fixture
def credentials_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return claude_dir / ".credentials.json"


def test_design_token_preferred(credentials_path):
    credentials_path.write_text(
        json.dumps(
            {
                "designOauth": {"accessToken": "design-token"},
                "claudeAiOauth": {"accessToken": "login-token"},
            }
        )
    )
    assert ds._read_bearer_token() == "design-token"


@pytest.mark.parametrize(
    "design_oauth", [{}, {"accessToken": ""}, {"accessToken": None}, {"accessToken": 123}, None]
)
def test_login_token_fallback(credentials_path, design_oauth):
    creds = {"claudeAiOauth": {"accessToken": "login-token"}}
    if design_oauth is not None:
        creds["designOauth"] = design_oauth
    credentials_path.write_text(json.dumps(creds))
    assert ds._read_bearer_token() == "login-token"


@pytest.mark.parametrize(
    "creds", [{}, {"designOauth": {"accessToken": ""}, "claudeAiOauth": {"accessToken": 123}}]
)
def test_missing_token_mentions_design_login(credentials_path, creds):
    credentials_path.write_text(json.dumps(creds))
    with pytest.raises(
        ValueError, match=r"No accessToken found in credentials file.* /design-login"
    ):
        ds._read_bearer_token()


def test_missing_credentials_file_keeps_message(credentials_path):
    with pytest.raises(FileNotFoundError) as exc:
        ds._read_bearer_token()
    assert str(exc.value) == (
        f"Credentials file not found: {credentials_path}\nRun `omnigent setup` to authenticate."
    )


@pytest.fixture
def http_error(monkeypatch):
    def install(body):
        stream = Mock(wraps=BytesIO(body))

        def fail(req, timeout):  # noqa: ARG001
            raise urllib.error.HTTPError(
                "https://api.anthropic.com/v1/design/mcp", 403, "private_token", {}, stream
            )

        monkeypatch.setattr(ds.urllib.request, "urlopen", fail)
        return stream

    return install


def test_http_design_scopes_error_is_surfaced(http_error):
    bearer = "private_token"
    http_error(json.dumps({"error": "needs_design_scopes", "prompt": "private_token"}).encode())
    with pytest.raises(RuntimeError) as exc:
        ds._jsonrpc_call(bearer, "initialize")
    assert str(exc.value) == "HTTP 403 error: needs_design_scopes"
    assert "private_token" not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON: private_token",
        b'\xff{"error": "needs_design_scopes"}',
        b'["private_token"]',
        b'{"prompt": "private_token"}',
        *[
            json.dumps({"error": error, "prompt": "private_token"}).encode()
            for error in ("Bearer private_token", "x" * 65, "bad\n", "private_token", 403, {})
        ],
    ],
)
def test_http_error_unsafe_bodies_are_redacted(http_error, body):
    bearer = "private_token"
    http_error(body)
    with pytest.raises(RuntimeError) as exc:
        ds._jsonrpc_call(bearer, "initialize")
    assert str(exc.value) == "HTTP 403 error"
    assert "private_token" not in "".join(traceback.format_exception(exc.value))


def test_http_error_body_read_is_bounded(http_error):
    stream = http_error(b'{"prompt": "' + b"x" * 8192 + b'", "error": "needs_design_scopes"}')
    with pytest.raises(RuntimeError, match=r"^HTTP 403 error$"):
        ds._jsonrpc_call("private_token", "initialize")
    stream.read.assert_called_once_with(4096)


def test_http_error_incomplete_read_is_redacted(http_error):
    stream = http_error(b"")
    stream.read.side_effect = http.client.IncompleteRead(b"private_token", 100)
    with pytest.raises(RuntimeError) as exc:
        ds._jsonrpc_call("private_token", "initialize")
    assert str(exc.value) == "HTTP 403 error"
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


def test_strip_mcp_wrapper():
    """Test MCP wrapper stripping preserves file content."""
    print("Testing _strip_mcp_wrapper...")

    # Preserves whitespace
    wrapped = (
        '<untrusted-project-content path="test.txt" etag="123">\n'
        "  \n"
        "content\n"
        "  \n"
        "</untrusted-project-content>\n"
        "(Note: body is escaped)"
    )
    result = _strip_mcp_wrapper(wrapped)
    assert result == "  \ncontent\n  ", f"Expected whitespace preserved, got: {result!r}"

    # Preserves legitimate comments
    wrapped = (
        '<untrusted-project-content path="script.sh" etag="456">\n'
        "#!/bin/bash\n"
        "# This is a real comment\n"
        'echo "test"\n'
        "</untrusted-project-content>"
    )
    result = _strip_mcp_wrapper(wrapped)
    assert result == '#!/bin/bash\n# This is a real comment\necho "test"', f"Got: {result!r}"

    # Preserves GENERATED markers
    wrapped = (
        '<untrusted-project-content path="gen.js" etag="789">\n'
        "// GENERATED by build.sh - do not edit\n"
        'export const VERSION = "1.0.0";\n'
        "</untrusted-project-content>"
    )
    result = _strip_mcp_wrapper(wrapped)
    expected = '// GENERATED by build.sh - do not edit\nexport const VERSION = "1.0.0";'
    assert result == expected, f"Got: {result!r}"

    # Empty file
    wrapped = (
        '<untrusted-project-content path="empty.txt" etag="000">\n</untrusted-project-content>'
    )
    result = _strip_mcp_wrapper(wrapped)
    assert result == "", f"Empty file should be empty, got: {result!r}"

    # No final newline preserved
    wrapped = (
        '<untrusted-project-content path="no-newline.txt" etag="111">\n'
        "content without trailing newline\n"
        "</untrusted-project-content>"
    )
    result = _strip_mcp_wrapper(wrapped)
    assert result == "content without trailing newline", f"Got: {result!r}"

    print("  ✓ All wrapper tests passed")


def test_html_entity_decode():
    """Test HTML entity decoding."""
    print("Testing _html_entity_decode...")

    # &amp; decoded last
    text = "&amp;lt; becomes &lt; and &amp; becomes &"
    result = _html_entity_decode(text)
    assert result == "&lt; becomes < and & becomes &", f"Got: {result!r}"

    # Mixed entities
    text = "Rock &amp; Roll &mdash; the &#39;60s"
    result = _html_entity_decode(text)
    assert result == "Rock & Roll — the '60s", f"Got: {result!r}"

    # Round-trip test
    original = "Test & < > \" ' content"
    # Simulate entity encoding (what MCP does)
    encoded = original.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    decoded = _html_entity_decode(encoded)
    assert decoded == original, f"Round-trip failed: {decoded!r}"

    print("  ✓ All entity decode tests passed")


def test_path_safety():
    """Test path traversal containment."""
    print("Testing _is_path_safe...")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Safe relative paths
        assert _is_path_safe(tmp_path, "foo/bar.txt")
        assert _is_path_safe(tmp_path, "a/b/c/d.txt")
        assert _is_path_safe(tmp_path, "_ds/tokens/colors.css")

        # Reject absolute paths
        assert not _is_path_safe(tmp_path, "/etc/passwd")
        assert not _is_path_safe(tmp_path, "/tmp/evil.txt")

        # Reject parent traversal
        assert not _is_path_safe(tmp_path, "../outside.txt")
        assert not _is_path_safe(tmp_path, "foo/../../outside.txt")
        assert not _is_path_safe(tmp_path, "a/b/../../../outside.txt")
        assert not _is_path_safe(tmp_path, "./../evil")

        # Reject symlink escape
        outside = tmp_path.parent / "outside_test"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "link"
        try:
            link.symlink_to(outside)
            assert not _is_path_safe(tmp_path, "link/evil.txt")
        finally:
            if link.exists():
                link.unlink()
            if outside.exists():
                outside.rmdir()

    print("  ✓ All path safety tests passed")


def test_list_errors_are_structured_failures():
    """Tool-level list errors must survive top-level, nested, and partial walks."""
    print("Testing list_files error propagation...")

    def run(responses):
        original = ds._jsonrpc_call

        def fake_call(bearer, method, params=None, request_id=1):  # noqa: ARG001
            path = params["arguments"].get("path", "")
            return responses[path]

        try:
            ds._jsonrpc_call = fake_call
            return _list_all_files("token", "project", True)
        finally:
            ds._jsonrpc_call = original

    files, failures = run({"": {"isError": True, "content": []}})
    assert files == []
    assert failures == [{"path": "/", "error": "List error"}]

    root_entries = [
        {"path": "ok.txt", "type": "file", "size": 2, "etag": "ok"},
        {"path": "broken", "type": "directory"},
    ]
    files, failures = run(
        {
            "": {"content": [{"type": "text", "text": json.dumps(root_entries)}]},
            "broken": {"isError": True, "content": []},
        }
    )
    assert [entry["path"] for entry in files] == ["ok.txt"]
    assert failures == [{"path": "broken", "error": "List error"}]

    files, failures = run(
        {
            "": {
                "content": [
                    {"type": "text", "text": json.dumps(root_entries[:1])},
                    {"type": "text", "isError": True, "text": "permission denied"},
                ]
            }
        }
    )
    assert [entry["path"] for entry in files] == ["ok.txt"]
    assert failures == [{"path": "/", "error": "List error"}]
    print("  ✓ top-level, subdirectory, and partial list failures preserved")


def test_resync_replaces_stale_entry_types():
    """Re-sync must replace stale file/dir entries without following symlinks."""
    print("Testing re-sync type changes...")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            (base / "foo").write_text("stale file")
            _write_file_contained(base_fd, "foo/bar.txt", b"new child", "file-to-dir")
            assert (base / "foo/bar.txt").read_bytes() == b"new child"

            (base / "reverse/old").mkdir(parents=True)
            (base / "reverse/old/stale.txt").write_text("stale child")
            _write_file_contained(base_fd, "reverse/old", b"new file", "dir-to-file")
            assert (base / "reverse/old").read_bytes() == b"new file"
            assert not list(base.rglob(".ds-*.old"))
        finally:
            os.close(base_fd)
    print("  ✓ file→dir and dir→file re-syncs passed")


def test_long_basename_uses_short_temp_name():
    """A valid near-limit basename must not be lengthened by the temp file."""
    print("Testing near-limit basename...")
    filename = "x" * 236 + ".txt"
    assert len(filename.encode()) == 240
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _write_file_contained(base_fd, filename, b"long-name", "long")
        finally:
            os.close(base_fd)
        assert (base / filename).read_bytes() == b"long-name"
        assert not list(base.glob(".ds-*.tmp"))
    print("  ✓ 240-byte basename synced")


def test_out_dir_requires_gitignored_mirror_namespace():
    """Repository metadata and tracked trees are rejected; ignored mirrors work."""
    print("Testing out_dir repository containment...")
    original_cwd = os.getcwd()
    original_token = ds._read_bearer_token
    original_rpc = ds._jsonrpc_call
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        (root / ".gitignore").write_text(".design-mocks/\n.design-sync-manifest.md\n")
        (root / ".github/workflows").mkdir(parents=True)
        (root / ".github/workflows/ci.yml").write_text("name: test\n")
        (root / "tracked").mkdir()
        (root / "tracked/source.py").write_text("print('product')\n")
        subprocess.run(["git", "add", ".gitignore", ".github", "tracked"], cwd=root, check=True)

        def fake_rpc(bearer, method, params=None, request_id=1):  # noqa: ARG001
            if method == "initialize":
                return {}
            return {"content": [{"type": "text", "text": "[]"}]}

        try:
            os.chdir(root)
            ds._read_bearer_token = lambda: "token"
            ds._jsonrpc_call = fake_rpc
            for rejected in (".git", ".github", "tracked"):
                result = ds.design_sync("https://claude.ai/design/p/project", rejected)
                assert "error" in result, f"{rejected} unexpectedly accepted"

            result = ds.design_sync("https://claude.ai/design/p/project", ".design-mocks")
            assert "error" not in result, result
            assert result["failures"] == []
            assert (root / ".design-mocks").is_dir()

            ds._jsonrpc_call = lambda bearer, method, params=None, request_id=1: (  # noqa: ARG005
                {} if method == "initialize" else {"isError": True, "content": []}
            )
            failed = ds.design_sync("https://claude.ai/design/p/project", ".design-mocks")
            assert failed["failures"] == [{"path": "/", "error": "List error"}]
            manifest = Path(failed["manifest_path"]).read_text()
            assert "## Listing failures" in manifest and "`/`: List error" in manifest
        finally:
            ds._read_bearer_token = original_token
            ds._jsonrpc_call = original_rpc
            os.chdir(original_cwd)
    print("  ✓ .git/.github/tracked rejected; ignored mirror accepted")


if __name__ == "__main__":
    try:
        test_strip_mcp_wrapper()
        test_html_entity_decode()
        test_path_safety()
        test_list_errors_are_structured_failures()
        test_resync_replaces_stale_entry_types()
        test_long_basename_uses_short_temp_name()
        test_out_dir_requires_gitignored_mirror_namespace()
        print("\n✓ All tests passed")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 — top-level test harness reports any failure and exits nonzero
        print(f"\n✗ Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
