import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from hcp_terraform_plugin import entry

ENTRY = Path(__file__).parents[2] / "scripts" / "entry"
REFUSED_LINE = (
    json.dumps(
        {
            "exit_code": 2,
            "stdout": "",
            "stderr": (
                "hcp-terraform plugin refused: HCP_TERRAFORM_WORKSPACES is not set "
                "or names no workspace; the grant's env sets it\n"
            ),
        },
        separators=(",", ":"),
    )
    + "\n"
)


@pytest.fixture(autouse=True)
def _no_workspace_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset the workspace list, so every action refuses before any request."""
    monkeypatch.delenv("HCP_TERRAFORM_WORKSPACES", raising=False)


def test_main_sends_a_script_name_to_its_action(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The main function prints the action's result on its stdin input as one line."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))

    exit_code = entry.main(["/plugin/bin/workspace_list"])

    assert (exit_code, capsys.readouterr().out) == (0, REFUSED_LINE)


def test_main_refuses_a_name_with_no_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A name that no script action runs under is refused with exit 2."""
    exit_code = entry.main(["/plugin/bin/run_apply"])

    assert exit_code == 2
    assert capsys.readouterr().err == (
        "hcp-terraform plugin refused: no action is named 'run_apply'\n"
    )


def test_a_script_link_prints_its_result_as_compact_json(tmp_path: Path) -> None:
    """A link named for a script action, run as a program, prints one JSON line."""
    link = tmp_path / "bin" / "plan_log"
    link.parent.mkdir()
    link.symlink_to(ENTRY)

    completed = subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [sys.executable, str(link)],
        capture_output=True,
        check=False,
        input='{"run_id":"run-1"}',
        text=True,
    )

    assert (completed.returncode, completed.stdout) == (0, REFUSED_LINE)
