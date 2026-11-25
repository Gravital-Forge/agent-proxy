import re
import socket
import threading
import types
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import ANY, MagicMock, call

import docker.errors
import docker.types
import pytest
import requests
from referencing.exceptions import Unresolvable
from schema_server import serving_schema

from agent_proxy import runner
from agent_proxy.cage_seccomp import cage_profile_json
from agent_proxy.config import EnvMount, FileMount, Grant, Root
from agent_proxy.credentials import CredentialStore, unset_message
from agent_proxy.egress import CageNetwork
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import CommandAction, Parameter, ScriptAction, parse_manifest
from agent_proxy.results import OUTPUT_CAP_BYTES
from agent_proxy.runner import (
    CAGE_LABEL,
    CAGE_MEMORY_BYTES,
    CAGE_PIDS_LIMIT,
    CONTAINER_LABELS,
    CONTAINER_USER,
    LOG_DRIVER,
    LOG_OPTIONS,
    TMPFS_OPTIONS,
    TMPFS_PATH,
    VERIFIER_IMAGE,
    VERIFIER_MOUNT,
    VERIFIER_PATH,
    VERIFIER_REFUSED_EXIT,
    CageCall,
    CageFailedError,
    CageLimiter,
    CageRunner,
    CageStartError,
    CageTimeoutError,
    CredentialError,
    InputError,
    Outcome,
    RootError,
    RootIdentity,
    RunnerError,
    cage_environment,
    cage_mounts,
    form_argv,
    hold_roots,
    script_stdin,
    validate_input,
)

_PINNED_VERIFIER = "ghcr.io/example/agent-proxy-verifier@sha256:" + "a" * 64
_SECURITY_OPT = ["no-new-privileges", f"seccomp={cage_profile_json()}"]
_LOG_OPTIONS = {"max-size": "512m", "max-file": "2"}
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


@pytest.fixture
def nonce(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin the nonce every call makes, and return it."""
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _nbytes: NONCE)
    return NONCE


CALL_ID = "0123456789abcdef0123456789abcdef"
IMAGE = "registry.test/plugin@sha256:" + "0" * 64
NETWORK = CageNetwork(name="apx-cage-net", environment={})
PROXY_VARIABLE = {"HTTPS_PROXY": "http://egress:1080"}
_GRANT = Grant(roots=[], domains=[], credentials={})
NONCE = "feedface" * 4


def _client(*, out: bytes = b"", err: bytes = b"", status: int = 0) -> MagicMock:
    """Return a Docker client whose cage exits with status after writing out and err."""
    client = MagicMock()
    container = client.containers.create.return_value
    container.wait.return_value = {"StatusCode": status}

    def logs(*, stdout: bool, stderr: bool, stream: bool) -> Iterator[bytes]:
        return iter([out] if stdout else [err])

    container.logs.side_effect = logs
    return client


def _container(client: MagicMock) -> MagicMock:
    return client.containers.create.return_value


def _stdin(client: MagicMock) -> MagicMock:
    """Return the raw socket the runner writes a script's input to."""
    return _container(client).attach_socket.return_value._sock


def _verifier_mount() -> dict[str, object]:
    return {
        "Type": "image",
        "Source": VERIFIER_IMAGE,
        "Target": VERIFIER_MOUNT,
        "ReadOnly": True,
    }


def _root_arguments(path: Path) -> list[str]:
    """Return the verifier's path, device, and inode arguments for a root."""
    status = path.stat()
    return [str(path), str(status.st_dev), str(status.st_ino)]


def _open_descriptors() -> int:
    """Return how many descriptors this process holds open."""
    return len(list(Path("/proc/self/fd").iterdir()))


def _cage_call(
    action: CommandAction | ScriptAction,
    arguments: Mapping[str, object],
    *,
    grant: Grant = _GRANT,
    network: CageNetwork = NETWORK,
    call_id: str = CALL_ID,
    overflow_dir: str = "/overflow",
    max_cages: int = 2,
) -> CageCall:
    return CageCall(
        call_id=call_id,
        image=IMAGE,
        action=action,
        grant=grant,
        arguments=arguments,
        network=network,
        overflow_dir=overflow_dir,
        max_cages=max_cages,
    )


def _run(client: MagicMock, store: CredentialStore, cage_call: CageCall) -> Outcome:
    return CageRunner(client, store, CageLimiter()).run(cage_call)


def _enters_slot(limiter: CageLimiter, limit: int, within: float) -> bool:
    """Return whether another thread takes a slot at limit within the given seconds."""
    entered = threading.Event()

    def take() -> None:
        with limiter.slot(limit):
            entered.set()

    threading.Thread(target=take, daemon=True).start()
    return entered.wait(timeout=within)


def _symlinked_root_grant(tmp_path: Path) -> Grant:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    return Grant(
        roots=[Root(path=str(tmp_path / "link"), mode="rw")],
        domains=[],
        credentials={},
    )


def _missing_root_grant(tmp_path: Path) -> Grant:
    return Grant(
        roots=[Root(path=str(tmp_path / "absent"), mode="rw")],
        domains=[],
        credentials={},
    )


def _unknown_env_credential_grant(tmp_path: Path) -> Grant:
    return Grant(
        roots=[], domains=[], credentials={"api-token": EnvMount(env="API_TOKEN")}
    )


def _unknown_file_credential_grant(tmp_path: Path) -> Grant:
    return Grant(
        roots=[],
        domains=[],
        credentials={"deploy-key": FileMount(file="/run/credentials/deploy")},
    )


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


def test_validate_input_fetches_no_remote_reference() -> None:
    """A remote $ref the manifest check would refuse is unresolvable, not fetched."""
    with serving_schema() as server:
        action = _command_action(
            ("echo",),
            {"text": {"$ref": f"{server.url}/text.json"}},
            (Parameter("text", "positional", None),),
        )

        with pytest.raises(Unresolvable):
            validate_input(action, {"text": "hi"})

    assert server.requested_paths == []


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


def _missing(tmp_path: Path) -> Path:
    return tmp_path / "absent" / "work"


def _file(tmp_path: Path) -> Path:
    (tmp_path / "work").write_text("")
    return tmp_path / "work"


def _link_as_root(tmp_path: Path) -> Path:
    (tmp_path / "real").mkdir()
    (tmp_path / "work").symlink_to(tmp_path / "real")
    return tmp_path / "work"


def _link_at_parent(tmp_path: Path) -> Path:
    (tmp_path / "real" / "work").mkdir(parents=True)
    (tmp_path / "parent").symlink_to(tmp_path / "real")
    return tmp_path / "parent" / "work"


def _dangling_link(tmp_path: Path) -> Path:
    (tmp_path / "work").symlink_to(tmp_path / "absent")
    return tmp_path / "work"


def test_hold_roots_returns_each_root_s_device_and_inode_in_order(
    tmp_path: Path,
) -> None:
    """Each root's identity is its path and its os.stat pair, in grant order."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    roots = [Root(path=str(first), mode="ro"), Root(path=str(second), mode="rw")]

    with ExitStack() as held:
        identities = hold_roots(roots, held)

    assert identities == [
        RootIdentity(str(first), first.stat().st_dev, first.stat().st_ino),
        RootIdentity(str(second), second.stat().st_dev, second.stat().st_ino),
    ]


@pytest.mark.parametrize(
    ("make_root", "component", "reason"),
    [
        (_missing, "absent", "does not exist"),
        (_file, "work", "is a symbolic link or not a directory"),
        (_link_as_root, "work", "is a symbolic link or not a directory"),
        (_link_at_parent, "parent", "is a symbolic link or not a directory"),
        (_dangling_link, "work", "is a symbolic link or not a directory"),
    ],
    ids=["missing", "file", "link-as-root", "link-at-parent", "dangling-link"],
)
def test_hold_roots_refuses_a_root_that_is_not_a_chain_of_directories(
    tmp_path: Path,
    make_root: Callable[[Path], Path],
    component: str,
    reason: str,
) -> None:
    """The refusal names the root, the component, and what is wrong with it."""
    root_path = str(make_root(tmp_path))

    with pytest.raises(RootError) as excinfo, ExitStack() as held:
        hold_roots([Root(path=root_path, mode="rw")], held)

    assert repr(root_path) in str(excinfo.value)
    assert f"component {component!r} {reason}" in str(excinfo.value)


def test_hold_roots_refuses_another_open_failure_with_the_os_s_reason(
    tmp_path: Path,
) -> None:
    """A component the OS will not open for another reason gives its strerror."""
    root_path = str(tmp_path / ("x" * 256))

    with pytest.raises(RootError) as excinfo, ExitStack() as held:
        hold_roots([Root(path=root_path, mode="rw")], held)

    assert repr(root_path) in str(excinfo.value)
    assert "cannot be opened: File name too long" in str(excinfo.value)


def test_hold_roots_holds_one_descriptor_per_root_until_the_stack_closes(
    tmp_path: Path,
) -> None:
    """Inside the stack each root holds one descriptor; after it, none is left."""
    (tmp_path / "deep" / "first").mkdir(parents=True)
    (tmp_path / "deep" / "second").mkdir()
    roots = [
        Root(path=str(tmp_path / "deep" / "first"), mode="ro"),
        Root(path=str(tmp_path / "deep" / "second"), mode="rw"),
    ]
    before = _open_descriptors()

    with ExitStack() as held:
        hold_roots(roots, held)
        inside = _open_descriptors()

    assert inside == before + 2
    assert _open_descriptors() == before


def test_hold_roots_closes_every_descriptor_when_a_later_root_is_refused(
    tmp_path: Path,
) -> None:
    """A refused later root leaves no descriptor open once the stack closes."""
    (tmp_path / "work").mkdir()
    roots = [
        Root(path=str(tmp_path / "work"), mode="rw"),
        Root(path=str(tmp_path / "absent"), mode="rw"),
    ]
    before = _open_descriptors()

    with pytest.raises(RootError), ExitStack() as held:
        hold_roots(roots, held)

    assert _open_descriptors() == before


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
    """A credential the store lacks refuses by name, naming no stored credential."""
    store.set("other-token", b"s3cret")

    with pytest.raises(CredentialError) as excinfo:
        cage_environment({"api-token": EnvMount(env="API_TOKEN")}, store)

    assert str(excinfo.value) == unset_message("api-token")


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
    """A file credential the store lacks refuses by name, naming no stored one."""
    store.set("other-key", b"key")

    with pytest.raises(CredentialError) as excinfo:
        cage_mounts(
            [], {"deploy-key": FileMount(file="/run/credentials/deploy")}, store
        )

    assert str(excinfo.value) == unset_message("deploy-key")


def test_the_cage_constants_are_the_spec_s() -> None:
    """No product labels, a 64 MiB /tmp, root, capped logs, the limits, the verifier."""
    assert isinstance(CONTAINER_LABELS, types.MappingProxyType)
    assert dict(CONTAINER_LABELS) == {}
    assert CAGE_LABEL == "agent-proxy.cage"
    assert TMPFS_PATH == "/tmp"  # noqa: S108 - the path is inside the cage
    assert TMPFS_OPTIONS == "rw,size=64m"
    assert CONTAINER_USER == "0:0"
    assert LOG_DRIVER == "json-file"
    assert runner.SOURCE_VERIFIER_IMAGE == "agent-proxy-verifier:1"
    assert runner.VERIFIER_IMAGE_VARIABLE == "AGENT_PROXY_VERIFIER_IMAGE"
    assert VERIFIER_MOUNT == "/.agent-proxy"
    assert VERIFIER_PATH == "/.agent-proxy/verify"
    assert VERIFIER_REFUSED_EXIT == 125
    assert CAGE_PIDS_LIMIT == 1024
    assert CAGE_MEMORY_BYTES == 4 * 2**30
    assert dict(LOG_OPTIONS) == {"max-size": "512m", "max-file": "2"}


def test_run_creates_a_hardened_command_cage_whose_verifier_runs_its_argv(
    tmp_path: Path, store: CredentialStore, nonce: str
) -> None:
    """The verifier checks the root, then runs the argv; its image mounts last."""
    store.set("api-token", b"s3cret")
    store.set("deploy-key", b"key")
    work = tmp_path / "work"
    work.mkdir()
    grant = Grant(
        roots=[Root(path=str(work), mode="rw")],
        domains=[],
        credentials={
            "api-token": EnvMount(env="API_TOKEN"),
            "deploy-key": FileMount(file="/run/credentials/deploy"),
        },
    )
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}, grant=grant))

    client.containers.create.assert_called_once_with(
        IMAGE,
        entrypoint=[
            VERIFIER_PATH,
            nonce,
            *_root_arguments(work),
            "--",
            "echo",
            "hi",
        ],
        stdin_open=False,
        user=CONTAINER_USER,
        read_only=True,
        cap_drop=["ALL"],
        security_opt=_SECURITY_OPT,
        tmpfs={TMPFS_PATH: TMPFS_OPTIONS},
        mounts=[
            docker.types.Mount(
                target=str(work), source=str(work), type="bind", read_only=False
            ),
            docker.types.Mount(
                target="/run/credentials/deploy",
                source=str(store.path("deploy-key")),
                type="bind",
                read_only=True,
            ),
            _verifier_mount(),
        ],
        environment={"API_TOKEN": "s3cret", "AGENT_PROXY_ROOTS": f'["{work}"]'},
        network=NETWORK.name,
        labels={"agent-proxy.cage": "1"},
        pids_limit=CAGE_PIDS_LIMIT,
        mem_limit=CAGE_MEMORY_BYTES,
        memswap_limit=CAGE_MEMORY_BYTES,
        log_config=docker.types.LogConfig(type=LOG_DRIVER, config=_LOG_OPTIONS),
    )


def test_run_creates_a_script_cage_that_takes_its_input_on_stdin(
    store: CredentialStore, nonce: str
) -> None:
    """The verifier runs the script's executable, and stdin is open."""
    client = _client(out=b"{}")

    _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    client.containers.create.assert_called_once_with(
        IMAGE,
        entrypoint=[VERIFIER_PATH, nonce, "--", "/plugin/run"],
        stdin_open=True,
        user=CONTAINER_USER,
        read_only=True,
        cap_drop=["ALL"],
        security_opt=_SECURITY_OPT,
        tmpfs={TMPFS_PATH: TMPFS_OPTIONS},
        mounts=[_verifier_mount()],
        environment={"AGENT_PROXY_ROOTS": "[]"},
        network=NETWORK.name,
        labels={"agent-proxy.cage": "1"},
        pids_limit=CAGE_PIDS_LIMIT,
        mem_limit=CAGE_MEMORY_BYTES,
        memswap_limit=CAGE_MEMORY_BYTES,
        log_config=docker.types.LogConfig(type=LOG_DRIVER, config=_LOG_OPTIONS),
    )


def test_run_gives_the_cage_its_grant_s_memory_with_no_extra_swap(
    store: CredentialStore,
) -> None:
    """A grant's memory replaces the default for the memory and the swap limit."""
    grant = Grant.model_validate(
        {"roots": [], "domains": [], "credentials": {}, "memory": "8g"}
    )
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}, grant=grant))

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["mem_limit"] == 8 * 2**30
    assert kwargs["memswap_limit"] == 8 * 2**30
    assert kwargs["pids_limit"] == CAGE_PIDS_LIMIT


def test_run_gives_the_cage_its_network_s_environment_beside_its_credentials(
    store: CredentialStore,
) -> None:
    """The cage joins the call's network and takes its variable as given."""
    store.set("api-token", b"s3cret")
    grant = Grant(
        roots=[], domains=[], credentials={"api-token": EnvMount(env="API_TOKEN")}
    )
    network = CageNetwork(
        name="apx-egress-0123456789abcdef", environment=PROXY_VARIABLE
    )
    client = _client()

    _run(
        client, store, _cage_call(_echo(), {"text": "hi"}, grant=grant, network=network)
    )

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["network"] == "apx-egress-0123456789abcdef"
    assert kwargs["environment"] == {
        "HTTPS_PROXY": "http://egress:1080",
        "API_TOKEN": "s3cret",
        "AGENT_PROXY_ROOTS": "[]",
    }


def test_run_gives_the_cage_its_grant_s_env_beside_its_other_variables(
    store: CredentialStore,
) -> None:
    """The grant's env joins the network's variable, the credentials, and the roots."""
    store.set("api-token", b"s3cret")
    grant = Grant(
        roots=[],
        domains=[],
        credentials={"api-token": EnvMount(env="API_TOKEN")},
        env={"GIT_AUTHOR_NAME": "Agent One"},
    )
    network = CageNetwork(
        name="apx-egress-0123456789abcdef", environment=PROXY_VARIABLE
    )
    client = _client()

    _run(
        client, store, _cage_call(_echo(), {"text": "hi"}, grant=grant, network=network)
    )

    assert client.containers.create.call_args.kwargs["environment"] == {
        "HTTPS_PROXY": "http://egress:1080",
        "API_TOKEN": "s3cret",
        "AGENT_PROXY_ROOTS": "[]",
        "GIT_AUTHOR_NAME": "Agent One",
    }


def test_run_gives_the_cage_its_roots_in_grant_order(
    tmp_path: Path, store: CredentialStore
) -> None:
    """The roots variable is the compact JSON array of the root paths as granted."""
    zeta = tmp_path / "zeta"
    alpha = tmp_path / "alpha"
    zeta.mkdir()
    alpha.mkdir()
    grant = Grant(
        roots=[Root(path=str(zeta), mode="rw"), Root(path=str(alpha), mode="ro")],
        domains=[],
        credentials={},
    )
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}, grant=grant))

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["environment"] == {"AGENT_PROXY_ROOTS": f'["{zeta}","{alpha}"]'}


def test_run_gives_a_cage_with_no_roots_an_empty_roots_array(
    store: CredentialStore,
) -> None:
    """A grant with no roots still sets the roots variable, to an empty array."""
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["environment"] == {"AGENT_PROXY_ROOTS": "[]"}


def test_run_starts_waits_reads_both_streams_then_removes_a_command_cage(
    store: CredentialStore,
) -> None:
    """A command cage attaches nothing; stdout is read before stderr."""
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert _container(client).mock_calls == [
        call.start(),
        call.wait(timeout=ANY),
        call.logs(stdout=True, stderr=False, stream=True),
        call.logs(stdout=False, stderr=True, stream=True),
        call.remove(force=True),
    ]


def test_run_sends_a_script_its_input_then_end_of_file_between_start_and_wait(
    store: CredentialStore,
) -> None:
    """Stdin is attached before start and closed after wait, before the logs."""
    client = _client(out=b"{}")

    _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    assert _container(client).mock_calls == [
        call.attach_socket(params={"stdin": 1, "stream": 1}),
        call.start(),
        call.attach_socket()._sock.settimeout(ANY),
        call.attach_socket()._sock.sendall(b'{"path":"/work"}'),
        call.attach_socket()._sock.shutdown(socket.SHUT_WR),
        call.wait(timeout=ANY),
        call.attach_socket()._sock.close(),
        call.logs(stdout=True, stderr=False, stream=True),
        call.logs(stdout=False, stderr=True, stream=True),
        call.remove(force=True),
    ]


def test_run_holds_a_slot_under_the_call_s_cap_from_create_until_removal(
    store: CredentialStore,
) -> None:
    """The slot is taken at max_cages before create and left after remove."""
    client = _client()
    limiter = MagicMock()
    order = MagicMock()
    order.attach_mock(limiter.slot, "slot")
    order.attach_mock(client.containers.create, "create")

    CageRunner(client, store, limiter).run(
        _cage_call(_echo(), {"text": "hi"}, max_cages=3)
    )

    assert order.mock_calls[0] == call.slot(3)
    assert [name for name, _, _ in order.mock_calls] == [
        "slot",
        "slot().__enter__",
        "create",
        "create().start",
        "create().wait",
        "create().logs",
        "create().logs",
        "create().remove",
        "slot().__exit__",
    ]


def test_run_returns_the_command_result_and_what_the_cage_measured(
    store: CredentialStore,
) -> None:
    """The result carries the exit code and both streams; the sizes are in bytes."""
    client = _client(out="é\n".encode(), err=b"", status=3)

    outcome = _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert outcome == Outcome(
        content={"exit_code": 3, "stdout": "é\n", "stderr": ""},
        exit_code=3,
        duration_ms=outcome.duration_ms,
        stdout_bytes=3,
        stderr_bytes=0,
        truncated=False,
    )
    assert isinstance(outcome.duration_ms, int)
    assert outcome.duration_ms >= 0


def test_run_returns_a_script_s_own_output_when_it_succeeds(
    store: CredentialStore,
) -> None:
    """A script that exits 0 with a valid object returns that object."""
    client = _client(out=b'{"ok": true}')

    outcome = _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    assert outcome.content == {"ok": True}


def test_run_returns_the_command_result_for_a_script_that_exits_non_zero(
    store: CredentialStore,
) -> None:
    """A failed script returns the command result."""
    client = _client(err=b"boom", status=1)

    outcome = _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    assert outcome.content == {"exit_code": 1, "stdout": "", "stderr": "boom"}


def test_run_spills_a_stream_over_the_cap_and_reports_it_truncated(
    tmp_path: Path, store: CredentialStore
) -> None:
    """A stream past the cap goes to the overflow directory under the call ID."""
    overflow = tmp_path / "overflow"
    overflow.mkdir()
    client = _client(out=b"x" * (OUTPUT_CAP_BYTES + 1))

    outcome = _run(
        client,
        store,
        _cage_call(_echo(), {"text": "hi"}, overflow_dir=str(overflow)),
    )

    assert outcome.truncated is True
    assert outcome.stdout_bytes == OUTPUT_CAP_BYTES + 1
    assert outcome.content["stdout_file"] == str(overflow / f"{CALL_ID}.stdout")


def test_run_waits_at_most_the_action_s_timeout_when_the_grant_sets_none(
    store: CredentialStore,
) -> None:
    """With no grant timeout, the wait is bounded by the manifest's."""
    client = _client()
    action = replace(_echo(), timeout=timedelta(seconds=10))

    _run(client, store, _cage_call(action, {"text": "hi"}))

    seconds = _container(client).wait.call_args.kwargs["timeout"]
    assert 0 < seconds <= 10


def test_run_waits_at_most_the_grant_s_timeout_when_it_sets_one(
    store: CredentialStore,
) -> None:
    """A grant timeout replaces the manifest's, even when it is longer."""
    client = _client()
    action = replace(_echo(), timeout=timedelta(seconds=10))
    grant = Grant.model_validate(
        {"roots": [], "domains": [], "credentials": {}, "timeout": "300s"}
    )

    _run(client, store, _cage_call(action, {"text": "hi"}, grant=grant))

    seconds = _container(client).wait.call_args.kwargs["timeout"]
    assert 10 < seconds <= 300


def test_run_times_out_without_waiting_when_no_time_is_left(
    store: CredentialStore,
) -> None:
    """A spent timeout refuses before wait, naming the action; the cage is removed."""
    client = _client()
    action = replace(_echo(), timeout=timedelta(0))

    with pytest.raises(CageTimeoutError, match="'echo'"):
        _run(client, store, _cage_call(action, {"text": "hi"}))

    _container(client).wait.assert_not_called()
    _container(client).remove.assert_called_once_with(force=True)


def test_run_times_out_without_sending_stdin_when_no_time_is_left(
    store: CredentialStore,
) -> None:
    """A spent timeout refuses before the input is sent; the socket is closed."""
    client = _client()
    action = replace(_SCRIPT, timeout=timedelta(0))

    with pytest.raises(CageTimeoutError):
        _run(client, store, _cage_call(action, {"path": "/work"}))

    _stdin(client).sendall.assert_not_called()
    _stdin(client).close.assert_called_once_with()
    _container(client).wait.assert_not_called()
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    "error",
    [requests.exceptions.ConnectionError(), requests.exceptions.ReadTimeout()],
    ids=["connection-error", "read-timeout"],
)
def test_run_times_out_when_the_wait_outlasts_the_timeout(
    store: CredentialStore, error: Exception
) -> None:
    """A wait that times out refuses with CageTimeoutError; the cage is removed."""
    client = _client()
    _container(client).wait.side_effect = error

    with pytest.raises(CageTimeoutError, match="'echo'"):
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    _container(client).remove.assert_called_once_with(force=True)


def test_run_times_out_when_the_script_does_not_take_its_input_in_time(
    store: CredentialStore,
) -> None:
    """A socket timeout while sending refuses; the socket closes, the cage goes."""
    client = _client()
    _stdin(client).sendall.side_effect = TimeoutError()

    with pytest.raises(CageTimeoutError, match="'test'"):
        _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    _container(client).wait.assert_not_called()
    _stdin(client).close.assert_called_once_with()
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.parametrize("error", [BrokenPipeError(), ConnectionResetError()])
def test_run_goes_on_when_the_script_stops_reading_its_input(
    store: CredentialStore, error: OSError
) -> None:
    """A script that closes its stdin early still runs to its exit."""
    client = _client(out=b"{}")
    _stdin(client).sendall.side_effect = error

    outcome = _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    _stdin(client).shutdown.assert_not_called()
    _container(client).wait.assert_called_once()
    assert outcome.content == {}


def test_run_fails_the_cage_on_another_error_of_the_stdin_socket(
    store: CredentialStore,
) -> None:
    """A socket error other than a timeout or a closed pipe raises CageFailedError."""
    client = _client()
    _stdin(client).shutdown.side_effect = OSError(
        107, "Transport endpoint is not connected"
    )

    with pytest.raises(CageFailedError, match="not connected"):
        _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    _container(client).wait.assert_not_called()
    _stdin(client).close.assert_called_once_with()
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    "error",
    [
        docker.errors.ImageNotFound("no such image"),
        docker.errors.APIError("create refused"),
        requests.exceptions.ConnectionError("daemon down"),
    ],
    ids=["image-not-found", "api-error", "connection-error"],
)
def test_run_refuses_a_cage_it_cannot_create_naming_the_image(
    store: CredentialStore, error: Exception
) -> None:
    """A create failure raises CageStartError naming both images and Docker's error."""
    client = _client()
    client.containers.create.side_effect = error

    with pytest.raises(CageStartError, match=re.escape(repr(IMAGE))) as excinfo:
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert str(error) in str(excinfo.value)
    assert repr(VERIFIER_IMAGE) in str(excinfo.value)
    _container(client).remove.assert_not_called()


def test_run_refuses_a_cage_it_cannot_start(store: CredentialStore) -> None:
    """A start failure raises CageStartError; the created cage is removed."""
    client = _client()
    _container(client).start.side_effect = docker.errors.APIError("start refused")

    with pytest.raises(CageStartError, match="start refused"):
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    _container(client).remove.assert_called_once_with(force=True)


def test_run_refuses_a_script_cage_whose_stdin_cannot_attach(
    store: CredentialStore,
) -> None:
    """An attach failure raises CageStartError before start; the cage is removed."""
    client = _client()
    _container(client).attach_socket.side_effect = docker.errors.APIError("no attach")

    with pytest.raises(CageStartError, match="no attach"):
        _run(client, store, _cage_call(_SCRIPT, {"path": "/work"}))

    _container(client).start.assert_not_called()
    _container(client).remove.assert_called_once_with(force=True)


def test_run_fails_the_cage_when_the_wait_fails(store: CredentialStore) -> None:
    """A Docker failure in wait that is not a timeout raises CageFailedError."""
    client = _client()
    _container(client).wait.side_effect = docker.errors.APIError("wait failed")

    with pytest.raises(CageFailedError, match="wait failed"):
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    _container(client).remove.assert_called_once_with(force=True)


def test_run_fails_the_cage_when_the_logs_call_fails(store: CredentialStore) -> None:
    """A Docker failure raised by the logs call raises CageFailedError."""
    client = _client()
    _container(client).logs.side_effect = docker.errors.APIError("logs failed")

    with pytest.raises(CageFailedError, match="logs failed"):
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    _container(client).remove.assert_called_once_with(force=True)


def test_run_fails_the_cage_when_it_cannot_be_removed(
    store: CredentialStore,
) -> None:
    """A removal failure raises CageFailedError, naming the action."""
    client = _client()
    _container(client).remove.side_effect = docker.errors.APIError("busy")

    with pytest.raises(CageFailedError, match="'echo'"):
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))


def test_run_gives_each_call_a_fresh_nonce_of_32_hex_digits(
    store: CredentialStore,
) -> None:
    """Two calls pass the verifier different nonces, each 16 random bytes in hex."""
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}))
    _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    nonces = [
        create.kwargs["entrypoint"][1]
        for create in client.containers.create.call_args_list
    ]
    assert all(re.fullmatch("[0-9a-f]{32}", value) for value in nonces)
    assert nonces[0] != nonces[1]


def test_run_mounts_the_verifier_image_named_when_the_cage_is_created(
    store: CredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image mount reads VERIFIER_IMAGE at create, so a test can redirect it."""
    monkeypatch.setattr(runner, "VERIFIER_IMAGE", "apx-verifier:it")
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    mounts = client.containers.create.call_args.kwargs["mounts"]
    assert mounts[-1]["Source"] == "apx-verifier:it"


@pytest.mark.parametrize("environ", [{}, {"AGENT_PROXY_VERIFIER_IMAGE": ""}])
def test_an_unset_or_empty_verifier_variable_gives_the_source_build_s_tag(
    environ: Mapping[str, str],
) -> None:
    """With no reference in the variable, the verifier is the source build's tag."""
    assert runner.verifier_image(environ) == "agent-proxy-verifier:1"


def test_the_verifier_variable_names_the_verifier_image() -> None:
    """A reference in AGENT_PROXY_VERIFIER_IMAGE is the verifier image."""
    environ = {"AGENT_PROXY_VERIFIER_IMAGE": _PINNED_VERIFIER}

    assert runner.verifier_image(environ) == _PINNED_VERIFIER


def test_a_pinned_verifier_on_the_daemon_is_not_pulled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest reference the daemon holds is looked up and left alone."""
    monkeypatch.setattr(runner, "VERIFIER_IMAGE", _PINNED_VERIFIER)
    client = MagicMock()

    runner.ensure_verifier(client)

    client.images.get.assert_called_once_with(_PINNED_VERIFIER)
    client.images.pull.assert_not_called()


def test_a_pinned_verifier_absent_from_the_daemon_is_pulled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest reference the daemon lacks is pulled by that reference."""
    monkeypatch.setattr(runner, "VERIFIER_IMAGE", _PINNED_VERIFIER)
    client = MagicMock()
    client.images.get.side_effect = docker.errors.ImageNotFound("no such image")

    runner.ensure_verifier(client)

    client.images.pull.assert_called_once_with(_PINNED_VERIFIER)


def test_a_verifier_named_by_tag_is_neither_looked_up_nor_pulled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tag reference must already be on the daemon; nothing touches it at start."""
    monkeypatch.setattr(runner, "VERIFIER_IMAGE", "agent-proxy-verifier:1")
    client = MagicMock()

    runner.ensure_verifier(client)

    client.images.get.assert_not_called()
    client.images.pull.assert_not_called()


_MEMORY_WARNING = (
    "the Docker daemon cannot enforce a cage's memory limit, "
    "so each cage runs without one"
)
_PIDS_WARNING = (
    "the Docker daemon cannot enforce a cage's process limit, "
    "so each cage runs without one"
)


@pytest.mark.parametrize(
    ("memory_limit", "pids_limit", "expected"),
    [
        pytest.param(True, True, (), id="both-enforced"),
        pytest.param(False, True, (_MEMORY_WARNING,), id="no-memory-limit"),
        pytest.param(True, False, (_PIDS_WARNING,), id="no-process-limit"),
        pytest.param(False, False, (_MEMORY_WARNING, _PIDS_WARNING), id="neither"),
    ],
)
def test_limit_warnings_name_each_limit_the_daemon_cannot_enforce(
    memory_limit: bool, pids_limit: bool, expected: tuple[str, ...]
) -> None:
    """Each limit the daemon's info reports false gives one warning, memory first."""
    client = MagicMock()
    client.info.return_value = {"MemoryLimit": memory_limit, "PidsLimit": pids_limit}

    assert runner.limit_warnings(client) == expected


@pytest.mark.parametrize(
    "error",
    [
        docker.errors.APIError("info refused"),
        requests.exceptions.ConnectionError("connection refused"),
    ],
)
def test_limit_warnings_fail_as_a_runner_error_when_the_info_call_fails(
    error: Exception,
) -> None:
    """A failed info call raises RunnerError, naming Docker's error."""
    client = MagicMock()
    client.info.side_effect = error

    with pytest.raises(RunnerError, match=re.escape(str(error))):
        runner.limit_warnings(client)


_TEST_LABELS = types.MappingProxyType({"agent-proxy.test": "1"})


def _left_cage(name: str) -> MagicMock:
    cage = MagicMock()
    cage.name = name
    return cage


def test_run_labels_the_cage_beside_the_container_labels(
    store: CredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cage carries the cage label and every label of CONTAINER_LABELS."""
    monkeypatch.setattr(runner, "CONTAINER_LABELS", _TEST_LABELS)
    client = _client()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    labels = client.containers.create.call_args.kwargs["labels"]
    assert labels == {"agent-proxy.cage": "1", "agent-proxy.test": "1"}


@pytest.mark.parametrize(
    ("container_labels", "label_filter"),
    [
        pytest.param({}, ["agent-proxy.cage"], id="product"),
        pytest.param(
            _TEST_LABELS, ["agent-proxy.cage", "agent-proxy.test=1"], id="test-image"
        ),
    ],
)
def test_remove_left_cages_lists_every_cage_with_the_container_labels(
    container_labels: Mapping[str, str],
    label_filter: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing takes stopped containers too, and needs every label to match."""
    monkeypatch.setattr(
        runner, "CONTAINER_LABELS", types.MappingProxyType(container_labels)
    )
    client = MagicMock()
    client.containers.list.return_value = []

    runner.remove_left_cages(client)

    client.containers.list.assert_called_once_with(
        all=True, filters={"label": label_filter}, ignore_removed=True
    )


def test_remove_left_cages_force_removes_each_listed_cage_and_names_it() -> None:
    """Each cage is removed by force, running or not, and named in listing order."""
    running, stopped = _left_cage("running"), _left_cage("stopped")
    client = MagicMock()
    client.containers.list.return_value = [running, stopped]

    removal = runner.remove_left_cages(client)

    assert removal == runner.LeftCageRemoval(
        removed=("running", "stopped"), warnings=()
    )
    running.remove.assert_called_once_with(force=True)
    stopped.remove.assert_called_once_with(force=True)


def test_remove_left_cages_warns_of_a_cage_it_cannot_remove_and_goes_on() -> None:
    """A failed removal gives one warning naming the cage; the next cage is removed."""
    stuck, next_cage = _left_cage("apx-stuck"), _left_cage("apx-next")
    stuck.remove.side_effect = docker.errors.APIError("removal refused")
    client = MagicMock()
    client.containers.list.return_value = [stuck, next_cage]

    removal = runner.remove_left_cages(client)

    [warning] = removal.warnings
    assert "'apx-stuck'" in warning
    assert "removal refused" in warning
    assert removal.removed == ("apx-next",)
    next_cage.remove.assert_called_once_with(force=True)


def test_remove_left_cages_neither_warns_of_nor_names_a_cage_already_gone() -> None:
    """A cage gone between the listing and its removal gives no warning and no name."""
    gone = _left_cage("apx-gone")
    gone.remove.side_effect = docker.errors.NotFound("no such container")
    client = MagicMock()
    client.containers.list.return_value = [gone]

    assert runner.remove_left_cages(client) == runner.LeftCageRemoval(
        removed=(), warnings=()
    )


@pytest.mark.parametrize(
    "error",
    [
        docker.errors.APIError("listing refused"),
        requests.exceptions.ConnectionError("connection refused"),
    ],
)
def test_remove_left_cages_fails_as_cage_failed_when_the_listing_fails(
    error: Exception,
) -> None:
    """A failed listing raises CageFailedError, naming Docker's error."""
    client = MagicMock()
    client.containers.list.side_effect = error

    with pytest.raises(CageFailedError, match=re.escape(str(error))):
        runner.remove_left_cages(client)


def test_run_holds_each_root_open_until_the_cage_is_removed(
    tmp_path: Path, store: CredentialStore
) -> None:
    """The root's descriptor is open while the cage is removed and closed after."""
    work = tmp_path / "work"
    work.mkdir()
    grant = Grant(roots=[Root(path=str(work), mode="rw")], domains=[], credentials={})
    client = _client()
    at_removal: list[int] = []
    _container(client).remove.side_effect = lambda **_: at_removal.append(
        _open_descriptors()
    )
    before = _open_descriptors()

    _run(client, store, _cage_call(_echo(), {"text": "hi"}, grant=grant))

    assert at_removal == [before + 1]
    assert _open_descriptors() == before


def test_run_closes_the_held_roots_when_a_credential_is_refused(
    tmp_path: Path, store: CredentialStore
) -> None:
    """A credential refused after the roots are held leaves no descriptor open."""
    work = tmp_path / "work"
    work.mkdir()
    grant = Grant(
        roots=[Root(path=str(work), mode="rw")],
        domains=[],
        credentials={"api-token": EnvMount(env="API_TOKEN")},
    )
    before = _open_descriptors()

    with pytest.raises(CredentialError):
        _run(_client(), store, _cage_call(_echo(), {"text": "hi"}, grant=grant))

    assert _open_descriptors() == before


def test_run_refuses_a_root_the_verifier_found_swapped(
    store: CredentialStore, nonce: str
) -> None:
    """A root line under the call's nonce raises RootError with the reason."""
    err = f'{nonce} root "/work": is 1:2, want 3:4\nlater\n'.encode()
    client = _client(err=err, status=VERIFIER_REFUSED_EXIT)

    with pytest.raises(
        RootError, match="not the directory the runner checked"
    ) as excinfo:
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert str(excinfo.value).endswith('"/work": is 1:2, want 3:4')
    _container(client).remove.assert_called_once_with(force=True)


def test_run_refuses_an_action_the_verifier_could_not_start(
    store: CredentialStore, nonce: str
) -> None:
    """An exec line under the call's nonce raises CageStartError naming the image."""
    err = f"{nonce} exec no-such-program: not found\n".encode()
    client = _client(err=err, status=VERIFIER_REFUSED_EXIT)

    with pytest.raises(CageStartError, match=re.escape(repr(IMAGE))) as excinfo:
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert str(excinfo.value).endswith("no-such-program: not found")
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    "err",
    [f"{'0' * 32} root forged\n".encode(), b""],
    ids=["another-nonce", "no-line"],
)
def test_run_returns_an_exit_of_125_without_the_call_s_line_as_the_result(
    store: CredentialStore, nonce: str, err: bytes
) -> None:
    """Exit 125 with no verifier line under the call's nonce is the action's own."""
    client = _client(err=err, status=VERIFIER_REFUSED_EXIT)

    outcome = _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert outcome.exit_code == VERIFIER_REFUSED_EXIT
    assert outcome.content == {
        "exit_code": VERIFIER_REFUSED_EXIT,
        "stdout": "",
        "stderr": err.decode(),
    }
    _container(client).remove.assert_called_once_with(force=True)


def test_run_reads_the_verifier_line_across_chunks_to_its_newline(
    store: CredentialStore, nonce: str
) -> None:
    """A line that arrives in pieces is joined, and it ends at the first newline."""
    client = _client(status=VERIFIER_REFUSED_EXIT)
    pieces = [nonce.encode(), b" root split", b" reason\nlater\n"]
    _container(client).logs.side_effect = lambda **_: iter(pieces)

    with pytest.raises(RootError) as excinfo:
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert str(excinfo.value).endswith(": split reason")


def test_run_reads_at_most_4096_bytes_of_the_verifier_line(
    store: CredentialStore, nonce: str
) -> None:
    """The line is cut at 4096 bytes, and no chunk past them is read."""
    head = f"{nonce} root ".encode()
    pieces = [head + b"x" * (4096 - len(head)), b"z\n"]
    read: list[bytes] = []

    def logs(**_: object) -> Iterator[bytes]:
        for piece in pieces:
            read.append(piece)
            yield piece

    client = _client(status=VERIFIER_REFUSED_EXIT)
    _container(client).logs.side_effect = logs

    with pytest.raises(RootError) as excinfo:
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    assert read == pieces[:1]
    assert str(excinfo.value).endswith("x" * (4096 - len(head)))


def test_run_fails_the_cage_when_the_verifier_line_cannot_be_read(
    store: CredentialStore,
) -> None:
    """A logs failure while reading a 125's stderr raises CageFailedError."""
    client = _client(status=VERIFIER_REFUSED_EXIT)
    _container(client).logs.side_effect = docker.errors.APIError("logs failed")

    with pytest.raises(CageFailedError, match="logs failed"):
        _run(client, store, _cage_call(_echo(), {"text": "hi"}))

    _container(client).logs.assert_called_once_with(
        stdout=False, stderr=True, stream=True
    )
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    "call_id",
    ["", CALL_ID.upper(), CALL_ID[:-1], CALL_ID + "0", CALL_ID + "\n"],
    ids=["empty", "upper-case", "short", "long", "trailing-newline"],
)
def test_run_refuses_a_malformed_call_id_before_any_slot_or_cage(
    store: CredentialStore, call_id: str
) -> None:
    """A call ID that is not 32 lower-case hex digits refuses, naming it."""
    client = _client()
    limiter = MagicMock()

    with pytest.raises(InputError, match=re.escape(repr(call_id))):
        CageRunner(client, store, limiter).run(
            _cage_call(_echo(), {"text": "hi"}, call_id=call_id)
        )

    limiter.slot.assert_not_called()
    client.containers.create.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "make_grant", "error_class"),
    [
        ({"text": 5}, lambda _: _GRANT, InputError),
        ({"text": "-n"}, lambda _: _GRANT, InputError),
        ({"text": "hi"}, _symlinked_root_grant, RootError),
        ({"text": "hi"}, _missing_root_grant, RootError),
        ({"text": "hi"}, _unknown_env_credential_grant, CredentialError),
        ({"text": "hi"}, _unknown_file_credential_grant, CredentialError),
    ],
    ids=[
        "schema",
        "dash",
        "symlinked-root",
        "missing-root",
        "env-credential",
        "file-credential",
    ],
)
def test_run_refuses_before_it_takes_a_slot_or_creates_a_cage(
    tmp_path: Path,
    store: CredentialStore,
    arguments: dict[str, object],
    make_grant: Callable[[Path], Grant],
    error_class: type[RunnerError],
) -> None:
    """A refused input, root, or credential takes no slot and creates no cage."""
    client = _client()
    limiter = MagicMock()

    with pytest.raises(error_class):
        CageRunner(client, store, limiter).run(
            _cage_call(_echo(), arguments, grant=make_grant(tmp_path))
        )

    limiter.slot.assert_not_called()
    client.containers.create.assert_not_called()


def test_run_names_the_unset_env_credential_before_an_unset_file_one(
    store: CredentialStore,
) -> None:
    """With an env and a file credential both unset, the refusal names the env one."""
    grant = Grant(
        roots=[],
        domains=[],
        credentials={
            "deploy-key": FileMount(file="/run/credentials/deploy"),
            "api-token": EnvMount(env="API_TOKEN"),
        },
    )
    client = _client()
    limiter = MagicMock()

    with pytest.raises(CredentialError) as excinfo:
        CageRunner(client, store, limiter).run(
            _cage_call(_echo(), {"text": "hi"}, grant=grant)
        )

    assert str(excinfo.value) == unset_message("api-token")
    limiter.slot.assert_not_called()
    client.containers.create.assert_not_called()


def test_a_second_slot_waits_until_the_first_exits_when_the_limit_is_1() -> None:
    """At a limit of 1, a second cage enters only once the first leaves."""
    limiter = CageLimiter()
    second_entered = threading.Event()

    def take_second() -> None:
        with limiter.slot(1):
            second_entered.set()

    second = threading.Thread(target=take_second, daemon=True)
    with limiter.slot(1):
        second.start()
        assert not second_entered.wait(timeout=0.2)
    assert second_entered.wait(timeout=5)
    second.join(timeout=5)


def test_slot_lets_as_many_cages_run_as_the_limit() -> None:
    """At a limit of 2, a second cage enters while the first runs."""
    limiter = CageLimiter()

    with limiter.slot(2):
        assert _enters_slot(limiter, 2, within=5)


def test_slot_reads_the_limit_each_call_passes() -> None:
    """A call with a higher limit enters where a lower one was full."""
    limiter = CageLimiter()

    with limiter.slot(1):
        assert _enters_slot(limiter, 2, within=5)


def test_slot_counts_a_cage_out_when_its_body_raises() -> None:
    """A body that raises still frees its slot."""
    limiter = CageLimiter()

    with pytest.raises(RuntimeError), limiter.slot(1):
        raise RuntimeError

    assert _enters_slot(limiter, 1, within=5)
