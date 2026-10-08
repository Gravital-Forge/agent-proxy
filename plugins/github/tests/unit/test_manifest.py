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
from github_plugin import scripts

MANIFEST_PATH = Path(__file__).parents[2] / "manifest.json"
DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile"
LINKED_ACTIONS = re.compile(r"for action in ([^;]+); do")
GIT_SIDECAR_GH_TOOLS = {
    "gh_pr_create",
    "gh_pr_edit",
    "gh_pr_view",
    "gh_pr_list",
    "gh_pr_fetch",
    "gh_pr_reply",
    "gh_pr_checks",
    "gh_pr_close",
    "gh_run_view",
    "gh_run_list",
}
GH_TOKEN = (EnvMount(env="GH_TOKEN"),)
MINUTE = timedelta(seconds=60)
REPO = Parameter(name="repo", placement="flag", flag="--repo")
REPO_PROPERTY = {
    "type": "string",
    "pattern": "^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]*[A-Za-z0-9_-][A-Za-z0-9_.-]*$",
}
NUMBER_PROPERTY = {"type": "integer", "minimum": 1}
TEXT_PROPERTY = {"type": "string"}
NON_EMPTY_PROPERTY = {"type": "string", "minLength": 1}
FLAG_PROPERTY = {"type": "boolean"}
# Each command action's argv, and its parameters in order.
COMMAND_ACTIONS: dict[str, tuple[tuple[str, ...], tuple[Parameter, ...]]] = {
    "pr_list": (
        ("gh", "pr", "list", "--json", "number,title,url,headRefName,state"),
        (REPO, Parameter(name="head", placement="flag", flag="--head")),
    ),
    "run_view": (
        ("gh", "run", "view"),
        (
            Parameter(name="run_id", placement="positional", flag=None),
            REPO,
            Parameter(name="log_failed", placement="flag", flag="--log-failed"),
        ),
    ),
    "run_list": (("gh", "run", "list"), (REPO,)),
}
SCRIPT_TIMEOUTS = {
    "pr_create": MINUTE,
    "pr_edit": MINUTE,
    "pr_view": MINUTE,
    "pr_fetch": timedelta(minutes=3),
    "pr_reply": MINUTE,
    "pr_checks": timedelta(minutes=5),
    "job_log_url": MINUTE,
    "pr_close": MINUTE,
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
# Each action's input properties, and the properties it requires.
INPUTS: dict[str, tuple[dict[str, dict[str, object]], set[str]]] = {
    "pr_create": (
        {
            "repo": REPO_PROPERTY,
            "base": NON_EMPTY_PROPERTY,
            "head": NON_EMPTY_PROPERTY,
            "title": NON_EMPTY_PROPERTY,
            "body": TEXT_PROPERTY,
        },
        {"repo", "base", "head", "title", "body"},
    ),
    "pr_edit": (
        {
            "repo": REPO_PROPERTY,
            "pr_number": NUMBER_PROPERTY,
            "title": TEXT_PROPERTY,
            "body": TEXT_PROPERTY,
            "base": TEXT_PROPERTY,
        },
        {"repo", "pr_number"},
    ),
    "pr_view": (
        {
            "repo": REPO_PROPERTY,
            "pr": {"type": "string", "pattern": "^[^-]"},
            "fields": TEXT_PROPERTY,
        },
        {"repo", "pr"},
    ),
    "pr_list": ({"repo": REPO_PROPERTY, "head": NON_EMPTY_PROPERTY}, {"repo"}),
    "pr_fetch": (
        {"repo": REPO_PROPERTY, "pr_number": NUMBER_PROPERTY},
        {"repo", "pr_number"},
    ),
    "pr_reply": (
        {
            "repo": REPO_PROPERTY,
            "pr_number": NUMBER_PROPERTY,
            "body": TEXT_PROPERTY,
            "comment_id": NUMBER_PROPERTY,
        },
        {"repo", "pr_number", "body"},
    ),
    "pr_checks": (
        {"repo": REPO_PROPERTY, "pr_number": NUMBER_PROPERTY},
        {"repo", "pr_number"},
    ),
    "job_log_url": (
        {"repo": REPO_PROPERTY, "job_id": NUMBER_PROPERTY},
        {"repo", "job_id"},
    ),
    "pr_close": (
        {
            "repo": REPO_PROPERTY,
            "pr_number": NUMBER_PROPERTY,
            "comment": TEXT_PROPERTY,
            "delete_branch": FLAG_PROPERTY,
        },
        {"repo", "pr_number"},
    ),
    "run_view": (
        {
            "repo": REPO_PROPERTY,
            "run_id": NUMBER_PROPERTY,
            "log_failed": FLAG_PROPERTY,
        },
        {"repo", "run_id"},
    ),
    "run_list": ({"repo": REPO_PROPERTY}, {"repo"}),
}


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    """The plugin's manifest.json, parsed as the core parses describe's output."""
    return parse_manifest(MANIFEST_PATH.read_bytes())


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


def test_manifest_declares_one_action_per_git_sidecar_gh_tool(
    manifest: Manifest,
) -> None:
    """The actions are git-sidecar's GitHub tools, unprefixed, and job_log_url."""
    assert set(manifest.actions) == {
        tool.removeprefix("gh_") for tool in GIT_SIDECAR_GH_TOOLS
    } | {"job_log_url"}


@pytest.mark.parametrize("name", sorted(COMMAND_ACTIONS))
def test_command_action_runs_its_gh_words(manifest: Manifest, name: str) -> None:
    """Each command action runs gh with its words and parameters, no pass-through."""
    argv, parameters = COMMAND_ACTIONS[name]
    action = manifest.actions[name]

    assert isinstance(action, CommandAction)
    assert (
        action.argv,
        action.parameters,
        action.passthrough_args,
        action.credentials,
        action.timeout,
    ) == (argv, parameters, False, GH_TOKEN, MINUTE)


@pytest.mark.parametrize("name", sorted(SCRIPT_TIMEOUTS))
def test_script_action_runs_its_executable(manifest: Manifest, name: str) -> None:
    """Each script action runs /plugin/bin/<name> with GH_TOKEN and its timeout."""
    action = manifest.actions[name]

    assert isinstance(action, ScriptAction)
    assert (
        action.executable,
        action.credentials,
        action.timeout,
        action.output_schema,
    ) == (f"/plugin/bin/{name}", GH_TOKEN, SCRIPT_TIMEOUTS[name], SCRIPT_OUTPUT_SCHEMA)


@pytest.mark.parametrize("name", sorted(INPUTS))
def test_action_states_its_input(manifest: Manifest, name: str) -> None:
    """Each action states its properties and requires its own."""
    properties, required = INPUTS[name]
    action = manifest.actions[name]

    assert (_properties(action), _required(action)) == (properties, required)


def test_every_action_refuses_an_unknown_member(manifest: Manifest) -> None:
    """Every input_schema is an object that sets additionalProperties to false."""
    offenders = [
        name
        for name, action in manifest.actions.items()
        if action.input_schema.get("type") != "object"
        or action.input_schema.get("additionalProperties") is not False
    ]

    assert offenders == []


def test_pr_edit_requires_one_field_to_change(manifest: Manifest) -> None:
    """The pr_edit schema requires at least one of title, body, and base."""
    assert manifest.actions["pr_edit"].input_schema.get("anyOf") == [
        {"required": ["title"]},
        {"required": ["body"]},
        {"required": ["base"]},
    ]


@pytest.mark.parametrize(
    ("name", "arguments", "argv"),
    [
        (
            "pr_list",
            {"repo": "o/n", "head": "task/x"},
            (
                "gh",
                "pr",
                "list",
                "--json",
                "number,title,url,headRefName,state",
                "--repo",
                "o/n",
                "--head",
                "task/x",
            ),
        ),
        (
            "pr_list",
            {"repo": "o/n"},
            (
                "gh",
                "pr",
                "list",
                "--json",
                "number,title,url,headRefName,state",
                "--repo",
                "o/n",
            ),
        ),
        (
            "run_view",
            {"repo": "o/n", "run_id": 42, "log_failed": True},
            ("gh", "run", "view", "42", "--repo", "o/n", "--log-failed"),
        ),
        (
            "run_view",
            {"repo": "o/n", "run_id": 42},
            ("gh", "run", "view", "42", "--repo", "o/n"),
        ),
        ("run_list", {"repo": "o/n"}, ("gh", "run", "list", "--repo", "o/n")),
    ],
)
def test_command_action_forms_its_argv(
    manifest: Manifest,
    name: str,
    arguments: dict[str, object],
    argv: tuple[str, ...],
) -> None:
    """A full and a minimal input each form the argv the action table gives."""
    action = manifest.actions[name]
    assert isinstance(action, CommandAction)

    validate_input(action, arguments)

    assert form_argv(action, arguments) == argv


@pytest.mark.parametrize("repo", ["o", "-o/n", "o/n/x", "o/..", "o/."])
def test_repo_pattern_refuses_a_value_that_is_not_owner_and_name(
    manifest: Manifest, repo: str
) -> None:
    """The repo pattern every action shares refuses a malformed owner/name."""
    with pytest.raises(InputError):
        validate_input(manifest.actions["run_list"], {"repo": repo})


@pytest.mark.parametrize("repo", ["o/n", "my-org/my.repo_1"])
def test_repo_pattern_accepts_an_owner_and_name(manifest: Manifest, repo: str) -> None:
    """The repo pattern every action shares accepts dashes, dots, and underscores."""
    validate_input(manifest.actions["run_list"], {"repo": repo})


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("pr_edit", {"pr_number": 5}),
        ("pr_view", {"pr": "-x"}),
        ("pr_close", {"pr_number": 0}),
        ("job_log_url", {"job_id": 0}),
        ("pr_create", {"base": "main", "head": "", "title": "t", "body": "b"}),
        ("pr_close", {"pr_number": 5, "force": True}),
    ],
)
def test_action_refuses_an_input_outside_its_schema(
    manifest: Manifest, name: str, arguments: dict[str, object]
) -> None:
    """No field to edit, an option as pr, a zero number, or a stray member fails."""
    with pytest.raises(InputError):
        validate_input(manifest.actions[name], {"repo": "o/n", **arguments})


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("pr_edit", {"pr_number": 5, "title": ""}),
        ("pr_view", {"pr": "task/x"}),
        ("pr_reply", {"pr_number": 5, "body": "b", "comment_id": 9}),
        ("pr_close", {"pr_number": 5, "comment": "", "delete_branch": True}),
    ],
)
def test_action_accepts_an_input_inside_its_schema(
    manifest: Manifest, name: str, arguments: dict[str, object]
) -> None:
    """An empty field to edit, a branch as pr, and every optional member pass."""
    validate_input(manifest.actions[name], {"repo": "o/n", **arguments})


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
