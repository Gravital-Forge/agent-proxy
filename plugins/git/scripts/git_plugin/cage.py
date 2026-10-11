"""The git-cage wrapper: git on one repository, with the cage's roots trusted."""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from git_plugin import rules

REFUSAL_EXIT = 2
REFUSAL_PREFIX = "git plugin refused: "
ROOTS_ENV_NAME = "AGENT_PROXY_ROOTS"
REPO_FLAG = "--repo"
HOOKS_OFF = ("-c", "core.hooksPath=/dev/null")
CREDENTIAL_HELPER_PATH = "/plugin/bin/git-credential-agent-proxy"
CREDENTIAL_HELPER = (
    "-c",
    "credential.helper=",
    "-c",
    f"credential.helper={CREDENTIAL_HELPER_PATH}",
    "-c",
    "credential.useHttpPath=true",
)
SIGNING_KEY_ENV_NAME = "GIT_SIGNING_KEY"
SIGNING_PROGRAM_PATH = "/usr/bin/ssh-keygen"
SIGNING_OFF = ("-c", "commit.gpgSign=false")


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


def git_argv(
    repo: str,
    roots: Sequence[str],
    args: Sequence[str],
    environ: Mapping[str, str],
) -> list[str]:
    """Return git's argv: repo, safe.directory values, hooks off, helper, signing, args.

    Each root gives two safe.directory values. A root's trailing slashes are
    dropped, so /x/ trusts /x and /x/*; the root / stays /. The signing values
    come from GIT_SIGNING_KEY in environ.
    """
    trusted = [
        item
        for root in map(_without_trailing_slashes, roots)
        for item in ("-c", f"safe.directory={root}", "-c", f"safe.directory={root}/*")
    ]
    return [
        "git",
        "-C",
        repo,
        *trusted,
        *HOOKS_OFF,
        *CREDENTIAL_HELPER,
        *_signing_values(environ),
        *args,
    ]


def _signing_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Return the -c values that sign with GIT_SIGNING_KEY's key, or turn signing off.

    Signing is off when the variable is unset, whatever the repository's
    config says.
    """
    key = environ.get(SIGNING_KEY_ENV_NAME)
    if key is None:
        return SIGNING_OFF
    return (
        "-c",
        "gpg.format=ssh",
        "-c",
        f"gpg.ssh.program={SIGNING_PROGRAM_PATH}",
        "-c",
        f"user.signingKey={key}",
        "-c",
        "commit.gpgSign=true",
    )


def check_signing_key(environ: Mapping[str, str]) -> None:
    """Refuse a GIT_SIGNING_KEY that is set but names no readable file.

    An unset variable passes: it turns signing off.

    Raises:
        RefusalError: the value is not an absolute path, or names no
            readable regular file.
    """
    key = environ.get(SIGNING_KEY_ENV_NAME)
    if key is None:
        return
    if not key.startswith("/"):
        raise RefusalError(
            f"{SIGNING_KEY_ENV_NAME} must be an absolute path, got {key!r}"
        )
    if not (Path(key).is_file() and os.access(key, os.R_OK)):
        raise RefusalError(
            f"{SIGNING_KEY_ENV_NAME} names no readable file: {key}; "
            "the grant mounts the key there as a file credential"
        )


def _without_trailing_slashes(root: str) -> str:
    return root.rstrip("/") or "/"


def wrapper(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Replace this process with git on the repository argv names, or refuse.

    argv is the wrapper's own argv, its name first. A cage that holds
    GH_TOKEN is refused unless GITHUB_REPOS parses, and a set GIT_SIGNING_KEY
    is refused unless it names a readable file. The function returns only on
    a refusal: it writes the reason to stderr and returns REFUSAL_EXIT.
    """
    try:
        roots = roots_from_env(environ)
        repo, args = split_repo(argv[1:])
        rules.check_token_cage(environ)
        check_signing_key(environ)
    except RefusalError as refusal:
        print(f"{REFUSAL_PREFIX}{refusal}", file=sys.stderr)
        return REFUSAL_EXIT
    os.execvp("git", git_argv(repo, roots, args, environ))  # noqa: S606, S607 - the wrapper exists to become git, found on PATH
