"""The script actions: each reads its input as JSON and returns git's result."""

import contextlib
import errno
import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from git_plugin import cage, rules

CONFIG_UNSET_EXIT = 1
DIFF_CHANGED_EXIT = 1
WRITE_FAILED_EXIT = 1
OUTPUT_DIR_NAME = ".agent-proxy"
# Ignores everything in the directory, this file included, so git reports
# nothing there and no repository needs an ignore entry of its own.
OUTPUT_DIR_IGNORE = b"*\n"
# What open() itself requests, so the umask governs the written file's mode.
OUTPUT_FILE_MODE = 0o666
# git elides the middle of a long path in a stat to fit 80 columns; these
# widths keep a path of up to 900 characters whole.
STAT_FORMAT = "--stat=1000,900"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# O_NONBLOCK keeps a planted fifo from blocking the open. No O_TRUNC: the file
# is emptied only after its descriptor passes the checks in _write_file.
FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK


class ScriptResult(TypedDict):
    exit_code: int
    stdout: str
    stderr: str


class DiffFileResult(TypedDict):
    exit_code: int
    stderr: str
    path: str
    stat: str


type ActionResult = ScriptResult | DiffFileResult


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
    output: NotRequired[str]


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
    return _script_result(_completed_git(repo, roots, args, env))


def _completed_git(
    repo: str,
    roots: Sequence[str],
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run git on repo with the roots trusted, env over the process environment."""
    environ = {**os.environ, **(env or {})}
    return subprocess.run(  # noqa: S603 - git_argv puts every input after git's own options
        cage.git_argv(repo, roots, args, environ),
        capture_output=True,
        check=False,
        env=environ,
    )


def _script_result(completed: subprocess.CompletedProcess[bytes]) -> ScriptResult:
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


def diff(arguments: DiffInput, roots: Sequence[str]) -> ActionResult:
    """Show the working tree's changes, or with staged the index's, against ref.

    With output, the diff goes undecoded into that file beneath the
    repository's .agent-proxy directory instead of into the result.

    Raises:
        RefusalError: output, its symlinks followed, does not lie strictly
            beneath the .agent-proxy directory.
    """
    selection = _diff_selection(arguments)
    if "output" in arguments:
        return _diff_to_file(arguments["repo"], roots, selection, arguments["output"])
    return run_git(arguments["repo"], roots, ["diff", *selection])


def _diff_selection(arguments: DiffInput) -> list[str]:
    """Return the arguments after git diff that choose what it compares."""
    selection: list[str] = []
    if arguments.get("staged"):
        selection.append("--cached")
    if "ref" in arguments:
        selection.append(arguments["ref"])
    if "files" in arguments:
        selection += ["--", *arguments["files"]]
    return selection


def _diff_to_file(
    repo: str, roots: Sequence[str], selection: Sequence[str], output: str
) -> ActionResult:
    """Write the diff of selection to output, and return its path and stat.

    The stat comes from a second git call on the same selection. A git
    failure in either call is returned as the result, and nothing is
    written; a failed write is exit code 1 with the reason on stderr.

    Raises:
        RefusalError: output does not resolve strictly beneath the
            .agent-proxy directory.
    """
    base, target = _output_target(repo, output)
    body = _completed_git(repo, roots, ["diff", *selection])
    if body.returncode != 0:
        return _script_result(body)
    summary = run_git(repo, roots, ["diff", STAT_FORMAT, *selection])
    if summary["exit_code"] != 0:
        return summary
    try:
        _write_output(base, target, body.stdout)
    except OSError as error:
        return {
            "exit_code": WRITE_FAILED_EXIT,
            "stdout": "",
            "stderr": f"diff could not be written to {target}: {error}\n",
        }
    return {
        "exit_code": body.returncode,
        "stderr": body.stderr.decode("utf-8", errors="replace"),
        "path": str(target),
        "stat": summary["stdout"],
    }


def _output_target(repo: str, output: str) -> tuple[Path, Path]:
    """Return repo's .agent-proxy directory, and output resolved beneath it.

    output is resolved with symlinks followed, so a symlink that leads out of
    the directory, or the directory itself as a symlink, is refused.

    Raises:
        RefusalError: output does not resolve strictly beneath the
            directory, or holds a NUL.
    """
    base = Path(os.path.realpath(repo)) / OUTPUT_DIR_NAME
    refusal = cage.RefusalError(f"output '{output}' resolves outside {base}")
    try:
        target = Path(os.path.realpath(base / output))
    except ValueError as error:
        raise refusal from error
    if base not in target.parents:
        raise refusal
    return base, target


def _write_output(base: Path, target: Path, body: bytes) -> None:
    """Write body to target, beneath base, through no symlink.

    Every step is taken relative to an open directory descriptor, each
    component opened with O_NOFOLLOW, so a name swapped for a symlink after
    _output_target's check fails the write instead of redirecting it. base is
    made on first use, with a .gitignore of its own; an existing base is left
    as found.

    Raises:
        OSError: a component is a symlink, the target is not a regular file
            or has other hard links, or a step fails.
    """
    *directories, name = target.relative_to(base).parts
    try:
        base.mkdir()
        created = True
    except FileExistsError:
        created = False
    dir_fd = os.open(base, DIRECTORY_FLAGS)
    try:
        if created:
            _write_file(dir_fd, ".gitignore", OUTPUT_DIR_IGNORE)
        for directory in directories:
            child_fd = _open_child_dir(dir_fd, directory)
            os.close(dir_fd)
            dir_fd = child_fd
        _write_file(dir_fd, name, body)
    finally:
        os.close(dir_fd)


def _open_child_dir(dir_fd: int, name: str) -> int:
    """Return a descriptor of the directory name beneath dir_fd, made if missing.

    Raises:
        OSError: name is a symlink, or cannot be made or opened.
    """
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, dir_fd=dir_fd)
    return os.open(name, DIRECTORY_FLAGS, dir_fd=dir_fd)


def _write_file(dir_fd: int, name: str, body: bytes) -> None:
    """Make name beneath dir_fd hold body alone, creating it when missing.

    The file is emptied only once its descriptor shows a regular file with no
    other hard link, so a name that aliases a file elsewhere is never
    truncated.

    Raises:
        OSError: name is a symlink, is not a regular file, has other hard
            links, or cannot be written.
    """
    fd = os.open(name, FILE_FLAGS, OUTPUT_FILE_MODE, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as handle:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", name)
        if info.st_nlink > 1:
            raise OSError(errno.EMLINK, "the file has other hard links", name)
        os.ftruncate(fd, 0)
        handle.write(body)


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
ACTIONS: Mapping[str, Callable[..., ActionResult]] = {
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


def run_script(name: str, stdin: bytes, environ: Mapping[str, str]) -> ActionResult:
    """Run the script action name on the JSON input stdin holds.

    A cage that holds GH_TOKEN is refused before the action runs unless
    GITHUB_REPOS parses, and a set GIT_SIGNING_KEY is refused unless it names a
    readable file. A refusal is returned as the result: exit code 2, no stdout,
    and the refusal's reason on stderr.
    """
    try:
        roots = cage.roots_from_env(environ)
        rules.check_token_cage(environ)
        cage.check_signing_key(environ)
        return ACTIONS[name](json.loads(stdin), roots)
    except cage.RefusalError as refusal:
        return {
            "exit_code": cage.REFUSAL_EXIT,
            "stdout": "",
            "stderr": f"{cage.REFUSAL_PREFIX}{refusal}\n",
        }
