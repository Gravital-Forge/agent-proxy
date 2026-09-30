"""Plugin manifests: the actions a plugin declares, checked and typed."""

import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Literal, NoReturn, NotRequired, TypedDict, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_proxy.config import (
    _CREDENTIALS_DIR,
    ACTION_NAME_PATTERN,
    ENV_NAME_PATTERN,
    DurationError,
    EnvMount,
    FileMount,
    _is_clean_absolute,
    parse_duration,
)
from agent_proxy.errors import ProxyError

FLAG_PATTERN = r"^--?[A-Za-z0-9][A-Za-z0-9-]*$"
_ARGS_PROPERTY: Mapping[str, object] = {"type": "array", "items": {"type": "string"}}
_PROPERTY_TYPES: Mapping[str, list[str]] = {
    "flag": ["boolean", "string", "integer"],
    "positional": ["string", "integer"],
}

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

    def warnings(self) -> tuple[str, ...]:
        """Return one message per action that declares pass-through args."""
        return tuple(
            f"actions.{name}: declares pass-through args"
            for name, action in self.actions.items()
            if isinstance(action, CommandAction) and action.passthrough_args
        )


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
            repeats a key in one object, holds a number that is not finite,
            breaks the manifest schema, or breaks a manifest rule the schema
            cannot state. It holds one message per failure.
    """
    document = _decode(data)
    failures = _schema_failures(document)
    if failures:
        raise ManifestError(*failures)
    manifest = cast(_ManifestDoc, document)
    failures = list(_rule_failures(manifest))
    if failures:
        raise ManifestError(*failures)
    return _to_manifest(manifest)


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


def _rule_failures(document: _ManifestDoc) -> Iterator[str]:
    """Yield one message per manifest rule the schema cannot state that is broken."""
    for name, action in document["actions"].items():
        if re.fullmatch(ACTION_NAME_PATTERN, name) is None:
            yield (
                f"$.actions: action name must match {ACTION_NAME_PATTERN!r}, "
                f"got {name!r}"
            )
            # Every other message about this action would carry the refused
            # name into its path.
            continue
        yield from _action_failures(f"$.actions.{name}", action)


def _action_failures(where: str, action: _CommandDoc | _ScriptDoc) -> Iterator[str]:
    yield from _credential_failures(where, action["credentials"])
    try:
        parse_duration(action["timeout"])
    except DurationError as exc:
        yield f"{where}.timeout: {exc}"
    input_schema_failures = list(
        _embedded_schema_failures(f"{where}.input_schema", action["input_schema"])
    )
    yield from input_schema_failures
    if action["kind"] == "script":
        yield from _embedded_schema_failures(
            f"{where}.output_schema", action["output_schema"]
        )
    elif not input_schema_failures:
        # The command rules read the input schema's properties, which only a
        # valid schema is sure to hold as a mapping of schemas.
        yield from _command_failures(where, action)


def _credential_failures(
    where: str, credentials: list[_CredentialDoc]
) -> Iterator[str]:
    for index, credential in enumerate(credentials):
        at = f"{where}.credentials[{index}]"
        if "env" in credential:
            env = credential["env"]
            if re.fullmatch(ENV_NAME_PATTERN, env) is None:
                yield f"{at}.env: env must match {ENV_NAME_PATTERN!r}, got {env!r}"
            continue
        file = credential["file"]
        path = PurePosixPath(file)
        if not _is_clean_absolute(path) or _CREDENTIALS_DIR not in path.parents:
            yield (
                f"{at}.file: file must be an absolute path beneath "
                f"'/run/credentials/' with no '..' segment, got {file!r}"
            )


def _embedded_schema_failures(
    where: str, schema: Mapping[str, object]
) -> Iterator[str]:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        yield f"{where}{exc.json_path.removeprefix('$')}: {exc.message}"


def _command_failures(where: str, action: _CommandDoc) -> Iterator[str]:
    input_schema = action["input_schema"]
    additional_properties = input_schema.get("additionalProperties")
    if additional_properties is not False:
        yield (
            f"{where}.input_schema.additionalProperties: a command action's "
            "input_schema must set additionalProperties to false, "
            f"got {additional_properties!r}"
        )
    properties = cast(Mapping[str, object], input_schema.get("properties", {}))
    yield from _parameter_failures(where, action["parameters"], properties)
    names = {parameter["name"] for parameter in action["parameters"]}
    yield from _unmapped_property_failures(where, properties, names)
    yield from _passthrough_failures(where, action["passthrough_args"], properties)


def _parameter_failures(
    where: str, parameters: list[_ParameterDoc], properties: Mapping[str, object]
) -> Iterator[str]:
    seen: set[str] = set()
    for index, parameter in enumerate(parameters):
        at = f"{where}.parameters[{index}]"
        name = parameter["name"]
        if name == "args":
            yield f"{at}.name: parameter name must not be 'args', got {name!r}"
        if name in seen:
            yield (
                f"{at}.name: parameter name must not repeat an earlier "
                f"parameter's, got {name!r}"
            )
        seen.add(name)
        flag = parameter.get("flag")
        if flag is not None and re.fullmatch(FLAG_PATTERN, flag) is None:
            yield f"{at}.flag: flag must match {FLAG_PATTERN!r}, got {flag!r}"
        if name not in properties:
            yield (
                f"{at}.name: parameter name must be a property of input_schema, "
                f"got {name!r}"
            )
            continue
        placement = parameter["as"]
        allowed = _PROPERTY_TYPES[placement]
        schema = properties[name]
        if not (isinstance(schema, dict) and schema.get("type") in allowed):
            yield (
                f"{at}: a {placement} parameter's property must set type to one "
                f"of {allowed!r}, got {schema!r}"
            )


def _unmapped_property_failures(
    where: str, properties: Mapping[str, object], parameter_names: set[str]
) -> Iterator[str]:
    # An args property is the pass-through rule's to judge.
    for name in properties:
        if name != "args" and name not in parameter_names:
            yield (
                f"{where}.input_schema.properties: each property other than "
                f"'args' must be a parameter, got {name!r}"
            )


def _passthrough_failures(
    where: str, passthrough_args: bool, properties: Mapping[str, object]
) -> Iterator[str]:
    if passthrough_args and "args" not in properties:
        yield (
            f"{where}.passthrough_args: passthrough_args must be false when "
            "input_schema has no 'args' property, got True"
        )
    if not passthrough_args and "args" in properties:
        yield (
            f"{where}.passthrough_args: passthrough_args must be true when "
            "input_schema has an 'args' property, got False"
        )
    if "args" in properties and properties["args"] != _ARGS_PROPERTY:
        yield (
            f"{where}.input_schema.properties.args: the 'args' property must be "
            f"{_ARGS_PROPERTY!r}, got {properties['args']!r}"
        )


def _to_manifest(document: _ManifestDoc) -> Manifest:
    """Return the typed manifest a document that passes every manifest rule declares."""
    return Manifest(
        actions={
            name: _to_action(name, action)
            for name, action in document["actions"].items()
        }
    )


def _to_action(
    name: str, document: _CommandDoc | _ScriptDoc
) -> CommandAction | ScriptAction:
    credentials = tuple(_to_credential(item) for item in document["credentials"])
    timeout = parse_duration(document["timeout"])
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
