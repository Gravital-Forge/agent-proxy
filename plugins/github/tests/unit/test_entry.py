import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from github_plugin import entry

ENTRY = Path(__file__).parents[2] / "scripts" / "entry"
STUB_DIR = Path(__file__).parents[1] / "stub"
TOKEN = "entry-token"
CLOSE_INPUT = json.dumps({"repo": "o/n", "pr_number": 5})


@pytest.fixture(autouse=True)
def _stub_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the stub gh first on PATH, with a token, o/n listed, and no log."""
    monkeypatch.setenv("PATH", f"{STUB_DIR}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_REPOS", "o/n")
    monkeypatch.delenv("GH_STUB_LOG", raising=False)


def _close_line() -> str:
    """Return the compact JSON line pr_close prints for pull request 5 of o/n."""
    echo = {"argv": ["pr", "close", "5", "--repo", "o/n"], "gh_token": TOKEN}
    result = {
        "exit_code": 0,
        "stdout": json.dumps(echo, separators=(",", ":")) + "\n",
        "stderr": "",
    }
    return json.dumps(result, separators=(",", ":")) + "\n"


def test_main_sends_a_script_name_to_its_action(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The main function prints the action's result on its stdin input as one line."""
    stdin = io.TextIOWrapper(io.BytesIO(CLOSE_INPUT.encode()))
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = entry.main(["/plugin/bin/pr_close"])

    assert exit_code == 0
    assert capsys.readouterr().out == _close_line()


def test_main_refuses_a_name_with_no_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A name that no script action runs under is refused with exit 2."""
    exit_code = entry.main(["/plugin/bin/pr_list"])

    assert exit_code == 2
    assert capsys.readouterr().err == (
        "github plugin refused: no action is named 'pr_list'\n"
    )


def test_a_script_link_prints_its_result_as_compact_json(tmp_path: Path) -> None:
    """A link named for a script action, run as a program, prints one JSON line."""
    link = tmp_path / "bin" / "pr_close"
    link.parent.mkdir()
    link.symlink_to(ENTRY)

    completed = subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [sys.executable, str(link)],
        capture_output=True,
        check=False,
        input=CLOSE_INPUT,
        text=True,
    )

    assert (completed.returncode, completed.stdout) == (0, _close_line())


def test_main_checks_a_script_actions_repo_against_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A script action on a repo the process's GITHUB_REPOS omits prints a refusal."""
    monkeypatch.setenv("GITHUB_REPOS", "other/*")
    stdin = io.TextIOWrapper(io.BytesIO(CLOSE_INPUT.encode()))
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = entry.main(["/plugin/bin/pr_close"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "exit_code": 2,
        "stdout": "",
        "stderr": "github plugin refused: repository 'o/n' is not in GITHUB_REPOS\n",
    }


def test_main_sends_gh_cage_to_the_wrapper(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The name gh-cage runs the wrapper, whatever its directory."""
    exit_code = entry.main(["/usr/local/bin/gh-cage", "run", "list"])

    assert exit_code == 2
    assert capsys.readouterr().err == "github plugin refused: --repo is missing\n"
