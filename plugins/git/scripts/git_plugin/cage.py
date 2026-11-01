"""The git-cage wrapper: git on one repository, with the cage's roots trusted."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
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
# The transports that run a program, or a local upload-pack or receive-pack
# that git starts without the repo-local environment, so without these -c
# values, inside the cage.
PROTOCOL_PINS = (
    "-c",
    "protocol.file.allow=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "protocol.fd.allow=never",
)
SCRATCH_PARENT = "/tmp"  # noqa: S108 - the path is inside the cage, on its own tmpfs
SCRATCH_PREFIX = "git-isolation-"
SCRATCH_HEAD = "ref: refs/heads/.invalid\n"
LINKED_ENTRIES = ("worktrees", "info", "lfs")
GITDIR_PREFIX = "gitdir: "
LFS_PROGRAM_PATH = "/usr/bin/git-lfs"
# The keys of git lfs install --system, which GIT_CONFIG_SYSTEM=/dev/null hides.
LFS_FILTER = (
    ("clean", f"{LFS_PROGRAM_PATH} clean -- %f"),
    ("smudge", f"{LFS_PROGRAM_PATH} smudge -- %f"),
    ("process", f"{LFS_PROGRAM_PATH} filter-process"),
    ("required", "true"),
)
# (section, variable) of each key copied from the real config; none names a
# program.
TRANSPORT_KEYS = frozenset(
    {
        ("remote", "url"),
        ("remote", "fetch"),
        ("remote", "pushurl"),
        ("branch", "remote"),
        ("branch", "merge"),
    }
)
# Extension keys as git lists them, in lower case.
REF_STORAGE = "extensions.refstorage"
WORKTREE_CONFIG = "extensions.worktreeconfig"
GIT_FALSE = frozenset({"false", "no", "off", "0", ""})
# Each reason follows the repository's path in a shape refusal.
SHALLOW_REASON = "is a shallow clone; the git plugin does not support shallow clones"
REFTABLE_REASON = "uses the reftable ref format; the git plugin supports only files"
REF_STORE_REASON = (
    "sets extensions.refstorage to a ref store other than files; "
    "the git plugin supports only files"
)
SUBMODULES_REASON = "has submodules; the git plugin does not support submodules"
PARTIAL_REASON = "is a partial clone; the git plugin does not support partial clones"
COMMITTER_FROM_AUTHOR = (
    ("GIT_COMMITTER_NAME", "GIT_AUTHOR_NAME"),
    ("GIT_COMMITTER_EMAIL", "GIT_AUTHOR_EMAIL"),
)
NO_GLOBAL_OR_SYSTEM = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


class RefusalError(Exception):
    """A call the plugin refuses; the message is the reason."""


@dataclass(frozen=True)
class GitDirs:
    """A checkout's real git directories, as its own files name them.

    git_dir is the per-checkout directory, which holds HEAD and the index;
    common_dir is the main .git, which holds the config, objects, and refs.
    """

    git_dir: Path
    common_dir: Path


type ConfigEntry = tuple[str, str | None]


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
    """Return git's argv: repo, trusted roots, hooks off, helper, signing, pins, args.

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
        *PROTOCOL_PINS,
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


def build_isolation(
    repo: str, environ: Mapping[str, str], scratch_parent: str = SCRATCH_PARENT
) -> dict[str, str]:
    """Return environ as git on repo runs in it, reading no config the agent wrote.

    git reads its config from a new scratch common directory beneath
    scratch_parent, while GIT_DIR, the objects, and through
    extensions.refStorage the refs stay repo's real ones. The scratch config
    holds the fixed keys and the transport keys of the real config; the
    global and system config are /dev/null. GIT_ALLOW_PROTOCOL is dropped, so
    the -c protocol pins govern. An unset committer takes the grant's author.

    Both the wrapper and the script actions call this function by its module
    name, so a test can replace it.

    Raises:
        RefusalError: repo is a directory with no .git, its .git file names
            no git directory, a file the real git directories are read
            from or the real config cannot be read, or repo has a shape
            check_shape refuses.
    """
    dirs = real_git_dirs(repo)
    entries = real_config_entries(dirs.common_dir, environ)
    check_shape(repo, dirs, entries)
    scratch = _make_scratch(dirs, entries, scratch_parent)
    env = dict(environ)
    env.pop("GIT_ALLOW_PROTOCOL", None)
    env |= {
        "GIT_DIR": str(dirs.git_dir),
        "GIT_COMMON_DIR": str(scratch),
        "GIT_OBJECT_DIRECTORY": str(dirs.common_dir / "objects"),
        **NO_GLOBAL_OR_SYSTEM,
    }
    for committer, author in COMMITTER_FROM_AUTHOR:
        if committer not in env and author in env:
            env[committer] = env[author]
    return env


def real_git_dirs(repo: str) -> GitDirs:
    """Return repo's real git directories, read from its files with no git run.

    <repo>/.git is the git dir unless it is a file; a .git file names the git
    dir after 'gitdir: ', relative to repo. The git dir's commondir file names
    the common dir, relative to the git dir; with no such file the git dir is
    the common dir. A repo that does not exist gets <repo>/.git, for git to
    report the missing directory.

    Raises:
        RefusalError: repo is a directory with no .git, the .git file
            names no git directory, or a file cannot be read.
    """
    dot_git = Path(repo) / ".git"
    if Path(repo).is_dir() and not dot_git.exists():
        raise RefusalError(
            f"{repo} has no .git; "
            "the repository path must be the top level of a checkout"
        )
    git_dir = dot_git
    if dot_git.is_file():
        named = _read_path_file(dot_git)
        if not named.startswith(GITDIR_PREFIX):
            raise RefusalError(f"{dot_git} names no git directory")
        git_dir = Path(repo) / named.removeprefix(GITDIR_PREFIX)
    commondir = git_dir / "commondir"
    common_dir = git_dir
    if commondir.is_file():
        common_dir = git_dir / _read_path_file(commondir)
    return GitDirs(
        git_dir=Path(os.path.realpath(git_dir)),
        common_dir=Path(os.path.realpath(common_dir)),
    )


def _read_path_file(path: Path) -> str:
    """Return the path the file at path holds, its line ending dropped.

    Raises:
        RefusalError: the file cannot be read.
    """
    try:
        return os.fsdecode(path.read_bytes()).rstrip("\r\n")
    except OSError as error:
        raise RefusalError(f"{path} could not be read: {error}") from error


def real_config_entries(
    common_dir: Path, environ: Mapping[str, str]
) -> list[ConfigEntry]:
    """Return each key and value of common_dir's config file, read as data.

    git reads that file alone, follows no include, and runs from / with no
    GIT_ variable and no global or system config, so it finds no repository
    and runs nothing the file names. A key written with no '=' has the value
    None. A missing file holds no entry.

    Raises:
        RefusalError: git cannot read the file.
    """
    config = common_dir / "config"
    if not config.is_file():
        return []
    completed = subprocess.run(  # noqa: S603 - fixed arguments and the file's path
        ["git", "config", "--file", str(config), "--no-includes", "--list", "-z"],  # noqa: S607 - the image's git, on PATH
        capture_output=True,
        check=False,
        cwd="/",
        env={**_without_git_variables(environ), **NO_GLOBAL_OR_SYSTEM},
    )
    if completed.returncode != 0:
        reason = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RefusalError(
            f"the repository's config {config} could not be read: {reason}"
        )
    return [_config_entry(record) for record in completed.stdout.split(b"\0") if record]


def check_shape(repo: str, dirs: GitDirs, entries: Sequence[ConfigEntry]) -> None:
    """Return if repo has a shape the isolation reads correctly.

    entries are the real config's. A .gitmodules counts whether git tracks
    it or not.

    Raises:
        RefusalError: repo is a shallow clone, uses reftable refs, has
            submodules, is a partial clone, or sets an extension the
            isolation does not support.
    """
    if (dirs.common_dir / "shallow").exists():
        reason = SHALLOW_REASON
    elif (dirs.common_dir / "reftable").exists():
        reason = REFTABLE_REASON
    elif os.path.lexists(Path(repo) / ".gitmodules"):
        reason = SUBMODULES_REASON
    else:
        reason = next(filter(None, map(_config_refusal, entries)), None)
    if reason is not None:
        raise RefusalError(f"{repo} {reason}")


def _config_refusal(entry: ConfigEntry) -> str | None:
    """Return why the real config entry marks a shape the isolation misreads.

    The scratch config carries none of the real config's extensions, so each
    one the isolation does not reproduce is refused: refStorage other than
    files, and every extension but worktreeConfig, whose config.worktree the
    isolation leaves unread by design. git clone --filter marks a partial
    clone by a promisor remote, which the scratch config does not carry.
    Names compare in lower case, as git compares them.
    """
    key, value = entry
    name = key.lower()
    section, _, rest = name.partition(".")
    subsection, _, variable = rest.rpartition(".")
    if section == "submodule":
        return SUBMODULES_REASON
    if section == "remote" and subsection and variable == "promisor":
        return None if _git_false(value) else PARTIAL_REASON
    if name == REF_STORAGE:
        return _ref_storage_refusal(value)
    if section == "extensions" and name != WORKTREE_CONFIG:
        return f"sets {name}, an extension the git plugin does not support"
    return None


def _ref_storage_refusal(value: str | None) -> str | None:
    if value == "files":
        return None
    if value is not None and value.startswith("reftable"):
        return REFTABLE_REASON
    return REF_STORE_REASON


def _git_false(value: str | None) -> bool:
    """Return if git reads value as the boolean false; a key with no '=' is true."""
    return value is not None and value.lower() in GIT_FALSE


def _without_git_variables(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: environ[name] for name in environ if not name.startswith("GIT_")}


def _config_entry(record: bytes) -> ConfigEntry:
    """Return the key and value of one record of git config --list -z."""
    key, newline, value = os.fsdecode(record).partition("\n")
    return key, value if newline else None


def _make_scratch(dirs: GitDirs, entries: Sequence[ConfigEntry], parent: str) -> Path:
    """Make a scratch common directory beneath parent for dirs, and return it."""
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=parent))
    (scratch / "config").write_text(
        scratch_config(dirs.common_dir, entries),
        encoding="utf-8",
        errors="surrogateescape",
    )
    (scratch / "HEAD").write_text(SCRATCH_HEAD)
    (scratch / "refs").mkdir()
    # git makes no directory through a dangling link, and worktree add writes
    # beneath worktrees. A read-only or missing .git cannot take it, and no
    # worktree can be added there either.
    with contextlib.suppress(OSError):
        (dirs.common_dir / "worktrees").mkdir(exist_ok=True)
    for name in LINKED_ENTRIES:
        (scratch / name).symlink_to(dirs.common_dir / name)
    return scratch


def scratch_config(common_dir: Path, entries: Sequence[ConfigEntry]) -> str:
    """Return the scratch config's text: the fixed keys, then entries' transport keys.

    Each value is quoted, so no value or remote name can open a key of its
    own.
    """
    lines = [
        "[core]",
        "\trepositoryformatversion = 1",
        "\tbare = false",
        "[extensions]",
        f"\trefStorage = {_quoted_value(f'files://{common_dir}')}",
        '[filter "lfs"]',
        *(f"\t{variable} = {_quoted_value(value)}" for variable, value in LFS_FILTER),
    ]
    for section, subsection, variable, value in _transport_entries(entries):
        lines += [
            f"[{section} {_quoted_subsection(subsection)}]",
            f"\t{variable} = {_quoted_value(value)}",
        ]
    return "\n".join(lines) + "\n"


def _transport_entries(
    entries: Sequence[ConfigEntry],
) -> Iterator[tuple[str, str, str, str]]:
    """Yield the section, subsection, variable, and value of each transport key.

    git lists a key as section.subsection.variable with section and variable
    in lower case; the subsection may hold dots.
    """
    for key, value in entries:
        section, _, rest = key.partition(".")
        subsection, _, variable = rest.rpartition(".")
        if value is not None and subsection and (section, variable) in TRANSPORT_KEYS:
            yield section, subsection, variable, value


def _quoted_subsection(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quoted_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def wrapper(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Replace this process with git on the repository argv names, or refuse.

    argv is the wrapper's own argv, its name first. A cage that holds
    GH_TOKEN is refused unless GITHUB_REPOS parses, and a set GIT_SIGNING_KEY
    is refused unless it names a readable file. git runs in the environment
    build_isolation returns. The function returns only on a refusal: it
    writes the reason to stderr and returns REFUSAL_EXIT.
    """
    try:
        roots = roots_from_env(environ)
        repo, args = split_repo(argv[1:])
        rules.check_token_cage(environ)
        check_signing_key(environ)
        isolated = build_isolation(repo, environ)
    except RefusalError as refusal:
        print(f"{REFUSAL_PREFIX}{refusal}", file=sys.stderr)
        return REFUSAL_EXIT
    os.execvpe("git", git_argv(repo, roots, args, isolated), isolated)  # noqa: S606, S607 - the wrapper exists to become git, found on PATH
