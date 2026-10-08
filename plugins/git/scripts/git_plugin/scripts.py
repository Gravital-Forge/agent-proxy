"""The script actions: each reads its input as JSON and returns git's result."""

import contextlib
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
STAT_OPTION = "--stat=1000,900"
OUTPUT_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
OUTPUT_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
OUTPUT_FILE_MODE = 0o666


class ScriptResult(TypedDict):
    exit_code: int
    stdout: str
    stderr: str


class DiffFileResult(TypedDict):
    exit_code: int
    stderr: str
    path: str
    stat: str


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
    return _result(_git_process(repo, roots, args, env))


def _git_process(
    repo: str,
    roots: Sequence[str],
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run git on repo with the roots trusted, env laid over the environment."""
    return subprocess.run(  # noqa: S603 - git_argv puts every input after git's own options
        cage.git_argv(repo, roots, args),
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _result(completed: subprocess.CompletedProcess[bytes]) -> ScriptResult:
    """Return the finished git's exit code and streams, each decoded as run_git says."""
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


def diff(arguments: DiffInput, roots: Sequence[str]) -> ScriptResult | DiffFileResult:
    """Show the working tree's changes, or with staged the index's, against ref.

    With output, the diff goes to that path beneath the repository's
    .agent-proxy directory instead, and the result holds the path written and
    a --stat of the same selection. A git failure is returned as the result.

    Raises:
        RefusalError: output, its symlinks followed, does not lie beneath
            .agent-proxy.
    """
    selection = _diff_selection(arguments)
    if "output" not in arguments:
        return run_git(arguments["repo"], roots, ["diff", *selection])
    return _diff_to_file(arguments["repo"], roots, selection, arguments["output"])


def _diff_selection(arguments: DiffInput) -> list[str]:
    """Return git diff's arguments after diff for the selection arguments name."""
    selection = ["--cached"] if arguments.get("staged") else []
    if "ref" in arguments:
        selection.append(arguments["ref"])
    if "files" in arguments:
        selection += ["--", *arguments["files"]]
    return selection


def _diff_to_file(
    repo: str, roots: Sequence[str], selection: Sequence[str], output: str
) -> ScriptResult | DiffFileResult:
    """Write the selection's diff to output beneath repo's .agent-proxy.

    A failed write is a result with exit code 1 and the reason on stderr.

    Raises:
        RefusalError: output does not lie beneath .agent-proxy.
    """
    base, target = _output_target(repo, output)
    shown = _git_process(repo, roots, ["diff", *selection])
    if shown.returncode != 0:
        return _result(shown)
    summary = run_git(repo, roots, ["diff", STAT_OPTION, *selection])
    if summary["exit_code"] != 0:
        return summary
    try:
        _write_output(base, target.relative_to(base).parts, shown.stdout)
    except OSError as error:
        return {
            "exit_code": WRITE_FAILED_EXIT,
            "stdout": "",
            "stderr": f"diff could not be written to {target}: {error}\n",
        }
    return {
        "exit_code": shown.returncode,
        "stderr": shown.stderr.decode("utf-8", errors="replace"),
        "path": str(target),
        "stat": summary["stdout"],
    }


def _output_target(repo: str, output: str) -> tuple[Path, Path]:
    """Return repo's .agent-proxy directory and the path output names beneath it.

    repo is resolved, and so is output beneath it, every symlink followed.

    Raises:
        RefusalError: output resolves to the directory or outside it, or
            holds a byte realpath cannot read.
    """
    base = Path(os.path.realpath(repo)) / OUTPUT_DIR_NAME
    refusal = cage.RefusalError(f"output '{output}' resolves outside {base}")
    try:
        target = Path(os.path.realpath(base / output))
    except ValueError as exc:
        raise refusal from exc
    if target == base or not target.is_relative_to(base):
        raise refusal
    return base, target


def _write_output(base: Path, parts: Sequence[str], data: bytes) -> None:
    """Write data to the file parts names beneath base, following no symlink.

    base, and each directory parts names, is made when missing; base gains a
    .gitignore of '*' only when this call makes it.

    Raises:
        OSError: a component is a symlink or not a directory, the file is not
            a regular file or has another hard link, or a step failed.
    """
    _make_output_dir(base)
    with contextlib.ExitStack() as descriptors:
        dir_fd = os.open(base, OUTPUT_DIR_FLAGS)
        descriptors.callback(os.close, dir_fd)
        for name in parts[:-1]:
            dir_fd = _open_child_dir(dir_fd, name)
            descriptors.callback(os.close, dir_fd)
        _write_file(dir_fd, parts[-1], data)


def _make_output_dir(base: Path) -> None:
    """Make base holding a .gitignore of '*', or leave an existing base as found."""
    try:
        base.mkdir()
    except FileExistsError:
        return
    (base / ".gitignore").write_text("*\n")


def _open_child_dir(dir_fd: int, name: str) -> int:
    """Open the directory name in dir_fd, made when missing, following no symlink."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, dir_fd=dir_fd)
    return os.open(name, OUTPUT_DIR_FLAGS, dir_fd=dir_fd)


def _write_file(dir_fd: int, name: str, data: bytes) -> None:
    """Replace the content of the file name in dir_fd with data.

    The file is emptied only once it is known to be a regular file with no
    other hard link, so a file linked from elsewhere keeps its bytes.

    Raises:
        OSError: name is a symlink, is not a regular file, or has another
            hard link.
    """
    fd = os.open(name, OUTPUT_FILE_FLAGS, OUTPUT_FILE_MODE, dir_fd=dir_fd)
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise OSError(f"{name} is not a regular file")
        if status.st_nlink > 1:
            raise OSError(f"{name} has another hard link")
        os.ftruncate(fd, 0)
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(fd, remaining) :]
    finally:
        os.close(fd)


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

    The allowed branch prefixes come from GIT_BRANCH_PREFIXES in the
    environment, which the grant's env sets; they are read before any git
    call. The refspec names both sides and carries no '+', so no push refspec
    or mirror setting in the repository's config can force the update. A git
    failure before the ref push is returned as the result.

    Raises:
        RefusalError: GIT_BRANCH_PREFIXES names no prefix, the current branch
            is protected or begins with no allowed prefix, or HEAD is
            detached.
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

    The allowed branch prefixes come from GIT_BRANCH_PREFIXES in the
    environment, which the grant's env sets; they are read before any check,
    so a checkout of main is refused without them too.

    Raises:
        RefusalError: GIT_BRANCH_PREFIXES names no prefix, or target is not
            protected and begins with no allowed prefix.
    """
    prefixes = rules.branch_prefixes(os.environ)
    target = arguments["target"]
    rules.check_checkout_target(target, prefixes)
    create = ["-b"] if arguments.get("create") else []
    return run_git(arguments["repo"], roots, ["checkout", *create, target])


def worktree(arguments: WorktreeInput, roots: Sequence[str]) -> ScriptResult:
    """Add, list, or remove a linked worktree of repo.

    add with a branch makes that branch, or with create_branch false checks
    out the branch that exists; add with no branch lets git name a new branch
    after the path's last component. A missing path is left to git to refuse.

    Raises:
        RefusalError: add names a path that is repo or lies beneath it.
    """
    repo = arguments["repo"]
    path = [arguments["path"]] if "path" in arguments else []
    if arguments["action"] == "list":
        return run_git(repo, roots, ["worktree", "list"])
    if arguments["action"] == "remove":
        return run_git(repo, roots, ["worktree", "remove", *path])
    return run_git(repo, roots, ["worktree", "add", *_worktree_add_args(arguments)])


def _worktree_add_args(arguments: WorktreeInput) -> list[str]:
    """Return git worktree add's arguments after add, in git-sidecar's order.

    Raises:
        RefusalError: path is repo or lies beneath it, symlinks resolved.
    """
    if "path" not in arguments:
        return []
    path = arguments["path"]
    if Path(os.path.realpath(path)).is_relative_to(os.path.realpath(arguments["repo"])):
        raise cage.RefusalError(
            f"worktree path '{path}' is inside the repository it comes from"
        )
    if "branch" not in arguments:
        return [path]
    if arguments.get("create_branch", True):
        return ["-b", arguments["branch"], path]
    return ["--", path, arguments["branch"]]


# Each takes its own input type, which the core validated against the action's
# input_schema before the cage started.
ACTIONS: Mapping[str, Callable[..., ScriptResult | DiffFileResult]] = {
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


def run_script(
    name: str, stdin: bytes, environ: Mapping[str, str]
) -> ScriptResult | DiffFileResult:
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
