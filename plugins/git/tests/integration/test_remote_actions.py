import hashlib

import pytest
from agent_proxy.config import EnvMount
from docker.models.containers import Container
from harness import (
    AGENT_EMAIL,
    AGENT_NAME,
    FIXTURE_EMAIL,
    FIXTURE_NAME,
    RemotePair,
    RunAction,
    host_git,
    stdout_of,
)

AUTH_URL = "http://auth:8080/repo.git"


def _push_from_peer(pair: RemotePair) -> str:
    """Commit b.txt in peer, push it to the remote's main, and return its hash."""
    (pair.peer / "b.txt").write_text("from peer\n")
    host_git(pair.peer, "add", "b.txt")
    host_git(pair.peer, "commit", "-q", "-m", "from peer")
    host_git(pair.peer, "push", "-q", "origin", "main")
    return host_git(pair.peer, "rev-parse", "HEAD")


def test_fetch_moves_origin_main(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """The fetch action in work brings the commit peer pushed to origin/main."""
    pushed = _push_from_peer(remote_pair)

    stdout_of(run_action("fetch", {"repo": str(remote_pair.work)}, [remote_pair.root]))

    assert host_git(remote_pair.work, "rev-parse", "origin/main") == pushed


def test_pull_fast_forwards_to_the_peers_commit(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """The pull action in work moves main to the commit peer pushed."""
    pushed = _push_from_peer(remote_pair)

    stdout_of(run_action("pull", {"repo": str(remote_pair.work)}, [remote_pair.root]))

    assert host_git(remote_pair.work, "rev-parse", "HEAD") == pushed


def test_merge_merges_a_task_branch_into_main(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """A merge of task/m on main brings main to task/m's commit."""
    work = remote_pair.work
    host_git(work, "checkout", "-q", "-b", "task/m")
    (work / "m.txt").write_text("merged\n")
    host_git(work, "add", "m.txt")
    host_git(work, "commit", "-q", "-m", "on task/m")
    host_git(work, "checkout", "-q", "main")

    outcome = run_action(
        "merge", {"repo": str(work), "branch": "task/m"}, [remote_pair.root]
    )

    stdout_of(outcome)
    assert host_git(work, "rev-parse", "main") == host_git(work, "rev-parse", "task/m")


@pytest.mark.parametrize(
    "repo_has_identity",
    [False, True],
    ids=["repo-sets-no-identity", "repo-sets-another-identity"],
)
def test_merge_takes_its_identity_from_the_grants_env(
    run_action: RunAction, remote_pair: RemotePair, repo_has_identity: bool
) -> None:
    """A merge commit carries the grant env's identity, whatever the repository sets."""
    work = remote_pair.work
    host_git(work, "checkout", "-q", "-b", "task/m")
    (work / "m.txt").write_text("merged\n")
    host_git(work, "add", "m.txt")
    host_git(work, "commit", "-q", "-m", "on task/m")
    host_git(work, "checkout", "-q", "main")
    (work / "a.txt").write_text("two\n")
    host_git(work, "commit", "-q", "-am", "on main")
    if repo_has_identity:
        assert host_git(work, "config", "user.name").strip() == FIXTURE_NAME
        assert host_git(work, "config", "user.email").strip() == FIXTURE_EMAIL
    else:
        host_git(work, "config", "--unset", "user.name")
        host_git(work, "config", "--unset", "user.email")
    identity = {
        "GIT_AUTHOR_NAME": AGENT_NAME,
        "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
        "GIT_COMMITTER_NAME": AGENT_NAME,
        "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
    }

    outcome = run_action(
        "merge",
        {"repo": str(work), "branch": "task/m"},
        [remote_pair.root],
        env=identity,
    )

    stdout_of(outcome)
    logged = host_git(work, "log", "-1", "--format=%p|%an <%ae>|%cn <%ce>", "main")
    parents, author, committer = logged.rstrip("\n").split("|")
    assert len(parents.split()) == 2
    assert author == committer == f"{AGENT_NAME} <{AGENT_EMAIL}>"


def test_worktree_adds_lists_and_removes_a_worktree(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """The worktree actions add, list, and remove act on a worktree inside the root."""
    repo = str(remote_pair.work)
    worktree = remote_pair.root / "wt"
    roots = [remote_pair.root]
    add = {"repo": repo, "action": "add", "path": str(worktree), "branch": "task/wt"}

    stdout_of(run_action("worktree", add, roots))
    listed = stdout_of(run_action("worktree", {"repo": repo, "action": "list"}, roots))
    status = stdout_of(run_action("status", {"repo": str(worktree)}, roots))
    remove = {"repo": repo, "action": "remove", "path": str(worktree)}
    stdout_of(run_action("worktree", remove, roots))

    assert f"{worktree} " in listed
    assert "On branch task/wt" in status
    assert not worktree.exists()


def test_stash_saves_shows_and_restores_an_edit(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """Stash push, show, apply, drop, and pop each exit 0; the list ends empty."""
    work = remote_pair.work
    repo = str(work)
    roots = [remote_pair.root]
    (work / "a.txt").write_text("edited\n")

    push = {"repo": repo, "action": "push", "message": "saved edit"}
    stdout_of(run_action("stash", push, roots))
    listed = stdout_of(run_action("stash_list", {"repo": repo}, roots))
    show = {"repo": repo, "action": "show", "index": 0}
    shown = stdout_of(run_action("stash", show, roots))
    for step in (
        {"action": "apply", "index": 0},
        {"action": "drop", "index": 0},
        {"action": "push"},
        {"action": "pop", "index": 0},
    ):
        stdout_of(run_action("stash", {"repo": repo, **step}, roots))

    assert "saved edit" in listed
    assert "a.txt" in shown
    assert (work / "a.txt").read_text() == "edited\n"
    assert stdout_of(run_action("stash_list", {"repo": repo}, roots)) == ""


def test_lfs_fetch_then_lfs_pull_fill_in_the_lfs_file(
    run_action: RunAction, lfs_remote_pair: RemotePair
) -> None:
    """lfs_fetch then lfs_pull in peer replace the pointer with work's big.bin."""
    peer = lfs_remote_pair.peer
    roots = [lfs_remote_pair.root]

    stdout_of(run_action("lfs_fetch", {"repo": str(peer)}, roots))
    stdout_of(run_action("lfs_pull", {"repo": str(peer)}, roots))

    pulled = (peer / "big.bin").read_bytes()
    committed = (lfs_remote_pair.work / "big.bin").read_bytes()
    assert len(pulled) == 2048
    assert hashlib.sha256(pulled).digest() == hashlib.sha256(committed).digest()


def test_credential_helper_answers_the_servers_challenge(
    run_action: RunAction, lfs_remote_pair: RemotePair, auth_server: Container
) -> None:
    """Both git and git LFS send GH_TOKEN to a server that answers 401 without it."""
    peer = lfs_remote_pair.peer
    host_git(peer, "remote", "set-url", "origin", AUTH_URL)
    credentials = {"gh-token": (EnvMount(env="GH_TOKEN"), b"dummy")}

    for name in ("fetch", "lfs_fetch"):
        outcome = run_action(
            name, {"repo": str(peer)}, [lfs_remote_pair.root], credentials
        )
        assert outcome.content["exit_code"] != 0, outcome.content

    log = auth_server.logs().decode()
    assert "AUTH OK GET /repo.git/info/refs?service=git-upload-pack\n" in log
    assert "AUTH OK POST /repo.git/info/lfs/objects/batch\n" in log
