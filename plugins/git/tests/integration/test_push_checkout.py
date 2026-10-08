import hashlib
import os
import shlex
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from agent_proxy.runner import InputError, Outcome
from harness import OtherUidRepo, RemotePair, RunAction, host_git, plant_hook, stdout_of

PREFIX_REFUSAL = (
    "git plugin refused: Branch '{}' does not match any allowed prefix: task/\n"
)
PROTECTED_REFUSAL = "git plugin refused: Cannot push to protected branch: {}\n"
NO_PREFIXES_REFUSAL = (
    "git plugin refused: "
    "GIT_BRANCH_PREFIXES is not set or names no prefix; the grant's env sets it\n"
)
TASK_PREFIX = {"GIT_BRANCH_PREFIXES": "task/"}
EXECUTABLE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def _commit_on_branch(repo: Path, branch: str) -> str:
    """Point branch at repo's HEAD, commit a change on it, and return its hash."""
    host_git(repo, "checkout", "-q", "-B", branch)
    (repo / "a.txt").write_text(f"{branch}\n")
    host_git(repo, "commit", "-q", "-am", f"on {branch}")
    return host_git(repo, "rev-parse", "HEAD")


def _refusal(stderr: str) -> dict[str, object]:
    """Return the result of a refused script, its reason on stderr."""
    return {"exit_code": 2, "stdout": "", "stderr": stderr}


def _push(run_action: RunAction, pair: RemotePair) -> Outcome:
    """Run the push action in pair's work, with pair's root and task/ granted."""
    return run_action("push", {"repo": str(pair.work)}, [pair.root], env=TASK_PREFIX)


def _checkout(
    run_action: RunAction, pair: RemotePair, arguments: dict[str, object]
) -> Outcome:
    """Run the checkout action in pair's work, with pair's root and task/ granted."""
    return run_action(
        "checkout", {"repo": str(pair.work), **arguments}, [pair.root], env=TASK_PREFIX
    )


def _head(repo: Path) -> str:
    """Return the name of repo's checked-out branch, with git's newline."""
    return host_git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def test_push_sends_a_task_branch_to_origin(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """The push action on task/p puts the remote's task/p at work's commit."""
    pushed = _commit_on_branch(remote_pair.work, "task/p")

    stdout_of(_push(run_action, remote_pair))

    assert host_git(remote_pair.remote, "rev-parse", "refs/heads/task/p") == pushed


def test_push_runs_no_pre_push_hook(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """A push of task/h succeeds past a planted pre-push hook, which never runs."""
    pushed = _commit_on_branch(remote_pair.work, "task/h")
    mark = remote_pair.root / "hook-ran"
    plant_hook(remote_pair.work, "pre-push", mark)

    stdout_of(_push(run_action, remote_pair))

    assert host_git(remote_pair.remote, "rev-parse", "refs/heads/task/h") == pushed
    assert not mark.exists()


@pytest.mark.parametrize(
    ("branch", "stderr"),
    [
        ("main", PROTECTED_REFUSAL.format("main")),
        ("master", PROTECTED_REFUSAL.format("master")),
        ("feature/x", PREFIX_REFUSAL.format("feature/x")),
    ],
)
def test_push_refuses_a_branch_outside_the_rules(
    run_action: RunAction, remote_pair: RemotePair, branch: str, stderr: str
) -> None:
    """A push on a protected or unprefixed branch is refused; no remote ref moves."""
    _commit_on_branch(remote_pair.work, branch)
    refs = host_git(remote_pair.remote, "for-each-ref")

    outcome = _push(run_action, remote_pair)

    assert outcome.content == _refusal(stderr)
    assert host_git(remote_pair.remote, "for-each-ref") == refs


def test_push_refuses_a_detached_head(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """A push on a detached HEAD reads the branch as HEAD, which lacks the prefix."""
    _commit_on_branch(remote_pair.work, "task/d")
    host_git(remote_pair.work, "checkout", "-q", "--detach")
    refs = host_git(remote_pair.remote, "for-each-ref")

    outcome = _push(run_action, remote_pair)

    assert outcome.content == _refusal(PREFIX_REFUSAL.format("HEAD"))
    assert host_git(remote_pair.remote, "for-each-ref") == refs


def test_push_without_branch_prefixes_is_refused(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """A push with no GIT_BRANCH_PREFIXES in the grant's env is refused, naming it."""
    _commit_on_branch(remote_pair.work, "task/p")
    refs = host_git(remote_pair.remote, "for-each-ref")

    outcome = run_action("push", {"repo": str(remote_pair.work)}, [remote_pair.root])

    assert outcome.content == _refusal(NO_PREFIXES_REFUSAL)
    assert host_git(remote_pair.remote, "for-each-ref") == refs


def test_push_with_force_is_refused_before_a_cage(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """The input schema has no force member, so the runner raises before any cage."""
    arguments = {"repo": str(remote_pair.work), "force": True}

    with pytest.raises(InputError):
        run_action("push", arguments, [remote_pair.root], env=TASK_PREFIX)


def test_push_does_not_force_through_a_planted_push_refspec(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """A '+' refspec in remote.origin.push does not force an amended push."""
    work = remote_pair.work
    pushed = _commit_on_branch(work, "task/p")
    stdout_of(_push(run_action, remote_pair))
    host_git(work, "commit", "-q", "--amend", "-m", "amended")
    host_git(work, "config", "remote.origin.push", "+refs/heads/*:refs/heads/*")

    outcome = _push(run_action, remote_pair)

    assert outcome.content["exit_code"] != 0
    assert "non-fast-forward" in str(outcome.content["stderr"])
    assert host_git(remote_pair.remote, "rev-parse", "refs/heads/task/p") == pushed


def test_push_fails_with_a_planted_mirror(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """remote.origin.mirror makes the explicit refspec fail, and no remote ref moves."""
    _commit_on_branch(remote_pair.work, "task/p")
    host_git(remote_pair.work, "config", "remote.origin.mirror", "true")
    refs = host_git(remote_pair.remote, "for-each-ref")

    outcome = _push(run_action, remote_pair)

    assert outcome.content["exit_code"] != 0
    assert host_git(remote_pair.remote, "for-each-ref") == refs


def test_push_uploads_a_new_lfs_object(
    run_action: RunAction, lfs_remote_pair: RemotePair
) -> None:
    """A new LFS file committed on task/l lands its object in the remote's store."""
    work = lfs_remote_pair.work
    content = os.urandom(2048)
    oid = hashlib.sha256(content).hexdigest()
    host_git(work, "checkout", "-q", "-b", "task/l")
    (work / "new.bin").write_bytes(content)
    host_git(work, "lfs", "track", "new.bin")
    host_git(work, "add", ".gitattributes", "new.bin")
    host_git(work, "commit", "-q", "-m", "new")

    stdout_of(_push(run_action, lfs_remote_pair))

    stored = lfs_remote_pair.remote / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    assert stored.read_bytes() == content


def test_checkout_switches_to_main(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """Checking out main from a task branch leaves work on main."""
    host_git(remote_pair.work, "checkout", "-q", "-b", "task/x")

    stdout_of(_checkout(run_action, remote_pair, {"target": "main"}))

    assert _head(remote_pair.work) == "main\n"


def test_checkout_with_create_makes_a_task_branch(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """The checkout action with create makes task/c and leaves work on it."""
    arguments = {"target": "task/c", "create": True}

    stdout_of(_checkout(run_action, remote_pair, arguments))

    assert _head(remote_pair.work) == "task/c\n"


def test_checkout_refuses_a_target_without_the_prefix(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """Checking out an existing feature/x is refused, and work stays on main."""
    host_git(remote_pair.work, "branch", "feature/x")

    outcome = _checkout(run_action, remote_pair, {"target": "feature/x"})

    assert outcome.content == _refusal(PREFIX_REFUSAL.format("feature/x"))
    assert _head(remote_pair.work) == "main\n"


def test_checkout_with_create_makes_a_branch_under_a_second_prefix(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """With task/ and dependabot/ granted, create makes dependabot/x and switches."""
    arguments = {
        "repo": str(remote_pair.work),
        "target": "dependabot/x",
        "create": True,
    }
    prefixes = {"GIT_BRANCH_PREFIXES": "task/,dependabot/"}

    stdout_of(run_action("checkout", arguments, [remote_pair.root], env=prefixes))

    assert _head(remote_pair.work) == "dependabot/x\n"


def test_checkout_changes_the_executable_bit_in_a_repository_another_uid_owns(
    run_action: RunAction,
    other_uid_repo: OtherUidRepo,
    run_as_other_uid: Callable[[str], None],
) -> None:
    """Checking out across a mode change sets run.sh's executable bit, then clears it.

    uid 1000 owns the repository and committed run.sh; after the cage's
    checkout, uid 1000 can still append to it.
    """
    repo = str(other_uid_repo.repo)
    roots = [other_uid_repo.root]
    run_sh = other_uid_repo.repo / "run.sh"
    run_as_other_uid(
        " && ".join(
            [
                f"cd {shlex.quote(repo)}",
                "printf 'echo hi\\n' > run.sh",
                "chmod 644 run.sh",
                "git add run.sh",
                "git commit -q -m 'run.sh 644'",
                "git checkout -q -b task/exec-bit",
                "chmod 755 run.sh",
                "git add run.sh",
                "git commit -q -m 'run.sh 755'",
                "git checkout -q main",
            ]
        )
    )

    to_task = {"repo": repo, "target": "task/exec-bit"}
    stdout_of(run_action("checkout", to_task, roots, env=TASK_PREFIX))
    assert run_sh.stat().st_mode & stat.S_IXUSR

    run_as_other_uid(f"echo more >> {shlex.quote(str(run_sh))}")
    assert run_sh.read_text() == "echo hi\nmore\n"
    run_sh.write_text("echo hi\n")

    to_main = {"repo": repo, "target": "main"}
    stdout_of(run_action("checkout", to_main, roots, env=TASK_PREFIX))
    assert not run_sh.stat().st_mode & EXECUTABLE_BITS
