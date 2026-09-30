from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent_proxy.config import (
    ConfigError,
    EnvMount,
    FileMount,
    InstanceConfig,
    Root,
    load_config,
)

AGENT_TREE = "/srv/agents/alpha"
IMAGE = "ghcr.io/example/git@sha256:" + "a" * 64
CREDENTIAL_NAMES = frozenset({"gh-token", "ssh-key"})
LONG_DOMAIN = ".".join(["a" * 63] * 4)
OMIT = object()


def _grant(**overrides: object) -> dict[str, object]:
    """Return a valid grant with overrides applied; an OMIT value drops the field."""
    fields: dict[str, object] = {
        "roots": [{"path": f"{AGENT_TREE}/repo", "mode": "rw"}],
        "domains": ["github.com"],
        "credentials": {
            "gh-token": {"env": "GH_TOKEN"},
            "ssh-key": {"file": "/run/credentials/ssh-key"},
        },
        "timeout": "120s",
    } | overrides
    return {key: value for key, value in fields.items() if value is not OMIT}


def _scopes(grants: dict[str, object]) -> dict[str, object]:
    return {"dev": {"grants": grants}}


def _config(**overrides: object) -> dict[str, object]:
    """Return a valid config with top-level overrides applied."""
    return {
        "agent_tree": AGENT_TREE,
        "plugins": {"git": IMAGE},
        "scopes": _scopes({"git.clone": _grant()}),
    } | overrides


def _with_grant(**overrides: object) -> dict[str, object]:
    """Return a valid config whose one grant has overrides applied."""
    return _config(scopes=_scopes({"git.clone": _grant(**overrides)}))


def _load(tmp_path: Path, document: object) -> InstanceConfig:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document))
    return load_config(path, CREDENTIAL_NAMES)


def _refusal(tmp_path: Path, document: object) -> ConfigError:
    """Load document, expecting a ConfigError, and return it."""
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, document)
    return excinfo.value


def test_load_config_returns_the_validated_config(tmp_path: Path) -> None:
    """A valid config loads into the model, with the timeout parsed."""
    config = _load(tmp_path, _config())

    grant = config.scopes["dev"].grants["git.clone"]
    assert config.agent_tree == AGENT_TREE
    assert config.plugins == {"git": IMAGE}
    assert grant.roots == [Root(path=f"{AGENT_TREE}/repo", mode="rw")]
    assert grant.domains == ["github.com"]
    assert grant.credentials == {
        "gh-token": EnvMount(env="GH_TOKEN"),
        "ssh-key": FileMount(file="/run/credentials/ssh-key"),
    }
    assert grant.timeout == timedelta(seconds=120)


def test_load_config_returns_a_frozen_config(tmp_path: Path) -> None:
    """The loaded config refuses assignment."""
    config = _load(tmp_path, _config())

    with pytest.raises(ValidationError):
        config.agent_tree = "/srv/agents/beta"


def test_load_config_accepts_empty_grant_fields(tmp_path: Path) -> None:
    """A grant may state roots, domains, and credentials as empty."""
    config = _load(tmp_path, _with_grant(roots=[], domains=[], credentials={}))

    grant = config.scopes["dev"].grants["git.clone"]
    assert grant.roots == []
    assert grant.domains == []
    assert grant.credentials == {}


def test_load_config_leaves_an_omitted_timeout_unset(tmp_path: Path) -> None:
    """A grant without a timeout loads with timeout None."""
    config = _load(tmp_path, _with_grant(timeout=OMIT))

    assert config.scopes["dev"].grants["git.clone"].timeout is None


def test_load_config_accepts_the_agent_tree_itself_as_a_root(tmp_path: Path) -> None:
    """A root equal to agent_tree lies inside it."""
    config = _load(tmp_path, _with_grant(roots=[{"path": AGENT_TREE, "mode": "ro"}]))

    assert config.scopes["dev"].grants["git.clone"].roots == [
        Root(path=AGENT_TREE, mode="ro")
    ]


REFUSALS = [
    pytest.param(["git"], "mapping", id="not-a-mapping"),
    pytest.param(_with_grant(roots=OMIT), "git.clone.roots", id="omitted-roots"),
    pytest.param(_with_grant(domains=OMIT), "git.clone.domains", id="omitted-domains"),
    pytest.param(
        _with_grant(credentials=OMIT), "git.clone.credentials", id="omitted-credentials"
    ),
    pytest.param(_config(owner="ops"), "owner", id="unknown-top-level-key"),
    pytest.param(_with_grant(retries=3), "retries", id="unknown-grant-key"),
    pytest.param(
        _config(agent_tree="srv/agents/alpha"),
        "'srv/agents/alpha'",
        id="relative-agent-tree",
    ),
    pytest.param(
        _config(agent_tree="/srv/agents/../alpha"),
        "'/srv/agents/../alpha'",
        id="agent-tree-dotdot",
    ),
    pytest.param(
        _with_grant(roots=[{"path": "/srv/other", "mode": "ro"}]),
        "'/srv/other'",
        id="root-outside-agent-tree",
    ),
    pytest.param(
        _with_grant(roots=[{"path": f"{AGENT_TREE}/../beta", "mode": "ro"}]),
        f"'{AGENT_TREE}/../beta'",
        id="root-dotdot",
    ),
    pytest.param(
        _with_grant(roots=[{"path": "repo", "mode": "ro"}]),
        "'repo'",
        id="relative-root",
    ),
    pytest.param(
        _with_grant(roots=[{"path": f"{AGENT_TREE}/repo", "mode": "rwx"}]),
        "'rwx'",
        id="mode-rwx",
    ),
    pytest.param(
        _config(plugins={"git": "ghcr.io/x/git:latest"}),
        "'ghcr.io/x/git:latest'",
        id="unpinned-plugin",
    ),
    pytest.param(
        _config(plugins={"git": IMAGE, "git_hub": IMAGE}),
        "git_hub",
        id="plugin-name-underscore",
    ),
    pytest.param(
        _config(scopes=_scopes({"svn.checkout": _grant()})),
        "svn.checkout",
        id="grant-plugin-not-in-plugins",
    ),
    pytest.param(
        _config(scopes=_scopes({"gitclone": _grant()})),
        "gitclone",
        id="grant-key-without-dot",
    ),
    pytest.param(
        _config(scopes=_scopes({"git.Clone": _grant()})),
        "git.Clone",
        id="grant-action-uppercase",
    ),
    pytest.param(
        _config(scopes={"Dev": {"grants": {"git.clone": _grant()}}}),
        "Dev",
        id="scope-name-uppercase",
    ),
    pytest.param(
        _with_grant(credentials={"GH": {"env": "GH_TOKEN"}}),
        "'GH'",
        id="credential-name-uppercase",
    ),
    pytest.param(
        _with_grant(credentials={"npm-token": {"env": "NPM_TOKEN"}}),
        "'npm-token'",
        id="credential-not-in-store",
    ),
    pytest.param(
        _with_grant(
            credentials={
                "gh-token": {"env": "GH_TOKEN", "file": "/run/credentials/gh-token"}
            }
        ),
        "gh-token",
        id="credential-env-and-file",
    ),
    pytest.param(
        _with_grant(credentials={"gh-token": {}}),
        "gh-token",
        id="credential-neither-env-nor-file",
    ),
    pytest.param(
        _with_grant(credentials={"gh-token": {"env": "gh_token"}}),
        "'gh_token'",
        id="env-name-lowercase",
    ),
    pytest.param(
        _with_grant(credentials={"ssh-key": {"file": "/etc/ssh-key"}}),
        "'/etc/ssh-key'",
        id="file-outside-run-credentials",
    ),
    pytest.param(
        _with_grant(credentials={"ssh-key": {"file": "run/credentials/ssh-key"}}),
        "'run/credentials/ssh-key'",
        id="file-relative",
    ),
    pytest.param(
        _with_grant(credentials={"ssh-key": {"file": "/run/credentials/../ssh-key"}}),
        "'/run/credentials/../ssh-key'",
        id="file-dotdot",
    ),
    pytest.param(
        _with_grant(
            credentials={"gh-token": {"env": "TOKEN"}, "ssh-key": {"env": "TOKEN"}}
        ),
        "'gh-token' and 'ssh-key'",
        id="credentials-share-env",
    ),
    pytest.param(
        _with_grant(
            credentials={
                "gh-token": {"file": "/run/credentials/key"},
                "ssh-key": {"file": "/run/credentials/key"},
            }
        ),
        "'gh-token' and 'ssh-key'",
        id="credentials-share-file",
    ),
    pytest.param(
        _with_grant(
            credentials={
                "gh-token": {"file": "/run/credentials/key"},
                "ssh-key": {"file": "/run/credentials//key"},
            }
        ),
        "'gh-token' and 'ssh-key'",
        id="credentials-share-file-after-normalizing",
    ),
    pytest.param(
        _with_grant(domains=["https://github.com"]),
        "'https://github.com'",
        id="domain-with-scheme",
    ),
    pytest.param(
        _with_grant(domains=[LONG_DOMAIN]), repr(LONG_DOMAIN), id="domain-over-253"
    ),
    pytest.param(_with_grant(timeout="0s"), "'0s'", id="timeout-0s"),
    pytest.param(_with_grant(timeout=120), "timeout", id="timeout-integer"),
]


@pytest.mark.parametrize(("document", "expected"), REFUSALS)
def test_load_config_refuses_an_invalid_config(
    tmp_path: Path, document: object, expected: str
) -> None:
    """A broken rule or constraint is refused, naming the offender and the file."""
    error = _refusal(tmp_path, document)

    assert any(expected in message for message in error.errors)
    assert all(
        repr(str(tmp_path / "config.yaml")) in message for message in error.errors
    )


def test_load_config_leaves_the_parent_mapping_out_of_a_missing_field(
    tmp_path: Path,
) -> None:
    """A missing field's message names the field and does not print its parent."""
    error = _refusal(tmp_path, _with_grant(roots=OMIT))

    assert len(error.errors) == 1
    assert "git.clone.roots" in error.errors[0]
    assert "GH_TOKEN" not in error.errors[0]


def test_load_config_reports_every_field_failure_together(tmp_path: Path) -> None:
    """A bad mode and a bad domain in one config are both reported."""
    error = _refusal(
        tmp_path,
        _with_grant(
            roots=[{"path": f"{AGENT_TREE}/repo", "mode": "rwx"}],
            domains=["https://github.com"],
        ),
    )

    assert len(error.errors) == 2
    assert any("'rwx'" in message for message in error.errors)
    assert any("'https://github.com'" in message for message in error.errors)


def test_load_config_reports_every_rule_failure_together(tmp_path: Path) -> None:
    """A root outside agent_tree and a missing credential are both reported."""
    error = _refusal(
        tmp_path,
        _with_grant(
            roots=[{"path": "/srv/other", "mode": "ro"}],
            credentials={"npm-token": {"env": "NPM_TOKEN"}},
        ),
    )

    assert len(error.errors) == 2
    assert any("'/srv/other'" in message for message in error.errors)
    assert any("'npm-token'" in message for message in error.errors)
