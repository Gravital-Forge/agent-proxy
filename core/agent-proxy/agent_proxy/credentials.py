"""Credentials stored one owner-only file per name."""

import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_proxy.config import NAME_PATTERN, check_name
from agent_proxy.errors import ProxyError


class EmptyCredentialError(ProxyError):
    """A credential value was empty."""


class UnknownCredentialError(ProxyError):
    """No credential is stored under the given name."""


@dataclass(frozen=True)
class CredentialEntry:
    name: str
    set_at: datetime


class CredentialStore:
    def __init__(self, directory: Path) -> None:
        """Open the store over directory, creating it and narrowing it to 0o700."""
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
        self._directory = directory

    def set(self, name: str, value: bytes) -> None:
        """Store value verbatim under name, replacing any value already there.

        Raises:
            InvalidNameError: name does not match NAME_PATTERN.
            EmptyCredentialError: value is empty.
        """
        check_name(name)
        if not value:
            raise EmptyCredentialError(
                f"credential {name!r} needs a non-empty value, got {value!r}"
            )
        # The staging name begins with a dot, outside NAME_PATTERN, so
        # entries() never lists a half-written credential.
        write_owner_only(self._directory / name, value)

    def entries(self) -> list[CredentialEntry]:
        """Return the stored credentials, sorted by name."""
        return sorted(
            (
                CredentialEntry(
                    name=path.name,
                    set_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                )
                for path in self._directory.iterdir()
                if re.fullmatch(NAME_PATTERN, path.name)
            ),
            key=lambda entry: entry.name,
        )

    def names(self) -> frozenset[str]:
        """Return the names of the stored credentials."""
        return frozenset(entry.name for entry in self.entries())

    def read(self, name: str) -> bytes:
        """Return the value stored under name.

        Raises:
            InvalidNameError: name does not match NAME_PATTERN.
            UnknownCredentialError: no credential is stored under name.
        """
        return self.path(name).read_bytes()

    def path(self, name: str) -> Path:
        """Return the file that holds the credential stored under name.

        Raises:
            InvalidNameError: name does not match NAME_PATTERN.
            UnknownCredentialError: no credential is stored under name.
        """
        check_name(name)
        return self._stored_path(name)

    def remove(self, name: str) -> None:
        """Delete the credential stored under name.

        Raises:
            UnknownCredentialError: no credential is stored under name.
        """
        self._stored_path(name).unlink()

    def _stored_path(self, name: str) -> Path:
        """Return the file of the credential stored under name.

        Raises:
            UnknownCredentialError: no credential is stored under name.
        """
        stored = self.names()
        if name not in stored:
            raise UnknownCredentialError(
                f"no credential named {name!r}; stored names are {sorted(stored)!r}"
            )
        return self._directory / name


def write_owner_only(target: Path, content: bytes) -> None:
    """Write content to target, mode 0o600, through a staging file renamed over it.

    The staging file sits beside target under a name beginning with a dot, and
    is removed if the write fails.
    """
    staging = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as staged:
            os.fchmod(fd, 0o600)
            staged.write(content)
            staged.flush()
            os.fsync(fd)
        staging.replace(target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
