"""The attach input's checks, against the grant's setting and roots.

GITHUB_PR_ATTACHMENTS, which the grant's env sets, turns attach on or off.
The roots come from AGENT_PROXY_ROOTS, which the core sets; the git plugin
reads the same variable with its own copy.
"""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from github_plugin import scripts

SETTING_ENV_NAME = "GITHUB_PR_ATTACHMENTS"
ROOTS_ENV_NAME = "AGENT_PROXY_ROOTS"
IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp")
VIDEO_EXTENSIONS = ("mp4", "mov", "webm")
ACCEPTED_EXTENSIONS = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS


def attach_args(
    entries: Sequence[tuple[str, str | None]], environ: Mapping[str, str]
) -> list[str]:
    """Return one gh --attach= item per entry, in order.

    Each entry is a path and its alt text, or None for no alt. An entry with
    alt becomes --attach=<path>#<alt>. The setting is checked first, then the
    roots, then each entry in order.

    Raises:
        RefusalError: the setting is not on, the roots are missing or not a
            JSON array of absolute paths, or an entry fails a check.
    """
    _check_setting(environ)
    roots = _real_roots(environ)
    return [_attach_item(path, alt, roots) for path, alt in entries]


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


def _attach_item(path: str, alt: str | None, roots: Sequence[Path]) -> str:
    """Return the --attach= item for path, after its checks.

    path is resolved with symlinks followed, so a symlink that leads out of
    every root is refused. The extension check applies to path and to its
    real path, since gh names the upload by path.

    Raises:
        RefusalError: path resolves outside every root, is not a regular
            file, lacks an accepted extension, or is a video given alt.
    """
    outside = scripts.RefusalError(
        f"attach path '{path}' resolves outside the grant's roots"
    )
    try:
        real = Path(os.path.realpath(path))
    except ValueError as error:
        raise outside from error
    if not any(root in real.parents for root in roots):
        raise outside
    if not real.is_file():
        raise scripts.RefusalError(f"attach path '{path}' is not a regular file")
    extension = _extension(path)
    if not {extension, _extension(str(real))} <= set(ACCEPTED_EXTENSIONS):
        raise scripts.RefusalError(
            f"attach path '{path}' is not a {', '.join(IMAGE_EXTENSIONS)}, "
            f"{', '.join(VIDEO_EXTENSIONS[:-1])}, or {VIDEO_EXTENSIONS[-1]} file"
        )
    if alt is None:
        return f"--attach={path}"
    if extension in VIDEO_EXTENSIONS:
        raise scripts.RefusalError(
            f"attach path '{path}' is a video, and a video takes no alt"
        )
    return f"--attach={path}#{alt}"


def _extension(path: str) -> str:
    """Return path's extension, lowercase and without its dot."""
    return Path(path).suffix.lower().removeprefix(".")
