#!/usr/bin/env python3
"""
TOCTOU symlink-escape regression test (round-4 BLOCKER 2).

Reproduces codex's exploit: a directory component of the mirror is swapped for
a symlink to an OUTSIDE directory AFTER the pre-check passes but BEFORE the
write. Asserts the descriptor-relative O_NOFOLLOW write refuses to follow the
link — NO bytes land outside out_dir — while the rest of the sync completes.

Run: uv run python examples/design-sync/tests/test_toctou.py
"""

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

URL = "https://claude.ai/design/p/deadbeef-0000-1111-2222-333344445555"


def main() -> int:
    prev_cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="toctou-cwd-")
    outside = tempfile.mkdtemp(prefix="toctou-outside-")
    try:
        os.chdir(tmp)
        out_dir = ".design-mocks"

        # list_files reports one evil file under sub/ plus one benign file.
        def fake_jsonrpc(bearer, method, params=None, request_id=1):  # noqa: ARG001
            if method == "initialize":
                return {}
            name = params["name"]
            if name == "list_files" and not params["arguments"].get("path"):
                entries = [
                    {"path": "sub/evil.txt", "type": "file", "size": 3, "etag": "e1"},
                    {"path": "ok.txt", "type": "file", "size": 2, "etag": "e2"},
                ]
                return {"content": [{"type": "text", "text": json.dumps(entries)}]}
            if name == "list_files":
                return {"content": [{"type": "text", "text": json.dumps([])}]}
            raise AssertionError(f"unexpected call {name}")

        base = Path(tmp) / out_dir

        # The attack: swap out_dir/sub for a symlink to `outside` between the
        # pre-check and the write. Injected as a side effect of the read.
        real_msg = {"count": 0}

        def evil_read(bearer, project_id, path, request_id):  # noqa: ARG001
            if path == "sub/evil.txt":
                subdir = base / "sub"
                if subdir.exists() or subdir.is_symlink():
                    if subdir.is_symlink():
                        subdir.unlink()
                    else:
                        shutil.rmtree(subdir)
                subdir.symlink_to(outside)  # now points OUTSIDE the mirror
                real_msg["count"] += 1
                return b"PWN", False, None
            return b"OK", False, None

        ds._read_bearer_token = lambda: "fake-token"
        ds._jsonrpc_call = fake_jsonrpc
        ds._read_file_content = evil_read

        result = ds.design_sync(URL, out_dir=out_dir, include_ds=True)

        ok = True

        # 1. The attack MUST have fired (symlink swapped in).
        if real_msg["count"] != 1:
            print(f"  ✗ attack side effect did not fire (count={real_msg['count']})")
            ok = False

        # 2. NO bytes may have landed outside the mirror.
        leaked = Path(outside) / "evil.txt"
        if leaked.exists():
            print(f"  ✗ OUTSIDE_WRITE True {leaked.read_bytes()!r}  <-- EXPLOIT REPRODUCED")
            ok = False
        else:
            print("  ✓ OUTSIDE_WRITE False (no bytes escaped out_dir)")

        # 3. evil.txt must be reported unsafe (not written).
        written_paths = {f["path"] for f in result.get("files_written", [])}
        if "sub/evil.txt" in written_paths:
            print("  ✗ sub/evil.txt reported as written")
            ok = False
        elif "sub/evil.txt" in result.get("skipped_unsafe", []):
            print("  ✓ sub/evil.txt reported unsafe")
        else:
            print(f"  ✗ sub/evil.txt not flagged unsafe: {result.get('failures')}")
            ok = False

        # 4. The sync did NOT crash and completed the benign file.
        if "error" in result:
            print(f"  ✗ whole sync aborted: {result['error']}")
            ok = False
        elif "ok.txt" in written_paths:
            print("  ✓ benign ok.txt still written (sync completed)")
        else:
            print("  ✗ benign ok.txt missing — sync did not complete")
            ok = False

        print("\n" + ("✓ TOCTOU REGRESSION PASSED" if ok else "✗ TOCTOU REGRESSION FAILED"))
        return 0 if ok else 1
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
