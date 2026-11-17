import contextlib
import json
import re
import shutil
import socket
import stat
import tempfile
import threading
from collections.abc import Callable, Collection, Iterable, Iterator
from pathlib import Path
from types import MappingProxyType
from unittest import mock

import docker.errors
import pytest
import requests
import yaml

from agent_proxy.config import ConfigError, load_config
from agent_proxy.credentials import CredentialStore
from agent_proxy.egress import EgressError
from agent_proxy.errors import ProxyError
from agent_proxy.mcp import snapshot_builder
from agent_proxy.reload import (
    CONTROL_SOCKET_NAME,
    ControlError,
    ControlServer,
    LiveConfig,
    ReloadReply,
    Snapshot,
    request_reload,
)

WARNING = "plugins.git.actions.raw: declares pass-through args"
WAIT_SECONDS = 5
PLUGIN_IMAGE = "ghcr.io/example/tools@sha256:" + "a" * 64
CONFIG = {
    "agent_tree": "/srv/agents/alpha",
    "overflow_dir": "/srv/agents/alpha/overflow",
    "max_cages": 4,
    "plugins": {},
    "scopes": {"ci": {"grants": {}}},
}


class RecordingEgress:
    """Records each ensure and prune call with the lists it was given."""

    def __init__(self, ensure_error: ProxyError | None = None) -> None:
        """Start with no calls; ensure raises ensure_error after recording, if set."""
        self.calls: list[tuple[str, set[tuple[str, ...]]]] = []
        self._ensure_error = ensure_error

    def ensure(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists, then raise ensure_error if one was given."""
        self.calls.append(("ensure", {tuple(domains) for domains in lists}))
        if self._ensure_error is not None:
            raise self._ensure_error

    def prune(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists."""
        self.calls.append(("prune", {tuple(domains) for domains in lists}))


@pytest.fixture
def socket_path() -> Iterator[Path]:
    """A socket path in a short directory, since an AF_UNIX path caps at 107 bytes."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    yield directory / CONTROL_SOCKET_NAME
    shutil.rmtree(directory)


def _snapshot(tmp_path: Path, warnings: tuple[str, ...] = ()) -> Snapshot:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return Snapshot(
        config=load_config(path),
        manifests=MappingProxyType({}),
        warnings=warnings,
    )


@contextlib.contextmanager
def _serving(
    socket_path: Path, live: LiveConfig, build: Callable[[], Snapshot]
) -> Iterator[ControlServer]:
    server = ControlServer(socket_path, live, build)
    server.start()
    try:
        yield server
    finally:
        server.close()


def _ask(socket_path: Path, request: bytes) -> dict[str, object]:
    """Send request as raw bytes and return the decoded reply line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(WAIT_SECONDS)
        connection.connect(str(socket_path))
        connection.sendall(request)
        with connection.makefile("rb") as replies:
            return json.loads(replies.readline())


@contextlib.contextmanager
def _stub_server(
    socket_path: Path, respond: Callable[[socket.socket], None]
) -> Iterator[None]:
    """Serve one connection on socket_path with respond, then close it."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            respond(connection)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        thread.join(WAIT_SECONDS)
        listener.close()


def _replying(reply: bytes) -> Callable[[socket.socket], None]:
    def respond(connection: socket.socket) -> None:
        connection.recv(64)
        connection.sendall(reply)

    return respond


def _refusal_naming(socket_path: Path) -> pytest.RaisesExc[ControlError]:
    return pytest.raises(ControlError, match=re.escape(str(socket_path)))


def _refuse_twice() -> Snapshot:
    raise ConfigError("config file 'a.yaml': first", "config file 'a.yaml': second")


def test_a_valid_reload_replies_ok_with_its_warnings_and_swaps_current(
    tmp_path: Path, socket_path: Path
) -> None:
    """A reload request puts the built snapshot in force and replies its warnings."""
    new = _snapshot(tmp_path, (WARNING,))
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, lambda: new):
        reply = _ask(socket_path, b"reload\n")

    assert reply == {"ok": True, "warnings": [WARNING]}
    assert live.current is new


def test_a_config_error_replies_every_error_and_keeps_current(
    tmp_path: Path, socket_path: Path
) -> None:
    """A refused build replies each failure, and the running config keeps serving."""
    old = _snapshot(tmp_path)
    live = LiveConfig(old, RecordingEgress())

    with _serving(socket_path, live, _refuse_twice):
        reply = _ask(socket_path, b"reload\n")

    assert reply == {
        "ok": False,
        "errors": ["config file 'a.yaml': first", "config file 'a.yaml': second"],
    }
    assert live.current is old


def test_another_proxy_error_replies_its_message_as_the_one_error(
    tmp_path: Path, socket_path: Path
) -> None:
    """A ProxyError that is not a ConfigError is one error, and current is kept."""
    old = _snapshot(tmp_path)
    live = LiveConfig(old, RecordingEgress())

    def unreachable() -> Snapshot:
        raise ProxyError("the Docker daemon is unreachable")

    with _serving(socket_path, live, unreachable):
        reply = _ask(socket_path, b"reload\n")

    assert reply == {"ok": False, "errors": ["the Docker daemon is unreachable"]}
    assert live.current is old


def test_an_egress_error_replies_its_message_and_keeps_current(
    tmp_path: Path, socket_path: Path
) -> None:
    """A reload whose ensure raises EgressError replies its message as the one error."""
    old = _snapshot(tmp_path)
    egress = RecordingEgress(ensure_error=EgressError("egress proxy did not start"))
    live = LiveConfig(old, egress)

    with _serving(socket_path, live, lambda: _snapshot(tmp_path, (WARNING,))):
        reply = _ask(socket_path, b"reload\n")

    assert reply == {"ok": False, "errors": ["egress proxy did not start"]}
    assert live.current is old


def test_an_unknown_request_is_refused_without_a_build(
    tmp_path: Path, socket_path: Path
) -> None:
    """A request line other than reload is refused by name, and nothing is built."""
    old = _snapshot(tmp_path)
    live = LiveConfig(old, RecordingEgress())
    builds: list[None] = []

    def build() -> Snapshot:
        builds.append(None)
        return old

    with _serving(socket_path, live, build):
        reply = _ask(socket_path, b"status\n")

    assert reply == {
        "ok": False,
        "errors": ["unknown request 'status'; expected 'reload'"],
    }
    assert builds == []


def test_a_request_line_is_read_up_to_64_bytes(
    tmp_path: Path, socket_path: Path
) -> None:
    """The server stops reading at 64 bytes, so a line with no newline still ends."""
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, _refuse_twice):
        reply = _ask(socket_path, b"x" * 64)

    assert reply == {
        "ok": False,
        "errors": [f"unknown request {'x' * 64!r}; expected 'reload'"],
    }


def test_the_socket_is_owner_only(tmp_path: Path, socket_path: Path) -> None:
    """The bound socket has mode 0o600."""
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, _refuse_twice):
        mode = socket_path.stat().st_mode

    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o600


def test_a_stale_socket_is_replaced(tmp_path: Path, socket_path: Path) -> None:
    """A socket file left by an earlier process is unlinked and bound again."""
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, lambda: _snapshot(tmp_path)):
        reply = request_reload(socket_path)

    assert reply.ok


def test_a_path_that_is_not_a_socket_is_refused_and_kept(
    tmp_path: Path, socket_path: Path
) -> None:
    """Start refuses to replace a regular file, naming its path."""
    socket_path.write_text("keep", encoding="utf-8")
    server = ControlServer(
        socket_path, LiveConfig(_snapshot(tmp_path), RecordingEgress()), _refuse_twice
    )

    with _refusal_naming(socket_path):
        server.start()

    assert socket_path.read_text(encoding="utf-8") == "keep"


def test_a_socket_that_cannot_be_bound_is_refused(
    tmp_path: Path, socket_path: Path
) -> None:
    """Start refuses a path in a directory that does not exist, naming the path."""
    absent = socket_path.parent / "absent" / CONTROL_SOCKET_NAME
    server = ControlServer(
        absent, LiveConfig(_snapshot(tmp_path), RecordingEgress()), _refuse_twice
    )

    with _refusal_naming(absent):
        server.start()


def test_close_removes_the_socket(tmp_path: Path, socket_path: Path) -> None:
    """After close, the socket file is gone."""
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, _refuse_twice):
        assert socket_path.exists()

    assert not socket_path.exists()


def test_request_reload_returns_the_warnings_of_a_valid_reload(
    tmp_path: Path, socket_path: Path
) -> None:
    """An ok reply becomes a ReloadReply holding its warnings."""
    new = _snapshot(tmp_path, (WARNING,))
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, lambda: new):
        reply = request_reload(socket_path)

    assert reply == ReloadReply(ok=True, warnings=(WARNING,), errors=())


def test_request_reload_returns_the_errors_of_a_refused_reload(
    tmp_path: Path, socket_path: Path
) -> None:
    """A refused reply becomes a ReloadReply holding its errors."""
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, _refuse_twice):
        reply = request_reload(socket_path)

    assert reply == ReloadReply(
        ok=False,
        warnings=(),
        errors=("config file 'a.yaml': first", "config file 'a.yaml': second"),
    )


def test_request_reload_refuses_a_path_with_nothing_on_it(socket_path: Path) -> None:
    """No socket file is a ControlError naming the path."""
    with _refusal_naming(socket_path):
        request_reload(socket_path)


def test_request_reload_refuses_a_socket_nothing_listens_on(socket_path: Path) -> None:
    """A socket file with no listener is a ControlError naming the path."""
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()

    with _refusal_naming(socket_path):
        request_reload(socket_path)


def test_request_reload_refuses_a_socket_path_it_cannot_connect_to(
    socket_path: Path,
) -> None:
    """Any other connect failure, here a path over the AF_UNIX cap, is refused."""
    too_long = socket_path.parent / ("x" * 200) / CONTROL_SOCKET_NAME

    with _refusal_naming(too_long):
        request_reload(too_long)


def test_request_reload_refuses_a_connection_closed_with_no_reply(
    socket_path: Path,
) -> None:
    """A server that reads the request and closes is a ControlError."""
    with _stub_server(socket_path, _replying(b"")), _refusal_naming(socket_path):
        request_reload(socket_path)


def test_request_reload_refuses_a_connection_closed_before_the_request_is_read(
    socket_path: Path,
) -> None:
    """A server that closes without reading is a ControlError, not an OSError."""
    with _stub_server(socket_path, lambda connection: None):
        with _refusal_naming(socket_path):
            request_reload(socket_path)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(b"not json\n", id="not-json"),
        pytest.param(b"\xff\xfe\n", id="not-utf-8"),
        pytest.param(b"[]\n", id="not-an-object"),
        pytest.param(b'{"warnings": []}\n', id="no-ok"),
        pytest.param(b'{"ok": 1, "warnings": []}\n', id="ok-not-a-boolean"),
        pytest.param(b'{"ok": true}\n', id="ok-without-warnings"),
        pytest.param(b'{"ok": false, "warnings": []}\n', id="refusal-without-errors"),
        pytest.param(b'{"ok": true, "warnings": "none"}\n', id="warnings-not-a-list"),
        pytest.param(b'{"ok": false, "errors": [1]}\n', id="error-not-a-string"),
    ],
)
def test_request_reload_refuses_a_reply_it_cannot_read(
    socket_path: Path, reply: bytes
) -> None:
    """A reply that is not the reply JSON line is a ControlError naming the path."""
    with _stub_server(socket_path, _replying(reply)), _refusal_naming(socket_path):
        request_reload(socket_path)


def test_request_reload_refuses_a_reply_that_does_not_arrive_in_time(
    socket_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that holds the connection past the timeout is a ControlError."""
    monkeypatch.setattr("agent_proxy.reload.RELOAD_REPLY_TIMEOUT_SECONDS", 0.1)
    released = threading.Event()

    def hold(connection: socket.socket) -> None:
        connection.recv(64)
        released.wait(WAIT_SECONDS)

    with _stub_server(socket_path, hold):
        with _refusal_naming(socket_path):
            request_reload(socket_path)
        released.set()


def _pull_refused(client: mock.MagicMock) -> None:
    client.images.get.side_effect = docker.errors.ImageNotFound("no such image")
    client.images.pull.side_effect = docker.errors.APIError("pull access denied")


def _pull_unreachable(client: mock.MagicMock) -> None:
    client.images.get.side_effect = docker.errors.ImageNotFound("no such image")
    client.images.pull.side_effect = requests.exceptions.ConnectionError(
        "connection refused"
    )


def _describe_refused(client: mock.MagicMock) -> None:
    client.containers.create.side_effect = docker.errors.APIError("create refused")


@pytest.mark.parametrize(
    ("break_docker", "error_text"),
    [
        pytest.param(_pull_refused, "pull access denied", id="pull-api-error"),
        pytest.param(
            _pull_unreachable, "connection refused", id="pull-connection-error"
        ),
        pytest.param(_describe_refused, "create refused", id="describe-api-error"),
    ],
)
def test_a_docker_failure_in_the_serving_build_replies_it_as_an_error(
    tmp_path: Path,
    socket_path: Path,
    break_docker: Callable[[mock.MagicMock], None],
    error_text: str,
) -> None:
    """snapshot_builder's Docker failures reach the reload reply as ok false."""
    config_path = tmp_path / "plugin-config.yaml"
    config_path.write_text(
        yaml.safe_dump({**CONFIG, "plugins": {"tools": PLUGIN_IMAGE}}),
        encoding="utf-8",
    )
    client = mock.MagicMock()
    break_docker(client)
    build = snapshot_builder(
        config_path, CredentialStore(tmp_path / "credentials"), client
    )
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())

    with _serving(socket_path, live, build):
        reply = request_reload(socket_path)

    assert reply.ok is False
    [error] = reply.errors
    assert error_text in error
