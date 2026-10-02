import re

import pytest
from git_plugin import rules
from git_plugin.cage import RefusalError


def _prefix_refusal(branch: str) -> str:
    """Return the pattern of the refusal for a branch outside the prefix."""
    return rf"^Branch '{re.escape(branch)}' does not match any allowed prefix: task/$"


def test_protected_branches_are_main_and_master() -> None:
    """Only main and master are protected."""
    assert rules.PROTECTED_BRANCHES == {"main", "master"}


def test_the_branch_prefix_is_task() -> None:
    """task/ is the one prefix; git-sidecar's dependabot/ is cut."""
    assert rules.BRANCH_PREFIX == "task/"


@pytest.mark.parametrize("branch", ["task/p", "task/a/b", "task/main"])
def test_push_branch_accepts_a_task_branch(branch: str) -> None:
    """A branch under task/ may be pushed."""
    rules.check_push_branch(branch)


@pytest.mark.parametrize("branch", ["main", "master"])
def test_push_branch_refuses_a_protected_branch(branch: str) -> None:
    """A protected branch is refused before the prefix is checked."""
    with pytest.raises(
        RefusalError, match=rf"^Cannot push to protected branch: {branch}$"
    ):
        rules.check_push_branch(branch)


@pytest.mark.parametrize(
    "branch",
    ["feature/x", "dependabot/x", "task", "tasks/x", "Task/x", "x/task/y", "HEAD", ""],
)
def test_push_branch_refuses_a_branch_without_the_prefix(branch: str) -> None:
    """A branch outside task/, a detached HEAD's 'HEAD' included, is refused."""
    with pytest.raises(RefusalError, match=_prefix_refusal(branch)):
        rules.check_push_branch(branch)


@pytest.mark.parametrize("target", ["main", "master", "task/c", "task/a/b"])
def test_checkout_target_accepts_a_protected_or_task_branch(target: str) -> None:
    """A protected branch or a branch under task/ may be checked out."""
    rules.check_checkout_target(target)


@pytest.mark.parametrize(
    "target",
    [
        "feature/x",
        "dependabot/x",
        "tasks/x",
        "HEAD",
        "HEAD~1",
        "v1",
        "origin/main",
        "-",
        "",
    ],
)
def test_checkout_target_refuses_any_other_target(target: str) -> None:
    """A tag, a commit, a remote branch, or any other branch is refused."""
    with pytest.raises(RefusalError, match=_prefix_refusal(target)):
        rules.check_checkout_target(target)
