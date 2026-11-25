import errno
import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
from audit_faults import fail_next_write_after_half
from egress_fakes import RecordingEgress

from agent_proxy.audit import (
    ACTION_CAP_CHARACTERS,
    REFUSAL_CAP_CHARACTERS,
    AuditError,
    AuditRecord,
    AuditWriter,
    tail,
)
from agent_proxy.auth import TokenStore
from agent_proxy.config import InstanceConfig
from agent_proxy.credentials import unset_message
from agent_proxy.egress import cage_network
from agent_proxy.errors import ProxyError
from agent_proxy.mcp import (
    REFUSAL_PREFIX,
    TOKENLESS_LINE_INTERVAL,
    CallGateway,
    CallRefusedError,
    ToolSpec,
)
from agent_proxy.plugins import parse_manifest
from agent_proxy.reload import LiveConfig, Snapshot
from agent_proxy.results import CALL_ID_PATTERN, OverflowWriteError
from agent_proxy.runner import (
    CageCall,
    CageFailedError,
    CageStartError,
    CageTimeoutError,
    CredentialError,
    InputError,
    Outcome,
)
from agent_proxy.selfcheck import (
    SELF_CHECK_INPUT_SCHEMA,
    SELF_CHECK_TOOL,
    CheckEntry,
    CheckReport,
)

MINTED_AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
LATER = MINTED_AT + timedelta(minutes=5)
AGENT_TREE = "/srv/agents/alpha"
OVERFLOW_DIR = f"{AGENT_TREE}/overflow"
MAX_CAGES = 3
TOOLS_IMAGE = "ghcr.io/example/tools@sha256:" + "a" * 64
OTHER_IMAGE = "ghcr.io/example/other@sha256:" + "b" * 64
UNKNOWN_TOKEN = "apx_" + "x" * 43
ECHO_ARGUMENTS = {"text": "hello"}
TOKEN_REFUSAL = "the bearer token is missing, unknown, revoked, or expired"
TOKEN_STORE_REFUSAL = "the token store could not be read"
AUDIT_REFUSAL = re.compile(
    r"the audit log could not record call [0-9a-f]{32}, so its result is withheld"
)
NOT_SQLITE = b"this file is not a SQLite database\n" * 8

TOOLS_MANIFEST = parse_manifest(
    json.dumps(
        {
            "actions": {
                "echo": {
                    "kind": "command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    "credentials": [],
                    "timeout": "10s",
                    "argv": ["echo"],
                    "parameters": [{"name": "text", "as": "positional"}],
                    "passthrough_args": False,
                },
                "count_bytes": {
                    "kind": "script",
                    "input_schema": {"type": "object"},
                    "credentials": [],
                    "timeout": "10s",
                    "executable": "/plugin/count-bytes",
                    "output_schema": {"type": "object"},
                },
            }
        }
    ).encode()
)


def _grant(domains: list[str]) -> dict[str, object]:
    return {"roots": [], "domains": domains, "credentials": {}}


CONFIG = InstanceConfig.model_validate(
    {
        "agent_tree": AGENT_TREE,
        "overflow_dir": OVERFLOW_DIR,
        "max_cages": MAX_CAGES,
        "plugins": {"tools": TOOLS_IMAGE, "other": OTHER_IMAGE},
        "scopes": {
            "dev": {
                "grants": {
                    "tools.echo": _grant(["example.com"]),
                    "tools.count_bytes": _grant([]),
                }
            },
            "ops": {"grants": {"other.echo": _grant([])}},
        },
    }
)
SNAPSHOT = Snapshot(
    config=CONFIG,
    manifests=MappingProxyType({"tools": TOOLS_MANIFEST, "other": TOOLS_MANIFEST}),
    warnings=(),
)
ECHO_OUTCOME = Outcome(
    content={"exit_code": 0, "stdout": {"text": "hello\n"}},
    exit_code=0,
    duration_ms=42,
    stdout_bytes=6,
    stderr_bytes=0,
    truncated=False,
)
FAILED_SCRIPT = Outcome(
    content={"exit_code": 3, "stderr": {"text": "boom\n"}},
    exit_code=3,
    duration_ms=7,
    stdout_bytes=0,
    stderr_bytes=5,
    truncated=False,
)
LATCH_RECORD = AuditRecord(
    timestamp=MINTED_AT,
    call_id="0" * 32,
    token_name=None,
    scope=None,
    action="tools_echo",
    arguments={},
    exit_code=None,
    duration_ms=0,
    stdout_bytes=None,
    stderr_bytes=None,
    truncated=False,
    refusal="latch",
)
CHECKS = (
    CheckEntry(
        check="credential",
        subject="api-token",
        ok=False,
        detail=unset_message("api-token"),
    ),
    CheckEntry(
        check="overflow_dir",
        subject=OVERFLOW_DIR,
        ok=True,
        detail="the instance can create a file in it",
    ),
)
SELF_CHECK_SPEC = ToolSpec(SELF_CHECK_TOOL, SELF_CHECK_INPUT_SCHEMA)


class Clock:
    """A settable clock the store and the gateway read."""

    def __init__(self, moment: datetime) -> None:
        """Start the clock at moment."""
        self.moment = moment

    def __call__(self) -> datetime:
        """Return the current moment."""
        return self.moment


class FakeRunner:
    """A cage runner that records each call and the use held while it runs."""

    def __init__(self, live: LiveConfig) -> None:
        """Return ECHO_OUTCOME until result is changed."""
        self.result: Outcome | ProxyError = ECHO_OUTCOME
        self.calls: list[CageCall] = []
        self.held: list[tuple[Snapshot, int]] = []
        self._live = live

    def run(self, call: CageCall) -> Outcome:
        """Record call and the snapshot in use, then return or raise result."""
        self.calls.append(call)
        current = self._live.current
        self.held.append((current, self._live._uses[id(current)].count))
        if isinstance(self.result, ProxyError):
            raise self.result
        return self.result


class FakeSelfCheck:
    """A self-check that records each run and the use held while it runs."""

    def __init__(self, live: LiveConfig) -> None:
        """Report CHECKS for every scope it runs."""
        self.runs: list[tuple[Snapshot, str, int]] = []
        self._live = live

    def run(self, snapshot: Snapshot, scope_name: str) -> CheckReport:
        """Record the run and the snapshot's use count, then return a report."""
        self.runs.append((snapshot, scope_name, self._live._uses[id(snapshot)].count))
        return CheckReport(scope=scope_name, checks=CHECKS)


@dataclass
class Harness:
    gateway: CallGateway
    runner: FakeRunner
    self_check: FakeSelfCheck
    store: TokenStore
    clock: Clock
    live: LiveConfig
    audit: AuditWriter
    audit_dir: Path
    tokens_path: Path
    token: str


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Return a gateway over real stores in tmp_path, a dev token, and a fake runner."""
    clock = Clock(MINTED_AT)
    tokens_path = tmp_path / "tokens.db"
    store = TokenStore(tokens_path, now=clock)
    audit_dir = tmp_path / "audit"
    audit = AuditWriter(audit_dir)
    live = LiveConfig(SNAPSHOT, RecordingEgress())
    runner = FakeRunner(live)
    self_check = FakeSelfCheck(live)
    return Harness(
        gateway=CallGateway(store, live, runner, self_check, audit, clock),
        runner=runner,
        self_check=self_check,
        store=store,
        clock=clock,
        live=live,
        audit=audit,
        audit_dir=audit_dir,
        tokens_path=tokens_path,
        token=store.mint("ci-bot", "dev", None),
    )


def _bearer(token: str) -> str:
    return f"Bearer {token}"


def _audit_lines(harness: Harness) -> list[dict[str, object]]:
    return [json.loads(line) for line in tail(harness.audit_dir, 1000)]


def _last_used(harness: Harness) -> datetime | None:
    [record] = [r for r in harness.store.records() if r.name == "ci-bot"]
    return record.last_used_at


def _refusal(
    harness: Harness,
    authorization: str | None,
    tool: str = "tools_echo",
) -> str:
    with pytest.raises(CallRefusedError) as caught:
        harness.gateway.call(authorization, tool, ECHO_ARGUMENTS)
    return str(caught.value)


def _latch(audit: AuditWriter, monkeypatch: pytest.MonkeyPatch) -> None:
    """Latch audit by failing one write and its rollback, then restore os."""

    def fail(*args: object) -> int:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    with monkeypatch.context() as patch:
        patch.setattr("agent_proxy.audit.os.write", fail)
        patch.setattr("agent_proxy.audit.os.ftruncate", fail)
        with pytest.raises(AuditError):
            audit.write(LATCH_RECORD)
    assert audit.available is False


def _missing(harness: Harness) -> str | None:
    return None


def _not_bearer(harness: Harness) -> str | None:
    return f"Basic {harness.token}"


def _two_spaces(harness: Harness) -> str | None:
    return f"Bearer  {harness.token}"


def _unknown(harness: Harness) -> str | None:
    return _bearer(UNKNOWN_TOKEN)


def _revoked(harness: Harness) -> str | None:
    harness.store.revoke("ci-bot")
    return _bearer(harness.token)


def _expired(harness: Harness) -> str | None:
    token = harness.store.mint("short-bot", "dev", timedelta(minutes=1))
    harness.clock.moment = MINTED_AT + timedelta(minutes=2)
    return _bearer(token)


BAD_AUTHORIZATIONS = [
    pytest.param(_missing, id="missing"),
    pytest.param(_not_bearer, id="not-bearer"),
    pytest.param(_two_spaces, id="two-spaces"),
    pytest.param(_unknown, id="unknown"),
    pytest.param(_revoked, id="revoked"),
    pytest.param(_expired, id="expired"),
]
MakeAuthorization = Callable[[Harness], str | None]


def test_the_refusal_prefix_is_fixed() -> None:
    """A client sees each refusal after this prefix."""
    assert REFUSAL_PREFIX == "agent-proxy refused: "


def test_a_refusal_is_a_proxy_error() -> None:
    """CallRefusedError subclasses ProxyError."""
    assert issubclass(CallRefusedError, ProxyError)


def test_the_listing_holds_the_scopes_grants_sorted_with_their_schemas(
    harness: Harness,
) -> None:
    """Each grant lists as plugin_action with its schema, beside the self-check."""
    listed = harness.gateway.list_tools(_bearer(harness.token))

    assert listed == [
        SELF_CHECK_SPEC,
        ToolSpec(
            "tools_count_bytes", TOOLS_MANIFEST.actions["count_bytes"].input_schema
        ),
        ToolSpec("tools_echo", TOOLS_MANIFEST.actions["echo"].input_schema),
    ]


@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "bEaReR"])
def test_the_bearer_scheme_is_matched_case_insensitively(
    harness: Harness, scheme: str
) -> None:
    """The scheme's case does not matter."""
    listed = harness.gateway.list_tools(f"{scheme} {harness.token}")

    assert [spec.name for spec in listed] == [
        SELF_CHECK_TOOL,
        "tools_count_bytes",
        "tools_echo",
    ]


@pytest.mark.parametrize("make_authorization", BAD_AUTHORIZATIONS)
def test_the_listing_is_empty_for_a_token_that_does_not_authenticate(
    harness: Harness, make_authorization: MakeAuthorization
) -> None:
    """A missing, malformed, unknown, revoked, or expired token lists nothing."""
    assert harness.gateway.list_tools(make_authorization(harness)) == []


def test_the_listing_is_the_self_check_alone_for_a_scope_the_config_does_not_define(
    harness: Harness,
) -> None:
    """A token whose scope the snapshot lacks lists the self-check tool alone."""
    token = harness.store.mint("ghost-bot", "ghost", None)

    assert harness.gateway.list_tools(_bearer(token)) == [SELF_CHECK_SPEC]


def test_the_listing_is_empty_when_the_token_store_cannot_be_read(
    harness: Harness,
) -> None:
    """A store file overwritten with bytes that are not SQLite lists nothing."""
    harness.tokens_path.write_bytes(NOT_SQLITE)

    assert harness.gateway.list_tools(_bearer(harness.token)) == []


def test_the_listing_leaves_last_used_at_unset(harness: Harness) -> None:
    """Listing verifies the token and never records its use."""
    harness.gateway.list_tools(_bearer(harness.token))

    assert _last_used(harness) is None


def test_the_listing_writes_no_audit_line(harness: Harness) -> None:
    """Only a call writes an audit line."""
    harness.gateway.list_tools(_bearer(harness.token))

    assert _audit_lines(harness) == []


@pytest.mark.parametrize("make_authorization", BAD_AUTHORIZATIONS)
def test_a_token_that_does_not_authenticate_is_refused(
    harness: Harness, make_authorization: MakeAuthorization
) -> None:
    """Every token that does not authenticate gets the one token refusal."""
    assert _refusal(harness, make_authorization(harness)) == TOKEN_REFUSAL
    assert harness.runner.calls == []


def test_a_token_store_that_cannot_be_read_is_refused(harness: Harness) -> None:
    """The refusal is fixed and never names the store's file."""
    harness.tokens_path.write_bytes(NOT_SQLITE)

    assert _refusal(harness, _bearer(harness.token)) == TOKEN_STORE_REFUSAL
    assert harness.runner.calls == []


def test_a_scope_the_config_does_not_define_is_refused(harness: Harness) -> None:
    """The refusal names the scope."""
    token = harness.store.mint("ghost-bot", "ghost", None)

    assert _refusal(harness, _bearer(token)) == (
        "scope 'ghost' is not defined in the config in force"
    )
    assert harness.runner.calls == []


@pytest.mark.parametrize(
    "tool",
    [
        pytest.param("echo", id="no-underscore"),
        pytest.param("ghost_echo", id="unknown-plugin"),
        pytest.param("tools_nope", id="undeclared-action"),
        pytest.param("other_echo", id="not-granted"),
    ],
)
def test_a_tool_that_names_no_grant_of_the_scope_is_refused(
    harness: Harness, tool: str
) -> None:
    """The refusal names the tool and the scope."""
    assert _refusal(harness, _bearer(harness.token), tool) == (
        f"tool {tool!r} is not granted to scope 'dev'"
    )
    assert harness.runner.calls == []


@pytest.mark.parametrize(
    "error",
    [
        CageTimeoutError(
            "action 'echo' did not finish within its timeout of 10 seconds"
        ),
        InputError("$.text: 3 is not of type 'string'"),
    ],
)
def test_a_runner_refusal_surfaces_its_own_text(
    harness: Harness, error: ProxyError
) -> None:
    """Any other ProxyError is refused with its own text."""
    harness.runner.result = error

    assert _refusal(harness, _bearer(harness.token)) == str(error)


def test_a_granted_call_returns_the_outcomes_content(harness: Harness) -> None:
    """The reply is the outcome's content."""
    reply = harness.gateway.call(_bearer(harness.token), "tools_echo", ECHO_ARGUMENTS)

    assert reply == ECHO_OUTCOME.content


def test_a_granted_call_gives_the_runner_its_cage_call(harness: Harness) -> None:
    """The cage call carries the ID, image, action, grant, network, and limits."""
    harness.gateway.call(_bearer(harness.token), "tools_echo", ECHO_ARGUMENTS)

    [call] = harness.runner.calls
    assert re.fullmatch(CALL_ID_PATTERN, call.call_id)
    assert call.image == TOOLS_IMAGE
    assert call.action == TOOLS_MANIFEST.actions["echo"]
    assert call.grant == CONFIG.scopes["dev"].grants["tools.echo"]
    assert call.arguments == ECHO_ARGUMENTS
    assert call.network == cage_network(["example.com"])
    assert call.overflow_dir == OVERFLOW_DIR
    assert call.max_cages == MAX_CAGES


def test_a_tool_name_splits_at_its_first_underscore(harness: Harness) -> None:
    """tools_count_bytes is plugin tools and action count_bytes."""
    harness.gateway.call(_bearer(harness.token), "tools_count_bytes", {})

    [call] = harness.runner.calls
    assert call.action == TOOLS_MANIFEST.actions["count_bytes"]
    assert call.network == cage_network([])


def test_a_granted_call_sets_last_used_at(harness: Harness) -> None:
    """The call records the token's use at the clock's time."""
    harness.clock.moment = LATER

    harness.gateway.call(_bearer(harness.token), "tools_echo", ECHO_ARGUMENTS)

    assert _last_used(harness) == LATER


def test_a_refused_call_still_sets_last_used_at(harness: Harness) -> None:
    """Every call authenticates, a refused call included."""
    harness.clock.moment = LATER

    _refusal(harness, _bearer(harness.token), "other_echo")

    assert _last_used(harness) == LATER


def test_a_failed_script_returns_its_content_as_a_normal_reply(
    harness: Harness,
) -> None:
    """A failed script's reply is its command-shaped content, not a refusal."""
    harness.runner.result = FAILED_SCRIPT

    reply = harness.gateway.call(_bearer(harness.token), "tools_count_bytes", {})

    assert reply == FAILED_SCRIPT.content


def test_the_runner_runs_while_the_snapshot_is_in_use(harness: Harness) -> None:
    """The call holds live.use() around run and releases it afterwards."""
    harness.gateway.call(_bearer(harness.token), "tools_echo", ECHO_ARGUMENTS)

    [(snapshot, count)] = harness.runner.held
    assert snapshot is SNAPSHOT
    assert count == 1
    assert harness.live._uses == {}


def test_every_call_writes_one_audit_line(harness: Harness) -> None:
    """Granted and refused calls each write exactly one line."""
    harness.gateway.call(_bearer(harness.token), "tools_echo", ECHO_ARGUMENTS)
    _refusal(harness, _bearer(harness.token), "other_echo")
    _refusal(harness, _bearer(UNKNOWN_TOKEN))

    assert len(_audit_lines(harness)) == 3


def test_a_granted_calls_audit_line(harness: Harness) -> None:
    """The line holds the call's start, token, tool, arguments, and outcome."""
    harness.clock.moment = LATER

    harness.gateway.call(_bearer(harness.token), "tools_echo", ECHO_ARGUMENTS)

    assert _audit_lines(harness) == [
        {
            "timestamp": LATER.isoformat(),
            "call_id": harness.runner.calls[0].call_id,
            "token_name": "ci-bot",
            "scope": "dev",
            "action": "tools_echo",
            "arguments": ECHO_ARGUMENTS,
            "arguments_cut": None,
            "exit_code": 0,
            "duration_ms": 42,
            "stdout_bytes": 6,
            "stderr_bytes": 0,
            "truncated": False,
            "refusal": None,
            "credential": None,
        }
    ]


def test_a_bad_tokens_audit_line_has_no_token_fields_and_no_arguments(
    harness: Harness,
) -> None:
    """A token that does not authenticate leaves the token and argument fields None."""
    _refusal(harness, _bearer(UNKNOWN_TOKEN))

    [line] = _audit_lines(harness)
    duration_ms = line.pop("duration_ms")
    call_id = line.pop("call_id")
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0
    assert isinstance(call_id, str)
    assert re.fullmatch(CALL_ID_PATTERN, call_id)
    assert line == {
        "timestamp": MINTED_AT.isoformat(),
        "token_name": None,
        "scope": None,
        "action": "tools_echo",
        "arguments": None,
        "arguments_cut": None,
        "exit_code": None,
        "stdout_bytes": None,
        "stderr_bytes": None,
        "truncated": False,
        "refusal": TOKEN_REFUSAL,
        "credential": None,
    }


def test_a_refusal_after_authentication_names_the_token_in_its_line(
    harness: Harness,
) -> None:
    """A refusal past authentication records the token and the message."""
    _refusal(harness, _bearer(harness.token), "other_echo")

    [line] = _audit_lines(harness)
    assert line["token_name"] == "ci-bot"
    assert line["scope"] == "dev"
    assert line["exit_code"] is None
    assert line["refusal"] == "tool 'other_echo' is not granted to scope 'dev'"


def test_a_runner_refusals_line_carries_the_cage_calls_id(harness: Harness) -> None:
    """The runner, the refusal, and the line share one call ID."""
    error = CageTimeoutError("action 'echo' did not finish within its timeout")
    harness.runner.result = error

    _refusal(harness, _bearer(harness.token))

    [line] = _audit_lines(harness)
    assert line["call_id"] == harness.runner.calls[0].call_id
    assert line["refusal"] == str(error)
    assert line["exit_code"] is None
    assert line["stdout_bytes"] is None
    assert line["stderr_bytes"] is None
    assert line["truncated"] is False


def test_an_unset_credential_is_refused_by_name_and_audited(
    harness: Harness,
) -> None:
    """The client and the one audit line both get the unset-credential text."""
    harness.runner.result = CredentialError(unset_message("api-token"))

    assert _refusal(harness, _bearer(harness.token)) == unset_message("api-token")

    [line] = _audit_lines(harness)
    assert line["refusal"] == unset_message("api-token")


def test_a_failed_audit_write_withholds_the_result_and_the_next_call_succeeds(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failed call is refused and absent from the log; the next one lands."""
    fail_next_write_after_half(monkeypatch)
    bearer = _bearer(harness.token)

    with pytest.raises(CallRefusedError) as caught:
        harness.gateway.call(bearer, "tools_echo", ECHO_ARGUMENTS)
    first_id = harness.runner.calls[0].call_id
    assert str(caught.value) == (
        "the action ran and may have taken effect, but the audit log could not"
        f" record call {first_id}, so its result is withheld; check what it"
        " changed before a retry"
    )

    reply = harness.gateway.call(bearer, "tools_echo", ECHO_ARGUMENTS)

    assert reply == ECHO_OUTCOME.content
    second_id = harness.runner.calls[1].call_id
    log = (harness.audit_dir / "audit.log").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in log.splitlines()]
    assert [line["call_id"] for line in lines] == [second_id]
    assert first_id not in log


def test_a_failed_audit_write_replaces_a_refusal(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused call whose line fails gets the audit refusal instead."""
    fail_next_write_after_half(monkeypatch)

    assert AUDIT_REFUSAL.fullmatch(_refusal(harness, _bearer(UNKNOWN_TOKEN)))


def test_a_failed_audit_write_replaces_a_refusal_past_authentication(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call refused before any cage ran keeps the plain audit refusal."""
    fail_next_write_after_half(monkeypatch)

    assert AUDIT_REFUSAL.fullmatch(
        _refusal(harness, _bearer(harness.token), "other_echo")
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(InputError("$.text: 3 is not of type 'string'"), id="input"),
        pytest.param(CredentialError(unset_message("api-token")), id="credential"),
        pytest.param(CageStartError("the cage cannot be started"), id="start"),
    ],
)
def test_a_failed_audit_write_of_a_refusal_before_the_cage_ran_is_plain(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, error: ProxyError
) -> None:
    """A runner refusal that comes before the action runs keeps the plain text."""
    harness.runner.result = error
    fail_next_write_after_half(monkeypatch)

    assert AUDIT_REFUSAL.fullmatch(_refusal(harness, _bearer(harness.token)))


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            CageTimeoutError("action 'echo' did not finish within its timeout"),
            id="timeout",
        ),
        pytest.param(CageFailedError("action 'echo': the cage failed"), id="failed"),
        pytest.param(
            OverflowWriteError("the overflow file cannot be written"), id="overflow"
        ),
    ],
)
def test_a_failed_audit_write_of_a_refusal_after_the_cage_ran_says_it_ran(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, error: ProxyError
) -> None:
    """A cage that started and then failed may have acted, and the refusal says so."""
    harness.runner.result = error
    fail_next_write_after_half(monkeypatch)

    refusal = _refusal(harness, _bearer(harness.token))

    call_id = harness.runner.calls[0].call_id
    assert refusal == (
        "the action ran and may have taken effect, but the audit log could not"
        f" record call {call_id}, so its result is withheld; check what it"
        " changed before a retry"
    )


def test_a_long_refusal_reaches_the_caller_whole_and_the_line_cut(
    harness: Harness,
) -> None:
    """The cap applies to the line alone; the caller gets every character."""
    error = InputError("$.text: " + "x" * (REFUSAL_CAP_CHARACTERS + 100))
    harness.runner.result = error

    assert _refusal(harness, _bearer(harness.token)) == str(error)

    [line] = _audit_lines(harness)
    assert line["refusal"] == str(error)[:REFUSAL_CAP_CHARACTERS] + " [cut]"


def test_a_long_tool_name_is_cut_in_its_line(harness: Harness) -> None:
    """A tool name the caller chose is cut to its cap in the line."""
    tool = "tools_" + "z" * ACTION_CAP_CHARACTERS

    _refusal(harness, _bearer(harness.token), tool)

    [line] = _audit_lines(harness)
    assert line["action"] == tool[:ACTION_CAP_CHARACTERS] + " [cut]"


def test_the_tokenless_line_interval_is_sixty_seconds() -> None:
    """At most one line each minute is written for calls with no verified token."""
    assert timedelta(seconds=60) == TOKENLESS_LINE_INTERVAL


def _tokenless_refusals(harness: Harness, count: int) -> list[str]:
    return [_refusal(harness, _bearer(UNKNOWN_TOKEN)) for _ in range(count)]


def test_tokenless_calls_inside_one_interval_write_one_line(
    harness: Harness,
) -> None:
    """A hundred calls at one moment are each refused, and one line is written."""
    refusals = _tokenless_refusals(harness, 100)

    assert refusals == [TOKEN_REFUSAL] * 100
    [line] = _audit_lines(harness)
    assert line["refusal"] == TOKEN_REFUSAL


def test_the_first_tokenless_line_after_the_interval_counts_the_unwritten(
    harness: Harness,
) -> None:
    """Once the interval has passed, the next line counts the calls with no line."""
    _tokenless_refusals(harness, 100)
    harness.clock.moment = MINTED_AT + TOKENLESS_LINE_INTERVAL

    assert _refusal(harness, _bearer(UNKNOWN_TOKEN)) == TOKEN_REFUSAL

    first, second = _audit_lines(harness)
    assert first["refusal"] == TOKEN_REFUSAL
    assert second["refusal"] == (
        f"{TOKEN_REFUSAL}; 99 more calls with no verified token were refused"
        " since the last such line"
    )


def test_a_tokenless_call_just_inside_the_interval_writes_no_line(
    harness: Harness,
) -> None:
    """A call a microsecond before the interval ends is counted, not written."""
    _tokenless_refusals(harness, 1)
    harness.clock.moment = (
        MINTED_AT + TOKENLESS_LINE_INTERVAL - timedelta(microseconds=1)
    )

    _tokenless_refusals(harness, 1)

    assert len(_audit_lines(harness)) == 1


def test_the_count_restarts_after_each_tokenless_line(harness: Harness) -> None:
    """A line with no call unwritten before it carries the refusal alone."""
    _tokenless_refusals(harness, 3)
    harness.clock.moment = MINTED_AT + TOKENLESS_LINE_INTERVAL
    _tokenless_refusals(harness, 1)
    harness.clock.moment = MINTED_AT + 2 * TOKENLESS_LINE_INTERVAL

    _tokenless_refusals(harness, 1)

    assert [line["refusal"] for line in _audit_lines(harness)] == [
        TOKEN_REFUSAL,
        f"{TOKEN_REFUSAL}; 2 more calls with no verified token were refused"
        " since the last such line",
        TOKEN_REFUSAL,
    ]


def test_an_unreadable_token_store_shares_the_tokenless_interval(
    harness: Harness,
) -> None:
    """A store refusal inside the interval of an unknown-token line writes none."""
    _tokenless_refusals(harness, 1)
    harness.tokens_path.write_bytes(NOT_SQLITE)

    assert _refusal(harness, _bearer(harness.token)) == TOKEN_STORE_REFUSAL

    [line] = _audit_lines(harness)
    assert line["refusal"] == TOKEN_REFUSAL


def test_a_verified_call_is_audited_inside_the_tokenless_interval(
    harness: Harness,
) -> None:
    """The interval holds back tokenless lines only."""
    _tokenless_refusals(harness, 1)

    harness.gateway.call(_bearer(harness.token), "tools_echo", ECHO_ARGUMENTS)
    _refusal(harness, _bearer(harness.token), "other_echo")

    assert [line["token_name"] for line in _audit_lines(harness)] == [
        None,
        "ci-bot",
        "ci-bot",
    ]


def test_a_failed_tokenless_line_is_counted_and_the_next_call_writes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tokenless line that fails leaves the interval open and counts its call."""
    fail_next_write_after_half(monkeypatch)
    assert AUDIT_REFUSAL.fullmatch(_refusal(harness, _bearer(UNKNOWN_TOKEN)))

    assert _refusal(harness, _bearer(UNKNOWN_TOKEN)) == TOKEN_REFUSAL

    [line] = _audit_lines(harness)
    assert line["refusal"] == (
        f"{TOKEN_REFUSAL}; 1 more calls with no verified token were refused"
        " since the last such line"
    )


def test_concurrent_tokenless_calls_inside_one_interval_write_one_line(
    harness: Harness,
) -> None:
    """Eight threads of twenty-five calls write one line, and the next counts 199."""
    refusals: list[str] = []
    lock = threading.Lock()

    def refuse_twenty_five() -> None:
        for _ in range(25):
            refusal = _refusal(harness, _bearer(UNKNOWN_TOKEN))
            with lock:
                refusals.append(refusal)

    threads = [threading.Thread(target=refuse_twenty_five) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    harness.clock.moment = MINTED_AT + TOKENLESS_LINE_INTERVAL
    _tokenless_refusals(harness, 1)

    assert refusals == [TOKEN_REFUSAL] * 200
    assert [line["refusal"] for line in _audit_lines(harness)] == [
        TOKEN_REFUSAL,
        f"{TOKEN_REFUSAL}; 199 more calls with no verified token were refused"
        " since the last such line",
    ]


def test_a_latched_writer_refuses_before_authenticating(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A latched writer refuses at once: no use recorded, no cage, no line."""
    _latch(harness.audit, monkeypatch)

    assert AUDIT_REFUSAL.fullmatch(_refusal(harness, _bearer(harness.token)))
    assert _last_used(harness) is None
    assert harness.runner.calls == []
    assert _audit_lines(harness) == []


def _self_check_refusal(
    harness: Harness,
    authorization: str | None,
    arguments: dict[str, object] | None = None,
) -> str:
    with pytest.raises(CallRefusedError) as caught:
        harness.gateway.call(authorization, SELF_CHECK_TOOL, arguments or {})
    return str(caught.value)


def test_the_self_check_returns_the_scopes_report(harness: Harness) -> None:
    """The reply is the report's content, and no cage call runs for it."""
    reply = harness.gateway.call(_bearer(harness.token), SELF_CHECK_TOOL, {})

    assert reply == CheckReport(scope="dev", checks=CHECKS).content()
    assert reply["ok"] is False
    assert harness.runner.calls == []


def test_the_self_check_runs_while_the_snapshot_is_in_use(harness: Harness) -> None:
    """The check runs on the snapshot in force, held in use, released afterwards."""
    harness.gateway.call(_bearer(harness.token), SELF_CHECK_TOOL, {})

    assert harness.self_check.runs == [(SNAPSHOT, "dev", 1)]
    assert harness.live._uses == {}


def test_the_self_check_sets_last_used_at(harness: Harness) -> None:
    """The self-check authenticates the token as any call does."""
    harness.clock.moment = LATER

    harness.gateway.call(_bearer(harness.token), SELF_CHECK_TOOL, {})

    assert _last_used(harness) == LATER


@pytest.mark.parametrize("make_authorization", BAD_AUTHORIZATIONS)
def test_the_self_check_refuses_a_token_that_does_not_authenticate(
    harness: Harness, make_authorization: MakeAuthorization
) -> None:
    """The self-check gets the one token refusal, and runs no check."""
    assert _self_check_refusal(harness, make_authorization(harness)) == TOKEN_REFUSAL
    assert harness.self_check.runs == []


def test_the_self_check_refuses_a_scope_the_config_does_not_define(
    harness: Harness,
) -> None:
    """The refusal names the scope, runs no check, and is audited."""
    token = harness.store.mint("ghost-bot", "ghost", None)

    refusal = _self_check_refusal(harness, _bearer(token))

    assert refusal == "scope 'ghost' is not defined in the config in force"
    assert harness.self_check.runs == []
    [line] = _audit_lines(harness)
    assert line["action"] == SELF_CHECK_TOOL
    assert line["token_name"] == "ghost-bot"
    assert line["refusal"] == refusal


def test_the_self_check_refuses_any_input(harness: Harness) -> None:
    """The refusal names the arguments sorted, runs no check, and is audited."""
    arguments: dict[str, object] = {"scope": "ops", "deep": True}

    refusal = _self_check_refusal(harness, _bearer(harness.token), arguments)

    assert refusal == (
        "tool 'agent-proxy_self_check' takes no input, got ['deep', 'scope']"
    )
    assert harness.self_check.runs == []
    [line] = _audit_lines(harness)
    assert line["arguments"] == arguments
    assert line["refusal"] == refusal


def test_the_self_checks_audit_line(harness: Harness) -> None:
    """One line holds the call's start, token, tool, and no cage's output."""
    harness.clock.moment = LATER

    harness.gateway.call(_bearer(harness.token), SELF_CHECK_TOOL, {})

    [line] = _audit_lines(harness)
    duration_ms = line.pop("duration_ms")
    call_id = line.pop("call_id")
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0
    assert isinstance(call_id, str)
    assert re.fullmatch(CALL_ID_PATTERN, call_id)
    assert line == {
        "timestamp": LATER.isoformat(),
        "token_name": "ci-bot",
        "scope": "dev",
        "action": SELF_CHECK_TOOL,
        "arguments": {},
        "arguments_cut": None,
        "exit_code": None,
        "stdout_bytes": None,
        "stderr_bytes": None,
        "truncated": False,
        "refusal": None,
        "credential": None,
    }


def test_a_failed_audit_write_withholds_the_report(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-check whose line cannot be written is refused, its report withheld."""
    fail_next_write_after_half(monkeypatch)

    refusal = _self_check_refusal(harness, _bearer(harness.token))

    assert AUDIT_REFUSAL.fullmatch(refusal)
    assert len(harness.self_check.runs) == 1


def test_a_latched_writer_refuses_the_self_check_before_authenticating(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A latched writer refuses at once: no use recorded, no check, no line."""
    _latch(harness.audit, monkeypatch)

    assert AUDIT_REFUSAL.fullmatch(_self_check_refusal(harness, _bearer(harness.token)))
    assert _last_used(harness) is None
    assert harness.self_check.runs == []
    assert _audit_lines(harness) == []


def _check_scopes_refusal(harness: Harness, scope_name: str | None) -> str:
    with pytest.raises(CallRefusedError) as caught:
        harness.gateway.check_scopes(scope_name)
    return str(caught.value)


def test_check_scopes_reports_the_one_scope_named(harness: Harness) -> None:
    """A named scope gets one report, checked on the snapshot held in use."""
    reports = harness.gateway.check_scopes("ops")

    assert reports == [CheckReport(scope="ops", checks=CHECKS).content()]
    assert harness.self_check.runs == [(SNAPSHOT, "ops", 1)]
    assert harness.live._uses == {}


def test_check_scopes_reports_every_scope_by_name_under_one_use(
    harness: Harness,
) -> None:
    """With no scope named, every scope is checked in name order, in one use."""
    reports = harness.gateway.check_scopes(None)

    assert [report["scope"] for report in reports] == ["dev", "ops"]
    assert harness.self_check.runs == [(SNAPSHOT, "dev", 1), (SNAPSHOT, "ops", 1)]
    assert harness.live._uses == {}


def test_check_scopes_refuses_an_undefined_scope_with_no_audit_line(
    harness: Harness,
) -> None:
    """The refusal names the scope and the defined ones; nothing runs or is audited."""
    refusal = _check_scopes_refusal(harness, "ghost")

    assert refusal == (
        "scope 'ghost' is not defined in the config in force;"
        " defined scopes are ['dev', 'ops']"
    )
    assert harness.self_check.runs == []
    assert _audit_lines(harness) == []


def test_check_scopes_is_refused_while_the_audit_log_is_unavailable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A latched writer refuses the check before it runs, with no line."""
    _latch(harness.audit, monkeypatch)

    refusal = _check_scopes_refusal(harness, None)

    assert refusal == "the audit log is unavailable, so the check is refused"
    assert harness.self_check.runs == []
    assert _audit_lines(harness) == []


def test_check_scopes_writes_one_audit_line_per_scope(harness: Harness) -> None:
    """Each scope's line has no token, no arguments, and no cage's output."""
    harness.clock.moment = LATER

    harness.gateway.check_scopes(None)

    lines = _audit_lines(harness)
    call_ids = [line.pop("call_id") for line in lines]
    durations = [line.pop("duration_ms") for line in lines]
    assert len(set(call_ids)) == len(call_ids) == 2
    assert all(
        isinstance(call_id, str) and re.fullmatch(CALL_ID_PATTERN, call_id)
        for call_id in call_ids
    )
    assert all(isinstance(ms, int) and ms >= 0 for ms in durations)
    assert lines == [
        {
            "timestamp": LATER.isoformat(),
            "token_name": None,
            "scope": scope,
            "action": SELF_CHECK_TOOL,
            "arguments": {},
            "arguments_cut": None,
            "exit_code": None,
            "stdout_bytes": None,
            "stderr_bytes": None,
            "truncated": False,
            "refusal": None,
            "credential": None,
        }
        for scope in ("dev", "ops")
    ]


def test_check_scopes_withholds_the_reports_when_a_line_cannot_be_written(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first scope's failed line refuses the request; no later scope runs."""
    fail_next_write_after_half(monkeypatch)

    refusal = _check_scopes_refusal(harness, None)

    assert AUDIT_REFUSAL.fullmatch(refusal)
    assert [scope for _, scope, _ in harness.self_check.runs] == ["dev"]
    assert harness.live._uses == {}
