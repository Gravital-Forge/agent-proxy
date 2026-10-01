"""The provisioning and teardown scripts refuse a short command line and a non-root user."""

import os
import subprocess
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).parents[3] / "docker"

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
