"""The cage runner: one action call in one short-lived, hardened container."""

import errno
import json
import os
import re
import secrets
import socket
import threading
import time
import types
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import cast

import docker.types
from docker import DockerClient
from docker.models.containers import Container

from agent_proxy.cage_seccomp import cage_profile_json
from agent_proxy.config import (
    PINNED_IMAGE_PATTERN,
    ROOTS_ENV_NAME,
    EnvMount,
    FileMount,
    Grant,
    Root,
)
from agent_proxy.credentials import (
    CredentialStore,
    UnknownCredentialError,
    unset_message,
)
from agent_proxy.egress import CageNetwork
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import (
    DOCKER_FAILURES,
    WAIT_TIMEOUTS,
    CommandAction,
    ScriptAction,
    ensure_image,
    schema_validator,
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
VERIFIER_IMAGE_VARIABLE = "AGENT_PROXY_VERIFIER_IMAGE"
SOURCE_VERIFIER_IMAGE = "agent-proxy-verifier:1"


def verifier_image(environ: Mapping[str, str]) -> str:
    """Return the verifier reference environ names, or the source build's tag."""
    return environ.get(VERIFIER_IMAGE_VARIABLE) or SOURCE_VERIFIER_IMAGE


VERIFIER_IMAGE = verifier_image(os.environ)
VERIFIER_MOUNT = "/.agent-proxy"
VERIFIER_PATH = f"{VERIFIER_MOUNT}/verify"
VERIFIER_REFUSED_EXIT = 125
_VERIFIER_LINE_BYTES = 4096
_NONCE_BYTES = 16
_DIRECTORY_FLAGS = os.O_PATH | os.O_DIRECTORY
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
    """A granted root is refused, at the runner's check or in the cage.

    The check refuses a path that holds a symbolic link, a non-directory, or a
    missing component. The cage refuses a root mount that is not the directory
    the check held open.
    """


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
    validator = schema_validator(action.input_schema)
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


@dataclass(frozen=True)
class RootIdentity:
    """A granted root's path, and the device and inode of the directory held there."""

    path: str
    device: int
    inode: int


def hold_roots(roots: Sequence[Root], held: ExitStack) -> list[RootIdentity]:
    """Open each root's directory without following a link, and hold it on held.

    The walk opens one component at a time from '/', so no link anywhere in a
    root's path is followed. While held is open, no other directory can take
    a held directory's inode.

    Raises:
        RootError: a component of a root's path is a symbolic link, is not a
            directory, does not exist, or cannot be opened.
    """
    # The daemon follows a symlinked bind source, so a link the agent planted in
    # its own tree would mount a path outside it.
    return [_hold_root(root, held) for root in roots]


def _hold_root(root: Root, held: ExitStack) -> RootIdentity:
    descriptor = _open_component(root.path, "/", _DIRECTORY_FLAGS, None)
    for component in PurePosixPath(root.path).parts[1:]:
        parent = descriptor
        try:
            descriptor = _open_component(
                root.path, component, _DIRECTORY_FLAGS | os.O_NOFOLLOW, parent
            )
        finally:
            os.close(parent)
    held.callback(os.close, descriptor)
    status = os.fstat(descriptor)
    return RootIdentity(path=root.path, device=status.st_dev, inode=status.st_ino)


def _open_component(
    root_path: str, component: str, flags: int, dir_fd: int | None
) -> int:
    """Open one component of a root's path and return its descriptor.

    Raises:
        RootError: the open fails; the message names the root and the component.
    """
    try:
        return os.open(component, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise RootError(
            f"root {root_path!r}: component {component!r} {_open_refusal(exc)}; "
            "a root's path must be directories only, with no symbolic link"
        ) from exc


def _open_refusal(exc: OSError) -> str:
    # O_NOFOLLOW with O_DIRECTORY gives ENOTDIR for a link and a file alike.
    if exc.errno == errno.ENOTDIR:
        return "is a symbolic link or not a directory"
    if exc.errno == errno.ENOENT:
        return "does not exist"
    return f"cannot be opened: {exc.strerror}"


def roots_environment(roots: Sequence[Root]) -> dict[str, str]:
    """Return the variable that names the grant's root paths to a cage, in order."""
    return {
        ROOTS_ENV_NAME: json.dumps([root.path for root in roots], separators=(",", ":"))
    }


def cage_environment(
    credentials: Mapping[str, EnvMount | FileMount], store: CredentialStore
) -> dict[str, str]:
    """Return the environment variables the grant's env credentials give a cage.

    Raises:
        CredentialError: a credential is not stored, is not UTF-8, or holds a
            NUL. For one not stored, the message is unset_message of the first
            such credential in grant order.
    """
    return {
        mount.env: _environment_value(name, store)
        for name, mount in credentials.items()
        if isinstance(mount, EnvMount)
    }


def _environment_value(name: str, store: CredentialStore) -> str:
    with _stored_credential(name):
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
        CredentialError: a file credential is not stored; the message is
            unset_message of the first such credential in grant order.
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
    credential_mounts = [
        _credential_mount(name, mount, store)
        for name, mount in credentials.items()
        if isinstance(mount, FileMount)
    ]
    return root_mounts + credential_mounts


def _credential_mount(
    name: str, mount: FileMount, store: CredentialStore
) -> docker.types.Mount:
    with _stored_credential(name):
        source = store.path(name)
    # The SDK serialises a mount with json.dumps, which refuses a Path.
    return docker.types.Mount(
        target=mount.file, source=str(source), type="bind", read_only=True
    )


@contextmanager
def _stored_credential(name: str) -> Iterator[None]:
    """Turn the store's refusal of name into a CredentialError of unset_message."""
    try:
        yield
    except UnknownCredentialError as error:
        raise CredentialError(unset_message(name)) from error


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


def ensure_verifier(client: DockerClient) -> None:
    """Pull VERIFIER_IMAGE when it is pinned by digest and absent from the daemon.

    A tag reference is neither looked up nor pulled: it must already be on
    the daemon, because an image mount never pulls.

    Raises:
        PluginImageError: a pinned verifier cannot be looked up or pulled.
    """
    if re.fullmatch(PINNED_IMAGE_PATTERN, VERIFIER_IMAGE):
        ensure_image(client, VERIFIER_IMAGE)


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
        the call takes a slot. The slot is held until the cage is removed. Each
        root's directory is held open from its check until the run ends,
        whether it returns or raises; a cage, if there is one, is removed
        before the directories close. The cage's verifier refuses a root mount
        that is not the directory held, after the slot is taken.

        Raises:
            InputError: the call ID or the input is refused.
            RootError: a root's path holds a symbolic link, a non-directory, or
                a missing component; or a root in the cage is not the directory
                the runner checked.
            CredentialError: a granted credential is not stored or cannot be
                placed in the cage. For one not stored, the message is
                unset_message of the first env credential in grant order that
                is not stored, else of the first such file credential.
            CageStartError: the cage cannot be created, attached, or started,
                or the verifier cannot start the action.
            CageTimeoutError: the cage runs past its timeout.
            CageFailedError: the cage fails after it starts, or cannot be removed.
            OverflowWriteError: a stream passes the cap and its overflow file
                cannot be written.
        """
        with ExitStack() as held:
            setup = _set_up(call, self._store, held)
            with self._limiter.slot(call.max_cages):
                container = _create(self._client, call, setup)
                try:
                    return _run_container(container, call, setup)
                finally:
                    _remove(container, call.action.name)


@dataclass(frozen=True)
class _CageSetup:
    nonce: str
    roots: Sequence[RootIdentity]
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


def _set_up(call: CageCall, store: CredentialStore, held: ExitStack) -> _CageSetup:
    """Check the call, hold its roots on held, and return what its cage needs.

    Raises:
        InputError: the call ID or the input is refused.
        RootError: a root's path holds a symbolic link, a non-directory, or a
            missing component.
        CredentialError: a granted credential is not stored or cannot be placed.
            For one not stored, the message is unset_message of the first env
            credential in grant order that is not stored, else of the first
            such file credential.
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
    roots = hold_roots(call.grant.roots, held)
    credentials = call.grant.credentials
    return _CageSetup(
        nonce=secrets.token_hex(_NONCE_BYTES),
        roots=roots,
        entrypoint=entrypoint,
        stdin=stdin,
        environment={
            **roots_environment(call.grant.roots),
            **cage_environment(credentials, store),
            **call.grant.env,
        },
        mounts=cage_mounts(call.grant.roots, credentials, store),
    )


def _create(client: DockerClient, call: CageCall, setup: _CageSetup) -> Container:
    """Create the call's cage behind the verifier, which also clears the command.

    Raises:
        CageStartError: Docker refuses the create, an absent action or verifier
            image included.
    """
    try:
        return client.containers.create(
            call.image,
            entrypoint=_verified_entrypoint(setup),
            stdin_open=setup.stdin is not None,
            user=CONTAINER_USER,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges", f"seccomp={cage_profile_json()}"],
            tmpfs={TMPFS_PATH: TMPFS_OPTIONS},
            mounts=[
                *setup.mounts,
                # docker-py's Mount refuses the image type.
                {
                    "Type": "image",
                    "Source": VERIFIER_IMAGE,
                    "Target": VERIFIER_MOUNT,
                    "ReadOnly": True,
                },
            ],
            environment={**call.network.environment, **setup.environment},
            network=call.network.name,
            labels=dict(CONTAINER_LABELS),
            log_config=docker.types.LogConfig(type=LOG_DRIVER, config={}),
        )
    except DOCKER_FAILURES as exc:
        raise CageStartError(
            f"image {call.image!r} with verifier image {VERIFIER_IMAGE!r}: "
            f"the cage could not be created: {exc}"
        ) from exc


def _verified_entrypoint(setup: _CageSetup) -> list[str]:
    root_arguments = [
        text
        for root in setup.roots
        for text in (root.path, str(root.device), str(root.inode))
    ]
    return [VERIFIER_PATH, setup.nonce, *root_arguments, "--", *setup.entrypoint]


def _run_container(container: Container, call: CageCall, setup: _CageSetup) -> Outcome:
    with _attach_stdin(container, setup.stdin, call.image) as feed:
        deadline = _start(container, call)
        if feed is not None:
            feed.send(deadline)
        exit_code = _wait(container, deadline)
        duration_ms = deadline.elapsed_ms()
    if exit_code == VERIFIER_REFUSED_EXIT:
        _raise_verifier_refusal(container, call, setup.nonce)
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


def _raise_verifier_refusal(container: Container, call: CageCall, nonce: str) -> None:
    """Raise the refusal the verifier wrote under nonce, when stderr leads with one.

    Raises:
        RootError: a root in the cage is not the directory the runner checked.
        CageStartError: the verifier could not start the action.
        CageFailedError: the cage's stderr cannot be read.
    """
    line = _first_stderr_line(container, call.action.name)
    root_prefix = f"{nonce} root ".encode()
    exec_prefix = f"{nonce} exec ".encode()
    if line.startswith(root_prefix):
        reason = line.removeprefix(root_prefix).decode(errors="replace")
        raise RootError(
            f"action {call.action.name!r}: a root in the cage is not the directory "
            f"the runner checked: {reason}"
        )
    if line.startswith(exec_prefix):
        reason = line.removeprefix(exec_prefix).decode(errors="replace")
        raise CageStartError(
            f"image {call.image!r}: the verifier could not start the action: {reason}"
        )


def _first_stderr_line(container: Container, action_name: str) -> bytes:
    """Return the cage's stderr up to its first newline, at most 4096 bytes of it.

    Raises:
        CageFailedError: the logs call fails.
    """
    head = b""
    try:
        for chunk in container.logs(stdout=False, stderr=True, stream=True):
            head += chunk
            if b"\n" in head or len(head) >= _VERIFIER_LINE_BYTES:
                break
    except DOCKER_FAILURES as exc:
        raise CageFailedError(
            f"action {action_name!r}: the cage's stderr could not be read: {exc}"
        ) from exc
    return head[:_VERIFIER_LINE_BYTES].split(b"\n", 1)[0]


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
