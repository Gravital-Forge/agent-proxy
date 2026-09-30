import errno
import json
import os
import stat
import threading
from collections.abc import Buffer
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_proxy.audit import (
    AUDIT_BACKUP_COUNT,
    AUDIT_MAX_BYTES,
    AuditError,
    AuditRecord,
    AuditWriter,
    tail,
)

FIELD_NAMES = [
    "timestamp",
    "call_id",
    "token_name",
    "scope",
    "action",
    "arguments",
    "exit_code",
    "duration_ms",
    "stdout_bytes",
    "stderr_bytes",
    "truncated",
    "refusal",
]

_REAL_WRITE = os.write


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    """Return an audit directory path that does not exist yet."""
    return tmp_path / "audit"


def _record(call_id: str = "call-0001") -> AuditRecord:
    return AuditRecord(
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        call_id=call_id,
        token_name="ci-runner",
        scope="ci",
        action="git_status",
        arguments={"repo": "agent-proxy"},
        exit_code=0,
        duration_ms=42,
        stdout_bytes=128,
        stderr_bytes=0,
        truncated=False,
        refusal=None,
    )


def _write(
    directory: Path,
    records: list[AuditRecord],
    max_bytes: int = AUDIT_MAX_BYTES,
    backup_count: int = AUDIT_BACKUP_COUNT,
) -> None:
    writer = AuditWriter(directory, max_bytes=max_bytes, backup_count=backup_count)
    for record in records:
        writer.write(record)
    writer.close()


def _log_lines(directory: Path) -> list[str]:
    return (directory / "audit.log").read_text(encoding="utf-8").splitlines()


def _call_ids(lines: list[str]) -> list[str]:
    return [json.loads(line)["call_id"] for line in lines]


def test_each_line_is_json_with_exactly_the_twelve_keys(directory: Path) -> None:
    """Every line parses as one JSON object keyed by the record's fields, in order."""
    _write(directory, [_record("call-0001"), _record("call-0002")])

    lines = _log_lines(directory)

    assert len(lines) == 2
    for line in lines:
        assert list(json.loads(line)) == FIELD_NAMES


def test_a_line_holds_the_record_values(directory: Path) -> None:
    """A line carries each field's value, the timestamp in ISO 8601."""
    _write(directory, [_record()])

    assert json.loads(_log_lines(directory)[0]) == {
        "timestamp": "2026-01-02T03:04:05+00:00",
        "call_id": "call-0001",
        "token_name": "ci-runner",
        "scope": "ci",
        "action": "git_status",
        "arguments": {"repo": "agent-proxy"},
        "exit_code": 0,
        "duration_ms": 42,
        "stdout_bytes": 128,
        "stderr_bytes": 0,
        "truncated": False,
        "refusal": None,
    }


def test_a_line_is_compact_json(directory: Path) -> None:
    """A line has no space after a JSON separator."""
    _write(directory, [_record()])

    line = _log_lines(directory)[0]

    assert ", " not in line
    assert ": " not in line


def test_log_holds_neither_the_credential_nor_the_output(directory: Path) -> None:
    """A call's credential value and stdout text appear in no file of the directory."""
    credential_value = "cred-7f3a9c1e-never-logged"
    stdout_text = "stdout-4b2d8e60-never-logged"
    record = AuditRecord(
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        call_id="call-0001",
        token_name="ci-runner",
        scope="ci",
        action="git_status",
        arguments={"repo": "agent-proxy"},
        exit_code=0,
        duration_ms=42,
        stdout_bytes=len(stdout_text.encode()),
        stderr_bytes=0,
        truncated=False,
        refusal=None,
    )

    _write(directory, [record])

    assert len(_log_lines(directory)) == 1
    for path in directory.iterdir():
        content = path.read_bytes()
        assert credential_value.encode() not in content
        assert stdout_text.encode() not in content


def test_rotation_keeps_backup_count_files_within_max_bytes(directory: Path) -> None:
    """Forty records at 512 bytes and two backups leave three files of 512 or less."""
    records = [_record(f"call-{index:04d}") for index in range(40)]

    _write(directory, records, max_bytes=512, backup_count=2)

    assert sorted(path.name for path in directory.iterdir()) == [
        "audit.log",
        "audit.log.1",
        "audit.log.2",
    ]
    for path in directory.iterdir():
        assert path.stat().st_size <= 512


def test_writer_appends_to_an_existing_log(directory: Path) -> None:
    """A second writer over the same directory keeps the lines already written."""
    _write(directory, [_record("call-0001")])
    _write(directory, [_record("call-0002")])

    assert _call_ids(_log_lines(directory)) == ["call-0001", "call-0002"]


def test_writer_creates_its_directory_owner_only(directory: Path) -> None:
    """A directory the writer creates has mode 0o700."""
    _write(directory, [_record()])

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_writer_narrows_an_existing_wider_directory(directory: Path) -> None:
    """A directory that existed at 0o755 is narrowed to 0o700."""
    directory.mkdir()
    directory.chmod(0o755)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755

    _write(directory, [_record()])

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_writer_creates_the_log_owner_only(directory: Path) -> None:
    """A log file the writer creates has mode 0o600."""
    _write(directory, [_record()])

    assert stat.S_IMODE((directory / "audit.log").stat().st_mode) == 0o600


def test_a_line_past_max_bytes_into_an_empty_log_does_not_rotate(
    directory: Path,
) -> None:
    """An empty log takes a line longer than max_bytes without rotating."""
    _write(directory, [_record()], max_bytes=1)

    assert [path.name for path in directory.iterdir()] == ["audit.log"]


def test_a_failed_rotation_raises_and_writes_nothing(directory: Path) -> None:
    """A rotation that cannot rename leaves the log as it was, the writer available."""
    writer = AuditWriter(directory, max_bytes=1, backup_count=1)
    writer.write(_record("call-0001"))
    (directory / "audit.log.1").mkdir()
    (directory / "audit.log.1" / "keep").touch()
    before = (directory / "audit.log").read_bytes()

    with pytest.raises(AuditError, match="Is a directory"):
        writer.write(_record("call-0002"))

    assert (directory / "audit.log").read_bytes() == before
    assert writer.available is True


def test_concurrent_writes_each_land_as_one_line(directory: Path) -> None:
    """Eight threads of fifty writes leave four hundred whole JSON lines."""
    writer = AuditWriter(directory, max_bytes=16_384, backup_count=10)

    def write_fifty(thread_index: int) -> None:
        for index in range(50):
            writer.write(_record(f"call-{thread_index}-{index:02d}"))

    threads = [
        threading.Thread(target=write_fifty, args=(thread_index,))
        for thread_index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    writer.close()

    lines = [
        line
        for path in directory.iterdir()
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 400
    assert len(set(_call_ids(lines))) == 400


def test_opening_cuts_a_trailing_fragment(directory: Path) -> None:
    """A log ending in a partial line is cut back to just after its last newline."""
    _write(directory, [_record("call-0001")])
    whole = (directory / "audit.log").read_bytes()
    with (directory / "audit.log").open("ab") as log:
        log.write(b'{"call_id":"frag' + b"x" * 10_000)

    AuditWriter(directory).close()

    assert (directory / "audit.log").read_bytes() == whole


def test_opening_empties_a_log_that_holds_only_a_fragment(directory: Path) -> None:
    """A log with no newline at all is cut to empty."""
    directory.mkdir()
    (directory / "audit.log").write_bytes(b'{"call_id":"frag')

    AuditWriter(directory).close()

    assert (directory / "audit.log").read_bytes() == b""


def _full_log_writer(directory: Path) -> AuditWriter:
    """Return a writer whose log is /dev/full, so every write fails with ENOSPC."""
    directory.mkdir()
    (directory / "audit.log").symlink_to("/dev/full")
    return AuditWriter(directory)


def test_a_write_the_log_cannot_take_raises(directory: Path) -> None:
    """A write that fails reaches the caller as an AuditError naming the OS error."""
    writer = _full_log_writer(directory)

    with pytest.raises(AuditError, match="No space left on device"):
        writer.write(_record())


def test_a_failed_write_prints_nothing_to_stderr(
    directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed write leaves stderr empty, so no record argument leaks there."""
    writer = _full_log_writer(directory)

    with pytest.raises(AuditError, match="No space left on device"):
        writer.write(_record())

    assert capsys.readouterr().err == ""


def test_a_failed_rollback_on_dev_full_latches_the_writer(directory: Path) -> None:
    """The rollback's ftruncate fails on a character device, so the writer latches."""
    writer = _full_log_writer(directory)

    with pytest.raises(AuditError):
        writer.write(_record())

    assert writer.available is False


def test_a_fresh_writer_is_available(directory: Path) -> None:
    """A writer that has not failed is available."""
    assert AuditWriter(directory).available is True


def test_a_log_that_cannot_be_opened_raises_and_stays_available(
    directory: Path,
) -> None:
    """A log the writer cannot create raises AuditError and latches nothing."""
    writer = AuditWriter(directory)
    directory.chmod(0o500)
    try:
        with pytest.raises(AuditError, match="Permission denied"):
            writer.write(_record())
    finally:
        directory.chmod(0o700)

    assert writer.available is True


def _fail_once_after_half(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_a_failed_write_leaves_the_log_as_it_was(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that lands half its line and then fails leaves no byte of that line."""
    writer = AuditWriter(directory)
    writer.write(_record("call-0001"))
    before = (directory / "audit.log").read_bytes()
    _fail_once_after_half(monkeypatch)

    with pytest.raises(AuditError, match="No space left on device"):
        writer.write(_record("call-0002"))

    assert (directory / "audit.log").read_bytes() == before


def test_the_write_after_a_rolled_back_failure_succeeds(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rolled-back failure leaves the writer available, and the next line lands."""
    writer = AuditWriter(directory)
    writer.write(_record("call-0001"))
    _fail_once_after_half(monkeypatch)
    with pytest.raises(AuditError):
        writer.write(_record("call-0002"))

    writer.write(_record("call-0003"))

    assert writer.available is True
    assert _call_ids(_log_lines(directory)) == ["call-0001", "call-0003"]


def test_a_failed_write_error_names_the_path_and_not_the_record(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error names the log path and carries no field of the record."""
    writer = AuditWriter(directory)
    _fail_once_after_half(monkeypatch)

    with pytest.raises(AuditError) as caught:
        writer.write(_record("call-0002"))

    message = str(caught.value)
    assert str(directory / "audit.log") in message
    assert "call-0002" not in message
    assert "ci-runner" not in message


def test_a_failed_rollback_latches_the_writer(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the rollback fails too, every later write raises without opening a file."""
    writer = AuditWriter(directory)
    _fail_once_after_half(monkeypatch)

    def ftruncate(fd: int, length: int) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr("agent_proxy.audit.os.ftruncate", ftruncate)
    with pytest.raises(AuditError):
        writer.write(_record("call-0001"))
    opened: list[str | os.PathLike[str]] = []
    real_open = os.open

    def counting_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        opened.append(path)
        return real_open(path, flags, mode)

    monkeypatch.setattr("agent_proxy.audit.os.open", counting_open)

    with pytest.raises(AuditError):
        writer.write(_record("call-0002"))

    assert writer.available is False
    assert opened == []


def test_tail_reads_back_across_a_rotation(directory: Path) -> None:
    """The last lines come back in write order when they span rotated files."""
    records = [_record(f"call-{index:04d}") for index in range(5)]
    _write(directory, records, max_bytes=512, backup_count=2)
    assert len(_log_lines(directory)) < 3

    lines = tail(directory, 3)

    assert _call_ids(lines) == ["call-0002", "call-0003", "call-0004"]


def test_tail_returns_every_line_when_fewer_exist(directory: Path) -> None:
    """Asking for more lines than exist returns all of them, in write order."""
    _write(directory, [_record("call-0001"), _record("call-0002")])

    assert _call_ids(tail(directory, 10)) == ["call-0001", "call-0002"]


def test_tail_of_zero_lines_is_empty(directory: Path) -> None:
    """A count of zero returns no lines."""
    _write(directory, [_record()])

    assert tail(directory, 0) == []


def test_tail_of_a_missing_directory_is_empty(directory: Path) -> None:
    """A directory that does not exist yields no lines."""
    assert tail(directory, 10) == []


def test_tail_of_a_directory_without_a_log_is_empty(directory: Path) -> None:
    """A directory with no audit.log yields no lines."""
    directory.mkdir()

    assert tail(directory, 10) == []
