import hashlib
import io
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agent_proxy.audit import AuditRecord, AuditWriter
from agent_proxy.auth import TokenStore
from agent_proxy.cli import main, run

PREFIX = "agent-proxy: "
TOKEN_HEADER = "NAME\tSCOPE\tCREATED\tLAST_USED\tEXPIRES"
CONFIG = {
    "agent_tree": "/srv/agents/alpha",
    "plugins": {},
    "scopes": {"ci": {"grants": {}}},
}


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
    for noun in ("token", "credential", "audit"):
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
