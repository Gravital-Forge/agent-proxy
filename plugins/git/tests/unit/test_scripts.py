import json
import os
import subprocess
from pathlib import Path

import pytest
from git_plugin import scripts
from git_plugin.cage import RefusalError

NAME = "Fixture"
EMAIL = "fixture@example.invalid"


@pytest.fixture(autouse=True)
def _isolate_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host's global and system git config away from every git run."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(repo: Path, *args: str) -> str:
    """Run the host's git in repo and return its stdout."""
    completed = subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        ["git", "-C", str(repo), *args],  # noqa: S607 - the host's git, on PATH
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository at tmp_path/root/repo with an identity and a.txt holding one."""
    repo = tmp_path / "root" / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", NAME)
    _git(repo, "config", "user.email", EMAIL)
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    return repo


@pytest.fixture
def roots(repo: Path) -> list[str]:
    """The root above repo, as the only root of the cage."""
    return [str(repo.parent)]


def test_actions_holds_each_script_action() -> None:
    """The table names every script action this plugin runs."""
    assert set(scripts.ACTIONS) == {
        "add",
        "rm",
        "restore",
        "commit",
        "diff",
        "lfs_track",
        "lfs_untrack",
    }


def test_run_git_returns_gits_exit_code_and_streams(
    repo: Path, roots: list[str]
) -> None:
    """run_git returns git's exit code, stdout, and stderr."""
    result = scripts.run_git(str(repo), roots, ["rev-parse", "--abbrev-ref", "HEAD"])

    assert result == {"exit_code": 0, "stdout": "main\n", "stderr": ""}


def test_run_git_returns_a_failure_as_a_result(repo: Path, roots: list[str]) -> None:
    """A git failure is a result, its exit code and stderr git's own."""
    result = scripts.run_git(str(repo), roots, ["rev-parse", "--verify", "missing"])

    assert result["exit_code"] == 128
    assert result["stderr"].startswith("fatal: ")


def test_run_git_replaces_bytes_that_are_not_utf8(repo: Path, roots: list[str]) -> None:
    """A byte that is not UTF-8 becomes the replacement character."""
    (repo / "a.txt").write_bytes(b"\xff\n")

    result = scripts.run_git(str(repo), roots, ["diff"])

    assert "+\N{REPLACEMENT CHARACTER}\n" in result["stdout"]


def test_run_git_trusts_each_root(repo: Path, roots: list[str]) -> None:
    """run_git runs git with the root and the root's /* as safe.directory."""
    result = scripts.run_git(
        str(repo), roots, ["config", "--get-all", "safe.directory"]
    )

    assert result["stdout"] == f"{roots[0]}\n{roots[0]}/*\n"


def test_run_git_adds_env_to_gits_environment(repo: Path, roots: list[str]) -> None:
    """The env mapping reaches git on top of the process environment."""
    env = {"GIT_AUTHOR_NAME": "Other", "GIT_AUTHOR_EMAIL": "other@example.invalid"}

    result = scripts.run_git(str(repo), roots, ["var", "GIT_AUTHOR_IDENT"], env)

    assert result["stdout"].startswith("Other <other@example.invalid> ")


def test_add_stages_the_files(repo: Path, roots: list[str]) -> None:
    """The add action stages each file it names."""
    (repo / "b.txt").write_text("b\n")
    (repo / "c.txt").write_text("c\n")

    result = scripts.add({"repo": str(repo), "files": ["b.txt", "c.txt"]}, roots)

    assert result["exit_code"] == 0
    assert _git(repo, "diff", "--cached", "--name-only") == "b.txt\nc.txt\n"


def test_add_takes_a_file_that_begins_with_a_dash(repo: Path, roots: list[str]) -> None:
    """A file named like an option is a path, since the files follow '--'."""
    (repo / "-v.txt").write_text("v\n")

    result = scripts.add({"repo": str(repo), "files": ["-v.txt"]}, roots)

    assert result["exit_code"] == 0
    assert _git(repo, "diff", "--cached", "--name-only") == "-v.txt\n"


def test_add_of_a_missing_file_returns_gits_error(repo: Path, roots: list[str]) -> None:
    """The result is git's refusal of a pathspec that matches nothing."""
    result = scripts.add({"repo": str(repo), "files": ["missing.txt"]}, roots)

    assert result["exit_code"] == 128
    assert "pathspec" in result["stderr"]


def test_rm_removes_the_file_from_the_index_and_the_tree(
    repo: Path, roots: list[str]
) -> None:
    """The rm action untracks the file and deletes it."""
    result = scripts.rm({"repo": str(repo), "files": ["a.txt"]}, roots)

    assert result["exit_code"] == 0
    assert _git(repo, "ls-files") == ""
    assert not (repo / "a.txt").exists()


def test_restore_discards_a_working_tree_change(repo: Path, roots: list[str]) -> None:
    """The restore action without staged puts the file back as the index holds it."""
    (repo / "a.txt").write_text("changed\n")

    result = scripts.restore({"repo": str(repo), "files": ["a.txt"]}, roots)

    assert result["exit_code"] == 0
    assert (repo / "a.txt").read_text() == "one\n"


def test_restore_with_staged_unstages_and_keeps_the_change(
    repo: Path, roots: list[str]
) -> None:
    """The restore action with staged unstages the change and keeps the edit."""
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "add", "a.txt")

    result = scripts.restore(
        {"repo": str(repo), "files": ["a.txt"], "staged": True}, roots
    )

    assert result["exit_code"] == 0
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert (repo / "a.txt").read_text() == "changed\n"


def test_diff_shows_a_working_tree_change(repo: Path, roots: list[str]) -> None:
    """The diff action with no options shows what the working tree changed."""
    (repo / "a.txt").write_text("two\n")

    result = scripts.diff({"repo": str(repo)}, roots)

    assert "-one\n+two\n" in result["stdout"]


def test_diff_with_staged_shows_only_the_staged_change(
    repo: Path, roots: list[str]
) -> None:
    """The diff action with staged shows the index's change, not the tree's."""
    (repo / "a.txt").write_text("two\n")
    _git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("three\n")

    result = scripts.diff({"repo": str(repo), "staged": True}, roots)

    assert "+two\n" in result["stdout"]
    assert "+three" not in result["stdout"]


def test_diff_with_a_ref_compares_against_it(repo: Path, roots: list[str]) -> None:
    """The diff action with ref compares the working tree with that commit."""
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "second")

    result = scripts.diff({"repo": str(repo), "ref": "HEAD~1"}, roots)

    assert "-one\n+two\n" in result["stdout"]


def test_diff_with_files_limits_the_paths(repo: Path, roots: list[str]) -> None:
    """The diff action with files shows only those files."""
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "b")
    (repo / "a.txt").write_text("two\n")
    (repo / "b.txt").write_text("bb\n")

    result = scripts.diff({"repo": str(repo), "files": ["b.txt"]}, roots)

    assert "b.txt" in result["stdout"]
    assert "a.txt" not in result["stdout"]


def test_diff_with_a_ref_and_files_compares_those_files(
    repo: Path, roots: list[str]
) -> None:
    """The diff action places the ref before '--' and the files after it."""
    (repo / "a.txt").write_text("two\n")

    result = scripts.diff({"repo": str(repo), "ref": "HEAD", "files": ["a.txt"]}, roots)

    assert result["exit_code"] == 0
    assert "-one\n+two\n" in result["stdout"]


def test_lfs_track_writes_the_pattern_to_gitattributes(
    repo: Path, roots: list[str]
) -> None:
    """lfs_track adds an LFS line for each pattern."""
    result = scripts.lfs_track({"repo": str(repo), "patterns": ["*.bin"]}, roots)

    assert result["exit_code"] == 0
    attributes = (repo / ".gitattributes").read_text()
    assert "*.bin filter=lfs diff=lfs merge=lfs -text" in attributes


def test_lfs_untrack_removes_the_pattern(repo: Path, roots: list[str]) -> None:
    """lfs_untrack drops the line lfs_track wrote."""
    scripts.lfs_track({"repo": str(repo), "patterns": ["*.bin"]}, roots)

    result = scripts.lfs_untrack({"repo": str(repo), "patterns": ["*.bin"]}, roots)

    assert result["exit_code"] == 0
    assert "*.bin" not in (repo / ".gitattributes").read_text()


def test_commit_records_the_staged_change_as_the_repos_identity(
    repo: Path, roots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit action sets author and committer from the repository's config.

    The process environment names another identity, which the config's
    values displace.
    """
    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(variable, "Intruder")
    (repo / "a.txt").write_text("two\n")
    _git(repo, "add", "a.txt")

    result = scripts.commit({"repo": str(repo), "message": "second"}, roots)

    assert result["exit_code"] == 0
    logged = _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s")
    assert logged == f"{NAME} <{EMAIL}>|{NAME} <{EMAIL}>|second\n"


def test_commit_refuses_without_user_name(repo: Path, roots: list[str]) -> None:
    """An unset user.name is refused before anything is committed."""
    _git(repo, "config", "--unset", "user.name")

    with pytest.raises(RefusalError, match=r"^git config user\.name is not set$"):
        scripts.commit({"repo": str(repo), "message": "m"}, roots)


def test_commit_refuses_an_empty_user_name(repo: Path, roots: list[str]) -> None:
    """An empty user.name counts as unset."""
    _git(repo, "config", "user.name", "")

    with pytest.raises(RefusalError, match=r"^git config user\.name is not set$"):
        scripts.commit({"repo": str(repo), "message": "m"}, roots)


def test_commit_refuses_without_user_email(repo: Path, roots: list[str]) -> None:
    """An unset user.email is refused once user.name is set."""
    _git(repo, "config", "--unset", "user.email")

    with pytest.raises(RefusalError, match=r"^git config user\.email is not set$"):
        scripts.commit({"repo": str(repo), "message": "m"}, roots)


def test_commit_refuses_with_nothing_staged(repo: Path, roots: list[str]) -> None:
    """A commit with an empty index is refused, and HEAD stays where it was."""
    head = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(
        RefusalError, match=r"^nothing staged; stage changes with git add$"
    ):
        scripts.commit({"repo": str(repo), "message": "m"}, roots)

    assert _git(repo, "rev-parse", "HEAD") == head


def test_commit_in_a_missing_directory_returns_gits_error(
    tmp_path: Path, roots: list[str]
) -> None:
    """When git cannot read the config, its own error is the result."""
    missing = tmp_path / "root" / "missing"

    result = scripts.commit({"repo": str(missing), "message": "m"}, roots)

    assert result["exit_code"] == 128
    assert result["stderr"].startswith("fatal: ")


def test_commit_outside_a_repository_returns_gits_error(
    tmp_path: Path, roots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an identity set but no repository, git's diff error is the result."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[user]\n\tname = {NAME}\n\temail = {EMAIL}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    plain = tmp_path / "root" / "plain"
    plain.mkdir()

    result = scripts.commit({"repo": str(plain), "message": "m"}, roots)

    assert result["exit_code"] not in (0, 1, 2)
    assert not result["stderr"].startswith("git plugin refused: ")


def test_run_script_runs_the_named_action_on_its_input(
    repo: Path, roots: list[str]
) -> None:
    """run_script decodes stdin, reads the roots, and runs the action."""
    (repo / "a.txt").write_text("two\n")
    stdin = json.dumps({"repo": str(repo)}).encode()
    environ = {"AGENT_PROXY_ROOTS": json.dumps(roots)}

    result = scripts.run_script("diff", stdin, environ)

    assert result["exit_code"] == 0
    assert "-one\n+two\n" in result["stdout"]


def test_run_script_returns_a_refusal_as_its_result(
    repo: Path, roots: list[str]
) -> None:
    """A refusal becomes exit code 2 and the refusal's stderr."""
    stdin = json.dumps({"repo": str(repo), "message": "m"}).encode()
    environ = {"AGENT_PROXY_ROOTS": json.dumps(roots)}

    result = scripts.run_script("commit", stdin, environ)

    assert result == {
        "exit_code": 2,
        "stdout": "",
        "stderr": "git plugin refused: nothing staged; stage changes with git add\n",
    }


def test_run_script_refuses_without_roots(repo: Path) -> None:
    """A cage with no AGENT_PROXY_ROOTS is refused before git runs."""
    stdin = json.dumps({"repo": str(repo)}).encode()

    result = scripts.run_script("diff", stdin, {})

    assert result == {
        "exit_code": 2,
        "stdout": "",
        "stderr": "git plugin refused: AGENT_PROXY_ROOTS is not set\n",
    }
