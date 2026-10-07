"""What the git plugin's integration tests share with their fixtures."""

import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from agent_proxy.config import EnvMount, FileMount
from agent_proxy.runner import Outcome

FIXTURE_NAME = "Fixture"
FIXTURE_EMAIL = "fixture@example.invalid"
AGENT_NAME = "Agent One"
AGENT_EMAIL = "agent-01@example.com"
_ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

type GrantedCredentials = Mapping[str, tuple[EnvMount | FileMount, bytes]]
type RootMode = Literal["ro", "rw"]


class RunAction(Protocol):
    def __call__(
        self,
        name: str,
        arguments: Mapping[str, object],
        roots: Sequence[Path],
        credentials: GrantedCredentials | None = None,
        env: Mapping[str, str] | None = None,
        mode: RootMode = "rw",
    ) -> Outcome:
        """Run the named action in a cage whose grant has the roots in mode, and env."""


@dataclass(frozen=True)
class OtherUidRepo:
    """A repository uid 1000 owns, its linked worktree, and the root above both."""

    root: Path
    repo: Path
    worktree: Path


@dataclass(frozen=True)
class RemotePair:
    """A bare remote, two clones of it, and the root above all three."""

    root: Path
    remote: Path
    work: Path
    peer: Path


def host_git(repo: Path, *args: str) -> str:
    """Run the host's git in repo and return its stdout.

    The host's global and system config are left out, so no host setting
    reaches a fixture.

    Raises:
        subprocess.CalledProcessError: git exits non-zero.
    """
    completed = subprocess.run(  # noqa: S603 - fixed arguments the tests choose
        ["git", "-C", str(repo), *args],  # noqa: S607 - the host's git, on PATH
        capture_output=True,
        check=True,
        text=True,
        env={**os.environ, **_ISOLATED_GIT},
    )
    return completed.stdout


def plant_hook(repo: Path, name: str, mark: Path) -> None:
    """Plant an executable hook name in repo that creates mark when git runs it.

    git init makes .git/hooks only from a template directory, so the function
    creates it first.
    """
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / name
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(mark))}\n")
    hook.chmod(0o755)


def stdout_of(outcome: Outcome) -> str:
    """Return the stdout of an action whose exit_code is 0; fail the test otherwise."""
    assert outcome.content["exit_code"] == 0, outcome.content
    stdout = outcome.content["stdout"]
    assert isinstance(stdout, str)
    return stdout
