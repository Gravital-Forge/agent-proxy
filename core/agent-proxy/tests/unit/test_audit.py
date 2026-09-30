import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_proxy.audit import (
    AUDIT_BACKUP_COUNT,
    AUDIT_MAX_BYTES,
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
