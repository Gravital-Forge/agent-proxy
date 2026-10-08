import re

import pytest
from git_plugin import rules
from git_plugin.cage import RefusalError

TASK = ("task/",)
TASK_AND_DEPENDABOT = ("task/", "dependabot/")
NO_PREFIXES = (
    r"^GIT_BRANCH_PREFIXES is not set or names no prefix; the grant's env sets it$"
)


def _prefix_refusal(branch: str, prefixes: str = "task/") -> str:
    """Return the pattern of the refusal for a branch outside the prefixes."""
    return (
        rf"^Branch '{re.escape(branch)}' does not match any allowed prefix: "
        rf"{re.escape(prefixes)}$"
    )


def test_protected_branches_are_main_and_master() -> None:
    """Only main and master are protected."""
    assert rules.PROTECTED_BRANCHES == {"main", "master"}


def test_the_prefixes_come_from_git_branch_prefixes() -> None:
    """The grant's env names the prefixes in GIT_BRANCH_PREFIXES."""
    assert rules.PREFIXES_ENV_NAME == "GIT_BRANCH_PREFIXES"


@pytest.mark.parametrize(
    ("value", "prefixes"),
    [
        ("task/, dependabot/", ("task/", "dependabot/")),
        (" ,task/,,", ("task/",)),
    ],
)
def test_branch_prefixes_strips_each_entry_and_drops_empty_ones(
    value: str, prefixes: tuple[str, ...]
) -> None:
    """The value splits on commas into stripped, non-empty prefixes, in order."""
    assert rules.branch_prefixes({"GIT_BRANCH_PREFIXES": value}) == prefixes


def test_branch_prefixes_refuses_when_the_variable_is_unset() -> None:
    """An environment without GIT_BRANCH_PREFIXES is refused, naming the variable."""
    with pytest.raises(RefusalError, match=NO_PREFIXES):
        rules.branch_prefixes({})


@pytest.mark.parametrize("value", ["", " , "])
def test_branch_prefixes_refuses_a_value_that_names_no_prefix(value: str) -> None:
    """A value with no prefix left after stripping is refused, naming the variable."""
    with pytest.raises(RefusalError, match=NO_PREFIXES):
        rules.branch_prefixes({"GIT_BRANCH_PREFIXES": value})


@pytest.mark.parametrize("branch", ["task/p", "task/a/b", "task/main"])
def test_push_branch_accepts_a_task_branch(branch: str) -> None:
    """A branch under task/ may be pushed."""
    rules.check_push_branch(branch, TASK)


def test_push_branch_accepts_a_branch_under_a_second_prefix() -> None:
    """A branch under the second of two prefixes may be pushed."""
    rules.check_push_branch("dependabot/x", TASK_AND_DEPENDABOT)


@pytest.mark.parametrize("branch", ["main", "master"])
def test_push_branch_refuses_a_protected_branch(branch: str) -> None:
    """A protected branch is refused before the prefix is checked."""
    with pytest.raises(
        RefusalError, match=rf"^Cannot push to protected branch: {branch}$"
    ):
        rules.check_push_branch(branch, TASK)


@pytest.mark.parametrize(
    "branch",
    ["feature/x", "dependabot/x", "task", "tasks/x", "Task/x", "x/task/y", "HEAD", ""],
)
def test_push_branch_refuses_a_branch_without_the_prefix(branch: str) -> None:
    """A branch outside task/, a detached HEAD's 'HEAD' included, is refused."""
    with pytest.raises(RefusalError, match=_prefix_refusal(branch)):
        rules.check_push_branch(branch, TASK)


def test_push_branch_refusal_names_every_prefix() -> None:
    """The refusal lists the prefixes in order, joined by a comma and a space."""
    with pytest.raises(
        RefusalError, match=_prefix_refusal("feature/x", "task/, dependabot/")
    ):
        rules.check_push_branch("feature/x", TASK_AND_DEPENDABOT)


@pytest.mark.parametrize("target", ["main", "master", "task/c", "task/a/b"])
def test_checkout_target_accepts_a_protected_or_task_branch(target: str) -> None:
    """A protected branch or a branch under task/ may be checked out."""
    rules.check_checkout_target(target, TASK)


def test_checkout_target_accepts_a_branch_under_a_second_prefix() -> None:
    """A branch under the second of two prefixes may be checked out."""
    rules.check_checkout_target("dependabot/x", TASK_AND_DEPENDABOT)


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
        rules.check_checkout_target(target, TASK)


def test_checkout_target_refusal_names_every_prefix() -> None:
    """The refusal lists the prefixes in order, joined by a comma and a space."""
    with pytest.raises(
        RefusalError, match=_prefix_refusal("feature/x", "task/, dependabot/")
    ):
        rules.check_checkout_target("feature/x", TASK_AND_DEPENDABOT)
