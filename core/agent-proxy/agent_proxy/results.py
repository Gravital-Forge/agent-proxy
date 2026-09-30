"""What a cage call returns: its captured streams, their overflow files, the sweep."""

import itertools
import logging
import os
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal

from jsonschema import Draft202012Validator

from agent_proxy.errors import ProxyError
from agent_proxy.plugins import ManifestError, decode_json_object

OUTPUT_CAP_BYTES = 262144
OVERFLOW_MAX_AGE = timedelta(days=7)
SWEEP_INTERVAL = timedelta(days=1)
CALL_ID_PATTERN = r"^[0-9a-f]{32}$"

# O_PATH needs only traverse permission on an ancestor, and the agent's home
# grants the service user no more. O_NOFOLLOW refuses a symlinked component.
_ANCESTOR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
# Under a default ACL this mode sets the ACL mask, so it must keep group access.
_FILE_MODE = 0o660

_logger = logging.getLogger(__name__)

Stream = Literal["stdout", "stderr"]


class OverflowWriteError(ProxyError):
    """An overflow file cannot be created or written."""


@dataclass(frozen=True)
class CapturedStream:
    """One output stream of a cage: its bytes inline, or the file that holds them.

    Exactly one of inline and file is set.
    """

    size: int
    inline: bytes | None
    file: str | None


def new_call_id() -> str:
    """Return a fresh call ID that matches CALL_ID_PATTERN."""
    return uuid.uuid4().hex


def capture_stream(
    chunks: Iterable[bytes], overflow_dir: str, call_id: str, stream: Stream
) -> CapturedStream:
    """Return a stream inline, or in <overflow_dir>/<call_id>.<stream> past the cap.

    Raises:
        OverflowWriteError: the stream passes OUTPUT_CAP_BYTES and its overflow
            file cannot be created or written.
    """
    remaining = iter(chunks)
    held, size = _hold_to_cap(remaining)
    if size <= OUTPUT_CAP_BYTES:
        return CapturedStream(size=size, inline=b"".join(held), file=None)
    path = PurePosixPath(overflow_dir, f"{call_id}.{stream}")
    size = _write_overflow(path, itertools.chain(_release(held), remaining))
    return CapturedStream(size=size, inline=None, file=str(path))


def _hold_to_cap(chunks: Iterator[bytes]) -> tuple[deque[bytes], int]:
    """Return the chunks read until their total passes the cap or they end, and it."""
    held: deque[bytes] = deque()
    size = 0
    for chunk in chunks:
        held.append(chunk)
        size += len(chunk)
        if size > OUTPUT_CAP_BYTES:
            break
    return held, size


def _release(held: deque[bytes]) -> Iterator[bytes]:
    """Yield each held chunk, dropping it from held so a written chunk leaves memory."""
    while held:
        yield held.popleft()


def _write_overflow(path: PurePosixPath, chunks: Iterable[bytes]) -> int:
    """Write chunks to a new file at path and return how many bytes they held.

    An error that chunks raises passes through unchanged.

    Raises:
        OverflowWriteError: the file cannot be created or written.
    """
    fd = _create_overflow_file(path)
    size = 0
    try:
        for chunk in chunks:
            _write_all(fd, path, chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    return size


def _create_overflow_file(path: PurePosixPath) -> int:
    """Return a write descriptor for a new file at path.

    Raises:
        OverflowWriteError: a component of path's directory is missing or a
            symlink, or the file exists or cannot be created.
    """
    try:
        with _open_directory(str(path.parent)) as directory_fd:
            return os.open(path.name, _FILE_FLAGS, _FILE_MODE, dir_fd=directory_fd)
    except OSError as exc:
        raise OverflowWriteError(
            f"overflow file {str(path)!r} cannot be created: {exc.strerror}"
        ) from exc


def _write_all(fd: int, path: PurePosixPath, chunk: bytes) -> None:
    """Write every byte of chunk to fd.

    Raises:
        OverflowWriteError: the write fails.
    """
    unwritten = memoryview(chunk)
    try:
        while unwritten:
            unwritten = unwritten[os.write(fd, unwritten) :]
    except OSError as exc:
        raise OverflowWriteError(
            f"overflow file {str(path)!r} cannot be written: {exc.strerror}"
        ) from exc


@contextmanager
def _open_directory(directory: str) -> Iterator[int]:
    """Yield a read descriptor for directory, walked one component at a time from /.

    Raises:
        OSError: a component is missing, is a symlink, or cannot be opened.
    """
    fd = _walk(PurePosixPath(directory))
    try:
        yield fd
    finally:
        os.close(fd)


def _walk(directory: PurePosixPath) -> int:
    *ancestors, leaf = directory.parts[1:]
    fd = os.open("/", _ANCESTOR_FLAGS)
    for name in ancestors:
        fd = _open_beneath(fd, name, _ANCESTOR_FLAGS)
    return _open_beneath(fd, leaf, _DIRECTORY_FLAGS)


def _open_beneath(parent_fd: int, name: str, flags: int) -> int:
    """Return name opened relative to parent_fd, closing parent_fd either way."""
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def sweep_overflow(directory: str, now: datetime) -> None:
    """Remove each old entry directly in directory that is not a directory.

    An entry is old when its own modification time is more than
    OVERFLOW_MAX_AGE before now. A symlink is judged and removed as itself; its
    target is never touched.

    Raises:
        OSError: the directory cannot be walked or listed, or an entry cannot be
            removed.
    """
    cutoff = (now - OVERFLOW_MAX_AGE).timestamp()
    with _open_directory(directory) as directory_fd:
        for name in _expired_names(directory_fd, cutoff):
            os.unlink(name, dir_fd=directory_fd)


def _expired_names(directory_fd: int, cutoff: float) -> list[str]:
    """Return the non-directory entries of directory_fd modified before cutoff."""
    with os.scandir(directory_fd) as entries:
        return [
            entry.name
            for entry in entries
            if not entry.is_dir(follow_symlinks=False)
            and entry.stat(follow_symlinks=False).st_mtime < cutoff
        ]


class OverflowSweeper:
    """A daemon thread that sweeps the overflow directory at start and each interval."""

    def __init__(
        self,
        directory: Callable[[], str],
        now: Callable[[], datetime],
        interval: timedelta = SWEEP_INTERVAL,
    ) -> None:
        """Hold what each sweep reads: the directory and the time, both read fresh."""
        self._directory = directory
        self._now = now
        self._interval = interval
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="overflow-sweeper", daemon=True
        )

    def start(self) -> None:
        """Start the thread; it sweeps at once."""
        self._thread.start()

    def stop(self) -> None:
        """Wake the thread and wait for it to end."""
        self._stopping.set()
        self._thread.join()

    def _run(self) -> None:
        self._sweep()
        while not self._stopping.wait(self._interval.total_seconds()):
            self._sweep()

    def _sweep(self) -> None:
        directory = self._directory()
        try:
            sweep_overflow(directory, self._now())
        except OSError as exc:
            _logger.warning("overflow sweep of %r failed: %s", directory, exc)


def command_content(
    exit_code: int, stdout: CapturedStream, stderr: CapturedStream
) -> dict[str, object]:
    """Return a command's result: its exit code and each stream, inline or as a file."""
    content: dict[str, object] = {"exit_code": exit_code}
    content |= _stream_content("stdout", stdout)
    content |= _stream_content("stderr", stderr)
    if stdout.file is not None or stderr.file is not None:
        content["truncated"] = True
    return content


def _stream_content(stream: Stream, captured: CapturedStream) -> dict[str, object]:
    if captured.inline is not None:
        return {stream: captured.inline.decode("utf-8", errors="replace")}
    return {f"{stream}_file": captured.file, f"{stream}_bytes": captured.size}


def script_content(
    output_schema: Mapping[str, object],
    exit_code: int,
    stdout: CapturedStream,
    stderr: CapturedStream,
) -> tuple[dict[str, object], bool]:
    """Return a script's result and whether the script failed.

    A script succeeds when it exits 0 and its inline stdout is a JSON object
    valid against output_schema; its result is then that object. A failed
    script's result is the command result.
    """
    output = _script_output(output_schema, exit_code, stdout)
    if output is None:
        return command_content(exit_code, stdout, stderr), True
    return output, False


def _script_output(
    output_schema: Mapping[str, object], exit_code: int, stdout: CapturedStream
) -> dict[str, object] | None:
    if exit_code != 0 or stdout.inline is None:
        return None
    try:
        output = decode_json_object(stdout.inline)
    except ManifestError:
        return None
    if not Draft202012Validator(output_schema).is_valid(output):
        return None
    return output
