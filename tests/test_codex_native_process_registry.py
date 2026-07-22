"""Tests for crash-safe native Codex process registry reconciliation."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from omnigent import codex_native_process_registry as registry

fcntl = pytest.importorskip("fcntl")


def _registry_payload(path: Path) -> list[dict[str, object]]:
    """
    Return the raw JSON registry payload.

    :param path: Registry file path.
    :returns: Parsed registry entries.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def test_registry_add_remove_round_trip(tmp_path: Path) -> None:
    """Registry writes and removes a tagged codex child entry."""
    path = tmp_path / "registry.json"

    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        tmux_session_name="omnigent-codex-123",
        session_tag="tag-123",
        owner_lock_path=tmp_path / "owner.lock",
        registry_path=path,
    )

    assert _registry_payload(path) == [
        {
            "pid": 123,
            "pgid": 456,
            "tmux_session_name": "omnigent-codex-123",
            "session_tag": "tag-123",
            "owner_lock_path": str(tmp_path / "owner.lock"),
            "sigterm_at": None,
        }
    ]

    registry.unregister_codex_native_process("tag-123", registry_path=path)

    assert _registry_payload(path) == []


def test_reconciliation_sigterms_alive_tagged_process_and_keeps_entry(
    tmp_path: Path, monkeypatch
) -> None:
    """A live tagged process is SIGTERMed and kept for escalation."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    signaled = registry.reconcile_codex_native_process_registry(registry_path=path)

    assert signaled == 1
    assert killed == [(456, signal.SIGTERM)]
    # The entry survives with its SIGTERM time recorded, so a later pass
    # can escalate to SIGKILL if the process ignores the SIGTERM.
    (payload,) = _registry_payload(path)
    assert payload["session_tag"] == "tag-123"
    assert isinstance(payload["sigterm_at"], float)


def test_reconciliation_escalates_to_sigkill_after_grace(tmp_path: Path, monkeypatch) -> None:
    """A SIGTERM-surviving process is SIGKILLed once the grace elapses."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)
    # Within the grace: no second signal, entry untouched.
    registry.reconcile_codex_native_process_registry(registry_path=path)
    assert killed == [(456, signal.SIGTERM)]
    assert len(_registry_payload(path)) == 1

    monkeypatch.setattr(registry, "_SIGKILL_GRACE_S", 0.0)
    monkeypatch.setattr(registry, "_process_group_alive", lambda _pgid: True)
    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == [(456, signal.SIGTERM), (456, signal.SIGKILL)]
    assert _registry_payload(path) == []


def test_reconciliation_skips_pid_reuse_without_matching_tag(tmp_path: Path, monkeypatch) -> None:
    """A reused PID is never killed when the cmdline lacks the session tag."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(registry, "_process_cmdline", lambda _pid: "python unrelated.py")
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_reconciliation_skips_live_sibling_when_owner_lock_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    """A healthy sibling child is not reaped while its launcher owns the lock."""
    path = tmp_path / "registry.json"
    owner_lock = tmp_path / "owner.lock"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=owner_lock,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_owner_lock_held", lambda value: value == str(owner_lock))
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == [
        {
            "pid": 123,
            "pgid": 456,
            "tmux_session_name": None,
            "session_tag": "tag-123",
            "owner_lock_path": str(owner_lock),
            "sigterm_at": None,
        }
    ]


def test_reconciliation_reaps_when_owner_lock_is_not_held(tmp_path: Path, monkeypatch) -> None:
    """A tagged child is reaped after its owning launcher lock is gone."""
    path = tmp_path / "registry.json"
    owner_lock = tmp_path / "owner.lock"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=owner_lock,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_owner_lock_held", lambda _value: False)
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == [(456, signal.SIGTERM)]
    (payload,) = _registry_payload(path)
    assert payload["sigterm_at"] is not None


def test_reconciliation_drops_dead_pids(tmp_path: Path, monkeypatch) -> None:
    """Dead process entries are discarded without kill attempts."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_tmux_session_reaped_only_when_recorded_name_exists(tmp_path: Path, monkeypatch) -> None:
    """Dropping an entry reaps only its recorded, still-existing tmux session."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        tmux_session_name="omnigent-codex-live",
        session_tag="tag-live",
        owner_lock_path=None,
        registry_path=path,
    )
    registry.register_codex_native_process(
        pid=124,
        pgid=457,
        tmux_session_name="omnigent-codex-missing",
        session_tag="tag-missing",
        owner_lock_path=None,
        registry_path=path,
    )
    killed_tmux: list[str] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(registry.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(
        registry,
        "_tmux_session_exists",
        lambda name: name == "omnigent-codex-live",
    )
    monkeypatch.setattr(registry, "_kill_tmux_session", lambda name: killed_tmux.append(name))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed_tmux == ["omnigent-codex-live"]
    assert _registry_payload(path) == []


def test_owner_lock_liveness_round_trip(tmp_path: Path, monkeypatch) -> None:
    """A held owner lock reads as held; releasing it makes the entry reapable."""
    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: tmp_path)
    lock = registry.acquire_codex_native_process_owner_lock()
    assert lock is not None
    assert registry._owner_lock_held(str(lock.path)) is True
    lock.close()
    assert registry._owner_lock_held(str(lock.path)) is False


def test_registry_lock_serializes_read_modify_write(tmp_path: Path) -> None:
    """The registry lock is exclusive across the read-modify-write window."""
    path = tmp_path / "registry.json"
    with registry._registry_lock(path):
        fd = registry.os.open(str(path) + ".lock", registry.os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            registry.os.close(fd)


def test_escalation_kills_surviving_child_after_leader_exits(tmp_path: Path, monkeypatch) -> None:
    """A TERM-ignoring group child is SIGKILLed even after its leader exits.

    SIGTERM kills the tagged leader but a descendant in the same group
    ignores it. The next pass must settle by group liveness — not drop the
    entry because the leader pid is gone — so the child still dies.
    """
    import subprocess
    import sys
    import time as time_mod

    path = tmp_path / "registry.json"
    tag = "tag-child"
    # Leader spawns a SIGTERM-ignoring child in its group, then sleeps
    # until SIGTERMed. The child prints its own pid only AFTER installing
    # the handler, so reading the pid proves the ignore is in place.
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', "
                "'import os, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print(os.getpid(), flush=True); "
                "time.sleep(120)'])\n"
                "time.sleep(120)\n"
            ),
            registry.codex_native_session_tag_cmdline_arg(tag),
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid: int | None = None
    try:
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline().strip())
        registry.register_codex_native_process(
            pid=leader.pid,
            pgid=leader.pid,
            session_tag=tag,
            owner_lock_path=None,
            registry_path=path,
        )

        # Pass 1: SIGTERM the group. Leader dies, child ignores it.
        assert registry.reconcile_codex_native_process_registry(registry_path=path) == 1
        assert leader.wait(timeout=10) is not None
        deadline = time_mod.monotonic() + 5.0
        while registry._pid_alive(leader.pid) and time_mod.monotonic() < deadline:
            time_mod.sleep(0.05)
        assert registry._pid_alive(child_pid), "child should have ignored the SIGTERM"

        # Pass 2 after grace: leader gone, but the group is still alive —
        # escalate to SIGKILL instead of dropping the entry.
        monkeypatch.setattr(registry, "_SIGKILL_GRACE_S", 0.0)
        assert registry.reconcile_codex_native_process_registry(registry_path=path) == 1
        deadline = time_mod.monotonic() + 5.0
        while registry._pid_alive(child_pid) and time_mod.monotonic() < deadline:
            time_mod.sleep(0.05)
        assert not registry._pid_alive(child_pid), "surviving group child leaked"
        assert _registry_payload(path) == []
    finally:
        import contextlib
        import os

        if child_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(child_pid, signal.SIGKILL)
        if leader.poll() is None:
            leader.kill()
        leader.wait(timeout=10)


def test_escalation_gives_up_past_the_group_trust_window(tmp_path: Path, monkeypatch) -> None:
    """A stale SIGTERMed entry is dropped, never group-killed.

    Past the trust window the pgid may have been recycled by an unrelated
    session leader, so killing it would be a false positive; the entry is
    dropped with a warning instead.
    """
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-stale",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-stale app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)
    assert killed == [(456, signal.SIGTERM)]

    # Fast-forward past the trust window: the entry must be dropped with no
    # further signal, even though the group would still read as alive.
    monkeypatch.setattr(registry, "_SIGKILL_GRACE_S", 0.0)
    monkeypatch.setattr(registry, "_SIGKILL_WINDOW_S", 0.0)
    monkeypatch.setattr(registry, "_process_group_alive", lambda _pgid: True)
    assert registry.reconcile_codex_native_process_registry(registry_path=path) == 0

    assert killed == [(456, signal.SIGTERM)]
    assert _registry_payload(path) == []


def test_end_to_end_spares_owned_process_and_reaps_after_owner_death(
    tmp_path: Path, monkeypatch
) -> None:
    """The full kill gate against a real process and a real kernel flock.

    While the launcher's owner lock is held (a live owned session), any
    number of reconciliation passes must leave the child untouched.
    Releasing the lock — what the kernel does on any launcher death,
    however unclean — makes the same child reapable: first pass SIGTERMs
    it, and once it has exited a later pass drops the entry.
    """
    import subprocess
    import sys
    import time as time_mod

    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: tmp_path)
    path = tmp_path / "registry.json"
    tag = "tag-e2e"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            registry.codex_native_session_tag_cmdline_arg(tag),
        ],
        start_new_session=True,
    )
    try:
        lock = registry.acquire_codex_native_process_owner_lock()
        assert lock is not None
        registry.register_codex_native_process(
            pid=proc.pid,
            pgid=proc.pid,
            session_tag=tag,
            owner_lock_path=lock.path,
            registry_path=path,
        )

        # Owned: repeated passes never touch the live child.
        for _ in range(3):
            assert registry.reconcile_codex_native_process_registry(registry_path=path) == 0
        assert proc.poll() is None

        # Owner dies (kernel releases the flock): the child is now reapable.
        lock.close()
        assert registry.reconcile_codex_native_process_registry(registry_path=path) == 1
        assert proc.wait(timeout=10) is not None

        # Group gone: a post-grace pass drops the entry without signals.
        deadline = time_mod.monotonic() + 5.0
        while registry._pid_alive(proc.pid) and time_mod.monotonic() < deadline:
            time_mod.sleep(0.05)
        monkeypatch.setattr(registry, "_SIGKILL_GRACE_S", 0.0)
        assert registry.reconcile_codex_native_process_registry(registry_path=path) == 0
        assert _registry_payload(path) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
