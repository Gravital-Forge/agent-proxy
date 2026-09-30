import contextlib
import secrets
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import docker.errors
import pytest
import yaml
from docker import DockerClient
from docker.models.containers import Container
from docker.models.networks import Network

from agent_proxy import egress
from agent_proxy.config import Grant
from agent_proxy.credentials import CredentialStore
from agent_proxy.plugins import CommandAction
from agent_proxy.results import new_call_id
from agent_proxy.runner import CageCall, CageLimiter, CageRunner

TEST_LABELS = {"agent-proxy.test": "1"}
COM = ("example.com",)
ORG = ("example.org",)
COM_URL = "https://example.com/"
ORG_URL = "https://example.org/"
OUTSIDE_URL = "https://1.1.1.1/"
PROXY_URL = "http://egress:1080"
TUNNEL_PORT = 1080
CURL = (
    "curl",
    "-sS",
    "-o",
    "/dev/null",
    "--connect-timeout",
    "5",
    "--max-time",
    "20",
    "-w",
    "%{http_code} %{http_connect}",
)
NO_ROUTE = "000 000"
# Three curl calls of up to 20 seconds each fit in one cage's timeout.
CAGE_TIMEOUT = timedelta(seconds=90)
MAX_CAGES = 4


@dataclass(frozen=True)
class _Bench:
    client: DockerClient
    proxies: egress.EgressProxies
    config_root: Path
    runner: CageRunner
    image: str
    overflow_dir: Path
    containers: list[Container] = field(default_factory=list)
    networks: list[Network] = field(default_factory=list)

    def plain_bridge(self) -> egress.CageNetwork:
        network = self.client.networks.create(
            f"apx-it-egress-{secrets.token_hex(4)}",
            driver="bridge",
            labels=TEST_LABELS,
        )
        self.networks.append(network)
        return egress.CageNetwork(name=network.name, environment={})

    def run(
        self, network: egress.CageNetwork, domains: Sequence[str], *lines: str
    ) -> list[str]:
        call = CageCall(
            call_id=new_call_id(),
            image=self.image,
            action=_shell("\n".join(lines)),
            grant=_grant(domains),
            arguments={},
            network=network,
            overflow_dir=str(self.overflow_dir),
            max_cages=MAX_CAGES,
        )
        return str(self.runner.run(call).content["stdout"]).splitlines()

    def run_on_list(self, domains: Sequence[str], *lines: str) -> list[str]:
        return self.run(egress.cage_network(domains), domains, *lines)

    def tear_down(self) -> None:
        for container in self.containers:
            with contextlib.suppress(docker.errors.NotFound):
                container.remove(force=True)
        self.proxies.prune(())
        with contextlib.suppress(docker.errors.NotFound):
            self.client.networks.get(egress.BRIDGE_NETWORK).remove()
        for network in self.networks:
            with contextlib.suppress(docker.errors.NotFound):
                network.remove()


@pytest.fixture
def bench(
    docker_client: DockerClient, probe_image: str, tmp_path: Path
) -> Iterator[_Bench]:
    """Egress proxies and a probe-image cage runner, all removed afterwards."""
    overflow_dir = tmp_path / "overflow"
    overflow_dir.mkdir()
    config_root = tmp_path / "egress"
    created = _Bench(
        client=docker_client,
        proxies=egress.EgressProxies(docker_client, config_root),
        config_root=config_root,
        runner=CageRunner(
            docker_client, CredentialStore(tmp_path / "credentials"), CageLimiter()
        ),
        image=probe_image,
        overflow_dir=overflow_dir,
    )
    try:
        yield created
    finally:
        created.tear_down()


def _shell(script: str) -> CommandAction:
    return CommandAction(
        name="shell",
        input_schema={"type": "object"},
        credentials=(),
        timeout=CAGE_TIMEOUT,
        argv=("sh", "-c", script),
        parameters=(),
        passthrough_args=False,
    )


def _grant(domains: Sequence[str]) -> Grant:
    return Grant.model_validate(
        {"roots": [], "domains": list(domains), "credentials": {}}
    )


def _curl(url: str, *options: str) -> str:
    """Return a shell line that runs curl on url and prints its status pair."""
    return f"{shlex.join([*CURL, *options, url])}; echo"


def _keys(node: object) -> set[object]:
    """Return every mapping key in a parsed YAML document, at any depth."""
    if isinstance(node, dict):
        keys: set[object] = set(node)
        children = list(node.values())
    elif isinstance(node, list):
        keys = set()
        children = node
    else:
        return set()
    return keys.union(*(_keys(child) for child in children))


def test_a_cage_reaches_its_lists_domain_and_is_refused_another(
    bench: _Bench,
) -> None:
    """The example.com cage gets example.com; its proxy refuses example.org."""
    bench.proxies.ensure([COM, ORG])

    pairs = bench.run_on_list(COM, _curl(COM_URL), _curl(ORG_URL))

    assert pairs == ["200 200", "000 403"]


def test_an_empty_list_cage_has_no_route_out(bench: _Bench) -> None:
    """No proxy variable, and no connection directly, via egress, or to an IP."""
    bench.proxies.ensure([()])
    network = egress.cage_network(())

    lines = bench.run_on_list(
        (),
        "if env | grep -q HTTPS_PROXY; then echo proxy=set; else echo proxy=unset; fi",
        _curl(COM_URL),
        _curl(COM_URL, "--proxy", PROXY_URL),
        _curl(OUTSIDE_URL),
    )

    assert network.environment == {}
    assert lines == ["proxy=unset", NO_ROUTE, NO_ROUTE, NO_ROUTE]


def test_a_cage_reaches_no_other_lists_proxy(bench: _Bench) -> None:
    """The example.org proxy, by name or either address, gives no connection."""
    bench.proxies.ensure([COM, ORG])
    org_name = egress.network_name(egress.list_key(ORG))
    attached = bench.client.containers.get(org_name).attrs["NetworkSettings"][
        "Networks"
    ]
    addresses = [
        attached[org_name]["IPAddress"],
        attached[egress.BRIDGE_NETWORK]["IPAddress"],
    ]
    assert all(addresses), attached

    pairs = bench.run_on_list(
        COM,
        *(
            _curl(ORG_URL, "--proxy", f"http://{host}:{TUNNEL_PORT}")
            for host in [org_name, *addresses]
        ),
    )

    assert pairs == [NO_ROUTE] * 3


def test_a_direct_connection_to_an_outside_address_fails(bench: _Bench) -> None:
    """The example.com cage gets no connection to 1.1.1.1; a plain-bridge cage does."""
    bench.proxies.ensure([COM])
    [control] = bench.run(bench.plain_bridge(), (), _curl(OUTSIDE_URL))
    if control.startswith("000 "):
        pytest.fail(
            f"inconclusive: a cage on a plain bridge got {control!r} from "
            f"{OUTSIDE_URL}, so the daemon may have no route out"
        )

    pairs = bench.run_on_list(COM, _curl(OUTSIDE_URL, "--noproxy", "*"))

    assert pairs == [NO_ROUTE]


def test_no_written_config_carries_source_ip(bench: _Bench) -> None:
    """Every proxy.yaml ensure writes is a mapping with no source_ip or rules key."""
    bench.proxies.ensure([COM, ORG])

    documents = [
        yaml.safe_load(path.read_bytes())
        for path in sorted(bench.config_root.rglob("proxy.yaml"))
    ]

    assert len(documents) == 2
    for document in documents:
        assert isinstance(document, dict)
        assert _keys(document).isdisjoint({"source_ip", "rules"})
