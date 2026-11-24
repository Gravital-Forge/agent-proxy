import contextlib
import hashlib
import io
import json
import shutil
import signal
import stat
import sys
import tempfile
import threading
from collections.abc import Callable, Collection, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from unittest import mock

import docker.errors
import pytest
import uvicorn
import yaml

from agent_proxy.audit import AuditRecord, AuditWriter
from agent_proxy.auth import TokenStore
from agent_proxy.cli import main, run
from agent_proxy.config import ConfigError, load_config
from agent_proxy.credentials import unset_message
from agent_proxy.mcp import CallRefusedError, Instance, _Server
from agent_proxy.reload import CONTROL_SOCKET_NAME, ControlServer, LiveConfig, Snapshot

PREFIX = "agent-proxy: "
TOKEN_HEADER = "NAME\tSCOPE\tCREATED\tLAST_USED\tEXPIRES"
CONFIG = {
    "agent_tree": "/srv/agents/alpha",
    "overflow_dir": "/srv/agents/alpha/overflow",
    "max_cages": 4,
    "plugins": {},
    "scopes": {"ci": {"grants": {}}},
}

MANIFEST_IMAGE = "example/echo:dev"
ECHO_ACTION = {
    "kind": "command",
    "input_schema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    },
    "credentials": [],
    "timeout": "30s",
    "argv": ["echo"],
    "parameters": [{"name": "message", "as": "positional"}],
    "passthrough_args": False,
}
RAW_ACTION = {
    "kind": "command",
    "input_schema": {
        "type": "object",
        "properties": {"args": {"type": "array", "items": {"type": "string"}}},
        "additionalProperties": False,
    },
    "credentials": [],
    "timeout": "30s",
    "argv": ["echo"],
    "parameters": [],
    "passthrough_args": True,
}
SERVE = ["serve", "--host", "127.0.0.1", "--port", "8765"]
HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both variables at a fresh state directory and a valid config."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    monkeypatch.setenv("AGENT_PROXY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AGENT_PROXY_CONFIG", str(config))
    return state_dir


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, value: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(value)))


def _mint(capsys: pytest.CaptureFixture[str], name: str = "ci-bot") -> str:
    """Mint a token for scope ci and return it, clearing the captured output."""
    assert main(["token", "mint", "--name", name, "--scope", "ci"]) == 0
    return capsys.readouterr().out.strip()


def _audit_record(call_id: str) -> AuditRecord:
    return AuditRecord(
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        call_id=call_id,
        token_name="ci-bot",
        scope="ci",
        action="git_status",
        arguments={"repo": "agent-proxy"},
        exit_code=0,
        duration_ms=42,
        stdout_bytes=128,
        stderr_bytes=0,
        truncated=False,
        refusal=None,
    )


def _write_audit(state_dir: Path, count: int) -> None:
    writer = AuditWriter(state_dir / "audit")
    for index in range(count):
        writer.write(_audit_record(f"call-{index:04d}"))
    writer.close()


def _assert_refusal(err: str) -> None:
    """Every stderr line carries the prefix, and no traceback leaks."""
    assert err
    assert all(line.startswith(PREFIX) for line in err.splitlines())
    assert "Traceback" not in err


def _docker_describing(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> mock.MagicMock:
    """Make docker.from_env return a client whose describe container exits 0."""
    client = mock.MagicMock()
    container = client.containers.create.return_value
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = stdout
    monkeypatch.setattr("agent_proxy.cli.docker.from_env", lambda: client)
    return client


class RecordingEgress:
    """Records each ensure and prune call with the lists it was given."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, set[tuple[str, ...]]]] = []

    def ensure(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists."""
        self.calls.append(("ensure", {tuple(domains) for domains in lists}))

    def prune(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists."""
        self.calls.append(("prune", {tuple(domains) for domains in lists}))


@pytest.fixture
def socket_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the state variable at a short directory; AF_UNIX paths cap at 107 bytes."""
    state_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    monkeypatch.setenv("AGENT_PROXY_STATE_DIR", str(state_dir))
    yield state_dir
    shutil.rmtree(state_dir)


def _snapshot(tmp_path: Path, warnings: tuple[str, ...] = ()) -> Snapshot:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return Snapshot(
        config=load_config(path),
        manifests=MappingProxyType({}),
        warnings=warnings,
    )


def _no_reports(scope_name: str | None) -> list[dict[str, object]]:
    return []


def _unused_build() -> Snapshot:
    raise AssertionError("a check test sends no reload")


@contextlib.contextmanager
def _control_server(
    state_dir: Path,
    tmp_path: Path,
    build: Callable[[], Snapshot],
    check: Callable[[str | None], list[dict[str, object]]] = _no_reports,
) -> Iterator[None]:
    """Serve reloads with build and checks with check on the state dir's socket."""
    live = LiveConfig(_snapshot(tmp_path), RecordingEgress())
    server = ControlServer(state_dir / CONTROL_SOCKET_NAME, live, build, check)
    server.start()
    try:
        yield
    finally:
        server.close()


class FakeInstance:
    """Stands in for a started Instance, recording its start and its close."""

    def __init__(self) -> None:
        """Hold a stand-in app; not started, not closed."""
        self.app = object()
        self.warnings: tuple[str, ...] = ()
        self.started_with: tuple[Path, Path, object] | None = None
        self.closed = False

    def close(self) -> None:
        """Record the close."""
        self.closed = True


@pytest.fixture
def fake_instance(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeInstance, mock.MagicMock]:
    """Make Instance.start record its arguments and return a FakeInstance."""
    fake = FakeInstance()
    client = mock.MagicMock()
    monkeypatch.setattr("agent_proxy.cli.docker.from_env", lambda: client)

    def start(
        state_dir: Path, config_path: Path, docker_client: object
    ) -> FakeInstance:
        fake.started_with = (state_dir, config_path, docker_client)
        return fake

    monkeypatch.setattr(Instance, "start", start)
    return fake, client


def _handlers() -> dict[int, object]:
    return {sig: signal.getsignal(sig) for sig in HANDLED_SIGNALS}


def test_token_mint_prints_the_token_alone(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mint prints one line, the token, and nothing else."""
    assert main(["token", "mint", "--name", "ci-bot", "--scope", "ci"]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("apx_")
    assert captured.out == lines[0] + "\n"
    assert captured.err == ""


def test_token_mint_under_an_unstored_credential_warns_and_prints_the_token(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mint exits 0 with the token on stdout and the warning on stderr."""
    grant = {
        "roots": [],
        "domains": [],
        "credentials": {"github-token": {"env": "GH_TOKEN"}},
    }
    document = CONFIG | {
        "plugins": {"github": "ghcr.io/example/github@sha256:" + "c" * 64},
        "scopes": {"ci": {"grants": {"github.pr_view": grant}}},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    assert main(["token", "mint", "--name", "ci-bot", "--scope", "ci"]) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("apx_")
    assert len(captured.out.splitlines()) == 1
    assert captured.err == f"{PREFIX}warning: {unset_message('github-token')}\n"


def test_token_mint_of_a_taken_name_exits_1_naming_it(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second mint of one name is refused, and stderr names it."""
    _mint(capsys)

    assert main(["token", "mint", "--name", "ci-bot", "--scope", "ci"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)
    assert "'ci-bot'" in captured.err


def test_token_mint_for_an_undefined_scope_exits_1_and_mints_nothing(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scope the config does not define is refused before any mint."""
    assert main(["token", "mint", "--name", "ci-bot", "--scope", "deploy"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)
    assert "'deploy'" in captured.err
    assert TokenStore(state / "tokens.sqlite3").records() == []


def test_token_mint_with_a_missing_config_file_exits_1_without_traceback(
    state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A config path naming no file is a one-line refusal."""
    monkeypatch.setenv("AGENT_PROXY_CONFIG", str(tmp_path / "absent.yaml"))

    assert main(["token", "mint", "--name", "ci-bot", "--scope", "ci"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)
    assert len(captured.err.splitlines()) == 1


def test_token_mint_with_an_unset_config_variable_exits_1(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mint needs AGENT_PROXY_CONFIG, and stderr names it."""
    monkeypatch.delenv("AGENT_PROXY_CONFIG")

    assert main(["token", "mint", "--name", "ci-bot", "--scope", "ci"]) == 1

    captured = capsys.readouterr()
    _assert_refusal(captured.err)
    assert "AGENT_PROXY_CONFIG" in captured.err


def test_token_mint_with_an_invalid_config_prints_one_line_per_error(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each entry of a ConfigError becomes its own prefixed stderr line."""
    invalid = CONFIG | {"extra_one": 1, "extra_two": 2}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(invalid), encoding="utf-8")

    assert main(["token", "mint", "--name", "ci-bot", "--scope", "ci"]) == 1

    captured = capsys.readouterr()
    _assert_refusal(captured.err)
    assert len(captured.err.splitlines()) == 2
    assert "extra_one" in captured.err
    assert "extra_two" in captured.err


@pytest.mark.parametrize(
    "source",
    ["agent_tree: [unclosed\n", "agent_tree: /srv/a\nagent_tree: /srv/b\n"],
    ids=["malformed", "duplicate-key"],
)
def test_token_mint_with_unparseable_yaml_prints_one_stderr_line(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    """A YAML parse refusal is a single prefixed stderr line."""
    (tmp_path / "config.yaml").write_text(source, encoding="utf-8")

    assert main(["token", "mint", "--name", "ci-bot", "--scope", "ci"]) == 1

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(PREFIX)


def test_token_mint_with_expires_records_an_expiry(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--expires sets the token's expiry."""
    assert (
        main(["token", "mint", "--name", "ci-bot", "--scope", "ci", "--expires", "1d"])
        == 0
    )
    capsys.readouterr()

    (record,) = TokenStore(state / "tokens.sqlite3").records()
    assert record.expires_at is not None


def test_token_mint_with_a_bad_duration_exits_1_naming_it(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A duration the parser refuses is a refusal naming the value."""
    argv = ["token", "mint", "--name", "ci-bot", "--scope", "ci", "--expires", "soon"]

    assert main(argv) == 1

    captured = capsys.readouterr()
    _assert_refusal(captured.err)
    assert "'soon'" in captured.err
    assert TokenStore(state / "tokens.sqlite3").records() == []


def test_token_list_prints_rows_without_secrets(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """List shows name, scope, and times, and neither the token nor its digest."""
    token = _mint(capsys)

    assert main(["token", "list"]) == 0

    out = capsys.readouterr().out
    header, row = out.splitlines()
    assert header == TOKEN_HEADER
    name, scope, created, last_used, expires = row.split("\t")
    assert (name, scope, last_used, expires) == ("ci-bot", "ci", "-", "-")
    assert datetime.fromisoformat(created).microsecond == 0
    assert "." not in created
    assert token not in out
    assert hashlib.sha256(token.encode()).hexdigest() not in out


def test_token_list_with_no_tokens_prints_the_header(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty store lists as the header alone."""
    assert main(["token", "list"]) == 0

    assert capsys.readouterr().out == TOKEN_HEADER + "\n"


def test_token_revoke_deletes_the_token(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Revoke prints its confirmation and the name leaves the store."""
    _mint(capsys)

    assert main(["token", "revoke", "ci-bot"]) == 0

    assert capsys.readouterr().out == "revoked ci-bot\n"
    assert TokenStore(state / "tokens.sqlite3").records() == []


def test_token_revoke_of_an_unknown_name_exits_1(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Revoking a name no token holds is a refusal naming it."""
    assert main(["token", "revoke", "nope"]) == 1

    captured = capsys.readouterr()
    _assert_refusal(captured.err)
    assert "'nope'" in captured.err


def test_credential_set_stores_stdin_bytes_owner_only(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Set stores stdin verbatim in a 0o600 file and reports the byte count."""
    value = b"s3cret\x00\xff\n"
    _feed_stdin(monkeypatch, value)

    assert main(["credential", "set", "api-key"]) == 0

    assert capsys.readouterr().out == f"stored api-key ({len(value)} bytes)\n"
    stored = state / "credentials" / "api-key"
    assert stored.read_bytes() == value
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_credential_set_with_a_value_argument_exits_2_and_stores_nothing(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value never travels as an argument."""
    _feed_stdin(monkeypatch, b"from-stdin")

    with pytest.raises(SystemExit) as excinfo:
        main(["credential", "set", "api-key", "hunter2"])

    assert excinfo.value.code == 2
    assert not (state / "credentials" / "api-key").exists()


def test_credential_set_with_empty_stdin_exits_1(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty value is a refusal."""
    _feed_stdin(monkeypatch, b"")

    assert main(["credential", "set", "api-key"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)


def test_credential_list_prints_a_header_and_one_row_per_entry(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """List shows each stored name with its set time, sorted by name."""
    for name in ("ssh-key", "api-key"):
        _feed_stdin(monkeypatch, b"value")
        assert main(["credential", "set", name]) == 0
    capsys.readouterr()

    assert main(["credential", "list"]) == 0

    header, *rows = capsys.readouterr().out.splitlines()
    assert header == "NAME\tSET_AT"
    assert [row.split("\t")[0] for row in rows] == ["api-key", "ssh-key"]
    for row in rows:
        set_at = row.split("\t")[1]
        assert datetime.fromisoformat(set_at).microsecond == 0
        assert "." not in set_at


def test_credential_rm_removes_the_credential(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rm prints its confirmation and deletes the file."""
    _feed_stdin(monkeypatch, b"value")
    assert main(["credential", "set", "api-key"]) == 0
    capsys.readouterr()

    assert main(["credential", "rm", "api-key"]) == 0

    assert capsys.readouterr().out == "removed api-key\n"
    assert not (state / "credentials" / "api-key").exists()


def test_credential_rm_of_an_unknown_name_exits_1(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removing a name no credential holds is a refusal naming it."""
    assert main(["credential", "rm", "nope"]) == 1

    captured = capsys.readouterr()
    _assert_refusal(captured.err)
    assert "'nope'" in captured.err


def test_audit_tail_prints_the_last_lines_written(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tail -n 2 prints the two newest records, oldest first."""
    _write_audit(state, 3)

    assert main(["audit", "tail", "-n", "2"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["call_id"] for line in lines] == [
        "call-0001",
        "call-0002",
    ]


def test_audit_tail_defaults_to_ten_lines(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without -n, tail prints ten lines."""
    _write_audit(state, 12)

    assert main(["audit", "tail"]) == 0

    assert len(capsys.readouterr().out.splitlines()) == 10


@pytest.mark.parametrize("count", ["0", "-1", "two"])
def test_audit_tail_refuses_a_count_that_is_not_positive(
    state: Path, count: str
) -> None:
    """COUNT must be a positive integer; anything else is a usage error."""
    with pytest.raises(SystemExit) as excinfo:
        main(["audit", "tail", "-n", count])

    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["token", "list"],
        ["credential", "list"],
        ["audit", "tail"],
    ],
)
def test_an_unset_state_dir_exits_1(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """Every command needs AGENT_PROXY_STATE_DIR, and stderr names it."""
    monkeypatch.delenv("AGENT_PROXY_STATE_DIR")

    assert main(argv) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)
    assert "AGENT_PROXY_STATE_DIR" in captured.err


def test_a_state_dir_that_does_not_exist_exits_1_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A state directory path naming nothing is refused, not created."""
    absent = tmp_path / "absent"
    monkeypatch.setenv("AGENT_PROXY_STATE_DIR", str(absent))

    assert main(["token", "list"]) == 1

    _assert_refusal(capsys.readouterr().err)
    assert not absent.exists()


def test_manifest_check_prints_warnings_and_the_pass_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pass prints each warning on stderr and one pass line on stdout."""
    manifest = {"actions": {"echo": ECHO_ACTION, "raw": RAW_ACTION}}
    _docker_describing(monkeypatch, json.dumps(manifest).encode())

    assert main(["manifest", "check", MANIFEST_IMAGE]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"manifest ok: {MANIFEST_IMAGE} declares 2 actions\n"
    assert captured.err == (
        "agent-proxy: warning: actions.raw: declares pass-through args\n"
    )


def test_manifest_check_prints_one_line_per_manifest_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each manifest failure is one prefixed stderr line, and the exit is 1."""
    broken = {key: value for key, value in ECHO_ACTION.items() if key != "credentials"}
    broken["timeout"] = "soon"
    _docker_describing(monkeypatch, json.dumps({"actions": {"echo": broken}}).encode())

    assert main(["manifest", "check", MANIFEST_IMAGE]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)
    assert len(captured.err.splitlines()) == 2
    assert "credentials" in captured.err
    assert "'soon'" in captured.err


def test_manifest_check_refuses_an_absent_image_without_pulling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absent image is refused, and manifest check never pulls."""
    client = _docker_describing(monkeypatch, b"")
    client.containers.create.side_effect = docker.errors.ImageNotFound("No such image")

    assert main(["manifest", "check", MANIFEST_IMAGE]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"agent-proxy: image {MANIFEST_IMAGE!r} is not present\n"
    client.images.pull.assert_not_called()


def test_manifest_check_exits_1_naming_docker_host_when_docker_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A daemon that cannot be reached is one refusal naming DOCKER_HOST."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")

    def unreachable() -> mock.MagicMock:
        raise docker.errors.DockerException("Error while fetching server API version")

    monkeypatch.setattr("agent_proxy.cli.docker.from_env", unreachable)

    assert main(["manifest", "check", MANIFEST_IMAGE]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)
    assert len(captured.err.splitlines()) == 1
    assert "DOCKER_HOST" in captured.err
    assert "unix:///nonexistent/docker.sock" in captured.err


def test_manifest_check_says_docker_host_is_unset_when_the_default_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With DOCKER_HOST unset, the refusal says so rather than printing None."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def unreachable() -> mock.MagicMock:
        raise docker.errors.DockerException("Error while fetching server API version")

    monkeypatch.setattr("agent_proxy.cli.docker.from_env", unreachable)

    assert main(["manifest", "check", MANIFEST_IMAGE]) == 1

    captured = capsys.readouterr()
    _assert_refusal(captured.err)
    assert "DOCKER_HOST is unset" in captured.err
    assert "None" not in captured.err


def test_reload_prints_the_warning_lines_and_reloaded(
    socket_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An accepted reload prints each warning on stderr and reloaded on stdout."""
    warnings = (
        "plugins.git.actions.raw: declares pass-through args",
        "plugins.fs.actions.raw: declares pass-through args",
    )
    new = _snapshot(tmp_path, warnings)

    with _control_server(socket_state, tmp_path, lambda: new):
        status = main(["reload"])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out == "reloaded\n"
    assert captured.err == "".join(f"{PREFIX}warning: {line}\n" for line in warnings)


def test_reload_refused_prints_one_refusal_line_per_error(
    socket_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reload the proxy refuses exits 1 with each error as a refusal line."""
    errors = ("config file 'a.yaml': first", "config file 'a.yaml': second")

    def refuse() -> Snapshot:
        raise ConfigError(*errors)

    with _control_server(socket_state, tmp_path, refuse):
        status = main(["reload"])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert captured.err == "".join(f"{PREFIX}{line}\n" for line in errors)


def test_reload_with_nothing_listening_exits_1_naming_the_socket(
    socket_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No control socket is one refusal line naming its path, with no traceback."""
    status = main(["reload"])

    err = capsys.readouterr().err
    assert status == 1
    _assert_refusal(err)
    assert len(err.splitlines()) == 1
    assert repr(str(socket_state / CONTROL_SOCKET_NAME)) in err


def _entry(check: str, subject: str, ok: bool, detail: str) -> dict[str, object]:
    return {"check": check, "subject": subject, "ok": ok, "detail": detail}


def _report(scope: str, *entries: dict[str, object]) -> dict[str, object]:
    return {
        "ok": all(entry["ok"] for entry in entries),
        "scope": scope,
        "checks": list(entries),
    }


class RecordingCheck:
    """Answers every check with fixed reports and records the scope it was asked."""

    def __init__(self, reports: list[dict[str, object]]) -> None:
        """Hold the reports to answer with; no request yet."""
        self.reports = reports
        self.asked: list[str | None] = []

    def __call__(self, scope_name: str | None) -> list[dict[str, object]]:
        """Record scope_name and return the reports."""
        self.asked.append(scope_name)
        return self.reports


def test_check_prints_a_header_and_one_row_per_entry_and_exits_0_when_all_pass(
    socket_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An all-pass reply is the header row, then each report's rows in reply order."""
    check = RecordingCheck(
        [
            _report(
                "alpha",
                _entry("credential", "github-token", True, "stored"),
                _entry("overflow_dir", "/srv/overflow", True, "writable"),
            ),
            _report("beta", _entry("overflow_dir", "/srv/overflow", True, "writable")),
        ]
    )

    with _control_server(socket_state, tmp_path, _unused_build, check):
        status = main(["check"])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out == (
        "SCOPE\tCHECK\tSUBJECT\tRESULT\tDETAIL\n"
        "alpha\tcredential\tgithub-token\tpass\tstored\n"
        "alpha\toverflow_dir\t/srv/overflow\tpass\twritable\n"
        "beta\toverflow_dir\t/srv/overflow\tpass\twritable\n"
    )
    assert captured.err == ""
    assert check.asked == [None]


def test_check_with_a_failed_entry_prints_its_row_and_the_count_and_exits_1(
    socket_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed entry prints fail in its row, then one failure line naming the count."""
    detail = unset_message("github-token")
    check = RecordingCheck(
        [
            _report(
                "alpha",
                _entry("credential", "github-token", False, detail),
                _entry("overflow_dir", "/srv/overflow", True, "writable"),
            ),
            _report("beta", _entry("root", "/srv/absent", False, "no such root")),
        ]
    )

    with _control_server(socket_state, tmp_path, _unused_build, check):
        status = main(["check"])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == (
        "SCOPE\tCHECK\tSUBJECT\tRESULT\tDETAIL\n"
        f"alpha\tcredential\tgithub-token\tfail\t{detail}\n"
        "alpha\toverflow_dir\t/srv/overflow\tpass\twritable\n"
        "beta\troot\t/srv/absent\tfail\tno such root\n"
    )
    assert captured.err == f"{PREFIX}2 of 3 checks failed\n"


def test_check_with_scope_asks_for_that_scope_alone(
    socket_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--scope NAME sends the scope's name to the serving process."""
    check = RecordingCheck(
        [_report("ci", _entry("overflow_dir", "/srv/overflow", True, "writable"))]
    )

    with _control_server(socket_state, tmp_path, _unused_build, check):
        status = main(["check", "--scope", "ci"])

    assert status == 0
    assert check.asked == ["ci"]
    assert capsys.readouterr().out.splitlines()[1].startswith("ci\t")


def test_check_refused_prints_the_refusal_line_and_no_rows(
    socket_state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A check the proxy refuses exits 1 with its error as a refusal line."""
    message = (
        "scope 'nope' is not defined in the config in force; defined scopes are ['ci']"
    )

    def refuse(scope_name: str | None) -> list[dict[str, object]]:
        raise CallRefusedError(message)

    with _control_server(socket_state, tmp_path, _unused_build, refuse):
        status = main(["check", "--scope", "nope"])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert captured.err == f"{PREFIX}{message}\n"


def test_check_with_nothing_listening_exits_1_naming_the_socket(
    socket_state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No control socket is one refusal line naming its path, with no traceback."""
    status = main(["check"])

    err = capsys.readouterr().err
    assert status == 1
    _assert_refusal(err)
    assert len(err.splitlines()) == 1
    assert repr(str(socket_state / CONTROL_SOCKET_NAME)) in err


def test_check_help_names_what_it_checks(capsys: pytest.CaptureFixture[str]) -> None:
    """The top-level help describes the check command."""
    with pytest.raises(SystemExit):
        main(["--help"])

    assert (
        "check the running proxy's credentials, roots, overflow directory,"
        " egress, and plugins" in " ".join(capsys.readouterr().out.split())
    )


@pytest.mark.parametrize("variable", ["AGENT_PROXY_STATE_DIR", "AGENT_PROXY_CONFIG"])
def test_serve_with_a_missing_setting_exits_1_naming_it(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
) -> None:
    """Serve needs the state directory and the config; stderr names the one unset."""
    monkeypatch.delenv(variable)

    assert main(SERVE) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    _assert_refusal(captured.err)
    assert variable in captured.err


def test_serve_with_docker_unreachable_exits_1_naming_docker_host(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A daemon that cannot be reached is one refusal naming DOCKER_HOST."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")

    def unreachable() -> mock.MagicMock:
        raise docker.errors.DockerException("Error while fetching server API version")

    monkeypatch.setattr("agent_proxy.cli.docker.from_env", unreachable)

    assert main(SERVE) == 1

    err = capsys.readouterr().err
    _assert_refusal(err)
    assert "DOCKER_HOST" in err


@pytest.mark.parametrize("port", ["0", "65536", "http"])
def test_serve_with_a_port_outside_1_to_65535_exits_2(
    port: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A port that is not an integer from 1 to 65535 is a usage error naming it."""
    with pytest.raises(SystemExit) as excinfo:
        main(["serve", "--host", "127.0.0.1", "--port", port])

    assert excinfo.value.code == 2
    assert repr(port) in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--port", "8765"],
        ["serve", "--host", "127.0.0.1"],
    ],
    ids=["no-host", "no-port"],
)
def test_serve_without_host_or_port_exits_2(argv: list[str]) -> None:
    """Both --host and --port are required."""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    assert excinfo.value.code == 2


def test_serve_help_names_host_and_port(capsys: pytest.CaptureFixture[str]) -> None:
    """The serve help exits 0 and names both options."""
    with pytest.raises(SystemExit) as excinfo:
        main(["serve", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out
    assert "--port" in out


def test_serve_refuses_a_start_that_raises_a_proxy_error(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused start prints one refusal line per error and exits 1."""
    monkeypatch.setattr("agent_proxy.cli.docker.from_env", mock.MagicMock)
    errors = ("config file 'a.yaml': first", "config file 'a.yaml': second")

    def start(state_dir: Path, config_path: Path, client: object) -> FakeInstance:
        raise ConfigError(*errors)

    monkeypatch.setattr(Instance, "start", start)

    assert main(SERVE) == 1

    assert capsys.readouterr().err == "".join(f"{PREFIX}{line}\n" for line in errors)


def test_serve_runs_the_app_on_host_and_port_then_closes_the_instance(
    state: Path,
    fake_instance: tuple[FakeInstance, mock.MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serve starts the instance, runs its app on host and port, then closes it."""
    instance, client = fake_instance
    runs: list[tuple[str, int, object, bool]] = []

    def run(self: _Server, sockets: object = None) -> None:
        runs.append(
            (self.config.host, self.config.port, self.config.app, instance.closed)
        )

    monkeypatch.setattr(_Server, "run", run)

    assert main(SERVE) == 0

    assert instance.started_with == (state, state.parent / "config.yaml", client)
    assert runs == [("127.0.0.1", 8765, instance.app, False)]
    assert instance.closed is True


def test_serve_prints_the_start_warnings_before_the_server_runs(
    fake_instance: tuple[FakeInstance, mock.MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each of the instance's warnings is on stderr by the time the server runs."""
    instance, _ = fake_instance
    instance.warnings = (
        "plugins.git.actions.raw: declares pass-through args",
        unset_message("github-token"),
    )
    err_at_run: list[str] = []

    def run(self: _Server, sockets: object = None) -> None:
        err_at_run.append(capsys.readouterr().err)

    monkeypatch.setattr(_Server, "run", run)

    assert main(SERVE) == 0

    assert err_at_run == [
        "".join(f"{PREFIX}warning: {line}\n" for line in instance.warnings)
    ]


def test_serve_closes_the_instance_when_run_raises(
    fake_instance: tuple[FakeInstance, mock.MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that fails still closes the instance on the way out."""
    instance, _ = fake_instance

    def run(self: _Server, sockets: object = None) -> None:
        raise RuntimeError("the server failed")

    monkeypatch.setattr(_Server, "run", run)

    with pytest.raises(RuntimeError, match="the server failed"):
        main(SERVE)

    assert instance.closed is True


@pytest.mark.parametrize("sig", HANDLED_SIGNALS, ids=["SIGINT", "SIGTERM"])
def test_capture_signals_turns_a_signal_into_should_exit_and_restores_handlers(
    sig: signal.Signals,
) -> None:
    """On the main thread a caught signal sets should_exit, and none is re-raised.

    Reaching the last assertion shows the context raised no signal on exit.
    """
    before = _handlers()
    server = _Server(uvicorn.Config(object(), host="127.0.0.1", port=8765))

    with server.capture_signals():
        signal.raise_signal(sig)
        assert server.should_exit is True

    assert _handlers() == before


def test_capture_signals_off_the_main_thread_installs_no_handler() -> None:
    """Off the main thread, capture_signals leaves every handler as it was."""
    before = _handlers()
    server = _Server(uvicorn.Config(object(), host="127.0.0.1", port=8765))
    seen: list[dict[int, object]] = []
    failures: list[ValueError] = []

    def enter() -> None:
        try:
            with server.capture_signals():
                seen.append(_handlers())
        # signal.signal raises ValueError off the main thread.
        except ValueError as exc:
            failures.append(exc)

    thread = threading.Thread(target=enter)
    thread.start()
    thread.join()

    assert failures == []
    assert seen == [before]
    assert _handlers() == before


def test_serve_returns_0_and_closes_the_instance_after_a_sigterm(
    fake_instance: tuple[FakeInstance, mock.MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGTERM while serving ends the run; serve closes the instance and exits 0."""
    instance, _ = fake_instance

    def run(self: _Server, sockets: object = None) -> None:
        with self.capture_signals():
            signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(_Server, "run", run)

    assert main(SERVE) == 0
    assert instance.closed is True


def test_no_command_is_a_usage_error() -> None:
    """A bare invocation exits 2 through argparse."""
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2


def test_help_lists_the_command_groups(capsys: pytest.CaptureFixture[str]) -> None:
    """--help exits 0 and names every noun."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    nouns = ("token", "credential", "audit", "manifest", "reload", "check", "serve")
    for noun in nouns:
        assert noun in out


def test_run_exits_with_the_status_main_returns(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The console entry point reads sys.argv and exits with main's status."""
    monkeypatch.setattr(sys, "argv", ["agent-proxy", "token", "revoke", "nope"])

    with pytest.raises(SystemExit) as excinfo:
        run()

    assert excinfo.value.code == 1
    assert "'nope'" in capsys.readouterr().err
