from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent_proxy.config import (
    DOMAIN_PATTERN,
    GRANT_KEY_PATTERN,
    PROXY_ENV_NAME,
    ConfigError,
    EnvMount,
    FileMount,
    InstanceConfig,
    Root,
    load_config,
)

AGENT_TREE = "/srv/agents/alpha"
OVERFLOW_DIR = f"{AGENT_TREE}/overflow"
MAX_CAGES = 4
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
    """Return a valid config with top-level overrides applied; OMIT drops a field."""
    fields: dict[str, object] = {
        "agent_tree": AGENT_TREE,
        "overflow_dir": OVERFLOW_DIR,
        "max_cages": MAX_CAGES,
        "plugins": {"git": IMAGE},
        "scopes": _scopes({"git.clone": _grant()}),
    } | overrides
    return {key: value for key, value in fields.items() if value is not OMIT}


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


def test_grant_key_pattern_joins_the_plugin_and_action_patterns() -> None:
    """A grant key is a plugin name and an action name joined by one dot."""
    assert GRANT_KEY_PATTERN == r"^[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*$"


def test_domain_pattern_admits_only_literal_host_names() -> None:
    """A domain is two or more DNS labels of lowercase letters, digits, and hyphens."""
    assert DOMAIN_PATTERN == (
        r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
    )


def test_proxy_env_name_is_the_egress_proxy_s_variable() -> None:
    """The variable a cage reaches its egress proxy through is HTTPS_PROXY."""
    assert PROXY_ENV_NAME == "HTTPS_PROXY"


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


def test_load_config_returns_the_overflow_dir_and_the_cage_cap(tmp_path: Path) -> None:
    """overflow_dir and max_cages load as the file states them."""
    config = _load(tmp_path, _config())

    assert config.overflow_dir == OVERFLOW_DIR
    assert config.max_cages == MAX_CAGES


def test_load_config_accepts_a_cage_cap_of_one(tmp_path: Path) -> None:
    """The smallest cap max_cages allows is 1."""
    assert _load(tmp_path, _config(max_cages=1)).max_cages == 1


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


def test_load_config_accepts_a_root_beside_the_overflow_dir(tmp_path: Path) -> None:
    """A root whose path starts with overflow_dir's text, but lies beside it, loads."""
    root = {"path": f"{OVERFLOW_DIR}-archive", "mode": "rw"}
    config = _load(tmp_path, _with_grant(roots=[root]))

    assert config.scopes["dev"].grants["git.clone"].roots == [
        Root(path=f"{OVERFLOW_DIR}-archive", mode="rw")
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
        "gh-token.EnvMount.file",
        id="credential-env-and-file",
    ),
    pytest.param(
        _with_grant(credentials={"gh-token": {}}),
        "gh-token.EnvMount.env",
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
    pytest.param(
        _with_grant(domains=["github.com", "api.github.com", "github.com"]),
        "git.clone.domains: 'github.com' is named more than once",
        id="domain-named-twice",
    ),
    pytest.param(
        _with_grant(credentials={"gh-token": {"env": "HTTPS_PROXY"}}),
        "git.clone.credentials.gh-token.env must not be 'HTTPS_PROXY'",
        id="credential-env-https-proxy",
    ),
    pytest.param(_with_grant(timeout="0s"), "'0s'", id="timeout-0s"),
    pytest.param(
        _with_grant(timeout=120),
        "timeout: timeout must be a duration string",
        id="timeout-integer",
    ),
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


INSTANCE_FIELD_REFUSALS = [
    pytest.param(
        _config(overflow_dir=OMIT),
        "overflow_dir: Field required",
        id="omitted-overflow-dir",
    ),
    pytest.param(
        _config(max_cages=OMIT), "max_cages: Field required", id="omitted-max-cages"
    ),
    pytest.param(
        _config(max_cages=True),
        "max_cages: Input should be a valid integer, got True",
        id="max-cages-boolean",
    ),
    pytest.param(
        _config(max_cages=2.0),
        "max_cages: Input should be a valid integer, got 2.0",
        id="max-cages-float",
    ),
    pytest.param(
        _config(max_cages="2"),
        "max_cages: Input should be a valid integer, got '2'",
        id="max-cages-string",
    ),
    pytest.param(
        _config(max_cages=0),
        "max_cages: Input should be greater than or equal to 1, got 0",
        id="max-cages-zero",
    ),
    pytest.param(
        _config(overflow_dir="overflow"),
        "overflow_dir must be an absolute path with no '..' segment, got 'overflow'",
        id="relative-overflow-dir",
    ),
    pytest.param(
        _config(overflow_dir=f"{AGENT_TREE}/../overflow"),
        "overflow_dir must be an absolute path with no '..' segment,"
        f" got '{AGENT_TREE}/../overflow'",
        id="overflow-dir-dotdot",
    ),
    pytest.param(
        _config(overflow_dir="/srv/other/overflow"),
        f"overflow_dir must lie inside agent_tree {AGENT_TREE!r},"
        " got '/srv/other/overflow'",
        id="overflow-dir-outside-agent-tree",
    ),
    pytest.param(
        _config(
            overflow_dir=AGENT_TREE, scopes=_scopes({"git.clone": _grant(roots=[])})
        ),
        f"overflow_dir must differ from agent_tree {AGENT_TREE!r}, got {AGENT_TREE!r}",
        id="overflow-dir-equals-agent-tree",
    ),
    pytest.param(
        _config(
            overflow_dir=f"{AGENT_TREE}/",
            scopes=_scopes({"git.clone": _grant(roots=[])}),
        ),
        f"overflow_dir must differ from agent_tree {AGENT_TREE!r}, got '{AGENT_TREE}/'",
        id="overflow-dir-equals-agent-tree-after-cleaning",
    ),
]


@pytest.mark.parametrize(("document", "expected"), INSTANCE_FIELD_REFUSALS)
def test_load_config_refuses_a_bad_overflow_dir_or_cage_cap(
    tmp_path: Path, document: object, expected: str
) -> None:
    """Each bad overflow_dir or max_cages is one failure, located at its field."""
    error = _refusal(tmp_path, document)

    assert error.errors == (
        f"config file {str(tmp_path / 'config.yaml')!r}: {expected}",
    )


ROOT_PATH_FIELD = "scopes.dev.grants.git.clone.roots.0.path"

ROOT_OVERFLOW_REFUSALS = [
    pytest.param(
        OVERFLOW_DIR,
        f"{ROOT_PATH_FIELD} must not equal overflow_dir {OVERFLOW_DIR!r},"
        f" got {OVERFLOW_DIR!r}",
        id="root-equals-overflow-dir",
    ),
    pytest.param(
        f"{OVERFLOW_DIR}/",
        f"{ROOT_PATH_FIELD} must not equal overflow_dir {OVERFLOW_DIR!r},"
        f" got '{OVERFLOW_DIR}/'",
        id="root-equals-overflow-dir-after-cleaning",
    ),
    pytest.param(
        AGENT_TREE,
        f"{ROOT_PATH_FIELD} must not contain overflow_dir {OVERFLOW_DIR!r},"
        f" got {AGENT_TREE!r}",
        id="root-contains-overflow-dir",
    ),
    pytest.param(
        f"{OVERFLOW_DIR}/calls",
        f"{ROOT_PATH_FIELD} must not lie inside overflow_dir {OVERFLOW_DIR!r},"
        f" got '{OVERFLOW_DIR}/calls'",
        id="root-inside-overflow-dir",
    ),
]


@pytest.mark.parametrize(("root_path", "expected"), ROOT_OVERFLOW_REFUSALS)
def test_load_config_refuses_a_root_that_overlaps_the_overflow_dir(
    tmp_path: Path, root_path: str, expected: str
) -> None:
    """A root equal to, containing, or inside overflow_dir is one failure there."""
    error = _refusal(tmp_path, _with_grant(roots=[{"path": root_path, "mode": "ro"}]))

    assert error.errors == (
        f"config file {str(tmp_path / 'config.yaml')!r}: {expected}",
    )


def test_load_config_refuses_a_dotdot_root_only_for_its_segment(
    tmp_path: Path,
) -> None:
    """A root with a '..' segment is not compared with overflow_dir."""
    error = _refusal(
        tmp_path,
        _with_grant(roots=[{"path": f"{OVERFLOW_DIR}/../repo", "mode": "ro"}]),
    )

    assert len(error.errors) == 1
    assert "no '..' segment" in error.errors[0]


def test_load_config_leaves_the_parent_mapping_out_of_a_missing_field(
    tmp_path: Path,
) -> None:
    """A missing field's message names the field and does not print its parent."""
    error = _refusal(tmp_path, _with_grant(roots=OMIT))

    assert len(error.errors) == 1
    assert "git.clone.roots" in error.errors[0]
    assert "GH_TOKEN" not in error.errors[0]


def test_load_config_names_a_validator_refused_value_once(tmp_path: Path) -> None:
    """A value a validator refuses, and already names, appears once in the message."""
    error = _refusal(tmp_path, _with_grant(timeout="0s"))

    assert len(error.errors) == 1
    assert error.errors[0].count("'0s'") == 1


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


def test_load_config_names_a_domain_named_three_times_once(tmp_path: Path) -> None:
    """A repeated domain is one failure, however often the grant names it."""
    error = _refusal(tmp_path, _with_grant(domains=["github.com"] * 3))

    assert len(error.errors) == 1
    assert "'github.com' is named more than once" in error.errors[0]


def test_load_config_names_each_repeated_domain(tmp_path: Path) -> None:
    """Two domains each named twice are two failures."""
    error = _refusal(tmp_path, _with_grant(domains=["a.io", "b.io", "a.io", "b.io"]))

    assert len(error.errors) == 2
    assert any("'a.io' is named more than once" in m for m in error.errors)
    assert any("'b.io' is named more than once" in m for m in error.errors)


def test_load_config_accepts_one_domain_in_two_grants(tmp_path: Path) -> None:
    """The repeated-domain rule holds within a grant, not across grants."""
    document = _config(scopes=_scopes({"git.clone": _grant(), "git.fetch": _grant()}))

    assert set(_load(tmp_path, document).scopes["dev"].grants) == {
        "git.clone",
        "git.fetch",
    }


def test_load_config_refuses_https_proxy_as_a_credential_env_in_any_grant(
    tmp_path: Path,
) -> None:
    """The reserved name is refused in a grant of a later scope too."""
    document = _config(
        scopes={
            "dev": {"grants": {"git.clone": _grant()}},
            "ops": {
                "grants": {
                    "git.push": _grant(credentials={"gh-token": {"env": "HTTPS_PROXY"}})
                }
            },
        }
    )

    error = _refusal(tmp_path, document)

    assert len(error.errors) == 1
    assert "scopes.ops.grants.git.push.credentials.gh-token.env" in error.errors[0]
