"""A repository shape the isolation cannot reproduce is refused before git runs."""

from collections.abc import Callable
from pathlib import Path

import pytest
from harness import FIXTURE_EMAIL, FIXTURE_NAME, RunAction, host_git

TASK_PREFIX = {"GIT_BRANCH_PREFIXES": "task/"}
# Each reason holds one {} for the repository path.
NOT_TOP_LEVEL = (
    "{} has no .git; the repository path must be the top level of a checkout"
)
SHALLOW = "{} is a shallow clone; the git plugin does not support shallow clones"
REFTABLE = "{} uses the reftable ref format; the git plugin supports only files"
SUBMODULES = "{} has submodules; the git plugin does not support submodules"
EXTENSION = "{{}} sets {}, an extension the git plugin does not support"


def _shallow_clone(host_repo: Path) -> Path:
    clone = host_repo.parent / "shallow"
    source = f"file://{host_repo}"
    host_git(host_repo.parent, "clone", "-q", "--depth", "1", source, str(clone))
    return clone


def _reftable_repo(host_repo: Path) -> Path:
    repo = host_repo.parent / "reftable"
    host_git(host_repo.parent, "init", "-q", "--ref-format=reftable", str(repo))
    return repo


def _sha256_repo(host_repo: Path) -> Path:
    repo = host_repo.parent / "sha256"
    host_git(host_repo.parent, "init", "-q", "--object-format=sha256", str(repo))
    return repo


def _partial_clone(host_repo: Path) -> Path:
    clone = host_repo.parent / "partial"
    source = f"file://{host_repo}"
    host_git(host_repo, "config", "uploadpack.allowFilter", "true")
    host_git(host_repo.parent, "clone", "-q", "--filter=blob:none", source, str(clone))
    return clone


def _with_submodule(host_repo: Path) -> Path:
    lib = host_repo.parent / "lib"
    host_git(host_repo.parent, "init", "-q", "-b", "main", str(lib))
    host_git(
        lib,
        *("-c", f"user.name={FIXTURE_NAME}", "-c", f"user.email={FIXTURE_EMAIL}"),
        *("commit", "-q", "--allow-empty", "-m", "lib"),
    )
    host_git(
        host_repo,
        *("-c", "protocol.file.allow=always"),
        *("submodule", "add", "-q", f"file://{lib}", "lib"),
    )
    return host_repo


def _subdirectory(host_repo: Path) -> Path:
    directory = host_repo / "sub"
    directory.mkdir()
    return directory


@pytest.mark.parametrize(
    ("make", "action", "arguments", "reason"),
    [
        pytest.param(_shallow_clone, "fetch", {}, SHALLOW, id="shallow-fetch"),
        pytest.param(_reftable_repo, "status", {}, REFTABLE, id="reftable-status"),
        pytest.param(
            _with_submodule,
            "checkout",
            {"target": "task/x", "create": True},
            SUBMODULES,
            id="submodule-checkout",
        ),
        pytest.param(
            _sha256_repo,
            "status",
            {},
            EXTENSION.format("extensions.objectformat"),
            id="sha256-status",
        ),
        pytest.param(
            _partial_clone,
            "fetch",
            {},
            EXTENSION.format("extensions.partialclone"),
            id="partial-clone-fetch",
        ),
        pytest.param(_subdirectory, "status", {}, NOT_TOP_LEVEL, id="subdir-status"),
        pytest.param(
            _subdirectory, "add", {"files": ["a.txt"]}, NOT_TOP_LEVEL, id="subdir-add"
        ),
    ],
)
def test_an_unsupported_repository_shape_is_refused(
    run_action: RunAction,
    host_repo: Path,
    make: Callable[[Path], Path],
    action: str,
    arguments: dict[str, object],
    reason: str,
) -> None:
    """The action meets the plugin's refusal naming the shape, and git does no work."""
    repo = make(host_repo)

    outcome = run_action(
        action, {"repo": str(repo), **arguments}, [host_repo.parent], env=TASK_PREFIX
    )

    assert outcome.content == {
        "exit_code": 2,
        "stdout": "",
        "stderr": f"git plugin refused: {reason.format(repo)}\n",
    }
    assert "task/x" not in host_git(host_repo, "branch", "--list")
