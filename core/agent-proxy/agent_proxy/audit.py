"""Audit records written as rotating JSON lines, and the tail reader."""

import dataclasses
import json
import logging
import logging.handlers
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import override

AUDIT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_BACKUP_COUNT = 5
_LOG_NAME = "audit.log"


@dataclass(frozen=True)
class AuditRecord:
    timestamp: datetime
    call_id: str
    token_name: str | None
    scope: str | None
    action: str
    arguments: Mapping[str, object]
    exit_code: int | None
    duration_ms: int
    stdout_bytes: int | None
    stderr_bytes: int | None
    truncated: bool
    refusal: str | None


class _RaisingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating handler that lets a failed write reach its caller."""

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        """Re-raise the exception emit is handling, printing nothing.

        Raises:
            Exception: the exception emit caught while writing record.
        """
        raise


class AuditWriter:
    def __init__(
        self,
        directory: Path,
        max_bytes: int = AUDIT_MAX_BYTES,
        backup_count: int = AUDIT_BACKUP_COUNT,
    ) -> None:
        """Open directory/audit.log for appending, narrowing directory to 0o700."""
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
        self._handler = _RaisingRotatingFileHandler(
            directory / _LOG_NAME,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, record: AuditRecord) -> None:
        """Append record to the log as one JSON line, rotating a full file first.

        Raises:
            OSError: the log cannot take the line or cannot be rotated.
        """
        line = _render_line(record)
        self._handler.handle(
            logging.makeLogRecord({"msg": line, "levelno": logging.INFO})
        )

    def close(self) -> None:
        """Close the log file."""
        self._handler.close()


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
    fields: dict[str, object] = {
        field.name: getattr(record, field.name) for field in dataclasses.fields(record)
    }
    fields["timestamp"] = record.timestamp.isoformat()
    fields["arguments"] = dict(record.arguments)
    return json.dumps(fields, separators=(",", ":"))


def _log_files_newest_first(directory: Path) -> Iterator[Path]:
    path = directory / _LOG_NAME
    index = 0
    while path.exists():
        yield path
        index += 1
        path = directory / f"{_LOG_NAME}.{index}"
