import dataclasses
import hashlib
import logging
import re
import shutil
import stat
from collections.abc import Callable, Collection, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

import docker.errors
import docker.types
import pytest
import requests
import yaml

from agent_proxy import egress
from agent_proxy.config import InstanceConfig
from agent_proxy.egress import (
    BRIDGE_NETWORK,
    IRON_PROXY_IMAGE,
    LABELS,
    PROXY_LOG_EXCERPT_BYTES,
    PROXY_READY_TIMEOUT_SECONDS,
    CageNetwork,
    EgressConfigError,
    EgressError,
    EgressProxies,
    EgressStartError,
    cage_network,
    domain_lists,
    list_key,
    network_name,
    proxy_status,
    render_config,
    start_proxy,
)
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import PluginImageError

IMAGE = "ghcr.io/example/git@sha256:" + "a" * 64
EMPTY_LIST_KEY = "e3b0c44298fc1c14"
PROXY_VARIABLE = {"HTTPS_PROXY": "http://egress:1080"}
RENDERED_LISTS = [
    pytest.param(["example.com"], id="one-domain"),
    pytest.param(["github.com", "api.github.com"], id="two-domains"),
    pytest.param(
        ["b.example.org", "a.example.org", "c.example.net"], id="three-domains"
    ),
]
KEY = "0123456789abcdef"
PROXY_NAME = "apx-egress-0123456789abcdef"
CONFIG_DIR = Path("/srv/egress/0123456789abcdef")
TUNNEL_LINE = (
    b'{"time":"2026-09-30T11:46:12.482870049Z","level":"INFO",'
    b'"msg":"tunnel proxy starting","addr":"[::]:1080"}\n'
)
EARLIER_LINE = (
    b'{"time":"2026-09-30T11:46:12.402Z","level":"INFO","msg":"iron-proxy starting"}\n'
)
DOCKER_ERRORS = [
    pytest.param(docker.errors.APIError("daemon said no"), id="api-error"),
    pytest.param(
        requests.exceptions.ConnectionError("daemon said no"), id="connection-error"
    ),
]
LISTED = ("example.com",)
LISTED_KEY = list_key(LISTED)
LISTED_NAME = network_name(LISTED_KEY)
OTHER = ("api.github.com", "github.com")
OTHER_KEY = list_key(OTHER)
OTHER_NAME = network_name(OTHER_KEY)
TEST_LABELS = MappingProxyType({"agent-proxy.test": "1"})


class _FakeClock:
    """A monotonic clock that only sleep advances."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Return the time the sleeps so far add up to."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record the sleep and advance the clock by it."""
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Replace the time module egress reads with a fake clock."""
    fake = _FakeClock()
    monkeypatch.setattr(
        egress, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep)
    )
    return fake


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Replace the start_proxy that egress calls with a double."""
    double = mock.MagicMock()
    monkeypatch.setattr(egress, "start_proxy", double)
    return double


def _client(*logs: bytes, status: str = "running") -> mock.MagicMock:
    """Return a client whose proxy reads each log in turn, then the last forever."""
    client = mock.MagicMock()
    container = _container(client)
    container.status = status
    reads = [*logs] or [b""]

    def read_log() -> bytes:
        return reads.pop(0) if len(reads) > 1 else reads[0]

    container.logs.side_effect = read_log
    return client


def _container(client: mock.MagicMock) -> mock.MagicMock:
    return client.containers.create.return_value


def _failing_bridge_lookup(client: mock.MagicMock, error: Exception) -> None:
    client.networks.get.side_effect = error


def _failing_connect(client: mock.MagicMock, error: Exception) -> None:
    client.networks.get.return_value.connect.side_effect = error


def _failing_start(client: mock.MagicMock, error: Exception) -> None:
    _container(client).start.side_effect = error


def _failing_log_read(client: mock.MagicMock, error: Exception) -> None:
    _container(client).logs.side_effect = error


def _failing_reload(client: mock.MagicMock, error: Exception) -> None:
    _container(client).reload.side_effect = error


def _instance_config(scopes: dict[str, dict[str, list[str]]]) -> InstanceConfig:
    """Return a config whose scopes map each grant key to its domains."""
    return InstanceConfig.model_validate(
        {
            "agent_tree": "/srv/agents/alpha",
            "overflow_dir": "/srv/agents/alpha/overflow",
            "max_cages": 1,
            "plugins": {"git": IMAGE},
            "scopes": {
                scope: {
                    "grants": {
                        key: {"roots": [], "domains": domains, "credentials": {}}
                        for key, domains in grants.items()
                    }
                }
                for scope, grants in scopes.items()
            },
        }
    )


def _keys(node: object) -> Iterator[object]:
    """Yield every mapping key at any depth of a parsed YAML document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


def _docker(
    *, networks: Collection[str] = (), proxy: mock.MagicMock | None = None
) -> mock.MagicMock:
    """Return a client holding the proxy image and only the named networks.

    The one proxy container, when given, is the listed list's.
    """
    client = mock.MagicMock()

    def get_network(name: str) -> mock.MagicMock:
        if name not in networks:
            raise docker.errors.NotFound(f"network {name} not found")
        return mock.MagicMock()

    def get_container(name: str) -> mock.MagicMock:
        if proxy is None or name != LISTED_NAME:
            raise docker.errors.NotFound(f"container {name} not found")
        return proxy

    client.networks.get.side_effect = get_network
    client.containers.get.side_effect = get_container
    return client


def _proxy(*, status: str = "running", image: str = IRON_PROXY_IMAGE) -> mock.MagicMock:
    proxy = mock.MagicMock()
    proxy.status = status
    proxy.attrs = {"Config": {"Image": image}}
    return proxy


def _write_listed_config(root: Path, config: bytes) -> Path:
    directory = root / LISTED_NAME
    directory.mkdir()
    (directory / "proxy.yaml").write_bytes(config)
    return directory


def _network_call(
    name: str,
    value: str,
    *,
    internal: bool,
    labels: Mapping[str, str] = LABELS,
) -> object:
    return mock.call(
        name,
        driver="bridge",
        internal=internal,
        labels={"agent-proxy.egress": value, **labels},
    )


def _failing_network_lookup(client: mock.MagicMock, error: Exception) -> None:
    client.networks.get.side_effect = error


def _failing_network_create(client: mock.MagicMock, error: Exception) -> None:
    client.networks.create.side_effect = error


def _failing_proxy_lookup(client: mock.MagicMock, error: Exception) -> None:
    client.containers.get.side_effect = error


def _failing_proxy_removal(client: mock.MagicMock, error: Exception) -> None:
    client.containers.get(LISTED_NAME).remove.side_effect = error


def _labelled(
    *,
    containers: Collection[mock.MagicMock] = (),
    networks: Collection[mock.MagicMock] = (),
) -> mock.MagicMock:
    """Return a client whose egress label filter finds these resources."""
    client = mock.MagicMock()
    client.containers.list.return_value = list(containers)
    client.networks.list.return_value = list(networks)
    return client


def _egress_container(key: str) -> mock.MagicMock:
    container = mock.MagicMock()
    container.name = network_name(key)
    container.labels = {"agent-proxy.egress": key}
    return container


def _egress_network(name: str, value: str) -> mock.MagicMock:
    network = mock.MagicMock()
    network.name = name
    network.attrs = {"Labels": {"agent-proxy.egress": value}}
    return network


def _write_config_dirs(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir()
        (root / name / "proxy.yaml").write_bytes(b"dns: {}\n")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the message of each record egress logged, all of them warnings."""
    records = [
        record for record in caplog.records if record.name == "agent_proxy.egress"
    ]
    assert {record.levelno for record in records} <= {logging.WARNING}
    return [record.getMessage() for record in records]


@pytest.mark.parametrize(
    ("error_class", "base"),
    [
        (EgressError, ProxyError),
        (EgressConfigError, EgressError),
        (EgressStartError, EgressError),
    ],
)
def test_each_egress_error_derives_from_its_base(
    error_class: type[Exception], base: type[Exception]
) -> None:
    """Each egress error subclasses the error its name says it refines."""
    assert issubclass(error_class, base)


def test_list_key_hashes_the_sorted_domains_joined_by_newlines() -> None:
    """The key is the first 16 hex digits of the SHA-256 of the sorted list."""
    expected = hashlib.sha256(b"a.example.com\nb.example.com").hexdigest()[:16]

    assert list_key(["b.example.com", "a.example.com"]) == expected


def test_list_key_is_the_same_for_the_same_domains_in_any_order() -> None:
    """Two orders of one list give one key."""
    assert list_key(["c.io", "a.io", "b.io"]) == list_key(["b.io", "c.io", "a.io"])


def test_list_key_of_the_empty_list_hashes_the_empty_string() -> None:
    """The empty list has a key like any other list."""
    assert list_key([]) == EMPTY_LIST_KEY


def test_network_name_prefixes_the_key() -> None:
    """A list's network and its proxy container are named apx-egress-<key>."""
    assert network_name("0123456789abcdef") == "apx-egress-0123456789abcdef"


def test_the_bridge_network_has_its_fixed_name() -> None:
    """Every proxy joins the one bridge, apx-egress-bridge."""
    assert BRIDGE_NETWORK == "apx-egress-bridge"


def test_cage_network_gives_a_listed_cage_its_network_and_the_proxy_variable() -> None:
    """A non-empty list names its network and sets only HTTPS_PROXY."""
    assert cage_network(["b.io", "a.io"]) == CageNetwork(
        name=network_name(list_key(["a.io", "b.io"])), environment=PROXY_VARIABLE
    )


def test_cage_network_gives_the_empty_list_its_network_and_no_variable() -> None:
    """The empty list's network holds no proxy, so the cage gets no variable."""
    assert cage_network([]) == CageNetwork(
        name=f"apx-egress-{EMPTY_LIST_KEY}", environment={}
    )


def test_cage_network_is_frozen() -> None:
    """A cage network refuses assignment."""
    network = cage_network([])

    with pytest.raises(dataclasses.FrozenInstanceError):
        network.name = "apx-egress-other"


def test_domain_lists_returns_each_distinct_sorted_list_of_every_scope() -> None:
    """Lists equal after sorting count once, and the empty list counts too."""
    config = _instance_config(
        {
            "dev": {"git.clone": ["b.io", "a.io"], "git.fetch": ["c.io"]},
            "ops": {"git.clone": ["a.io", "b.io"], "git.push": []},
        }
    )

    assert domain_lists(config) == {("a.io", "b.io"), ("c.io",), ()}


def test_domain_lists_leaves_out_the_empty_list_when_no_grant_names_it() -> None:
    """The empty tuple is there only for a grant with domains: []."""
    config = _instance_config({"dev": {"git.clone": ["a.io"]}})

    assert domain_lists(config) == {("a.io",)}


def test_proxy_status_returns_the_status_of_the_list_s_proxy() -> None:
    """The proxy is looked up by the name its list's key gives it."""
    client = _docker(proxy=_proxy(status="exited"))

    assert proxy_status(client, ["example.com"]) == "exited"
    client.containers.get.assert_called_once_with(LISTED_NAME)


def test_proxy_status_returns_none_for_an_absent_proxy() -> None:
    """NotFound says the proxy is absent, which is no error."""
    client = _docker()

    assert proxy_status(client, OTHER) is None


@pytest.mark.parametrize("error", DOCKER_ERRORS)
def test_proxy_status_refuses_a_failed_lookup(error: Exception) -> None:
    """Another Docker failure is an EgressError that names the proxy."""
    client = _docker()
    _failing_proxy_lookup(client, error)

    with pytest.raises(EgressError) as excinfo:
        proxy_status(client, LISTED)

    assert str(excinfo.value) == (
        f"proxy {LISTED_NAME!r} could not be looked up: daemon said no"
    )
    assert type(excinfo.value) is EgressError


def test_render_config_renders_the_proxy_config_with_the_domains_sorted() -> None:
    """The UTF-8 YAML parses to exactly the proxy config, and nothing else."""
    rendered = render_config(["b.example.org", "a.example.org"])

    assert yaml.safe_load(rendered.decode("utf-8")) == {
        "dns": {"listen": "127.0.0.1:53", "proxy_ip": "127.0.0.1"},
        "proxy": {
            "http_listen": "127.0.0.1:80",
            "https_listen": "127.0.0.1:443",
            "tunnel_listen": ":1080",
        },
        "metrics": {"listen": "127.0.0.1:9090"},
        "tls": {"mode": "sni-only"},
        "transforms": [
            {
                "name": "allowlist",
                "config": {"domains": ["a.example.org", "b.example.org"]},
            }
        ],
    }


@pytest.mark.parametrize("domains", RENDERED_LISTS)
def test_render_config_leaves_no_place_for_a_source_ip_rule(
    domains: list[str],
) -> None:
    """No key at any depth is source_ip or rules."""
    keys = set(_keys(yaml.safe_load(render_config(domains))))

    assert "source_ip" not in keys
    assert "rules" not in keys


@pytest.mark.parametrize("domains", RENDERED_LISTS)
def test_render_config_turns_the_management_listener_off(domains: list[str]) -> None:
    """No key at any depth is management."""
    assert "management" not in set(_keys(yaml.safe_load(render_config(domains))))


def test_render_config_refuses_the_empty_list() -> None:
    """A proxy with nothing to allow is refused."""
    with pytest.raises(EgressConfigError, match="at least one domain"):
        render_config([])


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.com",
        "*.example.com",
        "Example.com",
        "localhost",
        "example.com\n",
        "example.com\nrules: []",
    ],
)
def test_render_config_refuses_a_domain_the_pattern_does_not_fully_match(
    domain: str,
) -> None:
    """The refusal names the offending domain."""
    with pytest.raises(EgressConfigError, match=re.escape(repr(domain))):
        render_config(["github.com", domain])


def test_the_proxy_image_is_the_pinned_iron_proxy() -> None:
    """The proxy runs iron-proxy 0.52.0, pinned by digest."""
    assert IRON_PROXY_IMAGE == (
        "ironsh/iron-proxy:0.52.0"
        "@sha256:004fdc067dc4cc47cebfc0dab3c63ab9a5e21a3230c965e69775a565df99efc6"
    )


def test_the_start_limits_are_the_spec_s() -> None:
    """Ten seconds to become ready, and a 2 KiB log tail in a refusal."""
    assert PROXY_READY_TIMEOUT_SECONDS == 10
    assert PROXY_LOG_EXCERPT_BYTES == 2048


def test_labels_are_an_empty_read_only_mapping() -> None:
    """Production adds no labels; the integration conftest patches them in."""
    assert isinstance(LABELS, MappingProxyType)
    assert dict(LABELS) == {}


@pytest.mark.usefixtures("clock")
def test_start_proxy_creates_the_hardened_proxy_on_its_network() -> None:
    """Pinned image, config command, root, no writes, no capabilities, one mount."""
    client = _client(TUNNEL_LINE)

    start_proxy(client, KEY, CONFIG_DIR)

    client.api.create_endpoint_config.assert_called_once_with(aliases=["egress"])
    client.containers.create.assert_called_once_with(
        IRON_PROXY_IMAGE,
        command=["-config", "/etc/iron-proxy/proxy.yaml"],
        name=PROXY_NAME,
        user="0:0",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        mounts=[
            docker.types.Mount(
                target="/etc/iron-proxy",
                source=str(CONFIG_DIR),
                type="bind",
                read_only=True,
            )
        ],
        network=PROXY_NAME,
        networking_config={PROXY_NAME: client.api.create_endpoint_config.return_value},
        labels={"agent-proxy.egress": KEY},
    )


@pytest.mark.usefixtures("clock")
def test_start_proxy_adds_labels_to_the_proxy_s_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The patched labels join the key label."""
    monkeypatch.setattr(egress, "LABELS", MappingProxyType({"agent-proxy.test": "1"}))
    client = _client(TUNNEL_LINE)

    start_proxy(client, KEY, CONFIG_DIR)

    assert client.containers.create.call_args.kwargs["labels"] == {
        "agent-proxy.egress": KEY,
        "agent-proxy.test": "1",
    }


@pytest.mark.usefixtures("clock")
def test_start_proxy_joins_the_bridge_before_it_starts() -> None:
    """The proxy connects to the bridge network, then starts."""
    client = _client(TUNNEL_LINE)

    start_proxy(client, KEY, CONFIG_DIR)

    client.networks.get.assert_called_once_with(BRIDGE_NETWORK)
    client.networks.get.return_value.connect.assert_called_once_with(_container(client))
    names = [name for name, _, _ in client.mock_calls]
    assert names.index("networks.get().connect") < names.index(
        "containers.create().start"
    )


def test_start_proxy_returns_the_proxy_once_its_log_holds_the_tunnel_line(
    clock: _FakeClock,
) -> None:
    """It polls every tenth of a second until the tunnel line appears."""
    client = _client(b"", EARLIER_LINE, EARLIER_LINE + TUNNEL_LINE)

    assert start_proxy(client, KEY, CONFIG_DIR) is _container(client)

    assert clock.sleeps == [0.1, 0.1]
    _container(client).remove.assert_not_called()


@pytest.mark.usefixtures("clock")
def test_start_proxy_reads_past_a_line_that_is_not_json() -> None:
    """A line that is not JSON, or not UTF-8, is not the tunnel line."""
    client = _client(b"not json\n\xff\xfe\n" + TUNNEL_LINE)

    assert start_proxy(client, KEY, CONFIG_DIR) is _container(client)


@pytest.mark.usefixtures("clock")
@pytest.mark.parametrize(
    "log",
    [
        pytest.param(b"tunnel proxy starting\n", id="plain-text"),
        pytest.param(b'"tunnel proxy starting"\n', id="json-string"),
        pytest.param(
            b'{"msg":"listener failed","detail":"tunnel proxy starting"}\n',
            id="another-field",
        ),
        pytest.param(EARLIER_LINE, id="earlier-line"),
    ],
)
def test_start_proxy_is_not_ready_without_a_json_line_whose_msg_is_the_tunnel_s(
    log: bytes,
) -> None:
    """Only a JSON object whose msg is the tunnel's message marks readiness."""
    client = _client(log)

    with pytest.raises(EgressStartError):
        start_proxy(client, KEY, CONFIG_DIR)


def test_start_proxy_refuses_a_proxy_not_ready_in_time_and_removes_it(
    clock: _FakeClock,
) -> None:
    """After ten seconds of polling, the proxy is removed and refused."""
    client = _client(EARLIER_LINE)

    with pytest.raises(EgressStartError, match="within 10 seconds") as excinfo:
        start_proxy(client, KEY, CONFIG_DIR)

    assert PROXY_NAME in str(excinfo.value)
    assert set(clock.sleeps) == {0.1}
    assert PROXY_READY_TIMEOUT_SECONDS <= clock.now < PROXY_READY_TIMEOUT_SECONDS + 0.2
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.parametrize(
    "log",
    [
        pytest.param(EARLIER_LINE, id="before-the-tunnel-line"),
        pytest.param(EARLIER_LINE + TUNNEL_LINE, id="after-the-tunnel-line"),
    ],
)
def test_start_proxy_refuses_a_proxy_that_stops_and_removes_it(
    clock: _FakeClock, log: bytes
) -> None:
    """A stopped proxy is a failed start, whatever its log holds."""
    client = _client(log, status="exited")

    with pytest.raises(EgressStartError, match="stopped") as excinfo:
        start_proxy(client, KEY, CONFIG_DIR)

    assert PROXY_NAME in str(excinfo.value)
    assert clock.sleeps == []
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.usefixtures("clock")
def test_start_proxy_reloads_the_proxy_s_state_before_it_reads_it() -> None:
    """A proxy that stops is seen on the poll after it stops."""
    client = _client(EARLIER_LINE + TUNNEL_LINE)
    container = _container(client)
    container.reload.side_effect = lambda: setattr(container, "status", "exited")

    with pytest.raises(EgressStartError, match="stopped"):
        start_proxy(client, KEY, CONFIG_DIR)


@pytest.mark.usefixtures("clock")
def test_start_proxy_reads_the_log_before_it_refreshes_the_state() -> None:
    """A running state read after the log proves the proxy outlived the log."""
    client = _client(TUNNEL_LINE)

    start_proxy(client, KEY, CONFIG_DIR)

    names = [name for name, _, _ in _container(client).mock_calls]
    assert names.index("logs") < names.index("reload")


@pytest.mark.usefixtures("clock")
def test_a_start_refusal_carries_the_tail_of_the_proxy_s_log() -> None:
    """The refusal holds the last 2 KiB of the log and nothing before it."""
    client = _client(b"<" * 10 + b"z" * PROXY_LOG_EXCERPT_BYTES, status="exited")

    with pytest.raises(EgressStartError) as excinfo:
        start_proxy(client, KEY, CONFIG_DIR)

    message = str(excinfo.value)
    assert "z" * PROXY_LOG_EXCERPT_BYTES in message
    assert "<" not in message


@pytest.mark.usefixtures("clock")
@pytest.mark.parametrize("error", DOCKER_ERRORS)
@pytest.mark.parametrize(
    "break_step",
    [
        pytest.param(_failing_bridge_lookup, id="bridge-lookup"),
        pytest.param(_failing_connect, id="bridge-connect"),
        pytest.param(_failing_start, id="start"),
        pytest.param(_failing_log_read, id="log-read"),
        pytest.param(_failing_reload, id="reload"),
    ],
)
def test_a_docker_failure_after_the_create_removes_the_proxy(
    break_step: Callable[[mock.MagicMock, Exception], None], error: Exception
) -> None:
    """The refusal is a plain EgressError naming the proxy and the cause."""
    client = _client(TUNNEL_LINE)
    break_step(client, error)

    with pytest.raises(EgressError) as excinfo:
        start_proxy(client, KEY, CONFIG_DIR)

    assert type(excinfo.value) is EgressError
    assert PROXY_NAME in str(excinfo.value)
    assert "daemon said no" in str(excinfo.value)
    _container(client).remove.assert_called_once_with(force=True)


@pytest.mark.usefixtures("clock")
@pytest.mark.parametrize("error", DOCKER_ERRORS)
def test_a_refused_create_raises_egress_error_and_goes_no_further(
    error: Exception,
) -> None:
    """With no container made, nothing joins the bridge or is removed."""
    client = _client(TUNNEL_LINE)
    client.containers.create.side_effect = error

    with pytest.raises(EgressError) as excinfo:
        start_proxy(client, KEY, CONFIG_DIR)

    assert type(excinfo.value) is EgressError
    assert PROXY_NAME in str(excinfo.value)
    assert "daemon said no" in str(excinfo.value)
    client.networks.get.assert_not_called()


@pytest.mark.usefixtures("clock")
def test_a_failed_removal_names_the_removal_and_the_start_failure() -> None:
    """A proxy Docker will not remove is reported with the failure behind it."""
    client = _client(EARLIER_LINE, status="exited")
    _container(client).remove.side_effect = docker.errors.APIError("remove refused")

    with pytest.raises(EgressError) as excinfo:
        start_proxy(client, KEY, CONFIG_DIR)

    message = str(excinfo.value)
    assert type(excinfo.value) is EgressError
    assert f"proxy {PROXY_NAME!r} could not be removed: remove refused" in message
    assert f"proxy {PROXY_NAME!r} stopped before it was ready" in message
    assert type(excinfo.value.__cause__) is EgressStartError


@pytest.mark.usefixtures("started")
def test_ensure_makes_the_proxy_image_present(tmp_path: Path) -> None:
    """An image already present is not pulled again."""
    client = _docker()

    EgressProxies(client, tmp_path).ensure(())

    client.images.get.assert_called_once_with(IRON_PROXY_IMAGE)
    client.images.pull.assert_not_called()


def test_ensure_passes_a_failed_pull_through_unchanged(
    tmp_path: Path, started: mock.MagicMock
) -> None:
    """The image comes first: a failed pull creates nothing."""
    client = _docker()
    client.images.get.side_effect = docker.errors.ImageNotFound("no such image")
    client.images.pull.side_effect = docker.errors.APIError("daemon said no")

    with pytest.raises(PluginImageError) as excinfo:
        EgressProxies(client, tmp_path).ensure([LISTED])

    assert type(excinfo.value) is PluginImageError
    client.networks.create.assert_not_called()
    started.assert_not_called()


def test_ensure_of_no_lists_makes_only_the_image_and_the_bridge_present(
    tmp_path: Path, started: mock.MagicMock
) -> None:
    """With no lists, no internal network, proxy, or config is made."""
    client = _docker()

    EgressProxies(client, tmp_path).ensure(())

    assert client.networks.create.call_args_list == [
        _network_call(BRIDGE_NETWORK, "bridge", internal=False)
    ]
    client.containers.get.assert_not_called()
    started.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("started")
def test_ensure_creates_the_bridge_and_each_list_s_internal_network(
    tmp_path: Path,
) -> None:
    """The bridge is a plain network; each list's network is internal."""
    client = _docker()

    EgressProxies(client, tmp_path).ensure([LISTED, OTHER])

    assert client.networks.create.call_args_list == [
        _network_call(BRIDGE_NETWORK, "bridge", internal=False),
        _network_call(LISTED_NAME, LISTED_KEY, internal=True),
        _network_call(OTHER_NAME, OTHER_KEY, internal=True),
    ]


@pytest.mark.usefixtures("started")
def test_ensure_adds_labels_to_the_networks_it_creates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network carries LABELS beside its own egress label."""
    monkeypatch.setattr(egress, "LABELS", TEST_LABELS)
    client = _docker()

    EgressProxies(client, tmp_path).ensure([LISTED])

    assert client.networks.create.call_args_list == [
        _network_call(BRIDGE_NETWORK, "bridge", internal=False, labels=TEST_LABELS),
        _network_call(LISTED_NAME, LISTED_KEY, internal=True, labels=TEST_LABELS),
    ]


def test_ensure_creates_nothing_that_is_present(
    tmp_path: Path, started: mock.MagicMock
) -> None:
    """A running proxy on the pinned image, from the rendered config, is kept."""
    proxy = _proxy()
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME}, proxy=proxy)
    _write_listed_config(tmp_path, render_config(LISTED))

    EgressProxies(client, tmp_path).ensure([LISTED])

    client.networks.create.assert_not_called()
    proxy.remove.assert_not_called()
    started.assert_not_called()


def test_ensure_gives_the_empty_list_a_network_and_no_proxy(
    tmp_path: Path, started: mock.MagicMock
) -> None:
    """The empty list's network holds no proxy, so no config is written."""
    client = _docker(networks={BRIDGE_NETWORK})

    EgressProxies(client, tmp_path).ensure([()])

    assert client.networks.create.call_args_list == [
        _network_call(network_name(EMPTY_LIST_KEY), EMPTY_LIST_KEY, internal=True)
    ]
    client.containers.get.assert_not_called()
    started.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_ensure_writes_the_config_then_starts_an_absent_proxy(
    tmp_path: Path, started: mock.MagicMock
) -> None:
    """The proxy starts from a config already rendered on disk."""
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME})
    configs_at_start: list[bytes] = []
    started.side_effect = lambda _client, _key, config_dir: configs_at_start.append(
        (config_dir / "proxy.yaml").read_bytes()
    )

    EgressProxies(client, tmp_path).ensure([LISTED])

    started.assert_called_once_with(client, LISTED_KEY, tmp_path / LISTED_NAME)
    assert configs_at_start == [render_config(LISTED)]


def test_ensure_starts_a_proxy_for_each_listed_list(
    tmp_path: Path, started: mock.MagicMock
) -> None:
    """Each non-empty list gets its own proxy, in the order given."""
    client = _docker()

    EgressProxies(client, tmp_path).ensure([LISTED, OTHER])

    assert started.call_args_list == [
        mock.call(client, LISTED_KEY, tmp_path / LISTED_NAME),
        mock.call(client, OTHER_KEY, tmp_path / OTHER_NAME),
    ]


@pytest.mark.usefixtures("started")
def test_ensure_keeps_the_config_directory_and_file_private(tmp_path: Path) -> None:
    """Only the instance's user can read a proxy's config."""
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME})

    EgressProxies(client, tmp_path).ensure([LISTED])

    directory = tmp_path / LISTED_NAME
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "proxy.yaml").stat().st_mode) == 0o600


@pytest.mark.usefixtures("started")
def test_ensure_narrows_a_config_directory_and_file_it_finds_open(
    tmp_path: Path,
) -> None:
    """A config left open by another writer is narrowed and rewritten."""
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME})
    directory = _write_listed_config(tmp_path, b"stale: true\n")
    directory.chmod(0o755)
    (directory / "proxy.yaml").chmod(0o644)

    EgressProxies(client, tmp_path).ensure([LISTED])

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "proxy.yaml").stat().st_mode) == 0o600
    assert (directory / "proxy.yaml").read_bytes() == render_config(LISTED)


@pytest.mark.usefixtures("started")
def test_ensure_renames_a_dot_staging_file_into_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy never reads a half-written config."""
    renames: list[tuple[Path, Path]] = []
    replace = Path.replace

    def recording_replace(self: Path, target: Path) -> Path:
        renames.append((self, Path(target)))
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", recording_replace)
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME})

    EgressProxies(client, tmp_path).ensure([LISTED])

    directory = tmp_path / LISTED_NAME
    [(staging, target)] = renames
    assert target == directory / "proxy.yaml"
    assert staging.parent == directory
    assert staging.name.startswith(".")
    assert [path.name for path in directory.iterdir()] == ["proxy.yaml"]


def test_a_failed_config_write_raises_egress_error_and_leaves_no_staging_file(
    tmp_path: Path, started: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails is refused, and nothing is started from it."""

    def failing_replace(self: Path, target: Path) -> Path:
        raise OSError("disk said no")

    monkeypatch.setattr(Path, "replace", failing_replace)
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME})

    with pytest.raises(EgressError, match="disk said no") as excinfo:
        EgressProxies(client, tmp_path).ensure([LISTED])

    assert type(excinfo.value) is EgressError
    assert LISTED_NAME in str(excinfo.value)
    assert list((tmp_path / LISTED_NAME).iterdir()) == []
    started.assert_not_called()


@pytest.mark.parametrize(
    ("proxy_state", "config"),
    [
        pytest.param({"status": "exited"}, render_config(LISTED), id="stopped"),
        pytest.param(
            {"image": "ironsh/iron-proxy:0.51.0"},
            render_config(LISTED),
            id="another-image",
        ),
        pytest.param({}, render_config(["example.org"]), id="another-config"),
        pytest.param({}, None, id="no-config"),
    ],
)
def test_ensure_replaces_a_proxy_it_cannot_keep(
    tmp_path: Path,
    started: mock.MagicMock,
    proxy_state: dict[str, str],
    config: bytes | None,
) -> None:
    """The old proxy goes before the new config is written and started."""
    proxy = _proxy(**proxy_state)
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME}, proxy=proxy)
    if config is not None:
        _write_listed_config(tmp_path, config)
    events: list[object] = []
    proxy.remove.side_effect = lambda **_: events.append("remove")
    started.side_effect = lambda _client, _key, config_dir: events.append(
        (config_dir / "proxy.yaml").read_bytes()
    )

    EgressProxies(client, tmp_path).ensure([LISTED])

    proxy.remove.assert_called_once_with(force=True)
    started.assert_called_once_with(client, LISTED_KEY, tmp_path / LISTED_NAME)
    assert events == ["remove", render_config(LISTED)]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(EgressStartError("proxy stopped"), id="start-refused"),
        pytest.param(EgressError("daemon said no"), id="docker-failure"),
    ],
)
def test_a_failed_start_removes_the_proxy_s_config_directory(
    tmp_path: Path, started: mock.MagicMock, error: EgressError
) -> None:
    """No config iron-proxy refused stays on disk."""
    client = _docker(networks={BRIDGE_NETWORK, LISTED_NAME})
    started.side_effect = error

    with pytest.raises(EgressError) as excinfo:
        EgressProxies(client, tmp_path).ensure([LISTED])

    assert excinfo.value is error
    assert not (tmp_path / LISTED_NAME).exists()


@pytest.mark.usefixtures("started")
@pytest.mark.parametrize("error", DOCKER_ERRORS)
@pytest.mark.parametrize(
    "break_step",
    [
        pytest.param(_failing_network_lookup, id="network-lookup"),
        pytest.param(_failing_network_create, id="network-create"),
        pytest.param(_failing_proxy_lookup, id="proxy-lookup"),
        pytest.param(_failing_proxy_removal, id="proxy-removal"),
    ],
)
def test_a_docker_failure_in_ensure_raises_egress_error(
    tmp_path: Path,
    break_step: Callable[[mock.MagicMock, Exception], None],
    error: Exception,
) -> None:
    """The refusal is a plain EgressError carrying the cause."""
    client = _docker(proxy=_proxy(status="exited"))
    break_step(client, error)

    with pytest.raises(EgressError, match="daemon said no") as excinfo:
        EgressProxies(client, tmp_path).ensure([LISTED])

    assert type(excinfo.value) is EgressError


def test_prune_finds_egress_resources_by_their_label(tmp_path: Path) -> None:
    """Stopped proxies count too, so the container list includes them."""
    client = _labelled()

    EgressProxies(client, tmp_path).prune(())

    client.containers.list.assert_called_once_with(
        all=True, filters={"label": ["agent-proxy.egress"]}
    )
    client.networks.list.assert_called_once_with(
        filters={"label": ["agent-proxy.egress"]}
    )


def test_prune_filters_on_every_entry_of_labels_when_it_is_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test run prunes only what a test run made."""
    monkeypatch.setattr(egress, "LABELS", TEST_LABELS)
    client = _labelled()

    EgressProxies(client, tmp_path).prune(())

    filters = {"label": ["agent-proxy.egress", "agent-proxy.test=1"]}
    client.containers.list.assert_called_once_with(all=True, filters=filters)
    client.networks.list.assert_called_once_with(filters=filters)


def test_prune_removes_only_unlisted_proxies_with_their_config_directories(
    tmp_path: Path,
) -> None:
    """A listed proxy and its config stay."""
    kept, dropped = _egress_container(LISTED_KEY), _egress_container(OTHER_KEY)
    _write_config_dirs(tmp_path, LISTED_NAME, OTHER_NAME)
    client = _labelled(containers=[kept, dropped])

    EgressProxies(client, tmp_path).prune([LISTED])

    kept.remove.assert_not_called()
    dropped.remove.assert_called_once_with(force=True)
    assert (tmp_path / LISTED_NAME / "proxy.yaml").exists()
    assert not (tmp_path / OTHER_NAME).exists()


def test_prune_removes_only_unlisted_networks_and_never_the_bridge(
    tmp_path: Path,
) -> None:
    """The bridge stays even when no list is left."""
    bridge = _egress_network(BRIDGE_NETWORK, "bridge")
    kept = _egress_network(LISTED_NAME, LISTED_KEY)
    dropped = _egress_network(OTHER_NAME, OTHER_KEY)
    client = _labelled(networks=[bridge, kept, dropped])

    EgressProxies(client, tmp_path).prune([LISTED])

    bridge.remove.assert_not_called()
    kept.remove.assert_not_called()
    dropped.remove.assert_called_once_with()


def test_prune_removes_a_proxy_before_its_network(tmp_path: Path) -> None:
    """A network still holding its proxy's endpoint cannot be removed."""
    container = _egress_container(OTHER_KEY)
    network = _egress_network(OTHER_NAME, OTHER_KEY)
    events: list[str] = []
    container.remove.side_effect = lambda **_: events.append("proxy")
    network.remove.side_effect = lambda: events.append("network")
    client = _labelled(containers=[container], networks=[network])

    EgressProxies(client, tmp_path).prune(())

    assert events == ["proxy", "network"]


def test_prune_is_quiet_about_a_proxy_that_has_no_config_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A proxy whose config is already gone is no failure."""
    dropped = _egress_container(OTHER_KEY)
    client = _labelled(containers=[dropped])
    caplog.set_level(logging.WARNING, logger="agent_proxy.egress")

    EgressProxies(client, tmp_path).prune(())

    dropped.remove.assert_called_once_with(force=True)
    assert _warnings(caplog) == []


@pytest.mark.parametrize("error", DOCKER_ERRORS)
def test_prune_logs_a_failed_proxy_listing_and_still_prunes_networks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """A container listing that fails does not stop the network prune."""
    dropped = _egress_network(OTHER_NAME, OTHER_KEY)
    client = _labelled(networks=[dropped])
    client.containers.list.side_effect = error
    caplog.set_level(logging.WARNING, logger="agent_proxy.egress")

    EgressProxies(client, tmp_path).prune([LISTED])

    dropped.remove.assert_called_once_with()
    [message] = _warnings(caplog)
    assert "daemon said no" in message


@pytest.mark.parametrize("error", DOCKER_ERRORS)
def test_prune_logs_a_failed_network_listing_after_pruning_proxies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """A network listing that fails leaves the proxy prune done."""
    dropped = _egress_container(OTHER_KEY)
    client = _labelled(containers=[dropped])
    client.networks.list.side_effect = error
    caplog.set_level(logging.WARNING, logger="agent_proxy.egress")

    EgressProxies(client, tmp_path).prune([LISTED])

    dropped.remove.assert_called_once_with(force=True)
    [message] = _warnings(caplog)
    assert "daemon said no" in message


@pytest.mark.parametrize("error", DOCKER_ERRORS)
def test_prune_logs_a_failed_removal_and_moves_on_to_the_next_resource(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """A proxy it could not remove keeps its config directory."""
    stuck_proxy = _egress_container(LISTED_KEY)
    next_proxy = _egress_container(OTHER_KEY)
    stuck_proxy.remove.side_effect = error
    stuck_network = _egress_network(LISTED_NAME, LISTED_KEY)
    stuck_network.remove.side_effect = error
    next_network = _egress_network(OTHER_NAME, OTHER_KEY)
    _write_config_dirs(tmp_path, LISTED_NAME, OTHER_NAME)
    client = _labelled(
        containers=[stuck_proxy, next_proxy], networks=[stuck_network, next_network]
    )
    caplog.set_level(logging.WARNING, logger="agent_proxy.egress")

    EgressProxies(client, tmp_path).prune(())

    next_proxy.remove.assert_called_once_with(force=True)
    next_network.remove.assert_called_once_with()
    assert (tmp_path / LISTED_NAME / "proxy.yaml").exists()
    assert not (tmp_path / OTHER_NAME).exists()
    proxy_warning, network_warning = _warnings(caplog)
    assert LISTED_NAME in proxy_warning
    assert "daemon said no" in proxy_warning
    assert LISTED_NAME in network_warning
    assert "daemon said no" in network_warning


def test_prune_logs_a_config_directory_it_cannot_remove(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config directory that will not go is logged, and prune goes on."""

    def failing_rmtree(path: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("disk said no")

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)
    first, second = _egress_container(LISTED_KEY), _egress_container(OTHER_KEY)
    client = _labelled(containers=[first, second])
    caplog.set_level(logging.WARNING, logger="agent_proxy.egress")

    EgressProxies(client, tmp_path).prune(())

    second.remove.assert_called_once_with(force=True)
    first_warning, second_warning = _warnings(caplog)
    assert LISTED_NAME in first_warning
    assert "disk said no" in first_warning
    assert OTHER_NAME in second_warning
