import io
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from git_plugin import cage, entry

ENTRY = Path(__file__).parents[2] / "scripts" / "entry"
ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
REQUEST = "protocol=https\nhost=github.com\npath=octo/hello.git\n"
GRANTED = "username=x-access-token\npassword=dummy\n"


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
    link: Path, environment: Mapping[str, str], *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run link with this interpreter, the host's git config left out."""
    return subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [sys.executable, str(link), *args],
        capture_output=True,
        check=False,
        input=stdin,
        text=True,
        env={**os.environ, **ISOLATED_GIT, **environment},
    )


def _roots(*roots: Path) -> dict[str, str]:
    """Return AGENT_PROXY_ROOTS naming roots, as the core sets it."""
    return {"AGENT_PROXY_ROOTS": json.dumps([str(root) for root in roots])}


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
    exit_code = entry.main(["/plugin/bin/clone"])

    assert exit_code == 2
    assert capsys.readouterr().err == "git plugin refused: no action is named 'clone'\n"


def test_a_helper_link_answers_gits_get(tmp_path: Path) -> None:
    """A link named git-credential-agent-proxy answers get with the token."""
    link = tmp_path / "bin" / "git-credential-agent-proxy"
    link.parent.mkdir()
    link.symlink_to(ENTRY)
    environment = {"GITHUB_REPOS": "octo/hello", "GH_TOKEN": "dummy"}

    completed = _run(link, environment, "get", stdin=REQUEST)

    assert (completed.returncode, completed.stdout, completed.stderr) == (
        0,
        GRANTED,
        "",
    )


def test_main_sends_the_helper_name_to_the_credential_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The name git-credential-agent-proxy runs the helper on the request on stdin."""
    monkeypatch.setenv("GITHUB_REPOS", "octo/hello")
    monkeypatch.setenv("GH_TOKEN", "dummy")
    monkeypatch.setattr(sys, "stdin", io.StringIO(REQUEST))

    exit_code = entry.main(["/plugin/bin/git-credential-agent-proxy", "get"])

    assert exit_code == 0
    assert capsys.readouterr().out == GRANTED


def test_a_script_link_prints_its_result_as_compact_json(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link named for a script action prints one JSON line and exits 0.

    The environment holds no identity, so commit is refused before git runs,
    and the refusal is the printed result.
    """
    for variable in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        monkeypatch.delenv(variable, raising=False)
    link = tmp_path / "bin" / "commit"
    link.parent.mkdir()
    link.symlink_to(ENTRY)
    stdin = json.dumps({"repo": str(repo), "message": "m"})

    completed = _run(link, _roots(repo.parent), stdin=stdin)

    assert (completed.returncode, completed.stdout) == (
        0,
        '{"exit_code":2,"stdout":"","stderr":'
        '"git plugin refused: '
        "GIT_AUTHOR_NAME is not set in the grant's env\\n\"}\n",
    )


def test_main_sends_a_script_name_to_its_action(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], repo: Path
) -> None:
    """The main function reads the input on stdin and prints the action's result.

    The builder is replaced by one that leaves the environment as it is: the
    host's git predates the files:// form of extensions.refStorage.
    """
    monkeypatch.setattr(cage, "build_isolation", lambda _repo, environ: dict(environ))
    for name, value in ISOLATED_GIT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENT_PROXY_ROOTS", json.dumps([str(repo.parent)]))
    payload = json.dumps({"repo": str(repo)}).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))

    exit_code = entry.main(["/plugin/bin/diff"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
    }
