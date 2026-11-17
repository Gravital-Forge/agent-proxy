import base64
import json
import secrets
import shlex
import shutil
import statistics
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from docker import DockerClient
from docker.models.containers import Container

from agent_proxy import runner
from agent_proxy.config import EnvMount, FileMount, Grant, Root
from agent_proxy.credentials import CredentialStore
from agent_proxy.egress import CageNetwork
from agent_proxy.plugins import CommandAction, ScriptAction, parse_manifest
from agent_proxy.results import new_call_id
from agent_proxy.runner import (
    CageCall,
    CageLimiter,
    CageRunner,
    CageStartError,
    CageTimeoutError,
    InputError,
    Outcome,
    RootError,
)

MANIFEST = parse_manifest(
    (Path(__file__).parent.parent / "test-plugin" / "manifest.json").read_bytes()
)
ECHO = MANIFEST.actions["echo"]
COUNT = MANIFEST.actions["count"]
ECHO_HELLO = {"exit_code": 0, "stdout": "hello\n", "stderr": ""}
DEFAULT_MAX_CAGES = 4
# Probes whether the root filesystem and /tmp take a write, and reads the
# effective capabilities and the no_new_privs flag.
HARDEN_PROBE = (
    "if touch /probe 2>/dev/null; "
    "then echo root=written; else echo root=refused; fi; "
    "if touch /tmp/probe 2>/dev/null; "
    "then echo tmp=written; else echo tmp=refused; fi; "
    "awk '"
    '/^CapEff:/ {print "capeff=" $2} '
    '/^NoNewPrivs:/ {print "nonewprivs=" $2}'
    "' /proc/self/status"
)
OVERFLOW_BYTES = 1048576
OVERFLOW_LINE = b"abcdefg\n"
WARMUP_CALLS = 3
TIMED_CALLS = 30
PAIR_SLEEP_SECONDS = 2
CREDENTIAL_FILE = "/run/credentials/second"


@dataclass(frozen=True)
class _Cages:
    runner: CageRunner
    store: CredentialStore
    image: str
    network: str
    overflow_dir: Path

    def call(
        self,
        action: CommandAction | ScriptAction,
        arguments: Mapping[str, object],
        grant: Grant,
        max_cages: int = DEFAULT_MAX_CAGES,
    ) -> CageCall:
        return CageCall(
            call_id=new_call_id(),
            image=self.image,
            action=action,
            grant=grant,
            arguments=arguments,
            network=CageNetwork(name=self.network, environment={}),
            overflow_dir=str(self.overflow_dir),
            max_cages=max_cages,
        )

    def run(
        self,
        action: CommandAction | ScriptAction,
        arguments: Mapping[str, object],
        grant: Grant,
    ) -> Outcome:
        return self.runner.run(self.call(action, arguments, grant))


@pytest.fixture
def cages(
    docker_client: DockerClient, runner_image: str, cage_network: str, tmp_path: Path
) -> _Cages:
    """A runner, its store, and where its cages run, all outside every root."""
    store = CredentialStore(tmp_path / "credentials")
    overflow_dir = tmp_path / "overflow"
    overflow_dir.mkdir()
    return _Cages(
        runner=CageRunner(docker_client, store, CageLimiter()),
        store=store,
        image=runner_image,
        network=cage_network,
        overflow_dir=overflow_dir,
    )


def _shell(script: str) -> CommandAction:
    return CommandAction(
        name="shell",
        input_schema={"type": "object"},
        credentials=(),
        timeout=timedelta(seconds=10),
        argv=("sh", "-c", script),
        parameters=(),
        passthrough_args=False,
    )


def _grant(
    *roots: Root,
    credentials: Mapping[str, EnvMount | FileMount] | None = None,
    timeout: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Grant:
    document: dict[str, object] = {
        "roots": list(roots),
        "domains": [],
        "credentials": dict(credentials or {}),
    }
    if timeout is not None:
        document["timeout"] = timeout
    if env is not None:
        document["env"] = dict(env)
    return Grant.model_validate(document)


def _presence(label: str, path: Path) -> str:
    return (
        f"if [ -e {shlex.quote(str(path))} ]; "
        f"then echo {label}=present; else echo {label}=absent; fi"
    )


def _swap_after_create(
    monkeypatch: pytest.MonkeyPatch, swap: Callable[[], None]
) -> None:
    """Make each cage's create swap a root's path before the cage starts."""
    real_create = runner._create

    def create(
        client: DockerClient, call: CageCall, setup: runner._CageSetup
    ) -> Container:
        container = real_create(client, call, setup)
        swap()
        return container

    monkeypatch.setattr(runner, "_create", create)


def _assert_refused_unread(
    cages: _Cages, root: Path, writable: Path, secret: str
) -> None:
    """Assert the call is refused before the action writes or reads the secret."""
    action = _shell(
        f"echo ran > {shlex.quote(str(writable / 'marker'))}; "
        f"cat {shlex.quote(str(root / 'secret'))}"
    )
    grant = _grant(Root(path=str(root), mode="rw"), Root(path=str(writable), mode="rw"))

    with pytest.raises(RootError):
        cages.run(action, {}, grant)

    assert not (writable / "marker").exists()
    assert not any(
        secret.encode() in path.read_bytes()
        for path in cages.overflow_dir.rglob("*")
        if path.is_file()
    )


def _credential_search(encoded_values: Mapping[str, str]) -> str:
    lines = []
    for label, encoded in encoded_values.items():
        lines += [
            f"value=$(printf %s {shlex.quote(encoded)} | base64 -d)",
            f'if env | grep -qF -e "$value"; then echo {label}=env; fi',
            r"find / \( -path /proc -o -path /sys \) -prune -o -type f"
            ' -exec grep -lF -e "$value" {} + 2>/dev/null'
            f' | sed "s|^|{label}=file:|"',
        ]
    lines.append(
        f"if (echo x >> {CREDENTIAL_FILE}) 2>/dev/null; "
        "then echo write=written; else echo write=refused; fi"
    )
    return "\n".join(lines)


def _run_pair(cages: _Cages, max_cages: int) -> float:
    action = _shell(f"sleep {PAIR_SLEEP_SECONDS}")
    calls = [cages.call(action, {}, _grant(), max_cages) for _ in range(2)]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(cages.runner.run, calls))
    elapsed = time.monotonic() - started
    assert [outcome.exit_code for outcome in outcomes] == [0, 0]
    return elapsed


def test_an_unmounted_sibling_of_a_root_is_absent(
    cages: _Cages, tmp_path: Path
) -> None:
    """A directory beside the one root exists on the host and not in the cage."""
    root = tmp_path / "roots" / "root"
    sibling = tmp_path / "roots" / "sibling"
    root.mkdir(parents=True)
    sibling.mkdir()
    script = f"{_presence('root', root)}; {_presence('sibling', sibling)}"

    outcome = cages.run(_shell(script), {}, _grant(Root(path=str(root), mode="rw")))

    assert outcome.content["stdout"] == "root=present\nsibling=absent\n"


def test_a_ro_root_refuses_a_write_and_a_rw_root_takes_one(
    cages: _Cages, tmp_path: Path
) -> None:
    """The ro root's write fails as read-only; the rw root's write reaches the host."""
    ro_root = tmp_path / "roots" / "ro"
    rw_root = tmp_path / "roots" / "rw"
    ro_root.mkdir(parents=True)
    rw_root.mkdir()
    script = (
        f"echo data > {shlex.quote(str(rw_root / 'probe'))}; "
        f"echo data > {shlex.quote(str(ro_root / 'probe'))}"
    )
    grant = _grant(
        Root(path=str(ro_root), mode="ro"), Root(path=str(rw_root), mode="rw")
    )

    outcome = cages.run(_shell(script), {}, grant)

    assert outcome.exit_code != 0
    assert "Read-only file system" in str(outcome.content["stderr"])
    assert not (ro_root / "probe").exists()
    assert (rw_root / "probe").read_text() == "data\n"


def test_a_leading_dash_is_refused_before_any_cage(
    cages: _Cages, labelled_containers: Callable[[], list[Container]]
) -> None:
    """The echo action refuses text '-n' with no cage made, then echoes 'hello'."""
    with pytest.raises(InputError):
        cages.run(ECHO, {"text": "-n"}, _grant())

    assert labelled_containers() == []
    assert cages.run(ECHO, {"text": "hello"}, _grant()).content == ECHO_HELLO


def test_only_granted_credentials_reach_the_cage(cages: _Cages) -> None:
    """The env and file grants are found where granted; the ungranted value nowhere."""
    values = {label: secrets.token_hex(16) for label in ("first", "second", "third")}
    for label, value in values.items():
        cages.store.set(label, value.encode())
    encoded = {
        label: base64.b64encode(value.encode()).decode()
        for label, value in values.items()
    }
    grant = _grant(
        credentials={
            "first": EnvMount(env="APX_FIRST"),
            "second": FileMount(file=CREDENTIAL_FILE),
        }
    )

    outcome = cages.run(_shell(_credential_search(encoded)), {}, grant)

    assert set(str(outcome.content["stdout"]).splitlines()) == {
        "first=env",
        f"second=file:{CREDENTIAL_FILE}",
        "write=refused",
    }
    assert cages.store.read("second") == values["second"].encode()


def test_the_cage_reads_its_grant_s_roots_and_no_other_mount(
    cages: _Cages, tmp_path: Path
) -> None:
    """The roots variable names both roots in grant order, not the credential file."""
    second = tmp_path / "roots" / "second"
    first = tmp_path / "roots" / "first"
    second.mkdir(parents=True)
    first.mkdir()
    cages.store.set("second", b"key")
    grant = _grant(
        Root(path=str(second), mode="rw"),
        Root(path=str(first), mode="ro"),
        credentials={"second": FileMount(file=CREDENTIAL_FILE)},
    )

    outcome = cages.run(_shell('printf %s "$AGENT_PROXY_ROOTS"'), {}, grant)

    assert outcome.content["stdout"] == f'["{second}","{first}"]'


def test_a_cage_with_no_roots_reads_an_empty_roots_array(cages: _Cages) -> None:
    """A grant with no roots gives the cage the empty array."""
    outcome = cages.run(_shell('printf %s "$AGENT_PROXY_ROOTS"'), {}, _grant())

    assert outcome.content["stdout"] == "[]"


def test_the_cage_receives_its_grant_s_env(cages: _Cages) -> None:
    """A variable the grant's env sets reaches the cage's environment."""
    grant = _grant(env={"GIT_AUTHOR_NAME": "Agent One"})

    outcome = cages.run(_shell('printf %s "$GIT_AUTHOR_NAME"'), {}, grant)

    assert outcome.content["stdout"] == "Agent One"


def test_the_cage_passes_the_hardening_probes(cages: _Cages) -> None:
    """The cage is hardened.

    The root filesystem refuses a write, /tmp takes one, no capability is
    effective, and no_new_privs is set.
    """
    outcome = cages.run(_shell(HARDEN_PROBE), {}, _grant())

    assert outcome.content["stdout"] == (
        "root=refused\ntmp=written\ncapeff=0000000000000000\nnonewprivs=1\n"
    )


def test_a_timed_out_cage_leaves_no_container(
    cages: _Cages, labelled_containers: Callable[[], list[Container]]
) -> None:
    """A 2s grant timeout stops 'sleep 30' within ten seconds and removes its cage."""
    started = time.monotonic()
    with pytest.raises(CageTimeoutError):
        cages.run(_shell("sleep 30"), {}, _grant(timeout="2s"))

    assert time.monotonic() - started < 10
    assert labelled_containers() == []


def test_stdout_past_the_cap_goes_to_an_overflow_file(cages: _Cages) -> None:
    """A 1 MiB stdout lands whole in the overflow file; stderr stays inline."""
    count = OVERFLOW_BYTES // len(OVERFLOW_LINE)
    script = f"yes {OVERFLOW_LINE.decode().strip()} | head -n {count}"
    call = cages.call(_shell(script), {}, _grant())

    outcome = cages.runner.run(call)

    stdout_file = cages.overflow_dir / f"{call.call_id}.stdout"
    assert outcome.truncated is True
    assert outcome.content == {
        "exit_code": 0,
        "stdout_file": str(stdout_file),
        "stdout_bytes": OVERFLOW_BYTES,
        "stderr": "",
        "truncated": True,
    }
    assert stdout_file.read_bytes() == OVERFLOW_LINE * count


def test_a_call_over_the_cap_waits_for_a_slot(cages: _Cages) -> None:
    """Two 2s cages run one after the other under a cap of 1."""
    assert _run_pair(cages, max_cages=1) >= 2 * PAIR_SLEEP_SECONDS


def test_calls_within_the_cap_run_together(cages: _Cages) -> None:
    """Two 2s cages overlap under a cap of 2."""
    assert _run_pair(cages, max_cages=2) < 2 * PAIR_SLEEP_SECONDS


def test_a_script_receives_its_input_on_stdin(cages: _Cages) -> None:
    """The count script returns the byte length of the compact JSON input."""
    arguments = {"greeting": "héllo", "numbers": [1, 2, 3]}
    compact = json.dumps(arguments, separators=(",", ":"), ensure_ascii=False)

    outcome = cages.run(COUNT, arguments, _grant())

    assert outcome.content == {"bytes": len(compact.encode())}


def test_cage_cost(cages: _Cages, capsys: pytest.CaptureFixture[str]) -> None:
    """Time 30 echo calls after warm-up and print one MEASURE line."""
    for _ in range(WARMUP_CALLS):
        assert cages.run(ECHO, {"text": "hello"}, _grant()).content == ECHO_HELLO
    samples = []
    for _ in range(TIMED_CALLS):
        started = time.perf_counter()
        outcome = cages.run(ECHO, {"text": "hello"}, _grant())
        samples.append((time.perf_counter() - started) * 1000)
        assert outcome.content == ECHO_HELLO
    p95 = statistics.quantiles(samples, n=20, method="inclusive")[18]

    with capsys.disabled():
        print(
            f"\nMEASURE cage-cost calls={TIMED_CALLS} "
            f"median_ms={statistics.median(samples):.1f} "
            f"p95_ms={p95:.1f} max_ms={max(samples):.1f}"
        )


def test_a_root_swapped_for_a_link_after_the_check_is_refused_in_the_cage(
    cages: _Cages, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root renamed aside for a link outside fails the cage's identity check."""
    secret = secrets.token_hex(16)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text(secret)
    root = tmp_path / "roots" / "R"
    writable = tmp_path / "roots" / "W"
    root.mkdir(parents=True)
    writable.mkdir()

    def swap() -> None:
        root.rename(tmp_path / "roots" / "R-aside")
        root.symlink_to(outside)

    _swap_after_create(monkeypatch, swap)

    _assert_refused_unread(cages, root, writable, secret)


def test_a_parent_swapped_for_a_link_after_the_check_is_refused_in_the_cage(
    cages: _Cages, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root's parent renamed aside for a link outside fails the identity check."""
    secret = secrets.token_hex(16)
    outside = tmp_path / "outside"
    (outside / "R").mkdir(parents=True)
    (outside / "R" / "secret").write_text(secret)
    parent = tmp_path / "P"
    root = parent / "R"
    # W stays outside P: a W under the swapped parent would lose its bind
    # source, and the start would fail before the verifier runs.
    writable = tmp_path / "W"
    root.mkdir(parents=True)
    writable.mkdir()

    def swap() -> None:
        parent.rename(tmp_path / "P-aside")
        parent.symlink_to(outside)

    _swap_after_create(monkeypatch, swap)

    _assert_refused_unread(cages, root, writable, secret)


def test_a_root_deleted_and_recreated_after_the_check_is_refused_in_the_cage(
    cages: _Cages, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held descriptor keeps the old inode, so the new directory's differs."""
    secret = secrets.token_hex(16)
    root = tmp_path / "roots" / "R"
    writable = tmp_path / "roots" / "W"
    root.mkdir(parents=True)
    writable.mkdir()

    def swap() -> None:
        shutil.rmtree(root)
        root.mkdir()
        (root / "secret").write_text(secret)

    _swap_after_create(monkeypatch, swap)

    _assert_refused_unread(cages, root, writable, secret)


def test_an_action_the_image_lacks_fails_to_start(
    cages: _Cages, labelled_containers: Callable[[], list[Container]]
) -> None:
    """The verifier cannot exec a missing program; the runner raises a start error."""
    action = CommandAction(
        name="missing",
        input_schema={"type": "object"},
        credentials=(),
        timeout=timedelta(seconds=10),
        argv=("/apx-no-such-program",),
        parameters=(),
        passthrough_args=False,
    )

    with pytest.raises(CageStartError, match="apx-no-such-program"):
        cages.run(action, {}, _grant())

    assert labelled_containers() == []
