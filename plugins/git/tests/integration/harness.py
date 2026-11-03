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
SIGNING_KEY_PATH = "/run/credentials/git-signing-key"
_ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
_ISOLATED_GIT_PROGRAM = Path(__file__).with_name("isolated_git.py")

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


class RunInImage(Protocol):
    def __call__(self, root: Path, command: Sequence[str]) -> str:
        """Run command in the plugin image, root bound and no cage, for its stdout."""


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


def isolated_git(repo: Path, *args: str) -> list[str]:
    """Return the command that runs git on repo in the plugin image's isolation.

    git runs in the environment the plugin's isolation builder returns, with
    none of the plugin's -c values; a RunInImage runs the command.
    """
    return ["python3", "-c", _ISOLATED_GIT_PROGRAM.read_text(), str(repo), *args]


def make_signing_key(directory: Path) -> tuple[bytes, str]:
    """Make a passphrase-less ed25519 key pair in directory.

    Returns:
        The private key's bytes and the public key's text.

    Raises:
        subprocess.CalledProcessError: ssh-keygen exits non-zero.
    """
    key = directory / "signing-key"
    subprocess.run(  # noqa: S603 - fixed arguments the tests choose
        [  # noqa: S607 - the host's ssh-keygen, on PATH
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "integration",
            "-f",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    return key.read_bytes(), key.with_name(f"{key.name}.pub").read_text()


def write_allowed_signers(path: Path, email: str, public_key: str) -> Path:
    """Write an allowed-signers file at path that trusts public_key for email.

    Returns:
        path, for the caller to hand to gpg.ssh.allowedSignersFile.
    """
    path.write_text(f"{email} {public_key}")
    return path


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
