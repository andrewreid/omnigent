#!/usr/bin/env python3
"""
Byte-exact download self-test for design_sync — importing the REAL production
functions (no re-implementation). Network + credentials are stubbed by
monkeypatching `_read_bearer_token` and `_jsonrpc_call`; everything downstream
(wrapper strip, entity decode, size validation, atomic temp write, manifest)
runs the real code path.

Proves:
  - text files written byte-exact (sha256 of on-disk bytes == sha256 of expected)
  - binary files skipped + reported (not written)
  - `_ds/` tree present
  - manifest lists full sha256 + etag per file

Run: uv run python examples/design-sync/tests/self_test_download.py
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# Stub omnigent_client so the @tool decorator import resolves standalone.
sys.modules["omnigent_client"] = type(sys)("omnigent_client")
sys.modules["omnigent_client"].tool = lambda f: f

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools/python"))
import design_sync as ds  # noqa: E402

# --- Fixtures: the EXACT bytes each text file must end up as on disk ---------
EXPECTED = {
    "index.html": "<h1>Rock & Roll</h1>\n<p>the '60s — <b>bold</b></p>\n",
    "readme.txt": "plain text, no entities, trailing newline\n",
    "_ds/tokens.css": ":root { --brand: #ff0066; }\n/* A & B */\n",
}
BINARY_PATHS = {"assets/logo.png"}


def _escape(body: str) -> str:
    """Inverse of the tool's entity decode (& last-out => &amp; here)."""
    return body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wrap(path: str, etag: str, body: str) -> str:
    """Rebuild the MCP transport wrapper the tool expects to strip."""
    return (
        f'<untrusted-project-content path="{path}" etag="{etag}">\n'
        f"{_escape(body)}\n"
        f"</untrusted-project-content>\n"
        f"(Note: the body above is HTML-entity-escaped.)"
    )


def _byte_size(body: str) -> int:
    return len(body.encode("utf-8"))


# --- Canned JSON-RPC transport ----------------------------------------------
ETAGS = {
    "index.html": "et-index",
    "readme.txt": "et-readme",
    "_ds/tokens.css": "et-tokens",
    "assets/logo.png": "et-logo",
}


def _fake_jsonrpc_call(bearer, method, params=None, request_id=1):  # noqa: ARG001 — signature must mirror the real _jsonrpc_call
    if method == "initialize":
        return {"protocolVersion": "2024-11-05"}

    if method == "tools/call":
        name = params["name"]
        args = params["arguments"]

        if name == "list_files":
            path = args.get("path", "")
            if path == "":  # root
                entries = [
                    {
                        "path": "index.html",
                        "type": "file",
                        "size": _byte_size(EXPECTED["index.html"]),
                        "etag": ETAGS["index.html"],
                    },
                    {
                        "path": "readme.txt",
                        "type": "file",
                        "size": _byte_size(EXPECTED["readme.txt"]),
                        "etag": ETAGS["readme.txt"],
                    },
                    {"path": "assets", "type": "directory"},
                    {"path": "_ds", "type": "directory"},
                ]
            elif path == "assets":
                entries = [
                    {
                        "path": "assets/logo.png",
                        "type": "file",
                        "size": 2048,
                        "etag": ETAGS["assets/logo.png"],
                    },
                ]
            elif path == "_ds":
                entries = [
                    {
                        "path": "_ds/tokens.css",
                        "type": "file",
                        "size": _byte_size(EXPECTED["_ds/tokens.css"]),
                        "etag": ETAGS["_ds/tokens.css"],
                    },
                ]
            else:
                entries = []
            return {"content": [{"type": "text", "text": json.dumps(entries)}]}

        if name == "read_file":
            path = args["path"]
            if path in BINARY_PATHS:
                return {
                    "content": [
                        {
                            "type": "text",
                            "isError": True,
                            "text": "This is a binary file and cannot be read as text.",
                        }
                    ]
                }
            body = EXPECTED[path]
            return {"content": [{"type": "text", "text": _wrap(path, ETAGS[path], body)}]}

    raise AssertionError(f"unexpected JSON-RPC call: {method} {params}")


def main() -> int:
    ds._read_bearer_token = lambda: "fake-bearer-token"
    ds._jsonrpc_call = _fake_jsonrpc_call

    prev_cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="design-sync-selftest-")
    try:
        os.chdir(tmp)
        result = ds.design_sync(
            "https://claude.ai/design/p/deadbeef-0000-1111-2222-333344445555",
            out_dir=".design-mocks",
            include_ds=True,
        )
        rc = _check(result)
    finally:
        os.chdir(prev_cwd)
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    return rc


def _check(result) -> int:
    if "error" in result:
        print(f"✗ design_sync returned error: {result['error']}")
        return 1

    ok = True
    out_dir = Path(result["out_dir"])

    # 1. Every expected text file present + byte-exact sha256
    written_by_path = {f["path"]: f for f in result["files_written"]}
    print("== Byte-exact text files ==")
    for path, body in EXPECTED.items():
        expected_bytes = body.encode("utf-8")
        expected_sha = hashlib.sha256(expected_bytes).hexdigest()
        disk = out_dir / path
        if not disk.exists():
            print(f"  ✗ {path}: NOT written")
            ok = False
            continue
        disk_bytes = disk.read_bytes()
        disk_sha = hashlib.sha256(disk_bytes).hexdigest()
        reported_sha = written_by_path.get(path, {}).get("sha256")
        match = disk_sha == expected_sha == reported_sha
        print(
            f"  {'✓' if match else '✗'} {path}: "
            f"sha256={disk_sha[:16]}… ({len(disk_bytes)}B) "
            f"expected={expected_sha[:16]}… reported={str(reported_sha)[:16]}…"
        )
        ok = ok and match

    # 2. Binary skipped + reported (not on disk)
    print("== Binaries skipped ==")
    for path in BINARY_PATHS:
        skipped = path in result["skipped_binaries"]
        on_disk = (out_dir / path).exists()
        good = skipped and not on_disk
        print(f"  {'✓' if good else '✗'} {path}: skipped={skipped} on_disk={on_disk}")
        ok = ok and good

    # 3. _ds/ tree present
    ds_present = (out_dir / "_ds/tokens.css").exists()
    print(f"== _ds/ present: {'✓' if ds_present else '✗'} ==")
    ok = ok and ds_present

    # 4. Manifest lists full sha256 + etag
    manifest = Path(result["manifest_path"]).read_text()
    idx_sha = written_by_path["index.html"]["sha256"]
    manifest_ok = idx_sha in manifest and "et-index" in manifest and "binary" in manifest
    print(f"== Manifest has full sha256 + etag + binary marker: {'✓' if manifest_ok else '✗'} ==")
    ok = ok and manifest_ok

    # 5. No orphaned .tmp files left behind
    leftovers = list(out_dir.rglob(".design-sync-*.tmp"))
    tmp_ok = not leftovers
    print(f"== No orphaned .tmp files: {'✓' if tmp_ok else '✗'} {leftovers} ==")
    ok = ok and tmp_ok

    print("\n" + ("✓ SELF-TEST PASSED" if ok else "✗ SELF-TEST FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
