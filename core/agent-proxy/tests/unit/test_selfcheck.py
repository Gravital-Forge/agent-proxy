import errno
import os
import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from unittest import mock

import docker.errors
import pytest
import requests

from agent_proxy.config import Grant, InstanceConfig
from agent_proxy.credentials import CredentialStore, unset_message
from agent_proxy.egress import CageNetwork, list_key, network_name
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import DESCRIBE_TIMEOUT_SECONDS, CommandAction
from agent_proxy.reload import Snapshot
from agent_proxy.results import CALL_ID_PATTERN
from agent_proxy.runner import CageCall, CageStartError, Outcome
from agent_proxy.selfcheck import (
    SELF_CHECK_INPUT_SCHEMA,
    SELF_CHECK_TOOL,
    CheckEntry,
    CheckReport,
    SelfCheck,
)

GIT_IMAGE = "ghcr.io/example/git@sha256:" + "a" * 64
GITHUB_IMAGE = "ghcr.io/example/github@sha256:" + "b" * 64
MAX_CAGES = 2
SECRET = b"s3cret-value"
OWN_DESCRIPTORS = Path("/proc/self/fd")


def _outcome(exit_code: int) -> Outcome:
    return Outcome(
        content={"exit_code": exit_code, "stdout": "{}"},
        exit_code=exit_code,
        duration_ms=5,
        stdout_bytes=2,
        stderr_bytes=0,
        truncated=False,
    )


class FakeRunner:
    """A cage runner that records each call and returns or raises its result."""

    def __init__(self) -> None:
        """Return an outcome with exit code 0 until result is changed."""
        self.result: Outcome | ProxyError = _outcome(0)
        self.calls: list[CageCall] = []

    def run(self, call: CageCall) -> Outcome:
        """Record call, then return or raise result."""
        self.calls.append(call)
        if isinstance(self.result, ProxyError):
            raise self.result
        return self.result


class Tree:
    """An agent tree under tmp_path, with its overflow directory and a repo."""

    def __init__(self, tmp_path: Path) -> None:
        """Create the tree, its overflow directory, and one repo in it."""
        self.agent_tree = tmp_path / "agent"
        self.overflow_dir = self.agent_tree / "overflow"
        self.repo = self.agent_tree / "repo"
        self.overflow_dir.mkdir(parents=True)
        self.repo.mkdir()

    def snapshot(self, grants: Mapping[str, Mapping[str, object]]) -> Snapshot:
        """Return a snapshot whose scope dev holds grants, and whose idle holds none."""
        config = InstanceConfig.model_validate(
            {
                "agent_tree": str(self.agent_tree),
                "overflow_dir": str(self.overflow_dir),
                "max_cages": MAX_CAGES,
                "plugins": {"git": GIT_IMAGE, "github": GITHUB_IMAGE},
                "scopes": {"dev": {"grants": grants}, "idle": {"grants": {}}},
            }
        )
        return Snapshot(config=config, manifests=MappingProxyType({}), warnings=())


def _grant(
    *,
    roots: tuple[str, ...] = (),
    domains: tuple[str, ...] = (),
    credentials: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "roots": [{"path": path, "mode": "ro"} for path in roots],
        "domains": list(domains),
        "credentials": {
            name: {"env": f"CREDENTIAL_{index}"}
            for index, name in enumerate(credentials)
        },
    }


def _proxy(status: str) -> mock.MagicMock:
    proxy = mock.MagicMock()
    proxy.status = status
    return proxy


def _docker(proxies: Mapping[str, mock.MagicMock] | None = None) -> mock.MagicMock:
    """Return a client whose containers are proxies, keyed by container name."""
    held = proxies or {}
    client = mock.MagicMock()

    def get_container(name: str) -> mock.MagicMock:
        if name not in held:
            raise docker.errors.NotFound(f"container {name} not found")
        return held[name]

    client.containers.get.side_effect = get_container
    return client


def _proxy_name(*domains: str) -> str:
    return network_name(list_key(domains))


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """Return an agent tree under tmp_path."""
    return Tree(tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    """Return an empty credential store under tmp_path."""
    return CredentialStore(tmp_path / "credentials")


@pytest.fixture
def runner() -> FakeRunner:
    """Return a runner whose cages exit 0."""
    return FakeRunner()


def _check(
    snapshot: Snapshot,
    store: CredentialStore,
    runner: FakeRunner,
    client: mock.MagicMock | None = None,
) -> CheckReport:
    self_check = SelfCheck(client or _docker(), store, runner)
    return self_check.run(snapshot, "dev")


def _entries(report: CheckReport, kind: str) -> list[CheckEntry]:
    return [entry for entry in report.checks if entry.check == kind]


def test_the_tool_name_and_its_input_schema() -> None:
    """The tool takes no input."""
    assert SELF_CHECK_TOOL == "agent-proxy_self_check"
    assert dict(SELF_CHECK_INPUT_SCHEMA) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_a_stored_credential_passes(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A name the store holds passes with the detail 'stored'."""
    store.set("api-token", SECRET)
    snapshot = tree.snapshot({"git.clone": _grant(credentials=("api-token",))})

    report = _check(snapshot, store, runner)

    assert _entries(report, "credential") == [
        CheckEntry(check="credential", subject="api-token", ok=True, detail="stored")
    ]


def test_an_unstored_credential_fails_with_the_unset_message(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The detail is the one text that says a credential is unset."""
    snapshot = tree.snapshot({"git.clone": _grant(credentials=("api-token",))})

    report = _check(snapshot, store, runner)

    assert _entries(report, "credential") == [
        CheckEntry(
            check="credential",
            subject="api-token",
            ok=False,
            detail=unset_message("api-token"),
        )
    ]
    assert report.ok is False


def test_credentials_come_once_each_in_first_named_order(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A name two grants share is checked once, where the first grant names it."""
    snapshot = tree.snapshot(
        {
            "git.clone": _grant(credentials=("zeta", "alpha")),
            "git.push": _grant(credentials=("alpha", "mid")),
        }
    )

    report = _check(snapshot, store, runner)

    assert [entry.subject for entry in _entries(report, "credential")] == [
        "zeta",
        "alpha",
        "mid",
    ]


def test_the_credential_store_is_read_once_per_run(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """Every credential entry of one run reads the same listing."""
    snapshot = tree.snapshot(
        {"git.clone": _grant(credentials=("alpha", "beta", "gamma"))}
    )

    with mock.patch.object(store, "names", wraps=store.names) as names:
        _check(snapshot, store, runner)

    names.assert_called_once_with()


def test_no_credential_value_appears_in_the_report(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A credential is reported by name alone."""
    store.set("api-token", SECRET)
    snapshot = tree.snapshot({"git.clone": _grant(credentials=("api-token",))})

    report = _check(snapshot, store, runner)

    assert SECRET.decode() not in repr(report.content())


def test_a_root_the_instance_opens_passes(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """An existing directory with no link in its path passes."""
    snapshot = tree.snapshot({"git.clone": _grant(roots=(str(tree.repo),))})

    report = _check(snapshot, store, runner)

    assert _entries(report, "root") == [
        CheckEntry(
            check="root",
            subject=str(tree.repo),
            ok=True,
            detail="the instance opens it",
        )
    ]


def test_a_missing_root_fails_with_the_root_error(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The detail is the runner's own refusal of the root."""
    absent = tree.agent_tree / "absent"
    snapshot = tree.snapshot({"git.clone": _grant(roots=(str(absent),))})

    report = _check(snapshot, store, runner)

    [entry] = _entries(report, "root")
    assert entry.ok is False
    assert entry.detail == (
        f"root {str(absent)!r}: component 'absent' does not exist; "
        "a root's path must be directories only, with no symbolic link"
    )


def test_a_symlinked_root_fails(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A root that is a link fails, as a call's mount of it would."""
    link = tree.agent_tree / "link"
    link.symlink_to(tree.repo)
    snapshot = tree.snapshot({"git.clone": _grant(roots=(str(link),))})

    report = _check(snapshot, store, runner)

    [entry] = _entries(report, "root")
    assert entry.ok is False
    assert "is a symbolic link or not a directory" in entry.detail


def test_roots_come_once_each_by_path_in_first_named_order(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A path two grants name is checked once, whatever its mode."""
    other = tree.agent_tree / "other"
    other.mkdir()
    snapshot = tree.snapshot(
        {
            "git.clone": _grant(roots=(str(other), str(tree.repo))),
            "git.push": {
                "roots": [{"path": str(tree.repo), "mode": "rw"}],
                "domains": [],
                "credentials": {},
            },
        }
    )

    report = _check(snapshot, store, runner)

    assert [entry.subject for entry in _entries(report, "root")] == [
        str(other),
        str(tree.repo),
    ]


def test_the_root_checks_leave_no_descriptor_open(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """Each root is closed again, whether it passed or failed."""
    snapshot = tree.snapshot(
        {"git.clone": _grant(roots=(str(tree.repo), str(tree.agent_tree / "absent")))}
    )
    before = len(list(OWN_DESCRIPTORS.iterdir()))

    _check(snapshot, store, runner)

    assert len(list(OWN_DESCRIPTORS.iterdir())) == before


def test_a_writable_overflow_dir_passes(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The check leaves the overflow directory empty."""
    report = _check(tree.snapshot({}), store, runner)

    assert _entries(report, "overflow_dir") == [
        CheckEntry(
            check="overflow_dir",
            subject=str(tree.overflow_dir),
            ok=True,
            detail="the instance can create a file in it",
        )
    ]
    assert list(tree.overflow_dir.iterdir()) == []


def test_a_missing_overflow_dir_fails_with_the_overflow_error(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The detail names the directory and the OS error."""
    tree.overflow_dir.rmdir()

    report = _check(tree.snapshot({}), store, runner)

    [entry] = _entries(report, "overflow_dir")
    assert entry.ok is False
    assert entry.detail == (
        f"overflow directory {str(tree.overflow_dir)!r} cannot take a file: "
        f"{os.strerror(errno.ENOENT)}"
    )


def test_a_running_proxy_passes_and_names_its_domains(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The subject is the proxy container; the detail lists the domains sorted."""
    name = _proxy_name("github.com", "api.github.com")
    client = _docker({name: _proxy("running")})
    snapshot = tree.snapshot(
        {"git.clone": _grant(domains=("github.com", "api.github.com"))}
    )

    report = _check(snapshot, store, runner, client)

    assert _entries(report, "egress") == [
        CheckEntry(
            check="egress",
            subject=name,
            ok=True,
            detail="running; it allows api.github.com, github.com",
        )
    ]


def test_an_absent_proxy_fails(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A proxy Docker does not find is not present."""
    name = _proxy_name("example.com")
    snapshot = tree.snapshot({"git.clone": _grant(domains=("example.com",))})

    report = _check(snapshot, store, runner)

    assert _entries(report, "egress") == [
        CheckEntry(
            check="egress",
            subject=name,
            ok=False,
            detail=f"proxy {name!r} for example.com is not present",
        )
    ]


def test_a_proxy_that_is_not_running_fails_with_its_status(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The detail names the status Docker reports."""
    name = _proxy_name("example.com")
    client = _docker({name: _proxy("exited")})
    snapshot = tree.snapshot({"git.clone": _grant(domains=("example.com",))})

    report = _check(snapshot, store, runner, client)

    [entry] = _entries(report, "egress")
    assert entry.ok is False
    assert entry.detail == f"proxy {name!r} for example.com is exited"


def test_a_failed_proxy_lookup_fails_with_the_egress_error(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A Docker failure becomes the entry's detail, not an error."""
    name = _proxy_name("example.com")
    client = _docker()
    client.containers.get.side_effect = requests.exceptions.ConnectionError(
        "daemon said no"
    )
    snapshot = tree.snapshot({"git.clone": _grant(domains=("example.com",))})

    report = _check(snapshot, store, runner, client)

    [entry] = _entries(report, "egress")
    assert entry.ok is False
    assert entry.detail == f"proxy {name!r} could not be looked up: daemon said no"


def test_domain_lists_come_once_each_compared_sorted_and_the_empty_list_none(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """Lists equal once sorted share one proxy; the empty list has no proxy."""
    snapshot = tree.snapshot(
        {
            "git.clone": _grant(domains=("b.io", "a.io")),
            "git.fetch": _grant(),
            "git.push": _grant(domains=("c.io",)),
            "github.view": _grant(domains=("a.io", "b.io")),
        }
    )

    report = _check(snapshot, store, runner)

    assert [entry.subject for entry in _entries(report, "egress")] == [
        _proxy_name("a.io", "b.io"),
        _proxy_name("c.io"),
    ]


def test_the_plugin_check_runs_describe_in_a_bare_cage(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The cage gets no root, credential, or environment, and the network none."""
    snapshot = tree.snapshot(
        {
            "github.view": _grant(
                roots=(str(tree.repo),), domains=("example.com",), credentials=("t",)
            )
        }
    )

    _check(snapshot, store, runner)

    [call] = runner.calls
    assert re.fullmatch(CALL_ID_PATTERN, call.call_id)
    assert call == CageCall(
        call_id=call.call_id,
        image=GITHUB_IMAGE,
        action=CommandAction(
            name="describe",
            input_schema=SELF_CHECK_INPUT_SCHEMA,
            credentials=(),
            timeout=timedelta(seconds=DESCRIBE_TIMEOUT_SECONDS),
            argv=("describe",),
            parameters=(),
            passthrough_args=False,
        ),
        grant=Grant(roots=[], domains=[], credentials={}),
        arguments={},
        network=CageNetwork(name="none", environment={}),
        overflow_dir=str(tree.overflow_dir),
        max_cages=MAX_CAGES,
    )


def test_each_plugin_check_takes_a_fresh_call_id(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """No two cages share a call ID, so no two share an overflow file name."""
    snapshot = tree.snapshot({"git.clone": _grant(), "github.view": _grant()})

    _check(snapshot, store, runner)

    first, second = runner.calls
    assert first.call_id != second.call_id


def test_a_plugin_whose_describe_exits_0_passes(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The subject is the plugin's name from the grant key."""
    report = _check(tree.snapshot({"git.clone": _grant()}), store, runner)

    assert _entries(report, "plugin") == [
        CheckEntry(
            check="plugin",
            subject="git",
            ok=True,
            detail="describe ran in a cage and exited 0",
        )
    ]


def test_a_plugin_whose_describe_exits_non_zero_fails(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The detail names the exit status."""
    runner.result = _outcome(3)

    report = _check(tree.snapshot({"git.clone": _grant()}), store, runner)

    [entry] = _entries(report, "plugin")
    assert entry.ok is False
    assert entry.detail == "describe exited with status 3"


def test_a_runner_refusal_fails_the_plugin_check_with_its_text(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The runner's error becomes the entry's detail, not an error."""
    runner.result = CageStartError("image 'x': the cage could not be created: gone")

    report = _check(tree.snapshot({"git.clone": _grant()}), store, runner)

    [entry] = _entries(report, "plugin")
    assert entry.ok is False
    assert entry.detail == "image 'x': the cage could not be created: gone"


def test_plugins_come_once_each_in_first_named_order(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """A plugin two grants use runs one cage."""
    snapshot = tree.snapshot(
        {"github.view": _grant(), "git.clone": _grant(), "github.edit": _grant()}
    )

    report = _check(snapshot, store, runner)

    assert [entry.subject for entry in _entries(report, "plugin")] == [
        "github",
        "git",
    ]
    assert [call.image for call in runner.calls] == [GITHUB_IMAGE, GIT_IMAGE]


def test_entries_come_in_kind_order(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """Credentials, roots, the overflow directory, egress, then plugins."""
    store.set("api-token", SECRET)
    name = _proxy_name("example.com")
    client = _docker({name: _proxy("running")})
    snapshot = tree.snapshot(
        {
            "git.clone": _grant(
                roots=(str(tree.repo),),
                domains=("example.com",),
                credentials=("api-token",),
            )
        }
    )

    report = _check(snapshot, store, runner, client)

    assert [(entry.check, entry.subject) for entry in report.checks] == [
        ("credential", "api-token"),
        ("root", str(tree.repo)),
        ("overflow_dir", str(tree.overflow_dir)),
        ("egress", name),
        ("plugin", "git"),
    ]
    assert report.ok is True


def test_a_scope_with_no_grants_has_only_its_overflow_dir_entry(
    tree: Tree, store: CredentialStore, runner: FakeRunner
) -> None:
    """The report names the scope it checked, and no cage runs."""
    self_check = SelfCheck(_docker(), store, runner)

    report = self_check.run(tree.snapshot({}), "idle")

    assert report.scope == "idle"
    assert [entry.check for entry in report.checks] == ["overflow_dir"]
    assert runner.calls == []


def test_content_is_the_json_ready_report() -> None:
    """Its ok is false when one entry fails."""
    report = CheckReport(
        scope="dev",
        checks=(
            CheckEntry(check="credential", subject="t", ok=False, detail="unset"),
            CheckEntry(check="plugin", subject="git", ok=True, detail="ran"),
        ),
    )

    assert report.content() == {
        "ok": False,
        "scope": "dev",
        "checks": [
            {"check": "credential", "subject": "t", "ok": False, "detail": "unset"},
            {"check": "plugin", "subject": "git", "ok": True, "detail": "ran"},
        ],
    }


def test_a_report_whose_entries_all_pass_is_ok() -> None:
    """Its ok is true when every entry passes."""
    report = CheckReport(
        scope="dev",
        checks=(CheckEntry(check="plugin", subject="git", ok=True, detail="ran"),),
    )

    assert report.ok is True
