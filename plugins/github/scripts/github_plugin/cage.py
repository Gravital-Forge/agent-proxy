"""The gh-cage wrapper: a command action's gh, run only on a listed repository."""

import os
import sys
from collections.abc import Mapping, Sequence

from github_plugin import repos, scripts

REPO_FLAG = "--repo"


def repo_value(args: Sequence[str]) -> str:
    """Return the value after the one --repo in args.

    Raises:
        RefusalError: --repo is missing, repeated, or has no value after it.
    """
    positions = [index for index, item in enumerate(args) if item == REPO_FLAG]
    if not positions:
        raise scripts.RefusalError(f"{REPO_FLAG} is missing")
    if len(positions) > 1:
        raise scripts.RefusalError(
            f"{REPO_FLAG} must appear once, got {len(positions)}"
        )
    index = positions[0]
    if index + 1 == len(args):
        raise scripts.RefusalError(f"{REPO_FLAG} has no value")
    return args[index + 1]


def wrapper(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Replace this process with gh and argv's args, or refuse.

    argv is the wrapper's own argv, its name first. The repository after
    --repo must be in environ's GITHUB_REPOS. The function returns only on a
    refusal: it writes the reason to stderr and returns REFUSAL_EXIT.
    """
    args = argv[1:]
    try:
        repos.check_repo(repo_value(args), repos.allowed_repos(environ))
    except scripts.RefusalError as refusal:
        print(f"{scripts.REFUSAL_PREFIX}{refusal}", file=sys.stderr)
        return scripts.REFUSAL_EXIT
    os.execvp("gh", ["gh", *args])  # noqa: S606, S607 - the wrapper exists to become gh, found on PATH
