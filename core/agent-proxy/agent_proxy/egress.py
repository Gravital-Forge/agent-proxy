"""Egress for cages: one proxy per distinct domain list, each on its own network."""

import hashlib
import json
import re
import time
import types
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import docker.types
import yaml
from docker import DockerClient
from docker.models.containers import Container

from agent_proxy.config import DOMAIN_PATTERN, PROXY_ENV_NAME, InstanceConfig
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import DOCKER_FAILURES

BRIDGE_NETWORK = "apx-egress-bridge"
IRON_PROXY_IMAGE = (
    "ironsh/iron-proxy:0.52.0"
    "@sha256:004fdc067dc4cc47cebfc0dab3c63ab9a5e21a3230c965e69775a565df99efc6"
)
LABELS: Mapping[str, str] = types.MappingProxyType({})
PROXY_ALIAS = "egress"
PROXY_LOG_EXCERPT_BYTES = 2048
PROXY_READY_TIMEOUT_SECONDS = 10
TUNNEL_PORT = 1080
_CONFIG_MOUNT = "/etc/iron-proxy"
_EGRESS_LABEL = "agent-proxy.egress"
_KEY_DIGITS = 16
_POLL_SECONDS = 0.1
_READY_MESSAGE = "tunnel proxy starting"


class EgressError(ProxyError):
    """An egress proxy or network is refused or fails."""


class EgressConfigError(EgressError):
    """A proxy config cannot be rendered for a domain list."""


class EgressStartError(EgressError):
    """A proxy stops, or is not ready in time, after it starts."""


@dataclass(frozen=True)
class CageNetwork:
    """The network a cage joins, and the environment that network gives it."""

    name: str
    environment: Mapping[str, str]


def list_key(domains: Iterable[str]) -> str:
    """Return the key that names a domain list, the same in any order."""
    joined = "\n".join(sorted(domains))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_KEY_DIGITS]


def network_name(key: str) -> str:
    """Return the name of a list's internal network and of its proxy container."""
    return f"apx-egress-{key}"


def cage_network(domains: Collection[str]) -> CageNetwork:
    """Return the network a cage granted domains joins.

    A non-empty list's network holds its proxy, which the cage reaches through
    the proxy variable; the empty list's network holds no proxy.
    """
    environment = (
        {PROXY_ENV_NAME: f"http://{PROXY_ALIAS}:{TUNNEL_PORT}"} if domains else {}
    )
    return CageNetwork(name=network_name(list_key(domains)), environment=environment)


def domain_lists(config: InstanceConfig) -> set[tuple[str, ...]]:
    """Return each distinct sorted domain list the grants of every scope name."""
    return {
        tuple(sorted(grant.domains))
        for scope in config.scopes.values()
        for grant in scope.grants.values()
    }


def render_config(domains: Collection[str]) -> bytes:
    """Return the iron-proxy config, as UTF-8 YAML, that allows only domains.

    Every listener but the tunnel binds loopback, so a cage reaches only the
    tunnel; the config has no management block, which keeps that listener off.

    Raises:
        EgressConfigError: domains is empty, or a domain does not fully match
            DOMAIN_PATTERN.
    """
    _check_domains(domains)
    document = {
        "dns": {"listen": "127.0.0.1:53", "proxy_ip": "127.0.0.1"},
        "proxy": {
            "http_listen": "127.0.0.1:80",
            "https_listen": "127.0.0.1:443",
            "tunnel_listen": f":{TUNNEL_PORT}",
        },
        "metrics": {"listen": "127.0.0.1:9090"},
        "tls": {"mode": "sni-only"},
        "transforms": [{"name": "allowlist", "config": {"domains": sorted(domains)}}],
    }
    return yaml.safe_dump(document, sort_keys=False, encoding="utf-8")


def _check_domains(domains: Collection[str]) -> None:
    """Refuse a list no proxy config can allow.

    Raises:
        EgressConfigError: domains is empty, or a domain does not fully match
            DOMAIN_PATTERN.
    """
    if not domains:
        raise EgressConfigError(
            "a proxy config must allow at least one domain, got an empty list"
        )
    for domain in domains:
        if re.fullmatch(DOMAIN_PATTERN, domain) is None:
            raise EgressConfigError(
                f"a proxy config domain must match {DOMAIN_PATTERN!r}, got {domain!r}"
            )


def start_proxy(client: DockerClient, key: str, config_dir: Path) -> Container:
    """Start the proxy for a list's key and return it once it serves.

    The proxy joins its list's internal network under PROXY_ALIAS, then
    BRIDGE_NETWORK; both networks must already exist. It reads proxy.yaml from
    config_dir. It is ready when its log holds the tunnel line and it still
    runs: iron-proxy exits 0 when a listener fails to bind, so no exit code
    marks a good start.

    Raises:
        EgressStartError: the proxy stops, or is not ready within
            PROXY_READY_TIMEOUT_SECONDS; it is removed.
        EgressError: Docker refuses a step; a proxy already created is removed.
    """
    name = network_name(key)
    container = _create_proxy(client, key, config_dir)
    try:
        _start_proxy(client, container, name)
    except EgressError:
        _remove_proxy(container, name)
        raise
    return container


def _create_proxy(client: DockerClient, key: str, config_dir: Path) -> Container:
    name = network_name(key)
    try:
        return client.containers.create(
            IRON_PROXY_IMAGE,
            command=["-config", f"{_CONFIG_MOUNT}/proxy.yaml"],
            name=name,
            user="0:0",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            mounts=[
                docker.types.Mount(
                    target=_CONFIG_MOUNT,
                    source=str(config_dir),
                    type="bind",
                    read_only=True,
                )
            ],
            network=name,
            networking_config={
                name: client.api.create_endpoint_config(aliases=[PROXY_ALIAS])
            },
            labels={_EGRESS_LABEL: key} | LABELS,
        )
    except DOCKER_FAILURES as exc:
        raise EgressError(f"proxy {name!r} could not be created: {exc}") from exc


def _start_proxy(client: DockerClient, container: Container, name: str) -> None:
    try:
        client.networks.get(BRIDGE_NETWORK).connect(container)
        container.start()
    except DOCKER_FAILURES as exc:
        raise EgressError(f"proxy {name!r} could not be started: {exc}") from exc
    _wait_until_ready(container, name)


def _wait_until_ready(container: Container, name: str) -> None:
    deadline = time.monotonic() + PROXY_READY_TIMEOUT_SECONDS
    while True:
        log = _read_log_then_state(container, name)
        if container.status != "running":
            raise EgressStartError(
                f"proxy {name!r} stopped before it was ready; "
                f"log tail {_log_tail(log)!r}"
            )
        if _holds_tunnel_line(log):
            return
        if time.monotonic() >= deadline:
            raise EgressStartError(
                f"proxy {name!r} was not ready within "
                f"{PROXY_READY_TIMEOUT_SECONDS} seconds; log tail {_log_tail(log)!r}"
            )
        time.sleep(_POLL_SECONDS)


def _read_log_then_state(container: Container, name: str) -> bytes:
    """Return the proxy's log, refreshing its state after the read.

    A running state read after the log proves the proxy outlived every line
    in it.
    """
    try:
        log = container.logs()
        container.reload()
    except DOCKER_FAILURES as exc:
        raise EgressError(f"proxy {name!r} could not be read: {exc}") from exc
    return log


def _holds_tunnel_line(log: bytes) -> bool:
    for line in log.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("msg") == _READY_MESSAGE:
            return True
    return False


def _log_tail(log: bytes) -> str:
    return log[-PROXY_LOG_EXCERPT_BYTES:].decode("utf-8", errors="replace")


def _remove_proxy(container: Container, name: str) -> None:
    try:
        container.remove(force=True)
    except DOCKER_FAILURES as exc:
        raise EgressError(f"proxy {name!r} could not be removed: {exc}") from exc
