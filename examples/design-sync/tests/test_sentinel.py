#!/usr/bin/env python3
"""
Entity-sentinel collision regression test (round-4 BLOCKER 3).

The old two-pass decode used a fixed sentinel string and raised ValueError if
file content happened to contain it — crashing the ENTIRE sync. The single-pass
regex decode has no sentinel, so such content now round-trips byte-exact.

Proves both:
  1. `_html_entity_decode` handles content containing the FORMER sentinel string
     (byte-exact, no raise) plus the &amp;-last edge case.
  2. A full `design_sync` run over a file whose content contains that string
     writes it byte-exact and does NOT abort.

Run: uv run python examples/design-sync/tests/test_sentinel.py
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.modules["omnigent_client"] = type(sys)("omnigent_client")
sys.modules["omnigent_client"].tool = lambda f: f

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools/python"))
import design_sync as ds  # noqa: E402

# The exact string the old implementation used as its magic sentinel.
FORMER_SENTINEL = "\x00AMPERSAND_PLACEHOLDER_e4b9c2a1\x00"
URL = "https://claude.ai/design/p/deadbeef-0000-1111-2222-333344445555"


def _escape(body: str) -> str:
    return body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def test_decode_unit() -> bool:
    print("Testing _html_entity_decode with former sentinel + edge cases...")
    ok = True

    # Content containing the former sentinel must survive untouched.
    body = f"prefix {FORMER_SENTINEL} & <tag> suffix"
    decoded = ds._html_entity_decode(_escape(body))
    if decoded != body:
        print(f"  ✗ sentinel round-trip failed: {decoded!r}")
        ok = False
    else:
        print("  ✓ former-sentinel content round-trips byte-exact (no raise)")

    # &amp;-last edge case must not double-decode.
    cases = {
        "&amp;lt; becomes &lt; and &amp; becomes &": "&lt; becomes < and & becomes &",
        "Rock &amp; Roll &mdash; the &#39;60s": "Rock & Roll — the '60s",
        "&#x2014; dash": "— dash",
        "bare & ampersand": "bare & ampersand",
    }
    for raw, want in cases.items():
        got = ds._html_entity_decode(raw)
        if got != want:
            print(f"  ✗ {raw!r} -> {got!r}, want {want!r}")
            ok = False
    if ok:
        print("  ✓ &amp;-last / numeric / hex / bare-& all correct")
    return ok


def test_full_sync() -> bool:
    print("Testing full design_sync over file containing former sentinel...")
    expected = f"line1\n{FORMER_SENTINEL}\nRock & Roll <b>x</b>\n"
    size = len(expected.encode("utf-8"))

    def fake_jsonrpc(bearer, method, params=None, request_id=1):  # noqa: ARG001
        if method == "initialize":
            return {}
        name = params["name"]
        if name == "list_files" and not params["arguments"].get("path"):
            entries = [{"path": "coll.txt", "type": "file", "size": size, "etag": "e1"}]
            return {"content": [{"type": "text", "text": json.dumps(entries)}]}
        if name == "list_files":
            return {"content": [{"type": "text", "text": json.dumps([])}]}
        if name == "read_file":
            wrapped = (
                '<untrusted-project-content path="coll.txt" etag="e1">\n'
                f"{_escape(expected)}\n"
                "</untrusted-project-content>\n"
                "(Note: escaped.)"
            )
            return {"content": [{"type": "text", "text": wrapped}]}
        raise AssertionError(name)

    prev = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="sentinel-")
    try:
        os.chdir(tmp)
        ds._read_bearer_token = lambda: "fake"
        ds._jsonrpc_call = fake_jsonrpc
        result = ds.design_sync(URL, out_dir=".design-mocks")

        if "error" in result:
            print(f"  ✗ sync aborted: {result['error']}")
            return False
        disk = Path(result["out_dir"]) / "coll.txt"
        if not disk.exists():
            print(f"  ✗ file not written; failures={result.get('failures')}")
            return False
        disk_bytes = disk.read_bytes()
        want_sha = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        got_sha = hashlib.sha256(disk_bytes).hexdigest()
        if got_sha != want_sha:
            print(f"  ✗ byte mismatch: {got_sha[:16]}… != {want_sha[:16]}…")
            return False
        print(
            f"  ✓ SENTINEL content written byte-exact (sha256={got_sha[:16]}…, {len(disk_bytes)}B)"
        )
        return True
    finally:
        os.chdir(prev)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ok = test_decode_unit() and test_full_sync()
    print("\n" + ("✓ SENTINEL REGRESSION PASSED" if ok else "✗ SENTINEL REGRESSION FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
