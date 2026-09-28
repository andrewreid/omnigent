"""Refresh coordination: the store's lease primitives and a real multi-process race.

Providers rotate the refresh token on use, so two processes refreshing the same
connection leave one holding a spent token (and a provider with reuse detection
may revoke the whole chain). The race test runs 20 processes against one
database and asserts the provider's token endpoint is hit exactly once.
Runs on SQLite by default and on Postgres/MySQL when ``OMNIGENT_TEST_DB_URI``
is set.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

from omnigent.connections.github import GithubConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.server import github_identity
from omnigent.server.github_app import GitHubRefreshRejected, GitHubTokenSet

_USER = "alice@example.com"
_PROVIDER = "github"
_PROCS = 20
_REPO_ROOT = Path(__file__).resolve().parents[2]


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


def _store(db_uri: str) -> GithubConnectionStore:
    return GithubConnectionStore(db_uri, SecretBox("enc-key"))


def _tokens(access: str, expires_in: int) -> GitHubTokenSet:
    return GitHubTokenSet(access, f"ghr_{access}", now_epoch() + expires_in, None, "repo")


def _seed(db_uri: str, *, expires_in: int = 60) -> GithubConnectionStore:
    store = _store(db_uri)
    store.upsert(_USER, github_login="alice", github_user_id=7, tokens=_tokens("old", expires_in))
    return store


def _meta(store: GithubConnectionStore) -> dict:
    conn = store._store.get(_USER, _PROVIDER)
    assert conn is not None
    return dict(conn.metadata)


# --- store primitives -------------------------------------------------------


def test_lease_is_exclusive_until_released(db_uri: str) -> None:
    store = _seed(db_uri)
    assert store.acquire_refresh_lease(_USER, holder="a", ttl_s=60)
    assert not store.acquire_refresh_lease(_USER, holder="b", ttl_s=60)
    assert store.acquire_refresh_lease(_USER, holder="a", ttl_s=60)  # re-entrant
    assert not store.release_refresh_lease(_USER, holder="b")
    assert store.release_refresh_lease(_USER, holder="a")
    assert store.acquire_refresh_lease(_USER, holder="b", ttl_s=60)


def test_expired_lease_can_be_taken_over(db_uri: str) -> None:
    store = _seed(db_uri)
    assert store.acquire_refresh_lease(_USER, holder="crashed", ttl_s=0)
    assert store.acquire_refresh_lease(_USER, holder="b", ttl_s=60)


def test_commit_requires_the_lease_and_releases_it(db_uri: str) -> None:
    store = _seed(db_uri)
    assert store.acquire_refresh_lease(_USER, holder="a", ttl_s=60)
    assert not store.update_tokens(_USER, _tokens("stolen", 3600), lease_holder="b")
    assert store.get(_USER, with_tokens=True).access_token == "old"

    assert store.update_tokens(_USER, _tokens("new", 3600), lease_holder="a")
    assert store.get(_USER, with_tokens=True).access_token == "new"
    meta = _meta(store)
    assert "refresh_lease_holder" not in meta and "refresh_lease_until" not in meta
    assert meta["github_login"] == "alice"  # provider metadata preserved


def test_unleased_write_keeps_another_holders_lease(db_uri: str) -> None:
    store = _seed(db_uri)
    assert store.acquire_refresh_lease(_USER, holder="a", ttl_s=60)
    assert store.update_tokens(_USER, _tokens("other", 3600))
    assert _meta(store)["refresh_lease_holder"] == "a"


def test_needs_reconnect_is_set_and_cleared_by_reconnect(db_uri: str) -> None:
    store = _seed(db_uri)
    assert store.acquire_refresh_lease(_USER, holder="a", ttl_s=60)
    assert store.mark_needs_reconnect(_USER)
    assert store.needs_reconnect(_USER)
    assert "refresh_lease_holder" not in _meta(store)
    store.upsert(_USER, github_login="alice", github_user_id=7, tokens=_tokens("fresh", 3600))
    assert not store.needs_reconnect(_USER)


# --- resolver against the real store ----------------------------------------


class _Client:
    def __init__(self, *, result=None, exc=None) -> None:
        self.result, self.exc, self.calls = result, exc, 0

    async def refresh_token(self, refresh_token: str) -> GitHubTokenSet:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


def _resolve(store, client) -> str | None:
    return asyncio.run(github_identity.resolve_access_token(_USER, store=store, client=client))


def test_rejected_refresh_marks_reconnect_and_is_not_retried(db_uri: str) -> None:
    store = _seed(db_uri)
    client = _Client(exc=GitHubRefreshRejected("bad_refresh_token"))
    assert _resolve(store, client) is None
    assert store.needs_reconnect(_USER)
    assert _resolve(store, client) is None
    assert client.calls == 1


def test_refresh_persists_and_clears_the_lease(db_uri: str) -> None:
    store = _seed(db_uri)
    assert _resolve(store, _Client(result=_tokens("new", 28800))) == "new"
    assert store.get(_USER, with_tokens=True).access_token == "new"
    assert "refresh_lease_holder" not in _meta(store)


# --- the race ---------------------------------------------------------------


def race_worker_main(db_uri: str, workdir: str, index: int, bypass_lease: bool) -> None:
    """One worker process: resolve concurrently with the others and print the token.

    Run via :func:`_race` as a plain subprocess (not ``multiprocessing``, whose
    resource-tracker child would outlive the test).
    """

    class _CountingClient:
        async def refresh_token(self, refresh_token: str) -> GitHubTokenSet:
            with open(Path(workdir) / "refresh-calls.log", "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                fh.write(f"{refresh_token}\n")
            await asyncio.sleep(0.5)  # a slow provider widens the race window
            return _tokens("new", 28800)

    store = _store(db_uri)
    if bypass_lease:  # control: every process believes it won the lease
        store.acquire_refresh_lease = lambda *a, **k: True  # type: ignore[method-assign]
    (Path(workdir) / f"ready-{index}").touch()
    deadline = time.monotonic() + 60
    while len(list(Path(workdir).glob("ready-*"))) < _PROCS and time.monotonic() < deadline:
        time.sleep(0.01)
    token = asyncio.run(
        github_identity.resolve_access_token(_USER, store=store, client=_CountingClient())
    )
    print(json.dumps({"token": token}))


def _race(db_uri: str, workdir: Path, *, bypass_lease: bool = False) -> tuple[list[str], list]:
    """Run the resolver in ``_PROCS`` processes at once; return (upstream calls, tokens)."""
    (workdir / "refresh-calls.log").touch()
    code = (
        "import sys; from tests.server.test_refresh_coordination import race_worker_main; "
        "race_worker_main(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == '1')"
    )
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                db_uri,
                str(workdir),
                str(i),
                "1" if bypass_lease else "0",
            ],
            cwd=_REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(_PROCS)
    ]
    tokens = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        lines = out.strip().splitlines()
        tokens.append(
            json.loads(lines[-1])["token"]
            if proc.returncode == 0 and lines
            else f"ERROR: {err[-300:]}"
        )
    return (workdir / "refresh-calls.log").read_text().splitlines(), tokens


def test_concurrent_processes_refresh_exactly_once(db_uri: str, tmp_path: Path) -> None:
    _seed(db_uri)
    calls, results = _race(db_uri, tmp_path)
    assert calls == ["ghr_old"], f"expected one upstream refresh, got {len(calls)}"
    assert results == ["new"] * _PROCS, results
    store = _store(db_uri)
    assert store.get(_USER, with_tokens=True).access_token == "new"
    assert "refresh_lease_holder" not in _meta(store)


def test_without_the_lease_the_race_is_real(db_uri: str, tmp_path: Path) -> None:
    # Control for the test above: with the lease bypassed, the same workload
    # refreshes more than once — the hazard the lease exists to prevent.
    _seed(db_uri)
    calls, _ = _race(db_uri, tmp_path, bypass_lease=True)
    assert len(calls) > 1, "race harness failed to produce concurrent refreshes"


def test_race_leaves_no_lease_behind_after_crash_window(db_uri: str) -> None:
    # A holder that died mid-refresh leaves an expired lease; the next resolve
    # takes it over rather than waiting forever.
    store = _seed(db_uri)
    assert store.acquire_refresh_lease(_USER, holder="dead", ttl_s=0)
    time.sleep(1.1)
    assert _resolve(store, _Client(result=_tokens("new", 28800))) == "new"
