import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from agent_proxy.config import InvalidNameError
from agent_proxy.credentials import (
    CredentialEntry,
    CredentialStore,
    EmptyCredentialError,
    UnknownCredentialError,
)
from agent_proxy.errors import ProxyError


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    """Return a credentials directory path that does not exist yet."""
    return tmp_path / "credentials"


def test_set_stores_the_value_verbatim(directory: Path) -> None:
    """The stored bytes equal the input, trailing newline included."""
    CredentialStore(directory).set("github-token", b"s3cret\n")

    assert (directory / "github-token").read_bytes() == b"s3cret\n"


def test_set_writes_the_file_owner_only(directory: Path) -> None:
    """A stored credential file has mode 0o600."""
    CredentialStore(directory).set("github-token", b"s3cret")

    assert stat.S_IMODE((directory / "github-token").stat().st_mode) == 0o600


def test_store_creates_its_directory_owner_only(directory: Path) -> None:
    """A directory the store creates has mode 0o700."""
    CredentialStore(directory).set("github-token", b"s3cret")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_store_narrows_an_existing_wider_directory(directory: Path) -> None:
    """A directory that existed at 0o755 is narrowed to 0o700."""
    directory.mkdir()
    directory.chmod(0o755)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755

    CredentialStore(directory).set("github-token", b"s3cret")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_set_replaces_an_existing_value(directory: Path) -> None:
    """A second set replaces the value and leaves no other file behind."""
    store = CredentialStore(directory)
    store.set("github-token", b"old")

    store.set("github-token", b"new")

    assert (directory / "github-token").read_bytes() == b"new"
    assert [path.name for path in directory.iterdir()] == ["github-token"]


def test_set_leaves_no_file_behind_when_the_write_fails(directory: Path) -> None:
    """A write that fails part-way removes its temporary file and re-raises."""
    store = CredentialStore(directory)
    disk_full = OSError(28, "No space left on device")

    with (
        mock.patch("agent_proxy.credentials.os.fsync", side_effect=disk_full),
        pytest.raises(OSError, match="No space left"),
    ):
        store.set("github-token", b"s3cret")

    assert list(directory.iterdir()) == []


@pytest.mark.parametrize(
    "name",
    ["Bad_Name", "-leading-dash", "", "a" * 65, "../escape", "github-token\n"],
)
def test_set_refuses_an_invalid_name(directory: Path, name: str) -> None:
    """A name outside NAME_PATTERN is refused, named in the message, and not stored."""
    store = CredentialStore(directory)

    with pytest.raises(InvalidNameError) as raised:
        store.set(name, b"s3cret")

    assert repr(name) in str(raised.value)
    assert list(directory.iterdir()) == []


def test_set_accepts_a_name_of_64_characters(directory: Path) -> None:
    """The longest name NAME_PATTERN allows is stored."""
    name = "a" * 64

    CredentialStore(directory).set(name, b"s3cret")

    assert (directory / name).read_bytes() == b"s3cret"


def test_set_refuses_an_empty_value(directory: Path) -> None:
    """An empty value is refused and nothing is stored."""
    store = CredentialStore(directory)

    with pytest.raises(EmptyCredentialError, match="github-token"):
        store.set("github-token", b"")

    assert list(directory.iterdir()) == []


def test_entries_hide_temporary_files(directory: Path) -> None:
    """A file whose name is outside NAME_PATTERN is not an entry."""
    store = CredentialStore(directory)
    store.set("github-token", b"s3cret")
    (directory / ".github-token.0123abcd.tmp").write_bytes(b"partial")

    assert [entry.name for entry in store.entries()] == ["github-token"]


def test_entries_are_sorted_by_name(directory: Path) -> None:
    """Entries come back in name order, whatever the order they were set."""
    store = CredentialStore(directory)
    store.set("zulu", b"z")
    store.set("alpha", b"a")
    store.set("mike", b"m")

    assert [entry.name for entry in store.entries()] == ["alpha", "mike", "zulu"]


def test_entries_report_set_at_from_the_file_mtime(directory: Path) -> None:
    """An entry's set_at is the file's mtime as a UTC datetime."""
    store = CredentialStore(directory)
    store.set("github-token", b"s3cret")
    set_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    os.utime(directory / "github-token", (set_at.timestamp(), set_at.timestamp()))

    assert store.entries() == [CredentialEntry(name="github-token", set_at=set_at)]


def test_names_are_the_stored_names(directory: Path) -> None:
    """names() holds every stored credential name."""
    store = CredentialStore(directory)
    store.set("alpha", b"a")
    store.set("zulu", b"z")

    assert store.names() == frozenset({"alpha", "zulu"})


def test_read_returns_the_stored_bytes(directory: Path) -> None:
    """read() returns the value verbatim, trailing newline included."""
    store = CredentialStore(directory)
    store.set("github-token", b"s3cret\n")

    assert store.read("github-token") == b"s3cret\n"


def test_path_is_the_credential_file(directory: Path) -> None:
    """path() names the file the store keeps the credential in."""
    store = CredentialStore(directory)
    store.set("github-token", b"s3cret")

    assert store.path("github-token") == directory / "github-token"


@pytest.mark.parametrize("method", ["read", "path"])
def test_read_and_path_refuse_an_unknown_name(directory: Path, method: str) -> None:
    """A valid name the store does not hold is refused and named."""
    store = CredentialStore(directory)
    store.set("alpha", b"a")

    with pytest.raises(UnknownCredentialError, match="github-token"):
        getattr(store, method)("github-token")


@pytest.mark.parametrize("method", ["read", "path"])
@pytest.mark.parametrize("name", ["Bad_Name", "../escape", "github-token\n"])
def test_read_and_path_refuse_an_invalid_name(
    tmp_path: Path, directory: Path, method: str, name: str
) -> None:
    """A name outside NAME_PATTERN is refused as invalid, even if a file matches it."""
    (tmp_path / "escape").write_bytes(b"outside")
    store = CredentialStore(directory)

    with pytest.raises(InvalidNameError) as raised:
        getattr(store, method)(name)

    assert repr(name) in str(raised.value)


def test_remove_deletes_the_credential(directory: Path) -> None:
    """A removed credential's file is gone and it is no longer an entry."""
    store = CredentialStore(directory)
    store.set("github-token", b"s3cret")

    store.remove("github-token")

    assert not (directory / "github-token").exists()
    assert store.entries() == []


def test_remove_refuses_an_unknown_name(directory: Path) -> None:
    """Removing a name that is not stored raises and names it."""
    store = CredentialStore(directory)

    with pytest.raises(UnknownCredentialError, match="github-token"):
        store.remove("github-token")


def test_remove_never_reaches_outside_the_store(
    tmp_path: Path, directory: Path
) -> None:
    """A path-shaped name is unknown, and the file it points at survives."""
    outside = tmp_path / "tokens.sqlite3"
    outside.write_bytes(b"keep")
    store = CredentialStore(directory)

    with pytest.raises(UnknownCredentialError):
        store.remove("../tokens.sqlite3")

    assert outside.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "error", [InvalidNameError, EmptyCredentialError, UnknownCredentialError]
)
def test_errors_are_proxy_errors(error: type[Exception]) -> None:
    """The operator command catches every credential error as a ProxyError."""
    assert issubclass(error, ProxyError)
