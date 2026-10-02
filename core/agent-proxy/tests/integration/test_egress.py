import contextlib
import functools
import ipaddress
import secrets
import shlex
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType

import docker.errors
import pytest
import yaml
from conftest import remove_bridge
from docker import DockerClient
from docker.models.containers import Container
from docker.models.networks import Network

from agent_proxy import egress
from agent_proxy.config import Grant, load_config
from agent_proxy.credentials import CredentialStore
from agent_proxy.plugins import CommandAction
from agent_proxy.reload import LiveConfig, Snapshot
from agent_proxy.results import new_call_id
from agent_proxy.runner import CageCall, CageLimiter, CageRunner

TEST_LABELS = {"agent-proxy.test": "1"}
COM = ("example.com",)
ORG = ("example.org",)
NET = ("example.net",)
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

EGRESS_LABEL = "agent-proxy.egress"
CONFIG_FILE = "proxy.yaml"
TCP_LISTEN = "0A"
PROC_NET_SCRIPT = (
    "set -e; for name in tcp tcp6 udp udp6; do "
    'echo "file $name"; cat "/proc/net/$name"; done'
)
HELPER_TIMEOUT_SECONDS = 30
CAGE_START_SECONDS = 30
PRUNE_SECONDS = 30
POLL_SECONDS = 0.2
SOURCE_IP_RANGE = ipaddress.ip_network("172.31.9.0/24")
AGENT_TREE = "/srv/agents/it"
PLUGIN_IMAGE = "ghcr.io/example/probe@sha256:" + "a" * 64


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

    def keyed_network(self) -> str:
        """Create an internal network labelled as ensure labels one; return its key."""
        key = secrets.token_hex(8)
        network = self.client.networks.create(
            egress.network_name(key),
            driver="bridge",
            internal=True,
            labels={EGRESS_LABEL: key} | TEST_LABELS,
        )
        self.networks.append(network)
        return key

    def read_in_namespace(self, container: Container, script: str) -> str:
        """Return the stdout of script, run in container's network namespace."""
        helper = self.client.containers.create(
            self.image,
            command=["sh", "-c", script],
            network_mode=f"container:{container.id}",
            labels=TEST_LABELS,
        )
        self.containers.append(helper)
        helper.start()
        status = helper.wait(timeout=HELPER_TIMEOUT_SECONDS)["StatusCode"]
        if status != 0:
            pytest.fail(f"helper exited {status}; log {helper.logs()!r}")
        return helper.logs(stdout=True, stderr=False).decode()

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
        # ExitStack runs its callbacks last-registered first, and runs every
        # one even after an earlier one raised, then re-raises.
        with contextlib.ExitStack() as steps:
            for network in self.networks:
                steps.callback(_remove_network, network)
            steps.callback(remove_bridge, self.client)
            steps.callback(self.proxies.prune, ())
            for container in self.containers:
                steps.callback(_remove_container, container)


@dataclass(frozen=True)
class _Socket:
    protocol: str
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int


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


def _remove_container(container: Container) -> None:
    with contextlib.suppress(docker.errors.NotFound):
        container.remove(force=True)


def _remove_network(network: Network) -> None:
    with contextlib.suppress(docker.errors.NotFound):
        network.remove()


def _list_name(domains: Sequence[str]) -> str:
    """Return the name of a list's network and of its proxy."""
    return egress.network_name(egress.list_key(domains))


def _container_status(client: DockerClient, name: str) -> str | None:
    """Return the named container's status, or None when it does not exist."""
    try:
        return client.containers.get(name).status
    except docker.errors.NotFound:
        return None


def _network_exists(client: DockerClient, name: str) -> bool:
    try:
        client.networks.get(name)
    except docker.errors.NotFound:
        return False
    return True


def _listening_sockets(proc_net: str) -> set[_Socket]:
    """Return every listening TCP socket and every UDP socket in proc_net.

    proc_net holds /proc/net files, each after a line "file <name>".
    """
    sockets: set[_Socket] = set()
    protocol = ""
    for line in proc_net.splitlines():
        fields = line.split()
        if fields[:1] == ["file"]:
            protocol = fields[1].removesuffix("6")
        elif fields and fields[0].endswith(":"):
            if protocol == "udp" or fields[3] == TCP_LISTEN:
                sockets.add(_socket(protocol, fields[1]))
    return sockets


def _socket(protocol: str, local: str) -> _Socket:
    """Return the socket a /proc/net local_address field names.

    The address is hex, each 32-bit word little-endian; the port is big-endian
    hex.
    """
    address, port = local.split(":")
    raw = bytes.fromhex(address)
    ordered = b"".join(raw[start : start + 4][::-1] for start in range(0, len(raw), 4))
    return _Socket(protocol, ipaddress.ip_address(ordered), int(port, 16))


def _nc(port: int) -> str:
    """Return a shell line that prints whether a TCP connection to the proxy opens."""
    return (
        f"if nc -z -w 3 {egress.PROXY_ALIAS} {port}; "
        f"then echo '{port} open'; else echo '{port} closed'; fi"
    )


def _write_instance_config(path: Path, lists: Sequence[Sequence[str]]) -> Path:
    """Write an instance config with one grant per domain list, and return path."""
    grants = {
        f"probe.list_{index}": {
            "roots": [],
            "domains": list(domains),
            "credentials": {},
        }
        for index, domains in enumerate(lists)
    }
    document = {
        "agent_tree": AGENT_TREE,
        "overflow_dir": f"{AGENT_TREE}/overflow",
        "max_cages": MAX_CAGES,
        "plugins": {"probe": PLUGIN_IMAGE},
        "scopes": {"it": {"grants": grants}},
    }
    path.write_text(yaml.safe_dump(document))
    return path


def _snapshot(path: Path) -> Snapshot:
    return Snapshot(
        config=load_config(path, frozenset()),
        manifests=MappingProxyType({}),
        warnings=(),
    )


def _wait_for_cage(client: DockerClient, network_name: str) -> None:
    """Return once a running container other than the proxy is on network_name.

    A network's proxy carries the network's name, and only a running
    container has an endpoint on it.
    """
    deadline = time.monotonic() + CAGE_START_SECONDS
    while time.monotonic() < deadline:
        attached = client.networks.get(network_name).attrs["Containers"] or {}
        if any(entry["Name"] != network_name for entry in attached.values()):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"no cage joined network {network_name!r} within {CAGE_START_SECONDS} seconds"
    )


def _wait_for_list_pruned(client: DockerClient, domains: Sequence[str]) -> None:
    """Return once a list's proxy container and network are both gone."""
    name = _list_name(domains)
    deadline = time.monotonic() + PRUNE_SECONDS
    while time.monotonic() < deadline:
        if _container_status(client, name) is None and not _network_exists(
            client, name
        ):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"the proxy and network of list {list(domains)!r} were not pruned"
        f" within {PRUNE_SECONDS} seconds"
    )


def _write_proxy_config(directory: Path, transforms: list[dict[str, object]]) -> Path:
    """Write the config render_config gives example.com, with transforms replaced."""
    document = yaml.safe_load(egress.render_config(COM))
    document["transforms"] = transforms
    directory.mkdir(parents=True)
    (directory / CONFIG_FILE).write_text(yaml.safe_dump(document, sort_keys=False))
    return directory


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
        for path in sorted(bench.config_root.rglob(CONFIG_FILE))
    ]

    assert len(documents) == 2
    for document in documents:
        assert isinstance(document, dict)
        assert _keys(document).isdisjoint({"source_ip", "rules"})


def test_only_the_tunnel_is_reachable_on_a_proxy(bench: _Bench) -> None:
    """Every proxy socket off loopback is the tunnel; a cage reaches no other port."""
    bench.proxies.ensure([COM])
    proxy = bench.client.containers.get(_list_name(COM))
    sockets = _listening_sockets(bench.read_in_namespace(proxy, PROC_NET_SCRIPT))
    exposed = {(s.protocol, s.port) for s in sockets if not s.address.is_loopback}
    assert exposed == {("tcp", TUNNEL_PORT)}, sockets
    other_ports = sorted({s.port for s in sockets} - {TUNNEL_PORT})

    lines = bench.run_on_list(COM, *(_nc(port) for port in [*other_ports, TUNNEL_PORT]))

    assert lines == [
        *(f"{port} closed" for port in other_ports),
        f"{TUNNEL_PORT} open",
    ]


def test_a_dropped_lists_proxy_goes_after_its_last_cage(
    bench: _Bench, tmp_path: Path
) -> None:
    """A reload keeps a dropped list's proxy until the call using it ends."""
    first = _write_instance_config(tmp_path / "first.yaml", [COM, ORG])
    second = _write_instance_config(tmp_path / "second.yaml", [COM, NET])
    live = LiveConfig.start(functools.partial(_snapshot, first), bench.proxies)

    with live.use(), ThreadPoolExecutor(max_workers=1) as pool:
        cage = pool.submit(bench.run_on_list, ORG, "sleep 5")
        _wait_for_cage(bench.client, _list_name(ORG))
        live.reload(functools.partial(_snapshot, second))
        assert _container_status(bench.client, _list_name(NET)) == "running"
        assert _container_status(bench.client, _list_name(ORG)) == "running"
        assert _network_exists(bench.client, _list_name(ORG))
        cage.result()

    _wait_for_list_pruned(bench.client, ORG)
    assert _container_status(bench.client, _list_name(COM)) == "running"


def test_iron_proxy_ignores_a_rules_source_ip(bench: _Bench) -> None:
    """A cage outside a rule's source_ip range still reaches the rule's host.

    This test passes while iron-proxy ignores source_ip, which is why no
    config the core writes carries it. An iron-proxy upgrade repeats this
    test first.
    """
    bench.proxies.ensure(())
    key = bench.keyed_network()
    name = egress.network_name(key)
    subnets = [
        ipaddress.ip_network(entry["Subnet"])
        for entry in bench.client.networks.get(name).attrs["IPAM"]["Config"]
    ]
    if not subnets or any(subnet.overlaps(SOURCE_IP_RANGE) for subnet in subnets):
        pytest.fail(
            f"inconclusive: network {name!r} has subnets {subnets!r}, so a cage "
            f"on it may be inside {SOURCE_IP_RANGE}"
        )
    rule = {"host": "example.com", "source_ip": str(SOURCE_IP_RANGE)}
    directory = _write_proxy_config(
        bench.config_root / name,
        [{"name": "allowlist", "config": {"rules": [rule]}}],
    )
    bench.containers.append(egress.start_proxy(bench.client, key, directory))
    network = egress.CageNetwork(name=name, environment={"HTTPS_PROXY": PROXY_URL})

    pairs = bench.run(network, COM, _curl(COM_URL), _curl(ORG_URL))

    assert pairs == ["200 200", "000 403"]


def test_a_config_iron_proxy_refuses_leaves_no_proxy(bench: _Bench) -> None:
    """start_proxy on a config naming an unknown transform raises and removes it."""
    bench.proxies.ensure(())
    key = bench.keyed_network()
    name = egress.network_name(key)
    directory = _write_proxy_config(
        bench.config_root / name,
        [{"name": "apx-no-such-transform", "config": {}}],
    )

    with pytest.raises(egress.EgressStartError):
        egress.start_proxy(bench.client, key, directory)

    assert _container_status(bench.client, name) is None
