"""Snapshots of the config in force, the reference a reload swaps, and its socket."""

import json
import socket
import socketserver
import threading
from collections.abc import Callable, Iterator, Mapping, Set
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from agent_proxy.config import ConfigError, InstanceConfig, load_config
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import Manifest

CONTROL_SOCKET_NAME = "control.sock"
# A reload may pull plugin images, so the reply can take minutes.
RELOAD_REPLY_TIMEOUT_SECONDS = 600
_RELOAD_REQUEST = "reload"
_REQUEST_LIMIT_BYTES = 64
_SOCKET_MODE = 0o600
_REPLY_FORM = '{"ok": true, "warnings": [...]} or {"ok": false, "errors": [...]}'


@dataclass(frozen=True)
class Snapshot:
    """A config, the manifest of each of its plugins, and their warnings."""

    config: InstanceConfig
    manifests: MappingProxyType[str, Manifest]
    warnings: tuple[str, ...]


def build_snapshot(
    path: Path,
    credential_names: Set[str],
    load_manifest: Callable[[str], Manifest],
) -> Snapshot:
    """Return the snapshot of the config file at path.

    Args:
        path: the config file.
        credential_names: the names the credential store holds.
        load_manifest: returns the manifest of the image at a pinned
            reference, making the image present first.

    Raises:
        ConfigError: load_config refuses the file, and its error propagates
            unchanged with no plugin loaded; or a plugin fails to load, or a
            grant names an action its plugin's manifest does not declare. It
            holds one message per failure.
    """
    config = load_config(path, credential_names)
    manifests, failures = _load_manifests(config, load_manifest)
    failures.extend(_undeclared_action_failures(config, manifests))
    if failures:
        source = f"config file {str(path)!r}"
        raise ConfigError(*(f"{source}: {failure}" for failure in failures))
    return Snapshot(
        config=config,
        manifests=MappingProxyType(manifests),
        warnings=tuple(_warnings(manifests)),
    )


def _load_manifests(
    config: InstanceConfig, load_manifest: Callable[[str], Manifest]
) -> tuple[dict[str, Manifest], list[str]]:
    """Return each loaded plugin's manifest, and one message per failure line."""
    manifests: dict[str, Manifest] = {}
    failures: list[str] = []
    for plugin, image in config.plugins.items():
        try:
            manifests[plugin] = load_manifest(image)
        except ProxyError as exc:
            failures.extend(
                f"plugins.{plugin}: {line}" for line in str(exc).splitlines()
            )
    return manifests, failures


def _undeclared_action_failures(
    config: InstanceConfig, manifests: Mapping[str, Manifest]
) -> Iterator[str]:
    for scope_name, scope in config.scopes.items():
        for key in scope.grants:
            plugin, action = key.split(".", 1)
            # A plugin that failed to load is already reported, with no
            # manifest to check its grants against.
            if plugin in manifests and action not in manifests[plugin].actions:
                yield (
                    f"scopes.{scope_name}.grants.{key}: action {action!r} "
                    f"is not declared by plugin {plugin!r}"
                )


def _warnings(manifests: Mapping[str, Manifest]) -> Iterator[str]:
    for plugin, manifest in manifests.items():
        for warning in manifest.warnings():
            yield f"plugins.{plugin}.{warning}"


class LiveConfig:
    """The snapshot in force, which a reload replaces whole."""

    def __init__(self, snapshot: Snapshot) -> None:
        """Put snapshot in force."""
        self._current = snapshot
        self._reload_lock = threading.Lock()

    @property
    def current(self) -> Snapshot:
        """The snapshot in force.

        A call reads this once when it starts and uses that snapshot to its end.
        """
        return self._current

    def reload(self, build: Callable[[], Snapshot]) -> Snapshot:
        """Build a snapshot and put it in force, one reload at a time.

        Returns:
            The snapshot now in force.

        Raises:
            ConfigError: build refuses the config file; the snapshot in force
                is unchanged.
        """
        with self._reload_lock:
            snapshot = build()
            self._current = snapshot
        return snapshot


class ControlError(ProxyError):
    """The control socket cannot be bound or reached, or its reply cannot be read."""


@dataclass(frozen=True)
class ReloadReply:
    """What the serving process replied to a reload request."""

    ok: bool
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


class ControlServer:
    """Serves reload requests for a LiveConfig on a Unix socket."""

    def __init__(
        self, socket_path: Path, live: LiveConfig, build: Callable[[], Snapshot]
    ) -> None:
        """Hold the socket path, the config to reload, and how to build a snapshot."""
        self._socket_path = socket_path
        self._live = live
        self._build = build
        self._server = socketserver.UnixStreamServer(
            str(socket_path), _request_handler(self._answer), bind_and_activate=False
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="control-socket", daemon=True
        )

    def start(self) -> None:
        """Bind the socket owner-only and serve on a daemon thread until close.

        A socket already at the path is replaced.

        Raises:
            ControlError: the path holds something other than a socket, or the
                socket cannot be bound there.
        """
        try:
            _bind_owner_only(self._server, self._socket_path)
        except ControlError:
            self._server.server_close()
            raise
        self._thread.start()

    def close(self) -> None:
        """Stop serving, close the socket, and remove its file.

        Call it only after start has returned.
        """
        self._server.shutdown()
        self._server.server_close()
        self._socket_path.unlink(missing_ok=True)

    def _answer(self, request: str) -> dict[str, object]:
        if request != _RELOAD_REQUEST:
            return {
                "ok": False,
                "errors": [
                    f"unknown request {request!r}; expected {_RELOAD_REQUEST!r}"
                ],
            }
        try:
            snapshot = self._live.reload(self._build)
        except ConfigError as error:
            return {"ok": False, "errors": list(error.errors)}
        except ProxyError as error:
            return {"ok": False, "errors": [str(error)]}
        return {"ok": True, "warnings": list(snapshot.warnings)}


def _request_handler(
    answer: Callable[[str], dict[str, object]],
) -> type[socketserver.StreamRequestHandler]:
    class RequestHandler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            """Read one request line and write the one-line JSON reply."""
            line = self.rfile.readline(_REQUEST_LIMIT_BYTES)
            request = line.decode("utf-8", errors="replace").removesuffix("\n")
            reply = json.dumps(answer(request))
            self.wfile.write(f"{reply}\n".encode())

    return RequestHandler


def _bind_owner_only(server: socketserver.UnixStreamServer, path: Path) -> None:
    if path.is_socket():
        path.unlink()
    elif path.exists(follow_symlinks=False):
        raise ControlError(
            f"control socket {str(path)!r} is taken by something other than a"
            " socket; expected a socket or nothing at that path"
        )
    try:
        server.server_bind()
        # Listening starts only after the mode is set, so no connection is
        # accepted while the socket still has the umask's mode.
        path.chmod(_SOCKET_MODE)
        server.server_activate()
    except OSError as exc:
        raise ControlError(
            f"control socket {str(path)!r} cannot be bound: {exc.strerror}"
        ) from exc


def request_reload(socket_path: Path) -> ReloadReply:
    """Ask the process serving socket_path to reload its config.

    Returns:
        The reply: ok with the new snapshot's warnings, or not ok with one
        error per failure.

    Raises:
        ControlError: nothing listens on socket_path or it cannot be reached,
            the connection closes with no reply, the reply is not the reply
            JSON line, or it does not arrive within
            RELOAD_REPLY_TIMEOUT_SECONDS. It names socket_path.
    """
    source = f"control socket {str(socket_path)!r}"
    line = _exchange(socket_path, source)
    return _parse_reply(line, source)


def _exchange(socket_path: Path, source: str) -> bytes:
    """Send the reload request and return the reply line."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(RELOAD_REPLY_TIMEOUT_SECONDS)
        try:
            connection.connect(str(socket_path))
            connection.sendall(f"{_RELOAD_REQUEST}\n".encode())
            with connection.makefile("rb") as replies:
                line = replies.readline()
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ControlError(
                f"{source}: nothing is listening: {exc.strerror}"
            ) from exc
        except TimeoutError as exc:
            raise ControlError(
                f"{source}: no reply within {RELOAD_REPLY_TIMEOUT_SECONDS} seconds"
            ) from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ControlError(
                f"{source}: the connection closed with no reply"
            ) from exc
        except OSError as exc:
            raise ControlError(
                f"{source} cannot be reached: {exc.strerror or exc}"
            ) from exc
    if not line:
        raise ControlError(f"{source}: the connection closed with no reply")
    return line


def _parse_reply(line: bytes, source: str) -> ReloadReply:
    try:
        reply = json.loads(line.decode("utf-8"))
    except ValueError as exc:
        raise ControlError(
            f"{source}: reply {line!r} is not JSON; expected {_REPLY_FORM}"
        ) from exc
    match reply:
        case {"ok": True, "warnings": list() as warnings} if all(
            isinstance(warning, str) for warning in warnings
        ):
            return ReloadReply(ok=True, warnings=tuple(warnings), errors=())
        case {"ok": False, "errors": list() as errors} if all(
            isinstance(error, str) for error in errors
        ):
            return ReloadReply(ok=False, warnings=(), errors=tuple(errors))
    raise ControlError(f"{source}: reply {line!r} is not {_REPLY_FORM}")
