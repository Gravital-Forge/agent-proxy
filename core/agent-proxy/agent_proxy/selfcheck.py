"""The self-check: one scope's local wiring, checked against the config in force."""

import dataclasses
from collections.abc import Iterator, Mapping, Set
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Literal

from docker import DockerClient

from agent_proxy.config import Grant, InstanceConfig, ProvidedCredential, Root
from agent_proxy.credentials import CredentialStore, unset_message
from agent_proxy.egress import (
    CageNetwork,
    EgressError,
    list_key,
    network_name,
    proxy_status,
)
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import DESCRIBE_TIMEOUT_SECONDS, CommandAction
from agent_proxy.reload import Snapshot
from agent_proxy.results import OverflowWriteError, check_overflow_dir, new_call_id
from agent_proxy.runner import CageCall, CageRunner, RootError, hold_roots

SELF_CHECK_TOOL = "agent-proxy_self_check"
SELF_CHECK_INPUT_SCHEMA: Mapping[str, object] = MappingProxyType(
    {"type": "object", "properties": {}, "additionalProperties": False}
)
_DESCRIBE = CommandAction(
    name="describe",
    input_schema=SELF_CHECK_INPUT_SCHEMA,
    credentials=(),
    timeout=timedelta(seconds=DESCRIBE_TIMEOUT_SECONDS),
    argv=("describe",),
    parameters=(),
    passthrough_args=False,
)
_NO_NETWORK = CageNetwork(name="none", environment={})

CheckKind = Literal["credential", "root", "overflow_dir", "egress", "plugin"]


@dataclass(frozen=True)
class CheckEntry:
    """One check: its kind, what it checked, whether it passed, and why."""

    check: CheckKind
    subject: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CheckReport:
    """Every check of one scope, in kind order."""

    scope: str
    checks: tuple[CheckEntry, ...]

    @property
    def ok(self) -> bool:
        """Return whether every entry passed."""
        return all(entry.ok for entry in self.checks)

    def content(self) -> dict[str, object]:
        """Return the report as a JSON-ready object."""
        return {
            "ok": self.ok,
            "scope": self.scope,
            "checks": [dataclasses.asdict(entry) for entry in self.checks],
        }


class SelfCheck:
    """Checks what can be wrong in a scope's wiring while the instance looks healthy."""

    def __init__(
        self, client: DockerClient, credentials: CredentialStore, runner: CageRunner
    ) -> None:
        """Hold the instance's daemon client, credential store, and cage runner."""
        self._client = client
        self._credentials = credentials
        self._runner = runner

    def run(self, snapshot: Snapshot, scope_name: str) -> CheckReport:
        """Return the report of scope_name, a scope snapshot.config defines.

        Each kind's subjects come once each, in the order the scope's grants
        first name them. A provided credential the grants name is checked by
        its provider's stored inputs, and its provider's domain list follows
        the grants' lists; no provider runs. A failed check is an entry,
        never an error.
        """
        config = snapshot.config
        grants = config.scopes[scope_name].grants
        provided = _named_provided(config, grants)
        checks = (
            *self._credential_checks(grants, provided),
            *_root_checks(grants),
            _overflow_dir_check(config.overflow_dir),
            *self._egress_checks(grants, provided),
            *self._plugin_checks(config, grants),
        )
        return CheckReport(scope=scope_name, checks=checks)

    def _credential_checks(
        self,
        grants: Mapping[str, Grant],
        provided: Mapping[str, ProvidedCredential],
    ) -> Iterator[CheckEntry]:
        names = dict.fromkeys(
            name for grant in grants.values() for name in grant.credentials
        )
        stored = self._credentials.names()
        for name in names:
            if name in provided:
                yield _provided_check(name, provided[name], stored)
            elif name in stored:
                yield CheckEntry(
                    check="credential", subject=name, ok=True, detail="stored"
                )
            else:
                yield CheckEntry(
                    check="credential",
                    subject=name,
                    ok=False,
                    detail=unset_message(name),
                )

    def _egress_checks(
        self,
        grants: Mapping[str, Grant],
        provided: Mapping[str, ProvidedCredential],
    ) -> Iterator[CheckEntry]:
        named = (
            *(grant.domains for grant in grants.values()),
            *(entry.domains for entry in provided.values()),
        )
        lists = dict.fromkeys(tuple(sorted(domains)) for domains in named if domains)
        for domains in lists:
            yield self._egress_check(domains)

    def _egress_check(self, domains: tuple[str, ...]) -> CheckEntry:
        name = network_name(list_key(domains))
        allowed = ", ".join(domains)
        try:
            status = proxy_status(self._client, domains)
        except EgressError as error:
            return CheckEntry(check="egress", subject=name, ok=False, detail=str(error))
        if status == "running":
            return CheckEntry(
                check="egress",
                subject=name,
                ok=True,
                detail=f"running; it allows {allowed}",
            )
        state = "not present" if status is None else status
        return CheckEntry(
            check="egress",
            subject=name,
            ok=False,
            detail=f"proxy {name!r} for {allowed} is {state}",
        )

    def _plugin_checks(
        self, config: InstanceConfig, grants: Mapping[str, Grant]
    ) -> Iterator[CheckEntry]:
        plugins = dict.fromkeys(key.split(".", 1)[0] for key in grants)
        for plugin in plugins:
            yield self._plugin_check(config, plugin)

    def _plugin_check(self, config: InstanceConfig, plugin: str) -> CheckEntry:
        """Run the plugin image's describe in a cage with nothing granted."""
        try:
            outcome = self._runner.run(
                CageCall(
                    call_id=new_call_id(),
                    image=config.plugins[plugin],
                    action=_DESCRIBE,
                    grant=Grant(roots=[], domains=[], credentials={}),
                    arguments={},
                    network=_NO_NETWORK,
                    overflow_dir=config.overflow_dir,
                    max_cages=config.max_cages,
                )
            )
        except ProxyError as error:
            return CheckEntry(
                check="plugin", subject=plugin, ok=False, detail=str(error)
            )
        if outcome.exit_code != 0:
            return CheckEntry(
                check="plugin",
                subject=plugin,
                ok=False,
                detail=f"describe exited with status {outcome.exit_code}",
            )
        return CheckEntry(
            check="plugin",
            subject=plugin,
            ok=True,
            detail="describe ran in a cage and exited 0",
        )


def _named_provided(
    config: InstanceConfig, grants: Mapping[str, Grant]
) -> dict[str, ProvidedCredential]:
    """Return each provided credential grants name, in the order first named."""
    return {
        name: config.provided_credentials[name]
        for grant in grants.values()
        for name in grant.credentials
        if name in config.provided_credentials
    }


def _provided_check(
    name: str, entry: ProvidedCredential, stored: Set[str]
) -> CheckEntry:
    """Check that every stored credential name's provider takes is stored."""
    missing = [
        unset_message(taken) for taken in entry.credentials if taken not in stored
    ]
    if missing:
        return CheckEntry(
            check="credential",
            subject=name,
            ok=False,
            detail="; ".join((f"provided by {entry.provider!r}", *missing)),
        )
    return CheckEntry(
        check="credential",
        subject=name,
        ok=True,
        detail=f"provided by {entry.provider!r}; every stored credential it takes"
        " is stored",
    )


def _root_checks(grants: Mapping[str, Grant]) -> Iterator[CheckEntry]:
    roots: dict[str, Root] = {}
    for grant in grants.values():
        for root in grant.roots:
            roots.setdefault(root.path, root)
    for root in roots.values():
        yield _root_check(root)


def _root_check(root: Root) -> CheckEntry:
    """Open the root as the runner opens it for a call, then close it."""
    try:
        with ExitStack() as held:
            hold_roots([root], held)
    except RootError as error:
        return CheckEntry(check="root", subject=root.path, ok=False, detail=str(error))
    return CheckEntry(
        check="root", subject=root.path, ok=True, detail="the instance opens it"
    )


def _overflow_dir_check(overflow_dir: str) -> CheckEntry:
    try:
        check_overflow_dir(overflow_dir)
    except OverflowWriteError as error:
        return CheckEntry(
            check="overflow_dir", subject=overflow_dir, ok=False, detail=str(error)
        )
    return CheckEntry(
        check="overflow_dir",
        subject=overflow_dir,
        ok=True,
        detail="the instance can create a file in it",
    )
