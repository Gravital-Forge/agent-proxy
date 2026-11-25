from collections.abc import Callable
from pathlib import Path

import pytest
from agent_proxy.config import EnvMount
from agent_proxy.runner import Outcome
from harness import GitHubRemote, RunAction, host_git, stdout_of

LISTED_REPO = "listed-org/repo"
LISTED_URL = "https://github.com/listed-org/repo.git"
LISTED = {"GITHUB_REPOS": LISTED_REPO}
TOKEN = {"gh-token": (EnvMount(env="GH_TOKEN"), b"dummy")}

type TrustServer = Callable[[Path], dict[str, str]]


@pytest.fixture
def clone_root(tmp_path: Path) -> Path:
    """An empty root for a clone to fill."""
    root = tmp_path / "clone-root"
    root.mkdir()
    return root


@pytest.fixture
def trust_root(tmp_path: Path) -> Path:
    """A second root, for the server's certificate, so the clone root stays empty."""
    root = tmp_path / "trust-root"
    root.mkdir()
    return root


def _run_trusting_server(
    run_action: RunAction,
    trust_auth_server: TrustServer,
    name: str,
    repo_argument: dict[str, str],
    roots: tuple[Path, Path],
) -> Outcome:
    """Run the action name with both roots, GH_TOKEN dummy, and the list.

    The cage trusts the server's certificate, which the second root holds.
    """
    clone_root, trust_root = roots
    env = {**LISTED, **trust_auth_server(trust_root)}
    return run_action(name, repo_argument, [clone_root, trust_root], TOKEN, env=env)


@pytest.fixture
def cloned(
    run_action: RunAction,
    trust_auth_server: TrustServer,
    git_http_server: GitHubRemote,
    clone_root: Path,
    trust_root: Path,
) -> Path:
    """clone_root, which the clone action filled from the listed repository."""
    arguments = {"repo": LISTED_REPO, "path": str(clone_root)}
    outcome = _run_trusting_server(
        run_action, trust_auth_server, "clone", arguments, (clone_root, trust_root)
    )

    assert outcome.content["exit_code"] == 0, outcome.content
    return clone_root


def test_clone_names_origin_by_the_listed_url(cloned: Path) -> None:
    """The clone's own config names origin by the repository's GitHub URL."""
    assert host_git(cloned, "config", "--local", "remote.origin.url") == (
        f"{LISTED_URL}\n"
    )


def test_clone_checks_out_the_default_branch_tracking_origin(cloned: Path) -> None:
    """main, the remote's HEAD, is checked out with main's file and tracks origin."""
    assert host_git(cloned, "rev-parse", "--abbrev-ref", "HEAD") == "main\n"
    assert (cloned / "a.txt").read_text() == "one\n"
    assert host_git(cloned, "rev-parse", "--abbrev-ref", "main@{upstream}") == (
        "origin/main\n"
    )


def test_clone_puts_every_branch_under_origin(cloned: Path) -> None:
    """Each remote branch is under refs/remotes/origin/, beside the local main."""
    refs = host_git(cloned, "for-each-ref", "--format=%(refname)")

    assert refs.split() == [
        "refs/heads/main",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
        "refs/remotes/origin/task/b",
    ]


def test_clone_leaves_an_lfs_file_as_its_pointer(
    cloned: Path, git_http_server: GitHubRemote
) -> None:
    """A file the repository tracks with LFS holds its pointer, fetched from no one."""
    assert (cloned / "big.bin").read_text() == git_http_server.pointer


def test_status_and_fetch_accept_the_clone(
    run_action: RunAction,
    trust_auth_server: TrustServer,
    cloned: Path,
    trust_root: Path,
) -> None:
    """git.status and git.fetch on the cloned path both exit 0."""
    repo = {"repo": str(cloned)}

    status = stdout_of(run_action("status", repo, [cloned, trust_root]))
    fetched = _run_trusting_server(
        run_action, trust_auth_server, "fetch", repo, (cloned, trust_root)
    )

    assert "nothing to commit, working tree clean" in status
    assert fetched.content["exit_code"] == 0, fetched.content


def _refusal(outcome: Outcome, reason: str) -> None:
    """Fail unless the action was refused for reason."""
    assert outcome.content == {
        "exit_code": 2,
        "stdout": "",
        "stderr": f"git plugin refused: {reason}\n",
    }


def test_clone_refuses_an_unlisted_repository_without_a_token(
    run_action: RunAction, clone_root: Path
) -> None:
    """With no GH_TOKEN in the cage, a repository the list lacks is refused."""
    arguments = {"repo": "other-org/repo", "path": str(clone_root)}

    outcome = run_action("clone", arguments, [clone_root], env=LISTED)

    _refusal(outcome, "repository 'other-org/repo' is not in GITHUB_REPOS")
    assert list(clone_root.iterdir()) == []


def test_clone_refuses_a_path_that_is_not_a_root(
    run_action: RunAction, clone_root: Path
) -> None:
    """An empty directory beneath the root is refused."""
    path = clone_root / "sub"
    path.mkdir()
    arguments = {"repo": LISTED_REPO, "path": str(path)}

    outcome = run_action("clone", arguments, [clone_root], env=LISTED)

    _refusal(outcome, f"clone path '{path}' is not one of the grant's roots")
    assert list(path.iterdir()) == []


def test_clone_refuses_a_root_that_is_not_empty(
    run_action: RunAction, clone_root: Path
) -> None:
    """A root that holds a file is refused, and the file stays alone in it."""
    (clone_root / "a.txt").write_text("kept\n")
    arguments = {"repo": LISTED_REPO, "path": str(clone_root)}

    outcome = run_action("clone", arguments, [clone_root], env=LISTED)

    _refusal(outcome, f"clone path '{clone_root}' is not empty")
    assert [entry.name for entry in clone_root.iterdir()] == ["a.txt"]
