import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
import uvicorn
from egress_fakes import RecordingEgress
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.http import StarletteWithLifespan

from agent_proxy.audit import AuditError, AuditRecord, AuditWriter
from agent_proxy.auth import TokenStore
from agent_proxy.config import InstanceConfig
from agent_proxy.errors import ProxyError
from agent_proxy.mcp import REFUSAL_PREFIX, CallGateway, build_app
from agent_proxy.plugins import Manifest, parse_manifest
from agent_proxy.reload import LiveConfig, Snapshot
from agent_proxy.runner import CageCall, CageTimeoutError, InputError, Outcome
from agent_proxy.selfcheck import SELF_CHECK_TOOL, CheckEntry, CheckReport

WAIT_SECONDS = 10
AGENT_TREE = "/srv/agents/alpha"
TOOLS_IMAGE = "ghcr.io/example/tools@sha256:" + "a" * 64
OTHER_IMAGE = "ghcr.io/example/other@sha256:" + "b" * 64
UNKNOWN_TOKEN = "apx_" + "x" * 43
TOKEN_REFUSAL = "the bearer token is missing, unknown, revoked, or expired"
TOKEN_STORE_REFUSAL = "the token store could not be read"
AUDIT_REFUSAL = re.compile(
    r"the audit log could not record call [0-9a-f]{32}, so its result is withheld"
)
NOT_SQLITE = b"this file is not a SQLite database\n" * 8
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
INSPECT_SCHEMA = {
    "type": "object",
    "properties": {"target": {"$ref": "#/$defs/target"}},
    "required": ["target"],
    "$defs": {"target": {"type": "string", "minLength": 1}},
}
ECHO_ACTION = {
    "kind": "command",
    "input_schema": ECHO_SCHEMA,
    "credentials": [],
    "timeout": "10s",
    "argv": ["echo"],
    "parameters": [{"name": "text", "as": "positional"}],
    "passthrough_args": False,
}
INSPECT_ACTION = {
    "kind": "script",
    "input_schema": INSPECT_SCHEMA,
    "credentials": [],
    "timeout": "10s",
    "executable": "/plugin/inspect",
    "output_schema": {"type": "object"},
}


def _manifest(actions: dict[str, object]) -> Manifest:
    return parse_manifest(json.dumps({"actions": actions}).encode())


def _grant() -> dict[str, object]:
    return {"roots": [], "domains": [], "credentials": {}}


CONFIG = InstanceConfig.model_validate(
    {
        "agent_tree": AGENT_TREE,
        "overflow_dir": f"{AGENT_TREE}/overflow",
        "max_cages": 2,
        "plugins": {"tools": TOOLS_IMAGE, "other": OTHER_IMAGE},
        "scopes": {
            "dev": {"grants": {"tools.echo": _grant(), "tools.inspect": _grant()}},
            "ops": {"grants": {"other.status": _grant()}},
        },
    }
)
SNAPSHOT = Snapshot(
    config=CONFIG,
    manifests=MappingProxyType(
        {
            "tools": _manifest({"echo": ECHO_ACTION, "inspect": INSPECT_ACTION}),
            "other": _manifest({"status": ECHO_ACTION}),
        }
    ),
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
SELF_CHECK_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
CHECKS = (
    CheckEntry(
        check="root",
        subject=f"{AGENT_TREE}/absent",
        ok=False,
        detail=f"root '{AGENT_TREE}/absent' does not exist",
    ),
)


class FakeSelfCheck:
    """A self-check that reports CHECKS for every scope it runs."""

    def run(self, snapshot: Snapshot, scope_name: str) -> CheckReport:
        """Return the report of scope_name."""
        return CheckReport(scope=scope_name, checks=CHECKS)


class FakeRunner:
    """A cage runner that records each call and returns or raises a set result."""

    def __init__(self) -> None:
        """Return ECHO_OUTCOME until result is changed."""
        self.result: Outcome | ProxyError = ECHO_OUTCOME
        self.calls: list[CageCall] = []

    def run(self, call: CageCall) -> Outcome:
        """Record call, then return or raise result."""
        self.calls.append(call)
        if isinstance(self.result, ProxyError):
            raise self.result
        return self.result


class CollectingHandler(logging.Handler):
    """Keeps the message of every record it receives."""

    def __init__(self) -> None:
        """Start with no messages, at the lowest level."""
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record's message."""
        self.messages.append(record.getMessage())


@dataclass
class Served:
    url: str
    runner: FakeRunner
    dev_token: str
    ops_token: str
    retired_token: str
    tokens_path: Path
    audit: AuditWriter


@contextlib.contextmanager
def _serving(app: StarletteWithLifespan) -> Iterator[str]:
    """Serve app on a free loopback port on a uvicorn thread; yield its MCP URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail(f"the test server did not start within {WAIT_SECONDS}s")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(WAIT_SECONDS)
        if thread.is_alive():
            pytest.fail(f"the test server did not stop within {WAIT_SECONDS}s")


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Served]:
    """Serve build_app over a gateway with real stores and a fake runner."""
    tokens_path = tmp_path / "tokens.sqlite3"
    store = TokenStore(tokens_path)
    runner = FakeRunner()
    audit = AuditWriter(tmp_path / "audit")
    gateway = CallGateway(
        store,
        LiveConfig(SNAPSHOT, RecordingEgress()),
        runner,
        FakeSelfCheck(),
        audit,
        lambda: datetime.now(UTC),
    )
    tokens = {
        "dev": store.mint("dev-bot", "dev", None),
        "ops": store.mint("ops-bot", "ops", None),
        "retired": store.mint("old-bot", "retired", None),
    }
    with _serving(build_app(gateway)) as url:
        yield Served(
            url,
            runner,
            tokens["dev"],
            tokens["ops"],
            tokens["retired"],
            tokens_path,
            audit,
        )


def _list(url: str, token: str | None) -> dict[str, object]:
    """Return each listed tool's input schema by tool name."""

    async def listing() -> dict[str, object]:
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            return {tool.name: tool.input_schema for tool in await client.list_tools()}

    return asyncio.run(listing())


def _call(
    url: str,
    token: str | None,
    tool: str,
    arguments: dict[str, object],
    *,
    raise_on_error: bool = True,
) -> CallToolResult:
    async def calling() -> CallToolResult:
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            return await client.call_tool(
                tool, arguments, raise_on_error=raise_on_error
            )

    return asyncio.run(calling())


def _compact(content: Mapping[str, object]) -> str:
    return json.dumps(content, separators=(",", ":"))


def _bearer(served: Served, who: str | None) -> str | None:
    return {
        None: None,
        "unknown": UNKNOWN_TOKEN,
        "dev": served.dev_token,
        "retired": served.retired_token,
    }[who]


def test_two_scopes_list_their_own_tools_with_verbatim_schemas(
    served: Served,
) -> None:
    """Each token lists its scope's tools with their schemas, and the self-check."""
    assert _list(served.url, served.dev_token) == {
        SELF_CHECK_TOOL: SELF_CHECK_SCHEMA,
        "tools_echo": ECHO_SCHEMA,
        "tools_inspect": INSPECT_SCHEMA,
    }
    assert _list(served.url, served.ops_token) == {
        SELF_CHECK_TOOL: SELF_CHECK_SCHEMA,
        "other_status": ECHO_SCHEMA,
    }


def test_a_scope_the_config_does_not_define_lists_the_self_check_alone(
    served: Served,
) -> None:
    """The self-check's schema reaches the client as the plain JSON schema."""
    assert _list(served.url, served.retired_token) == {
        SELF_CHECK_TOOL: SELF_CHECK_SCHEMA
    }


def test_the_self_checks_report_is_a_normal_result_with_structured_content(
    served: Served,
) -> None:
    """A failing report is a normal result: its content and its compact JSON text."""
    expected = CheckReport(scope="dev", checks=CHECKS).content()

    result = _call(
        served.url, served.dev_token, SELF_CHECK_TOOL, {}, raise_on_error=False
    )

    assert result.is_error is False
    assert result.structured_content == expected
    [text] = result.content
    assert text.text == _compact(expected)
    assert served.runner.calls == []


@pytest.mark.parametrize("who", [None, "unknown"], ids=["missing", "unknown"])
def test_a_token_that_does_not_authenticate_lists_nothing(
    served: Served, who: str | None
) -> None:
    """A missing or unknown token lists no tool, and is not an error."""
    assert _list(served.url, _bearer(served, who)) == {}


@pytest.mark.parametrize(
    ("who", "tool", "failure", "refusal"),
    [
        pytest.param(None, "tools_echo", None, TOKEN_REFUSAL, id="missing-token"),
        pytest.param("unknown", "tools_echo", None, TOKEN_REFUSAL, id="unknown-token"),
        pytest.param(
            "retired",
            "tools_echo",
            None,
            "scope 'retired' is not defined in the config in force",
            id="undefined-scope",
        ),
        pytest.param(
            "dev",
            "other_status",
            None,
            "tool 'other_status' is not granted to scope 'dev'",
            id="not-granted",
        ),
        pytest.param(
            "dev",
            "tools_echo",
            InputError("text: 3 is not of type 'string'"),
            "text: 3 is not of type 'string'",
            id="input",
        ),
        pytest.param(
            "dev",
            "tools_echo",
            CageTimeoutError("action 'echo' ran past its timeout of 10s"),
            "action 'echo' ran past its timeout of 10s",
            id="timeout",
        ),
    ],
)
def test_every_refusal_reaches_the_client_as_a_prefixed_tool_error(
    served: Served,
    who: str | None,
    tool: str,
    failure: ProxyError | None,
    refusal: str,
) -> None:
    """A refusal is a ToolError whose text is the prefix and the gateway's message."""
    if failure is not None:
        served.runner.result = failure

    with pytest.raises(ToolError) as caught:
        _call(served.url, _bearer(served, who), tool, {"text": "hello"})

    assert str(caught.value) == REFUSAL_PREFIX + refusal


def test_an_unreadable_token_store_reaches_the_client_as_a_prefixed_tool_error(
    served: Served,
) -> None:
    """A token store that is not SQLite refuses the call with its fixed message."""
    served.tokens_path.write_bytes(NOT_SQLITE)

    with pytest.raises(ToolError) as caught:
        _call(served.url, served.dev_token, "tools_echo", {"text": "hello"})

    assert str(caught.value) == REFUSAL_PREFIX + TOKEN_STORE_REFUSAL


def test_a_failed_audit_write_reaches_the_client_as_a_prefixed_tool_error(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call whose audit line cannot be written is refused, its result withheld."""

    def fail(record: AuditRecord) -> None:
        raise AuditError("the audit log cannot take the line")

    monkeypatch.setattr(served.audit, "write", fail)

    with pytest.raises(ToolError) as caught:
        _call(served.url, served.dev_token, "tools_echo", {"text": "hello"})

    message = str(caught.value)
    assert message.startswith(REFUSAL_PREFIX)
    assert AUDIT_REFUSAL.fullmatch(message.removeprefix(REFUSAL_PREFIX))


def test_a_failed_script_is_a_normal_result_carrying_its_content(
    served: Served,
) -> None:
    """A failed script is a normal result with structured content and its JSON text."""
    served.runner.result = FAILED_SCRIPT

    result = _call(
        served.url,
        served.dev_token,
        "tools_inspect",
        {"target": "src"},
        raise_on_error=False,
    )

    assert result.is_error is False
    assert result.structured_content == FAILED_SCRIPT.content
    [text] = result.content
    assert text.text == _compact(FAILED_SCRIPT.content)
    assert not text.text.startswith(REFUSAL_PREFIX)


def test_a_normal_result_carries_the_outcome_as_structured_content_and_text(
    served: Served,
) -> None:
    """A normal result's structured content is the outcome's, and its text the JSON."""
    result = _call(served.url, served.dev_token, "tools_echo", {"text": "hello"})

    assert result.is_error is False
    assert result.structured_content == ECHO_OUTCOME.content
    [text] = result.content
    assert text.text == _compact(ECHO_OUTCOME.content)
    [cage_call] = served.runner.calls
    assert cage_call.arguments == {"text": "hello"}


def test_no_log_output_carries_a_token(
    served: Served,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A token in the header and in the arguments reaches no log output.

    The fastmcp logger does not propagate, so caplog alone never sees its
    records; a handler on it collects them.
    """
    handler = CollectingHandler()
    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(handler)
    try:
        _call(served.url, served.dev_token, "tools_echo", {"text": served.dev_token})
        with pytest.raises(ToolError):
            _call(
                served.url,
                served.dev_token,
                "other_status",
                {"text": served.dev_token},
            )
    finally:
        fastmcp_logger.removeHandler(handler)

    captured = capfd.readouterr()
    assert handler.messages
    assert not any(served.dev_token in message for message in handler.messages)
    assert served.dev_token not in captured.out
    assert served.dev_token not in captured.err
    assert served.dev_token not in caplog.text
