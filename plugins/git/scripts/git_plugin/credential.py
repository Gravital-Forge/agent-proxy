"""The credential helper git runs under the name git-credential-agent-proxy.

git runs it with credential.useHttpPath set, so a request carries the path of
the URL git connects to, after any insteadOf or pushurl. The helper answers
with GH_TOKEN only when the request is for https://github.com and that path
names a repository GITHUB_REPOS lists.
"""

import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from git_plugin import cage, rules

GET_OPERATION = "get"
USERNAME = "x-access-token"
PROTOCOL_KEY = "protocol"
HOST_KEY = "host"
PATH_KEY = "path"
ANSWERED_PROTOCOL = "https"
ANSWERED_HOST = "github.com"
GIT_SUFFIX = ".git"
DOT_SEGMENTS = frozenset({".", ".."})


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
        _check_origin(
            _requested_value(request, PROTOCOL_KEY),
            _requested_value(request, HOST_KEY),
        )
        token = _token_for(_requested_value(request, PATH_KEY), environ)
    except cage.RefusalError as refusal:
        print(f"{cage.REFUSAL_PREFIX}{refusal}", file=sys.stderr)
        print("quit=1")
        return 0
    print(f"username={USERNAME}")
    print(f"password={token}")
    return 0


def _requested_value(request: str, wanted: str) -> str | None:
    """Return the value of the request's first line for key wanted, or None."""
    for line in request.splitlines():
        key, _, value = line.partition("=")
        if key == wanted:
            return value
    return None


def _check_origin(protocol: str | None, host: str | None) -> None:
    """Return if the request is for https and github.com, with no port.

    Raises:
        RefusalError: the protocol or the host is missing or is another.
    """
    if protocol != ANSWERED_PROTOCOL or host != ANSWERED_HOST:
        raise cage.RefusalError(
            f"credential request names protocol {protocol!r} and host {host!r}; "
            f"the git plugin answers only protocol {ANSWERED_PROTOCOL!r} "
            f"and host {ANSWERED_HOST!r}"
        )


def _token_for(path: str | None, environ: Mapping[str, str]) -> str:
    """Return GH_TOKEN when path names a repository GITHUB_REPOS lists.

    Raises:
        RefusalError: path is missing or names no owner/name, the list
            refuses or does not name the repository, or GH_TOKEN is unset
            or empty.
    """
    if path is None:
        raise cage.RefusalError(
            "credential request names no path; "
            "git sends one only under credential.useHttpPath"
        )
    owner, name = _repository_of(path)
    rules.check_repo(f"{owner}/{name}", rules.allowed_repos(environ))
    token = environ.get(rules.CREDENTIAL_ENV_NAME, "")
    if not token:
        raise cage.RefusalError(f"{rules.CREDENTIAL_ENV_NAME} is not set or empty")
    return token


def _repository_of(path: str) -> tuple[str, str]:
    """Return the owner and name that path's first two segments give.

    One trailing .git is dropped from the name. The segments after the two,
    such as LFS's info/lfs/objects/batch, play no part. git hands the helper
    the path percent-decoded but sends the request still encoded, so an
    encoded ../ reaches the helper as a dot segment the server may resolve
    to another repository.

    Raises:
        RefusalError: path has fewer than two segments, a . or .. segment
            anywhere, or a first or second segment that fails its pattern.
    """
    segments = path.split("/")
    if len(segments) >= 2 and DOT_SEGMENTS.isdisjoint(segments):
        owner, name = segments[0], segments[1].removesuffix(GIT_SUFFIX)
        if rules.OWNER_PATTERN.fullmatch(owner) and rules.NAME_PATTERN.fullmatch(name):
            return owner, name
    raise cage.RefusalError(f"credential path '{path}' names no owner/name")
