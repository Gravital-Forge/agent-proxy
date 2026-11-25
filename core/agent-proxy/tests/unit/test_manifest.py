import json
from dataclasses import FrozenInstanceError
from datetime import timedelta
from http.client import HTTPConnection
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator
from referencing.exceptions import Unresolvable
from schema_server import serving_schema

from agent_proxy.config import (
    ACTION_NAME_PATTERN,
    DURATION_PATTERN,
    ENV_NAME_PATTERN,
    EnvMount,
    FileMount,
)
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import (
    FLAG_PATTERN,
    CommandAction,
    Manifest,
    ManifestError,
    Parameter,
    ProviderAction,
    ScriptAction,
    parse_manifest,
    schema_validator,
)

OMIT = object()
ECHO = "$.actions.echo"
REPORT = "$.actions.report"
ISSUE = "$.actions.issue"
ECHO_PROPERTIES = {"loud": {"type": "boolean"}, "message": {"type": "string"}}
ECHO_INPUT_SCHEMA = {
    "type": "object",
    "properties": ECHO_PROPERTIES,
    "additionalProperties": False,
}
ARGS_PROPERTY = {"type": "array", "items": {"type": "string"}}
INTEGER_ARGS_PROPERTY = {"type": "array", "items": {"type": "integer"}}
LOUD = {"name": "loud", "as": "flag", "flag": "--loud"}
MESSAGE = {"name": "message", "as": "positional"}
FILE_RULE = "must be an absolute path beneath '/run/credentials/' with no '..' segment"
REPORT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "number", "maximum": 1.5}},
}
COMMAND_MEMBERS = (
    "input_schema",
    "credentials",
    "timeout",
    "argv",
    "parameters",
    "passthrough_args",
)
SCRIPT_MEMBERS = (
    "input_schema",
    "credentials",
    "timeout",
    "executable",
    "output_schema",
)
PROVIDER_MEMBERS = ("credentials", "timeout", "executable")


def _command(**overrides: object) -> dict[str, object]:
    """Return a valid command action with overrides applied; OMIT drops a member."""
    members: dict[str, object] = {
        "kind": "command",
        "input_schema": ECHO_INPUT_SCHEMA,
        "credentials": [{"env": "GH_TOKEN"}, {"file": "/run/credentials/ssh-key"}],
        "timeout": "30s",
        "argv": ["echo"],
        "parameters": [LOUD, MESSAGE],
        "passthrough_args": False,
    } | overrides
    return {key: value for key, value in members.items() if value is not OMIT}


def _script(**overrides: object) -> dict[str, object]:
    """Return a valid script action with overrides applied; OMIT drops a member."""
    members: dict[str, object] = {
        "kind": "script",
        "input_schema": {"type": "object"},
        "credentials": [],
        "timeout": "5m",
        "executable": "/usr/local/bin/report",
        "output_schema": REPORT_OUTPUT_SCHEMA,
    } | overrides
    return {key: value for key, value in members.items() if value is not OMIT}


def _provider(**overrides: object) -> dict[str, object]:
    """Return a valid provider action with overrides applied; OMIT drops a member."""
    members: dict[str, object] = {
        "kind": "provider",
        "credentials": [{"file": "/run/credentials/app-key"}],
        "timeout": "30s",
        "executable": "/plugin/bin/issue",
    } | overrides
    return {key: value for key, value in members.items() if value is not OMIT}


def _echo_input(**properties: object) -> dict[str, object]:
    """Return echo's input schema with properties added or replaced."""
    return ECHO_INPUT_SCHEMA | {"properties": ECHO_PROPERTIES | properties}


def _passthrough_command(**overrides: object) -> dict[str, object]:
    """Return a valid command action that declares pass-through args."""
    passthrough: dict[str, object] = {
        "input_schema": _echo_input(args=ARGS_PROPERTY),
        "passthrough_args": True,
    }
    return _command(**(passthrough | overrides))


def _echo(**overrides: object) -> dict[str, object]:
    """Return a manifest whose one action, echo, is a command action."""
    return {"actions": {"echo": _command(**overrides)}}


def _report(**overrides: object) -> dict[str, object]:
    """Return a manifest whose one action, report, is a script action."""
    return {"actions": {"report": _script(**overrides)}}


def _issue(**overrides: object) -> dict[str, object]:
    """Return a manifest whose one action, issue, is a provider action."""
    return {"actions": {"issue": _provider(**overrides)}}


def _encode(document: object) -> bytes:
    return json.dumps(document).encode()


def _refusal(data: bytes) -> ManifestError:
    """Return the ManifestError parse_manifest raises for data."""
    with pytest.raises(ManifestError) as excinfo:
        parse_manifest(data)
    return excinfo.value


def _schema() -> object:
    source = files("agent_proxy.schemas").joinpath("manifest.schema.json")
    return json.loads(source.read_text(encoding="utf-8"))


def test_schema_is_a_valid_draft_2020_12_schema() -> None:
    """The shipped schema passes the draft 2020-12 metaschema."""
    Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize(
    ("path", "constant"),
    [
        (("properties", "actions", "propertyNames", "pattern"), ACTION_NAME_PATTERN),
        (("$defs", "timeout", "pattern"), DURATION_PATTERN),
        (("$defs", "credential", "properties", "env", "pattern"), ENV_NAME_PATTERN),
        (("$defs", "parameter", "properties", "flag", "pattern"), FLAG_PATTERN),
    ],
)
def test_schema_repeats_each_shared_pattern_exactly(
    path: tuple[str, ...], constant: str
) -> None:
    """Each pattern the schema shares with the Python code equals its constant."""
    node = _schema()
    for key in path:
        assert isinstance(node, dict)
        node = node[key]

    assert node == constant


def test_manifest_error_joins_its_messages_one_per_line() -> None:
    """A ManifestError holds each message and prints them one per line."""
    error = ManifestError("first failure", "second failure")

    assert isinstance(error, ProxyError)
    assert error.errors == ("first failure", "second failure")
    assert str(error) == "first failure\nsecond failure"


def test_parse_manifest_returns_each_action_as_a_typed_object() -> None:
    """A valid manifest with one command and one script action parses to both."""
    data = _encode({"actions": {"echo": _command(), "report": _script()}})

    manifest = parse_manifest(data)

    assert manifest == Manifest(
        actions={
            "echo": CommandAction(
                name="echo",
                input_schema=ECHO_INPUT_SCHEMA,
                credentials=(
                    EnvMount(env="GH_TOKEN"),
                    FileMount(file="/run/credentials/ssh-key"),
                ),
                timeout=timedelta(seconds=30),
                argv=("echo",),
                parameters=(
                    Parameter(name="loud", placement="flag", flag="--loud"),
                    Parameter(name="message", placement="positional", flag=None),
                ),
                passthrough_args=False,
            ),
            "report": ScriptAction(
                name="report",
                input_schema={"type": "object"},
                credentials=(),
                timeout=timedelta(minutes=5),
                executable="/usr/local/bin/report",
                output_schema=REPORT_OUTPUT_SCHEMA,
            ),
        }
    )


def test_parse_manifest_returns_a_provider_action_as_a_typed_object() -> None:
    """A provider action parses to its name, credentials, timeout, and executable."""
    manifest = parse_manifest(_encode(_issue(credentials=[{"env": "VAULT_TOKEN"}])))

    assert manifest == Manifest(
        actions={
            "issue": ProviderAction(
                name="issue",
                credentials=(EnvMount(env="VAULT_TOKEN"),),
                timeout=timedelta(seconds=30),
                executable="/plugin/bin/issue",
            )
        }
    )


def test_manifest_tools_hold_every_action_but_the_providers() -> None:
    """The tools map each command and script action by name, and no provider."""
    document = {
        "actions": {"echo": _command(), "issue": _provider(), "report": _script()}
    }

    manifest = parse_manifest(_encode(document))

    assert dict(manifest.tools) == {
        "echo": manifest.actions["echo"],
        "report": manifest.actions["report"],
    }


def test_parsed_manifest_is_frozen() -> None:
    """Neither the manifest nor an action it holds takes a new field value."""
    manifest = parse_manifest(_encode(_echo()))

    with pytest.raises(FrozenInstanceError):
        manifest.actions = {}
    with pytest.raises(FrozenInstanceError):
        manifest.actions["echo"].timeout = timedelta(seconds=1)


def test_parsed_manifest_actions_take_no_new_entry() -> None:
    """The actions mapping of a parsed manifest refuses an assignment."""
    manifest = parse_manifest(_encode(_echo()))

    with pytest.raises(TypeError):
        manifest.actions["other"] = manifest.actions["echo"]


SCHEMA_RULE_CASES = [
    pytest.param({}, "$: 'actions' is a required property", id="actions-required"),
    pytest.param(
        _echo() | {"version": 1},
        "$: Additional properties are not allowed ('version' was unexpected)",
        id="top-level-unknown-member",
    ),
    pytest.param(
        {"actions": []}, "$.actions: [] is not of type 'object'", id="actions-type"
    ),
    pytest.param(
        {"actions": {}}, "$.actions: {} should be non-empty", id="actions-empty"
    ),
    pytest.param(
        {"actions": {"Echo": _command()}},
        f"$.actions: 'Echo' does not match {ACTION_NAME_PATTERN!r}",
        id="action-name-pattern",
    ),
    pytest.param(
        {"actions": {"echo": 5}}, f"{ECHO}: 5 is not of type 'object'", id="action-type"
    ),
    pytest.param(
        _echo(kind=OMIT), f"{ECHO}: 'kind' is a required property", id="kind-required"
    ),
    pytest.param(
        _echo(kind="shell"),
        f"{ECHO}.kind: 'shell' is not one of ['command', 'script', 'provider']",
        id="kind-value",
    ),
    *(
        pytest.param(
            _issue(**{member: OMIT}),
            f"{ISSUE}: {member!r} is a required property",
            id=f"provider-{member}-required",
        )
        for member in PROVIDER_MEMBERS
    ),
    *(
        pytest.param(
            _issue(**{member: value}),
            f"{ISSUE}: Additional properties are not allowed"
            f" ({member!r} was unexpected)",
            id=f"provider-forbids-{member}",
        )
        for member, value in (
            ("input_schema", {"type": "object"}),
            ("output_schema", {"type": "object"}),
            ("argv", ["issue"]),
            ("retries", 3),
        )
    ),
    pytest.param(
        _issue(executable="issue"),
        f"{ISSUE}.executable: 'issue' does not match '^/'",
        id="provider-executable-absolute",
    ),
    pytest.param(
        _issue(credentials=[{"file": "/etc/app-key"}]),
        f"{ISSUE}.credentials[0].file: '/etc/app-key' does not match"
        " '^/run/credentials/'",
        id="provider-credential-file-pattern",
    ),
    pytest.param(
        _issue(timeout="30"),
        f"{ISSUE}.timeout: '30' does not match {DURATION_PATTERN!r}",
        id="provider-timeout-pattern",
    ),
    *(
        pytest.param(
            _echo(**{member: OMIT}),
            f"{ECHO}: {member!r} is a required property",
            id=f"command-{member}-required",
        )
        for member in COMMAND_MEMBERS
    ),
    *(
        pytest.param(
            _report(**{member: OMIT}),
            f"{REPORT}: {member!r} is a required property",
            id=f"script-{member}-required",
        )
        for member in SCRIPT_MEMBERS
    ),
    pytest.param(
        _echo(input_schema=[]),
        f"{ECHO}.input_schema: [] is not of type 'object'",
        id="input-schema-type",
    ),
    pytest.param(
        _echo(input_schema={}),
        f"{ECHO}.input_schema: 'type' is a required property",
        id="input-schema-type-required",
    ),
    pytest.param(
        _echo(input_schema={"type": "array"}),
        f"{ECHO}.input_schema.type: 'object' was expected",
        id="input-schema-type-object",
    ),
    pytest.param(
        _report(output_schema=[]),
        f"{REPORT}.output_schema: [] is not of type 'object'",
        id="output-schema-type",
    ),
    pytest.param(
        _report(output_schema={}),
        f"{REPORT}.output_schema: 'type' is a required property",
        id="output-schema-type-required",
    ),
    pytest.param(
        _report(output_schema={"type": "string"}),
        f"{REPORT}.output_schema.type: 'object' was expected",
        id="output-schema-type-object",
    ),
    pytest.param(
        _echo(credentials={}),
        ECHO + ".credentials: {} is not of type 'array'",
        id="credentials-type",
    ),
    pytest.param(
        _echo(credentials=["GH_TOKEN"]),
        f"{ECHO}.credentials[0]: 'GH_TOKEN' is not of type 'object'",
        id="credential-type",
    ),
    pytest.param(
        _echo(credentials=[{}]),
        ECHO + ".credentials[0]: {} should be non-empty",
        id="credential-empty",
    ),
    pytest.param(
        _echo(credentials=[{"env": "GH_TOKEN", "file": "/run/credentials/k"}]),
        ECHO + ".credentials[0]: {'env': 'GH_TOKEN', 'file': '/run/credentials/k'}"
        " has too many properties",
        id="credential-two-forms",
    ),
    pytest.param(
        _echo(credentials=[{"path": "/run/credentials/k"}]),
        f"{ECHO}.credentials[0]: Additional properties are not allowed"
        " ('path' was unexpected)",
        id="credential-unknown-member",
    ),
    pytest.param(
        _echo(credentials=[{"env": "gh_token"}]),
        f"{ECHO}.credentials[0].env: 'gh_token' does not match {ENV_NAME_PATTERN!r}",
        id="credential-env-pattern",
    ),
    pytest.param(
        _echo(credentials=[{"env": 7}]),
        f"{ECHO}.credentials[0].env: 7 is not of type 'string'",
        id="credential-env-type",
    ),
    pytest.param(
        _echo(credentials=[{"file": "/etc/passwd"}]),
        f"{ECHO}.credentials[0].file: '/etc/passwd' does not match"
        " '^/run/credentials/'",
        id="credential-file-pattern",
    ),
    pytest.param(
        _echo(timeout="30"),
        f"{ECHO}.timeout: '30' does not match {DURATION_PATTERN!r}",
        id="timeout-pattern",
    ),
    pytest.param(
        _echo(timeout=30),
        f"{ECHO}.timeout: 30 is not of type 'string'",
        id="timeout-type",
    ),
    pytest.param(
        _echo(retries=3),
        f"{ECHO}: Additional properties are not allowed ('retries' was unexpected)",
        id="command-unknown-member",
    ),
    pytest.param(
        _report(retries=3),
        f"{REPORT}: Additional properties are not allowed ('retries' was unexpected)",
        id="script-unknown-member",
    ),
    pytest.param(
        _echo(argv="echo"),
        f"{ECHO}.argv: 'echo' is not of type 'array'",
        id="argv-type",
    ),
    pytest.param(
        _echo(argv=[]), f"{ECHO}.argv: [] should be non-empty", id="argv-empty"
    ),
    pytest.param(
        _echo(argv=[""]),
        f"{ECHO}.argv[0]: '' should be non-empty",
        id="argv-item-empty",
    ),
    pytest.param(
        _echo(argv=[1]),
        f"{ECHO}.argv[0]: 1 is not of type 'string'",
        id="argv-item-type",
    ),
    pytest.param(
        _echo(parameters={}),
        ECHO + ".parameters: {} is not of type 'array'",
        id="parameters-type",
    ),
    pytest.param(
        _echo(parameters=["message"]),
        f"{ECHO}.parameters[0]: 'message' is not of type 'object'",
        id="parameter-type",
    ),
    pytest.param(
        _echo(parameters=[{"as": "positional"}]),
        f"{ECHO}.parameters[0]: 'name' is a required property",
        id="parameter-name-required",
    ),
    pytest.param(
        _echo(parameters=[{"name": 1, "as": "positional"}]),
        f"{ECHO}.parameters[0].name: 1 is not of type 'string'",
        id="parameter-name-type",
    ),
    pytest.param(
        _echo(parameters=[{"name": "message"}]),
        f"{ECHO}.parameters[0]: 'as' is a required property",
        id="parameter-as-required",
    ),
    pytest.param(
        _echo(parameters=[{"name": "message", "as": "option"}]),
        f"{ECHO}.parameters[0].as: 'option' is not one of ['flag', 'positional']",
        id="parameter-as-value",
    ),
    pytest.param(
        _echo(parameters=[{"name": "loud", "as": "flag"}]),
        f"{ECHO}.parameters[0]: 'flag' is a required property",
        id="flag-parameter-flag-required",
    ),
    pytest.param(
        _echo(parameters=[{"name": "loud", "as": "flag", "flag": "loud"}]),
        f"{ECHO}.parameters[0].flag: 'loud' does not match"
        " '^--?[A-Za-z0-9][A-Za-z0-9-]*$'",
        id="flag-parameter-flag-pattern",
    ),
    pytest.param(
        _echo(parameters=[{"name": "message", "as": "positional", "flag": "--m"}]),
        ECHO + ".parameters[0]: {'name': 'message', 'as': 'positional',"
        " 'flag': '--m'} should not be valid under {'required': ['flag']}",
        id="positional-parameter-flag-forbidden",
    ),
    pytest.param(
        _echo(parameters=[{"name": "message", "as": "positional", "default": 1}]),
        f"{ECHO}.parameters[0]: Additional properties are not allowed"
        " ('default' was unexpected)",
        id="parameter-unknown-member",
    ),
    pytest.param(
        _echo(passthrough_args="yes"),
        f"{ECHO}.passthrough_args: 'yes' is not of type 'boolean'",
        id="passthrough-args-type",
    ),
    pytest.param(
        _report(executable="report"),
        f"{REPORT}.executable: 'report' does not match '^/'",
        id="executable-absolute",
    ),
    pytest.param(
        _report(executable=5),
        f"{REPORT}.executable: 5 is not of type 'string'",
        id="executable-type",
    ),
    *(
        pytest.param(
            _echo(**{member: value}),
            f"{ECHO}: Additional properties are not allowed"
            f" ({member!r} was unexpected)",
            id=f"command-forbids-{member}",
        )
        for member, value in (
            ("executable", "/usr/local/bin/report"),
            ("output_schema", {"type": "object"}),
        )
    ),
    *(
        pytest.param(
            _report(**{member: value}),
            f"{REPORT}: Additional properties are not allowed"
            f" ({member!r} was unexpected)",
            id=f"script-forbids-{member}",
        )
        for member, value in (
            ("argv", ["report"]),
            ("parameters", []),
            ("passthrough_args", False),
        )
    ),
]


@pytest.mark.parametrize(("document", "expected"), SCHEMA_RULE_CASES)
def test_parse_manifest_refuses_each_schema_rule(
    document: object, expected: str
) -> None:
    """A manifest breaking one schema rule is refused with that rule's one message."""
    assert _refusal(_encode(document)).errors == (expected,)


def test_parse_manifest_reports_schema_failures_by_path_then_message() -> None:
    """A member's own failures come before its children's, each set in order."""
    document = _echo(credentials=OMIT, timeout=OMIT, argv=[])

    assert _refusal(_encode(document)).errors == (
        f"{ECHO}: 'credentials' is a required property",
        f"{ECHO}: 'timeout' is a required property",
        f"{ECHO}.argv: [] should be non-empty",
    )


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [
        pytest.param(
            "30s\n",
            f"{ECHO}.timeout: duration must match {DURATION_PATTERN!r}, got '30s\\n'",
            id="trailing-newline",
        ),
        pytest.param(
            "1000000000d",
            f"{ECHO}.timeout: duration must fit in a timedelta, got '1000000000d'",
            id="too-large",
        ),
    ],
)
def test_parse_manifest_refuses_a_timeout_parse_duration_refuses(
    timeout: str, expected: str
) -> None:
    """A timeout the schema passes but parse_duration refuses is a ManifestError."""
    assert _refusal(_encode(_echo(timeout=timeout))).errors == (expected,)


def test_parse_manifest_reports_every_refused_timeout() -> None:
    """Each action's refused timeout is its own message in one ManifestError."""
    document = {
        "actions": {
            "echo": _command(timeout="30s\n"),
            "report": _script(timeout="1000000000d"),
        }
    }

    assert _refusal(_encode(document)).errors == (
        f"{ECHO}.timeout: duration must match {DURATION_PATTERN!r}, got '30s\\n'",
        f"{REPORT}.timeout: duration must fit in a timedelta, got '1000000000d'",
    )


RULE_CASES = [
    pytest.param(
        {"actions": {"echo\n": _command()}},
        f"$.actions: action name must match {ACTION_NAME_PATTERN!r}, got 'echo\\n'",
        id="action-name-trailing-newline",
    ),
    pytest.param(
        _echo(credentials=[{"env": "GH_TOKEN\n"}]),
        f"{ECHO}.credentials[0].env: env must match {ENV_NAME_PATTERN!r},"
        " got 'GH_TOKEN\\n'",
        id="env-trailing-newline",
    ),
    pytest.param(
        _echo(parameters=[LOUD | {"flag": "--loud\n"}, MESSAGE]),
        f"{ECHO}.parameters[0].flag: flag must match {FLAG_PATTERN!r}, got '--loud\\n'",
        id="flag-trailing-newline",
    ),
    pytest.param(
        _echo(credentials=[{"file": "/run/credentials/../etc/passwd"}]),
        f"{ECHO}.credentials[0].file: file {FILE_RULE},"
        " got '/run/credentials/../etc/passwd'",
        id="file-parent-segment",
    ),
    pytest.param(
        _echo(credentials=[{"file": "/run/credentials/"}]),
        f"{ECHO}.credentials[0].file: file {FILE_RULE}, got '/run/credentials/'",
        id="file-credentials-directory",
    ),
    pytest.param(
        _issue(credentials=[{"env": "VAULT_TOKEN\n"}]),
        f"{ISSUE}.credentials[0].env: env must match {ENV_NAME_PATTERN!r},"
        " got 'VAULT_TOKEN\\n'",
        id="provider-env-trailing-newline",
    ),
    pytest.param(
        _issue(credentials=[{"file": "/run/credentials/../etc/passwd"}]),
        f"{ISSUE}.credentials[0].file: file {FILE_RULE},"
        " got '/run/credentials/../etc/passwd'",
        id="provider-file-parent-segment",
    ),
    pytest.param(
        _issue(timeout="30s\n"),
        f"{ISSUE}.timeout: duration must match {DURATION_PATTERN!r}, got '30s\\n'",
        id="provider-timeout-trailing-newline",
    ),
    pytest.param(
        _echo(input_schema=_echo_input(message={"type": "strin"})),
        f"{ECHO}.input_schema.properties.message.type:"
        " 'strin' is not valid under any of the given schemas",
        id="command-input-schema-invalid",
    ),
    pytest.param(
        _report(input_schema={"type": "object", "required": "score"}),
        f"{REPORT}.input_schema.required: 'score' is not of type 'array'",
        id="script-input-schema-invalid",
    ),
    pytest.param(
        _report(
            output_schema={"type": "object", "properties": {"score": {"type": "strin"}}}
        ),
        f"{REPORT}.output_schema.properties.score.type:"
        " 'strin' is not valid under any of the given schemas",
        id="output-schema-invalid",
    ),
    pytest.param(
        _echo(input_schema=ECHO_INPUT_SCHEMA | {"additionalProperties": True}),
        f"{ECHO}.input_schema.additionalProperties: a command action's input_schema"
        " must set additionalProperties to false, got True",
        id="additional-properties-true",
    ),
    pytest.param(
        _echo(input_schema={"type": "object", "properties": ECHO_PROPERTIES}),
        f"{ECHO}.input_schema.additionalProperties: a command action's input_schema"
        " must set additionalProperties to false, got None",
        id="additional-properties-unset",
    ),
    pytest.param(
        _echo(
            parameters=[
                LOUD,
                MESSAGE,
                {"name": "quiet", "as": "flag", "flag": "--quiet"},
            ]
        ),
        f"{ECHO}.parameters[2].name: parameter name must be a property of"
        " input_schema, got 'quiet'",
        id="parameter-not-a-property",
    ),
    pytest.param(
        _echo(parameters=[LOUD, MESSAGE, MESSAGE]),
        f"{ECHO}.parameters[2].name: parameter name must not repeat an earlier"
        " parameter's, got 'message'",
        id="parameter-repeated",
    ),
    pytest.param(
        _echo(
            input_schema=_echo_input(quiet={"type": "boolean"}),
            parameters=[
                LOUD,
                MESSAGE,
                {"name": "quiet", "as": "flag", "flag": "--loud"},
            ],
        ),
        f"{ECHO}.parameters[2].flag: flag must not repeat an earlier parameter's,"
        " got '--loud'",
        id="flag-repeated",
    ),
    pytest.param(
        _echo(parameters=[LOUD]),
        f"{ECHO}.input_schema.properties: each property other than 'args' must be"
        " a parameter, got 'message'",
        id="property-not-a-parameter",
    ),
    pytest.param(
        _echo(passthrough_args=True),
        f"{ECHO}.passthrough_args: passthrough_args must be false when"
        " input_schema has no 'args' property, got True",
        id="passthrough-without-args",
    ),
    pytest.param(
        _echo(input_schema=_echo_input(args=ARGS_PROPERTY)),
        f"{ECHO}.passthrough_args: passthrough_args must be true when"
        " input_schema has an 'args' property, got False",
        id="args-without-passthrough",
    ),
    pytest.param(
        {
            "actions": {
                "echo": _passthrough_command(
                    input_schema=_echo_input(args=INTEGER_ARGS_PROPERTY)
                )
            }
        },
        f"{ECHO}.input_schema.properties.args: the 'args' property must be"
        f" {ARGS_PROPERTY!r}, got {INTEGER_ARGS_PROPERTY!r}",
        id="args-property-shape",
    ),
    pytest.param(
        _echo(input_schema=_echo_input(loud={"type": "number"})),
        f"{ECHO}.parameters[0]: a flag parameter's property must set type to one"
        " of ['boolean', 'string', 'integer'], got {'type': 'number'}",
        id="flag-property-type",
    ),
    pytest.param(
        _echo(input_schema=_echo_input(message={"type": "boolean"})),
        f"{ECHO}.parameters[1]: a positional parameter's property must set type to"
        " one of ['string', 'integer'], got {'type': 'boolean'}",
        id="positional-property-type",
    ),
    pytest.param(
        _echo(input_schema=_echo_input(message=True)),
        f"{ECHO}.parameters[1]: a positional parameter's property must set type to"
        " one of ['string', 'integer'], got True",
        id="positional-property-boolean-schema",
    ),
]


@pytest.mark.parametrize(("document", "expected"), RULE_CASES)
def test_parse_manifest_refuses_each_rule_outside_the_schema(
    document: object, expected: str
) -> None:
    """A manifest breaking one rule outside the schema is refused with its message."""
    assert _refusal(_encode(document)).errors == (expected,)


def test_parse_manifest_refuses_a_parameter_named_args() -> None:
    """A parameter named args is refused, and its array property fails its type."""
    action = _passthrough_command(
        parameters=[LOUD, MESSAGE, {"name": "args", "as": "positional"}]
    )

    assert _refusal(_encode({"actions": {"echo": action}})).errors == (
        f"{ECHO}.parameters[2].name: parameter name must not be 'args', got 'args'",
        f"{ECHO}.parameters[2]: a positional parameter's property must set type to"
        f" one of ['string', 'integer'], got {ARGS_PROPERTY!r}",
    )


def _nested_schema(levels: int) -> dict[str, object]:
    """Return an object schema whose one property nests levels deep."""
    schema: dict[str, object] = {"type": "object"}
    for _ in range(levels):
        schema = {"type": "object", "properties": {"a": schema}}
    return schema


@pytest.mark.parametrize(
    ("document", "where"),
    [
        pytest.param(
            _report(input_schema=_nested_schema(200)),
            f"{REPORT}.input_schema",
            id="script-input",
        ),
        pytest.param(
            _report(output_schema=_nested_schema(200)),
            f"{REPORT}.output_schema",
            id="script-output",
        ),
        pytest.param(
            _echo(input_schema=_nested_schema(200)),
            f"{ECHO}.input_schema",
            id="command-input",
        ),
    ],
)
def test_parse_manifest_refuses_a_schema_nested_too_deep_to_check(
    document: object, where: str
) -> None:
    """An embedded schema too deep for the checker to recurse is refused at its path."""
    (message,) = _refusal(_encode(document)).errors

    assert message.startswith(f"{where}: schema must nest shallowly enough to check: ")


def test_parse_manifest_checks_nothing_else_in_an_action_whose_name_is_refused() -> (
    None
):
    """No message carries a refused action name into its path."""
    document = {"actions": {"echo\n": _command(timeout="30s\n")}}

    assert _refusal(_encode(document)).errors == (
        f"$.actions: action name must match {ACTION_NAME_PATTERN!r}, got 'echo\\n'",
    )


def test_parse_manifest_reports_every_broken_rule_in_action_order() -> None:
    """Rules broken in two actions are each reported in one ManifestError."""
    document = {
        "actions": {
            "echo": _command(credentials=[{"env": "GH_TOKEN\n"}]),
            "report": _script(input_schema={"type": "object", "required": "score"}),
        }
    }

    assert _refusal(_encode(document)).errors == (
        f"{ECHO}.credentials[0].env: env must match {ENV_NAME_PATTERN!r},"
        " got 'GH_TOKEN\\n'",
        f"{REPORT}.input_schema.required: 'score' is not of type 'array'",
    )


@pytest.mark.parametrize(
    ("name", "property_type"),
    [
        ("loud", "boolean"),
        ("loud", "string"),
        ("loud", "integer"),
        ("message", "string"),
        ("message", "integer"),
    ],
)
def test_parse_manifest_accepts_each_allowed_parameter_type(
    name: str, property_type: str
) -> None:
    """A flag takes a boolean, string, or integer; a positional a string or integer."""
    input_schema = _echo_input(**{name: {"type": property_type}})

    manifest = parse_manifest(_encode(_echo(input_schema=input_schema)))

    assert manifest.actions["echo"].input_schema == input_schema


def test_parse_manifest_accepts_pass_through_args() -> None:
    """A command action tied to an args property of strings parses with the flag set."""
    manifest = parse_manifest(_encode({"actions": {"echo": _passthrough_command()}}))

    action = manifest.actions["echo"]
    assert isinstance(action, CommandAction)
    assert action.passthrough_args is True
    assert action.input_schema == _echo_input(args=ARGS_PROPERTY)


def test_manifest_warns_once_per_action_declaring_pass_through_args() -> None:
    """warnings() holds one message per action whose passthrough_args is true."""
    document = {
        "actions": {
            "echo": _passthrough_command(),
            "report": _script(),
            "shout": _passthrough_command(),
            "whisper": _command(),
        }
    }

    assert parse_manifest(_encode(document)).warnings() == (
        "actions.echo: declares pass-through args",
        "actions.shout: declares pass-through args",
    )


def test_manifest_without_pass_through_args_has_no_warnings() -> None:
    """A manifest with no action declaring pass-through args warns of nothing."""
    document = {
        "actions": {"echo": _command(), "issue": _provider(), "report": _script()}
    }

    assert parse_manifest(_encode(document)).warnings() == ()


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff", id="invalid-byte"),
        pytest.param(json.dumps(_echo()).encode("utf-16"), id="utf-16"),
        pytest.param(json.dumps(_echo()).encode("utf-32"), id="utf-32"),
    ],
)
def test_parse_manifest_refuses_bytes_that_are_not_utf_8(data: bytes) -> None:
    """Only UTF-8 is read, so a manifest in UTF-16 or UTF-32 is refused too."""
    (message,) = _refusal(data).errors

    assert message.startswith("manifest must be UTF-8: ")


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"{", id="unclosed"),
        pytest.param(b'{"actions": }', id="missing-value"),
        pytest.param(b"\xef\xbb\xbf{}", id="byte-order-mark"),
        pytest.param(b'{"n": ' + b"1" * 5000 + b"}", id="integer-too-long"),
        pytest.param(b"[" * 100_000, id="nested-too-deep"),
    ],
)
def test_parse_manifest_refuses_text_that_is_not_json(data: bytes) -> None:
    """Text the JSON decoder cannot read is refused with the decoder's reason."""
    (message,) = _refusal(data).errors

    assert message.startswith("manifest must be valid JSON: ")


@pytest.mark.parametrize(
    ("data", "key"),
    [
        pytest.param(b'{"actions": {}, "actions": {}}', "actions", id="top-level"),
        pytest.param(
            b'{"actions": {"echo": {"kind": "command", "kind": "script"}}}',
            "kind",
            id="nested",
        ),
    ],
)
def test_parse_manifest_refuses_a_repeated_key(data: bytes, key: str) -> None:
    """A key repeated in any one object is refused, naming the key."""
    assert _refusal(data).errors == (
        f"manifest must not repeat a key in one object, got {key!r} twice",
    )


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_parse_manifest_refuses_a_number_that_is_not_finite(literal: str) -> None:
    """NaN, either infinity, or a float literal too large to be finite is refused."""
    data = b'{"actions": {"echo": {"input_schema": {"maximum": ' + literal.encode()
    data += b"}}}}"

    assert _refusal(data).errors == (
        f"manifest must hold only finite numbers, got {literal!r}",
    )


@pytest.mark.parametrize(
    ("data", "found"),
    [
        pytest.param(b"[]", "list", id="array"),
        pytest.param(b'"actions"', "str", id="string"),
    ],
)
def test_parse_manifest_refuses_a_top_level_that_is_not_an_object(
    data: bytes, found: str
) -> None:
    """A manifest is one JSON object; anything else is refused, naming its type."""
    assert _refusal(data).errors == (
        f"manifest must be a JSON object at the top level, got {found}",
    )


REMOTE_REF = "https://schemas.example.test/remote.json"


def _unresolved(at: str, reference: str) -> str:
    return f"{at}: reference must resolve inside the schema, got {reference!r}"


def _object_with(**properties: object) -> dict[str, object]:
    return {"type": "object", "properties": properties}


def _report_input(schema: dict[str, object]) -> dict[str, object]:
    """Return the report manifest with schema, made an object schema, as its input."""
    return _report(input_schema={"type": "object"} | schema)


UNRESOLVED_REFERENCE_CASES = [
    pytest.param(
        _report(input_schema=_object_with(a={"$ref": REMOTE_REF})),
        _unresolved(f"{REPORT}.input_schema.properties.a.$ref", REMOTE_REF),
        id="script-input-remote",
    ),
    pytest.param(
        _report(output_schema=_object_with(a={"$ref": REMOTE_REF})),
        _unresolved(f"{REPORT}.output_schema.properties.a.$ref", REMOTE_REF),
        id="script-output-remote",
    ),
    pytest.param(
        _echo(input_schema=_echo_input(message={"$ref": REMOTE_REF})),
        _unresolved(f"{ECHO}.input_schema.properties.message.$ref", REMOTE_REF),
        id="command-input-remote",
    ),
    pytest.param(
        _report_input({"$ref": "other.json"}),
        _unresolved(f"{REPORT}.input_schema.$ref", "other.json"),
        id="relative-uri",
    ),
    pytest.param(
        _report_input({"$ref": "#/$defs/missing"}),
        _unresolved(f"{REPORT}.input_schema.$ref", "#/$defs/missing"),
        id="pointer-to-nowhere",
    ),
    pytest.param(
        _report_input({"$ref": "#missing"}),
        _unresolved(f"{REPORT}.input_schema.$ref", "#missing"),
        id="no-such-anchor",
    ),
    pytest.param(
        _report_input({"$dynamicRef": REMOTE_REF}),
        _unresolved(f"{REPORT}.input_schema.$dynamicRef", REMOTE_REF),
        id="dynamic-ref-remote",
    ),
    pytest.param(
        _report_input({"$dynamicRef": "#missing"}),
        _unresolved(f"{REPORT}.input_schema.$dynamicRef", "#missing"),
        id="dynamic-ref-no-such-anchor",
    ),
    pytest.param(
        _report_input({"prefixItems": [{}], "items": {"$ref": "#/prefixItems/a"}}),
        _unresolved(f"{REPORT}.input_schema.items.$ref", "#/prefixItems/a"),
        id="pointer-index-not-a-number",
    ),
    pytest.param(
        _report_input({"prefixItems": [{}], "items": {"$ref": "#/prefixItems/5"}}),
        _unresolved(f"{REPORT}.input_schema.items.$ref", "#/prefixItems/5"),
        id="pointer-index-past-the-end",
    ),
    pytest.param(
        _report_input({"$defs": {"t": True}, "$ref": "#/$defs/t/x"}),
        _unresolved(f"{REPORT}.input_schema.$ref", "#/$defs/t/x"),
        id="pointer-through-a-boolean",
    ),
    pytest.param(
        _report_input({"anyOf": [{}, {"$ref": REMOTE_REF}]}),
        _unresolved(f"{REPORT}.input_schema.anyOf[1].$ref", REMOTE_REF),
        id="inside-an-array",
    ),
    pytest.param(
        _report_input({"x-note": {"$ref": REMOTE_REF}}),
        _unresolved(f"{REPORT}.input_schema.x-note.$ref", REMOTE_REF),
        id="under-an-unknown-keyword",
    ),
    pytest.param(
        _report(input_schema=_object_with(const={"$ref": REMOTE_REF})),
        _unresolved(f"{REPORT}.input_schema.properties.const.$ref", REMOTE_REF),
        id="property-named-const",
    ),
    pytest.param(
        _report_input({"$defs": {"examples": {"$ref": REMOTE_REF}}}),
        _unresolved(f"{REPORT}.input_schema.$defs.examples.$ref", REMOTE_REF),
        id="definition-named-examples",
    ),
    pytest.param(
        _report_input(
            {
                "$id": "https://a.example.test/root.json",
                "$defs": {
                    "sub": {"$id": "sub.json", "$ref": "#/$defs/sibling"},
                    "sibling": {},
                },
            }
        ),
        _unresolved(f"{REPORT}.input_schema.$defs.sub.$ref", "#/$defs/sibling"),
        id="pointer-against-a-nested-id",
    ),
    pytest.param(
        _report_input({"$ref": "#/default", "default": {"$ref": REMOTE_REF}}),
        _unresolved(f"{REPORT}.input_schema.$ref", "#/default"),
        id="pointer-to-a-default-value",
    ),
    pytest.param(
        _report_input({"$ref": "#/const/a", "const": {"a": {"$ref": "#/nowhere"}}}),
        _unresolved(f"{REPORT}.input_schema.$ref", "#/const/a"),
        id="pointer-into-a-const-value",
    ),
    pytest.param(
        _report_input({"$ref": "#/enum/0", "enum": [{"$ref": REMOTE_REF}]}),
        _unresolved(f"{REPORT}.input_schema.$ref", "#/enum/0"),
        id="pointer-into-an-enum-value",
    ),
    pytest.param(
        _report_input({"$ref": "#/examples", "examples": [{}]}),
        _unresolved(f"{REPORT}.input_schema.$ref", "#/examples"),
        id="pointer-to-an-examples-value",
    ),
]


@pytest.mark.parametrize(("document", "expected"), UNRESOLVED_REFERENCE_CASES)
def test_parse_manifest_refuses_a_reference_that_does_not_resolve_inside_the_schema(
    document: object, expected: str
) -> None:
    """A $ref or $dynamicRef that the schema alone cannot resolve is refused."""
    assert _refusal(_encode(document)).errors == (expected,)


def test_parse_manifest_reports_each_unresolved_reference_in_document_order() -> None:
    """Every unresolved reference in one schema is its own message, in order."""
    schema = {
        "properties": {"a": {"$ref": "#/b"}, "c": {"items": {"$ref": REMOTE_REF}}},
        "$ref": "#nowhere",
    }

    assert _refusal(_encode(_report_input(schema))).errors == (
        _unresolved(f"{REPORT}.input_schema.properties.a.$ref", "#/b"),
        _unresolved(f"{REPORT}.input_schema.properties.c.items.$ref", REMOTE_REF),
        _unresolved(f"{REPORT}.input_schema.$ref", "#nowhere"),
    )


def test_parse_manifest_walks_an_unknown_keyword_as_deep_as_the_decoder_reads() -> None:
    """The checker skips an unknown keyword, so the walk alone meets its depth."""
    deep: list[object] = []
    for _ in range(2000):
        deep = [deep]
    schema = {"x-deep": deep, "$ref": "#/nowhere"}

    assert _refusal(_encode(_report_input(schema))).errors == (
        _unresolved(f"{REPORT}.input_schema.$ref", "#/nowhere"),
    )


RESOLVED_REFERENCE_SCHEMAS = [
    pytest.param({"$ref": "#"}, id="root"),
    pytest.param(
        _object_with(a={"$ref": "#/$defs/name"}) | {"$defs": {"name": {}}},
        id="pointer",
    ),
    pytest.param({"$defs": {"~/": {}}, "$ref": "#/$defs/~0~1"}, id="escaped-pointer"),
    pytest.param({"$defs": {"n": {"$anchor": "n"}}, "$ref": "#n"}, id="anchor"),
    pytest.param(
        {"$dynamicAnchor": "node", "items": {"$dynamicRef": "#node"}},
        id="dynamic-anchor",
    ),
    pytest.param(
        {
            "$id": "https://a.example.test/root.json",
            "$defs": {"sub": {"$id": "sub.json"}},
            "$ref": "sub.json",
        },
        id="nested-id",
    ),
    pytest.param(
        {
            "$id": "https://a.example.test/root.json",
            "$defs": {
                "sub": {
                    "$id": "sub.json",
                    "$ref": "#/$defs/inner",
                    "$defs": {"inner": {}},
                }
            },
        },
        id="pointer-against-a-nested-id",
    ),
    pytest.param(
        {
            "const": {"$ref": REMOTE_REF},
            "enum": [{"$ref": REMOTE_REF}],
            "default": {"$ref": REMOTE_REF},
            "examples": [{"$dynamicRef": REMOTE_REF}],
        },
        id="references-held-as-values",
    ),
    pytest.param(_object_with(**{"$ref": {"type": "string"}}), id="property-named-ref"),
]


@pytest.mark.parametrize("schema", RESOLVED_REFERENCE_SCHEMAS)
def test_parse_manifest_accepts_references_that_resolve_inside_the_schema(
    schema: dict[str, object],
) -> None:
    """A schema whose references all resolve inside it loads as written."""
    manifest = parse_manifest(_encode(_report_input(schema)))

    assert manifest.actions["report"].input_schema == {"type": "object"} | schema


def test_schema_validator_validates_against_a_local_reference() -> None:
    """A validator from schema_validator follows a # pointer inside its schema."""
    validator = schema_validator(
        {"$defs": {"name": {"type": "string"}}, "items": {"$ref": "#/$defs/name"}}
    )

    assert validator.is_valid(["a"])
    assert not validator.is_valid([1])


def test_schema_validator_builds_a_draft_2020_12_validator() -> None:
    """Under draft 2020-12, items applies only after prefixItems."""
    validator = schema_validator({"prefixItems": [{"type": "string"}], "items": False})

    assert isinstance(validator, Draft202012Validator)
    assert validator.is_valid(["a"])
    assert not validator.is_valid(["a", "b"])


def test_a_remote_reference_makes_no_request() -> None:
    """The manifest check refuses a remote $ref, and a validator never fetches it."""
    with serving_schema() as server:
        reference = f"{server.url}/remote.json"
        schema = {"type": "object", "$ref": reference}

        refusal = _refusal(_encode(_report(input_schema=schema)))
        with pytest.raises(Unresolvable):
            schema_validator(schema).is_valid({})
        requested_before_probe = list(server.requested_paths)
        connection = HTTPConnection(server.url.removeprefix("http://"))
        connection.request("GET", "/probe")
        connection.getresponse().read()
        connection.close()

    assert refusal.errors == (_unresolved(f"{REPORT}.input_schema.$ref", reference),)
    assert requested_before_probe == []
    assert server.requested_paths == ["/probe"]
