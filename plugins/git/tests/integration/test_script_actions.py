import os
from pathlib import Path

import pytest
from agent_proxy.runner import InputError
from harness import OtherUidRepo, RunAction, host_git, stdout_of

LFS_POINTER_HEADER = "version https://git-lfs.github.com/spec/v1"
NOTHING_STAGED = "git plugin refused: nothing staged; stage changes with git add\n"


def test_add_then_commit_makes_a_commit_that_log_shows(
    run_action: RunAction, host_repo: Path
) -> None:
    """The add action stages b.txt, commit records it, and log shows the commit."""
    repo = str(host_repo)
    roots = [host_repo.parent]
    (host_repo / "b.txt").write_text("new\n")

    stdout_of(run_action("add", {"repo": repo, "files": ["b.txt"]}, roots))
    stdout_of(run_action("commit", {"repo": repo, "message": "add b"}, roots))
    log = stdout_of(
        run_action("log", {"repo": repo, "max_count": 1, "oneline": True}, roots)
    )

    assert log.endswith(" add b\n")


def test_add_then_commit_on_a_repository_another_uid_owns(
    run_action: RunAction, other_uid_repo: OtherUidRepo
) -> None:
    """The add and commit actions exit 0 on a repository the cage does not own."""
    repo = str(other_uid_repo.repo)
    roots = [other_uid_repo.root]
    (other_uid_repo.repo / "b.txt").write_text("new\n")

    stdout_of(run_action("add", {"repo": repo, "files": ["b.txt"]}, roots))
    stdout_of(run_action("commit", {"repo": repo, "message": "add b"}, roots))


def test_rm_removes_a_tracked_file_from_the_index(
    run_action: RunAction, host_repo: Path
) -> None:
    """The rm action of a.txt leaves the index with no file."""
    arguments = {"repo": str(host_repo), "files": ["a.txt"]}

    stdout_of(run_action("rm", arguments, [host_repo.parent]))

    assert host_git(host_repo, "ls-files") == ""


def test_restore_with_staged_unstages_a_staged_change(
    run_action: RunAction, host_repo: Path
) -> None:
    """The restore action with staged empties the index change and keeps the edit."""
    (host_repo / "a.txt").write_text("three\n")
    host_git(host_repo, "add", "a.txt")
    arguments = {"repo": str(host_repo), "files": ["a.txt"], "staged": True}

    stdout_of(run_action("restore", arguments, [host_repo.parent]))

    assert host_git(host_repo, "diff", "--cached", "--name-only") == ""
    assert (host_repo / "a.txt").read_text() == "three\n"


def test_diff_shows_a_working_tree_change(
    run_action: RunAction, host_repo: Path
) -> None:
    """The diff action shows the edit the working tree holds."""
    (host_repo / "a.txt").write_text("three\n")

    diff = stdout_of(run_action("diff", {"repo": str(host_repo)}, [host_repo.parent]))

    assert "-two\n+three\n" in diff


def test_diff_with_staged_shows_a_staged_change(
    run_action: RunAction, host_repo: Path
) -> None:
    """The diff action with staged shows the index's change, not the later edit."""
    (host_repo / "a.txt").write_text("three\n")
    host_git(host_repo, "add", "a.txt")
    (host_repo / "a.txt").write_text("four\n")
    arguments = {"repo": str(host_repo), "staged": True}

    diff = stdout_of(run_action("diff", arguments, [host_repo.parent]))

    assert "-two\n+three\n" in diff
    assert "+four" not in diff


def test_commit_with_nothing_staged_returns_the_refusal(
    run_action: RunAction, host_repo: Path
) -> None:
    """The commit action with an empty index returns the refusal as its result."""
    arguments = {"repo": str(host_repo), "message": "empty"}

    outcome = run_action("commit", arguments, [host_repo.parent])

    assert outcome.content == {"exit_code": 2, "stdout": "", "stderr": NOTHING_STAGED}


def test_lfs_track_add_and_commit_store_a_pointer(
    run_action: RunAction, host_repo: Path
) -> None:
    """A committed file lfs_track names is an LFS pointer lfs_ls_files lists."""
    repo = str(host_repo)
    roots = [host_repo.parent]
    (host_repo / "big.bin").write_bytes(os.urandom(2048))

    stdout_of(run_action("lfs_track", {"repo": repo, "patterns": ["*.bin"]}, roots))
    files = [".gitattributes", "big.bin"]
    stdout_of(run_action("add", {"repo": repo, "files": files}, roots))
    stdout_of(run_action("commit", {"repo": repo, "message": "big"}, roots))
    shown = stdout_of(run_action("show", {"repo": repo, "ref": "HEAD:big.bin"}, roots))
    listed = stdout_of(run_action("lfs_ls_files", {"repo": repo}, roots))

    assert shown.startswith(LFS_POINTER_HEADER)
    assert "big.bin" in listed


def test_lfs_untrack_removes_the_pattern_from_gitattributes(
    run_action: RunAction, host_repo: Path
) -> None:
    """lfs_untrack drops the line lfs_track wrote."""
    arguments = {"repo": str(host_repo), "patterns": ["*.bin"]}
    roots = [host_repo.parent]

    stdout_of(run_action("lfs_track", arguments, roots))
    stdout_of(run_action("lfs_untrack", arguments, roots))

    assert "*.bin" not in (host_repo / ".gitattributes").read_text()


def test_diff_past_the_cap_returns_the_command_shape_with_a_file(
    run_action: RunAction, host_repo: Path
) -> None:
    """A diff whose JSON passes 256 KiB comes back as the script's stream on file."""
    lines = "".join(f"{number:099d}\n" for number in range(3000))
    (host_repo / "a.txt").write_text(lines)

    outcome = run_action("diff", {"repo": str(host_repo)}, [host_repo.parent])

    assert set(outcome.content) == {
        "exit_code",
        "stdout_file",
        "stdout_bytes",
        "stderr",
        "truncated",
    }
    assert (outcome.content["exit_code"], outcome.content["truncated"]) == (0, True)


def test_add_of_a_dot_dot_file_is_refused_before_a_cage(
    run_action: RunAction, host_repo: Path
) -> None:
    """The input schema refuses '../x', so the runner raises before any cage."""
    arguments = {"repo": str(host_repo), "files": ["../x"]}

    with pytest.raises(InputError):
        run_action("add", arguments, [host_repo.parent])
