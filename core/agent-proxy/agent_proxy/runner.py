"""The cage runner: one action call in one short-lived, hardened container."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import docker.types
from jsonschema import Draft202012Validator

from agent_proxy.config import EnvMount, FileMount, Root
from agent_proxy.credentials import CredentialStore, UnknownCredentialError
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import CommandAction, ScriptAction


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
    stored_bytes = _stored_path(name, store).read_bytes()
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
    # The SDK serialises a mount with json.dumps, which refuses a Path.
    credential_mounts = [
        docker.types.Mount(
            target=mount.file,
            source=str(_stored_path(name, store)),
            type="bind",
            read_only=True,
        )
        for name, mount in credentials.items()
        if isinstance(mount, FileMount)
    ]
    return root_mounts + credential_mounts


def _stored_path(name: str, store: CredentialStore) -> Path:
    try:
        return store.path(name)
    except UnknownCredentialError as error:
        raise CredentialError(str(error)) from error
