"""The call path of the MCP surface: list, check, run, and audit each tool call."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from agent_proxy.audit import AuditError, AuditRecord, AuditWriter
from agent_proxy.auth import TokenRecord, TokenStore, TokenStoreError
from agent_proxy.egress import cage_network
from agent_proxy.errors import ProxyError
from agent_proxy.reload import LiveConfig
from agent_proxy.results import new_call_id
from agent_proxy.runner import CageCall, CageRunner, Outcome

REFUSAL_PREFIX = "agent-proxy refused: "
_BEARER_SCHEME = "bearer"
_UNAUTHENTICATED_REFUSAL = "the bearer token is missing, unknown, revoked, or expired"
_STORE_UNREADABLE_REFUSAL = "the token store could not be read"


class CallRefusedError(ProxyError):
    """The proxy refuses a call; str() is the refusal without REFUSAL_PREFIX."""


@dataclass(frozen=True)
class ToolSpec:
    """One listed tool: its name and its input schema."""

    name: str
    input_schema: Mapping[str, object]


@dataclass(frozen=True)
class Reply:
    """A call's result, and whether it is an error result."""

    content: Mapping[str, object]
    is_error: bool


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

    def refusal_line(self, token: TokenRecord | None, refusal: str) -> AuditRecord:
        """Return the audit record of a call refused now with refusal."""
        return AuditRecord(
            timestamp=self.started_at,
            call_id=self.call_id,
            token_name=None if token is None else token.name,
            scope=None if token is None else token.scope,
            action=self.tool,
            arguments=self.arguments,
            exit_code=None,
            duration_ms=round((time.monotonic() - self.started) * 1000),
            stdout_bytes=None,
            stderr_bytes=None,
            truncated=False,
            refusal=refusal,
        )


class CallGateway:
    """Lists the tools a bearer token's scope grants, and runs each call of one.

    Every call is refused or run, then audited in one line. A call whose line
    cannot be written is refused, and its result is withheld.
    """

    def __init__(
        self,
        tokens: TokenStore,
        live: LiveConfig,
        runner: CageRunner,
        audit: AuditWriter,
        now: Callable[[], datetime],
    ) -> None:
        """Hold the stores and collaborators; now returns the current time in UTC."""
        self._tokens = tokens
        self._live = live
        self._runner = runner
        self._audit = audit
        self._now = now

    def list_tools(self, authorization: str | None) -> list[ToolSpec]:
        """Return one spec per grant of the token's scope, sorted by name.

        The token is verified and its use is not recorded. The list is empty
        when the token does not verify, when its scope is not defined in the
        config in force, or when the token store cannot be read.
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
        scope = snapshot.config.scopes.get(record.scope)
        if scope is None:
            return []
        specs = []
        for key in scope.grants:
            plugin, action = key.split(".", 1)
            schema = snapshot.manifests[plugin].actions[action].input_schema
            specs.append(ToolSpec(f"{plugin}_{action}", schema))
        return sorted(specs, key=lambda spec: spec.name)

    def call(
        self, authorization: str | None, tool: str, arguments: Mapping[str, object]
    ) -> Reply:
        """Run one call of tool with arguments, audit it, and return its reply.

        Raises:
            CallRefusedError: the call is refused, or its audit line cannot be
                written; its message is the refusal without REFUSAL_PREFIX.
        """
        call = _Call(
            call_id=new_call_id(),
            tool=tool,
            arguments=arguments,
            started_at=self._now(),
            started=time.monotonic(),
        )
        if not self._audit.available:
            raise CallRefusedError(_audit_refusal(call.call_id))
        try:
            token = self._authenticate(authorization)
        except CallRefusedError as error:
            self._refuse(call, None, error)
        try:
            outcome = self._run(call, token.scope)
        except ProxyError as error:
            self._refuse(call, token, error)
        self._write(call.outcome_line(token, outcome))
        return Reply(outcome.content, is_error=outcome.script_failed)

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
                raise CallRefusedError(
                    f"scope {scope_name!r} is not defined in the config in force"
                )
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
                    action=snapshot.manifests[plugin].actions[action],
                    grant=grant,
                    arguments=call.arguments,
                    network=cage_network(grant.domains),
                    overflow_dir=snapshot.config.overflow_dir,
                    max_cages=snapshot.config.max_cages,
                )
            )

    def _refuse(
        self, call: _Call, token: TokenRecord | None, error: ProxyError
    ) -> NoReturn:
        """Audit call as refused with error's text, then raise that refusal.

        Raises:
            CallRefusedError: always; the audit refusal when the line cannot be
                written.
        """
        refusal = str(error)
        self._write(call.refusal_line(token, refusal))
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
