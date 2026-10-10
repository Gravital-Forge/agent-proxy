"""The credential helper git runs under the name git-credential-agent-proxy.

git runs it with credential.useHttpPath set, so a request carries the path of
the URL git connects to, after any insteadOf or pushurl. The helper answers
with GH_TOKEN only when that path names a repository GITHUB_REPOS lists.
"""

import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from git_plugin import cage, rules

GET_OPERATION = "get"
USERNAME = "x-access-token"
PATH_KEY = "path"
GIT_SUFFIX = ".git"


def helper(argv: Sequence[str], stdin: TextIO, environ: Mapping[str, str]) -> int:
    """Answer git's credential request on stdin, and return 0.

    argv is the helper's own argv, its name first and git's operation second.
    The request is read to its end whatever the operation. Only get gets an
    answer: the token, or on a refusal, quit=1 with the reason on stderr.
    """
    request = stdin.read()
    operation = argv[1] if len(argv) > 1 else None
    if operation != GET_OPERATION:
        return 0
    try:
        token = _token_for(_requested_path(request), environ)
    except cage.RefusalError as refusal:
        print(f"{cage.REFUSAL_PREFIX}{refusal}", file=sys.stderr)
        print("quit=1")
        return 0
    print(f"username={USERNAME}")
    print(f"password={token}")
    return 0


def _requested_path(request: str) -> str | None:
    """Return the value of the request's path line, or None if it has none."""
    for line in request.splitlines():
        key, _, value = line.partition("=")
        if key == PATH_KEY:
            return value
    return None


def _token_for(path: str | None, environ: Mapping[str, str]) -> str:
    """Return GH_TOKEN when path names a repository GITHUB_REPOS lists.

    The repository is path's first two segments, with one trailing .git
    dropped from the second; the segments after them, such as LFS's
    info/lfs, play no part.

    Raises:
        RefusalError: path is missing or has fewer than two segments, the
            list refuses or does not name the repository, or GH_TOKEN is
            unset or empty.
    """
    if path is None:
        raise cage.RefusalError(
            "credential request names no path; "
            "git sends one only under credential.useHttpPath"
        )
    segments = path.split("/")
    if len(segments) < 2:
        raise cage.RefusalError(f"credential path '{path}' names no owner/name")
    owner, name = segments[0], segments[1].removesuffix(GIT_SUFFIX)
    rules.check_repo(f"{owner}/{name}", rules.allowed_repos(environ))
    token = environ.get(rules.CREDENTIAL_ENV_NAME, "")
    if not token:
        raise cage.RefusalError(f"{rules.CREDENTIAL_ENV_NAME} is not set or empty")
    return token
