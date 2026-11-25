"""Provided credentials: each value a provider action gives, cached until it is due."""

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from agent_proxy.audit import AuditError, AuditRecord, AuditWriter, audit_refusal
from agent_proxy.config import Grant, InstanceConfig, ProvidedCredential
from agent_proxy.egress import cage_network
from agent_proxy.reload import Snapshot
from agent_proxy.results import OUTPUT_CAP_BYTES, new_call_id
from agent_proxy.runner import (
    CageCall,
    CageRunner,
    CredentialError,
    Outcome,
    RunnerError,
    check_environment_value,
)

_OUTPUT_KEYS = frozenset({"value", "expires_at"})


class ProvisionError(CredentialError):
    """A provided credential has no value a call can use.

    Its provider failed and no unexpired value is cached, or the provider's
    audit line could not be written. No message holds a value.
    """


@dataclass(frozen=True)
class ProvidedValue:
    """A provided credential's value and the moment it expires."""

    value: str = field(repr=False)
    expires_at: datetime


def parse_provider_output(
    stdout: bytes, fetched_at: datetime, max_age: timedelta | None
) -> ProvidedValue:
    """Return the value a provider's stdout gives, and its expiry.

    The output is a UTF-8 JSON object with a non-empty string value, an
    optional string expires_at with a time zone, and no other key. The expiry
    is the earlier of expires_at and fetched_at plus max_age.

    Raises:
        ProvisionError: the output breaks that form, gives no expiry and
            max_age is None, or its value cannot be an environment value. The
            message never holds the output.
    """
    document = _output_document(stdout)
    value = document.get("value")
    if not isinstance(value, str) or not value:
        raise ProvisionError("its output's 'value' is not a non-empty string")
    try:
        check_environment_value("value", value)
    except CredentialError:
        raise ProvisionError(
            "its output's 'value' is not valid UTF-8 or holds a NUL, so it cannot"
            " be an environment value"
        ) from None
    expiries = [] if max_age is None else [fetched_at + max_age]
    if "expires_at" in document:
        expiries.append(_expires_at(document["expires_at"]))
    if not expiries:
        raise ProvisionError(
            "its output gives no 'expires_at' and the entry sets no max_age"
        )
    return ProvidedValue(value=value, expires_at=min(expiries))


def _output_document(stdout: bytes) -> Mapping[str, object]:
    """Return the JSON object stdout holds.

    Raises:
        ProvisionError: stdout is not UTF-8 JSON, is not an object, or holds a
            key other than value and expires_at.
    """
    try:
        document = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # These errors quote or carry the output, which may hold the value.
        raise ProvisionError("its output is not UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise ProvisionError("its output is not a JSON object")
    extra = sorted(document.keys() - _OUTPUT_KEYS)
    if extra:
        raise ProvisionError(
            f"its output holds keys other than 'value' and 'expires_at': {extra!r}"
        )
    return cast("dict[str, object]", document)


def _expires_at(text: object) -> datetime:
    """Return the moment an output's expires_at names.

    Raises:
        ProvisionError: text is not a string, not an ISO 8601 date and time,
            or carries no time zone.
    """
    if not isinstance(text, str):
        raise ProvisionError("its output's 'expires_at' is not a string")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ProvisionError(
            "its output's 'expires_at' is not an ISO 8601 date and time"
        ) from None
    if moment.utcoffset() is None:
        raise ProvisionError("its output's 'expires_at' carries no time zone")
    return moment


@dataclass(frozen=True)
class _Definition:
    """What a cached value came from: its entry and its provider plugin's image."""

    entry: ProvidedCredential
    image: str

    @classmethod
    def of(cls, config: InstanceConfig, entry: ProvidedCredential) -> "_Definition":
        """Return entry's definition under config."""
        plugin = entry.provider.partition(".")[0]
        return cls(entry=entry, image=config.plugins[plugin])


@dataclass(frozen=True)
class _Cached:
    definition: _Definition
    provided: ProvidedValue


@dataclass(frozen=True)
class _RunStart:
    """One provider run as started: its ID, its credential, and its start."""

    call_id: str
    credential: str
    action: str
    fetched_at: datetime
    started: float

    def outcome_line(self, outcome: Outcome, failure: str | None) -> AuditRecord:
        """Return the audit record of a run whose cage ran."""
        return AuditRecord(
            timestamp=self.fetched_at,
            call_id=self.call_id,
            token_name=None,
            scope=None,
            action=self.action,
            arguments=None,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            stdout_bytes=outcome.stdout_bytes,
            stderr_bytes=outcome.stderr_bytes,
            truncated=outcome.truncated,
            refusal=failure,
            credential=self.credential,
        )

    def refused_line(self, failure: str) -> AuditRecord:
        """Return the audit record of a run the runner refused or failed."""
        return AuditRecord(
            timestamp=self.fetched_at,
            call_id=self.call_id,
            token_name=None,
            scope=None,
            action=self.action,
            arguments=None,
            exit_code=None,
            duration_ms=round((time.monotonic() - self.started) * 1000),
            stdout_bytes=None,
            stderr_bytes=None,
            truncated=False,
            refusal=failure,
            credential=self.credential,
        )


class ProvidedCredentials:
    """The instance's cache of provided values, and the provider runs that fill it.

    A value is fresh until refresh_before before its expiry, usable until its
    expiry, and never handed out at or after it. One run at a time refreshes
    each credential; runs of different credentials go side by side.
    """

    def __init__(
        self, runner: CageRunner, audit: AuditWriter, now: Callable[[], datetime]
    ) -> None:
        """Hold the runner and the audit writer; now returns the time in UTC."""
        self._runner = runner
        self._audit = audit
        self._now = now
        self._state_lock = threading.Lock()
        self._cached: dict[str, _Cached] = {}
        self._refresh_locks: dict[str, threading.Lock] = {}

    def resolve(self, snapshot: Snapshot, grant: Grant) -> dict[str, str]:
        """Return the value of each provided credential grant names, by name.

        A fresh cached value is returned with no run. Otherwise the provider
        runs once, under that credential's lock, and its line is audited. A
        failed run falls back to the cached value while it has not expired. A
        cached value whose entry or provider image differs from snapshot's
        counts as absent.

        Raises:
            ProvisionError: a run failed and no unexpired value is cached; or
                a run's audit line could not be written, which withholds its
                value.
        """
        provided = snapshot.config.provided_credentials
        return {
            name: self._value(snapshot, name)
            for name in grant.credentials
            if name in provided
        }

    def retain(self, snapshot: Snapshot) -> None:
        """Drop each cached value whose definition snapshot does not hold."""
        config = snapshot.config
        with self._state_lock:
            self._cached = {
                name: cached
                for name, cached in self._cached.items()
                if name in config.provided_credentials
                and cached.definition
                == _Definition.of(config, config.provided_credentials[name])
            }

    def _value(self, snapshot: Snapshot, name: str) -> str:
        """Return name's value, refreshing it when it is not fresh.

        Raises:
            ProvisionError: as resolve gives.
        """
        config = snapshot.config
        definition = _Definition.of(config, config.provided_credentials[name])
        cached = self._lookup(name, definition)
        if cached is not None and self._is_fresh(cached, definition.entry):
            return cached.value
        with self._refresh_lock(name):
            # Another call may have refreshed the value while this one waited.
            cached = self._lookup(name, definition)
            if cached is not None and self._is_fresh(cached, definition.entry):
                return cached.value
            return self._refresh(snapshot, name, definition, cached)

    def _lookup(self, name: str, definition: _Definition) -> ProvidedValue | None:
        with self._state_lock:
            cached = self._cached.get(name)
        if cached is None or cached.definition != definition:
            return None
        return cached.provided

    def _is_fresh(self, provided: ProvidedValue, entry: ProvidedCredential) -> bool:
        return self._now() < provided.expires_at - entry.refresh_before

    def _refresh_lock(self, name: str) -> threading.Lock:
        with self._state_lock:
            return self._refresh_locks.setdefault(name, threading.Lock())

    def _refresh(
        self,
        snapshot: Snapshot,
        name: str,
        definition: _Definition,
        cached: ProvidedValue | None,
    ) -> str:
        """Run name's provider once, audit the run, and return the value to use.

        Raises:
            ProvisionError: as resolve gives.
        """
        line, provided = self._run(snapshot, name, definition)
        try:
            self._audit.write(line)
        except AuditError as error:
            raise ProvisionError(audit_refusal(line.call_id)) from error
        if provided is not None:
            with self._state_lock:
                self._cached[name] = _Cached(definition=definition, provided=provided)
            return provided.value
        if cached is not None and self._now() < cached.expires_at:
            return cached.value
        raise ProvisionError(
            f"provided credential {name!r} has no unexpired value: provider"
            f" {definition.entry.provider!r} failed: {line.refusal}"
        )

    def _run(
        self, snapshot: Snapshot, name: str, definition: _Definition
    ) -> tuple[AuditRecord, ProvidedValue | None]:
        """Run name's provider and return its audit line and its value.

        The value is None when the run failed; the line's refusal says why.
        """
        plugin, _, action = definition.entry.provider.partition(".")
        start = _RunStart(
            call_id=new_call_id(),
            credential=name,
            action=f"{plugin}_{action}",
            fetched_at=self._now(),
            started=time.monotonic(),
        )
        try:
            outcome = self._runner.run(_cage_call(snapshot, start.call_id, definition))
        except RunnerError as error:
            return start.refused_line(str(error)), None
        try:
            provided = self._judge(outcome, start.fetched_at, definition.entry.max_age)
        except ProvisionError as error:
            return start.outcome_line(outcome, str(error)), None
        return start.outcome_line(outcome, None), provided

    def _judge(
        self, outcome: Outcome, fetched_at: datetime, max_age: timedelta | None
    ) -> ProvidedValue:
        """Return the unexpired value a provider's outcome gives.

        Raises:
            ProvisionError: the run exited non-zero, its stdout passed the cap,
                parse_provider_output refuses its output, or its value has
                already expired.
        """
        if outcome.exit_code != 0:
            raise ProvisionError(
                f"it exited {outcome.exit_code}, stderr {outcome.content['stderr']!r}"
            )
        stdout = cast("bytes | None", outcome.content["stdout"])
        if stdout is None:
            raise ProvisionError(
                f"its stdout passed the output cap of {OUTPUT_CAP_BYTES} bytes"
            )
        provided = parse_provider_output(stdout, fetched_at, max_age)
        if not self._now() < provided.expires_at:
            raise ProvisionError(
                f"the value it gave expired at {provided.expires_at.isoformat()}"
            )
        return provided


def _cage_call(snapshot: Snapshot, call_id: str, definition: _Definition) -> CageCall:
    """Return the cage call that runs definition's provider, with no roots."""
    entry = definition.entry
    plugin, _, action = entry.provider.partition(".")
    return CageCall(
        call_id=call_id,
        image=definition.image,
        action=snapshot.manifests[plugin].actions[action],
        # Grant's validators take a duration only as text; the entry's is parsed.
        grant=Grant.model_construct(
            roots=[],
            domains=entry.domains,
            credentials=entry.credentials,
            env=entry.env,
            timeout=entry.timeout,
            memory=None,
        ),
        arguments={},
        network=cage_network(entry.domains),
        overflow_dir=snapshot.config.overflow_dir,
        max_cages=snapshot.config.max_cages,
    )
