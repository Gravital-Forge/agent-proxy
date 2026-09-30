import contextlib
import errno
import itertools
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from agent_proxy.errors import ProxyError
from agent_proxy.results import (
    CALL_ID_PATTERN,
    OUTPUT_CAP_BYTES,
    OVERFLOW_MAX_AGE,
    SWEEP_INTERVAL,
    CapturedStream,
    OverflowSweeper,
    OverflowWriteError,
    capture_stream,
    command_content,
    new_call_id,
    script_content,
    sweep_overflow,
)

CALL_ID = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
OVER_CAP = b"x" * (OUTPUT_CAP_BYTES + 1)
OVERFLOW_FILE = "/srv/agents/alpha/overflow/0123.stdout"
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"sha": {"type": "string"}},
    "required": ["sha"],
}


def _inline(data: bytes) -> CapturedStream:
    return CapturedStream(size=len(data), inline=data, file=None)


def _overflowed(path: str, size: int) -> CapturedStream:
    return CapturedStream(size=size, inline=None, file=path)


def _age(path: Path, age: timedelta) -> None:
    """Set path's own modification time to age before NOW, not following a link."""
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("condition not met within 5 seconds")
        time.sleep(0.01)


def test_constants_hold_the_cap_the_retention_and_the_interval() -> None:
    """The cap is 256 KiB, overflow lives seven days, and the sweep runs daily."""
    assert OUTPUT_CAP_BYTES == 262144
    assert OVERFLOW_MAX_AGE == timedelta(days=7)
    assert SWEEP_INTERVAL == timedelta(days=1)
    assert CALL_ID_PATTERN == r"^[0-9a-f]{32}$"


def test_new_call_id_matches_the_pattern_and_is_fresh() -> None:
    """A call ID is 32 lowercase hex digits, different on each call."""
    first, second = new_call_id(), new_call_id()

    assert re.fullmatch(CALL_ID_PATTERN, first)
    assert re.fullmatch(CALL_ID_PATTERN, second)
    assert first != second


def test_capture_stream_keeps_a_stream_at_the_cap_inline(tmp_path: Path) -> None:
    """A stream of exactly OUTPUT_CAP_BYTES stays inline and creates no file."""
    data = b"a" * OUTPUT_CAP_BYTES

    captured = capture_stream(
        [data[:1000], data[1000:]], str(tmp_path), CALL_ID, "stdout"
    )

    assert captured == CapturedStream(size=OUTPUT_CAP_BYTES, inline=data, file=None)
    assert list(tmp_path.iterdir()) == []


def test_capture_stream_keeps_an_empty_stream_inline(tmp_path: Path) -> None:
    """A stream with no chunks is inline and empty."""
    captured = capture_stream([], str(tmp_path), CALL_ID, "stdout")

    assert captured == CapturedStream(size=0, inline=b"", file=None)


def test_capture_stream_writes_a_stream_over_the_cap_to_a_file(tmp_path: Path) -> None:
    """A stream one byte over the cap goes whole to <call_id>.<stream>."""
    chunks = [b"x" * 16384] * 16 + [b"y"]
    path = tmp_path / f"{CALL_ID}.stdout"

    captured = capture_stream(chunks, str(tmp_path), CALL_ID, "stdout")

    assert captured == CapturedStream(
        size=OUTPUT_CAP_BYTES + 1, inline=None, file=str(path)
    )
    assert path.read_bytes() == b"".join(chunks)


def test_capture_stream_names_the_file_for_its_stream(tmp_path: Path) -> None:
    """A stderr stream over the cap goes to <call_id>.stderr."""
    captured = capture_stream([OVER_CAP], str(tmp_path), CALL_ID, "stderr")

    assert captured.file == str(tmp_path / f"{CALL_ID}.stderr")


def test_capture_stream_creates_the_file_with_mode_0o660(tmp_path: Path) -> None:
    """The overflow file's mode is 0o660 when no umask bit masks it."""
    previous = os.umask(0)
    try:
        capture_stream([OVER_CAP], str(tmp_path), CALL_ID, "stdout")
    finally:
        os.umask(previous)

    mode = (tmp_path / f"{CALL_ID}.stdout").stat().st_mode
    assert stat.S_IMODE(mode) == 0o660


def test_capture_stream_writes_through_once_over_the_cap(tmp_path: Path) -> None:
    """Chunks stay in memory until the cap is passed, then go straight to the file."""
    chunk_size = 32768
    path = tmp_path / f"{CALL_ID}.stdout"

    def chunks() -> Iterator[bytes]:
        for index in range(20):
            # Checked as each chunk is requested: nine chunks pass the cap.
            if index * chunk_size <= OUTPUT_CAP_BYTES:
                assert not path.exists()
            else:
                assert path.stat().st_size == index * chunk_size
            yield bytes([index]) * chunk_size

    captured = capture_stream(chunks(), str(tmp_path), CALL_ID, "stdout")

    assert captured.size == 20 * chunk_size
    assert path.read_bytes() == b"".join(bytes([i]) * chunk_size for i in range(20))


def test_capture_stream_refuses_a_symlink_at_the_file_name(tmp_path: Path) -> None:
    """A link planted at <call_id>.<stream> is refused, and its target is kept."""
    overflow = tmp_path / "overflow"
    overflow.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    (overflow / f"{CALL_ID}.stdout").symlink_to(target)

    with pytest.raises(OverflowWriteError):
        capture_stream([OVER_CAP], str(overflow), CALL_ID, "stdout")

    assert target.read_bytes() == b"keep"


def test_capture_stream_refuses_an_existing_file(tmp_path: Path) -> None:
    """A file already at <call_id>.<stream> is refused and left as it was."""
    existing = tmp_path / f"{CALL_ID}.stdout"
    existing.write_bytes(b"earlier")

    with pytest.raises(OverflowWriteError):
        capture_stream([OVER_CAP], str(tmp_path), CALL_ID, "stdout")

    assert existing.read_bytes() == b"earlier"


def test_capture_stream_refuses_a_symlinked_overflow_dir(tmp_path: Path) -> None:
    """An overflow directory that is a link is refused, and nothing is written."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "overflow"
    link.symlink_to(real)

    with pytest.raises(OverflowWriteError):
        capture_stream([OVER_CAP], str(link), CALL_ID, "stdout")

    assert list(real.iterdir()) == []


def test_capture_stream_refuses_a_symlinked_component_above(tmp_path: Path) -> None:
    """A link among the overflow directory's ancestors is refused."""
    real = tmp_path / "real"
    (real / "overflow").mkdir(parents=True)
    link = tmp_path / "home"
    link.symlink_to(real)

    with pytest.raises(OverflowWriteError):
        capture_stream([OVER_CAP], str(link / "overflow"), CALL_ID, "stdout")

    assert list((real / "overflow").iterdir()) == []


def test_overflow_write_error_names_the_path_and_the_os_error(tmp_path: Path) -> None:
    """The message names the file and the OS error, never the stream's content."""
    missing = tmp_path / "missing"

    with pytest.raises(OverflowWriteError) as excinfo:
        capture_stream([b"SECRET" * OUTPUT_CAP_BYTES], str(missing), CALL_ID, "stdout")

    message = str(excinfo.value)
    assert repr(str(missing / f"{CALL_ID}.stdout")) in message
    assert os.strerror(errno.ENOENT) in message
    assert "SECRET" not in message
    assert isinstance(excinfo.value, ProxyError)


def test_a_traverse_only_ancestor_allows_a_write_and_a_sweep(tmp_path: Path) -> None:
    """An ancestor at mode 0o111 blocks an O_RDONLY walk but not the O_PATH walk."""
    locked = tmp_path / "home"
    overflow = locked / "overflow"
    overflow.mkdir(parents=True)
    old = overflow / "old.stdout"
    old.write_bytes(b"old")
    _age(old, timedelta(days=8))
    locked.chmod(0o111)
    try:
        with pytest.raises(PermissionError):
            os.open(locked, os.O_RDONLY | os.O_DIRECTORY)
        captured = capture_stream([OVER_CAP], str(overflow), CALL_ID, "stdout")
        sweep_overflow(str(overflow), NOW)
    finally:
        locked.chmod(0o755)

    assert captured.size == OUTPUT_CAP_BYTES + 1
    assert (overflow / f"{CALL_ID}.stdout").read_bytes() == OVER_CAP
    assert not old.exists()


def test_sweep_overflow_removes_only_old_non_directories(tmp_path: Path) -> None:
    """An eight-day-old file goes; a six-day-old file and a subdirectory stay."""
    old = tmp_path / "old.stdout"
    old.write_bytes(b"old")
    _age(old, timedelta(days=8))
    recent = tmp_path / "recent.stdout"
    recent.write_bytes(b"recent")
    _age(recent, timedelta(days=6))
    subdirectory = tmp_path / "nested"
    subdirectory.mkdir()
    nested = subdirectory / "old.stdout"
    nested.write_bytes(b"nested")
    _age(nested, timedelta(days=8))
    _age(subdirectory, timedelta(days=8))

    sweep_overflow(str(tmp_path), NOW)

    assert not old.exists()
    assert recent.read_bytes() == b"recent"
    assert nested.read_bytes() == b"nested"


def test_sweep_overflow_unlinks_an_old_symlink_and_keeps_its_target(
    tmp_path: Path,
) -> None:
    """An eight-day-old link is removed; the file it points to stays."""
    overflow = tmp_path / "overflow"
    overflow.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    _age(target, timedelta(days=8))
    link = overflow / "link.stdout"
    link.symlink_to(target)
    _age(link, timedelta(days=8))

    sweep_overflow(str(overflow), NOW)

    assert not link.is_symlink()
    assert target.read_bytes() == b"keep"


def test_sweep_overflow_ages_a_symlink_by_its_own_time(tmp_path: Path) -> None:
    """A one-day-old link to an eight-day-old file stays."""
    overflow = tmp_path / "overflow"
    overflow.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    _age(target, timedelta(days=8))
    link = overflow / "link.stdout"
    link.symlink_to(target)
    _age(link, timedelta(days=1))

    sweep_overflow(str(overflow), NOW)

    assert link.is_symlink()


def test_sweep_overflow_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    """A swept directory that is a link raises OSError and removes nothing."""
    real = tmp_path / "real"
    real.mkdir()
    old = real / "old.stdout"
    old.write_bytes(b"old")
    _age(old, timedelta(days=8))
    link = tmp_path / "overflow"
    link.symlink_to(real)

    with pytest.raises(OSError) as excinfo:
        sweep_overflow(str(link), NOW)

    assert excinfo.value.errno in {errno.ENOTDIR, errno.ELOOP}
    assert old.exists()


def test_sweep_overflow_skips_an_entry_gone_before_its_unlink(tmp_path: Path) -> None:
    """An entry removed by someone else before its unlink does not stop the sweep."""
    for name in ("a.stdout", "b.stdout"):
        (tmp_path / name).write_bytes(b"old")
        _age(tmp_path / name, timedelta(days=8))
    real_unlink = os.unlink

    def unlink_after_a_race(name: str, *, dir_fd: int) -> None:
        real_unlink(name, dir_fd=dir_fd)
        if name == "a.stdout":
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)

    with mock.patch("agent_proxy.results.os.unlink", side_effect=unlink_after_a_race):
        sweep_overflow(str(tmp_path), NOW)

    assert list(tmp_path.iterdir()) == []


def test_sweep_overflow_skips_an_entry_gone_before_its_stat(tmp_path: Path) -> None:
    """An entry removed by someone else before its stat does not stop the sweep."""
    old = tmp_path / "old.stdout"
    old.write_bytes(b"old")
    _age(old, timedelta(days=8))
    vanished = mock.Mock(spec=os.DirEntry)
    vanished.name = "vanished.stdout"
    vanished.is_dir.return_value = False
    vanished.stat.side_effect = FileNotFoundError(
        errno.ENOENT, os.strerror(errno.ENOENT), "vanished.stdout"
    )
    real_scandir = os.scandir

    @contextlib.contextmanager
    def scandir_with_a_vanished_entry(fd: int) -> Iterator[Iterator[os.DirEntry]]:
        with real_scandir(fd) as entries:
            yield itertools.chain([vanished], entries)

    with mock.patch(
        "agent_proxy.results.os.scandir", side_effect=scandir_with_a_vanished_entry
    ):
        sweep_overflow(str(tmp_path), NOW)

    assert not old.exists()


def test_overflow_sweeper_sweeps_at_once_on_start(tmp_path: Path) -> None:
    """start() sweeps without waiting for the first interval."""
    old = tmp_path / "old.stdout"
    old.write_bytes(b"old")
    _age(old, timedelta(days=8))
    sweeper = OverflowSweeper(lambda: str(tmp_path), lambda: NOW)

    sweeper.start()
    try:
        _wait_until(lambda: not old.exists())
    finally:
        sweeper.stop()


def test_overflow_sweeper_sweeps_again_after_each_interval(tmp_path: Path) -> None:
    """The sweeper asks for its directory once per sweep, sweep after sweep."""
    sweeps: list[str] = []

    def directory() -> str:
        sweeps.append(str(tmp_path))
        return str(tmp_path)

    sweeper = OverflowSweeper(
        directory, lambda: NOW, interval=timedelta(milliseconds=10)
    )

    sweeper.start()
    try:
        _wait_until(lambda: len(sweeps) >= 3)
    finally:
        sweeper.stop()


def test_overflow_sweeper_stop_wakes_and_joins_the_thread(tmp_path: Path) -> None:
    """stop() returns without waiting out a one-day interval, and the thread ends."""
    sweeps: list[str] = []

    def directory() -> str:
        sweeps.append(str(tmp_path))
        return str(tmp_path)

    threads_before = set(threading.enumerate())
    sweeper = OverflowSweeper(directory, lambda: NOW)
    sweeper.start()
    _wait_until(lambda: len(sweeps) == 1)

    started = time.monotonic()
    sweeper.stop()

    assert time.monotonic() - started < 5
    assert set(threading.enumerate()) - threads_before == set()


def test_overflow_sweeper_logs_a_failed_sweep_and_keeps_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A sweep that raises OSError is a warning, and the next sweep still runs."""
    missing = tmp_path / "missing"
    sweeps: list[str] = []

    def directory() -> str:
        sweeps.append(str(missing))
        return str(missing)

    caplog.set_level(logging.WARNING, logger="agent_proxy.results")
    sweeper = OverflowSweeper(
        directory, lambda: NOW, interval=timedelta(milliseconds=10)
    )

    sweeper.start()
    try:
        _wait_until(lambda: len(sweeps) >= 2)
    finally:
        sweeper.stop()

    assert any(
        record.levelno == logging.WARNING and str(missing) in record.getMessage()
        for record in caplog.records
    )


def test_command_content_returns_inline_streams_as_text() -> None:
    """Inline streams come back as text beside the exit code."""
    content = command_content(3, _inline(b"out"), _inline(b"err"))

    assert content == {"exit_code": 3, "stdout": "out", "stderr": "err"}


def test_command_content_replaces_bytes_that_are_not_utf8() -> None:
    """A byte that is not UTF-8 becomes the replacement character."""
    content = command_content(0, _inline(b"a\xffb"), _inline(b""))

    assert content["stdout"] == "a�b"


def test_command_content_points_at_an_overflowed_stdout() -> None:
    """An overflowed stream is its file and size, and the result is truncated."""
    content = command_content(0, _overflowed(OVERFLOW_FILE, 300000), _inline(b"err"))

    assert content == {
        "exit_code": 0,
        "stdout_file": OVERFLOW_FILE,
        "stdout_bytes": 300000,
        "stderr": "err",
        "truncated": True,
    }
    assert list(content) == [
        "exit_code",
        "stdout_file",
        "stdout_bytes",
        "stderr",
        "truncated",
    ]


def test_command_content_points_at_an_overflowed_stderr() -> None:
    """An overflowed stderr alone also marks the result truncated."""
    content = command_content(1, _inline(b"out"), _overflowed(OVERFLOW_FILE, 300000))

    assert content == {
        "exit_code": 1,
        "stdout": "out",
        "stderr_file": OVERFLOW_FILE,
        "stderr_bytes": 300000,
        "truncated": True,
    }


def test_script_content_returns_a_valid_output_object() -> None:
    """A zero exit with a schema-valid JSON object on stdout returns that object."""
    result = script_content(OUTPUT_SCHEMA, 0, _inline(b'{"sha": "abc"}'), _inline(b""))

    assert result == {"sha": "abc"}


def test_script_content_validates_against_draft_2020_12() -> None:
    """Under draft 2020-12, items applies only after prefixItems."""
    schema = {
        "type": "object",
        "properties": {"tags": {"prefixItems": [{"type": "string"}], "items": False}},
    }

    result = script_content(schema, 0, _inline(b'{"tags": ["a"]}'), _inline(b""))

    assert result == {"tags": ["a"]}


FAILED_SCRIPTS = [
    pytest.param(1, _inline(b'{"sha": "abc"}'), id="non-zero-exit"),
    pytest.param(0, _inline(b"not json"), id="not-json"),
    pytest.param(0, _inline(b'[{"sha": "abc"}]'), id="json-array"),
    pytest.param(0, _inline(b'{"sha": 7}'), id="fails-schema"),
    pytest.param(
        0, _overflowed(OVERFLOW_FILE, OUTPUT_CAP_BYTES + 1), id="overflowed-stdout"
    ),
    pytest.param(0, _inline(b'{"sha": "abc", "sha": "def"}'), id="repeated-key"),
    pytest.param(0, _inline(b'{"sha": "abc", "n": NaN}'), id="non-finite-number"),
    pytest.param(0, _inline(b'{"sha": "\xff"}'), id="not-utf8"),
]


@pytest.mark.parametrize(("exit_code", "stdout"), FAILED_SCRIPTS)
def test_script_content_returns_the_command_result_for_a_failed_script(
    exit_code: int, stdout: CapturedStream
) -> None:
    """A failed script returns the command-shaped result."""
    stderr = _inline(b"trace")

    result = script_content(OUTPUT_SCHEMA, exit_code, stdout, stderr)

    assert result == command_content(exit_code, stdout, stderr)
