import hashlib
from pathlib import Path

import pytest
from agent_proxy.config import EnvMount, FileMount
from agent_proxy.runner import Outcome
from docker.models.containers import Container
from harness import (
    AGENT_EMAIL,
    AGENT_NAME,
    FIXTURE_EMAIL,
    FIXTURE_NAME,
    SIGNING_KEY_PATH,
    RemotePair,
    RunAction,
    host_git,
    make_signing_key,
    stdout_of,
    write_allowed_signers,
)

LISTED_URL = "http://auth:8080/listed-org/repo.git"
UNLISTED_URL = "http://auth:8080/other-org/repo.git"
LISTED = {"GITHUB_REPOS": "listed-org/repo"}
PUSH_ENV = {**LISTED, "GIT_BRANCH_PREFIXES": "task/"}
TOKEN = {"gh-token": (EnvMount(env="GH_TOKEN"), b"dummy")}
UNLISTED_REFUSAL = (
    "git plugin refused: repository 'other-org/repo' is not in GITHUB_REPOS\n"
)
NO_PATH_REFUSAL = (
    "git plugin refused: credential request names no path; "
    "git sends one only under credential.useHttpPath\n"
)
NO_REPOS_REFUSAL = (
    "git plugin refused: "
    "GITHUB_REPOS is not set or names no repository; the grant's env sets it\n"
)


def _push_from_peer(pair: RemotePair) -> str:
    """Commit b.txt in peer, push it to the remote's main, and return its hash."""
    (pair.peer / "b.txt").write_text("from peer\n")
    host_git(pair.peer, "add", "b.txt")
    host_git(pair.peer, "commit", "-q", "-m", "from peer")
    host_git(pair.peer, "push", "-q", "origin", "main")
    return host_git(pair.peer, "rev-parse", "HEAD")


def _point_lfs_clone_at(repo: Path, url: str) -> None:
    """Set repo's origin to url and drop its .lfsconfig, so git LFS dials url too."""
    host_git(repo, "remote", "set-url", "origin", url)
    (repo / ".lfsconfig").unlink()


def _run_with_token(
    run_action: RunAction,
    name: str,
    repo: Path,
    root: Path,
    env: dict[str, str] | None,
) -> Outcome:
    """Run the action name on repo with root granted, GH_TOKEN dummy, and env."""
    return run_action(name, {"repo": str(repo)}, [root], TOKEN, env=env)


def _refused(outcome: Outcome, reason: str, log: str) -> None:
    """Fail unless the action failed with reason on stderr and no AUTH OK logged."""
    assert outcome.content["exit_code"] != 0, outcome.content
    assert reason in str(outcome.content["stderr"]), outcome.content
    assert "AUTH OK" not in log


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


def test_merge_with_a_granted_signing_key_signs_the_merge_commit(
    run_action: RunAction, remote_pair: RemotePair, tmp_path: Path
) -> None:
    """A non-fast-forward merge with the key granted makes a signed merge commit."""
    work = remote_pair.work
    host_git(work, "checkout", "-q", "-b", "task/m")
    (work / "m.txt").write_text("merged\n")
    host_git(work, "add", "m.txt")
    host_git(work, "commit", "-q", "-m", "on task/m")
    host_git(work, "checkout", "-q", "main")
    (work / "a.txt").write_text("two\n")
    host_git(work, "commit", "-q", "-am", "on main")
    keys = tmp_path / "keys"
    keys.mkdir()
    private_key, public_key = make_signing_key(keys)
    allowed = write_allowed_signers(keys / "allowed-signers", AGENT_EMAIL, public_key)
    env = {
        "GIT_AUTHOR_NAME": AGENT_NAME,
        "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
        "GIT_COMMITTER_NAME": AGENT_NAME,
        "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
        "GIT_SIGNING_KEY": SIGNING_KEY_PATH,
    }

    outcome = run_action(
        "merge",
        {"repo": str(work), "branch": "task/m"},
        [remote_pair.root],
        {"git-signing-key": (FileMount(file=SIGNING_KEY_PATH), private_key)},
        env=env,
    )

    stdout_of(outcome)
    logged = host_git(
        work,
        "-c",
        f"gpg.ssh.allowedSignersFile={allowed}",
        "log",
        "-1",
        "--format=%p|%G?",
        "main",
    )
    parents, status = logged.rstrip("\n").split("|")
    assert len(parents.split()) == 2
    assert status == "G"


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

    assert listed.startswith(f"{repo} ")
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
    """Both git and git LFS send GH_TOKEN to a listed server that answers 401."""
    peer = lfs_remote_pair.peer
    _point_lfs_clone_at(peer, LISTED_URL)

    for name in ("fetch", "lfs_fetch"):
        outcome = _run_with_token(run_action, name, peer, lfs_remote_pair.root, LISTED)
        assert outcome.content["exit_code"] != 0, outcome.content

    log = auth_server.logs().decode()
    assert "AUTH OK GET /listed-org/repo.git/info/refs?service=git-upload-pack\n" in log
    assert "AUTH OK POST /listed-org/repo.git/info/lfs/objects/batch\n" in log


def test_push_hands_the_helper_the_listed_path(
    run_action: RunAction, remote_pair: RemotePair, auth_server: Container
) -> None:
    """A push to a listed remote sends GH_TOKEN for the receive-pack request."""
    work = remote_pair.work
    host_git(work, "checkout", "-q", "-b", "task/p")
    host_git(work, "remote", "set-url", "origin", LISTED_URL)

    outcome = _run_with_token(run_action, "push", work, remote_pair.root, PUSH_ENV)

    assert outcome.content["exit_code"] != 0, outcome.content
    log = auth_server.logs().decode()
    assert (
        "AUTH OK GET /listed-org/repo.git/info/refs?service=git-receive-pack\n" in log
    )


def test_pull_sends_the_token_to_a_listed_remote(
    run_action: RunAction, remote_pair: RemotePair, auth_server: Container
) -> None:
    """A pull from a listed remote sends GH_TOKEN for the upload-pack request."""
    work = remote_pair.work
    host_git(work, "remote", "set-url", "origin", LISTED_URL)

    outcome = _run_with_token(run_action, "pull", work, remote_pair.root, LISTED)

    assert outcome.content["exit_code"] != 0, outcome.content
    log = auth_server.logs().decode()
    assert "AUTH OK GET /listed-org/repo.git/info/refs?service=git-upload-pack\n" in log


def test_lfs_pull_sends_the_token_to_a_listed_remote(
    run_action: RunAction, lfs_remote_pair: RemotePair, auth_server: Container
) -> None:
    """An LFS pull from a listed remote sends GH_TOKEN for the batch request."""
    peer = lfs_remote_pair.peer
    _point_lfs_clone_at(peer, LISTED_URL)

    outcome = _run_with_token(
        run_action, "lfs_pull", peer, lfs_remote_pair.root, LISTED
    )

    assert outcome.content["exit_code"] != 0, outcome.content
    log = auth_server.logs().decode()
    assert "AUTH OK POST /listed-org/repo.git/info/lfs/objects/batch\n" in log


@pytest.mark.parametrize("name", ["fetch", "pull", "push"])
def test_a_git_action_refuses_an_unlisted_remote(
    run_action: RunAction,
    remote_pair: RemotePair,
    auth_server: Container,
    name: str,
) -> None:
    """An origin outside GITHUB_REPOS gets no token, and the refusal names it."""
    work = remote_pair.work
    host_git(work, "checkout", "-q", "-b", "task/p")
    host_git(work, "remote", "set-url", "origin", UNLISTED_URL)

    outcome = _run_with_token(run_action, name, work, remote_pair.root, PUSH_ENV)

    _refused(outcome, UNLISTED_REFUSAL, auth_server.logs().decode())


@pytest.mark.parametrize("name", ["lfs_fetch", "lfs_pull"])
def test_an_lfs_action_refuses_an_unlisted_remote(
    run_action: RunAction,
    lfs_remote_pair: RemotePair,
    auth_server: Container,
    name: str,
) -> None:
    """An LFS origin outside GITHUB_REPOS gets no token, and the refusal names it."""
    peer = lfs_remote_pair.peer
    _point_lfs_clone_at(peer, UNLISTED_URL)

    outcome = _run_with_token(run_action, name, peer, lfs_remote_pair.root, LISTED)

    _refused(outcome, UNLISTED_REFUSAL, auth_server.logs().decode())


def test_push_refuses_a_pushurl_to_an_unlisted_repository(
    run_action: RunAction, remote_pair: RemotePair, auth_server: Container
) -> None:
    """A listed origin whose pushurl names an unlisted repository gets no token."""
    work = remote_pair.work
    host_git(work, "checkout", "-q", "-b", "task/p")
    host_git(work, "remote", "set-url", "origin", LISTED_URL)
    host_git(work, "config", "remote.origin.pushurl", UNLISTED_URL)

    outcome = _run_with_token(run_action, "push", work, remote_pair.root, PUSH_ENV)

    _refused(outcome, UNLISTED_REFUSAL, auth_server.logs().decode())


def test_fetch_ignores_a_planted_insteadof(
    run_action: RunAction, remote_pair: RemotePair, auth_server: Container
) -> None:
    """A planted insteadOf has no effect: the fetch goes to the listed origin.

    The key is never copied to the scratch config, so the listed origin gets
    the token and the unlisted one gets no request.
    """
    work = remote_pair.work
    host_git(work, "remote", "set-url", "origin", LISTED_URL)
    host_git(work, "config", f"url.{UNLISTED_URL}.insteadOf", LISTED_URL)

    outcome = _run_with_token(run_action, "fetch", work, remote_pair.root, LISTED)

    assert outcome.content["exit_code"] != 0, outcome.content
    assert UNLISTED_REFUSAL not in str(outcome.content["stderr"]), outcome.content
    log = auth_server.logs().decode()
    assert "AUTH OK GET /listed-org/repo.git/info/refs?service=git-upload-pack\n" in log
    assert "/other-org/" not in log


def test_lfs_fetch_ignores_a_planted_use_http_path(
    run_action: RunAction, lfs_remote_pair: RemotePair, auth_server: Container
) -> None:
    """A planted useHttpPath=false has no effect: LFS sends the path.

    The key is never copied to the scratch config, so the plugin's -c value
    governs, and the helper grants the LFS batch request.
    """
    peer = lfs_remote_pair.peer
    _point_lfs_clone_at(peer, LISTED_URL)
    host_git(peer, "config", "credential.http://auth:8080.useHttpPath", "false")

    outcome = _run_with_token(
        run_action, "lfs_fetch", peer, lfs_remote_pair.root, LISTED
    )

    assert outcome.content["exit_code"] != 0, outcome.content
    assert NO_PATH_REFUSAL not in str(outcome.content["stderr"]), outcome.content
    log = auth_server.logs().decode()
    assert "AUTH OK POST /listed-org/repo.git/info/lfs/objects/batch\n" in log


def test_fetch_with_a_token_refuses_without_a_repository_list(
    run_action: RunAction, remote_pair: RemotePair
) -> None:
    """A fetch granted GH_TOKEN but no GITHUB_REPOS is refused before git runs."""
    work = remote_pair.work
    before = host_git(work, "rev-parse", "origin/main")
    _push_from_peer(remote_pair)

    outcome = _run_with_token(run_action, "fetch", work, remote_pair.root, None)

    assert outcome.content == {
        "exit_code": 2,
        "stdout": "",
        "stderr": NO_REPOS_REFUSAL,
    }
    assert host_git(work, "rev-parse", "origin/main") == before
