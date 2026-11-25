from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent_proxy.config import (
    DOMAIN_PATTERN,
    GRANT_KEY_PATTERN,
    PROXY_ENV_NAME,
    ROOTS_ENV_NAME,
    ConfigError,
    EnvMount,
    FileMount,
    InstanceConfig,
    ProvidedCredential,
    Root,
    load_config,
)

AGENT_TREE = "/srv/agents/alpha"
OVERFLOW_DIR = f"{AGENT_TREE}/overflow"
MAX_CAGES = 4
IMAGE = "ghcr.io/example/git@sha256:" + "a" * 64
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
    return load_config(path)


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


def test_roots_env_name_is_the_cage_s_roots_variable() -> None:
    """The variable a cage reads its grant's root paths from is AGENT_PROXY_ROOTS."""
    assert ROOTS_ENV_NAME == "AGENT_PROXY_ROOTS"


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


def test_load_config_accepts_a_credential_outside_any_store(tmp_path: Path) -> None:
    """A grant may name any credential; the config load consults no store."""
    config = _load(tmp_path, _with_grant(credentials={"npm-token": {"env": "NPM"}}))

    assert config.scopes["dev"].grants["git.clone"].credentials == {
        "npm-token": EnvMount(env="NPM")
    }


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


@pytest.mark.parametrize(
    ("memory", "expected_bytes"),
    [
        pytest.param("64m", 64 * 2**20, id="floor-in-mib"),
        pytest.param("1024m", 2**30, id="mib"),
        pytest.param("8g", 8 * 2**30, id="gib"),
    ],
)
def test_load_config_reads_a_grant_s_memory_in_bytes(
    tmp_path: Path, memory: str, expected_bytes: int
) -> None:
    """A grant's memory counts MiB for 'm' and GiB for 'g', and loads as bytes."""
    config = _load(tmp_path, _with_grant(memory=memory))

    assert config.scopes["dev"].grants["git.clone"].memory == expected_bytes


def test_load_config_leaves_an_omitted_memory_unset(tmp_path: Path) -> None:
    """A grant without a memory loads with memory None."""
    config = _load(tmp_path, _config())

    assert config.scopes["dev"].grants["git.clone"].memory is None


@pytest.mark.parametrize(
    "memory",
    [
        pytest.param("63m", id="under-the-floor"),
        pytest.param("0g", id="zero"),
        pytest.param("08g", id="leading-zero"),
        pytest.param("8G", id="upper-case-unit"),
        pytest.param("8gb", id="two-letter-unit"),
        pytest.param("8k", id="kib-unit"),
        pytest.param("8", id="no-unit"),
        pytest.param("1.5g", id="fraction"),
        pytest.param(" 8g", id="leading-space"),
        pytest.param("1" * 5000 + "g", id="past-the-int-digit-limit"),
        pytest.param(8, id="integer"),
    ],
)
def test_load_config_refuses_a_bad_memory_naming_the_form_and_the_floor(
    tmp_path: Path, memory: object
) -> None:
    """A memory off the form or under 64 MiB fails the load with one message."""
    error = _refusal(tmp_path, _with_grant(memory=memory))

    assert len(error.errors) == 1
    assert "scopes.dev.grants.git.clone.memory: " in error.errors[0]
    assert repr(r"^[1-9][0-9]*[mg]$") in error.errors[0]
    assert "at least 64m" in error.errors[0]


def test_load_config_names_a_refused_memory_once(tmp_path: Path) -> None:
    """The refusal of a memory is the field, the form, the floor, and the value."""
    error = _refusal(tmp_path, _with_grant(memory="32m"))

    assert error.errors == (
        f"config file {str(tmp_path / 'config.yaml')!r}: "
        "scopes.dev.grants.git.clone.memory: memory must match "
        "'^[1-9][0-9]*[mg]$', a count of MiB or GiB such as '8g', "
        "and be at least 64m, got '32m'",
    )


def test_load_config_returns_a_grant_s_env(tmp_path: Path) -> None:
    """A grant's env loads as the file states it."""
    config = _load(tmp_path, _with_grant(env={"GIT_AUTHOR_NAME": "Agent One"}))

    assert config.scopes["dev"].grants["git.clone"].env == {
        "GIT_AUTHOR_NAME": "Agent One"
    }


def test_load_config_leaves_an_omitted_env_empty(tmp_path: Path) -> None:
    """A grant without env loads with an empty env."""
    config = _load(tmp_path, _config())

    assert config.scopes["dev"].grants["git.clone"].env == {}


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
    pytest.param(
        _with_grant(credentials={"gh-token": {"env": "AGENT_PROXY_ROOTS"}}),
        "git.clone.credentials.gh-token.env must not be 'AGENT_PROXY_ROOTS', "
        "which the core sets",
        id="credential-env-agent-proxy-roots",
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


ENV_REFUSALS = [
    pytest.param(
        {"git_author": "Agent One"},
        "scopes.dev.grants.git.clone.env.git_author.[key]: String should match "
        "pattern '^[A-Z_][A-Z0-9_]*$', got 'git_author'",
        id="grant-env-name-lowercase",
    ),
    pytest.param(
        {"1NAME": "Agent One"},
        "scopes.dev.grants.git.clone.env.1NAME.[key]: String should match "
        "pattern '^[A-Z_][A-Z0-9_]*$', got '1NAME'",
        id="grant-env-name-leading-digit",
    ),
    pytest.param(
        {"GIT_AUTHOR_NAME": 123},
        "scopes.dev.grants.git.clone.env.GIT_AUTHOR_NAME: "
        "Input should be a valid string, got 123",
        id="grant-env-value-number",
    ),
    pytest.param(
        {"HTTPS_PROXY": "http://elsewhere:1080"},
        "scopes.dev.grants.git.clone.env must not name 'HTTPS_PROXY', "
        "which the egress proxy sets",
        id="grant-env-https-proxy",
    ),
    pytest.param(
        {"AGENT_PROXY_ROOTS": "[]"},
        "scopes.dev.grants.git.clone.env must not name 'AGENT_PROXY_ROOTS', "
        "which the core sets",
        id="grant-env-agent-proxy-roots",
    ),
    pytest.param(
        {"GH_TOKEN": "not-a-secret"},
        "scopes.dev.grants.git.clone.env must not name 'GH_TOKEN', "
        "which credential 'gh-token' takes",
        id="grant-env-credential-env",
    ),
    pytest.param(
        {"GIT_AUTHOR_NAME": "Agent\x00One"},
        "scopes.dev.grants.git.clone.env.GIT_AUTHOR_NAME must not hold a NUL",
        id="grant-env-value-nul",
    ),
]


@pytest.mark.parametrize(("env", "expected"), ENV_REFUSALS)
def test_load_config_refuses_a_bad_grant_env(
    tmp_path: Path, env: dict[str, object], expected: str
) -> None:
    """A bad name or value in a grant's env is one failure, naming it and the file."""
    error = _refusal(tmp_path, _with_grant(env=env))

    assert error.errors == (
        f"config file {str(tmp_path / 'config.yaml')!r}: {expected}",
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
    """A root outside agent_tree and a repeated domain are both reported."""
    error = _refusal(
        tmp_path,
        _with_grant(
            roots=[{"path": "/srv/other", "mode": "ro"}],
            domains=["github.com", "github.com"],
        ),
    )

    assert len(error.errors) == 2
    assert any("'/srv/other'" in message for message in error.errors)
    assert any("'github.com' is named more than once" in m for m in error.errors)


def test_load_config_reports_an_env_failure_beside_the_other_rule_failures(
    tmp_path: Path,
) -> None:
    """A reserved env name and a repeated domain are both reported."""
    error = _refusal(
        tmp_path,
        _with_grant(
            domains=["github.com", "github.com"],
            env={"HTTPS_PROXY": "http://elsewhere:1080"},
        ),
    )

    assert len(error.errors) == 2
    assert any("env must not name 'HTTPS_PROXY'" in m for m in error.errors)
    assert any("'github.com' is named more than once" in m for m in error.errors)


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


def test_load_config_refuses_the_plugin_name_the_self_check_tool_takes(
    tmp_path: Path,
) -> None:
    """A plugin named agent-proxy is refused, with the file's name, as one rule."""
    document = _config(plugins={"git": IMAGE, "agent-proxy": IMAGE})

    error = _refusal(tmp_path, document)

    assert error.errors == (
        f"config file {str(tmp_path / 'config.yaml')!r}: plugins.agent-proxy: "
        "the plugin name 'agent-proxy' is reserved, "
        "because the tool 'agent-proxy_self_check' takes it",
    )


def test_load_config_accepts_a_plugin_name_that_extends_the_reserved_one(
    tmp_path: Path,
) -> None:
    """Only the exact name agent-proxy is reserved."""
    document = _config(plugins={"git": IMAGE, "agent-proxy-x": IMAGE})

    assert set(_load(tmp_path, document).plugins) == {"git", "agent-proxy-x"}


APP_IMAGE = "ghcr.io/example/github-app@sha256:" + "b" * 64
PROVIDED = "provided_credentials.app-token"
FILE_PATH = "/run/credentials/t"


def _provided(**overrides: object) -> dict[str, object]:
    """Return a valid provided credential with overrides applied; OMIT drops a field."""
    fields: dict[str, object] = {
        "provider": "github-app.installation_token",
        "credentials": {"app-key": {"file": "/run/credentials/github-app-key"}},
        "env": {"GITHUB_APP_ID": "1", "GITHUB_INSTALLATION_ID": "2"},
        "domains": ["api.github.com"],
        "refresh_before": "10m",
    } | overrides
    return {key: value for key, value in fields.items() if value is not OMIT}


def _with_provided(
    grants: dict[str, object] | None = None, **overrides: object
) -> dict[str, object]:
    """Return a valid config with one provided credential, app-token."""
    return _config(
        plugins={"git": IMAGE, "github-app": APP_IMAGE},
        provided_credentials={"app-token": _provided(**overrides)},
        **({} if grants is None else {"scopes": _scopes(grants)}),
    )


def _errors(tmp_path: Path, *messages: str) -> tuple[str, ...]:
    """Return messages as load_config prefixes them with the file."""
    source = f"config file {str(tmp_path / 'config.yaml')!r}"
    return tuple(f"{source}: {message}" for message in messages)


def test_load_config_returns_a_provided_credential(tmp_path: Path) -> None:
    """An entry loads with its fields, refresh_before parsed, and no max_age."""
    config = _load(tmp_path, _with_provided())

    (entry,) = config.provided_credentials.values()
    assert isinstance(entry, ProvidedCredential)
    assert set(config.provided_credentials) == {"app-token"}
    assert entry.provider == "github-app.installation_token"
    assert entry.credentials == {
        "app-key": FileMount(file="/run/credentials/github-app-key")
    }
    assert entry.env == {"GITHUB_APP_ID": "1", "GITHUB_INSTALLATION_ID": "2"}
    assert entry.domains == ["api.github.com"]
    assert entry.refresh_before == timedelta(minutes=10)
    assert entry.max_age is None
    assert entry.timeout is None


def test_load_config_parses_a_provided_credential_s_max_age_and_timeout(
    tmp_path: Path,
) -> None:
    """max_age and timeout load as durations."""
    config = _load(tmp_path, _with_provided(max_age="1h", timeout="30s"))

    entry = config.provided_credentials["app-token"]
    assert entry.max_age == timedelta(hours=1)
    assert entry.timeout == timedelta(seconds=30)


def test_load_config_leaves_provided_credentials_empty_when_omitted(
    tmp_path: Path,
) -> None:
    """A config without provided_credentials loads with none."""
    assert _load(tmp_path, _config()).provided_credentials == {}


def test_load_config_leaves_a_provided_credential_s_omitted_env_empty(
    tmp_path: Path,
) -> None:
    """An entry without env loads with an empty env."""
    config = _load(tmp_path, _with_provided(env=OMIT))

    assert config.provided_credentials["app-token"].env == {}


def test_load_config_accepts_a_grant_passing_a_provided_credential_as_env(
    tmp_path: Path,
) -> None:
    """A grant names a provided credential as it names a stored one."""
    grant = _grant(credentials={"app-token": {"env": "GH_TOKEN"}})

    config = _load(tmp_path, _with_provided({"git.clone": grant}))

    assert config.scopes["dev"].grants["git.clone"].credentials == {
        "app-token": EnvMount(env="GH_TOKEN")
    }


PROVIDED_REFUSALS = [
    pytest.param(
        _with_provided(retries=3),
        f"{PROVIDED}.retries: Extra inputs are not permitted, got 3",
        id="unknown-field",
    ),
    *(
        pytest.param(
            _with_provided(**{field: OMIT}),
            f"{PROVIDED}.{field}: Field required",
            id=f"{field}-required",
        )
        for field in ("provider", "credentials", "domains", "refresh_before")
    ),
    pytest.param(
        _config(provided_credentials={"App": _provided()}),
        "provided_credentials.App.[key]: String should match pattern "
        "'^[a-z0-9][a-z0-9-]{0,63}$', got 'App'",
        id="name-pattern",
    ),
    pytest.param(
        _with_provided(provider="github-app"),
        f"{PROVIDED}.provider: String should match pattern "
        f"'{GRANT_KEY_PATTERN}', got 'github-app'",
        id="provider-not-a-key",
    ),
    pytest.param(
        _config(provided_credentials={"app-token": _provided()}),
        f"{PROVIDED}.provider: plugin 'github-app' is not in plugins",
        id="provider-plugin-not-in-plugins",
    ),
    pytest.param(
        _with_provided(refresh_before=600),
        f"{PROVIDED}.refresh_before: refresh_before must be a duration string "
        "such as '120s', got 600",
        id="refresh-before-integer",
    ),
    pytest.param(
        _with_provided(refresh_before="0m"),
        f"{PROVIDED}.refresh_before: duration must match "
        "'^[1-9][0-9]*[smhd]$', got '0m'",
        id="refresh-before-zero",
    ),
    pytest.param(
        _with_provided(max_age="10m"),
        f"{PROVIDED}.max_age must be longer than refresh_before 0:10:00, got 0:10:00",
        id="max-age-equal-to-refresh-before",
    ),
    pytest.param(
        _with_provided(max_age="5m"),
        f"{PROVIDED}.max_age must be longer than refresh_before 0:10:00, got 0:05:00",
        id="max-age-shorter-than-refresh-before",
    ),
    pytest.param(
        _with_provided(timeout=30),
        f"{PROVIDED}.timeout: timeout must be a duration string such as '120s', got 30",
        id="timeout-integer",
    ),
    pytest.param(
        _with_provided(credentials={"app-key": {"file": "/etc/github-app-key"}}),
        f"{PROVIDED}.credentials.app-key.file must be an absolute path beneath "
        "'/run/credentials/' with no '..' segment, got '/etc/github-app-key'",
        id="file-outside-run-credentials",
    ),
    pytest.param(
        _with_provided(
            credentials={"app-id": {"env": "KEY"}, "app-key": {"env": "KEY"}},
        ),
        f"{PROVIDED}.credentials: 'app-id' and 'app-key' share env 'KEY'",
        id="credentials-share-env",
    ),
    pytest.param(
        _with_provided(credentials={"app-key": {"env": "HTTPS_PROXY"}}),
        f"{PROVIDED}.credentials.app-key.env must not be 'HTTPS_PROXY', "
        "which the egress proxy sets",
        id="credential-env-reserved",
    ),
    pytest.param(
        _with_provided(env={"AGENT_PROXY_ROOTS": "[]"}),
        f"{PROVIDED}.env must not name 'AGENT_PROXY_ROOTS', which the core sets",
        id="env-reserved",
    ),
    pytest.param(
        _with_provided(
            credentials={"app-key": {"env": "APP_KEY"}}, env={"APP_KEY": "x"}
        ),
        f"{PROVIDED}.env must not name 'APP_KEY', which credential 'app-key' takes",
        id="env-names-a-credential-env",
    ),
    pytest.param(
        _with_provided(env={"GITHUB_APP_ID": "1\x002"}),
        f"{PROVIDED}.env.GITHUB_APP_ID must not hold a NUL",
        id="env-value-nul",
    ),
    pytest.param(
        _with_provided(domains=["api.github.com", "api.github.com"]),
        f"{PROVIDED}.domains: 'api.github.com' is named more than once",
        id="domain-named-twice",
    ),
    pytest.param(
        _config(
            plugins={"git": IMAGE, "github-app": APP_IMAGE},
            provided_credentials={
                "app-token": _provided(),
                "other-token": _provided(
                    credentials={"app-token": {"env": "GH_TOKEN"}}
                ),
            },
        ),
        "provided_credentials.other-token.credentials.app-token: a provider takes "
        "only stored credentials, got provided credential 'app-token'",
        id="input-is-a-provided-credential",
    ),
    pytest.param(
        _with_provided(
            {"git.clone": _grant(credentials={"app-token": {"file": FILE_PATH}})}
        ),
        "scopes.dev.grants.git.clone.credentials.app-token: provided credential "
        "'app-token' must reach a cage as env, got file '/run/credentials/t'",
        id="grant-passes-a-provided-credential-as-a-file",
    ),
]


@pytest.mark.parametrize(("document", "expected"), PROVIDED_REFUSALS)
def test_load_config_refuses_a_bad_provided_credential(
    tmp_path: Path, document: object, expected: str
) -> None:
    """Each broken provided-credential rule is one failure, naming its location."""
    assert _refusal(tmp_path, document).errors == _errors(tmp_path, expected)


def test_load_config_reports_every_provided_credential_failure_together(
    tmp_path: Path,
) -> None:
    """An unknown provider plugin, a short max_age, and a repeated domain all fail."""
    document = _config(
        provided_credentials={
            "app-token": _provided(max_age="1m", domains=["a.io", "a.io"])
        }
    )

    assert _refusal(tmp_path, document).errors == _errors(
        tmp_path,
        f"{PROVIDED}.provider: plugin 'github-app' is not in plugins",
        f"{PROVIDED}.domains: 'a.io' is named more than once",
        f"{PROVIDED}.max_age must be longer than refresh_before 0:10:00, got 0:01:00",
    )
