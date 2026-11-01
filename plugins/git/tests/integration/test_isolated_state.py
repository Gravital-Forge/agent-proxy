"""Under isolation the cage's git reads and writes the repository's real state."""

import hashlib
import os
import re
from pathlib import Path

import pytest
from harness import RunAction, RunInImage, host_git, isolated_git, stdout_of

TASK_PREFIX = {"GIT_BRANCH_PREFIXES": "task/"}
LFS_POINTER_HEADER = "version https://git-lfs.github.com/spec/v1"
# extensions.refStorage = files://<path> arrived in git 2.54.0.
GIT_FLOOR = (2, 54, 0)
GIT_VERSION = re.compile(r"git version (\d+)\.(\d+)\.(\d+)\b.*\n")


@pytest.fixture(params=["main", "linked"])
def host_checkout(request: pytest.FixtureRequest, host_repo: Path) -> tuple[Path, str]:
    """host_repo on main, or a linked worktree of it on task/wt, and that branch."""
    if request.param == "main":
        return host_repo, "main"
    worktree = host_repo.parent / "wt"
    host_git(host_repo, "worktree", "add", "-q", "-b", "task/wt", str(worktree))
    return worktree, "task/wt"


def _version(output: str) -> tuple[int, ...]:
    """Return the numbers of a git version line."""
    match = GIT_VERSION.fullmatch(output)
    assert match is not None, output
    return tuple(int(number) for number in match.groups())


def test_refs_round_trip_through_the_files_ref_store_on_the_images_git(
    run_in_plugin_image: RunInImage, host_repo: Path
) -> None:
    """The image's git is at least 2.54.0, and its isolated refs are the real ones.

    The scratch config names the real ref store as files://<.git>, a form git
    takes from 2.54.0 on; a git that drops or changes it fails here. A ref
    the isolated git writes lands, with its reflog, in the real .git, and one
    the host writes there resolves for it.
    """
    root = host_repo.parent
    dot_git = host_repo / ".git"
    version = run_in_plugin_image(root, ["git", "version"])
    assert _version(version) >= GIT_FLOOR, version
    host_git(host_repo, "branch", "task/host", "HEAD~1")

    run_in_plugin_image(
        root, isolated_git(host_repo, "update-ref", "refs/heads/task/cage", "HEAD")
    )
    resolved = run_in_plugin_image(
        root, isolated_git(host_repo, "rev-parse", "task/host")
    )

    assert (dot_git / "refs" / "heads" / "task" / "cage").read_text() == host_git(
        host_repo, "rev-parse", "HEAD"
    )
    assert (dot_git / "logs" / "refs" / "heads" / "task" / "cage").is_file()
    assert resolved == host_git(host_repo, "rev-parse", "task/host")


def test_an_isolated_branch_delete_removes_a_packed_ref(
    run_in_plugin_image: RunInImage, host_repo: Path
) -> None:
    """A branch whose ref lies only in packed-refs is deleted from the real store."""
    packed = host_repo / ".git" / "packed-refs"
    host_git(host_repo, "branch", "task/old")
    host_git(host_repo, "pack-refs", "--all")
    assert "refs/heads/task/old" in packed.read_text()

    run_in_plugin_image(
        host_repo.parent, isolated_git(host_repo, "branch", "-D", "task/old")
    )

    assert "refs/heads/task/old" not in packed.read_text()
    assert host_git(host_repo, "branch", "--list") == "* main\n"


def test_a_conflicted_merge_resolved_in_later_cages_makes_a_two_parent_commit(
    run_action: RunAction, host_repo: Path
) -> None:
    """The merge cage leaves MERGE_HEAD in the real .git, for the commit cage to use."""
    repo, roots = str(host_repo), [host_repo.parent]
    host_git(host_repo, "checkout", "-q", "-b", "task/c")
    (host_repo / "a.txt").write_text("task\n")
    host_git(host_repo, "commit", "-q", "-am", "on task/c")
    host_git(host_repo, "checkout", "-q", "main")
    (host_repo / "a.txt").write_text("main\n")
    host_git(host_repo, "commit", "-q", "-am", "on main")
    parents = [
        host_git(host_repo, "rev-parse", ref).strip() for ref in ("main", "task/c")
    ]

    merged = run_action("merge", {"repo": repo, "branch": "task/c"}, roots)
    assert merged.content["exit_code"] == 1, merged.content
    assert "CONFLICT" in str(merged.content["stdout"])
    (host_repo / "a.txt").write_text("resolved\n")
    stdout_of(run_action("add", {"repo": repo, "files": ["a.txt"]}, roots))
    stdout_of(run_action("commit", {"repo": repo, "message": "merge task/c"}, roots))

    assert host_git(host_repo, "log", "-1", "--format=%P").split() == parents
    assert not (host_repo / ".git" / "MERGE_HEAD").exists()


def test_lfs_track_add_and_checkout_keep_the_object_in_the_real_store(
    run_action: RunAction, host_repo: Path, host_checkout: tuple[Path, str]
) -> None:
    """The clean filter stores big.bin's object in the real .git; checkout smudges."""
    checkout, branch = host_checkout
    content = os.urandom(2048)
    oid = hashlib.sha256(content).hexdigest()

    def run(name: str, **arguments: object) -> str:
        full = {"repo": str(checkout), **arguments}
        outcome = run_action(name, full, [host_repo.parent], env=TASK_PREFIX)
        return stdout_of(outcome)

    run("checkout", target="task/l", create=True)
    run("lfs_track", patterns=["*.bin"])
    (checkout / "big.bin").write_bytes(content)
    run("add", files=[".gitattributes", "big.bin"])
    run("commit", message="big")
    run("checkout", target=branch)
    assert not (checkout / "big.bin").exists()
    run("checkout", target="task/l")

    stored = host_repo / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    assert stored.read_bytes() == content
    assert (checkout / "big.bin").read_bytes() == content
    assert run("show", ref="HEAD:big.bin").startswith(LFS_POINTER_HEADER)
