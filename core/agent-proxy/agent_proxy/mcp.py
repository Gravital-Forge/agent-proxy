"""The MCP surface: the call path, its FastMCP glue, and the serving instance."""

import contextlib
import json
import signal
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, Self

import uvicorn
from anyio import to_thread
from docker import DockerClient
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.versions import VersionSpec
from pydantic import PrivateAttr

from agent_proxy import plugins
from agent_proxy.audit import AuditError, AuditRecord, AuditWriter
from agent_proxy.auth import TokenRecord, TokenStore, TokenStoreError
from agent_proxy.credentials import CredentialStore
from agent_proxy.egress import EgressProxies, cage_network
from agent_proxy.errors import ProxyError
from agent_proxy.reload import (
    CONTROL_SOCKET_NAME,
    ControlServer,
    LiveConfig,
    Snapshot,
    build_snapshot,
)
from agent_proxy.results import OverflowSweeper, OverflowWriteError, new_call_id
from agent_proxy.runner import (
    CageCall,
    CageFailedError,
    CageLimiter,
    CageRunner,
    CageTimeoutError,
    Outcome,
    ensure_verifier,
    limit_warnings,
    remove_left_cages,
)
from agent_proxy.selfcheck import (
    SELF_CHECK_INPUT_SCHEMA,
    SELF_CHECK_TOOL,
    CheckReport,
    SelfCheck,
)

REFUSAL_PREFIX = "agent-proxy refused: "
TOKENLESS_LINE_INTERVAL = timedelta(seconds=60)
_BEARER_SCHEME = "bearer"
_UNAUTHENTICATED_REFUSAL = "the bearer token is missing, unknown, revoked, or expired"
_STORE_UNREADABLE_REFUSAL = "the token store could not be read"
_SERVER_NAME = "agent-proxy"
_MCP_PATH = "/mcp"
_AUTHORIZATION_HEADER = "authorization"
_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_AFTER_START_ERRORS = (CageTimeoutError, CageFailedError, OverflowWriteError)


class CallRefusedError(ProxyError):
    """The proxy refuses a call; str() is the refusal without REFUSAL_PREFIX."""


@dataclass(frozen=True)
class ToolSpec:
    """One listed tool: its name and its input schema."""

    name: str
    input_schema: Mapping[str, object]


@dataclass(frozen=True)
class _Call:
    """One tools/call as received, with its ID and its start."""

    call_id: str
    tool: str
    arguments: Mapping[str, object]
    started_at: datetime
    started: float

    def outcome_line(self, token: TokenRecord, outcome: Outcome) -> AuditRecord:
        """Return the audit record of a call whose cage ran."""
        return AuditRecord(
            timestamp=self.started_at,
            call_id=self.call_id,
            token_name=token.name,
            scope=token.scope,
            action=self.tool,
            arguments=self.arguments,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            stdout_bytes=outcome.stdout_bytes,
            stderr_bytes=outcome.stderr_bytes,
            truncated=outcome.truncated,
            refusal=None,
        )

    def check_line(self, token_name: str | None, scope: str) -> AuditRecord:
        """Return the audit record of a self-check of scope that ran.

        token_name is None when the operator's command asked for the check. No
        single cage's output is the result, so the output fields are None.
        """
        return AuditRecord(
            timestamp=self.started_at,
            call_id=self.call_id,
            token_name=token_name,
            scope=scope,
            action=self.tool,
            arguments=self.arguments,
            exit_code=None,
            duration_ms=self._elapsed_ms(),
            stdout_bytes=None,
            stderr_bytes=None,
            truncated=False,
            refusal=None,
        )

    def refusal_line(self, token: TokenRecord | None, refusal: str) -> AuditRecord:
        """Return the audit record of a call refused now with refusal.

        token is None when no token verified. Anyone who reaches the port can
        make such a call, so its line holds none of the call's arguments.
        """
        return AuditRecord(
            timestamp=self.started_at,
            call_id=self.call_id,
            token_name=None if token is None else token.name,
            scope=None if token is None else token.scope,
            action=self.tool,
            arguments=None if token is None else self.arguments,
            exit_code=None,
            duration_ms=self._elapsed_ms(),
            stdout_bytes=None,
            stderr_bytes=None,
            truncated=False,
            refusal=refusal,
        )

    def _elapsed_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)


class CallGateway:
    """Lists the tools a bearer token's scope grants, and runs each call of one.

    Every call is refused or run, then audited in one line, except a call
    with no verified token: such calls write at most one line each
    TOKENLESS_LINE_INTERVAL, and that line counts the ones that wrote none. A
    call whose line cannot be written is refused, and its result is withheld.
    """

    def __init__(
        self,
        tokens: TokenStore,
        live: LiveConfig,
        runner: CageRunner,
        self_check: SelfCheck,
        audit: AuditWriter,
        now: Callable[[], datetime],
    ) -> None:
        """Hold the stores and collaborators; now returns the current time in UTC."""
        self._tokens = tokens
        self._live = live
        self._runner = runner
        self._self_check = self_check
        self._audit = audit
        self._now = now
        self._tokenless_lock = threading.Lock()
        self._tokenless_line_at: datetime | None = None
        self._tokenless_unwritten = 0

    def list_tools(self, authorization: str | None) -> list[ToolSpec]:
        """Return the self-check and one spec per grant of the token's scope, by name.

        The token is verified and its use is not recorded. The list is empty
        when the token does not verify or when the token store cannot be read.
        A token whose scope the config in force does not define lists the
        self-check alone.
        """
        token = _bearer_token(authorization)
        if token is None:
            return []
        try:
            record = self._tokens.verify(token)
        except TokenStoreError:
            return []
        if record is None:
            return []
        snapshot = self._live.current
        specs = [ToolSpec(SELF_CHECK_TOOL, SELF_CHECK_INPUT_SCHEMA)]
        scope = snapshot.config.scopes.get(record.scope)
        if scope is not None:
            for key in scope.grants:
                plugin, action = key.split(".", 1)
                schema = snapshot.manifests[plugin].tools[action].input_schema
                specs.append(ToolSpec(f"{plugin}_{action}", schema))
        return sorted(specs, key=lambda spec: spec.name)

    def call(
        self, authorization: str | None, tool: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Run one call of tool with arguments, audit it, and return its content.

        The self-check tool returns its report; every other tool runs a cage.

        Raises:
            CallRefusedError: the call is refused, or its audit line cannot be
                written; its message is the refusal without REFUSAL_PREFIX.
        """
        call = self._start(tool, arguments)
        if not self._audit.available:
            raise CallRefusedError(_audit_refusal(call.call_id))
        try:
            token = self._authenticate(authorization)
        except CallRefusedError as error:
            self._refuse_tokenless(call, error)
        if tool == SELF_CHECK_TOOL:
            return self._self_check_call(call, token)
        return self._cage_call(call, token)

    def _start(self, tool: str, arguments: Mapping[str, object]) -> _Call:
        """Return a call of tool with arguments, with a fresh ID, starting now."""
        return _Call(
            call_id=new_call_id(),
            tool=tool,
            arguments=arguments,
            started_at=self._now(),
            started=time.monotonic(),
        )

    def _cage_call(self, call: _Call, token: TokenRecord) -> Mapping[str, object]:
        """Run call in a cage, audit it, and return the outcome's content.

        Raises:
            CallRefusedError: the call is refused, or its audit line cannot be
                written; once the cage has started, that refusal says the
                action ran.
        """
        try:
            outcome = self._run(call, token.scope)
        except _AFTER_START_ERRORS as error:
            refusal = str(error)
            self._write_ran(call.refusal_line(token, refusal))
            raise CallRefusedError(refusal) from error
        except ProxyError as error:
            self._refuse(call, token, error)
        self._write_ran(call.outcome_line(token, outcome))
        return outcome.content

    def _self_check_call(self, call: _Call, token: TokenRecord) -> Mapping[str, object]:
        """Check the token's scope, audit the call, and return the report's content.

        Raises:
            CallRefusedError: the call is refused, or its audit line cannot be
                written.
        """
        try:
            report = self._check(call, token.scope)
        except ProxyError as error:
            self._refuse(call, token, error)
        self._write(call.check_line(token.name, token.scope))
        return report.content()

    def check_scopes(self, scope_name: str | None) -> list[dict[str, object]]:
        """Check scope_name, or every scope by name when None, and audit each.

        The snapshot in force is held in use for the whole request. Each
        scope's check writes one line with no token name; a refused request
        writes none.

        Raises:
            CallRefusedError: the audit log is unavailable, the scope is not
                defined in the config in force, or a scope's line cannot be
                written, which withholds every report.
        """
        if not self._audit.available:
            raise CallRefusedError(
                "the audit log is unavailable, so the check is refused"
            )
        with self._live.use() as snapshot:
            defined = sorted(snapshot.config.scopes)
            if scope_name is not None and scope_name not in defined:
                raise CallRefusedError(
                    f"{_undefined_scope_refusal(scope_name)};"
                    f" defined scopes are {defined!r}"
                )
            names = defined if scope_name is None else [scope_name]
            return [self._check_scope(snapshot, name) for name in names]

    def _check_scope(self, snapshot: Snapshot, scope_name: str) -> dict[str, object]:
        """Check scope_name on snapshot, audit it, and return the report's content.

        Raises:
            CallRefusedError: the audit line cannot be written.
        """
        call = self._start(SELF_CHECK_TOOL, {})
        report = self._self_check.run(snapshot, scope_name)
        self._write(call.check_line(None, scope_name))
        return report.content()

    def _check(self, call: _Call, scope_name: str) -> CheckReport:
        """Check scope_name, holding the snapshot in force in use throughout.

        Raises:
            CallRefusedError: the scope is not defined in the config in force,
                or the call carries arguments.
        """
        with self._live.use() as snapshot:
            if scope_name not in snapshot.config.scopes:
                raise CallRefusedError(_undefined_scope_refusal(scope_name))
            if call.arguments:
                raise CallRefusedError(
                    f"tool {SELF_CHECK_TOOL!r} takes no input, "
                    f"got {sorted(call.arguments)!r}"
                )
            return self._self_check.run(snapshot, scope_name)

    def _authenticate(self, authorization: str | None) -> TokenRecord:
        """Return the record of the call's token, recording its use.

        Raises:
            CallRefusedError: the token does not authenticate, or the token
                store cannot be read.
        """
        token = _bearer_token(authorization)
        try:
            record = None if token is None else self._tokens.authenticate(token)
        except TokenStoreError as error:
            raise CallRefusedError(_STORE_UNREADABLE_REFUSAL) from error
        if record is None:
            raise CallRefusedError(_UNAUTHENTICATED_REFUSAL)
        return record

    def _run(self, call: _Call, scope_name: str) -> Outcome:
        """Run call in a cage, holding the snapshot in force in use throughout.

        Raises:
            CallRefusedError: the scope is not defined in the config in force,
                or the tool names no grant of it.
            ProxyError: the runner refuses or fails the call.
        """
        with self._live.use() as snapshot:
            scope = snapshot.config.scopes.get(scope_name)
            if scope is None:
                raise CallRefusedError(_undefined_scope_refusal(scope_name))
            plugin, _, action = call.tool.partition("_")
            grant = scope.grants.get(f"{plugin}.{action}")
            if grant is None:
                raise CallRefusedError(
                    f"tool {call.tool!r} is not granted to scope {scope_name!r}"
                )
            return self._runner.run(
                CageCall(
                    call_id=call.call_id,
                    image=snapshot.config.plugins[plugin],
                    action=snapshot.manifests[plugin].tools[action],
                    grant=grant,
                    arguments=call.arguments,
                    network=cage_network(grant.domains),
                    overflow_dir=snapshot.config.overflow_dir,
                    max_cages=snapshot.config.max_cages,
                )
            )

    def _refuse(self, call: _Call, token: TokenRecord, error: ProxyError) -> NoReturn:
        """Audit call as refused with error's text, then raise that refusal.

        Raises:
            CallRefusedError: always; the audit refusal when the line cannot be
                written.
        """
        refusal = str(error)
        self._write(call.refusal_line(token, refusal))
        raise CallRefusedError(refusal) from error

    def _refuse_tokenless(self, call: _Call, error: CallRefusedError) -> NoReturn:
        """Refuse call, which has no verified token, auditing it when a line is due.

        A line is due when none was written yet or TOKENLESS_LINE_INTERVAL
        has passed since the last one. A call that writes no line, a failed
        write included, is counted in the next line's refusal.

        Raises:
            CallRefusedError: always; the audit refusal when a due line cannot
                be written.
        """
        refusal = str(error)
        with self._tokenless_lock:
            now = self._now()
            last = self._tokenless_line_at
            if last is not None and now - last < TOKENLESS_LINE_INTERVAL:
                self._tokenless_unwritten += 1
                raise CallRefusedError(refusal) from error
            line = call.refusal_line(
                None, _with_unwritten(refusal, self._tokenless_unwritten)
            )
            try:
                self._write(line)
            except CallRefusedError:
                self._tokenless_unwritten += 1
                raise
            self._tokenless_line_at = now
            self._tokenless_unwritten = 0
        raise CallRefusedError(refusal) from error

    def _write(self, line: AuditRecord) -> None:
        """Write line to the audit log.

        Raises:
            CallRefusedError: the log cannot record the line.
        """
        try:
            self._audit.write(line)
        except AuditError as error:
            raise CallRefusedError(_audit_refusal(line.call_id)) from error

    def _write_ran(self, line: AuditRecord) -> None:
        """Write the line of a call whose cage ran to the audit log.

        Raises:
            CallRefusedError: the log cannot record the line; the refusal says
                the action ran and may have taken effect.
        """
        try:
            self._audit.write(line)
        except AuditError as error:
            raise CallRefusedError(_ran_audit_refusal(line.call_id)) from error


def _bearer_token(authorization: str | None) -> str | None:
    """Return the token of a "Bearer <token>" header, or None for any other form."""
    if authorization is None:
        return None
    scheme, space, token = authorization.partition(" ")
    if not space or scheme.lower() != _BEARER_SCHEME:
        return None
    return token


def _audit_refusal(call_id: str) -> str:
    return f"the audit log could not record call {call_id}, so its result is withheld"


def _ran_audit_refusal(call_id: str) -> str:
    return (
        "the action ran and may have taken effect, but the audit log could not"
        f" record call {call_id}, so its result is withheld; check what it changed"
        " before a retry"
    )


def _with_unwritten(refusal: str, unwritten: int) -> str:
    """Return refusal, naming the unwritten calls with no verified token, if any."""
    if unwritten == 0:
        return refusal
    return (
        f"{refusal}; {unwritten} more calls with no verified token were refused"
        " since the last such line"
    )


def _undefined_scope_refusal(scope_name: str) -> str:
    return f"scope {scope_name!r} is not defined in the config in force"


class ProxyTool(Tool):
    """A tool of the proxy; its run hands each call to the gateway."""

    _gateway: CallGateway = PrivateAttr()

    @classmethod
    def of(cls, gateway: CallGateway, name: str, schema: Mapping[str, object]) -> Self:
        """Return the tool name, taking schema as its input, that calls gateway."""
        tool = cls(name=name, parameters=dict(schema), output_schema=None)
        tool._gateway = gateway
        return tool

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        """Run the call on a worker thread and return its reply as a result.

        Raises:
            ToolError: the proxy refuses the call; its text is REFUSAL_PREFIX
                followed by the refusal.
        """
        authorization = get_http_request().headers.get(_AUTHORIZATION_HEADER)
        try:
            reply = await to_thread.run_sync(
                self._gateway.call, authorization, self.name, arguments
            )
        except CallRefusedError as error:
            raise ToolError(REFUSAL_PREFIX + str(error)) from error
        content = dict(reply)
        return ToolResult(
            content=json.dumps(content, separators=(",", ":")),
            structured_content=content,
        )


class ScopedTools(Provider):
    """Lists each request's token's tools, and resolves every name to a tool."""

    def __init__(self, gateway: CallGateway) -> None:
        """List and call through gateway."""
        super().__init__()
        self._gateway = gateway

    async def _list_tools(self) -> Sequence[Tool]:
        """Return the tools the request's bearer token lists.

        FastMCP also lists at startup, outside any request; that list is empty.
        """
        try:
            request = get_http_request()
        except RuntimeError:
            return []
        specs = await to_thread.run_sync(
            self._gateway.list_tools, request.headers.get(_AUTHORIZATION_HEADER)
        )
        return [
            ProxyTool.of(self._gateway, spec.name, spec.input_schema) for spec in specs
        ]

    async def _get_tool(
        self, name: str, version: VersionSpec | None = None
    ) -> Tool | None:
        """Return a tool for every name, so every call reaches the gateway."""
        return ProxyTool.of(self._gateway, name, {"type": "object"})


def build_app(gateway: CallGateway) -> StarletteWithLifespan:
    """Return the ASGI app that serves gateway's tools over MCP at /mcp."""
    server = FastMCP(
        _SERVER_NAME,
        providers=[ScopedTools(gateway)],
        dereference_schemas=False,
        mask_error_details=True,
    )
    return server.http_app(path=_MCP_PATH)


def snapshot_builder(
    config_path: Path, credentials: CredentialStore, client: DockerClient
) -> Callable[[], Snapshot]:
    """Return a build of config_path's snapshot, loading manifests through client.

    Each build reads the credential names afresh.
    """

    def build() -> Snapshot:
        return build_snapshot(
            config_path,
            credentials.names(),
            lambda reference: plugins.load_manifest(client, reference),
        )

    return build


class Instance:
    """One proxy instance's running parts, and the app serving its MCP surface."""

    def __init__(
        self,
        app: StarletteWithLifespan,
        control: ControlServer,
        sweeper: OverflowSweeper,
        audit: AuditWriter,
        removed_cages: tuple[str, ...],
        warnings: tuple[str, ...],
    ) -> None:
        """Hold the app, the left cages removed, the warnings, and what close stops."""
        self.app = app
        self.removed_cages = removed_cages
        self.warnings = warnings
        self._control = control
        self._sweeper = sweeper
        self._audit = audit

    @classmethod
    def start(cls, state_dir: Path, config_path: Path, client: DockerClient) -> Self:
        """Open the stores under state_dir, then start the config and its servers.

        The order is the stores, the verifier, the daemon's limits, the left
        cages, the live config, the control socket, and the overflow sweeper.
        The verifier step pulls a verifier pinned by digest when the daemon
        lacks it. The left-cages step force-removes every cage an earlier
        instance left on the daemon, and the instance holds the name of each
        cage it removed. The control socket serves reloads of the live config
        and the gateway's checks. A step that fails closes what the steps
        before it started, then its error propagates. The instance's warnings
        are one for each cage limit the daemon cannot enforce, then one for
        each left cage that could not be removed, then those of the first
        snapshot.

        Raises:
            PluginImageError: a verifier pinned by digest cannot be looked up
                or pulled.
            CageFailedError: the left cages cannot be listed.
            RunnerError: the daemon's info cannot be read.
            ProxyError: the audit log cannot be prepared, the config cannot be
                built, its egress cannot be made ready, or the control socket
                cannot be bound.
        """
        tokens = TokenStore(state_dir / "tokens.sqlite3")
        credentials = CredentialStore(state_dir / "credentials")
        audit = AuditWriter(state_dir / "audit")
        build = snapshot_builder(config_path, credentials, client)
        with contextlib.ExitStack() as started:
            started.callback(audit.close)
            ensure_verifier(client)
            daemon_warnings = limit_warnings(client)
            removal = remove_left_cages(client)
            live = LiveConfig.start(build, EgressProxies(client, state_dir / "egress"))
            runner = CageRunner(client, credentials, CageLimiter())
            gateway = CallGateway(
                tokens,
                live,
                runner,
                SelfCheck(client, credentials, runner),
                audit,
                _utc_now,
            )
            control = ControlServer(
                state_dir / CONTROL_SOCKET_NAME, live, build, gateway.check_scopes
            )
            control.start()
            started.callback(control.close)
            sweeper = OverflowSweeper(
                lambda: live.current.config.overflow_dir, _utc_now
            )
            sweeper.start()
            started.callback(sweeper.stop)
            instance = cls(
                build_app(gateway),
                control,
                sweeper,
                audit,
                removal.removed,
                (*daemon_warnings, *removal.warnings, *live.current.warnings),
            )
            started.pop_all()
        return instance

    def close(self) -> None:
        """Close the control socket, stop the sweeper, and close the audit writer."""
        self._control.close()
        self._sweeper.stop()
        self._audit.close()


class _Server(uvicorn.Server):
    """A uvicorn server that stops on SIGINT or SIGTERM and re-raises neither.

    uvicorn's own capture_signals re-raises each signal it caught once the
    server has shut down, which ends the process before its instance closes.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """On the main thread, route SIGINT and SIGTERM to handle_exit, then restore."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {
            handled: signal.signal(handled, self.handle_exit)
            for handled in _HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for handled, handler in previous.items():
                signal.signal(handled, handler)


def _utc_now() -> datetime:
    return datetime.now(UTC)
