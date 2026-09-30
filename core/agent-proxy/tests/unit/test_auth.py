import hashlib
import hmac
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from agent_proxy.auth import (
    TokenNameInUseError,
    TokenRecord,
    TokenStore,
    TokenStoreError,
    UnknownTokenError,
)
from agent_proxy.config import InvalidNameError
from agent_proxy.errors import ProxyError

MINTED_AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
LATER = MINTED_AT + timedelta(minutes=5)
UNKNOWN_TOKEN = "apx_" + "x" * 43
NOT_SQLITE = b"this file is not a SQLite database\n" * 8


class Clock:
    """A settable clock the store reads through its now argument."""

    def __init__(self, moment: datetime) -> None:
        """Start the clock at moment."""
        self.moment = moment

    def __call__(self) -> datetime:
        """Return the current moment."""
        return self.moment


@pytest.fixture
def clock() -> Clock:
    """Return a clock stopped at MINTED_AT."""
    return Clock(MINTED_AT)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    """Return a database path that does not exist yet."""
    return tmp_path / "tokens.db"


@pytest.fixture
def store(path: Path, clock: Clock) -> TokenStore:
    """Return a store over a new database that reads the fixed clock."""
    return TokenStore(path, now=clock)


def test_token_errors_are_proxy_errors() -> None:
    """Both token errors subclass ProxyError."""
    assert issubclass(TokenNameInUseError, ProxyError)
    assert issubclass(UnknownTokenError, ProxyError)


def test_mint_returns_a_prefixed_token_of_47_characters(store: TokenStore) -> None:
    """A minted token is "apx_" and 43 URL-safe characters."""
    token = store.mint("ci-bot", "ci", None)

    assert token.startswith("apx_")
    assert len(token) == 47


def test_store_creates_the_database_file_owner_only(
    store: TokenStore, path: Path
) -> None:
    """The database file has mode 0o600."""
    store.mint("ci-bot", "ci", None)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_mint_stores_the_sha256_digest_and_not_the_token(
    store: TokenStore, path: Path
) -> None:
    """The row holds the token's SHA-256 digest, and the file never holds the token."""
    token = store.mint("ci-bot", "ci", None)

    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT token_sha256 FROM tokens").fetchall()
    assert rows == [(hashlib.sha256(token.encode()).digest(),)]
    assert token.encode() not in path.read_bytes()


def test_mint_stores_times_as_isoformat(store: TokenStore, path: Path) -> None:
    """Created and expiry times are stored as isoformat text; last-used is null."""
    store.mint("ci-bot", "ci", timedelta(hours=1))

    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT created_at, last_used_at, expires_at FROM tokens"
        ).fetchall()
    assert rows == [
        (MINTED_AT.isoformat(), None, (MINTED_AT + timedelta(hours=1)).isoformat())
    ]


def test_mint_refuses_an_invalid_token_name(store: TokenStore) -> None:
    """A token name outside NAME_PATTERN raises and stores nothing."""
    with pytest.raises(InvalidNameError, match="'CI Bot'"):
        store.mint("CI Bot", "ci", None)

    assert store.records() == []


def test_mint_refuses_an_invalid_scope_name(store: TokenStore) -> None:
    """A scope name outside NAME_PATTERN raises and stores nothing."""
    with pytest.raises(InvalidNameError, match="'-ci'"):
        store.mint("ci-bot", "-ci", None)

    assert store.records() == []


def test_mint_of_a_stored_name_raises_and_keeps_the_first_token(
    store: TokenStore,
) -> None:
    """A second mint of a name raises, stores nothing, and the first token verifies."""
    first = store.mint("ci-bot", "ci", None)

    with pytest.raises(TokenNameInUseError, match="'ci-bot'"):
        store.mint("ci-bot", "deploy", None)

    assert [record.scope for record in store.records()] == ["ci"]
    verified = store.verify(first)
    assert verified is not None
    assert verified.scope == "ci"


def test_records_hold_the_mint_times(store: TokenStore) -> None:
    """A record carries the mint time, a null last-used time, and the expiry."""
    store.mint("ci-bot", "ci", timedelta(hours=1))
    store.mint("deploy-bot", "deploy", None)

    assert store.records() == [
        TokenRecord(
            name="ci-bot",
            scope="ci",
            created_at=MINTED_AT,
            last_used_at=None,
            expires_at=MINTED_AT + timedelta(hours=1),
        ),
        TokenRecord(
            name="deploy-bot",
            scope="deploy",
            created_at=MINTED_AT,
            last_used_at=None,
            expires_at=None,
        ),
    ]


def test_records_are_sorted_by_name(store: TokenStore) -> None:
    """Records come back in name order, not mint order."""
    for name in ("zeta", "alpha", "mid"):
        store.mint(name, "ci", None)

    assert [record.name for record in store.records()] == ["alpha", "mid", "zeta"]


def test_record_repr_holds_neither_the_token_nor_its_digest(
    store: TokenStore,
) -> None:
    """A record's repr shows no token text and no hex digest."""
    token = store.mint("ci-bot", "ci", None)

    [record] = store.records()
    shown = repr(record)
    assert token not in shown
    assert token.removeprefix("apx_") not in shown
    assert hashlib.sha256(token.encode()).hexdigest() not in shown


def test_verify_returns_the_record_of_a_stored_token(store: TokenStore) -> None:
    """A stored token verifies to its own record."""
    store.mint("alpha", "ci", None)
    token = store.mint("beta", "deploy", None)

    assert store.verify(token) == store.records()[1]


def test_verify_returns_none_for_an_unknown_token(store: TokenStore) -> None:
    """A token the store never minted does not verify."""
    store.mint("ci-bot", "ci", None)

    assert store.verify("apx_" + "x" * 43) is None


def test_verify_returns_none_for_a_revoked_token(store: TokenStore) -> None:
    """A revoked token does not verify."""
    token = store.mint("ci-bot", "ci", None)
    store.revoke("ci-bot")

    assert store.verify(token) is None


@pytest.mark.parametrize(
    "elapsed", [timedelta(hours=1), timedelta(hours=1, seconds=1)], ids=["at", "past"]
)
def test_verify_returns_none_once_the_expiry_is_reached(
    store: TokenStore, clock: Clock, elapsed: timedelta
) -> None:
    """A token whose expiry is at or before now does not verify."""
    token = store.mint("ci-bot", "ci", timedelta(hours=1))
    clock.moment = MINTED_AT + elapsed

    assert store.verify(token) is None


def test_verify_accepts_a_token_just_before_its_expiry(
    store: TokenStore, clock: Clock
) -> None:
    """A token verifies until the moment its expiry is reached."""
    token = store.mint("ci-bot", "ci", timedelta(hours=1))
    clock.moment = MINTED_AT + timedelta(hours=1) - timedelta(microseconds=1)

    assert store.verify(token) is not None


def test_a_token_with_no_expiry_verifies(store: TokenStore, clock: Clock) -> None:
    """A token minted without an expiry verifies however far the clock moves."""
    token = store.mint("ci-bot", "ci", None)
    clock.moment = MINTED_AT + timedelta(days=3650)

    assert store.verify(token) is not None


def test_revoke_deletes_the_record(store: TokenStore) -> None:
    """After revoke, the name is gone from the records."""
    store.mint("ci-bot", "ci", None)

    store.revoke("ci-bot")

    assert store.records() == []


def test_revoke_of_an_unknown_name_raises(store: TokenStore) -> None:
    """Revoking a name no token holds raises and names it."""
    store.mint("ci-bot", "ci", None)

    with pytest.raises(UnknownTokenError, match="'ghost'"):
        store.revoke("ghost")


def test_a_revoked_name_mints_again(store: TokenStore) -> None:
    """After revoke the name mints a new token, and only the new one verifies."""
    old = store.mint("ci-bot", "ci", None)
    store.revoke("ci-bot")

    new = store.mint("ci-bot", "ci", None)

    assert store.verify(new) is not None
    assert store.verify(old) is None


def test_an_expired_token_keeps_its_name(store: TokenStore, clock: Clock) -> None:
    """An expired token still holds its name until it is revoked."""
    store.mint("ci-bot", "ci", timedelta(hours=1))
    clock.moment = MINTED_AT + timedelta(hours=2)

    with pytest.raises(TokenNameInUseError, match="'ci-bot'"):
        store.mint("ci-bot", "ci", None)


def test_a_new_store_over_the_same_file_keeps_the_tokens(
    store: TokenStore, path: Path, clock: Clock
) -> None:
    """Opening the database again neither wipes nor hides its tokens."""
    token = store.mint("ci-bot", "ci", None)

    reopened = TokenStore(path, now=clock)

    assert reopened.verify(token) == store.records()[0]


def test_store_connects_with_a_five_second_timeout(path: Path, clock: Clock) -> None:
    """Every connection the store opens waits up to 5 seconds for a lock."""
    connect = mock.Mock(wraps=sqlite3.connect)
    with mock.patch("agent_proxy.auth.sqlite3.connect", connect):
        store = TokenStore(path, now=clock)
        token = store.mint("ci-bot", "ci", None)
        store.verify(token)

    assert connect.call_count >= 3
    for call in connect.call_args_list:
        assert call.kwargs["timeout"] == 5


def test_store_closes_every_connection_it_opens(path: Path, clock: Clock) -> None:
    """No connection stays open after any store call, a failing one included."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    with mock.patch("agent_proxy.auth.sqlite3.connect", recording_connect):
        store = TokenStore(path, now=clock)
        token = store.mint("ci-bot", "ci", None)
        with pytest.raises(TokenNameInUseError):
            store.mint("ci-bot", "ci", None)
        store.records()
        store.verify(token)
        store.revoke("ci-bot")
        with pytest.raises(UnknownTokenError):
            store.revoke("ci-bot")

    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def compare_digest_calls(store: TokenStore, token: str) -> list[tuple[object, ...]]:
    """Return the arguments of each hmac.compare_digest call one verify makes."""
    compare = mock.Mock(wraps=hmac.compare_digest)
    with mock.patch("agent_proxy.auth.hmac.compare_digest", compare):
        store.verify(token)
    return [call.args for call in compare.call_args_list]


def assert_one_digest_comparison_per_row(
    store: TokenStore, token: str, rows: int
) -> None:
    """Assert verify compares two 32-byte digests once for each stored row."""
    calls = compare_digest_calls(store, token)
    assert len(calls) == rows
    for args in calls:
        assert len(args) == 2
        for digest in args:
            assert isinstance(digest, bytes)
            assert len(digest) == 32


@pytest.fixture
def minted(store: TokenStore) -> list[str]:
    """Mint alpha, beta, and gamma in that order; gamma expires after an hour."""
    return [
        store.mint("alpha", "ci", None),
        store.mint("beta", "ci", None),
        store.mint("gamma", "ci", timedelta(hours=1)),
    ]


def test_verify_compares_every_row_for_the_first_stored_token(
    store: TokenStore, minted: list[str]
) -> None:
    """A match on the first row does not end the comparisons."""
    assert_one_digest_comparison_per_row(store, minted[0], rows=3)
    verified = store.verify(minted[0])
    assert verified is not None
    assert verified.name == "alpha"


def test_verify_compares_every_row_for_the_last_stored_token(
    store: TokenStore, minted: list[str]
) -> None:
    """The last stored token costs the same comparisons as the first."""
    assert_one_digest_comparison_per_row(store, minted[2], rows=3)
    verified = store.verify(minted[2])
    assert verified is not None
    assert verified.name == "gamma"


def test_verify_compares_every_row_for_an_unknown_token(
    store: TokenStore, minted: list[str]
) -> None:
    """An unknown token costs one comparison per stored row."""
    assert_one_digest_comparison_per_row(store, "apx_" + "x" * 43, rows=3)


def test_verify_compares_every_row_for_an_expired_token(
    store: TokenStore, minted: list[str], clock: Clock
) -> None:
    """An expired token costs one comparison per stored row and does not verify."""
    clock.moment = MINTED_AT + timedelta(hours=2)

    assert_one_digest_comparison_per_row(store, minted[2], rows=3)
    assert store.verify(minted[2]) is None


def test_verify_compares_every_remaining_row_for_a_revoked_token(
    store: TokenStore, minted: list[str]
) -> None:
    """A revoked token costs one comparison per row that remains."""
    store.revoke("alpha")

    assert_one_digest_comparison_per_row(store, minted[0], rows=2)


def _last_used(store: TokenStore, name: str) -> datetime | None:
    [record] = [record for record in store.records() if record.name == name]
    return record.last_used_at


def test_token_store_error_is_a_proxy_error() -> None:
    """TokenStoreError subclasses ProxyError."""
    assert issubclass(TokenStoreError, ProxyError)


def test_authenticate_returns_the_record_with_last_used_at_set(
    store: TokenStore, clock: Clock
) -> None:
    """A live token's record comes back with last_used_at at the clock's time."""
    token = store.mint("ci-bot", "ci", None)
    clock.moment = LATER

    assert store.authenticate(token) == TokenRecord(
        name="ci-bot",
        scope="ci",
        created_at=MINTED_AT,
        last_used_at=LATER,
        expires_at=None,
    )


def test_authenticate_stores_last_used_at(store: TokenStore, clock: Clock) -> None:
    """The last-used time is written to the store."""
    token = store.mint("ci-bot", "ci", None)
    clock.moment = LATER

    store.authenticate(token)

    assert _last_used(store, "ci-bot") == LATER


def test_authenticate_of_an_unknown_token_returns_none_and_writes_nothing(
    store: TokenStore, path: Path
) -> None:
    """An unknown token neither authenticates nor changes the file."""
    store.mint("ci-bot", "ci", None)
    before = path.read_bytes()

    assert store.authenticate(UNKNOWN_TOKEN) is None
    assert path.read_bytes() == before
    assert _last_used(store, "ci-bot") is None


def test_authenticate_of_an_expired_token_returns_none_and_writes_nothing(
    store: TokenStore, path: Path, clock: Clock
) -> None:
    """An expired token neither authenticates nor records its use."""
    token = store.mint("ci-bot", "ci", timedelta(hours=1))
    clock.moment = MINTED_AT + timedelta(hours=2)
    before = path.read_bytes()

    assert store.authenticate(token) is None
    assert path.read_bytes() == before
    assert _last_used(store, "ci-bot") is None


def test_authenticate_compares_every_row(store: TokenStore, minted: list[str]) -> None:
    """A match on the first row does not end authenticate's comparisons."""
    compare = mock.Mock(wraps=hmac.compare_digest)
    with mock.patch("agent_proxy.auth.hmac.compare_digest", compare):
        store.authenticate(minted[0])

    assert compare.call_count == 3


def test_verify_never_writes(store: TokenStore, path: Path) -> None:
    """Verifying a live token leaves the file and last_used_at unchanged."""
    token = store.mint("ci-bot", "ci", None)
    before = path.read_bytes()

    verified = store.verify(token)

    assert verified is not None
    assert verified.last_used_at is None
    assert path.read_bytes() == before
    assert _last_used(store, "ci-bot") is None


@pytest.mark.parametrize("method", ["verify", "authenticate"])
def test_an_unreadable_store_raises_token_store_error(
    store: TokenStore, path: Path, method: str
) -> None:
    """A file overwritten with bytes that are not SQLite raises TokenStoreError."""
    token = store.mint("ci-bot", "ci", None)
    path.write_bytes(NOT_SQLITE)

    with pytest.raises(TokenStoreError) as caught:
        getattr(store, method)(token)

    message = str(caught.value)
    assert message.startswith(f"token store {str(path)!r} could not be read: ")
    assert token not in message
