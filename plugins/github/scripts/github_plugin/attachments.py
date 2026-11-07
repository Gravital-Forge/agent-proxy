"""The attach input's checks, and the private paths gh gets for the files that pass.

GITHUB_PR_ATTACHMENTS, which the grant's env sets, turns attach on or off.
The roots come from AGENT_PROXY_ROOTS, which the core sets; the git plugin
reads the same variable with its own copy.

gh stats a file, makes its API calls, then opens the file by name, and the
agent can change what its own path names in that time. So the script holds each
file open for the life of the process, judges the opened file, and gives gh a
path on the cage's tmpfs, out of the agent's reach, that leads to the descriptor.
"""

import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from github_plugin import scripts

SETTING_ENV_NAME = "GITHUB_PR_ATTACHMENTS"
ROOTS_ENV_NAME = "AGENT_PROXY_ROOTS"
IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp")
VIDEO_EXTENSIONS = ("mp4", "mov", "webm")
ACCEPTED_EXTENSIONS = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS
TREE_PARENT = "/tmp"  # noqa: S108 - the cage's tmpfs, named because a grant's env may set TMPDIR
# O_NONBLOCK keeps a FIFO from hanging the call.
OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass(frozen=True)
class AttachedFiles:
    """The --attach= items for gh, and each private path's entry path."""

    args: list[str]
    entry_paths: Mapping[str, str]

    def restore(self, text: str) -> str:
        """Return text with each private path replaced by its entry's path."""
        for private in sorted(self.entry_paths, key=len, reverse=True):
            text = text.replace(private, self.entry_paths[private])
        return text


def attach_args(
    entries: Sequence[tuple[str, str | None]], environ: Mapping[str, str]
) -> AttachedFiles:
    """Return one gh --attach= item per entry, in order, and how to undo its path.

    Each entry is a path and its alt text, or None for no alt. An entry with
    alt becomes --attach=<private path>#<alt>. The setting is checked first,
    then the roots, then each entry in order. Each checked file stays open until
    the process exits, because gh reaches it through the descriptor.

    Raises:
        RefusalError: the setting is not on, the roots are missing or not a
            JSON array of absolute paths, or an entry fails a check.
    """
    _check_setting(environ)
    roots = _real_roots(environ)
    descriptors = [_open_checked(path, alt, roots) for path, alt in entries]
    paths = [path for path, _ in entries]
    private_paths = _private_paths(paths, descriptors)
    return AttachedFiles(
        args=[
            f"--attach={private}" if alt is None else f"--attach={private}#{alt}"
            for private, (_, alt) in zip(private_paths, entries, strict=True)
        ],
        entry_paths=dict(zip(private_paths, paths, strict=True)),
    )


def _check_setting(environ: Mapping[str, str]) -> None:
    """Return if environ's setting is unset or on.

    Raises:
        RefusalError: the setting is off, or is any other value.
    """
    setting = environ.get(SETTING_ENV_NAME, "on")
    if setting == "off":
        raise scripts.RefusalError(f"attach is off: {SETTING_ENV_NAME} is 'off'")
    if setting != "on":
        raise scripts.RefusalError(
            f"{SETTING_ENV_NAME} must be 'on' or 'off', got '{setting}'"
        )


def _real_roots(environ: Mapping[str, str]) -> list[Path]:
    """Return the real path of each root environ's AGENT_PROXY_ROOTS names.

    Raises:
        RefusalError: the variable is unset, or is not a JSON array of
            absolute paths.
    """
    value = environ.get(ROOTS_ENV_NAME)
    if value is None:
        raise scripts.RefusalError(
            f"attach needs the grant's roots: {ROOTS_ENV_NAME} is not set"
        )
    malformed = scripts.RefusalError(
        f"{ROOTS_ENV_NAME} must be a JSON array of absolute paths, got '{value}'"
    )
    try:
        roots = json.loads(value)
    # Deep nesting exhausts the decoder's recursion limit.
    except (ValueError, RecursionError) as error:
        raise malformed from error
    if not (
        isinstance(roots, list)
        and all(isinstance(root, str) and root.startswith("/") for root in roots)
    ):
        raise malformed
    return [Path(os.path.realpath(root)) for root in roots]


def _open_checked(path: str, alt: str | None, roots: Sequence[Path]) -> int:
    """Return a descriptor open on path, after its checks.

    path is resolved with symlinks followed, so a symlink that leads out of
    every root is refused before anything opens. The other checks judge the
    opened file, by the path of its descriptor and by fstat, so a swap of path
    after the resolve changes nothing. The extension check applies to path and
    to the opened file's path, since gh names the upload by path.

    Raises:
        RefusalError: path resolves outside every root, is not a regular
            file, lacks an accepted extension, or is a video given alt.
    """
    outside = scripts.RefusalError(
        f"attach path '{path}' resolves outside the grant's roots"
    )
    not_regular = scripts.RefusalError(_not_regular_reason(path))
    try:
        real = Path(os.path.realpath(path))
    except ValueError as error:
        raise outside from error
    if not _below_a_root(real, roots):
        raise outside
    try:
        descriptor = os.open(path, OPEN_FLAGS)
    except OSError as error:
        raise not_regular from error
    opened = Path(f"/proc/self/fd/{descriptor}").readlink()
    if not _below_a_root(opened, roots):
        raise outside
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise not_regular
    extension = _extension(path)
    if not {extension, _extension(str(opened))} <= set(ACCEPTED_EXTENSIONS):
        raise scripts.RefusalError(
            f"attach path '{path}' is not a {', '.join(IMAGE_EXTENSIONS)}, "
            f"{', '.join(VIDEO_EXTENSIONS[:-1])}, or {VIDEO_EXTENSIONS[-1]} file"
        )
    if alt is not None and extension in VIDEO_EXTENSIONS:
        raise scripts.RefusalError(
            f"attach path '{path}' is a video, and a video takes no alt"
        )
    return descriptor


def _not_regular_reason(path: str) -> str:
    """Return why a path that does not lead to one regular file is refused."""
    return f"attach path '{path}' is not a regular file"


def _below_a_root(path: Path, roots: Sequence[Path]) -> bool:
    """Return whether path lies below one of roots."""
    return any(root in path.parents for root in roots)


def _extension(path: str) -> str:
    """Return path's extension, lowercase and without its dot."""
    return Path(path).suffix.lower().removeprefix(".")


def _private_paths(paths: Sequence[str], descriptors: Sequence[int]) -> list[str]:
    """Return a private path for each of paths, leading to its descriptor.

    gh matches a body reference to an attachment by comparing filepath.Abs of
    each, and Abs cleans lexically. So a private path must clean to the agent's
    path while the kernel, which follows symlinks before each "..", lands
    elsewhere. It is <jump>, then one ".." per parent of <jump>, then the
    agent's path. Lexically the ".." parts climb <jump> to /. The kernel enters
    the symlink <jump> first, which leads as many directories deep below <tree>,
    so it climbs to <tree>, where <tree>/<agent's path> links to the descriptor.

    Raises:
        RefusalError: two paths need one name to be both a file and a directory.
    """
    base = Path(tempfile.mkdtemp(dir=TREE_PARENT))
    tree = base / "tree"
    jump = base / "jump"
    climbs = len(jump.parents)
    deepest = tree.joinpath(*["up"] * climbs)
    deepest.mkdir(parents=True)
    jump.symlink_to(deepest)
    return [
        f"{jump}{'/..' * climbs}/{_link_descriptor(path, descriptor, tree)}"
        for path, descriptor in zip(paths, descriptors, strict=True)
    ]


def _link_descriptor(path: str, descriptor: int, tree: Path) -> str:
    """Link path's place below tree to descriptor; return path relative to tree.

    Entries share parents, and the same path given twice shares its link.

    Raises:
        RefusalError: another entry needs path's place to be a directory, or a
            parent of it to be a file.
    """
    relative = os.path.normpath(path).lstrip("/")
    link = tree / relative
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink():
            link.symlink_to(f"/proc/{os.getpid()}/fd/{descriptor}")
    except OSError as error:
        raise scripts.RefusalError(_not_regular_reason(path)) from error
    return relative
