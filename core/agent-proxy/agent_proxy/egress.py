"""Egress for cages: one proxy per distinct domain list, each on its own network."""

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

import yaml

from agent_proxy.config import DOMAIN_PATTERN, PROXY_ENV_NAME, InstanceConfig
from agent_proxy.errors import ProxyError

BRIDGE_NETWORK = "apx-egress-bridge"
PROXY_ALIAS = "egress"
TUNNEL_PORT = 1080
_KEY_DIGITS = 16


class EgressError(ProxyError):
    """An egress proxy or network is refused or fails."""


class EgressConfigError(EgressError):
    """A proxy config cannot be rendered for a domain list."""


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
