"""Identity-verified process helpers for tests.

Tests must never observe or signal a NON-CHILD pid through raw
primitives: on a busy host the pid can be recycled mid-test, making a
liveness poll watch a stranger forever and a cleanup ``os.kill`` shoot
one. These wrappers speak the same identity vocabulary as production
(:mod:`omnigent.inner._proc`). A test's own unreaped ``Popen`` child is
exempt — holding the handle pins the pid — and should keep using
``poll()``/``kill()``/``wait()``.
"""

from __future__ import annotations

import signal
import time

from omnigent.inner import _proc

_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def capture_identity(pid: int) -> str:
    """Record *pid*'s incarnation identity; fails the test if unreadable.

    :param pid: A live process the test just learned about.
    :returns: The identity string for later verified waits/kills.
    """
    identity = _proc.process_start_identity(pid)
    assert identity is not None, f"could not capture identity of pid {pid}"
    return identity


def alive(pid: int, identity: str) -> bool:
    """Whether the recorded incarnation is still running (zombie = dead).

    :param pid: The recorded pid.
    :param identity: Its captured identity.
    :returns: ``True`` only for the same, non-zombie incarnation.
    """
    return _proc.process_identity_state(pid, identity) == "match" and not _proc.process_is_zombie(
        pid
    )


def wait_gone(pid: int, identity: str, deadline_s: float = 10.0) -> bool:
    """Poll until the recorded incarnation is dead (or the deadline hits).

    :param pid: The recorded pid.
    :param identity: Its captured identity.
    :param deadline_s: Wall-clock budget.
    :returns: ``True`` when it died within the budget.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if not alive(pid, identity):
            return True
        time.sleep(0.05)
    return not alive(pid, identity)


def safe_kill(pid: int, identity: str) -> None:
    """SIGKILL exactly the recorded incarnation (pidfd-pinned on Linux).

    A no-op when the incarnation is already gone or the pid was recycled.

    :param pid: The recorded pid.
    :param identity: Its captured identity.
    """
    _proc.kill_verified(pid, identity, _KILL)
