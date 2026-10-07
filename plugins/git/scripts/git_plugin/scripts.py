"""The script actions: each reads its input as JSON and returns git's result."""

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from git_plugin import cage, rules

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


class RepoInput(TypedDict):
    repo: str


class CheckoutInput(TypedDict):
    repo: str
    target: str
    create: NotRequired[bool]


class WorktreeInput(TypedDict):
    repo: str
    action: Literal["add", "list", "remove"]
    path: NotRequired[str]
    branch: NotRequired[str]
    create_branch: NotRequired[bool]


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
    """Commit what is staged, as the repository's identity or else the grant's.

    The name comes from user.name in the repository's config, or else from
    GIT_AUTHOR_NAME in the environment, which the grant's env sets. The email
    comes from user.email, or else from GIT_AUTHOR_EMAIL. A git failure in a
    check before the commit is returned as the result.

    Raises:
        RefusalError: the name or the email is unset or empty in both
            sources, or nothing is staged.
    """
    repo = arguments["repo"]
    try:
        name = _identity_value(repo, roots, "user.name", "GIT_AUTHOR_NAME")
        email = _identity_value(repo, roots, "user.email", "GIT_AUTHOR_EMAIL")
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


def _identity_value(repo: str, roots: Sequence[str], key: str, env_name: str) -> str:
    """Return key's value in repo's config, or else env_name's in the environment.

    An empty value counts as unset in both sources.

    Raises:
        RefusalError: key and env_name are both unset or empty.
        _GitFailedError: git failed other than by finding key unset.
    """
    result = run_git(repo, roots, ["config", "--get", key])
    if result["exit_code"] not in (0, CONFIG_UNSET_EXIT):
        raise _GitFailedError(result)
    value = result["stdout"].strip() or os.environ.get(env_name, "")
    if not value:
        raise cage.RefusalError(
            f"git config {key} is not set, nor is {env_name} in the grant's env"
        )
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


def push(arguments: RepoInput, roots: Sequence[str]) -> ScriptResult:
    """Push the current branch to the same name on origin, its LFS objects first.

    The allowed prefixes come from GIT_BRANCH_PREFIXES in the environment,
    which the grant's env sets; they are read before git runs. The refspec
    names both sides and carries no '+', so no push refspec or mirror setting
    in the repository's config can force the update. A git failure before the
    ref push is returned as the result.

    Raises:
        RefusalError: GIT_BRANCH_PREFIXES names no prefix, the current branch
            is protected or matches no prefix, or HEAD is detached.
    """
    repo = arguments["repo"]
    prefixes = rules.branch_prefixes(os.environ)
    try:
        branch = _current_branch(repo, roots)
        rules.check_push_branch(branch, prefixes)
        _push_lfs_objects(repo, roots, branch)
    except _GitFailedError as failure:
        return failure.result
    ref = f"refs/heads/{branch}"
    return run_git(repo, roots, ["push", "-u", "origin", f"{ref}:{ref}"])


def _current_branch(repo: str, roots: Sequence[str]) -> str:
    """Return the name of repo's checked-out branch, or HEAD when it is detached.

    Raises:
        _GitFailedError: git could not read HEAD.
    """
    result = run_git(repo, roots, ["rev-parse", "--abbrev-ref", "HEAD"])
    if result["exit_code"] != 0:
        raise _GitFailedError(result)
    return result["stdout"].strip()


def _push_lfs_objects(repo: str, roots: Sequence[str], branch: str) -> None:
    """Upload branch's LFS objects to origin when repo tracks any LFS file.

    A failed ls-files counts as no LFS file, as git-sidecar has it.

    Raises:
        _GitFailedError: the LFS push failed.
    """
    files = run_git(repo, roots, ["lfs", "ls-files", "--name-only"])
    if files["exit_code"] != 0 or not files["stdout"].strip():
        return
    result = run_git(repo, roots, ["lfs", "push", "origin", branch])
    if result["exit_code"] != 0:
        raise _GitFailedError(result)


def checkout(arguments: CheckoutInput, roots: Sequence[str]) -> ScriptResult:
    """Switch to target, or with create, make the branch target and switch to it.

    The allowed prefixes come from GIT_BRANCH_PREFIXES in the environment,
    which the grant's env sets; they are read before git runs.

    Raises:
        RefusalError: GIT_BRANCH_PREFIXES names no prefix, or target is not
            protected and matches no prefix.
    """
    target = arguments["target"]
    rules.check_checkout_target(target, rules.branch_prefixes(os.environ))
    create = ["-b"] if arguments.get("create") else []
    return run_git(arguments["repo"], roots, ["checkout", *create, target])


def worktree(arguments: WorktreeInput, roots: Sequence[str]) -> ScriptResult:
    """Add, list, or remove a linked worktree of repo.

    add with branch creates that branch, or with create_branch false attaches
    the existing branch of that name; add without branch lets git name a new
    branch after the path's last component. add or remove without path runs
    git without one, and git's error is the result.

    Raises:
        RefusalError: add names a path that resolves to repo or beneath it.
    """
    repo = arguments["repo"]
    action = arguments["action"]
    path = arguments.get("path")
    args = ["worktree", action]
    if action == "add" and path is not None:
        _check_outside_repo(repo, path)
        args += _worktree_add_args(
            path, arguments.get("branch"), arguments.get("create_branch", True)
        )
    elif action == "remove" and path is not None:
        args.append(path)
    return run_git(repo, roots, args)


def _check_outside_repo(repo: str, path: str) -> None:
    """Return if path, its symlinks followed, lies outside repo.

    Raises:
        RefusalError: path resolves to repo or beneath it.
    """
    if Path(os.path.realpath(path)).is_relative_to(os.path.realpath(repo)):
        raise cage.RefusalError(
            f"worktree path '{path}' is inside the repository it comes from"
        )


def _worktree_add_args(path: str, branch: str | None, create_branch: bool) -> list[str]:
    """Return the arguments that follow git worktree add.

    With create_branch false, '--' makes git look branch up as a name even
    when it reads like an option, so '--detach' fails instead of detaching.
    """
    if branch is None:
        return [path]
    if create_branch:
        return ["-b", branch, path]
    return ["--", path, branch]


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
    "push": push,
    "checkout": checkout,
    "worktree": worktree,
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
