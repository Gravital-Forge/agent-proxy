"""A partial-write fault for the os.write that agent_proxy.audit calls."""

import errno
import os
from collections.abc import Buffer

import pytest

_REAL_WRITE = os.write


def fail_next_write_after_half(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next os.write land half its buffer and raise ENOSPC; later ones pass."""
    failed = False

    def write(fd: int, data: Buffer) -> int:
        nonlocal failed
        if failed:
            return _REAL_WRITE(fd, data)
        failed = True
        view = memoryview(data)
        _REAL_WRITE(fd, view[: len(view) // 2])
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr("agent_proxy.audit.os.write", write)
