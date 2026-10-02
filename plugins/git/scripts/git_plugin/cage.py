"""The git-cage wrapper: git on one repository, with the cage's roots trusted."""

import json
import os
import sys
from collections.abc import Mapping, Sequence

REFUSAL_EXIT = 2
REFUSAL_PREFIX = "git plugin refused: "
ROOTS_ENV_NAME = "AGENT_PROXY_ROOTS"
REPO_FLAG = "--repo"
CREDENTIAL_HELPER = (
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!f() { echo username=x-access-token; "
    'echo "password=$GH_TOKEN"; }; f',
)


class RefusalError(Exception):
    """A call the plugin refuses; the message is the reason."""


def roots_from_env(environ: Mapping[str, str]) -> list[str]:
    """Return the root paths AGENT_PROXY_ROOTS names, in order.

    Raises:
        RefusalError: the variable is missing, is not JSON, or is not an array
            of strings that each begin with '/'.
    """
    value = environ.get(ROOTS_ENV_NAME)
    if value is None:
        raise RefusalError(f"{ROOTS_ENV_NAME} is not set")
    try:
        roots = json.loads(value)
    # Deep nesting exhausts the decoder's recursion limit.
    except (ValueError, RecursionError) as exc:
        raise RefusalError(f"{ROOTS_ENV_NAME} must be JSON, got {value!r}") from exc
    if not (
        isinstance(roots, list)
        and all(isinstance(root, str) and root.startswith("/") for root in roots)
    ):
        raise RefusalError(
            f"{ROOTS_ENV_NAME} must be a JSON array of absolute paths, got {value!r}"
        )
    return roots


def split_repo(args: Sequence[str]) -> tuple[str, list[str]]:
    """Return the value of the one --repo in args, and args without the pair.

    The search runs past any '--': blame's fixed words end in '--', and the
    core places --repo after the fixed words.

    Raises:
        RefusalError: --repo is missing, repeated, or has no value after it.
    """
    positions = [index for index, item in enumerate(args) if item == REPO_FLAG]
    if not positions:
        raise RefusalError(f"{REPO_FLAG} is missing")
    if len(positions) > 1:
        raise RefusalError(f"{REPO_FLAG} must appear once, got {len(positions)}")
    index = positions[0]
    if index + 1 == len(args):
        raise RefusalError(f"{REPO_FLAG} has no value")
    return args[index + 1], [*args[:index], *args[index + 2 :]]


def git_argv(repo: str, roots: Sequence[str], args: Sequence[str]) -> list[str]:
    """Return git's argv: the repo, two safe.directory values per root, helper, args.

    A root's trailing slashes are dropped, so /x/ trusts /x and /x/*; the root
    / stays /.
    """
    trusted = [
        item
        for root in map(_without_trailing_slashes, roots)
        for item in ("-c", f"safe.directory={root}", "-c", f"safe.directory={root}/*")
    ]
    return ["git", "-C", repo, *trusted, *CREDENTIAL_HELPER, *args]


def _without_trailing_slashes(root: str) -> str:
    return root.rstrip("/") or "/"


def wrapper(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Replace this process with git on the repository argv names, or refuse.

    argv is the wrapper's own argv, its name first. The function returns only
    on a refusal: it writes the reason to stderr and returns REFUSAL_EXIT.
    """
    try:
        roots = roots_from_env(environ)
        repo, args = split_repo(argv[1:])
    except RefusalError as refusal:
        print(f"{REFUSAL_PREFIX}{refusal}", file=sys.stderr)
        return REFUSAL_EXIT
    os.execvp("git", git_argv(repo, roots, args))  # noqa: S606, S607 - the wrapper exists to become git, found on PATH
