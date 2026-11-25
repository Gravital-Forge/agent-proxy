from datetime import timedelta
from pathlib import Path

from agent_proxy.config import EnvMount, FileMount, load_config
from agent_proxy.credentials import unset_message, unset_warnings

EXAMPLE_CONFIG = Path(__file__).parents[4] / "examples" / "instance.yaml"


def test_example_config_loads_with_its_paths() -> None:
    """The example config loads, and holds the placeholder agent's tree and overflow."""
    config = load_config(EXAMPLE_CONFIG)

    assert config.agent_tree == "/home/agent-01/Projects"
    assert config.overflow_dir == "/home/agent-01/Projects/.agent-proxy-overflow"


def test_example_github_grants_name_the_hosts_their_actions_reach() -> None:
    """The example's github grants name the hosts their actions reach.

    pr_view and pr_checks name api.github.com alone. pr_create and pr_edit,
    which allow attach, add uploads.github.com.
    """
    config = load_config(EXAMPLE_CONFIG)

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


def test_example_hcp_terraform_grants_name_its_two_hosts() -> None:
    """Each example hcp-terraform grant names the API host and the log host."""
    config = load_config(EXAMPLE_CONFIG)

    hcp_terraform_domains = {
        key: grant.domains
        for scope in config.scopes.values()
        for key, grant in scope.grants.items()
        if key.startswith("hcp-terraform.")
    }
    hosts = ["app.terraform.io", "archivist.terraform.io"]
    assert hcp_terraform_domains == {
        "hcp-terraform.workspace_list": hosts,
        "hcp-terraform.run_list": hosts,
        "hcp-terraform.run_view": hosts,
        "hcp-terraform.plan_log": hosts,
        "hcp-terraform.apply_log": hosts,
    }


def test_example_commit_grant_sets_the_agents_identity() -> None:
    """The example's git.commit grant gives its cages the placeholder's identity."""
    config = load_config(EXAMPLE_CONFIG)

    assert config.scopes["dev-agent"].grants["git.commit"].env == {
        "GIT_AUTHOR_NAME": "Agent One",
        "GIT_AUTHOR_EMAIL": "agent-01@example.com",
    }


def test_example_provided_credential_is_a_github_app_token() -> None:
    """The example's provided credential runs the github-app provider on the App key.

    Its IDs are placeholder strings, and it refreshes ten minutes before
    expiry with no max_age, because GitHub gives each token's expiry.
    """
    config = load_config(EXAMPLE_CONFIG)

    entry = config.provided_credentials["github-app-token"]
    assert list(config.provided_credentials) == ["github-app-token"]
    assert entry.provider == "github-app.installation_token"
    assert entry.credentials == {
        "github-app-key": FileMount(file="/run/credentials/github-app-key")
    }
    assert entry.env == {
        "GITHUB_APP_ID": "000000",
        "GITHUB_INSTALLATION_ID": "00000000",
    }
    assert entry.domains == ["api.github.com"]
    assert entry.refresh_before == timedelta(minutes=10)
    assert entry.max_age is None


def test_example_clone_grant_takes_the_provided_token_beside_stored_ones() -> None:
    """git.clone alone takes the provided token as GH_TOKEN; push keeps github-token."""
    config = load_config(EXAMPLE_CONFIG)

    grants = config.scopes["dev-agent"].grants
    takers = {
        key for key, grant in grants.items() if "github-app-token" in grant.credentials
    }
    assert takers == {"git.clone"}
    assert grants["git.clone"].credentials == {
        "github-app-token": EnvMount(env="GH_TOKEN")
    }
    assert grants["git.clone"].domains == ["github.com"]
    assert grants["git.push"].credentials == {"github-token": EnvMount(env="GH_TOKEN")}


def test_example_config_warns_of_each_credential_an_empty_store_lacks() -> None:
    """An empty store lacks github-token, hcp-terraform-token, then the App key."""
    config = load_config(EXAMPLE_CONFIG)

    assert unset_warnings(
        config.scopes.values(), config.provided_credentials, frozenset()
    ) == (
        unset_message("github-token"),
        unset_message("hcp-terraform-token"),
        unset_message("github-app-key"),
    )
