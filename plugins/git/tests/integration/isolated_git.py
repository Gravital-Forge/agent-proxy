"""Run git on a repository in the environment the git plugin's isolation builds.

It runs in a container from the plugin image, with /plugin on PYTHONPATH and
the repository's root bound at its own path, as python3 -c <this file> <repo>
<git argument>... git gets none of the plugin's -c values, so the config it
reads is the isolation's alone.
"""

import os
import sys

from git_plugin import cage

repo, *args = sys.argv[1:]
isolated = cage.build_isolation(repo, os.environ)
os.execvpe("git", ["git", "-C", repo, *args], isolated)  # noqa: S606, S607 - the image's git, found on PATH
