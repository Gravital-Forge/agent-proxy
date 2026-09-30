"""Bearer tokens stored as SHA-256 digests in one SQLite file."""

import dataclasses
import hashlib
import hmac
import os
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_proxy.config import check_name
from agent_proxy.errors import ProxyError

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tokens (
    name TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    token_sha256 BLOB NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT
)
"""


class TokenNameInUseError(ProxyError):
    """A stored token already holds the name being minted."""


class UnknownTokenError(ProxyError):
    """No token is stored under the given name."""


class TokenStoreError(ProxyError):
    """The token store's file cannot be read."""


@dataclass(frozen=True)
class TokenRecord:
    name: str
    scope: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class TokenStore:
    def __init__(
        self, path: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        """Open the store over the SQLite file at path, creating it 0o600 if absent."""
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        self._path = path
        self._now = now
        with self._connect() as connection:
            connection.execute(_CREATE_TABLE)

    def mint(self, name: str, scope: str, expires_in: timedelta | None) -> str:
        """Store a new token for scope under name and return it.

        Raises:
            InvalidNameError: name or scope does not match NAME_PATTERN.
            TokenNameInUseError: a stored token already holds name.
        """
        check_name(name)
        check_name(scope)
        token = "apx_" + secrets.token_urlsafe(32)
        created_at = self._now()
        expires_at = None if expires_in is None else created_at + expires_in
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO tokens VALUES (?, ?, ?, ?, NULL, ?)",
                    (
                        name,
                        scope,
                        _digest(token),
                        created_at.isoformat(),
                        _format_time(expires_at),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise TokenNameInUseError(
                f"a token named {name!r} already exists; revoke it before minting"
                " that name again"
            ) from error
        return token

    def records(self) -> list[TokenRecord]:
        """Return the stored tokens, sorted by name."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, scope, created_at, last_used_at, expires_at"
                " FROM tokens ORDER BY name"
            ).fetchall()
        return [_record(*row) for row in rows]

    def revoke(self, name: str) -> None:
        """Delete the token stored under name.

        Raises:
            UnknownTokenError: no token is stored under name.
        """
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM tokens WHERE name = ?", (name,)
            ).rowcount
        if deleted == 0:
            stored = [record.name for record in self.records()]
            raise UnknownTokenError(
                f"no token named {name!r}; stored names are {stored!r}"
            )

    def verify(self, token: str) -> TokenRecord | None:
        """Return the record of a stored, unexpired token, or None.

        Every stored digest is compared, so the time taken depends on the row
        count alone. Nothing is written.

        Raises:
            TokenStoreError: the store's file cannot be read.
        """
        with self._checked() as connection:
            return _match(connection, token, self._now())

    def authenticate(self, token: str) -> TokenRecord | None:
        """Verify token as verify does, and record its use on a match.

        The match and the last-used time are one transaction.

        Returns:
            The token's record with last_used_at set to now, or None when the
            token is unknown or expired.

        Raises:
            TokenStoreError: the store's file cannot be read.
        """
        used_at = self._now()
        with self._checked() as connection:
            matched = _match(connection, token, used_at)
            if matched is None:
                return None
            connection.execute(
                "UPDATE tokens SET last_used_at = ? WHERE name = ?",
                (used_at.isoformat(), matched.name),
            )
        return dataclasses.replace(matched, last_used_at=used_at)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside one transaction, and close it afterwards."""
        with closing(sqlite3.connect(self._path, timeout=5)) as connection, connection:
            yield connection

    @contextmanager
    def _checked(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection as _connect does, raising TokenStoreError on failure.

        Raises:
            TokenStoreError: any sqlite3.Error from the file or the block.
        """
        try:
            with self._connect() as connection:
                yield connection
        except sqlite3.Error as error:
            raise TokenStoreError(
                f"token store {str(self._path)!r} could not be read: {error}"
            ) from error


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _match(
    connection: sqlite3.Connection, token: str, now: datetime
) -> TokenRecord | None:
    """Return the record of token when stored and unexpired at now, or None.

    Every stored digest is compared, never stopping at a match.
    """
    presented = _digest(token)
    rows = connection.execute(
        "SELECT token_sha256, name, scope, created_at, last_used_at, expires_at"
        " FROM tokens"
    ).fetchall()
    matched = None
    for stored_digest, *fields in rows:
        if hmac.compare_digest(presented, stored_digest):
            matched = _record(*fields)
    if matched is None:
        return None
    if matched.expires_at is not None and matched.expires_at <= now:
        return None
    return matched


def _format_time(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _parse_time(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _record(
    name: str,
    scope: str,
    created_at: str,
    last_used_at: str | None,
    expires_at: str | None,
) -> TokenRecord:
    return TokenRecord(
        name=name,
        scope=scope,
        created_at=datetime.fromisoformat(created_at),
        last_used_at=_parse_time(last_used_at),
        expires_at=_parse_time(expires_at),
    )
