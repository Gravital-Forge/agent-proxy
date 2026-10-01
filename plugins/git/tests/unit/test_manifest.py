from datetime import timedelta
from pathlib import Path

import pytest
from agent_proxy.plugins import (
    CommandAction,
    Manifest,
    Parameter,
    ScriptAction,
    parse_manifest,
)
from agent_proxy.runner import InputError, validate_input

MANIFEST_PATH = Path(__file__).parents[2] / "manifest.json"
REPO = Parameter(name="repo", placement="flag", flag="--repo")
REPO_PROPERTY = {"type": "string", "pattern": "^/"}
REF = Parameter(name="ref", placement="positional", flag=None)
REF_PROPERTY = {"type": "string", "minLength": 1}
# Each read-only action's argv, and its parameters after repo.
READ_ACTIONS: dict[str, tuple[tuple[str, ...], tuple[Parameter, ...]]] = {
    "status": (("git-cage", "status"), ()),
    "log": (
        ("git-cage", "log", "--max-count=20"),
        (
            Parameter(name="max_count", placement="flag", flag="--max-count"),
            Parameter(name="oneline", placement="flag", flag="--oneline"),
            REF,
        ),
    ),
    "show": (("git-cage", "show"), (REF,)),
    "branch": (
        ("git-cage", "branch"),
        (Parameter(name="all", placement="flag", flag="--all"),),
    ),
    "rev_parse": (("git-cage", "rev-parse"), (REF,)),
    "ls_files": (("git-cage", "ls-files"), ()),
    "stash_list": (("git-cage", "stash", "list"), ()),
    "remote": (("git-cage", "remote", "-v"), ()),
    "blame": (
        ("git-cage", "blame", "--"),
        (Parameter(name="file", placement="positional", flag=None),),
    ),
    "tag": (("git-cage", "tag"), ()),
    "config_get": (
        ("git-cage", "config", "--get"),
        (Parameter(name="key", placement="positional", flag=None),),
    ),
    "lfs_ls_files": (("git-cage", "lfs", "ls-files"), ()),
    "lfs_status": (("git-cage", "lfs", "status"), ()),
}
REQUIRED_BEYOND_REPO = {"rev_parse": {"ref"}, "blame": {"file"}, "config_get": {"key"}}


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    """The plugin's manifest.json, parsed as the core parses describe's output."""
    return parse_manifest(MANIFEST_PATH.read_bytes())


def _command_actions(manifest: Manifest) -> list[CommandAction]:
    return [
        action
        for action in manifest.actions.values()
        if isinstance(action, CommandAction)
    ]


def _properties(action: CommandAction | ScriptAction) -> dict[str, dict[str, object]]:
    properties = action.input_schema.get("properties", {})
    assert isinstance(properties, dict)
    return properties


def _required(action: CommandAction | ScriptAction) -> set[str]:
    required = action.input_schema.get("required", [])
    assert isinstance(required, list)
    return set(required)


def test_manifest_parses_with_no_warning(manifest: Manifest) -> None:
    """The core parses manifest.json and warns of nothing."""
    assert manifest.warnings() == ()


@pytest.mark.parametrize("name", sorted(READ_ACTIONS))
def test_read_action_runs_its_git_words(manifest: Manifest, name: str) -> None:
    """Each read-only action runs git-cage with its git words and parameters."""
    argv, parameters = READ_ACTIONS[name]
    action = manifest.actions[name]

    assert isinstance(action, CommandAction)
    assert (action.argv, action.parameters) == (argv, (REPO, *parameters))


@pytest.mark.parametrize("name", sorted(READ_ACTIONS))
def test_read_action_takes_no_credential_and_a_minute(
    manifest: Manifest, name: str
) -> None:
    """A read-only action reaches no remote, so it declares no credential."""
    action = manifest.actions[name]

    assert (action.credentials, action.timeout) == ((), timedelta(seconds=60))


@pytest.mark.parametrize("name", sorted(READ_ACTIONS))
def test_read_action_requires_repo_and_its_own_parameter(
    manifest: Manifest, name: str
) -> None:
    """Only repo, and the ref, file, or key some actions take, are required."""
    required = _required(manifest.actions[name])

    assert required == {"repo"} | REQUIRED_BEYOND_REPO.get(name, set())


def test_every_command_action_runs_git_cage(manifest: Manifest) -> None:
    """Every command action's argv begins with the wrapper."""
    offenders = [
        action.name
        for action in _command_actions(manifest)
        if action.argv[0] != "git-cage"
    ]

    assert offenders == []


def test_every_command_action_passes_repo_first_as_a_flag(manifest: Manifest) -> None:
    """Every command action's first parameter is repo, placed as --repo."""
    offenders = [
        action.name
        for action in _command_actions(manifest)
        if action.parameters[:1] != (REPO,)
    ]

    assert offenders == []


def test_every_action_requires_repo_as_an_absolute_path(manifest: Manifest) -> None:
    """Every action requires repo, a string that begins with '/'."""
    offenders = [
        name
        for name, action in manifest.actions.items()
        if _properties(action).get("repo") != REPO_PROPERTY
        or "repo" not in _required(action)
    ]

    assert offenders == []


def test_every_action_refuses_an_unknown_member(manifest: Manifest) -> None:
    """Every input_schema sets additionalProperties to false."""
    offenders = [
        name
        for name, action in manifest.actions.items()
        if action.input_schema.get("additionalProperties") is not False
    ]

    assert offenders == []


def test_every_other_string_is_non_empty(manifest: Manifest) -> None:
    """Every string input but repo sets minLength to 1."""
    offenders = [
        f"{name}.{property_name}"
        for name, action in manifest.actions.items()
        for property_name, schema in _properties(action).items()
        if property_name != "repo"
        and schema.get("type") == "string"
        and schema.get("minLength") != 1
    ]

    assert offenders == []


def test_log_states_each_parameter(manifest: Manifest) -> None:
    """The log action takes a positive max_count, a boolean oneline, a ref."""
    assert _properties(manifest.actions["log"]) == {
        "repo": REPO_PROPERTY,
        "max_count": {"type": "integer", "minimum": 1},
        "oneline": {"type": "boolean"},
        "ref": REF_PROPERTY,
    }


def test_branch_states_all_as_a_boolean(manifest: Manifest) -> None:
    """The all of branch is a boolean flag."""
    assert _properties(manifest.actions["branch"]) == {
        "repo": REPO_PROPERTY,
        "all": {"type": "boolean"},
    }


@pytest.mark.parametrize("file", ["../x", "a/../b", "a/..", "..", "x\n/../y"])
def test_blame_refuses_a_file_with_a_dot_dot_segment(
    manifest: Manifest, file: str
) -> None:
    """The input schema refuses a '..' segment before any cage starts."""
    with pytest.raises(InputError):
        validate_input(manifest.actions["blame"], {"repo": "/repo", "file": file})


@pytest.mark.parametrize("file", ["a..b", "a.txt", "dir/a.txt", "..a", "a/..b"])
def test_blame_accepts_a_file_with_no_dot_dot_segment(
    manifest: Manifest, file: str
) -> None:
    """Two dots inside a segment name are not a '..' segment."""
    validate_input(manifest.actions["blame"], {"repo": "/repo", "file": file})


def test_status_refuses_a_relative_repo(manifest: Manifest) -> None:
    """The repo must be an absolute path."""
    with pytest.raises(InputError):
        validate_input(manifest.actions["status"], {"repo": "repo"})


def test_rev_parse_refuses_an_empty_ref(manifest: Manifest) -> None:
    """rev_parse's ref must not be empty."""
    with pytest.raises(InputError):
        validate_input(manifest.actions["rev_parse"], {"repo": "/repo", "ref": ""})


def test_log_refuses_a_max_count_below_one(manifest: Manifest) -> None:
    """The max_count of log must be at least 1."""
    with pytest.raises(InputError):
        validate_input(manifest.actions["log"], {"repo": "/repo", "max_count": 0})
