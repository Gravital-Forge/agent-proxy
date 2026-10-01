import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from git_plugin import entry

ENTRY = Path(__file__).parents[2] / "scripts" / "entry"
ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
HELPER_VALUE = '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty repository the host's git creates at tmp_path/root/repo."""
    repo = tmp_path / "root" / "repo"
    subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        ["git", "init", "-q", str(repo)],  # noqa: S607 - the host's git, on PATH
        check=True,
        env={**os.environ, **ISOLATED_GIT},
    )
    return repo


@pytest.fixture
def git_cage(tmp_path: Path) -> Path:
    """A link named git-cage to scripts/entry, as the image installs it."""
    link = tmp_path / "bin" / "git-cage"
    link.parent.mkdir()
    link.symlink_to(ENTRY)
    return link


def _run(
    link: Path, environment: Mapping[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    """Run link with this interpreter, the host's git config left out."""
    return subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [sys.executable, str(link), *args],
        capture_output=True,
        check=False,
        text=True,
        env={**os.environ, **ISOLATED_GIT, **environment},
    )


def _roots(*roots: Path) -> dict[str, str]:
    """Return AGENT_PROXY_ROOTS naming roots, as the core sets it."""
    return {"AGENT_PROXY_ROOTS": json.dumps([str(root) for root in roots])}


def test_git_cage_runs_git_in_the_repo(git_cage: Path, repo: Path) -> None:
    """git-cage runs git -C the repo, so git reports the repo's top level."""
    completed = _run(
        git_cage,
        _roots(repo.parent),
        "rev-parse",
        "--repo",
        str(repo),
        "--show-toplevel",
    )

    assert (completed.returncode, completed.stdout) == (0, f"{repo}\n")


def test_git_cage_trusts_each_root_and_what_lies_beneath(
    git_cage: Path, repo: Path
) -> None:
    """Git sees the root and the root's /* as safe.directory values."""
    completed = _run(
        git_cage,
        _roots(repo.parent),
        "config",
        "--get-all",
        "--repo",
        str(repo),
        "safe.directory",
    )

    assert completed.stdout == f"{repo.parent}\n{repo.parent}/*\n"


def test_git_cage_clears_inherited_helpers_then_sets_its_own(
    git_cage: Path, repo: Path
) -> None:
    """Git sees an empty helper, which clears any other, then the GH_TOKEN helper."""
    completed = _run(
        git_cage,
        _roots(repo.parent),
        "config",
        "--get-all",
        "--repo",
        str(repo),
        "credential.helper",
    )

    assert completed.stdout == f"\n{HELPER_VALUE}\n"


def test_git_cage_exits_with_gits_code(git_cage: Path, repo: Path) -> None:
    """Git's exit code and stderr are the wrapper's, since git replaced it."""
    completed = _run(
        git_cage,
        _roots(repo.parent),
        "rev-parse",
        "--repo",
        str(repo),
        "--verify",
        "missing",
    )

    assert completed.returncode == 128
    assert completed.stderr.startswith("fatal: ")


def test_git_cage_refuses_without_roots(git_cage: Path, repo: Path) -> None:
    """git-cage refuses with exit 2 when AGENT_PROXY_ROOTS is not set."""
    completed = _run(git_cage, {}, "status", "--repo", str(repo))

    assert (completed.returncode, completed.stdout, completed.stderr) == (
        2,
        "",
        "git plugin refused: AGENT_PROXY_ROOTS is not set\n",
    )


def test_main_sends_git_cage_to_the_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The name git-cage runs the wrapper, whatever its directory."""
    monkeypatch.setenv("AGENT_PROXY_ROOTS", "[]")

    exit_code = entry.main(["/usr/local/bin/git-cage", "status"])

    assert exit_code == 2
    assert capsys.readouterr().err == "git plugin refused: --repo is missing\n"


def test_main_refuses_a_name_with_no_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A name that no action runs under is refused with exit 2."""
    exit_code = entry.main(["/plugin/bin/push"])

    assert exit_code == 2
    assert capsys.readouterr().err == "git plugin refused: no action is named 'push'\n"
