from pathlib import Path

from agent_proxy.runner import Outcome
from harness import FIXTURE_EMAIL, FIXTURE_NAME, OtherUidRepo, RunAction, host_git


def _stdout(outcome: Outcome) -> str:
    """Return the stdout of an action that exited 0; fail the test otherwise."""
    assert outcome.content["exit_code"] == 0, outcome.content
    stdout = outcome.content["stdout"]
    assert isinstance(stdout, str)
    return stdout


def test_status_reports_the_branch(run_action: RunAction, host_repo: Path) -> None:
    """The status action runs git status on the repository."""
    outcome = run_action("status", {"repo": str(host_repo)}, [host_repo.parent])

    assert "On branch main" in _stdout(outcome)


def test_log_prints_the_history(run_action: RunAction, host_repo: Path) -> None:
    """The log action with no parameter prints both commits."""
    outcome = run_action("log", {"repo": str(host_repo)}, [host_repo.parent])

    stdout = _stdout(outcome)
    assert "    second\n" in stdout
    assert "    first\n" in stdout


def test_log_with_max_count_one_prints_one_line(
    run_action: RunAction, host_repo: Path
) -> None:
    """A given max_count follows the fixed --max-count=20, and git takes it."""
    outcome = run_action(
        "log",
        {"repo": str(host_repo), "max_count": 1, "oneline": True},
        [host_repo.parent],
    )

    lines = _stdout(outcome).splitlines()
    assert len(lines) == 1
    assert lines[0].endswith(" second")


def test_show_prints_head(run_action: RunAction, host_repo: Path) -> None:
    """The show action with no ref shows HEAD, the second commit."""
    outcome = run_action("show", {"repo": str(host_repo)}, [host_repo.parent])

    stdout = _stdout(outcome)
    assert "    second\n" in stdout
    assert "+two\n" in stdout


def test_branch_lists_main(run_action: RunAction, host_repo: Path) -> None:
    """The branch action with all lists the one branch."""
    outcome = run_action(
        "branch", {"repo": str(host_repo), "all": True}, [host_repo.parent]
    )

    assert _stdout(outcome) == "* main\n"


def test_rev_parse_prints_the_commit_hash(
    run_action: RunAction, host_repo: Path
) -> None:
    """rev_parse of HEAD prints the hash the host's git gives."""
    outcome = run_action(
        "rev_parse", {"repo": str(host_repo), "ref": "HEAD"}, [host_repo.parent]
    )

    assert _stdout(outcome) == host_git(host_repo, "rev-parse", "HEAD")


def test_ls_files_lists_the_tracked_file(
    run_action: RunAction, host_repo: Path
) -> None:
    """ls_files lists the one tracked file."""
    outcome = run_action("ls_files", {"repo": str(host_repo)}, [host_repo.parent])

    assert _stdout(outcome) == "a.txt\n"


def test_stash_list_prints_a_stash(run_action: RunAction, host_repo: Path) -> None:
    """stash_list prints a stash the host's git made."""
    (host_repo / "a.txt").write_text("three\n")
    host_git(host_repo, "stash", "-q")

    outcome = run_action("stash_list", {"repo": str(host_repo)}, [host_repo.parent])

    assert _stdout(outcome).startswith("stash@{0}: ")


def test_remote_names_origin(run_action: RunAction, host_repo: Path) -> None:
    """The remote action prints origin and its URL."""
    outcome = run_action("remote", {"repo": str(host_repo)}, [host_repo.parent])

    origin = host_repo.parent / "origin.git"
    assert f"origin\t{origin} (fetch)\n" in _stdout(outcome)


def test_blame_annotates_the_file(run_action: RunAction, host_repo: Path) -> None:
    """The blame action annotates each line with its commit's author."""
    outcome = run_action(
        "blame", {"repo": str(host_repo), "file": "a.txt"}, [host_repo.parent]
    )

    stdout = _stdout(outcome)
    assert FIXTURE_NAME in stdout
    assert stdout.endswith(") two\n")


def test_tag_lists_v1(run_action: RunAction, host_repo: Path) -> None:
    """The tag action lists the one tag."""
    outcome = run_action("tag", {"repo": str(host_repo)}, [host_repo.parent])

    assert _stdout(outcome) == "v1\n"


def test_config_get_prints_the_value(run_action: RunAction, host_repo: Path) -> None:
    """config_get prints a value from the repository's own config."""
    outcome = run_action(
        "config_get", {"repo": str(host_repo), "key": "user.email"}, [host_repo.parent]
    )

    assert _stdout(outcome) == f"{FIXTURE_EMAIL}\n"


def test_lfs_ls_files_exits_zero(run_action: RunAction, host_repo: Path) -> None:
    """lfs_ls_files runs on a repository that tracks no LFS file."""
    _stdout(run_action("lfs_ls_files", {"repo": str(host_repo)}, [host_repo.parent]))


def test_lfs_status_exits_zero(run_action: RunAction, host_repo: Path) -> None:
    """lfs_status runs on a repository that tracks no LFS file."""
    _stdout(run_action("lfs_status", {"repo": str(host_repo)}, [host_repo.parent]))


def test_status_runs_on_a_repository_another_uid_owns(
    run_action: RunAction, other_uid_repo: OtherUidRepo
) -> None:
    """The root's /* value trusts a repository beneath the root."""
    outcome = run_action(
        "status", {"repo": str(other_uid_repo.repo)}, [other_uid_repo.root]
    )

    assert "On branch main" in _stdout(outcome)


def test_status_runs_with_the_repository_as_its_own_root(
    run_action: RunAction, other_uid_repo: OtherUidRepo
) -> None:
    """The root's own value trusts a repository whose top level is the root."""
    outcome = run_action(
        "status", {"repo": str(other_uid_repo.repo)}, [other_uid_repo.repo]
    )

    assert "On branch main" in _stdout(outcome)


def test_status_runs_on_a_linked_worktree_beneath_the_root(
    run_action: RunAction, other_uid_repo: OtherUidRepo
) -> None:
    """The root's /* value matches a linked worktree by the worktree's path."""
    outcome = run_action(
        "status", {"repo": str(other_uid_repo.worktree)}, [other_uid_repo.root]
    )

    assert "On branch task/wt" in _stdout(outcome)


def test_status_outside_every_root_fails_with_gits_error(
    run_action: RunAction, host_repo: Path, tmp_path: Path
) -> None:
    """A repository outside every root does not exist in the cage, and git says so."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    outcome = run_action("status", {"repo": str(host_repo)}, [elsewhere])

    assert outcome.content["exit_code"] != 0
    stderr = outcome.content["stderr"]
    assert isinstance(stderr, str)
    assert "cannot change to" in stderr
