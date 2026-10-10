"""The repository list, from the grant's env.

The repositories come from GITHUB_REPOS, which the grant's env sets. The git
plugin applies the same rule with its own copy.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from github_plugin import scripts

REPOS_ENV_NAME = "GITHUB_REPOS"
ANY_NAME = "*"
# The two halves of the manifest's repo pattern.
OWNER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]*[A-Za-z0-9_-][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class RepoEntry:
    """One GITHUB_REPOS entry: a repository, or with name '*', all of owner's."""

    owner: str
    name: str


def allowed_repos(environ: Mapping[str, str]) -> list[RepoEntry]:
    """Return the entries environ's GITHUB_REPOS names, in order.

    The value is split on commas, each entry stripped, and empty entries
    dropped.

    Raises:
        RefusalError: the variable is unset, names no entry, or holds an
            entry that is not owner/name or owner/*.
    """
    entries = environ.get(REPOS_ENV_NAME, "").split(",")
    stripped = [entry.strip() for entry in entries if entry.strip()]
    if not stripped:
        raise scripts.RefusalError(
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
        raise scripts.RefusalError(
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
        raise scripts.RefusalError(f"repository '{repo}' is not in {REPOS_ENV_NAME}")


def _names(entry: RepoEntry, owner: str, name: str) -> bool:
    """Return whether entry names the repository owner/name, in any case."""
    return entry.owner.casefold() == owner.casefold() and (
        entry.name == ANY_NAME or entry.name.casefold() == name.casefold()
    )
