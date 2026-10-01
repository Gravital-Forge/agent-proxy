"""The script actions: each reads its input as JSON and returns git's result."""

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import NotRequired, TypedDict

from git_plugin import cage

CONFIG_UNSET_EXIT = 1
DIFF_CHANGED_EXIT = 1


class ScriptResult(TypedDict):
    exit_code: int
    stdout: str
    stderr: str


class FilesInput(TypedDict):
    repo: str
    files: list[str]


class RestoreInput(TypedDict):
    repo: str
    files: list[str]
    staged: NotRequired[bool]


class DiffInput(TypedDict):
    repo: str
    staged: NotRequired[bool]
    ref: NotRequired[str]
    files: NotRequired[list[str]]


class CommitInput(TypedDict):
    repo: str
    message: str


class PatternsInput(TypedDict):
    repo: str
    patterns: list[str]


class _GitFailedError(Exception):
    """git failed in a step before an action's last; its result is the action's."""

    def __init__(self, result: ScriptResult) -> None:
        super().__init__(result["stderr"])
        self.result = result


def run_git(
    repo: str,
    roots: Sequence[str],
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> ScriptResult:
    """Run git on repo with the roots trusted, and return its exit code and streams.

    env is laid over the process environment for this run. Each stream is
    decoded as UTF-8, a byte that is not UTF-8 becoming U+FFFD.
    """
    completed = subprocess.run(  # noqa: S603 - git_argv puts every input after git's own options
        cage.git_argv(repo, roots, args),
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
    }


def add(arguments: FilesInput, roots: Sequence[str]) -> ScriptResult:
    """Stage the files."""
    return run_git(arguments["repo"], roots, ["add", "--", *arguments["files"]])


def rm(arguments: FilesInput, roots: Sequence[str]) -> ScriptResult:
    """Remove the files from the index and the working tree."""
    return run_git(arguments["repo"], roots, ["rm", "--", *arguments["files"]])


def restore(arguments: RestoreInput, roots: Sequence[str]) -> ScriptResult:
    """Restore the files in the working tree, or with staged, in the index."""
    staged = ["--staged"] if arguments.get("staged") else []
    return run_git(
        arguments["repo"], roots, ["restore", *staged, "--", *arguments["files"]]
    )


def diff(arguments: DiffInput, roots: Sequence[str]) -> ScriptResult:
    """Show the working tree's changes, or with staged the index's, against ref."""
    args = ["diff"]
    if arguments.get("staged"):
        args.append("--cached")
    if "ref" in arguments:
        args.append(arguments["ref"])
    if "files" in arguments:
        args += ["--", *arguments["files"]]
    return run_git(arguments["repo"], roots, args)


def lfs_track(arguments: PatternsInput, roots: Sequence[str]) -> ScriptResult:
    """Track the patterns with git LFS."""
    return run_git(
        arguments["repo"], roots, ["lfs", "track", "--", *arguments["patterns"]]
    )


def lfs_untrack(arguments: PatternsInput, roots: Sequence[str]) -> ScriptResult:
    """Stop tracking the patterns with git LFS."""
    return run_git(
        arguments["repo"], roots, ["lfs", "untrack", "--", *arguments["patterns"]]
    )


def commit(arguments: CommitInput, roots: Sequence[str]) -> ScriptResult:
    """Commit what is staged, as the identity the repository's config names.

    A git failure in a check before the commit is returned as the result.

    Raises:
        RefusalError: user.name or user.email is unset or empty, or nothing
            is staged.
    """
    repo = arguments["repo"]
    try:
        name = _config_value(repo, roots, "user.name")
        email = _config_value(repo, roots, "user.email")
        _require_staged(repo, roots)
    except _GitFailedError as failure:
        return failure.result
    identity = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    return run_git(repo, roots, ["commit", "-m", arguments["message"]], identity)


def _config_value(repo: str, roots: Sequence[str], key: str) -> str:
    """Return the value of key in repo's config.

    Raises:
        RefusalError: key is unset or empty.
        _GitFailedError: git failed other than by finding key unset.
    """
    result = run_git(repo, roots, ["config", "--get", key])
    if result["exit_code"] not in (0, CONFIG_UNSET_EXIT):
        raise _GitFailedError(result)
    value = result["stdout"].strip()
    if not value:
        raise cage.RefusalError(f"git config {key} is not set")
    return value


def _require_staged(repo: str, roots: Sequence[str]) -> None:
    """Return if repo's index differs from HEAD.

    Raises:
        RefusalError: the index holds no change.
        _GitFailedError: git could not compare the index with HEAD.
    """
    result = run_git(repo, roots, ["diff", "--cached", "--quiet"])
    if result["exit_code"] == 0:
        raise cage.RefusalError("nothing staged; stage changes with git add")
    if result["exit_code"] != DIFF_CHANGED_EXIT:
        raise _GitFailedError(result)


# Each takes its own input type, which the core validated against the action's
# input_schema before the cage started.
ACTIONS: Mapping[str, Callable[..., ScriptResult]] = {
    "add": add,
    "rm": rm,
    "restore": restore,
    "commit": commit,
    "diff": diff,
    "lfs_track": lfs_track,
    "lfs_untrack": lfs_untrack,
}


def run_script(name: str, stdin: bytes, environ: Mapping[str, str]) -> ScriptResult:
    """Run the script action name on the JSON input stdin holds.

    A refusal is returned as the result: exit code 2, no stdout, and the
    refusal's reason on stderr.
    """
    try:
        roots = cage.roots_from_env(environ)
        return ACTIONS[name](json.loads(stdin), roots)
    except cage.RefusalError as refusal:
        return {
            "exit_code": cage.REFUSAL_EXIT,
            "stdout": "",
            "stderr": f"{cage.REFUSAL_PREFIX}{refusal}\n",
        }
