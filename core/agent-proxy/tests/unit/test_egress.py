import dataclasses
import hashlib
import re
from collections.abc import Callable, Iterator
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
    EgressStartError,
    cage_network,
    domain_lists,
    list_key,
    network_name,
    render_config,
    start_proxy,
)
from agent_proxy.errors import ProxyError

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
        },
        context={"credential_names": frozenset()},
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
def test_a_failed_removal_raises_egress_error_naming_the_proxy() -> None:
    """A proxy Docker will not remove is reported, not left silently."""
    client = _client(EARLIER_LINE, status="exited")
    _container(client).remove.side_effect = docker.errors.APIError("daemon said no")

    with pytest.raises(EgressError, match="could not be removed") as excinfo:
        start_proxy(client, KEY, CONFIG_DIR)

    assert type(excinfo.value) is EgressError
    assert PROXY_NAME in str(excinfo.value)
