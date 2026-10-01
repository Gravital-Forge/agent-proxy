"""The cage runner: one action call in one short-lived, hardened container."""

import json
import re
import socket
import threading
import time
import types
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

import docker.types
from docker import DockerClient
from docker.models.containers import Container
from jsonschema import Draft202012Validator

from agent_proxy.config import EnvMount, FileMount, Grant, Root
from agent_proxy.credentials import CredentialStore, UnknownCredentialError
from agent_proxy.egress import CageNetwork
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import (
    DOCKER_FAILURES,
    WAIT_TIMEOUTS,
    CommandAction,
    ScriptAction,
)
from agent_proxy.results import (
    CALL_ID_PATTERN,
    CapturedStream,
    Stream,
    capture_stream,
    command_content,
    script_content,
)

CONTAINER_LABELS: Mapping[str, str] = types.MappingProxyType({})
TMPFS_PATH = "/tmp"  # noqa: S108 - the path is inside the cage, on its own tmpfs
TMPFS_OPTIONS = "rw,size=64m"
CONTAINER_USER = "0:0"
LOG_DRIVER = "json-file"
VERIFIER_MOUNT = "/.agent-proxy"
VERIFIER_PATH = f"{VERIFIER_MOUNT}/verify"
VERIFIER_REFUSED_EXIT = 125
_STDIN_ATTACH_PARAMS: Mapping[str, int] = types.MappingProxyType(
    {"stdin": 1, "stream": 1}
)


class RunnerError(ProxyError):
    """A cage call is refused or fails."""


class InputError(RunnerError):
    """A call's input breaks its action's schema or cannot form argv."""

    def __init__(self, *errors: str) -> None:
        """Hold one message per failure; str() prints them one per line."""
        self.errors = errors
        super().__init__("\n".join(errors))


class RootError(RunnerError):
    """A granted root has a symbolic link in its path."""


class CredentialError(RunnerError):
    """A granted credential is not stored or cannot be placed in a cage."""


class CageStartError(RunnerError):
    """A cage cannot be created or started."""


class CageTimeoutError(RunnerError):
    """A cage ran past its timeout."""


class CageFailedError(RunnerError):
    """A cage failed after it was created, other than by timing out."""


def validate_input(
    action: CommandAction | ScriptAction, arguments: Mapping[str, object]
) -> None:
    """Check arguments against the action's input schema, draft 2020-12.

    Raises:
        InputError: arguments break the schema; one message per violation.
    """
    validator = Draft202012Validator(action.input_schema)
    # The schema's object check accepts only a dict, not any Mapping.
    violations = [
        f"{error.json_path}: {error.message}"
        for error in validator.iter_errors(dict(arguments))
    ]
    if violations:
        raise InputError(*violations)


def form_argv(
    action: CommandAction, arguments: Mapping[str, object]
) -> tuple[str, ...]:
    """Return the argv the action forms from arguments that validate_input passed.

    Raises:
        InputError: a value a parameter places in argv begins with '-'.
    """
    argv = list(action.argv)
    for parameter in action.parameters:
        if parameter.name not in arguments:
            continue
        value = arguments[parameter.name]
        if parameter.flag is None:
            argv.append(_argv_text(parameter.name, value))
        elif value is True:
            argv.append(parameter.flag)
        elif value is not False:
            argv += (parameter.flag, _argv_text(parameter.name, value))
    argv += cast("list[str]", arguments.get("args", []))
    return tuple(argv)


def _argv_text(parameter_name: str, value: object) -> str:
    # Draft 2020-12 passes an integral float such as 5.0 as an integer.
    text = value if isinstance(value, str) else str(int(cast("float", value)))
    if text.startswith("-"):
        raise InputError(
            f"parameter {parameter_name!r} would place {text!r} in argv; "
            "a value placed in argv must not begin with '-'"
        )
    return text


def script_stdin(arguments: Mapping[str, object]) -> bytes:
    """Return arguments as the compact UTF-8 JSON document a script reads on stdin."""
    document = json.dumps(dict(arguments), separators=(",", ":"), ensure_ascii=False)
    return document.encode()


def check_roots(roots: Sequence[Root]) -> None:
    """Refuse a root whose path holds a symbolic link at any component.

    A component that does not exist is not a link; Docker refuses the bind of
    a missing path at create.

    Raises:
        RootError: a component of a root's path is a symbolic link.
    """
    # The daemon follows a symlinked bind source, so a link the agent planted in
    # its own tree would mount a path outside it.
    for root in roots:
        path = Path(root.path)
        for component in (*reversed(path.parents), path):
            if component.is_symlink():
                raise RootError(
                    f"root {root.path!r} has a symbolic link at {str(component)!r}; "
                    "a root's path must hold no symbolic link"
                )


def cage_environment(
    credentials: Mapping[str, EnvMount | FileMount], store: CredentialStore
) -> dict[str, str]:
    """Return the environment variables the grant's env credentials give a cage.

    Raises:
        CredentialError: a credential is not stored, is not UTF-8, or holds a NUL.
    """
    return {
        mount.env: _environment_value(name, store)
        for name, mount in credentials.items()
        if isinstance(mount, EnvMount)
    }


def _environment_value(name: str, store: CredentialStore) -> str:
    with _known_credentials():
        stored_bytes = store.read(name)
    try:
        value = stored_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # The decode error quotes the offending byte, so it must not travel as
        # the cause.
        raise CredentialError(
            f"credential {name!r} is not valid UTF-8, "
            "so it cannot be an environment value"
        ) from None
    if "\0" in value:
        raise CredentialError(
            f"credential {name!r} holds a NUL, so it cannot be an environment value"
        )
    return value


def cage_mounts(
    roots: Sequence[Root],
    credentials: Mapping[str, EnvMount | FileMount],
    store: CredentialStore,
) -> list[docker.types.Mount]:
    """Return the bind mounts of the grant's roots, then of its file credentials.

    Raises:
        CredentialError: a file credential is not stored.
    """
    root_mounts = [
        docker.types.Mount(
            target=root.path,
            source=root.path,
            type="bind",
            read_only=root.mode == "ro",
        )
        for root in roots
    ]
    with _known_credentials():
        # The SDK serialises a mount with json.dumps, which refuses a Path.
        credential_mounts = [
            docker.types.Mount(
                target=mount.file,
                source=str(store.path(name)),
                type="bind",
                read_only=True,
            )
            for name, mount in credentials.items()
            if isinstance(mount, FileMount)
        ]
    return root_mounts + credential_mounts


@contextmanager
def _known_credentials() -> Iterator[None]:
    """Turn the store's refusal of an unknown name into a CredentialError."""
    try:
        yield
    except UnknownCredentialError as error:
        raise CredentialError(str(error)) from error


class CageLimiter:
    """The count of an instance's running cages, shared by every call."""

    def __init__(self) -> None:
        """Start with no cage running."""
        self._condition = threading.Condition()
        self._running = 0

    @contextmanager
    def slot(self, limit: int) -> Iterator[None]:
        """Wait until fewer than limit cages run, then count one in for the body.

        The wait has no bound. The cage is counted out when the body ends, also
        when it raises.
        """
        with self._condition:
            self._condition.wait_for(lambda: self._running < limit)
            self._running += 1
        try:
            yield
        finally:
            with self._condition:
                self._running -= 1
                # Each call brings its own limit, so the waiter one notify would
                # wake may still be over its limit while another could go.
                self._condition.notify_all()


@dataclass(frozen=True)
class CageCall:
    """One action call, and what the instance's config gives it."""

    call_id: str
    image: str
    action: CommandAction | ScriptAction
    grant: Grant
    arguments: Mapping[str, object]
    network: CageNetwork
    overflow_dir: str
    max_cages: int


@dataclass(frozen=True)
class Outcome:
    """What a cage produced: its result, and what an audit record needs of it."""

    content: Mapping[str, object]
    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool


class CageRunner:
    """Runs each action call in a cage of its own, under the instance's cap."""

    def __init__(
        self, client: DockerClient, store: CredentialStore, limiter: CageLimiter
    ) -> None:
        """Hold the daemon client, the credential store, and the instance's limiter."""
        self._client = client
        self._store = store
        self._limiter = limiter

    def run(self, call: CageCall) -> Outcome:
        """Run call in a new cage, remove the cage, and return what it produced.

        Every refusal of the call ID, input, roots, or credentials comes before
        the call takes a slot. The slot is held until the cage is removed.

        Raises:
            InputError: the call ID or the input is refused.
            RootError: a root's path holds a symbolic link.
            CredentialError: a granted credential is not stored or cannot be
                placed in the cage.
            CageStartError: the cage cannot be created, attached, or started.
            CageTimeoutError: the cage runs past its timeout.
            CageFailedError: the cage fails after it starts, or cannot be removed.
            OverflowWriteError: a stream passes the cap and its overflow file
                cannot be written.
        """
        setup = _set_up(call, self._store)
        with self._limiter.slot(call.max_cages):
            container = _create(self._client, call, setup)
            try:
                return _run_container(container, call, setup)
            finally:
                _remove(container, call.action.name)


@dataclass(frozen=True)
class _CageSetup:
    entrypoint: tuple[str, ...]
    stdin: bytes | None
    environment: Mapping[str, str]
    mounts: Sequence[docker.types.Mount]


@dataclass(frozen=True)
class _Deadline:
    action: str
    timeout: timedelta
    started: float

    def remaining(self) -> float:
        """Return the seconds left before the timeout.

        Raises:
            CageTimeoutError: no time is left.
        """
        left = self.started + self.timeout.total_seconds() - time.monotonic()
        if left <= 0:
            raise self.expired()
        return left

    def expired(self) -> CageTimeoutError:
        """Return the error that refuses the call for running past its timeout."""
        return CageTimeoutError(
            f"action {self.action!r} did not finish within its timeout of "
            f"{self.timeout.total_seconds():g} seconds"
        )

    def elapsed_ms(self) -> int:
        """Return the whole milliseconds since the cage started."""
        return round((time.monotonic() - self.started) * 1000)


@dataclass(frozen=True)
class _StdinFeed:
    raw: socket.socket
    payload: bytes

    def send(self, deadline: _Deadline) -> None:
        """Send the payload, then end of file, within the time deadline leaves.

        Raises:
            CageTimeoutError: the time runs out before or while sending.
            CageFailedError: the socket fails otherwise.
        """
        self.raw.settimeout(deadline.remaining())
        try:
            self.raw.sendall(self.payload)
            self.raw.shutdown(socket.SHUT_WR)
        except TimeoutError as exc:
            raise deadline.expired() from exc
        except (BrokenPipeError, ConnectionResetError):
            # The script stopped reading its input; its exit tells the rest.
            return
        except OSError as exc:
            raise CageFailedError(
                f"action {deadline.action!r}: the cage's input could not be sent: {exc}"
            ) from exc


def _set_up(call: CageCall, store: CredentialStore) -> _CageSetup:
    """Check the call and return what its cage is created with.

    Raises:
        InputError: the call ID or the input is refused.
        RootError: a root's path holds a symbolic link.
        CredentialError: a granted credential is not stored or cannot be placed.
    """
    if re.fullmatch(CALL_ID_PATTERN, call.call_id) is None:
        raise InputError(f"call ID {call.call_id!r} does not match {CALL_ID_PATTERN!r}")
    validate_input(call.action, call.arguments)
    if isinstance(call.action, CommandAction):
        entrypoint = form_argv(call.action, call.arguments)
        stdin = None
    else:
        entrypoint = (call.action.executable,)
        stdin = script_stdin(call.arguments)
    check_roots(call.grant.roots)
    credentials = call.grant.credentials
    return _CageSetup(
        entrypoint=entrypoint,
        stdin=stdin,
        environment=cage_environment(credentials, store),
        mounts=cage_mounts(call.grant.roots, credentials, store),
    )


def _create(client: DockerClient, call: CageCall, setup: _CageSetup) -> Container:
    """Create the call's cage; its entrypoint also clears the image's command.

    Raises:
        CageStartError: Docker refuses the create, an absent image included.
    """
    try:
        return client.containers.create(
            call.image,
            entrypoint=list(setup.entrypoint),
            stdin_open=setup.stdin is not None,
            user=CONTAINER_USER,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            tmpfs={TMPFS_PATH: TMPFS_OPTIONS},
            mounts=list(setup.mounts),
            environment={**call.network.environment, **setup.environment},
            network=call.network.name,
            labels=dict(CONTAINER_LABELS),
            log_config=docker.types.LogConfig(type=LOG_DRIVER, config={}),
        )
    except DOCKER_FAILURES as exc:
        raise CageStartError(
            f"image {call.image!r}: the cage could not be created: {exc}"
        ) from exc


def _run_container(container: Container, call: CageCall, setup: _CageSetup) -> Outcome:
    with _attach_stdin(container, setup.stdin, call.image) as feed:
        deadline = _start(container, call)
        if feed is not None:
            feed.send(deadline)
        exit_code = _wait(container, deadline)
        duration_ms = deadline.elapsed_ms()
    stdout = _capture(container, call, "stdout")
    stderr = _capture(container, call, "stderr")
    return _outcome(call.action, exit_code, duration_ms, stdout, stderr)


@contextmanager
def _attach_stdin(
    container: Container, payload: bytes | None, image: str
) -> Iterator[_StdinFeed | None]:
    """Yield a feed attached to the cage's stdin, or None with no payload.

    Raises:
        CageStartError: the attach fails.
    """
    if payload is None:
        yield None
        return
    try:
        attached = container.attach_socket(params=dict(_STDIN_ATTACH_PARAMS))
    except DOCKER_FAILURES as exc:
        raise CageStartError(
            f"image {image!r}: the cage's stdin could not be attached: {exc}"
        ) from exc
    # attach_socket returns a SocketIO; only its raw socket can shut down the
    # write side, which is the one way to give the script end of file.
    raw = attached._sock
    try:
        yield _StdinFeed(raw=raw, payload=payload)
    finally:
        raw.close()


def _start(container: Container, call: CageCall) -> _Deadline:
    """Start the cage and return its deadline, timed from the start.

    Raises:
        CageStartError: the start fails.
    """
    try:
        container.start()
    except DOCKER_FAILURES as exc:
        raise CageStartError(
            f"image {call.image!r}: the cage could not be started: {exc}"
        ) from exc
    return _Deadline(
        action=call.action.name,
        timeout=call.grant.timeout or call.action.timeout,
        started=time.monotonic(),
    )


def _wait(container: Container, deadline: _Deadline) -> int:
    """Wait for the cage to exit within the time left, and return its exit code.

    Raises:
        CageTimeoutError: no time is left, or the wait outlasts it.
        CageFailedError: the wait fails otherwise.
    """
    seconds = deadline.remaining()
    try:
        result = container.wait(timeout=seconds)
    except WAIT_TIMEOUTS as exc:
        raise deadline.expired() from exc
    except DOCKER_FAILURES as exc:
        raise CageFailedError(
            f"action {deadline.action!r}: waiting for the cage failed: {exc}"
        ) from exc
    return result["StatusCode"]


def _capture(container: Container, call: CageCall, stream: Stream) -> CapturedStream:
    """Read one stream from the exited cage's log.

    Raises:
        CageFailedError: the logs call fails.
        OverflowWriteError: the stream passes the cap and its overflow file
            cannot be written.
    """
    try:
        chunks = container.logs(
            stdout=stream == "stdout", stderr=stream == "stderr", stream=True
        )
        return capture_stream(chunks, call.overflow_dir, call.call_id, stream)
    except DOCKER_FAILURES as exc:
        raise CageFailedError(
            f"action {call.action.name!r}: the cage's {stream} could not be read: {exc}"
        ) from exc


def _outcome(
    action: CommandAction | ScriptAction,
    exit_code: int,
    duration_ms: int,
    stdout: CapturedStream,
    stderr: CapturedStream,
) -> Outcome:
    if isinstance(action, ScriptAction):
        content = script_content(action.output_schema, exit_code, stdout, stderr)
    else:
        content = command_content(exit_code, stdout, stderr)
    return Outcome(
        content=content,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout_bytes=stdout.size,
        stderr_bytes=stderr.size,
        truncated=stdout.file is not None or stderr.file is not None,
    )


def _remove(container: Container, action_name: str) -> None:
    """Force-remove the cage, running or not.

    Raises:
        CageFailedError: the removal fails.
    """
    try:
        container.remove(force=True)
    except DOCKER_FAILURES as exc:
        raise CageFailedError(
            f"action {action_name!r}: cage {container.id!r} could not be removed: {exc}"
        ) from exc
