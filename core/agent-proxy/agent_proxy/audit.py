"""Audit records written as rotating JSON lines, and the tail reader."""

import contextlib
import json
import os
import stat
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from agent_proxy.errors import ProxyError

AUDIT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_BACKUP_COUNT = 5
ARGUMENTS_CAP_CHARACTERS = 16_384
ACTION_CAP_CHARACTERS = 256
REFUSAL_CAP_CHARACTERS = 4_096
_LOG_NAME = "audit.log"
_LOG_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT
_LOG_MODE = 0o600
_SCAN_CHUNK = 4096
_CUT_MARKER = " [cut]"


class AuditError(ProxyError):
    """The audit log could not be prepared or could not record a line."""


@dataclass(frozen=True)
class AuditRecord:
    timestamp: datetime
    call_id: str
    token_name: str | None
    scope: str | None
    action: str
    arguments: Mapping[str, object] | None
    exit_code: int | None
    duration_ms: int
    stdout_bytes: int | None
    stderr_bytes: int | None
    truncated: bool
    refusal: str | None


class AuditWriter:
    def __init__(
        self,
        directory: Path,
        max_bytes: int = AUDIT_MAX_BYTES,
        backup_count: int = AUDIT_BACKUP_COUNT,
    ) -> None:
        """Prepare directory/audit.log for appending, narrowing directory to 0o700.

        A regular-file log whose last byte is not a newline is cut back to
        just after its last newline, so a fragment never prefixes a later line.

        Raises:
            AuditError: the directory cannot be made or narrowed, or the log's
                trailing fragment cannot be cut.
        """
        self._path = directory / _LOG_NAME
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._lock = threading.Lock()
        self._latched = False
        try:
            directory.mkdir(exist_ok=True)
            directory.chmod(0o700)
            _cut_trailing_fragment(self._path)
        except OSError as exc:
            raise AuditError(
                f"audit log {str(self._path)!r} cannot be prepared: {exc}"
            ) from exc

    @property
    def available(self) -> bool:
        """Whether the writer accepts lines; False once a rollback has failed."""
        return not self._latched

    def write(self, record: AuditRecord) -> None:
        """Append record to the log as one JSON line, rotating a full file first.

        A line that fails part way is truncated away, so the log holds no
        byte of it.

        Raises:
            AuditError: the writer is latched, or the log cannot be rotated,
                take the line, or be closed.
        """
        line = f"{_render_line(record)}\n".encode()
        with self._lock:
            if self._latched:
                raise AuditError(
                    f"audit log {str(self._path)!r} is unavailable after a"
                    " failed rollback"
                )
            self._rotate_if_due(len(line))
            self._append(line)

    def close(self) -> None:
        """Release the writer; no file stays open between writes."""

    def _rotate_if_due(self, line_size: int) -> None:
        """Rotate the log when line would take a non-empty file to max_bytes.

        Raises:
            AuditError: the log cannot be measured or renamed.
        """
        try:
            size = self._path.stat().st_size if self._path.exists() else 0
            if size and size + line_size >= self._max_bytes:
                self._rotate()
        except OSError as exc:
            raise AuditError(
                f"audit log {str(self._path)!r} cannot be rotated: {exc}"
            ) from exc

    def _rotate(self) -> None:
        for index in range(self._backup_count, 0, -1):
            source = self._path if index == 1 else self._backup(index - 1)
            if source.exists():
                source.replace(self._backup(index))

    def _backup(self, index: int) -> Path:
        return self._path.with_name(f"{_LOG_NAME}.{index}")

    def _append(self, line: bytes) -> None:
        """Append line to the log, truncating it away when the write fails.

        Raises:
            AuditError: the log cannot be opened, take the line, or be closed.
        """
        try:
            fd = os.open(self._path, _LOG_FLAGS, _LOG_MODE)
        except OSError as exc:
            raise AuditError(
                f"audit log {str(self._path)!r} cannot be opened: {exc}"
            ) from exc
        try:
            self._write_or_roll_back(fd, line)
        except BaseException:
            # Linux frees the descriptor even when close fails, so the error
            # worth raising is the write's.
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        self._close(fd)

    def _close(self, fd: int) -> None:
        """Close fd.

        Raises:
            AuditError: the close fails.
        """
        try:
            os.close(fd)
        except OSError as exc:
            raise AuditError(
                f"audit log {str(self._path)!r} cannot be closed: {exc}"
            ) from exc

    def _write_or_roll_back(self, fd: int, line: bytes) -> None:
        """Write line to fd, cutting fd back to its prior size on failure.

        Raises:
            AuditError: the log cannot be measured or cannot take the line.
        """
        try:
            size_before = os.fstat(fd).st_size
        except OSError as exc:
            raise AuditError(
                f"audit log {str(self._path)!r} cannot be measured: {exc}"
            ) from exc
        try:
            _write_all(fd, line)
        except OSError as exc:
            self._roll_back(fd, size_before)
            raise AuditError(
                f"audit log {str(self._path)!r} cannot take a line: {exc}"
            ) from exc

    def _roll_back(self, fd: int, size: int) -> None:
        try:
            os.ftruncate(fd, size)
        except OSError:
            self._latched = True


def tail(directory: Path, count: int) -> list[str]:
    """Return the last count audit lines in write order.

    Rotated files are read, newest first, only while fewer than count lines
    are in hand. A missing directory or log yields no lines.
    """
    lines: list[str] = []
    for path in _log_files_newest_first(directory):
        if len(lines) >= count:
            break
        lines = path.read_text(encoding="utf-8").splitlines() + lines
    start = max(len(lines) - count, 0)
    return lines[start:]


def _render_line(record: AuditRecord) -> str:
    """Render record as compact JSON, its caller-chosen fields capped."""
    arguments, arguments_cut = _capped_arguments(record.arguments)
    fields: dict[str, object] = {
        "timestamp": record.timestamp.isoformat(),
        "call_id": record.call_id,
        "token_name": record.token_name,
        "scope": record.scope,
        "action": _capped(record.action, ACTION_CAP_CHARACTERS),
        "arguments": arguments,
        "arguments_cut": arguments_cut,
        "exit_code": record.exit_code,
        "duration_ms": record.duration_ms,
        "stdout_bytes": record.stdout_bytes,
        "stderr_bytes": record.stderr_bytes,
        "truncated": record.truncated,
        "refusal": (
            None
            if record.refusal is None
            else _capped(record.refusal, REFUSAL_CAP_CHARACTERS)
        ),
    }
    return _compact_json(fields)


def _capped_arguments(
    arguments: Mapping[str, object] | None,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Return the line's arguments and arguments_cut fields for arguments.

    Arguments whose compact JSON passes the cap are replaced by that JSON's
    UTF-8 size and its first ARGUMENTS_CAP_CHARACTERS characters.
    """
    if arguments is None:
        return None, None
    text = _compact_json(arguments)
    if len(text) <= ARGUMENTS_CAP_CHARACTERS:
        return dict(arguments), None
    return None, {"bytes": len(text.encode()), "head": text[:ARGUMENTS_CAP_CHARACTERS]}


def _capped(text: str, cap: int) -> str:
    return text if len(text) <= cap else f"{text[:cap]}{_CUT_MARKER}"


def _compact_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, separators=(",", ":"))


def _log_files_newest_first(directory: Path) -> Iterator[Path]:
    path = directory / _LOG_NAME
    index = 0
    while path.exists():
        yield path
        index += 1
        path = directory / f"{_LOG_NAME}.{index}"


def _write_all(fd: int, line: bytes) -> None:
    """Write every byte of line to fd.

    Raises:
        OSError: a write fails.
    """
    unwritten = memoryview(line)
    while unwritten:
        unwritten = unwritten[os.write(fd, unwritten) :]


def _cut_trailing_fragment(path: Path) -> None:
    """Cut a regular-file log back to just after its last newline.

    A missing log and anything that is not a regular file are left alone.
    """
    try:
        status = path.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(status.st_mode):
        return
    with path.open("r+b") as log:
        end = _end_of_last_line(log, status.st_size)
        if end < status.st_size:
            log.truncate(end)


def _end_of_last_line(log: BinaryIO, size: int) -> int:
    """Return the offset just past log's last newline, or 0 when it holds none.

    The file is read backwards from size, one chunk at a time.
    """
    end = size
    while end > 0:
        start = max(end - _SCAN_CHUNK, 0)
        log.seek(start)
        newline = log.read(end - start).rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0
