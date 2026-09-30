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
        # The leading dot keeps the staging name outside NAME_PATTERN, so
        # entries() never lists a half-written credential.
        staging = self._directory / f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as staged:
            os.fchmod(fd, 0o600)
            staged.write(value)
            staged.flush()
            os.fsync(fd)
        staging.replace(self._directory / name)

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

    def remove(self, name: str) -> None:
        """Delete the credential stored under name.

        Raises:
            UnknownCredentialError: no credential is stored under name.
        """
        stored = self.names()
        if name not in stored:
            raise UnknownCredentialError(
                f"no credential named {name!r}; stored names are {sorted(stored)!r}"
            )
        (self._directory / name).unlink()
