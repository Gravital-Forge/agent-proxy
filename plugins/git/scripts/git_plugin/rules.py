"""The branch rules and the repository list, from the grant's env.

The branch prefixes come from GIT_BRANCH_PREFIXES, and the repositories from
GITHUB_REPOS; the grant's env sets both. main and master are protected
whatever the prefixes hold.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from git_plugin import cage

PROTECTED_BRANCHES = frozenset({"main", "master"})
PREFIXES_ENV_NAME = "GIT_BRANCH_PREFIXES"
REPOS_ENV_NAME = "GITHUB_REPOS"
CREDENTIAL_ENV_NAME = "GH_TOKEN"
ANY_NAME = "*"
# The two halves of the github plugin manifest's repo pattern.
OWNER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]*[A-Za-z0-9_-][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class RepoEntry:
    """One GITHUB_REPOS entry: a repository, or with name '*', all of owner's."""

    owner: str
    name: str


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
        raise cage.RefusalError(
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
        raise cage.RefusalError(f"Cannot push to protected branch: {branch}")
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
        raise cage.RefusalError(
            f"Branch '{branch}' does not match any allowed prefix: "
            f"{', '.join(prefixes)}"
        )


def allowed_repos(environ: Mapping[str, str]) -> list[RepoEntry]:
    """Return the entries environ's GITHUB_REPOS names, in order.

    The value is split on commas, each entry stripped, and empty entries
    dropped, as for GIT_BRANCH_PREFIXES.

    Raises:
        RefusalError: the variable is unset, names no entry, or holds an
            entry that is not owner/name or owner/*.
    """
    entries = environ.get(REPOS_ENV_NAME, "").split(",")
    stripped = [entry.strip() for entry in entries if entry.strip()]
    if not stripped:
        raise cage.RefusalError(
            f"{REPOS_ENV_NAME} is not set or names no repository; "
            "the grant's env sets it"
        )
    return [_repo_entry(entry) for entry in stripped]


def _repo_entry(entry: str) -> RepoEntry:
    """Return entry parsed as owner/name or owner/*.

    Raises:
        RefusalError: entry has any other form.
    """
    owner, slash, name = entry.partition("/")
    if not (
        slash
        and OWNER_PATTERN.fullmatch(owner)
        and (name == ANY_NAME or NAME_PATTERN.fullmatch(name))
    ):
        raise cage.RefusalError(
            f"{REPOS_ENV_NAME} entry '{entry}' is not owner/name or owner/*"
        )
    return RepoEntry(owner=owner, name=name)


def check_repo(repo: str, entries: Sequence[RepoEntry]) -> None:
    """Return if one of entries names repo, given as owner/name, in any case.

    GitHub treats owner and repository names without regard to case.

    Raises:
        RefusalError: no entry names repo.
    """
    owner, _, name = repo.partition("/")
    if not any(_names(entry, owner, name) for entry in entries):
        raise cage.RefusalError(f"repository '{repo}' is not in {REPOS_ENV_NAME}")


def _names(entry: RepoEntry, owner: str, name: str) -> bool:
    """Return whether entry names the repository owner/name, in any case."""
    return entry.owner.casefold() == owner.casefold() and (
        entry.name == ANY_NAME or entry.name.casefold() == name.casefold()
    )


def check_token_cage(environ: Mapping[str, str]) -> None:
    """Return if environ holds no GH_TOKEN, or its GITHUB_REPOS parses.

    The check keys on GH_TOKEN, not on the action: the core does not compare
    a grant's credentials with the manifest's, so any cage that holds the
    token needs the list.

    Raises:
        RefusalError: environ holds GH_TOKEN, and allowed_repos refuses.
    """
    if CREDENTIAL_ENV_NAME in environ:
        allowed_repos(environ)
