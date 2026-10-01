from pathlib import Path

import pytest

from agent_proxy.config import ConfigError, load_config

EXAMPLE_CONFIG = Path(__file__).parents[4] / "examples" / "instance.yaml"
CREDENTIAL_NAMES = frozenset({"github-token"})


def test_example_config_loads_with_its_paths() -> None:
    """The example config loads, and holds the placeholder agent's tree and overflow."""
    config = load_config(EXAMPLE_CONFIG, CREDENTIAL_NAMES)

    assert config.agent_tree == "/home/agent-01/Projects"
    assert config.overflow_dir == "/home/agent-01/Projects/.agent-proxy-overflow"


def test_example_github_grants_name_only_the_api_host() -> None:
    """Every github grant in the example names api.github.com and no other host."""
    config = load_config(EXAMPLE_CONFIG, CREDENTIAL_NAMES)

    github_domains = {
        key: grant.domains
        for scope in config.scopes.values()
        for key, grant in scope.grants.items()
        if key.startswith("github.")
    }
    assert github_domains == {
        "github.pr_view": ["api.github.com"],
        "github.pr_create": ["api.github.com"],
        "github.pr_checks": ["api.github.com"],
    }


def test_example_config_needs_the_github_token_credential() -> None:
    """With an empty credential store, the example is refused for github-token."""
    with pytest.raises(
        ConfigError, match="'github-token' is not in the credential store"
    ):
        load_config(EXAMPLE_CONFIG, frozenset())
