"""git-sidecar's branch rules, with the allowed prefixes from the grant's env.

The prefixes come from GIT_BRANCH_PREFIXES, which the grant's env sets. main
and master are protected whatever it holds.
"""

from collections.abc import Mapping, Sequence

from git_plugin.cage import RefusalError

PROTECTED_BRANCHES = frozenset({"main", "master"})
PREFIXES_ENV_NAME = "GIT_BRANCH_PREFIXES"


def branch_prefixes(environ: Mapping[str, str]) -> list[str]:
    """Return the branch prefixes environ's GIT_BRANCH_PREFIXES names, in order.

    The value is split on commas, each entry stripped, and empty entries
    dropped.

    Raises:
        RefusalError: the variable is unset, or names no prefix.
    """
    entries = environ.get(PREFIXES_ENV_NAME, "").split(",")
    prefixes = [entry.strip() for entry in entries if entry.strip()]
    if not prefixes:
        raise RefusalError(
            f"{PREFIXES_ENV_NAME} is not set or names no prefix; "
            "the grant's env sets it"
        )
    return prefixes


def check_push_branch(branch: str, prefixes: Sequence[str]) -> None:
    """Return if branch may be pushed: not protected, and under one of prefixes.

    A detached HEAD reaches this check as the branch 'HEAD'.

    Raises:
        RefusalError: branch is protected, or begins with none of prefixes.
    """
    if branch in PROTECTED_BRANCHES:
        raise RefusalError(f"Cannot push to protected branch: {branch}")
    _check_prefix(branch, prefixes)


def check_checkout_target(target: str, prefixes: Sequence[str]) -> None:
    """Return if target may be checked out: protected, or under one of prefixes.

    Raises:
        RefusalError: target is not protected and begins with none of
            prefixes.
    """
    if target not in PROTECTED_BRANCHES:
        _check_prefix(target, prefixes)


def _check_prefix(branch: str, prefixes: Sequence[str]) -> None:
    """Return if branch begins with one of prefixes.

    Raises:
        RefusalError: branch begins with none of prefixes.
    """
    if not branch.startswith(tuple(prefixes)):
        raise RefusalError(
            f"Branch '{branch}' does not match any allowed prefix: "
            f"{', '.join(prefixes)}"
        )
