"""The cage's git reads no config the agent wrote, so runs no program it names."""

import re
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_proxy.plugins import Manifest
from harness import (
    FIXTURE_EMAIL,
    FIXTURE_NAME,
    RemotePair,
    RunAction,
    RunInImage,
    host_git,
    isolated_git,
    stdout_of,
)

TASK_PREFIX = {"GIT_BRANCH_PREFIXES": "task/"}
COMMIT_AS_FIXTURE = (
    "-c",
    f"user.name={FIXTURE_NAME}",
    "-c",
    f"user.email={FIXTURE_EMAIL}",
)
# The host's git LFS checks each LFS file out as its pointer, so the object
# stays missing for the cage's LFS fetch.
SKIP_LFS_SMUDGE = (
    "-c",
    "filter.lfs.process=git-lfs filter-process --skip",
    "-c",
    "filter.lfs.smudge=git-lfs smudge --skip -- %f",
)
CANARY_ATTRIBUTES = "*.txt filter=canary diff=canary\n*.proc filter=canaryproc\n"
MARK_SUFFIX = ".fired"


@dataclass(frozen=True)
class Canary:
    """A program a config key names, which leaves a mark when it runs.

    A filter or textconv program echoes its input, so git goes on past it.
    """

    name: str
    key: str
    echoes_input: bool


REPO_CONFIG_CANARIES = (
    Canary("filter-clean", "filter.canary.clean", echoes_input=True),
    Canary("filter-smudge", "filter.canary.smudge", echoes_input=True),
    Canary("filter-process", "filter.canaryproc.process", echoes_input=False),
    Canary("textconv", "diff.canary.textconv", echoes_input=True),
    Canary("fsmonitor", "core.fsmonitor", echoes_input=False),
    Canary("lfs-customtransfer", "lfs.customtransfer.canary.path", echoes_input=False),
)
LFSCONFIG_CANARY = Canary(
    "lfsconfig-customtransfer", "lfs.customtransfer.lfsconfig.path", echoes_input=False
)
# Each command reaches a planted program; what a program's failure does to the
# command is beside the point.
RAW_TRIGGERS = """
cd "$1"
git status
echo changed > a.txt && git add a.txt
echo changed > p.proc && git add p.proc
git show HEAD
rm a.txt && git checkout -- a.txt
git lfs fetch origin
true
"""
PLANTED_FSMONITOR = "/planted/fsmonitor"
FSMONITOR_SETTING = f"core.fsmonitor {PLANTED_FSMONITOR}"
CONFIG_LISTING = ("config", "--list", "--show-origin", "-z")
SCRATCH_CONFIG_ORIGIN = re.compile(r"file:/tmp/git-isolation-[^/]+/config")
# The fixed keys, then the transport keys _plant_repo_config writes.
SCRATCH_KEYS = sorted(
    [
        "core.repositoryformatversion",
        "core.bare",
        "extensions.refstorage",
        "filter.lfs.clean",
        "filter.lfs.smudge",
        "filter.lfs.process",
        "filter.lfs.required",
        "remote.origin.url",
        "remote.origin.fetch",
        "remote.origin.pushurl",
        "branch.main.remote",
        "branch.main.merge",
    ]
)


@dataclass(frozen=True)
class CanaryCheckout:
    """A checkout of an LFS pair's peer, every canary planted, on branch."""

    root: Path
    remote: Path
    repo: Path
    branch: str


def _mark(root: Path, name: str) -> Path:
    return root / "canary" / f"{name}{MARK_SUFFIX}"


def _fired(root: Path) -> set[str]:
    """Return the name of each canary that ran."""
    marks = (root / "canary").glob(f"*{MARK_SUFFIX}")
    return {mark.name.removesuffix(MARK_SUFFIX) for mark in marks}


def _plant_program(root: Path, canary: Canary) -> str:
    """Write canary's program beneath root, and return its path."""
    directory = root / "canary"
    directory.mkdir(exist_ok=True)
    program = directory / canary.name
    then = 'exec cat "$@"' if canary.echoes_input else "exit 1"
    program.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(_mark(root, canary.name)))}\n{then}\n"
    )
    program.chmod(0o755)
    return str(program)


def _commit_canary_files(peer: Path) -> None:
    """Commit a.txt and p.proc in peer, with the attributes that name the drivers."""
    (peer / "a.txt").write_text("one\n")
    (peer / "p.proc").write_text("one\n")
    with (peer / ".gitattributes").open("a") as attributes:
        attributes.write(CANARY_ATTRIBUTES)
    host_git(peer, "add", ".gitattributes", "a.txt", "p.proc")
    host_git(peer, *COMMIT_AS_FIXTURE, "commit", "-q", "-m", "canary files")


def _plant_canaries(main: Path, checkout: Path, root: Path) -> None:
    """Plant the drivers in main's .git/config, the LFS one in checkout's .lfsconfig.

    Each standalone transfer agent selects its custom transfer for every LFS
    transfer, with no server to offer it.
    """
    for canary in REPO_CONFIG_CANARIES:
        host_git(main, "config", canary.key, _plant_program(root, canary))
    host_git(main, "config", "lfs.standalonetransferagent", "canary")
    lfsconfig = ("config", "--file", str(checkout / ".lfsconfig"))
    host_git(
        checkout,
        *lfsconfig,
        LFSCONFIG_CANARY.key,
        _plant_program(root, LFSCONFIG_CANARY),
    )
    host_git(checkout, *lfsconfig, "lfs.standalonetransferagent", "lfsconfig")


@pytest.fixture(params=["main", "linked"])
def canary_checkout(
    request: pytest.FixtureRequest, lfs_remote_pair: RemotePair
) -> CanaryCheckout:
    """The LFS pair's peer, or a linked worktree of it on task/wt, its canaries planted.

    The linked worktree gets a copy of peer's .lfsconfig, which no checkout
    shares.
    """
    peer = lfs_remote_pair.peer
    _commit_canary_files(peer)
    repo, branch = peer, "main"
    if request.param == "linked":
        repo, branch = lfs_remote_pair.root / "peer-wt", "task/wt"
        host_git(
            peer, *SKIP_LFS_SMUDGE, "worktree", "add", "-q", "-b", branch, str(repo)
        )
        shutil.copy(peer / ".lfsconfig", repo / ".lfsconfig")
    _plant_canaries(peer, repo, lfs_remote_pair.root)
    return CanaryCheckout(
        root=lfs_remote_pair.root,
        remote=lfs_remote_pair.remote,
        repo=repo,
        branch=branch,
    )


def test_every_action_round_trips_and_runs_no_planted_program(
    run_action: RunAction, plugin_manifest: Manifest, canary_checkout: CanaryCheckout
) -> None:
    """Each action, in an ordinary round trip, succeeds and fires no canary.

    The drivers sit in the repository's .git/config and the LFS transfer in
    its .lfsconfig. The pull after the push finds the upstream the push wrote
    back to the real config.
    """
    checkout = canary_checkout
    ran: set[str] = set()

    def run(name: str, **arguments: object) -> str:
        ran.add(name)
        outcome = run_action(
            name,
            {"repo": str(checkout.repo), **arguments},
            [checkout.root],
            env=TASK_PREFIX,
        )
        return stdout_of(outcome)

    for name in ("status", "log", "show", "branch", "ls_files", "stash_list", "remote"):
        run(name)
    run("blame", file="a.txt")
    run("tag")
    run("config_get", key="remote.origin.url")
    for name in ("lfs_ls_files", "lfs_status", "fetch", "lfs_fetch", "lfs_pull"):
        run(name)
    assert len((checkout.repo / "big.bin").read_bytes()) == 2048

    run("checkout", target="task/canary", create=True)
    (checkout.repo / "a.txt").write_text("two\n")
    (checkout.repo / "p.proc").write_text("two\n")
    run("diff")
    run("add", files=["a.txt", "p.proc"])
    run("commit", message="canary")
    run("push")
    run("pull")
    committed = run("rev_parse", ref="HEAD")
    assert host_git(checkout.remote, "rev-parse", "refs/heads/task/canary") == committed

    run("checkout", target=checkout.branch)
    assert (checkout.repo / "a.txt").read_text() == "one\n"
    run("merge", branch="task/canary")
    assert run("rev_parse", ref=checkout.branch) == committed

    (checkout.repo / "a.txt").write_text("stashed\n")
    run("stash", action="push")
    run("stash", action="pop", index=0)
    assert (checkout.repo / "a.txt").read_text() == "stashed\n"
    run("restore", files=["a.txt"])
    run("rm", files=["p.proc"])
    run("restore", files=["p.proc"], staged=True)
    run("restore", files=["p.proc"])
    run("lfs_track", patterns=["*.dat"])
    run("lfs_untrack", patterns=["*.dat"])
    added = str(checkout.root / "canary-wt")
    run("worktree", action="add", path=added, branch="task/canary-wt")
    run("worktree", action="list")
    run("worktree", action="remove", path=added)

    assert ran == set(plugin_manifest.actions)
    assert _fired(checkout.root) == set()


def test_raw_git_in_the_plugin_image_runs_each_planted_program(
    run_in_plugin_image: RunInImage, lfs_remote_pair: RemotePair
) -> None:
    """Past git-cage and the isolation, the image's git fires each driver canary.

    So the planted programs and their triggers work, and their silence under
    the actions is the isolation's doing. The .lfsconfig canary is no part of
    this test: the standalone agent in .git/config outranks the one in
    .lfsconfig.
    """
    peer = lfs_remote_pair.peer
    _commit_canary_files(peer)
    _plant_canaries(peer, peer, lfs_remote_pair.root)

    run_in_plugin_image(
        lfs_remote_pair.root, ["sh", "-c", RAW_TRIGGERS, "sh", str(peer)]
    )

    assert _fired(lfs_remote_pair.root) == {
        canary.name for canary in REPO_CONFIG_CANARIES
    }


def _set_fsmonitor(config: Path) -> None:
    host_git(
        config.parent,
        "config",
        "--file",
        str(config),
        "core.fsmonitor",
        PLANTED_FSMONITOR,
    )


def _in_repo_config(repo: Path) -> str:
    _set_fsmonitor(repo / ".git" / "config")
    return "true"


def _through_include_path(repo: Path) -> str:
    included = repo.parent / "included"
    _set_fsmonitor(included)
    host_git(repo, "config", "include.path", str(included))
    return "true"


def _through_include_if(repo: Path) -> str:
    included = repo.parent / "included"
    _set_fsmonitor(included)
    host_git(repo, "config", f"includeIf.gitdir:{repo}/.path", str(included))
    return "true"


def _in_config_worktree(repo: Path) -> str:
    host_git(repo, "config", "extensions.worktreeConfig", "true")
    _set_fsmonitor(repo / ".git" / "config.worktree")
    return "true"


def _in_global_files(repo: Path) -> str:
    return " && ".join(
        [
            f"git config --file /root/.gitconfig {FSMONITOR_SETTING}",
            "mkdir -p /root/.config/git",
            f"git config --file /root/.config/git/config {FSMONITOR_SETTING}",
        ]
    )


def _in_system_file(repo: Path) -> str:
    return f"git config --file /etc/gitconfig {FSMONITOR_SETTING}"


def _plant_repo_config(repo: Path) -> None:
    """Give repo's .git/config the transport keys and keys the scratch must not hold."""
    for key, value in (
        ("remote.origin.pushurl", str(repo.parent / "origin.git")),
        ("branch.main.remote", "origin"),
        ("branch.main.merge", "refs/heads/main"),
        ("filter.canary.clean", "/planted/clean"),
        ("diff.canary.textconv", "/planted/textconv"),
        ("lfs.customtransfer.canary.path", "/planted/transfer"),
        ("lfs.standalonetransferagent", "canary"),
        ("url.ext::sh.insteadOf", "/"),
        ("remote.origin.mirror", "true"),
        ("core.sshCommand", "/planted/ssh"),
        ("credential.helper", "/planted/helper"),
        ("user.name", "Planted"),
    ):
        host_git(repo, "config", key, value)


def _entries(listing: bytes) -> list[tuple[str, str, str]]:
    """Return the origin, key, and value of each entry of a --show-origin -z listing."""
    fields = listing.decode().split("\0")[:-1]
    return [
        (origin, *record.partition("\n")[::2])
        for origin, record in zip(fields[::2], fields[1::2], strict=True)
    ]


@pytest.mark.parametrize(
    "plant",
    [
        pytest.param(_in_repo_config, id="repo-config"),
        pytest.param(_through_include_path, id="include-path"),
        pytest.param(_through_include_if, id="include-if"),
        pytest.param(_in_config_worktree, id="config-worktree"),
        pytest.param(_in_global_files, id="global-files"),
        pytest.param(_in_system_file, id="system-file"),
    ],
)
def test_git_reads_only_the_scratch_config(
    run_in_plugin_image: RunInImage, host_repo: Path, plant: Callable[[Path], str]
) -> None:
    """Under isolation git reads the scratch config alone, holding only its keys.

    A core.fsmonitor planted through the config file, an include, an
    includeIf, a config.worktree, or a global or system file reaches raw git,
    which shows the plant took, and never the isolated git.
    """
    root = host_repo.parent
    raw, isolated = root / "raw.z", root / "isolated.z"
    _plant_repo_config(host_repo)
    in_image = plant(host_repo)
    script = (
        f"{in_image}"
        f" && git -C {shlex.quote(str(host_repo))} {' '.join(CONFIG_LISTING)}"
        f" > {shlex.quote(str(raw))}"
        f' && "$@" > {shlex.quote(str(isolated))}'
    )

    run_in_plugin_image(
        root, ["sh", "-c", script, "sh", *isolated_git(host_repo, *CONFIG_LISTING)]
    )

    raw_entries = _entries(raw.read_bytes())
    assert ("core.fsmonitor", PLANTED_FSMONITOR) in {
        (key, value) for _, key, value in raw_entries
    }, raw_entries
    entries = _entries(isolated.read_bytes())
    assert sorted(key for _, key, _ in entries) == SCRATCH_KEYS, entries
    assert all(SCRATCH_CONFIG_ORIGIN.fullmatch(origin) for origin, _, _ in entries)


@pytest.mark.parametrize("name", ["fetch", "pull", "push"])
@pytest.mark.parametrize(
    ("url", "transport"),
    [
        pytest.param("ext::sh -c touch% {mark}", "ext", id="ext"),
        pytest.param("fd::3", "fd", id="fd"),
        pytest.param("file://{origin}", "file", id="file-url"),
        pytest.param("{origin}", "file", id="local-path"),
    ],
)
def test_a_remote_on_a_denied_transport_fails_before_it_runs(
    run_action: RunAction, host_repo: Path, name: str, url: str, transport: str
) -> None:
    """An origin URL in a transport that runs a program in the cage is refused by git.

    A local path or file:// URL counts: git runs the local upload-pack or
    receive-pack without the plugin's -c values.
    """
    root = host_repo.parent
    mark = root / "ext-ran"
    host_git(host_repo, "checkout", "-q", "-b", "task/t")
    host_git(host_repo, "config", "branch.task/t.remote", "origin")
    host_git(host_repo, "config", "branch.task/t.merge", "refs/heads/task/t")
    origin_url = url.format(mark=mark, origin=root / "origin.git")
    host_git(host_repo, "remote", "set-url", "origin", origin_url)

    outcome = run_action(name, {"repo": str(host_repo)}, [root], env=TASK_PREFIX)

    assert outcome.content["exit_code"] != 0, outcome.content
    stderr = str(outcome.content["stderr"])
    assert f"fatal: transport '{transport}' not allowed" in stderr, outcome.content
    assert not mark.exists()
