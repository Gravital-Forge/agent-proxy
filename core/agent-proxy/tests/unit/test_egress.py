import dataclasses
import hashlib
import re
from collections.abc import Iterator

import pytest
import yaml

from agent_proxy.config import InstanceConfig
from agent_proxy.egress import (
    BRIDGE_NETWORK,
    CageNetwork,
    EgressConfigError,
    EgressError,
    cage_network,
    domain_lists,
    list_key,
    network_name,
    render_config,
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
    [(EgressError, ProxyError), (EgressConfigError, EgressError)],
)
def test_each_egress_error_derives_from_its_base(
    error_class: type[Exception], base: type[Exception]
) -> None:
    """EgressError subclasses ProxyError, and EgressConfigError subclasses it."""
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
