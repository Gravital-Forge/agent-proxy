import re
from datetime import timedelta
from pathlib import Path

import pytest
from agent_proxy.config import EnvMount
from agent_proxy.plugins import Manifest, ScriptAction, parse_manifest
from agent_proxy.runner import InputError, validate_input
from hcp_terraform_plugin import actions

PLUGIN_DIR = Path(__file__).parents[2]
MANIFEST_PATH = PLUGIN_DIR / "manifest.json"
DOCKERFILE_PATH = PLUGIN_DIR / "Dockerfile"
GITHUB_DOCKERFILE_PATH = PLUGIN_DIR.parent / "github" / "Dockerfile"
LINKED_ACTIONS = re.compile(r"for action in ([^;]+); do")
TFE_TOKEN = (EnvMount(env="TFE_TOKEN"),)
TIMEOUT = timedelta(minutes=2)
NAME_PROPERTY = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]*$"}
RUN_ID_PROPERTY = {"type": "string", "pattern": "^run-[A-Za-z0-9]+$"}
LIMIT_PROPERTY = {"type": "integer", "minimum": 1, "maximum": 100}
OBJECTS = {"type": "array", "items": {"type": "object"}}
# Each action's input properties, and the properties it requires.
INPUTS: dict[str, tuple[dict[str, dict[str, object]], set[str]]] = {
    "workspace_list": ({}, set()),
    "run_list": (
        {
            "organization": NAME_PROPERTY,
            "workspace": NAME_PROPERTY,
            "limit": LIMIT_PROPERTY,
        },
        {"organization", "workspace"},
    ),
    "run_view": ({"run_id": RUN_ID_PROPERTY}, {"run_id"}),
    "plan_log": ({"run_id": RUN_ID_PROPERTY}, {"run_id"}),
    "apply_log": ({"run_id": RUN_ID_PROPERTY}, {"run_id"}),
}
# Each action's data member on success, and that member's schema.
DATA_MEMBERS: dict[str, tuple[str, dict[str, object]]] = {
    "workspace_list": ("workspaces", OBJECTS),
    "run_list": ("runs", OBJECTS),
    "run_view": ("run", {"type": "object"}),
    "plan_log": ("log", {"type": "string"}),
    "apply_log": ("log", {"type": "string"}),
}


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    """The plugin's manifest.json, parsed as the core parses describe's output."""
    return parse_manifest(MANIFEST_PATH.read_bytes())


def _output_schema(member: str, schema: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "exit_code": {"type": "integer"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            member: schema,
        },
        "required": ["exit_code", "stdout", "stderr"],
        "additionalProperties": False,
    }


def _properties(action: ScriptAction) -> dict[str, dict[str, object]]:
    properties = action.input_schema.get("properties")
    assert isinstance(properties, dict)
    return properties


def _required(action: ScriptAction) -> set[str]:
    required = action.input_schema.get("required", [])
    assert isinstance(required, list)
    return set(required)


def test_manifest_parses_with_no_warning(manifest: Manifest) -> None:
    """The core parses manifest.json and warns of nothing."""
    assert manifest.warnings() == ()


def test_manifest_declares_the_five_reads(manifest: Manifest) -> None:
    """The manifest's actions are exactly the actions module's."""
    assert set(manifest.actions) == set(INPUTS) == set(actions.ACTIONS)


@pytest.mark.parametrize("name", sorted(DATA_MEMBERS))
def test_script_action_runs_its_executable(manifest: Manifest, name: str) -> None:
    """Each action runs /plugin/bin/<name> with TFE_TOKEN, 2m, and its schema."""
    action = manifest.actions[name]

    assert isinstance(action, ScriptAction)
    assert (
        action.executable,
        action.credentials,
        action.timeout,
        action.output_schema,
    ) == (
        f"/plugin/bin/{name}",
        TFE_TOKEN,
        TIMEOUT,
        _output_schema(*DATA_MEMBERS[name]),
    )


@pytest.mark.parametrize("name", sorted(INPUTS))
def test_action_states_its_input(manifest: Manifest, name: str) -> None:
    """Each action states its properties and requires its own."""
    properties, required = INPUTS[name]
    action = manifest.actions[name]
    assert isinstance(action, ScriptAction)

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


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("workspace_list", {"organization": "o"}),
        ("run_list", {"organization": "o"}),
        ("run_list", {"organization": "-o", "workspace": "a"}),
        ("run_list", {"organization": "o/x", "workspace": "a"}),
        ("run_list", {"organization": "o", "workspace": "a.b"}),
        ("run_list", {"organization": "o", "workspace": "a", "limit": 0}),
        ("run_list", {"organization": "o", "workspace": "a", "limit": 101}),
        ("run_view", {"run_id": "plan-1"}),
        ("run_view", {"run_id": "run-1/../x"}),
        ("plan_log", {"run_id": "run-1", "force": True}),
        ("apply_log", {}),
    ],
)
def test_action_refuses_an_input_outside_its_schema(
    manifest: Manifest, name: str, arguments: dict[str, object]
) -> None:
    """A missing, malformed, out-of-range, or stray member fails."""
    with pytest.raises(InputError):
        validate_input(manifest.actions[name], arguments)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("workspace_list", {}),
        ("run_list", {"organization": "Org-1", "workspace": "a_B-9"}),
        ("run_list", {"organization": "o", "workspace": "a", "limit": 1}),
        ("run_list", {"organization": "o", "workspace": "a", "limit": 100}),
        ("run_view", {"run_id": "run-AbC123"}),
        ("plan_log", {"run_id": "run-1"}),
        ("apply_log", {"run_id": "run-1"}),
    ],
)
def test_action_accepts_an_input_inside_its_schema(
    manifest: Manifest, name: str, arguments: dict[str, object]
) -> None:
    """Names, run IDs, and limits from 1 to 100 pass."""
    validate_input(manifest.actions[name], arguments)


def test_dockerfile_links_each_script_action(manifest: Manifest) -> None:
    """The Dockerfile's /plugin/bin links, ACTIONS, and the manifest agree."""
    linked = LINKED_ACTIONS.search(DOCKERFILE_PATH.read_text())

    assert linked is not None
    assert set(linked[1].split()) == set(actions.ACTIONS) == set(manifest.actions)


def test_dockerfile_starts_from_the_github_plugins_pinned_alpine() -> None:
    """The image's base is the same digest-pinned Alpine as the github plugin's."""
    assert (
        DOCKERFILE_PATH.read_text().splitlines()[0]
        == GITHUB_DOCKERFILE_PATH.read_text().splitlines()[0]
    )
