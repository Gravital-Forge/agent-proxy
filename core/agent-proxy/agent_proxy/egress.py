"""Egress for cages: one proxy per distinct domain list, each on its own network."""

import hashlib
import json
import logging
import re
import shutil
import time
import types
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import docker.errors
import docker.types
import yaml
from docker import DockerClient
from docker.models.containers import Container
from docker.models.networks import Network

from agent_proxy.config import DOMAIN_PATTERN, PROXY_ENV_NAME, InstanceConfig
from agent_proxy.credentials import write_owner_only
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import DOCKER_FAILURES, ensure_image

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
_BRIDGE_VALUE = "bridge"
_CONFIG_FILE = "proxy.yaml"
_CONFIG_MOUNT = "/etc/iron-proxy"
_EGRESS_LABEL = "agent-proxy.egress"
_KEY_DIGITS = 16
_POLL_SECONDS = 0.1
_READY_MESSAGE = "tunnel proxy starting"

_logger = logging.getLogger(__name__)


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
            A refused removal names the failure that called for it.
    """
    name = network_name(key)
    container = _create_proxy(client, key, config_dir)
    try:
        _start_proxy(client, container, name)
    except EgressError as failure:
        _remove_failed_proxy(container, name, failure)
        raise
    return container


def _create_proxy(client: DockerClient, key: str, config_dir: Path) -> Container:
    name = network_name(key)
    try:
        return client.containers.create(
            IRON_PROXY_IMAGE,
            command=["-config", f"{_CONFIG_MOUNT}/{_CONFIG_FILE}"],
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


def _remove_failed_proxy(container: Container, name: str, failure: EgressError) -> None:
    try:
        _remove_proxy(container, name)
    except EgressError as removal_failure:
        raise EgressError(
            f"{removal_failure}; it was being removed because {failure}"
        ) from failure


class EgressProxies:
    """The egress networks and proxies on one daemon, with each proxy's config."""

    def __init__(self, client: DockerClient, config_root: Path) -> None:
        """Keep each proxy's config in its own directory under config_root."""
        self._client = client
        self._config_root = config_root

    def ensure(self, lists: Iterable[Collection[str]]) -> None:
        """Make the proxy image, the bridge, and each list's network and proxy present.

        The empty list gets its network and no proxy. A proxy is kept when it
        runs, on IRON_PROXY_IMAGE, from a config file holding exactly what
        render_config renders; any other proxy of that name is replaced.

        Raises:
            PluginImageError: the proxy image cannot be looked up or pulled.
            EgressStartError: a proxy stops, or is not ready in time; it is
                removed with its config directory.
            EgressError: Docker refuses a step, or a config cannot be written.
        """
        ensure_image(self._client, IRON_PROXY_IMAGE)
        self._ensure_network(BRIDGE_NETWORK, _BRIDGE_VALUE, internal=False)
        for domains in lists:
            key = list_key(domains)
            self._ensure_network(network_name(key), key, internal=True)
            if domains:
                self._ensure_proxy(key, render_config(domains))

    def prune(self, lists: Iterable[Collection[str]]) -> None:
        """Remove every egress proxy and network that no list in lists names.

        Proxies go first, each with its config directory, so their networks
        hold no endpoint; the bridge always stays. It never raises: a failure
        logs a warning, and the next prune tries again.
        """
        kept = {list_key(domains) for domains in lists}
        for container in self._egress_containers():
            key = container.labels[_EGRESS_LABEL]
            if key not in kept:
                self._prune_proxy(container, key)
        for network in self._egress_networks():
            if network.attrs["Labels"][_EGRESS_LABEL] not in kept | {_BRIDGE_VALUE}:
                self._prune_network(network)

    def _ensure_network(self, name: str, value: str, *, internal: bool) -> None:
        if not self._network_exists(name):
            self._create_network(name, value, internal=internal)

    def _network_exists(self, name: str) -> bool:
        try:
            self._client.networks.get(name)
        except docker.errors.NotFound:
            return False
        except DOCKER_FAILURES as exc:
            raise EgressError(
                f"network {name!r} could not be looked up: {exc}"
            ) from exc
        return True

    def _create_network(self, name: str, value: str, *, internal: bool) -> None:
        try:
            self._client.networks.create(
                name,
                driver="bridge",
                internal=internal,
                labels={_EGRESS_LABEL: value} | LABELS,
            )
        except DOCKER_FAILURES as exc:
            raise EgressError(f"network {name!r} could not be created: {exc}") from exc

    def _ensure_proxy(self, key: str, config: bytes) -> None:
        name = network_name(key)
        proxy = self._find_proxy(name)
        if proxy is not None:
            if self._is_current(proxy, key, config):
                return
            _remove_proxy(proxy, name)
        directory = self._write_config(key, config)
        try:
            start_proxy(self._client, key, directory)
        except EgressError:
            self._remove_config_dir(key)
            raise

    def _find_proxy(self, name: str) -> Container | None:
        try:
            return self._client.containers.get(name)
        except docker.errors.NotFound:
            return None
        except DOCKER_FAILURES as exc:
            raise EgressError(f"proxy {name!r} could not be looked up: {exc}") from exc

    def _is_current(self, proxy: Container, key: str, config: bytes) -> bool:
        return (
            proxy.status == "running"
            and proxy.attrs["Config"]["Image"] == IRON_PROXY_IMAGE
            and self._config_holds(key, config)
        )

    def _config_holds(self, key: str, config: bytes) -> bool:
        """Return whether key's config file holds exactly config.

        A file that cannot be read does not; rewriting it reports any failure.
        """
        try:
            return (self._config_dir(key) / _CONFIG_FILE).read_bytes() == config
        except OSError:
            return False

    def _write_config(self, key: str, config: bytes) -> Path:
        directory = self._config_dir(key)
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
            write_owner_only(directory / _CONFIG_FILE, config)
        except OSError as exc:
            raise EgressError(
                f"config for proxy {network_name(key)!r} could not be written: {exc}"
            ) from exc
        return directory

    def _config_dir(self, key: str) -> Path:
        return self._config_root / network_name(key)

    def _remove_config_dir(self, key: str) -> None:
        directory = self._config_dir(key)
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _logger.warning(
                "egress config directory %r could not be removed: %s",
                str(directory),
                exc,
            )

    def _egress_containers(self) -> list[Container]:
        try:
            return self._client.containers.list(all=True, filters=_egress_filters())
        except DOCKER_FAILURES as exc:
            _logger.warning("egress proxies could not be listed: %s", exc)
            return []

    def _egress_networks(self) -> list[Network]:
        try:
            return self._client.networks.list(filters=_egress_filters())
        except DOCKER_FAILURES as exc:
            _logger.warning("egress networks could not be listed: %s", exc)
            return []

    def _prune_proxy(self, container: Container, key: str) -> None:
        try:
            container.remove(force=True)
        except DOCKER_FAILURES as exc:
            _logger.warning(
                "egress proxy %r could not be removed: %s", container.name, exc
            )
            return
        self._remove_config_dir(key)

    def _prune_network(self, network: Network) -> None:
        try:
            network.remove()
        except DOCKER_FAILURES as exc:
            _logger.warning(
                "egress network %r could not be removed: %s", network.name, exc
            )


def _egress_filters() -> dict[str, list[str]]:
    """Return the label filter that finds egress resources.

    Docker matches a container or network only when it carries every label
    in the list.
    """
    return {
        "label": [
            _EGRESS_LABEL,
            *(f"{name}={value}" for name, value in LABELS.items()),
        ]
    }
