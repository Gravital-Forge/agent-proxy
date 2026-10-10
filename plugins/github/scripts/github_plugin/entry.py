"""The image's one executable, which acts by the name it runs under."""

import json
import os
import sys
from collections.abc import Sequence
from pathlib import PurePosixPath

from github_plugin import cage, scripts

WRAPPER_NAME = "gh-cage"


def main(argv: Sequence[str]) -> int:
    """Run what argv[0]'s base name names, and return its exit code.

    The name gh-cage runs the wrapper. A script action's name runs that action
    on the JSON input on stdin, prints its result as one compact JSON line,
    and returns 0. Any other name is refused.
    """
    name = PurePosixPath(argv[0]).name
    if name == WRAPPER_NAME:
        return cage.wrapper(argv, os.environ)
    if name in scripts.ACTIONS:
        result = scripts.run_script(name, sys.stdin.buffer.read())
        print(json.dumps(result, separators=(",", ":")))
        return 0
    print(f"{scripts.REFUSAL_PREFIX}no action is named {name!r}", file=sys.stderr)
    return scripts.REFUSAL_EXIT
