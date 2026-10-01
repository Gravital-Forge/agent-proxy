"""git-sidecar's branch rules, with task/ as the one branch prefix."""

from git_plugin.cage import RefusalError

PROTECTED_BRANCHES = frozenset({"main", "master"})
BRANCH_PREFIX = "task/"


def check_push_branch(branch: str) -> None:
    """Return if branch may be pushed: not protected, and under BRANCH_PREFIX.

    A detached HEAD reaches this check as the branch 'HEAD'.

    Raises:
        RefusalError: branch is protected, or does not begin with BRANCH_PREFIX.
    """
    if branch in PROTECTED_BRANCHES:
        raise RefusalError(f"Cannot push to protected branch: {branch}")
    _check_prefix(branch)


def check_checkout_target(target: str) -> None:
    """Return if target may be checked out: protected, or under BRANCH_PREFIX.

    Raises:
        RefusalError: target is not protected and does not begin with
            BRANCH_PREFIX.
    """
    if target not in PROTECTED_BRANCHES:
        _check_prefix(target)


def _check_prefix(branch: str) -> None:
    """Return if branch begins with BRANCH_PREFIX.

    Raises:
        RefusalError: branch does not begin with BRANCH_PREFIX.
    """
    if not branch.startswith(BRANCH_PREFIX):
        raise RefusalError(
            f"Branch '{branch}' does not match any allowed prefix: {BRANCH_PREFIX}"
        )
