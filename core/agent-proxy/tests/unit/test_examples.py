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


def test_example_github_grants_name_the_hosts_their_actions_reach() -> None:
    """The example's github grants name the hosts their actions reach.

    pr_view and pr_checks name api.github.com alone. pr_create and pr_edit,
    which allow attach, add uploads.github.com.
    """
    config = load_config(EXAMPLE_CONFIG, CREDENTIAL_NAMES)

    github_domains = {
        key: grant.domains
        for scope in config.scopes.values()
        for key, grant in scope.grants.items()
        if key.startswith("github.")
    }
    assert github_domains == {
        "github.pr_view": ["api.github.com"],
        "github.pr_create": ["api.github.com", "uploads.github.com"],
        "github.pr_edit": ["api.github.com", "uploads.github.com"],
        "github.pr_checks": ["api.github.com"],
    }


def test_example_commit_grant_sets_the_agents_identity() -> None:
    """The example's git.commit grant gives its cages the placeholder's identity."""
    config = load_config(EXAMPLE_CONFIG, CREDENTIAL_NAMES)

    assert config.scopes["dev-agent"].grants["git.commit"].env == {
        "GIT_AUTHOR_NAME": "Agent One",
        "GIT_AUTHOR_EMAIL": "agent-01@example.com",
    }


def test_example_config_needs_the_github_token_credential() -> None:
    """With an empty credential store, the example is refused for github-token."""
    with pytest.raises(
        ConfigError, match="'github-token' is not in the credential store"
    ):
        load_config(EXAMPLE_CONFIG, frozenset())
