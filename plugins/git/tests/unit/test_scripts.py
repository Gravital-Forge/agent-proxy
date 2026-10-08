import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from git_plugin import scripts
from git_plugin.cage import RefusalError

NAME = "Fixture"
EMAIL = "fixture@example.invalid"
AGENT_NAME = "Agent One"
AGENT_EMAIL = "agent-01@example.com"
NO_NAME = (
    r"^git config user\.name is not set, nor is GIT_AUTHOR_NAME in the grant's env$"
)
NO_EMAIL = (
    r"^git config user\.email is not set, nor is GIT_AUTHOR_EMAIL in the grant's env$"
)


@pytest.fixture(autouse=True)
def _isolate_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host's git config and the grant-env variables away from every run."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for variable in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_BRANCH_PREFIXES"):
        monkeypatch.delenv(variable, raising=False)


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
        "push",
        "checkout",
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


def test_commit_takes_the_environments_identity_when_the_repo_has_none(
    repo: Path, roots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no identity in the config, author and committer are the environment's."""
    _git(repo, "config", "--unset", "user.name")
    _git(repo, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_AUTHOR_NAME", AGENT_NAME)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", AGENT_EMAIL)
    (repo / "a.txt").write_text("two\n")
    _git(repo, "add", "a.txt")

    result = scripts.commit({"repo": str(repo), "message": "second"}, roots)

    assert result["exit_code"] == 0
    logged = _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>")
    assert logged == f"{AGENT_NAME} <{AGENT_EMAIL}>|{AGENT_NAME} <{AGENT_EMAIL}>\n"


def test_commit_resolves_the_name_and_the_email_each_on_its_own(
    repo: Path, roots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository with only user.name keeps it and takes the environment's email."""
    _git(repo, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_AUTHOR_NAME", AGENT_NAME)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", AGENT_EMAIL)
    (repo / "a.txt").write_text("two\n")
    _git(repo, "add", "a.txt")

    result = scripts.commit({"repo": str(repo), "message": "second"}, roots)

    assert result["exit_code"] == 0
    logged = _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>")
    assert logged == f"{NAME} <{AGENT_EMAIL}>|{NAME} <{AGENT_EMAIL}>\n"


def test_commit_refuses_without_user_name(repo: Path, roots: list[str]) -> None:
    """An unset user.name, with no GIT_AUTHOR_NAME, is refused before a commit."""
    _git(repo, "config", "--unset", "user.name")

    with pytest.raises(RefusalError, match=NO_NAME):
        scripts.commit({"repo": str(repo), "message": "m"}, roots)


def test_commit_refuses_an_empty_user_name(repo: Path, roots: list[str]) -> None:
    """An empty user.name counts as unset."""
    _git(repo, "config", "user.name", "")

    with pytest.raises(RefusalError, match=NO_NAME):
        scripts.commit({"repo": str(repo), "message": "m"}, roots)


def test_commit_refuses_an_empty_git_author_name(
    repo: Path, roots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty GIT_AUTHOR_NAME counts as unset."""
    _git(repo, "config", "--unset", "user.name")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")

    with pytest.raises(RefusalError, match=NO_NAME):
        scripts.commit({"repo": str(repo), "message": "m"}, roots)


def test_commit_refuses_without_user_email(repo: Path, roots: list[str]) -> None:
    """An unset user.email, with no GIT_AUTHOR_EMAIL, is refused once a name is set."""
    _git(repo, "config", "--unset", "user.email")

    with pytest.raises(RefusalError, match=NO_EMAIL):
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


PROTECTED_MAIN = "Cannot push to protected branch: main"
NO_PREFIXES = (
    "GIT_BRANCH_PREFIXES is not set or names no prefix; the grant's env sets it"
)


def _prefix_refusal(branch: str) -> str:
    """Return the pattern of the refusal for a branch outside the prefix."""
    return rf"^Branch '{branch}' does not match any allowed prefix: task/$"


@pytest.fixture
def origin(repo: Path) -> Path:
    """A bare repository beside repo, which repo names as its origin."""
    origin = repo.parent / "origin.git"
    _git(repo.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    return origin


@pytest.fixture
def _task_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow task/ as the one branch prefix, as the grant's env sets it."""
    monkeypatch.setenv("GIT_BRANCH_PREFIXES", "task/")


def _commit_on_branch(repo: Path, branch: str) -> str:
    """Point branch at repo's HEAD, commit a change on it, and return its hash."""
    _git(repo, "checkout", "-q", "-B", branch)
    (repo / "a.txt").write_text(f"{branch}\n")
    _git(repo, "commit", "-q", "-am", f"on {branch}")
    return _git(repo, "rev-parse", "HEAD")


def _commit_lfs_file(repo: Path) -> bytes:
    """Commit big.bin through git LFS on task/l, and return its content.

    LFS's pre-push hook is removed, so only the push action's own LFS push
    can upload the object.
    """
    content = os.urandom(2048)
    _git(repo, "lfs", "install", "--local")
    _git(repo, "checkout", "-q", "-b", "task/l")
    (repo / "big.bin").write_bytes(content)
    _git(repo, "lfs", "track", "big.bin")
    _git(repo, "add", ".gitattributes", "big.bin")
    _git(repo, "commit", "-q", "-m", "big")
    (repo / ".git" / "hooks" / "pre-push").unlink()
    return content


def _head(repo: Path) -> str:
    """Return the name of repo's checked-out branch, with git's newline."""
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


@pytest.mark.usefixtures("_task_prefix")
def test_push_sends_a_task_branch_to_origin(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """The push action on task/p puts origin's task/p at the repository's commit."""
    pushed = _commit_on_branch(repo, "task/p")

    result = scripts.push({"repo": str(repo)}, roots)

    assert result["exit_code"] == 0, result
    assert _git(origin, "rev-parse", "refs/heads/task/p") == pushed


@pytest.mark.usefixtures("_task_prefix")
def test_push_sets_the_branchs_upstream(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """The push action tracks the pushed branch, as git push -u does."""
    _commit_on_branch(repo, "task/p")

    scripts.push({"repo": str(repo)}, roots)

    assert _git(repo, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/task/p\n"


@pytest.mark.usefixtures("_task_prefix")
@pytest.mark.parametrize("branch", ["main", "master"])
def test_push_refuses_a_protected_branch(
    repo: Path, roots: list[str], origin: Path, branch: str
) -> None:
    """The push action on a protected branch is refused, and origin gets no ref."""
    _commit_on_branch(repo, branch)

    with pytest.raises(
        RefusalError, match=rf"^Cannot push to protected branch: {branch}$"
    ):
        scripts.push({"repo": str(repo)}, roots)

    assert _git(origin, "for-each-ref") == ""


@pytest.mark.usefixtures("_task_prefix")
def test_push_refuses_a_branch_without_the_prefix(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """The push action on feature/x is refused, and origin gets no ref."""
    _commit_on_branch(repo, "feature/x")

    with pytest.raises(RefusalError, match=_prefix_refusal("feature/x")):
        scripts.push({"repo": str(repo)}, roots)

    assert _git(origin, "for-each-ref") == ""


@pytest.mark.usefixtures("_task_prefix")
def test_push_refuses_a_detached_head(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """A detached HEAD reads as the branch HEAD, which lacks the prefix."""
    _commit_on_branch(repo, "task/d")
    _git(repo, "checkout", "-q", "--detach")

    with pytest.raises(RefusalError, match=_prefix_refusal("HEAD")):
        scripts.push({"repo": str(repo)}, roots)

    assert _git(origin, "for-each-ref") == ""


@pytest.mark.usefixtures("_task_prefix")
def test_push_does_not_force_through_a_planted_push_refspec(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """A '+' refspec in remote.origin.push does not force an amended push."""
    pushed = _commit_on_branch(repo, "task/p")
    scripts.push({"repo": str(repo)}, roots)
    _git(repo, "commit", "-q", "--amend", "-m", "amended")
    _git(repo, "config", "remote.origin.push", "+refs/heads/*:refs/heads/*")

    result = scripts.push({"repo": str(repo)}, roots)

    assert result["exit_code"] != 0
    assert "non-fast-forward" in result["stderr"]
    assert _git(origin, "rev-parse", "refs/heads/task/p") == pushed


@pytest.mark.usefixtures("_task_prefix")
def test_push_uploads_lfs_objects_without_the_pre_push_hook(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """The push action uploads the branch's LFS object to origin before the ref."""
    content = _commit_lfs_file(repo)
    oid = hashlib.sha256(content).hexdigest()

    result = scripts.push({"repo": str(repo)}, roots)

    assert result["exit_code"] == 0, result
    stored = origin / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    assert stored.read_bytes() == content


@pytest.mark.usefixtures("_task_prefix")
def test_push_returns_a_failed_lfs_push_and_pushes_no_branch(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """With the local LFS object gone, the LFS push's failure is the result."""
    _commit_lfs_file(repo)
    shutil.rmtree(repo / ".git" / "lfs" / "objects")
    # git-lfs rebuilds a missing object from a working-tree file that matches it.
    (repo / "big.bin").unlink()

    result = scripts.push({"repo": str(repo)}, roots)

    assert result["exit_code"] != 0
    assert "(missing) big.bin" in result["stdout"]
    assert _git(origin, "for-each-ref") == ""


@pytest.mark.usefixtures("_task_prefix")
def test_push_in_a_missing_directory_returns_gits_error(
    tmp_path: Path, roots: list[str]
) -> None:
    """When git cannot read HEAD, its own error is the result."""
    missing = tmp_path / "root" / "missing"

    result = scripts.push({"repo": str(missing)}, roots)

    assert result["exit_code"] == 128
    assert result["stderr"].startswith("fatal: ")


def test_push_sends_a_branch_under_a_second_prefix(
    repo: Path, roots: list[str], origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With task/ and dependabot/ allowed, a push of dependabot/x reaches origin."""
    monkeypatch.setenv("GIT_BRANCH_PREFIXES", "task/,dependabot/")
    pushed = _commit_on_branch(repo, "dependabot/x")

    result = scripts.push({"repo": str(repo)}, roots)

    assert result["exit_code"] == 0, result
    assert _git(origin, "rev-parse", "refs/heads/dependabot/x") == pushed


def test_push_refuses_without_branch_prefixes(
    repo: Path, roots: list[str], origin: Path
) -> None:
    """With GIT_BRANCH_PREFIXES unset, a push of task/p is refused."""
    _commit_on_branch(repo, "task/p")

    with pytest.raises(RefusalError, match=f"^{NO_PREFIXES}$"):
        scripts.push({"repo": str(repo)}, roots)

    assert _git(origin, "for-each-ref") == ""


def test_push_refuses_without_branch_prefixes_before_git_runs(
    tmp_path: Path, roots: list[str]
) -> None:
    """With GIT_BRANCH_PREFIXES unset, the refusal comes before git reads HEAD."""
    missing = tmp_path / "root" / "missing"

    with pytest.raises(RefusalError, match=f"^{NO_PREFIXES}$"):
        scripts.push({"repo": str(missing)}, roots)


@pytest.mark.usefixtures("_task_prefix")
def test_checkout_switches_to_a_protected_branch(repo: Path, roots: list[str]) -> None:
    """Checking out main from a task branch leaves the repository on main."""
    _git(repo, "checkout", "-q", "-b", "task/x")

    result = scripts.checkout({"repo": str(repo), "target": "main"}, roots)

    assert result["exit_code"] == 0, result
    assert _head(repo) == "main\n"


@pytest.mark.usefixtures("_task_prefix")
def test_checkout_switches_to_a_task_branch(repo: Path, roots: list[str]) -> None:
    """Checking out an existing task/x leaves the repository on it."""
    _git(repo, "branch", "task/x")

    result = scripts.checkout({"repo": str(repo), "target": "task/x"}, roots)

    assert result["exit_code"] == 0, result
    assert _head(repo) == "task/x\n"


@pytest.mark.usefixtures("_task_prefix")
def test_checkout_with_create_makes_a_task_branch(repo: Path, roots: list[str]) -> None:
    """The checkout action with create makes task/c at HEAD and switches to it."""
    arguments = {"repo": str(repo), "target": "task/c", "create": True}

    result = scripts.checkout(arguments, roots)

    assert result["exit_code"] == 0, result
    assert _head(repo) == "task/c\n"


@pytest.mark.usefixtures("_task_prefix")
@pytest.mark.parametrize("create", [False, True])
def test_checkout_refuses_a_target_without_the_prefix(
    repo: Path, roots: list[str], create: bool
) -> None:
    """Checking out feature/x is refused, with or without create."""
    _git(repo, "branch", "feature/x")
    arguments = {"repo": str(repo), "target": "feature/x", "create": create}

    with pytest.raises(RefusalError, match=_prefix_refusal("feature/x")):
        scripts.checkout(arguments, roots)

    assert _head(repo) == "main\n"


@pytest.mark.usefixtures("_task_prefix")
def test_checkout_of_a_missing_branch_returns_gits_error(
    repo: Path, roots: list[str]
) -> None:
    """Checking out a task branch that does not exist returns git's failure."""
    result = scripts.checkout({"repo": str(repo), "target": "task/none"}, roots)

    assert result["exit_code"] == 1
    assert "task/none" in result["stderr"]


def test_checkout_with_create_makes_a_branch_under_a_second_prefix(
    repo: Path, roots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With task/ and dependabot/ allowed, create makes dependabot/x and switches."""
    monkeypatch.setenv("GIT_BRANCH_PREFIXES", "task/, dependabot/")
    arguments = {"repo": str(repo), "target": "dependabot/x", "create": True}

    result = scripts.checkout(arguments, roots)

    assert result["exit_code"] == 0, result
    assert _head(repo) == "dependabot/x\n"


@pytest.mark.parametrize("target", ["main", "task/x"])
def test_checkout_refuses_without_branch_prefixes(
    repo: Path, roots: list[str], target: str
) -> None:
    """With GIT_BRANCH_PREFIXES unset, checking out any target, main too, is refused."""
    _git(repo, "branch", "task/x")
    _git(repo, "checkout", "-q", "-b", "task/y")

    with pytest.raises(RefusalError, match=f"^{NO_PREFIXES}$"):
        scripts.checkout({"repo": str(repo), "target": target}, roots)

    assert _head(repo) == "task/y\n"


@pytest.mark.usefixtures("_task_prefix")
def test_run_script_returns_a_push_refusal_as_its_result(
    repo: Path, roots: list[str]
) -> None:
    """A push on main comes back as exit code 2 and the rule's reason."""
    stdin = json.dumps({"repo": str(repo)}).encode()
    environ = {"AGENT_PROXY_ROOTS": json.dumps(roots)}

    result = scripts.run_script("push", stdin, environ)

    assert result == {
        "exit_code": 2,
        "stdout": "",
        "stderr": f"git plugin refused: {PROTECTED_MAIN}\n",
    }


def test_run_script_returns_a_missing_prefixes_refusal_as_its_result(
    repo: Path, roots: list[str]
) -> None:
    """A checkout of main with GIT_BRANCH_PREFIXES unset comes back as exit code 2."""
    stdin = json.dumps({"repo": str(repo), "target": "main"}).encode()
    environ = {"AGENT_PROXY_ROOTS": json.dumps(roots)}

    result = scripts.run_script("checkout", stdin, environ)

    assert result == {
        "exit_code": 2,
        "stdout": "",
        "stderr": f"git plugin refused: {NO_PREFIXES}\n",
    }
