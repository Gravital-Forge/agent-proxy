import re
from datetime import timedelta
from pathlib import Path

import pytest
from agent_proxy.config import EnvMount
from agent_proxy.plugins import (
    CommandAction,
    Manifest,
    Parameter,
    ScriptAction,
    parse_manifest,
)
from agent_proxy.runner import InputError, form_argv, validate_input
from git_plugin import scripts
from jsonschema import Draft202012Validator

MANIFEST_PATH = Path(__file__).parents[2] / "manifest.json"
DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile"
LINKED_ACTIONS = re.compile(r"for action in ([^;]+); do")
GIT_SIDECAR_TOOLS = {
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "rev_parse",
    "ls_files",
    "stash_list",
    "remote",
    "blame",
    "tag",
    "config_get",
    "add",
    "rm",
    "commit",
    "restore",
    "stash",
    "fetch",
    "pull",
    "merge",
    "worktree",
    "checkout",
    "push",
    "lfs_track",
    "lfs_untrack",
    "lfs_ls_files",
    "lfs_status",
    "lfs_fetch",
    "lfs_pull",
}
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
    "lfs_ls_files": (("git-cage", "lfs", "ls-files"), ()),
    "lfs_status": (("git-cage", "lfs", "status"), ()),
}
REQUIRED_BEYOND_REPO = {
    "rev_parse": {"ref"},
    "blame": {"file"},
    "merge": {"branch"},
    "stash": {"action"},
}
ACTION = Parameter(name="action", placement="positional", flag=None)
# Each action that changes a repository or reaches a remote: its argv, and its
# parameters after repo.
CHANGE_ACTIONS: dict[str, tuple[tuple[str, ...], tuple[Parameter, ...]]] = {
    "fetch": (("git-cage", "fetch", "origin"), ()),
    "pull": (("git-cage", "pull"), ()),
    "merge": (
        ("git-cage", "merge"),
        (Parameter(name="branch", placement="positional", flag=None),),
    ),
    "stash": (
        ("git-cage", "stash"),
        (
            ACTION,
            Parameter(name="message", placement="flag", flag="-m"),
            Parameter(name="index", placement="positional", flag=None),
        ),
    ),
    "lfs_fetch": (("git-cage", "lfs", "fetch", "origin"), ()),
    "lfs_pull": (("git-cage", "lfs", "pull", "origin"), ()),
}
COMMAND_ACTIONS = READ_ACTIONS | CHANGE_ACTIONS
GH_TOKEN = EnvMount(env="GH_TOKEN")
MINUTE = timedelta(seconds=60)
CHANGE_LIMITS: dict[str, tuple[tuple[EnvMount, ...], timedelta]] = {
    "fetch": ((GH_TOKEN,), MINUTE),
    "pull": ((GH_TOKEN,), MINUTE),
    "merge": ((), MINUTE),
    "stash": ((), MINUTE),
    "lfs_fetch": ((GH_TOKEN,), timedelta(minutes=10)),
    "lfs_pull": ((GH_TOKEN,), timedelta(minutes=10)),
}
SCRIPT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "exit_code": {"type": "integer"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
    },
    "required": ["exit_code", "stdout", "stderr"],
    "additionalProperties": False,
}
DIFF_FILE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "exit_code": {"type": "integer"},
        "stderr": {"type": "string"},
        "path": {"type": "string"},
        "stat": {"type": "string"},
    },
    "required": ["exit_code", "stderr", "path", "stat"],
    "additionalProperties": False,
}
DIFF_OUTPUT_SCHEMA = {
    "type": "object",
    "oneOf": [SCRIPT_OUTPUT_SCHEMA, DIFF_FILE_OUTPUT_SCHEMA],
}
PATH_LIST_PROPERTY = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "string",
        "minLength": 1,
        "pattern": r"^(?!([\s\S]*/)?\.\.(/|$))",
    },
}
OUTPUT_PROPERTY = {
    "type": "string",
    "minLength": 1,
    "pattern": r"^(?!/)(?!([\s\S]*/)?\.\.(/|$))",
}
STAGED_PROPERTY = {"type": "boolean"}
# Each script action's input properties, and the properties it requires.
SCRIPT_ACTIONS: dict[str, tuple[dict[str, dict[str, object]], set[str]]] = {
    "add": (
        {"repo": REPO_PROPERTY, "files": PATH_LIST_PROPERTY},
        {"repo", "files"},
    ),
    "rm": (
        {"repo": REPO_PROPERTY, "files": PATH_LIST_PROPERTY},
        {"repo", "files"},
    ),
    "restore": (
        {
            "repo": REPO_PROPERTY,
            "files": PATH_LIST_PROPERTY,
            "staged": STAGED_PROPERTY,
        },
        {"repo", "files"},
    ),
    "commit": (
        {"repo": REPO_PROPERTY, "message": {"type": "string", "minLength": 1}},
        {"repo", "message"},
    ),
    "diff": (
        {
            "repo": REPO_PROPERTY,
            "staged": STAGED_PROPERTY,
            "ref": {"type": "string", "minLength": 1, "pattern": "^[^-]"},
            "files": PATH_LIST_PROPERTY,
            "output": OUTPUT_PROPERTY,
        },
        {"repo"},
    ),
    "lfs_track": (
        {"repo": REPO_PROPERTY, "patterns": PATH_LIST_PROPERTY},
        {"repo", "patterns"},
    ),
    "lfs_untrack": (
        {"repo": REPO_PROPERTY, "patterns": PATH_LIST_PROPERTY},
        {"repo", "patterns"},
    ),
    "push": ({"repo": REPO_PROPERTY}, {"repo"}),
    "checkout": (
        {
            "repo": REPO_PROPERTY,
            "target": {"type": "string", "minLength": 1},
            "create": {"type": "boolean"},
        },
        {"repo", "target"},
    ),
    "worktree": (
        {
            "repo": REPO_PROPERTY,
            "action": {
                "type": "string",
                "minLength": 1,
                "enum": ["add", "list", "remove"],
            },
            "path": {"type": "string", "minLength": 1, "pattern": "^/"},
            "branch": {"type": "string", "minLength": 1, "pattern": "^[^-]"},
            "create_branch": {"type": "boolean", "default": True},
        },
        {"repo", "action"},
    ),
    "config_get": (
        {"repo": REPO_PROPERTY, "key": {"type": "string", "minLength": 1}},
        {"repo", "key"},
    ),
}
# A script action's credentials and timeout, where they are not none and a minute.
SCRIPT_LIMITS: dict[str, tuple[tuple[EnvMount, ...], timedelta]] = {
    "push": ((GH_TOKEN,), timedelta(minutes=11)),
}


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


@pytest.mark.parametrize("name", sorted(COMMAND_ACTIONS))
def test_command_action_runs_its_git_words(manifest: Manifest, name: str) -> None:
    """Each command action runs git-cage with its git words and parameters."""
    argv, parameters = COMMAND_ACTIONS[name]
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


@pytest.mark.parametrize("name", sorted(CHANGE_LIMITS))
def test_change_action_declares_its_credential_and_timeout(
    manifest: Manifest, name: str
) -> None:
    """Actions that reach a remote declare GH_TOKEN; LFS transfers take ten minutes."""
    action = manifest.actions[name]

    assert (action.credentials, action.timeout) == CHANGE_LIMITS[name]


@pytest.mark.parametrize("name", sorted(COMMAND_ACTIONS))
def test_command_action_requires_repo_and_its_own_parameter(
    manifest: Manifest, name: str
) -> None:
    """Only repo, and the one parameter some actions need, are required."""
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


def test_merge_states_branch_as_a_non_empty_string(manifest: Manifest) -> None:
    """The branch of merge is a non-empty string."""
    assert _properties(manifest.actions["merge"]) == {
        "repo": REPO_PROPERTY,
        "branch": {"type": "string", "minLength": 1},
    }


def test_stash_states_each_parameter(manifest: Manifest) -> None:
    """The stash action is one of git-sidecar's five; its index is not negative."""
    assert _properties(manifest.actions["stash"]) == {
        "repo": REPO_PROPERTY,
        "action": {
            "type": "string",
            "minLength": 1,
            "enum": ["push", "pop", "apply", "drop", "show"],
        },
        "message": {"type": "string", "minLength": 1},
        "index": {"type": "integer", "minimum": 0},
    }


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("stash", {"action": "clear"}),
        ("stash", {"action": "show", "index": -1}),
        ("merge", {}),
    ],
)
def test_change_action_refuses_an_input_outside_its_schema(
    manifest: Manifest, name: str, arguments: dict[str, object]
) -> None:
    """An unknown sub-action, a negative index, or a missing branch is refused."""
    with pytest.raises(InputError):
        validate_input(manifest.actions[name], {"repo": "/repo", **arguments})


def test_stash_places_its_index_after_its_action(manifest: Manifest) -> None:
    """A stash show with index 0 forms 'show 0', which git reads as stash@{0}."""
    arguments = {"repo": "/repo", "action": "show", "index": 0}

    argv = form_argv(manifest.actions["stash"], arguments)

    assert argv == ("git-cage", "stash", "--repo", "/repo", "show", "0")


@pytest.mark.parametrize("name", sorted(SCRIPT_ACTIONS.keys() - {"diff"}))
def test_script_action_runs_its_executable(manifest: Manifest, name: str) -> None:
    """Each script action but diff runs /plugin/bin/<name> with the plain result."""
    credentials, timeout = SCRIPT_LIMITS.get(name, ((), MINUTE))
    action = manifest.actions[name]

    assert isinstance(action, ScriptAction)
    assert (
        action.executable,
        action.credentials,
        action.timeout,
        action.output_schema,
    ) == (f"/plugin/bin/{name}", credentials, timeout, SCRIPT_OUTPUT_SCHEMA)


def test_diff_runs_its_executable_with_two_result_shapes(manifest: Manifest) -> None:
    """The diff output_schema is the plain result or the four-member file result."""
    action = manifest.actions["diff"]

    assert isinstance(action, ScriptAction)
    assert (
        action.executable,
        action.credentials,
        action.timeout,
        action.output_schema,
    ) == ("/plugin/bin/diff", (), MINUTE, DIFF_OUTPUT_SCHEMA)


@pytest.mark.parametrize(
    "result",
    [
        {"exit_code": 0, "stdout": "diff --git a/a b/a\n", "stderr": ""},
        {
            "exit_code": 0,
            "stderr": "",
            "path": "/repo/.agent-proxy/a.diff",
            "stat": " a | 2 +-\n",
        },
    ],
)
def test_diff_output_schema_accepts_each_result_shape(
    manifest: Manifest, result: dict[str, object]
) -> None:
    """A result of either shape the diff script returns validates."""
    action = manifest.actions["diff"]

    assert isinstance(action, ScriptAction)
    assert Draft202012Validator(action.output_schema).is_valid(result)


@pytest.mark.parametrize(
    "result",
    [
        {"exit_code": 0, "stdout": "", "stderr": "", "path": "/x", "stat": ""},
        {"exit_code": 0, "stderr": "", "path": "/x"},
    ],
)
def test_diff_output_schema_refuses_a_mixed_or_partial_result(
    manifest: Manifest, result: dict[str, object]
) -> None:
    """A result with stdout beside path, or with no stat, matches neither shape."""
    action = manifest.actions["diff"]

    assert isinstance(action, ScriptAction)
    assert not Draft202012Validator(action.output_schema).is_valid(result)


@pytest.mark.parametrize("name", sorted(SCRIPT_ACTIONS))
def test_script_action_states_its_input(manifest: Manifest, name: str) -> None:
    """Each script action states its properties and requires its own."""
    properties, required = SCRIPT_ACTIONS[name]
    action = manifest.actions[name]

    assert (_properties(action), _required(action)) == (properties, required)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("add", {"files": []}),
        ("add", {"files": [""]}),
        ("add", {"files": ["../x"]}),
        ("rm", {"files": ["a/../b"]}),
        ("restore", {"files": ["a/.."]}),
        ("restore", {"files": ["a.txt"], "staged": "yes"}),
        ("diff", {"files": ["x\n/../y"]}),
        ("diff", {"ref": "--output=/tmp/x"}),
        ("diff", {"output": ""}),
        ("diff", {"output": "/x"}),
        ("diff", {"output": ".."}),
        ("diff", {"output": "a/../b"}),
        ("lfs_track", {"patterns": [".."]}),
        ("lfs_untrack", {"patterns": []}),
        ("commit", {}),
        ("commit", {"message": ""}),
        ("commit", {"message": "m", "amend": True}),
        ("push", {"force": True}),
        ("checkout", {}),
        ("checkout", {"target": ""}),
        ("checkout", {"target": "task/c", "create": "yes"}),
        ("worktree", {}),
        ("worktree", {"action": "prune"}),
        ("worktree", {"action": "add", "path": "wt"}),
        ("worktree", {"action": "add", "path": "/wt", "branch": "-b"}),
        ("worktree", {"action": "add", "path": "/wt", "branch": "--detach"}),
        ("worktree", {"action": "add", "path": "/wt", "create_branch": "no"}),
    ],
)
def test_script_action_refuses_an_input_outside_its_schema(
    manifest: Manifest, name: str, arguments: dict[str, object]
) -> None:
    """An empty list, a '..', an option, a relative path, or a stray member fails."""
    with pytest.raises(InputError):
        validate_input(manifest.actions[name], {"repo": "/repo", **arguments})


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("add", {"files": ["a..b", "-v.txt", "dir/a.txt"]}),
        ("diff", {}),
        ("diff", {"staged": True, "ref": "HEAD~1", "files": ["a.txt"]}),
        ("diff", {"output": "a.diff"}),
        ("diff", {"output": "d/a.diff"}),
        ("lfs_track", {"patterns": ["*.bin"]}),
        ("commit", {"message": "m"}),
        ("push", {}),
        ("checkout", {"target": "task/c", "create": True}),
        ("worktree", {"action": "list"}),
        ("worktree", {"action": "remove", "path": "/wt"}),
        (
            "worktree",
            {
                "action": "add",
                "path": "/wt",
                "branch": "task/wt",
                "create_branch": False,
            },
        ),
    ],
)
def test_script_action_accepts_an_input_inside_its_schema(
    manifest: Manifest, name: str, arguments: dict[str, object]
) -> None:
    """Names with two dots inside a segment, and every optional member, pass."""
    validate_input(manifest.actions[name], {"repo": "/repo", **arguments})


def test_manifest_declares_one_action_per_git_sidecar_tool(manifest: Manifest) -> None:
    """The actions are git-sidecar's 29 git tools, each without its git_ prefix."""
    assert set(manifest.actions) == GIT_SIDECAR_TOOLS


def test_dockerfile_links_each_script_action(manifest: Manifest) -> None:
    """The Dockerfile's /plugin/bin links, scripts.ACTIONS, and the manifest agree."""
    linked = LINKED_ACTIONS.search(DOCKERFILE_PATH.read_text())
    script_actions = {
        name
        for name, action in manifest.actions.items()
        if isinstance(action, ScriptAction)
    }

    assert linked is not None
    assert (set(linked[1].split()), set(scripts.ACTIONS)) == (
        script_actions,
        script_actions,
    )
