import re
import types
from datetime import timedelta
from pathlib import Path

import docker.types
import pytest

from agent_proxy.config import EnvMount, FileMount, Root
from agent_proxy.credentials import CredentialStore
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import CommandAction, Parameter, ScriptAction, parse_manifest
from agent_proxy.runner import (
    CageFailedError,
    CageStartError,
    CageTimeoutError,
    CredentialError,
    InputError,
    RootError,
    RunnerError,
    cage_environment,
    cage_mounts,
    check_roots,
    form_argv,
    script_stdin,
    validate_input,
)

_MANIFEST = parse_manifest(
    (Path(__file__).parents[1] / "test-plugin" / "manifest.json").read_bytes()
)


def _echo() -> CommandAction:
    action = _MANIFEST.actions["echo"]
    assert isinstance(action, CommandAction)
    return action


def _command_action(
    argv: tuple[str, ...],
    properties: dict[str, object],
    parameters: tuple[Parameter, ...],
    *,
    passthrough_args: bool = False,
) -> CommandAction:
    return CommandAction(
        name="test",
        input_schema={
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
        credentials=(),
        timeout=timedelta(seconds=10),
        argv=argv,
        parameters=parameters,
        passthrough_args=passthrough_args,
    )


_SEARCH = _command_action(
    ("grep",),
    {
        "max_count": {"type": "integer"},
        "pattern": {"type": "string"},
        "path": {"type": "string"},
    },
    (
        Parameter("max_count", "flag", "-m"),
        Parameter("pattern", "flag", "-e"),
        Parameter("path", "positional", None),
    ),
)
_TAIL = _command_action(
    ("tail", "-n"),
    {"lines": {"type": "integer"}},
    (Parameter("lines", "positional", None),),
)
_LIST = _command_action(
    ("ls",),
    {
        "path": {"type": "string"},
        "args": {"type": "array", "items": {"type": "string"}},
    },
    (Parameter("path", "positional", None),),
    passthrough_args=True,
)
_SCRIPT = ScriptAction(
    name="test",
    input_schema={"type": "object", "required": ["path"]},
    credentials=(),
    timeout=timedelta(seconds=10),
    executable="/plugin/run",
    output_schema={"type": "object"},
)


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    """Return an empty credential store."""
    return CredentialStore(tmp_path / "credentials")


@pytest.mark.parametrize(
    "error_class",
    [
        InputError,
        RootError,
        CredentialError,
        CageStartError,
        CageTimeoutError,
        CageFailedError,
    ],
)
def test_each_runner_error_is_a_proxy_error(error_class: type[RunnerError]) -> None:
    """Every runner error subclasses RunnerError, which subclasses ProxyError."""
    assert issubclass(error_class, RunnerError)
    assert issubclass(RunnerError, ProxyError)


def test_validate_input_accepts_input_that_meets_the_schema() -> None:
    """Input that meets the schema returns without raising."""
    validate_input(_echo(), {"text": "hi", "no_newline": True})


def test_validate_input_accepts_a_mapping_that_is_not_a_dict() -> None:
    """A read-only mapping passes the schema's object check."""
    validate_input(_echo(), types.MappingProxyType({"text": "hi"}))


def test_validate_input_names_each_violation_with_its_json_path() -> None:
    """Each violation is its own message, led by its JSON path."""
    with pytest.raises(InputError) as excinfo:
        validate_input(_echo(), {"text": 5, "extra": 1})

    assert sorted(excinfo.value.errors) == sorted(
        [
            "$: Additional properties are not allowed ('extra' was unexpected)",
            "$.text: 5 is not of type 'string'",
        ]
    )


def test_validate_input_checks_a_script_action_against_its_schema() -> None:
    """A script action's input is checked against its own input schema."""
    with pytest.raises(InputError, match="'path' is a required property"):
        validate_input(_SCRIPT, {})


def test_form_argv_adds_a_true_boolean_flag_before_the_positional() -> None:
    """A true boolean flag adds the flag; the positional value follows."""
    assert form_argv(_echo(), {"no_newline": True, "text": "hi"}) == (
        "echo",
        "-n",
        "hi",
    )


@pytest.mark.parametrize(
    "arguments",
    [{"text": "hi", "no_newline": False}, {"text": "hi"}],
    ids=["false", "absent"],
)
def test_form_argv_adds_nothing_for_a_false_or_absent_flag(
    arguments: dict[str, object],
) -> None:
    """A false or absent boolean flag adds nothing."""
    assert form_argv(_echo(), arguments) == ("echo", "hi")


@pytest.mark.parametrize("text", ["-n", "--help", "-"])
def test_form_argv_refuses_a_positional_value_that_begins_with_a_dash(
    text: str,
) -> None:
    """A positional value that begins with '-' refuses, naming the parameter."""
    with pytest.raises(InputError, match="'text'"):
        form_argv(_echo(), {"text": text})


def test_form_argv_refuses_a_negative_integer_flag_value() -> None:
    """An integer flag's value of -1 refuses, naming the parameter."""
    with pytest.raises(InputError, match="'max_count'"):
        form_argv(_SEARCH, {"max_count": -1})


def test_form_argv_refuses_a_string_flag_value_that_begins_with_a_dash() -> None:
    """A string flag's value that begins with '-' refuses, naming the parameter."""
    with pytest.raises(InputError, match="'pattern'"):
        form_argv(_SEARCH, {"pattern": "-v"})


def test_form_argv_refuses_a_negative_integer_positional() -> None:
    """A negative integer positional refuses, naming the parameter."""
    with pytest.raises(InputError, match="'lines'"):
        form_argv(_TAIL, {"lines": -3})


@pytest.mark.parametrize("count", [5, 5.0])
def test_form_argv_writes_an_integer_as_decimal_text(count: float) -> None:
    """An integral float, which draft 2020-12 passes as an integer, forms "5"."""
    assert form_argv(_SEARCH, {"max_count": count}) == ("grep", "-m", "5")


def test_form_argv_writes_an_integer_positional() -> None:
    """An integer positional adds its decimal text after the argv prefix."""
    assert form_argv(_TAIL, {"lines": 3}) == ("tail", "-n", "3")


def test_form_argv_adds_a_string_flag_then_its_value() -> None:
    """A string flag adds the flag, then the value; an inner dash is allowed."""
    assert form_argv(_SEARCH, {"pattern": "a-b"}) == ("grep", "-e", "a-b")


def test_form_argv_follows_the_parameter_order_not_the_input_order() -> None:
    """Parameters reach argv in the order the manifest lists them."""
    arguments = {"path": "/work", "pattern": "x", "max_count": 2}

    assert form_argv(_SEARCH, arguments) == ("grep", "-m", "2", "-e", "x", "/work")


def test_form_argv_appends_passthrough_args_last_and_unchecked() -> None:
    """Items of args follow the parameters, and a leading dash passes."""
    arguments = {"args": ["-x", "-l"], "path": "/work"}

    assert form_argv(_LIST, arguments) == ("ls", "/work", "-x", "-l")


def test_script_stdin_is_compact_utf8_json() -> None:
    """The document has no spaces and keeps non-ASCII text as UTF-8."""
    assert script_stdin({"name": "é", "n": 1}) == '{"name":"é","n":1}'.encode()


def test_script_stdin_accepts_a_mapping_that_is_not_a_dict() -> None:
    """A read-only mapping serialises as a JSON object."""
    assert script_stdin(types.MappingProxyType({"n": 1})) == b'{"n":1}'


def test_check_roots_accepts_real_directories(tmp_path: Path) -> None:
    """A root with no symbolic link in its path passes."""
    (tmp_path / "work").mkdir()

    check_roots([Root(path=str(tmp_path / "work"), mode="rw")])


def test_check_roots_leaves_a_missing_path_to_docker(tmp_path: Path) -> None:
    """A root whose path does not exist passes; Docker refuses its bind."""
    check_roots([Root(path=str(tmp_path / "absent" / "work"), mode="ro")])


def test_check_roots_refuses_a_root_below_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    """A root beneath a symbolic link refuses, naming the root."""
    (tmp_path / "real" / "work").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    root_path = str(tmp_path / "link" / "work")

    with pytest.raises(RootError, match=re.escape(repr(root_path))):
        check_roots([Root(path=root_path, mode="ro")])


def test_check_roots_refuses_a_root_that_is_a_dangling_symlink(
    tmp_path: Path,
) -> None:
    """A root that is itself a link to a missing path refuses."""
    (tmp_path / "link").symlink_to(tmp_path / "absent")

    with pytest.raises(RootError):
        check_roots([Root(path=str(tmp_path / "link"), mode="rw")])


def test_check_roots_refuses_a_later_symlinked_root(tmp_path: Path) -> None:
    """Every root is checked, not only the first."""
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    link = str(tmp_path / "link")
    roots = [Root(path=str(tmp_path / "real"), mode="ro"), Root(path=link, mode="rw")]

    with pytest.raises(RootError, match=re.escape(repr(link))):
        check_roots(roots)


def test_cage_environment_maps_each_env_credential_to_its_value(
    store: CredentialStore,
) -> None:
    """An env credential maps to its stored value; a file credential is left out."""
    store.set("api-token", b"s3cret")
    store.set("deploy-key", b"key")
    credentials = {
        "api-token": EnvMount(env="API_TOKEN"),
        "deploy-key": FileMount(file="/run/credentials/deploy-key"),
    }

    assert cage_environment(credentials, store) == {"API_TOKEN": "s3cret"}


def test_cage_environment_refuses_a_value_that_is_not_utf8(
    store: CredentialStore,
) -> None:
    """A non-UTF-8 value refuses; neither message nor cause carries the byte."""
    store.set("api-token", b"\xff")

    with pytest.raises(CredentialError, match="'api-token'") as excinfo:
        cage_environment({"api-token": EnvMount(env="API_TOKEN")}, store)

    assert "xff" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_cage_environment_refuses_a_value_that_holds_a_nul(
    store: CredentialStore,
) -> None:
    """A value holding a NUL refuses without the value in the message."""
    store.set("api-token", b"s3cret\x00tail")

    with pytest.raises(CredentialError, match="'api-token'") as excinfo:
        cage_environment({"api-token": EnvMount(env="API_TOKEN")}, store)

    assert "s3cret" not in str(excinfo.value)


def test_cage_environment_refuses_an_unknown_credential(
    store: CredentialStore,
) -> None:
    """A credential the store lacks refuses with the store's message."""
    with pytest.raises(CredentialError) as excinfo:
        cage_environment({"api-token": EnvMount(env="API_TOKEN")}, store)

    assert str(excinfo.value) == (
        "no credential named 'api-token'; stored names are []"
    )


def test_cage_mounts_binds_each_root_at_its_own_path(store: CredentialStore) -> None:
    """A root binds at its own path, read-only exactly when its mode is ro."""
    roots = [Root(path="/work/src", mode="ro"), Root(path="/work/out", mode="rw")]

    assert cage_mounts(roots, {}, store) == [
        docker.types.Mount(
            target="/work/src", source="/work/src", type="bind", read_only=True
        ),
        docker.types.Mount(
            target="/work/out", source="/work/out", type="bind", read_only=False
        ),
    ]


def test_cage_mounts_binds_each_file_credential_read_only_after_the_roots(
    store: CredentialStore,
) -> None:
    """A file credential binds the store's file read-only; an env one binds nothing."""
    store.set("api-token", b"s3cret")
    store.set("deploy-key", b"key")
    credentials = {
        "api-token": EnvMount(env="API_TOKEN"),
        "deploy-key": FileMount(file="/run/credentials/deploy"),
    }

    assert cage_mounts([Root(path="/work", mode="rw")], credentials, store) == [
        docker.types.Mount(
            target="/work", source="/work", type="bind", read_only=False
        ),
        docker.types.Mount(
            target="/run/credentials/deploy",
            source=str(store.path("deploy-key")),
            type="bind",
            read_only=True,
        ),
    ]


def test_cage_mounts_gives_a_file_credential_a_str_source(
    store: CredentialStore,
) -> None:
    """The source is a str, because the SDK's json.dumps refuses a Path."""
    store.set("deploy-key", b"key")

    [mount] = cage_mounts(
        [], {"deploy-key": FileMount(file="/run/credentials/deploy")}, store
    )

    assert type(mount["Source"]) is str


def test_cage_mounts_refuses_an_unknown_file_credential(
    store: CredentialStore,
) -> None:
    """A file credential the store lacks refuses, naming it."""
    with pytest.raises(CredentialError, match="'deploy-key'"):
        cage_mounts(
            [], {"deploy-key": FileMount(file="/run/credentials/deploy")}, store
        )
