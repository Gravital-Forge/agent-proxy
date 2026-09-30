"""Plugin manifests: the actions a plugin declares, checked and typed."""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from importlib.resources import files
from typing import Literal, NoReturn, NotRequired, TypedDict, cast

from jsonschema import Draft202012Validator

from agent_proxy.config import (
    DurationError,
    EnvMount,
    FileMount,
    parse_duration,
)
from agent_proxy.errors import ProxyError

_VALIDATOR = Draft202012Validator(
    json.loads(
        files("agent_proxy.schemas")
        .joinpath("manifest.schema.json")
        .read_text(encoding="utf-8")
    )
)


class ManifestError(ProxyError):
    """A manifest is not a UTF-8 JSON object or breaks the manifest format."""

    def __init__(self, *errors: str) -> None:
        """Hold one message per failure; str() prints them one per line."""
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class Parameter:
    """One input property and how it reaches argv."""

    name: str
    placement: Literal["flag", "positional"]
    flag: str | None


@dataclass(frozen=True)
class CommandAction:
    """An action that runs a fixed argv prefix extended from its input."""

    name: str
    input_schema: Mapping[str, object]
    credentials: tuple[EnvMount | FileMount, ...]
    timeout: timedelta
    argv: tuple[str, ...]
    parameters: tuple[Parameter, ...]
    passthrough_args: bool


@dataclass(frozen=True)
class ScriptAction:
    """An action that runs an executable in the image and returns structured output."""

    name: str
    input_schema: Mapping[str, object]
    credentials: tuple[EnvMount | FileMount, ...]
    timeout: timedelta
    executable: str
    output_schema: Mapping[str, object]


@dataclass(frozen=True)
class Manifest:
    """The actions a plugin declares, by action name."""

    actions: Mapping[str, CommandAction | ScriptAction]


class _CredentialDoc(TypedDict, total=False):
    env: str
    file: str


_ParameterDoc = TypedDict(
    "_ParameterDoc",
    {"name": str, "as": Literal["flag", "positional"], "flag": NotRequired[str]},
)


class _CommandDoc(TypedDict):
    kind: Literal["command"]
    input_schema: dict[str, object]
    credentials: list[_CredentialDoc]
    timeout: str
    argv: list[str]
    parameters: list[_ParameterDoc]
    passthrough_args: bool


class _ScriptDoc(TypedDict):
    kind: Literal["script"]
    input_schema: dict[str, object]
    credentials: list[_CredentialDoc]
    timeout: str
    executable: str
    output_schema: dict[str, object]


class _ManifestDoc(TypedDict):
    actions: dict[str, _CommandDoc | _ScriptDoc]


def parse_manifest(data: bytes) -> Manifest:
    """Return the manifest the JSON document in data declares.

    Raises:
        ManifestError: data is not UTF-8 JSON with an object at the top level,
            repeats a key in one object, holds a number that is not finite, or
            breaks the manifest schema. It holds one message per failure.
    """
    document = _decode(data)
    failures = _schema_failures(document)
    if failures:
        raise ManifestError(*failures)
    return _to_manifest(cast(_ManifestDoc, document))


def _decode(data: bytes) -> dict[str, object]:
    """Return the JSON object data holds.

    Raises:
        ManifestError: data is not UTF-8, is not JSON, repeats a key in one
            object, holds a number that is not finite, or holds something other
            than an object at the top level.
    """
    # json.loads given bytes would also accept UTF-16 and UTF-32.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"manifest must be UTF-8: {exc}") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_refuse_non_finite,
        )
    # int() raises a plain ValueError past the interpreter's digit limit, and
    # deep nesting exhausts the decoder's recursion limit.
    except (ValueError, RecursionError) as exc:
        raise ManifestError(f"manifest must be valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError(
            "manifest must be a JSON object at the top level, "
            f"got {type(document).__name__}"
        )
    return document


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    members: dict[str, object] = {}
    for key, value in pairs:
        if key in members:
            raise ManifestError(
                f"manifest must not repeat a key in one object, got {key!r} twice"
            )
        members[key] = value
    return members


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        _refuse_non_finite(text)
    return number


def _refuse_non_finite(text: str) -> NoReturn:
    raise ManifestError(f"manifest must hold only finite numbers, got {text!r}")


def _schema_failures(document: dict[str, object]) -> list[str]:
    """Return one message per schema violation, sorted by path then message."""
    located = sorted(
        {(error.json_path, error.message) for error in _VALIDATOR.iter_errors(document)}
    )
    return [f"{path}: {message}" for path, message in located]


def _to_manifest(document: _ManifestDoc) -> Manifest:
    """Return the typed manifest a schema-valid document declares.

    Raises:
        ManifestError: parse_duration refuses one or more timeouts.
    """
    timeouts = _timeouts(document["actions"])
    return Manifest(
        actions={
            name: _to_action(name, action, timeouts[name])
            for name, action in document["actions"].items()
        }
    )


def _timeouts(actions: Mapping[str, _CommandDoc | _ScriptDoc]) -> dict[str, timedelta]:
    """Return each action's timeout, by action name.

    Raises:
        ManifestError: parse_duration refuses one or more timeouts; it holds one
            message per refused timeout.
    """
    timeouts: dict[str, timedelta] = {}
    failures: list[str] = []
    for name, action in actions.items():
        try:
            timeouts[name] = parse_duration(action["timeout"])
        except DurationError as exc:
            failures.append(f"$.actions.{name}.timeout: {exc}")
    if failures:
        raise ManifestError(*failures)
    return timeouts


def _to_action(
    name: str, document: _CommandDoc | _ScriptDoc, timeout: timedelta
) -> CommandAction | ScriptAction:
    credentials = tuple(_to_credential(item) for item in document["credentials"])
    if document["kind"] == "command":
        return CommandAction(
            name=name,
            input_schema=document["input_schema"],
            credentials=credentials,
            timeout=timeout,
            argv=tuple(document["argv"]),
            parameters=tuple(_to_parameter(item) for item in document["parameters"]),
            passthrough_args=document["passthrough_args"],
        )
    return ScriptAction(
        name=name,
        input_schema=document["input_schema"],
        credentials=credentials,
        timeout=timeout,
        executable=document["executable"],
        output_schema=document["output_schema"],
    )


def _to_credential(document: _CredentialDoc) -> EnvMount | FileMount:
    if "env" in document:
        return EnvMount(env=document["env"])
    return FileMount(file=document["file"])


def _to_parameter(document: _ParameterDoc) -> Parameter:
    return Parameter(
        name=document["name"],
        placement=document["as"],
        flag=document.get("flag"),
    )
