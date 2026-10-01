"""The instance scripts refuse bad callers and act in the agent's tree as the agent."""

import os
import re
import subprocess
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).parents[3] / "docker"

TREE_COMMAND = re.compile(r"(?<![\w-])(?:chgrp|chmod|find|install|rm|setfacl)(?![\w-])")

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="the root refusal is only observable as a non-root user"
)


def run_script(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the script name from core/docker with arguments, capturing its output."""
    return subprocess.run(  # noqa: S603 - the tests pass their own arguments
        [str(DOCKER_DIR / name), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("provision-instance.sh", ("agent-01", "8100", "/nonexistent")),
        ("teardown-instance.sh", ("agent-01",)),
    ],
)
def test_script_refuses_a_non_root_user(name: str, arguments: tuple[str, ...]) -> None:
    """A full command line from a non-root user exits 1 before any step runs."""
    result = run_script(name, *arguments)

    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == f"{name}: must run as root\n"


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        (
            "provision-instance.sh",
            "usage: provision-instance.sh <agent> <port> <config-file>",
        ),
        ("teardown-instance.sh", "usage: teardown-instance.sh <agent>"),
    ],
)
def test_script_without_arguments_prints_its_usage(name: str, usage: str) -> None:
    """No arguments exits 2 with the usage line on stderr."""
    result = run_script(name)

    assert (result.returncode, result.stdout) == (2, "")
    assert result.stderr == f"{usage}\n"


def function_body(script: str, function: str) -> str:
    """Return the body of the shell function named function in script."""
    text = (DOCKER_DIR / script).read_text()
    pattern = rf"^{function}\(\) \{{\n(.*?)^\}}"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    assert match, f"{script} defines no function {function}"
    return match.group(1)


@pytest.mark.parametrize("script", ["provision-instance.sh", "teardown-instance.sh"])
def test_as_agent_runs_as_the_agent_from_the_root_directory(script: str) -> None:
    """The helper drops to the agent through runuser, outside root's cwd."""
    expected = '(cd / && runuser -u "$agent" -- "$@")'
    assert function_body(script, "as_agent").strip() == expected


@pytest.mark.parametrize(
    ("script", "function"),
    [
        ("provision-instance.sh", "create_overflow_dir"),
        ("provision-instance.sh", "apply_tree_permissions"),
        ("teardown-instance.sh", "remove_overflow_dir"),
    ],
)
def test_step_changes_the_agent_tree_only_as_the_agent(
    script: str, function: str
) -> None:
    """Every command that walks or writes the agent's tree runs as the agent."""
    body = function_body(script, function)

    unguarded = [
        match.group()
        for match in TREE_COMMAND.finditer(body)
        if not body[: match.start()].endswith(("as_agent ", "-exec "))
    ]

    assert TREE_COMMAND.search(body)
    assert unguarded == []
