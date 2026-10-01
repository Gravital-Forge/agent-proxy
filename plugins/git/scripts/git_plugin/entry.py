"""The image's one executable, which acts by the name it runs under."""

import os
import sys
from collections.abc import Sequence
from pathlib import PurePosixPath

from git_plugin import cage

WRAPPER_NAME = "git-cage"


def main(argv: Sequence[str]) -> int:
    """Run what argv[0]'s base name names, and return its exit code.

    The name git-cage runs the wrapper. Any other name is refused.
    """
    name = PurePosixPath(argv[0]).name
    if name == WRAPPER_NAME:
        return cage.wrapper(argv, os.environ)
    print(f"{cage.REFUSAL_PREFIX}no action is named {name!r}", file=sys.stderr)
    return cage.REFUSAL_EXIT
