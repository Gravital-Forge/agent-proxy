import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
from audit_faults import fail_next_write_after_half

from agent_proxy.audit import AuditWriter, audit_refusal, tail
from agent_proxy.config import EnvMount, FileMount, Grant, InstanceConfig
from agent_proxy.egress import cage_network
from agent_proxy.plugins import parse_manifest
from agent_proxy.providers import (
    ProvidedCredentials,
    ProvidedValue,
    ProvisionError,
    parse_provider_output,
)
from agent_proxy.reload import Snapshot
from agent_proxy.results import CALL_ID_PATTERN, OUTPUT_CAP_BYTES
from agent_proxy.runner import (
    CageCall,
    CageTimeoutError,
    CredentialError,
    Outcome,
    RunnerError,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
REFRESH_BEFORE = timedelta(minutes=10)
SECRET = "issued-SECRET-value"
AGENT_TREE = "/srv/agents/alpha"
OVERFLOW_DIR = f"{AGENT_TREE}/overflow"
MAX_CAGES = 3
ISSUER_IMAGE = "ghcr.io/example/issuer@sha256:" + "c" * 64
OTHER_IMAGE = "ghcr.io/example/issuer@sha256:" + "d" * 64
PROVIDER = "issuer.issue"
WAIT_SECONDS = 10

ISSUER_MANIFEST = parse_manifest(
    json.dumps(
        {
            "actions": {
                "issue": {
                    "kind": "provider",
                    "credentials": [{"file": "/run/credentials/app-key"}],
                    "timeout": "30s",
                    "executable": "/plugin/bin/issue",
                },
            }
        }
    ).encode()
)


def _entry(**overrides: object) -> dict[str, object]:
    return {
        "provider": PROVIDER,
        "credentials": {"app-key": {"file": "/run/credentials/app-key"}},
        "env": {"APP_ID": "42"},
        "domains": ["api.example.com"],
        "refresh_before": "10m",
    } | overrides


def _snapshot(image: str = ISSUER_IMAGE, **entries: dict[str, object]) -> Snapshot:
    config = InstanceConfig.model_validate(
        {
            "agent_tree": AGENT_TREE,
            "overflow_dir": OVERFLOW_DIR,
            "max_cages": MAX_CAGES,
            "plugins": {"issuer": image},
            "scopes": {},
            "provided_credentials": entries or {"app-token": _entry()},
        }
    )
    return Snapshot(
        config=config,
        manifests=MappingProxyType({"issuer": ISSUER_MANIFEST}),
        warnings=(),
    )


SNAPSHOT = _snapshot()
GRANT = Grant.model_validate(
    {
        "roots": [],
        "domains": [],
        "credentials": {
            "app-token": {"env": "TOKEN"},
            "stored-token": {"env": "OTHER"},
        },
    }
)


def _stdout(value: str = SECRET, expires_at: datetime | None = NOW + HOUR) -> bytes:
    document: dict[str, object] = {"value": value}
    if expires_at is not None:
        document["expires_at"] = expires_at.isoformat()
    return json.dumps(document).encode()


def _outcome(
    stdout: bytes | None,
    exit_code: int = 0,
    stderr: str = "",
    stdout_bytes: int | None = None,
) -> Outcome:
    return Outcome(
        content={"exit_code": exit_code, "stdout": stdout, "stderr": stderr},
        exit_code=exit_code,
        duration_ms=12,
        stdout_bytes=len(stdout or b"") if stdout_bytes is None else stdout_bytes,
        stderr_bytes=len(stderr.encode()),
        truncated=stdout is None,
    )


def _issued(value: str = SECRET, expires_at: datetime | None = NOW + HOUR) -> Outcome:
    return _outcome(_stdout(value, expires_at))


class Clock:
    """A settable clock; hook, when set, runs on each reading."""

    def __init__(self, moment: datetime) -> None:
        """Start at moment."""
        self.moment = moment
        self.hook: Callable[[], None] | None = None

    def __call__(self) -> datetime:
        """Run the hook, if any, then return the current moment."""
        if self.hook is not None:
            self.hook()
        return self.moment


class FakeRunner:
    """A cage runner that records each call and answers from respond."""

    def __init__(self) -> None:
        """Answer _issued() until respond is changed."""
        self.calls: list[CageCall] = []
        self.respond: Callable[[CageCall], Outcome] = lambda _call: _issued()
        self._lock = threading.Lock()

    def run(self, call: CageCall) -> Outcome:
        """Record call, then return or raise what respond gives for it."""
        with self._lock:
            self.calls.append(call)
        return self.respond(call)


def _answer(*results: Outcome | RunnerError) -> Callable[[CageCall], Outcome]:
    """Return a respond that gives results in order, raising each error."""
    remaining = list(results)

    def respond(_call: CageCall) -> Outcome:
        result = remaining.pop(0)
        if isinstance(result, RunnerError):
            raise result
        return result

    return respond


@dataclass
class Harness:
    provided: ProvidedCredentials
    runner: FakeRunner
    clock: Clock
    audit_dir: Path


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Return a cache over a fake runner, a real audit writer, and a fake clock."""
    runner = FakeRunner()
    clock = Clock(NOW)
    audit_dir = tmp_path / "audit"
    return Harness(
        provided=ProvidedCredentials(runner, AuditWriter(audit_dir), clock),
        runner=runner,
        clock=clock,
        audit_dir=audit_dir,
    )


def _audit_text(harness: Harness) -> str:
    return "\n".join(tail(harness.audit_dir, 1000))


def _audit_lines(harness: Harness) -> list[dict[str, object]]:
    return [json.loads(line) for line in tail(harness.audit_dir, 1000)]


def _refusal(harness: Harness, snapshot: Snapshot = SNAPSHOT) -> str:
    with pytest.raises(ProvisionError) as caught:
        harness.provided.resolve(snapshot, GRANT)
    return str(caught.value)


def _no_value(failure: str) -> str:
    return (
        f"provided credential 'app-token' has no unexpired value: provider"
        f" {PROVIDER!r} failed: {failure}"
    )


def _document(**members: object) -> bytes:
    return json.dumps(members).encode()


def _wait(event: threading.Event) -> None:
    assert event.wait(WAIT_SECONDS), "the other thread did not arrive in time"


def test_a_provision_error_is_a_credential_error() -> None:
    """The gateway refuses a provider failure as it refuses a credential."""
    assert issubclass(ProvisionError, CredentialError)


def test_output_with_value_and_expiry_parses() -> None:
    """A value and an expires_at give the value and that expiry."""
    parsed = parse_provider_output(_stdout(), NOW, None)

    assert parsed == ProvidedValue(value=SECRET, expires_at=NOW + HOUR)


def test_a_provided_value_s_repr_omits_the_value() -> None:
    """A provided value printed in a log or a traceback shows no secret."""
    assert SECRET not in repr(ProvidedValue(value=SECRET, expires_at=NOW))


@pytest.mark.parametrize(
    ("max_age", "expected"),
    [
        pytest.param(timedelta(minutes=30), NOW + timedelta(minutes=30), id="max-age"),
        pytest.param(2 * HOUR, NOW + HOUR, id="expires-at"),
    ],
)
def test_the_expiry_is_the_earlier_of_expires_at_and_max_age(
    max_age: timedelta, expected: datetime
) -> None:
    """With both an expires_at and a max_age, the earlier expiry wins."""
    assert parse_provider_output(_stdout(), NOW, max_age).expires_at == expected


def test_max_age_alone_sets_the_expiry_from_the_fetch_time() -> None:
    """Output with no expires_at expires max_age after the fetch."""
    parsed = parse_provider_output(_stdout(expires_at=None), NOW, HOUR)

    assert parsed.expires_at == NOW + HOUR


def test_an_expires_at_in_another_time_zone_parses() -> None:
    """Any time zone is accepted, and compares as the same moment."""
    parsed = parse_provider_output(
        _document(value=SECRET, expires_at="2026-11-24T14:00:00+01:00"), NOW, None
    )

    assert parsed.expires_at == NOW + HOUR


@pytest.mark.parametrize(
    ("stdout", "max_age", "failure"),
    [
        pytest.param(
            b"\xff" + SECRET.encode(),
            HOUR,
            "its output is not UTF-8 JSON",
            id="not-utf8",
        ),
        pytest.param(
            f"value={SECRET}".encode(),
            HOUR,
            "its output is not UTF-8 JSON",
            id="not-json",
        ),
        pytest.param(
            b"[" * 100_000, HOUR, "its output is not UTF-8 JSON", id="too-deep"
        ),
        pytest.param(
            json.dumps([SECRET]).encode(),
            HOUR,
            "its output is not a JSON object",
            id="not-object",
        ),
        pytest.param(
            _document(value=SECRET, token_type="bearer"),
            HOUR,
            "its output holds keys other than 'value' and 'expires_at': ['token_type']",
            id="extra-key",
        ),
        pytest.param(
            _document(expires_at=(NOW + HOUR).isoformat()),
            HOUR,
            "its output's 'value' is not a non-empty string",
            id="no-value",
        ),
        pytest.param(
            _document(value=""),
            HOUR,
            "its output's 'value' is not a non-empty string",
            id="empty-value",
        ),
        pytest.param(
            _document(value={"token": SECRET}),
            HOUR,
            "its output's 'value' is not a non-empty string",
            id="value-not-string",
        ),
        pytest.param(
            _document(value=SECRET, expires_at=1_700_000_000),
            HOUR,
            "its output's 'expires_at' is not a string",
            id="expires-at-not-string",
        ),
        pytest.param(
            _document(value=SECRET, expires_at="in an hour"),
            HOUR,
            "its output's 'expires_at' is not an ISO 8601 date and time",
            id="expires-at-not-iso",
        ),
        pytest.param(
            _document(value=SECRET, expires_at="2026-11-24T13:00:00"),
            HOUR,
            "its output's 'expires_at' carries no time zone",
            id="expires-at-naive",
        ),
        pytest.param(
            _document(value=SECRET),
            None,
            "its output gives no 'expires_at' and the entry sets no max_age",
            id="no-expiry",
        ),
        pytest.param(
            _document(value=f"{SECRET}\0"),
            HOUR,
            "its output's 'value' is not valid UTF-8 or holds a NUL, so it cannot"
            " be an environment value",
            id="nul",
        ),
        pytest.param(
            b'{"value": "' + SECRET.encode() + b'\\ud800"}',
            HOUR,
            "its output's 'value' is not valid UTF-8 or holds a NUL, so it cannot"
            " be an environment value",
            id="lone-surrogate",
        ),
    ],
)
def test_output_that_breaks_the_contract_is_refused_without_the_value(
    stdout: bytes, max_age: timedelta | None, failure: str
) -> None:
    """Each broken rule has its own message, and no error carries the output."""
    with pytest.raises(ProvisionError) as caught:
        parse_provider_output(stdout, NOW, max_age)

    assert str(caught.value) == failure
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_grant_naming_no_provided_credential_resolves_to_nothing(
    harness: Harness,
) -> None:
    """A grant of stored credentials alone runs no provider."""
    grant = Grant.model_validate(
        {"roots": [], "domains": [], "credentials": {"stored-token": {"env": "X"}}}
    )

    assert harness.provided.resolve(SNAPSHOT, grant) == {}
    assert harness.runner.calls == []


def test_the_first_resolve_runs_the_provider_and_maps_its_value(
    harness: Harness,
) -> None:
    """The map holds the provided credential only, never a stored one."""
    assert harness.provided.resolve(SNAPSHOT, GRANT) == {"app-token": SECRET}
    assert len(harness.runner.calls) == 1


def test_the_provider_runs_in_a_cage_call_built_from_its_entry(
    harness: Harness,
) -> None:
    """The cage has no roots, and the entry's domains, credentials, and env."""
    harness.provided.resolve(SNAPSHOT, GRANT)

    [call] = harness.runner.calls
    assert re.fullmatch(CALL_ID_PATTERN, call.call_id)
    assert call.image == ISSUER_IMAGE
    assert call.action == ISSUER_MANIFEST.actions["issue"]
    assert call.arguments == {}
    assert call.network == cage_network(["api.example.com"])
    assert call.overflow_dir == OVERFLOW_DIR
    assert call.max_cages == MAX_CAGES
    assert dict(call.provided) == {}
    assert call.grant.roots == []
    assert call.grant.domains == ["api.example.com"]
    assert call.grant.credentials == {
        "app-key": FileMount(file="/run/credentials/app-key")
    }
    assert call.grant.env == {"APP_ID": "42"}
    assert call.grant.timeout is None
    assert call.grant.memory is None


def test_the_provider_cage_takes_the_entry_s_timeout(harness: Harness) -> None:
    """An entry's timeout reaches the runner as the grant's."""
    harness.provided.resolve(_snapshot(**{"app-token": _entry(timeout="45s")}), GRANT)

    [call] = harness.runner.calls
    assert call.grant.timeout == timedelta(seconds=45)


def test_a_provider_env_credential_is_passed_to_its_cage(harness: Harness) -> None:
    """An entry's stored env credential keeps its env form."""
    snapshot = _snapshot(
        **{"app-token": _entry(credentials={"vault-auth": {"env": "VAULT_AUTH"}})}
    )

    harness.provided.resolve(snapshot, GRANT)

    [call] = harness.runner.calls
    assert call.grant.credentials == {"vault-auth": EnvMount(env="VAULT_AUTH")}


def test_each_run_has_a_fresh_call_id(harness: Harness) -> None:
    """Two runs of one credential carry two call IDs."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - REFRESH_BEFORE
    harness.provided.resolve(SNAPSHOT, GRANT)

    first, second = harness.runner.calls
    assert first.call_id != second.call_id


def test_a_fresh_value_is_returned_with_no_run(harness: Harness) -> None:
    """Until refresh_before before its expiry, the cached value serves."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - REFRESH_BEFORE - timedelta(microseconds=1)

    assert harness.provided.resolve(SNAPSHOT, GRANT) == {"app-token": SECRET}
    assert len(harness.runner.calls) == 1


def test_a_value_within_refresh_before_of_its_expiry_is_refreshed(
    harness: Harness,
) -> None:
    """At refresh_before before its expiry, the provider runs again."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - REFRESH_BEFORE
    harness.runner.respond = _answer(_issued("second-value", NOW + 2 * HOUR))

    assert harness.provided.resolve(SNAPSHOT, GRANT) == {"app-token": "second-value"}
    assert len(harness.runner.calls) == 2


def test_a_successful_refresh_replaces_the_entry(harness: Harness) -> None:
    """The refreshed value and its expiry serve the calls after it."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - REFRESH_BEFORE
    harness.runner.respond = _answer(_issued("second-value", NOW + 2 * HOUR))
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR

    assert harness.provided.resolve(SNAPSHOT, GRANT) == {"app-token": "second-value"}
    assert len(harness.runner.calls) == 2


def test_a_failed_refresh_serves_the_unexpired_value_and_the_next_call_retries(
    harness: Harness,
) -> None:
    """A failure before expiry serves the cached value; the next call runs again."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - timedelta(microseconds=1)
    harness.runner.respond = _answer(
        _outcome(None, exit_code=1, stderr="refused"), _issued("second-value")
    )

    assert harness.provided.resolve(SNAPSHOT, GRANT) == {"app-token": SECRET}
    assert harness.provided.resolve(SNAPSHOT, GRANT) == {"app-token": "second-value"}
    assert len(harness.runner.calls) == 3


def test_a_failed_refresh_leaves_the_entry_as_it_was(harness: Harness) -> None:
    """A failed refresh neither extends nor drops the cached value."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - REFRESH_BEFORE
    harness.runner.respond = _answer(_outcome(b"{}"), _outcome(b"{}"))
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR

    assert _refusal(harness) == _no_value(
        "its output's 'value' is not a non-empty string"
    )


@pytest.mark.parametrize(
    ("result", "failure"),
    [
        pytest.param(
            CageTimeoutError(
                "action 'issue' did not finish within its timeout of 30 seconds"
            ),
            "action 'issue' did not finish within its timeout of 30 seconds",
            id="runner-error",
        ),
        pytest.param(
            _outcome(_stdout(), exit_code=1, stderr="401 Bad credentials\n"),
            "it exited 1, stderr '401 Bad credentials\\n'",
            id="non-zero-exit",
        ),
        pytest.param(
            _outcome(None, stdout_bytes=OUTPUT_CAP_BYTES + 1),
            f"its stdout passed the output cap of {OUTPUT_CAP_BYTES} bytes",
            id="past-the-cap",
        ),
        pytest.param(
            _outcome(b"[]"), "its output is not a JSON object", id="refused-output"
        ),
        pytest.param(
            _issued(expires_at=NOW),
            f"the value it gave expired at {NOW.isoformat()}",
            id="expired-on-arrival",
        ),
    ],
)
def test_a_failed_run_with_no_cached_value_is_refused_by_name(
    harness: Harness, result: Outcome | RunnerError, failure: str
) -> None:
    """The refusal names the credential, the provider, and the failure."""
    harness.runner.respond = _answer(result)

    refusal = _refusal(harness)

    assert refusal == _no_value(failure)
    assert SECRET not in refusal


def test_no_value_is_returned_at_its_expiry(harness: Harness) -> None:
    """A failed refresh at the expiry has no value to fall back on."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR
    harness.runner.respond = _answer(_outcome(None, exit_code=1, stderr="down"))

    assert _refusal(harness) == _no_value("it exited 1, stderr 'down'")


def test_an_entry_from_another_definition_counts_as_absent(harness: Harness) -> None:
    """A changed entry runs its provider, though the cached value is fresh."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    changed = _snapshot(**{"app-token": _entry(env={"APP_ID": "43"})})
    harness.runner.respond = _answer(_issued("second-value"))

    assert harness.provided.resolve(changed, GRANT) == {"app-token": "second-value"}
    assert len(harness.runner.calls) == 2


def test_an_entry_from_another_plugin_image_counts_as_absent(
    harness: Harness,
) -> None:
    """A changed provider image never falls back on the old image's value."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.runner.respond = _answer(_outcome(b"[]"))

    assert _refusal(harness, _snapshot(OTHER_IMAGE)) == _no_value(
        "its output is not a JSON object"
    )


def test_retain_drops_an_entry_whose_definition_the_snapshot_lacks(
    harness: Harness,
) -> None:
    """After a reload that changes an entry, its old value is gone."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.provided.retain(_snapshot(**{"app-token": _entry(refresh_before="5m")}))

    harness.provided.resolve(SNAPSHOT, GRANT)

    assert len(harness.runner.calls) == 2


def test_retain_drops_an_entry_the_snapshot_no_longer_names(
    harness: Harness,
) -> None:
    """After a reload that removes an entry, its old value is gone."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.provided.retain(_snapshot(**{"other-token": _entry()}))

    harness.provided.resolve(SNAPSHOT, GRANT)

    assert len(harness.runner.calls) == 2


def test_retain_keeps_an_entry_whose_definition_is_unchanged(
    harness: Harness,
) -> None:
    """A reload that leaves an entry as it was keeps its value."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.provided.retain(_snapshot())

    harness.provided.resolve(SNAPSHOT, GRANT)

    assert len(harness.runner.calls) == 1


def test_a_successful_run_writes_one_audit_line_without_the_value(
    harness: Harness,
) -> None:
    """The line names the credential and the provider, with no token or value."""
    harness.provided.resolve(SNAPSHOT, GRANT)

    [call] = harness.runner.calls
    [line] = _audit_lines(harness)
    assert line == {
        "timestamp": NOW.isoformat(),
        "call_id": call.call_id,
        "token_name": None,
        "scope": None,
        "action": "issuer_issue",
        "arguments": None,
        "arguments_cut": None,
        "exit_code": 0,
        "duration_ms": 12,
        "stdout_bytes": len(_stdout()),
        "stderr_bytes": 0,
        "truncated": False,
        "refusal": None,
        "credential": "app-token",
    }
    assert SECRET not in _audit_text(harness)


def test_a_fresh_value_writes_no_audit_line(harness: Harness) -> None:
    """Only a provider run writes a line."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.provided.resolve(SNAPSHOT, GRANT)

    assert len(_audit_lines(harness)) == 1


def test_a_failed_run_s_audit_line_carries_its_failure(harness: Harness) -> None:
    """A non-zero exit's line holds its exit code and failure, not its stdout."""
    harness.runner.respond = _answer(
        _outcome(_stdout(), exit_code=1, stderr="401 Bad credentials")
    )
    _refusal(harness)

    [line] = _audit_lines(harness)
    assert line["exit_code"] == 1
    assert line["stderr_bytes"] == len("401 Bad credentials")
    assert line["refusal"] == "it exited 1, stderr '401 Bad credentials'"
    assert line["credential"] == "app-token"
    assert SECRET not in _audit_text(harness)


def test_a_run_the_runner_refused_has_a_line_with_no_output(
    harness: Harness,
) -> None:
    """A runner error's line has no exit code and no stream sizes."""
    harness.runner.respond = _answer(CageTimeoutError("too slow"))
    _refusal(harness)

    [line] = _audit_lines(harness)
    assert line["exit_code"] is None
    assert line["stdout_bytes"] is None
    assert line["stderr_bytes"] is None
    assert line["truncated"] is False
    assert line["refusal"] == "too slow"
    assert isinstance(line["duration_ms"], int)


def test_a_run_past_the_cap_has_a_truncated_line(harness: Harness) -> None:
    """Stdout past the cap is counted whole and marked truncated."""
    harness.runner.respond = _answer(_outcome(None, stdout_bytes=OUTPUT_CAP_BYTES + 1))
    _refusal(harness)

    [line] = _audit_lines(harness)
    assert line["truncated"] is True
    assert line["stdout_bytes"] == OUTPUT_CAP_BYTES + 1


def test_an_unwritten_line_withholds_and_does_not_cache_the_value(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no line, the call gets the audit refusal and the next call runs again."""
    fail_next_write_after_half(monkeypatch)

    refusal = _refusal(harness)

    [call] = harness.runner.calls
    assert refusal == audit_refusal(call.call_id)
    assert harness.provided.resolve(SNAPSHOT, GRANT) == {"app-token": SECRET}
    assert len(harness.runner.calls) == 2


def test_an_unwritten_line_of_a_failed_run_refuses_even_with_a_cached_value(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run is audited before any value is handed out."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - REFRESH_BEFORE
    harness.runner.respond = _answer(_outcome(b"[]"))
    fail_next_write_after_half(monkeypatch)

    refusal = _refusal(harness)

    assert refusal == audit_refusal(harness.runner.calls[-1].call_id)


def test_a_waiting_call_is_served_by_the_refresh_another_call_runs(
    harness: Harness,
) -> None:
    """Two calls of one stale credential make one run, and both get its value."""
    harness.provided.resolve(SNAPSHOT, GRANT)
    harness.clock.moment = NOW + HOUR - REFRESH_BEFORE
    running = threading.Event()
    release = threading.Event()

    def respond(_call: CageCall) -> Outcome:
        running.set()
        _wait(release)
        return _issued("second-value", NOW + 2 * HOUR)

    harness.runner.respond = respond
    results: dict[str, dict[str, str]] = {}
    first = threading.Thread(
        target=lambda: results.update(first=harness.provided.resolve(SNAPSHOT, GRANT))
    )
    first.start()
    _wait(running)
    # The first call holds the lock inside its run, so the next clock reading
    # is the second call's freshness check of the stale value, before it
    # takes the lock.
    harness.clock.hook = release.set
    results["second"] = harness.provided.resolve(SNAPSHOT, GRANT)
    first.join(WAIT_SECONDS)

    refreshed = {"app-token": "second-value"}
    assert results == {"first": refreshed, "second": refreshed}
    assert len(harness.runner.calls) == 2
    assert len(_audit_lines(harness)) == 2


def test_runs_of_different_credentials_go_side_by_side(harness: Harness) -> None:
    """Each run waits for the other to start, so one lock for both would hang."""
    snapshot = _snapshot(
        **{
            "app-token": _entry(env={"APP_ID": "1"}),
            "other-token": _entry(env={"APP_ID": "2"}),
        }
    )
    started = {"1": threading.Event(), "2": threading.Event()}

    def respond(call: CageCall) -> Outcome:
        mine = call.grant.env["APP_ID"]
        started[mine].set()
        _wait(started["2" if mine == "1" else "1"])
        return _issued(f"value-{mine}")

    harness.runner.respond = respond
    other_grant = Grant.model_validate(
        {"roots": [], "domains": [], "credentials": {"other-token": {"env": "OTHER"}}}
    )
    results: dict[str, dict[str, str]] = {}
    first = threading.Thread(
        target=lambda: results.update(first=harness.provided.resolve(snapshot, GRANT))
    )
    first.start()
    results["second"] = harness.provided.resolve(snapshot, other_grant)
    first.join(WAIT_SECONDS)

    assert results == {
        "first": {"app-token": "value-1"},
        "second": {"other-token": "value-2"},
    }
