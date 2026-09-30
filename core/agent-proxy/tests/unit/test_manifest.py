import json
from dataclasses import FrozenInstanceError
from datetime import timedelta
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator

from agent_proxy.config import (
    ACTION_NAME_PATTERN,
    DURATION_PATTERN,
    ENV_NAME_PATTERN,
    EnvMount,
    FileMount,
)
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import (
    CommandAction,
    Manifest,
    ManifestError,
    Parameter,
    ScriptAction,
    parse_manifest,
)

OMIT = object()
ECHO = "$.actions.echo"
REPORT = "$.actions.report"
ECHO_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"loud": {"type": "boolean"}, "message": {"type": "string"}},
    "additionalProperties": False,
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


def _command(**overrides: object) -> dict[str, object]:
    """Return a valid command action with overrides applied; OMIT drops a member."""
    members: dict[str, object] = {
        "kind": "command",
        "input_schema": ECHO_INPUT_SCHEMA,
        "credentials": [{"env": "GH_TOKEN"}, {"file": "/run/credentials/ssh-key"}],
        "timeout": "30s",
        "argv": ["echo"],
        "parameters": [
            {"name": "loud", "as": "flag", "flag": "--loud"},
            {"name": "message", "as": "positional"},
        ],
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
        "output_schema": {"type": "object"},
    } | overrides
    return {key: value for key, value in members.items() if value is not OMIT}


def _echo(**overrides: object) -> dict[str, object]:
    """Return a manifest whose one action, echo, is a command action."""
    return {"actions": {"echo": _command(**overrides)}}


def _report(**overrides: object) -> dict[str, object]:
    """Return a manifest whose one action, report, is a script action."""
    return {"actions": {"report": _script(**overrides)}}


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
    ],
)
def test_schema_repeats_each_shared_pattern_exactly(
    path: tuple[str, ...], constant: str
) -> None:
    """Each pattern the schema copies from config.py equals its constant."""
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
                output_schema={"type": "object"},
            ),
        }
    )


def test_parsed_manifest_is_frozen() -> None:
    """Neither the manifest nor an action it holds takes a new field value."""
    manifest = parse_manifest(_encode(_echo()))

    with pytest.raises(FrozenInstanceError):
        manifest.actions = {}
    with pytest.raises(FrozenInstanceError):
        manifest.actions["echo"].timeout = timedelta(seconds=1)


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
        f"{ECHO}.kind: 'shell' is not one of ['command', 'script']",
        id="kind-value",
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
