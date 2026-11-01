import os
import subprocess
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path

import pytest
from git_plugin import cage

ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
LFS_FILTER = {
    "filter.lfs.clean": "/usr/bin/git-lfs clean -- %f",
    "filter.lfs.smudge": "/usr/bin/git-lfs smudge -- %f",
    "filter.lfs.process": "/usr/bin/git-lfs filter-process",
    "filter.lfs.required": "true",
}
PROTOCOL_PINS = (
    "-c",
    "protocol.file.allow=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "protocol.fd.allow=never",
)
HOOKS_OFF = ("-c", "core.hooksPath=/dev/null")
HELPER = (
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=/plugin/bin/git-credential-agent-proxy",
    "-c",
    "credential.useHttpPath=true",
)
SIGNING_OFF = ("-c", "commit.gpgSign=false")
KEY_PATH = "/run/credentials/git-signing-key"
NO_KEY_REFUSAL = (
    "GIT_SIGNING_KEY names no readable file: {}; "
    "the grant mounts the key there as a file credential"
)
SHAPE_REFUSAL = "AGENT_PROXY_ROOTS must be a JSON array of absolute paths, got {!r}"
NOT_TOP_LEVEL = (
    "{} has no .git; the repository path must be the top level of a checkout"
)
SHALLOW = "{} is a shallow clone; the git plugin does not support shallow clones"
REFTABLE = "{} uses the reftable ref format; the git plugin supports only files"
SUBMODULES = "{} has submodules; the git plugin does not support submodules"
EXTENSION = "{} sets {}, an extension the git plugin does not support"
REF_STORE = (
    "{} sets extensions.refstorage to a ref store other than files; "
    "the git plugin supports only files"
)
PARTIAL = "{} is a partial clone; the git plugin does not support partial clones"
NO_REPOS_STDERR = (
    "git plugin refused: "
    "GITHUB_REPOS is not set or names no repository; the grant's env sets it\n"
)


class _ExecedError(Exception):
    """Raised in place of os.execvpe; its args are execvpe's file, argv, and env."""


def _isolated_as_marked(repo: str, environ: Mapping[str, str]) -> dict[str, str]:
    """A stand-in builder whose env is environ marked with the repo it was built for."""
    return {**environ, "ISOLATED_FOR": repo}


@pytest.fixture
def _no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make os.execvpe raise _ExecedError, and build no scratch directory."""

    def fake_execvpe(file: str, args: list[str], env: Mapping[str, str]) -> None:
        raise _ExecedError(file, args, env)

    monkeypatch.setattr(cage.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(cage, "build_isolation", _isolated_as_marked)


def _signing_on(key: str) -> tuple[str, ...]:
    """The four -c values git_argv passes when GIT_SIGNING_KEY names key."""
    return (
        "-c",
        "gpg.format=ssh",
        "-c",
        "gpg.ssh.program=/usr/bin/ssh-keygen",
        "-c",
        f"user.signingKey={key}",
        "-c",
        "commit.gpgSign=true",
    )


def test_git_argv_orders_repo_roots_hooks_helper_signing_pins_then_args() -> None:
    """git_argv gives -C, repo, two per root, hooks off, helper, signing, pins, args."""
    argv = cage.git_argv("/work/repo", ["/work", "/other"], ["log", "--oneline"], {})

    assert argv == [
        "git",
        "-C",
        "/work/repo",
        "-c",
        "safe.directory=/work",
        "-c",
        "safe.directory=/work/*",
        "-c",
        "safe.directory=/other",
        "-c",
        "safe.directory=/other/*",
        *HOOKS_OFF,
        *HELPER,
        *SIGNING_OFF,
        *PROTOCOL_PINS,
        "log",
        "--oneline",
    ]


def test_git_argv_with_no_roots_trusts_no_directory() -> None:
    """With no roots, git_argv passes no safe.directory value."""
    argv = cage.git_argv("/repo", [], ["status"], {})

    assert argv == [
        "git",
        "-C",
        "/repo",
        *HOOKS_OFF,
        *HELPER,
        *SIGNING_OFF,
        *PROTOCOL_PINS,
        "status",
    ]


@pytest.mark.parametrize("root", ["/work/", "/work//"])
def test_git_argv_drops_a_roots_trailing_slashes(root: str) -> None:
    """A root given as /work/ trusts /work and /work/*, never /work//*."""
    argv = cage.git_argv("/work/repo", [root], ["status"], {})

    assert argv == [
        "git",
        "-C",
        "/work/repo",
        "-c",
        "safe.directory=/work",
        "-c",
        "safe.directory=/work/*",
        *HOOKS_OFF,
        *HELPER,
        *SIGNING_OFF,
        *PROTOCOL_PINS,
        "status",
    ]


def test_git_argv_keeps_the_filesystem_root_as_a_slash() -> None:
    """The root / keeps its one slash, so its own value never goes empty."""
    argv = cage.git_argv("/repo", ["/"], ["status"], {})

    assert argv[3:7] == ["-c", "safe.directory=/", "-c", "safe.directory=//*"]


def test_git_argv_turns_hooks_off() -> None:
    """git_argv passes -c core.hooksPath=/dev/null, as one pair, before the args."""
    argv = cage.git_argv("/repo", ["/work"], ["commit", "-m", "x"], {})

    assert HOOKS_OFF in pairwise(argv[: argv.index("commit")])


def test_git_argv_with_a_signing_key_signs_with_it() -> None:
    """With GIT_SIGNING_KEY set, the four signing values follow the helper's."""
    argv = cage.git_argv(
        "/repo", [], ["commit", "-m", "x"], {"GIT_SIGNING_KEY": KEY_PATH}
    )

    assert argv == [
        "git",
        "-C",
        "/repo",
        *HOOKS_OFF,
        *HELPER,
        *_signing_on(KEY_PATH),
        *PROTOCOL_PINS,
        "commit",
        "-m",
        "x",
    ]


def test_git_argv_without_a_signing_key_turns_signing_off() -> None:
    """With GIT_SIGNING_KEY unset, git_argv passes commit.gpgSign=false and no key."""
    argv = cage.git_argv("/repo", [], ["commit", "-m", "x"], {"GH_TOKEN": "t"})

    assert argv[-11:] == [*SIGNING_OFF, *PROTOCOL_PINS, "commit", "-m", "x"]
    assert not any(item.startswith("user.signingKey=") for item in argv)


def test_check_signing_key_passes_an_unset_variable() -> None:
    """With GIT_SIGNING_KEY unset, check_signing_key returns: signing is off."""
    cage.check_signing_key({"AGENT_PROXY_ROOTS": "[]"})


def test_check_signing_key_passes_a_readable_file(tmp_path: Path) -> None:
    """A GIT_SIGNING_KEY naming a readable file passes."""
    key = tmp_path / "key"
    key.write_text("key\n")

    cage.check_signing_key({"GIT_SIGNING_KEY": str(key)})


@pytest.mark.parametrize(
    "value",
    ["key", "run/credentials/git-signing-key", ""],
    ids=["bare-name", "relative-path", "empty"],
)
def test_check_signing_key_refuses_a_value_that_is_not_an_absolute_path(
    value: str,
) -> None:
    """A GIT_SIGNING_KEY that does not begin with '/' is refused, and quoted."""
    with pytest.raises(cage.RefusalError) as refused:
        cage.check_signing_key({"GIT_SIGNING_KEY": value})

    assert str(refused.value) == (
        f"GIT_SIGNING_KEY must be an absolute path, got {value!r}"
    )


@pytest.mark.parametrize("name", ["missing", "."], ids=["missing", "directory"])
def test_check_signing_key_refuses_a_path_that_names_no_file(
    tmp_path: Path, name: str
) -> None:
    """A GIT_SIGNING_KEY naming nothing, or a directory, is refused."""
    path = str(tmp_path / name)

    with pytest.raises(cage.RefusalError) as refused:
        cage.check_signing_key({"GIT_SIGNING_KEY": path})

    assert str(refused.value) == NO_KEY_REFUSAL.format(path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file of any mode")
def test_check_signing_key_refuses_a_file_it_cannot_read(tmp_path: Path) -> None:
    """A GIT_SIGNING_KEY naming a file this user cannot read is refused."""
    key = tmp_path / "key"
    key.write_text("key\n")
    key.chmod(0)

    with pytest.raises(cage.RefusalError) as refused:
        cage.check_signing_key({"GIT_SIGNING_KEY": str(key)})

    assert str(refused.value) == NO_KEY_REFUSAL.format(key)


def test_split_repo_takes_out_the_flag_and_its_value() -> None:
    """split_repo returns the repo, and the other items in their order."""
    repo, args = cage.split_repo(
        ["log", "--max-count=20", "--repo", "/repo", "--oneline"]
    )

    assert repo == "/repo"
    assert args == ["log", "--max-count=20", "--oneline"]


def test_split_repo_finds_the_flag_past_a_double_dash() -> None:
    """The --repo of blame follows its '--', and split_repo still takes it out."""
    repo, args = cage.split_repo(["blame", "--", "--repo", "/repo", "a.txt"])

    assert repo == "/repo"
    assert args == ["blame", "--", "a.txt"]


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["status"], "--repo is missing"),
        (["status", "--repo", "/a", "--repo", "/b"], "--repo must appear once, got 2"),
        (["status", "--repo"], "--repo has no value"),
    ],
)
def test_split_repo_refuses_a_missing_repeated_or_empty_flag(
    args: list[str], reason: str
) -> None:
    """split_repo refuses unless exactly one --repo carries a value."""
    with pytest.raises(cage.RefusalError) as refused:
        cage.split_repo(args)

    assert str(refused.value) == reason


def test_roots_from_env_returns_the_paths_in_order() -> None:
    """roots_from_env returns the array's paths in the order the core gave them."""
    roots = cage.roots_from_env({"AGENT_PROXY_ROOTS": '["/b","/a"]'})

    assert roots == ["/b", "/a"]


def test_roots_from_env_accepts_no_roots() -> None:
    """An empty array is a grant with no roots, not a refusal."""
    assert cage.roots_from_env({"AGENT_PROXY_ROOTS": "[]"}) == []


def test_roots_from_env_refuses_a_missing_variable() -> None:
    """roots_from_env refuses when the core set no AGENT_PROXY_ROOTS."""
    with pytest.raises(cage.RefusalError) as refused:
        cage.roots_from_env({})

    assert str(refused.value) == "AGENT_PROXY_ROOTS is not set"


def test_roots_from_env_refuses_text_that_is_not_json() -> None:
    """roots_from_env refuses a value JSON cannot decode, and quotes it."""
    with pytest.raises(cage.RefusalError) as refused:
        cage.roots_from_env({"AGENT_PROXY_ROOTS": "not json"})

    assert str(refused.value) == "AGENT_PROXY_ROOTS must be JSON, got 'not json'"


def test_roots_from_env_refuses_json_nested_past_the_recursion_limit() -> None:
    """Text nested too deep to decode is refused as text that is not JSON."""
    with pytest.raises(cage.RefusalError) as refused:
        cage.roots_from_env({"AGENT_PROXY_ROOTS": "[" * 100_000})

    assert str(refused.value).startswith("AGENT_PROXY_ROOTS must be JSON, got '[[[")


@pytest.mark.parametrize(
    "value", ['{"root": "/a"}', '"/a"', "[1]", '["a"]', '["/a", null]', '[""]']
)
def test_roots_from_env_refuses_anything_but_absolute_paths(value: str) -> None:
    """roots_from_env refuses JSON that is not an array of strings beginning '/'."""
    with pytest.raises(cage.RefusalError) as refused:
        cage.roots_from_env({"AGENT_PROXY_ROOTS": value})

    assert str(refused.value) == SHAPE_REFUSAL.format(value)


def test_wrapper_refuses_a_missing_repo_with_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wrapper writes the prefixed reason to stderr and returns 2."""
    exit_code = cage.wrapper(["git-cage", "status"], {"AGENT_PROXY_ROOTS": "[]"})

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (captured.out, captured.err) == (
        "",
        "git plugin refused: --repo is missing\n",
    )


def test_wrapper_refuses_a_missing_roots_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wrapper refuses before git runs when AGENT_PROXY_ROOTS is not set."""
    exit_code = cage.wrapper(["git-cage", "status", "--repo", "/repo"], {})

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (captured.out, captured.err) == (
        "",
        "git plugin refused: AGENT_PROXY_ROOTS is not set\n",
    )


@pytest.mark.usefixtures("_no_exec")
@pytest.mark.parametrize("token", ["t", ""], ids=["token", "empty-token"])
def test_wrapper_refuses_a_token_cage_without_a_repository_list(
    capsys: pytest.CaptureFixture[str], token: str
) -> None:
    """With GH_TOKEN set, to any value, an unset GITHUB_REPOS refuses before git."""
    environ = {"AGENT_PROXY_ROOTS": "[]", "GH_TOKEN": token}

    exit_code = cage.wrapper(["git-cage", "fetch", "--repo", "/repo"], environ)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (captured.out, captured.err) == ("", NO_REPOS_STDERR)


@pytest.mark.usefixtures("_no_exec")
def test_wrapper_refuses_a_token_cage_with_a_malformed_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With GH_TOKEN set, a malformed GITHUB_REPOS entry refuses before git."""
    environ = {
        "AGENT_PROXY_ROOTS": "[]",
        "GH_TOKEN": "t",
        "GITHUB_REPOS": "octo/api-*",
    }

    exit_code = cage.wrapper(["git-cage", "fetch", "--repo", "/repo"], environ)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (captured.out, captured.err) == (
        "",
        "git plugin refused: "
        "GITHUB_REPOS entry 'octo/api-*' is not owner/name or owner/*\n",
    )


@pytest.mark.usefixtures("_no_exec")
@pytest.mark.parametrize(
    "extra",
    [
        {"GH_TOKEN": "t", "GITHUB_REPOS": "octo/hello"},
        {},
        {"GITHUB_REPOS": "octo/api-*"},
    ],
    ids=["token-and-list", "no-token", "no-token-malformed-list"],
)
def test_wrapper_runs_git_when_the_list_parses_or_no_token_is_held(
    extra: dict[str, str],
) -> None:
    """A token cage whose list parses, or a cage with no GH_TOKEN, reaches git."""
    environ = {"AGENT_PROXY_ROOTS": "[]", **extra}

    with pytest.raises(_ExecedError) as execed:
        cage.wrapper(["git-cage", "fetch", "--repo", "/repo"], environ)

    assert execed.value.args[:2] == (
        "git",
        cage.git_argv("/repo", [], ["fetch"], environ),
    )


@pytest.mark.usefixtures("_no_exec")
def test_wrapper_runs_git_in_the_isolation_the_builder_returns() -> None:
    """Git replaces the wrapper in the env build_isolation returns for the repo."""
    environ = {"AGENT_PROXY_ROOTS": "[]", "GIT_ALLOW_PROTOCOL": "file"}

    with pytest.raises(_ExecedError) as execed:
        cage.wrapper(["git-cage", "status", "--repo", "/repo"], environ)

    assert execed.value.args[2] == _isolated_as_marked("/repo", environ)


@pytest.mark.usefixtures("_no_exec")
def test_wrapper_refuses_a_signing_key_that_names_no_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A GIT_SIGNING_KEY naming no readable file refuses before git runs."""
    missing = tmp_path / "missing"
    environ = {"AGENT_PROXY_ROOTS": "[]", "GIT_SIGNING_KEY": str(missing)}

    exit_code = cage.wrapper(["git-cage", "status", "--repo", "/repo"], environ)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert (captured.out, captured.err) == (
        "",
        f"git plugin refused: {NO_KEY_REFUSAL.format(missing)}\n",
    )


@pytest.mark.usefixtures("_no_exec")
def test_wrapper_runs_git_with_the_signing_values(tmp_path: Path) -> None:
    """A GIT_SIGNING_KEY naming a readable file reaches git with the signing values."""
    key = tmp_path / "key"
    key.write_text("key\n")
    environ = {"AGENT_PROXY_ROOTS": "[]", "GIT_SIGNING_KEY": str(key)}

    with pytest.raises(_ExecedError) as execed:
        cage.wrapper(["git-cage", "commit", "-m", "x", "--repo", "/repo"], environ)

    assert execed.value.args[:2] == (
        "git",
        [
            "git",
            "-C",
            "/repo",
            *HOOKS_OFF,
            *HELPER,
            *_signing_on(str(key)),
            *PROTOCOL_PINS,
            "commit",
            "-m",
            "x",
        ],
    )


def _host_git(repo: Path, *args: str) -> str:
    """Run the host's git in repo, its own config left out, and return stdout."""
    completed = subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        ["git", "-C", str(repo), *args],  # noqa: S607 - the host's git, on PATH
        capture_output=True,
        check=True,
        text=True,
        env={**os.environ, **ISOLATED_GIT},
    )
    return completed.stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository at tmp_path/root/repo with an origin and main's upstream."""
    repo = tmp_path / "root" / "repo"
    _host_git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _host_git(repo, "remote", "add", "origin", "https://github.com/octo/hello.git")
    _host_git(repo, "config", "branch.main.remote", "origin")
    _host_git(repo, "config", "branch.main.merge", "refs/heads/main")
    return repo


@pytest.fixture
def scratch_parent(tmp_path: Path) -> Path:
    """The directory that stands in for the cage's tmpfs."""
    parent = tmp_path / "tmpfs"
    parent.mkdir()
    return parent


def _build(
    repo: Path, scratch_parent: Path, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Run the real builder on repo, its scratch directory under scratch_parent."""
    return cage.build_isolation(
        str(repo),
        {"PATH": os.environ["PATH"], **(environ or {})},
        scratch_parent=str(scratch_parent),
    )


def _scratch_entries(env: Mapping[str, str]) -> list[str]:
    """Return each key=value line git lists from the scratch config env names."""
    config = Path(env["GIT_COMMON_DIR"]) / "config"
    return _host_git(config.parent, "config", "--file", str(config), "--list").split(
        "\n"
    )[:-1]


def _base_entries(common_dir: Path) -> list[str]:
    """Return the entries the builder writes whatever the real config holds."""
    return [
        "core.repositoryformatversion=1",
        "core.bare=false",
        f"extensions.refstorage=files://{common_dir}",
        *(f"{key}={value}" for key, value in LFS_FILTER.items()),
    ]


def test_build_isolation_copies_only_the_transport_keys(
    checkout: Path, scratch_parent: Path, tmp_path: Path
) -> None:
    """Only remote url, fetch, pushurl and branch remote, merge reach the scratch."""
    included = tmp_path / "included"
    included.write_text('[remote "planted"]\n\turl = https://example.invalid/x\n')
    planted = {
        "remote.origin.pushurl": "https://github.com/octo/push.git",
        "remote.origin.mirror": "true",
        "remote.origin.receivepack": "touch /tmp/mark",
        "filter.evil.clean": "touch /tmp/mark",
        "diff.evil.textconv": "touch /tmp/mark",
        "core.fsmonitor": "touch /tmp/mark",
        "url.ext::sh -c touch% /tmp/mark.insteadOf": "https://github.com/",
        "include.path": str(included),
        f"includeIf.gitdir:{checkout}/.path": str(included),
        "user.name": "Planted",
        "credential.helper": "!touch /tmp/mark",
        "lfs.customtransfer.evil.path": "touch /tmp/mark",
        "extensions.worktreeConfig": "true",
    }
    for key, value in planted.items():
        _host_git(checkout, "config", key, value)
    with (checkout / ".git" / "config").open("a") as config:
        config.write('[remote "valueless"]\n\turl\n')

    env = _build(checkout, scratch_parent)

    assert _scratch_entries(env) == [
        *_base_entries(checkout / ".git"),
        "remote.origin.url=https://github.com/octo/hello.git",
        "remote.origin.fetch=+refs/heads/*:refs/remotes/origin/*",
        "remote.origin.pushurl=https://github.com/octo/push.git",
        "branch.main.remote=origin",
        "branch.main.merge=refs/heads/main",
    ]


def test_build_isolation_points_git_at_the_scratch_and_the_real_state(
    checkout: Path, scratch_parent: Path
) -> None:
    """The env names the scratch common dir and the real git dir and objects.

    The global and system config are /dev/null, GIT_ALLOW_PROTOCOL is gone,
    and the rest of the environment is kept.
    """
    environ = {"GH_TOKEN": "t", "GIT_ALLOW_PROTOCOL": "file:ext"}

    env = _build(checkout, scratch_parent, environ)

    scratch = Path(env["GIT_COMMON_DIR"])
    names = (
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GH_TOKEN",
    )
    assert scratch.parent == scratch_parent
    assert {name: env.get(name) for name in names} == {
        "GIT_DIR": str(checkout / ".git"),
        "GIT_COMMON_DIR": str(scratch),
        "GIT_OBJECT_DIRECTORY": str(checkout / ".git" / "objects"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GH_TOKEN": "t",
    }
    assert "GIT_ALLOW_PROTOCOL" not in env


def test_build_isolation_lays_out_the_scratch_common_dir(
    checkout: Path, scratch_parent: Path
) -> None:
    """The scratch holds a stub HEAD, an empty refs/, and links into the real .git."""
    env = _build(checkout, scratch_parent)

    scratch = Path(env["GIT_COMMON_DIR"])
    real = checkout / ".git"
    assert sorted(path.name for path in scratch.iterdir()) == [
        "HEAD",
        "config",
        "info",
        "lfs",
        "refs",
        "worktrees",
    ]
    assert (scratch / "HEAD").read_text() == "ref: refs/heads/.invalid\n"
    assert list((scratch / "refs").iterdir()) == []
    assert {name: (scratch / name).readlink() for name in cage.LINKED_ENTRIES} == {
        name: real / name for name in cage.LINKED_ENTRIES
    }
    assert (real / "worktrees").is_dir()


def test_build_isolation_resolves_a_linked_worktree_by_its_files(
    checkout: Path, scratch_parent: Path, tmp_path: Path
) -> None:
    """A linked worktree's git dir is its own; its common dir is the main .git."""
    worktree = tmp_path / "root" / "wt"
    _host_git(checkout, "worktree", "add", "-q", "--orphan", str(worktree))

    env = _build(worktree, scratch_parent)

    real = checkout / ".git"
    assert (env["GIT_DIR"], env["GIT_OBJECT_DIRECTORY"]) == (
        str(real / "worktrees" / "wt"),
        str(real / "objects"),
    )
    assert _scratch_entries(env)[2] == f"extensions.refstorage=files://{real}"


def test_build_isolation_resolves_a_relative_gitdir_against_the_repo(
    checkout: Path, scratch_parent: Path, tmp_path: Path
) -> None:
    """A .git file's relative gitdir is taken from the repository's directory."""
    linked = tmp_path / "root" / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../repo/.git\n")

    env = _build(linked, scratch_parent)

    assert env["GIT_DIR"] == str(checkout / ".git")


@pytest.mark.parametrize("place", ["plain", "subdirectory"])
def test_build_isolation_refuses_a_directory_that_is_no_checkouts_top_level(
    checkout: Path, scratch_parent: Path, tmp_path: Path, place: str
) -> None:
    """A directory with no .git, even one inside a checkout, is refused."""
    directory = tmp_path / "plain" if place == "plain" else checkout / "sub"
    directory.mkdir()

    with pytest.raises(cage.RefusalError) as refused:
        _build(directory, scratch_parent)

    assert str(refused.value) == NOT_TOP_LEVEL.format(directory)
    assert list(scratch_parent.iterdir()) == []


def test_build_isolation_leaves_a_missing_directory_for_git_to_report(
    tmp_path: Path, scratch_parent: Path
) -> None:
    """A repository path that does not exist gets <repo>/.git, and git's own error."""
    missing = tmp_path / "missing"

    env = _build(missing, scratch_parent)

    assert env["GIT_DIR"] == str(missing / ".git")


def _set_real(checkout: Path, key: str, value: str) -> None:
    """Set key in checkout's real config file, which git reads as a plain file."""
    config = checkout / ".git" / "config"
    _host_git(checkout.parent, "config", "--file", str(config), key, value)


def test_build_isolation_refuses_a_shallow_clone(
    checkout: Path, scratch_parent: Path
) -> None:
    """A shallow file in the common dir marks a shallow clone, which is refused."""
    (checkout / ".git" / "shallow").write_text("0" * 40 + "\n")

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == SHALLOW.format(checkout)
    assert list(scratch_parent.iterdir()) == []


def test_build_isolation_refuses_a_linked_worktree_of_a_shallow_clone(
    checkout: Path, scratch_parent: Path, tmp_path: Path
) -> None:
    """The shallow file is read from the common dir, not the worktree's git dir."""
    worktree = tmp_path / "root" / "wt"
    _host_git(checkout, "worktree", "add", "-q", "--orphan", str(worktree))
    (checkout / ".git" / "shallow").write_text("0" * 40 + "\n")

    with pytest.raises(cage.RefusalError) as refused:
        _build(worktree, scratch_parent)

    assert str(refused.value) == SHALLOW.format(worktree)


@pytest.mark.parametrize("value", ["reftable", "reftable:///elsewhere"])
def test_build_isolation_refuses_a_reftable_ref_storage_setting(
    checkout: Path, scratch_parent: Path, value: str
) -> None:
    """extensions.refStorage naming reftable in the real config is refused."""
    _set_real(checkout, "extensions.refStorage", value)

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == REFTABLE.format(checkout)


def test_build_isolation_refuses_a_reftable_directory(
    checkout: Path, scratch_parent: Path
) -> None:
    """A reftable/ directory in the common dir is refused whatever the config says."""
    (checkout / ".git" / "reftable").mkdir()

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == REFTABLE.format(checkout)


def test_build_isolation_refuses_a_gitmodules_file(
    checkout: Path, scratch_parent: Path
) -> None:
    """A .gitmodules in the working tree is refused, tracked or not."""
    (checkout / ".gitmodules").write_text('[submodule "s"]\n\tpath = s\n')

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == SUBMODULES.format(checkout)


def test_build_isolation_refuses_a_submodule_config_entry(
    checkout: Path, scratch_parent: Path
) -> None:
    """A submodule.* key in the real config is refused."""
    _set_real(checkout, "submodule.s.url", "https://github.com/octo/s.git")

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == SUBMODULES.format(checkout)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[extensions]\n\tobjectFormat = sha256\n", "extensions.objectformat"),
        ("[Extensions]\n\tPartialClone = origin\n", "extensions.partialclone"),
        ("[extensions]\n\tnoop\n", "extensions.noop"),
    ],
    ids=["object-format", "partial-clone-any-case", "unknown"],
)
def test_build_isolation_refuses_an_extension_it_does_not_allow(
    checkout: Path, scratch_parent: Path, text: str, key: str
) -> None:
    """An extensions.* key beyond files refStorage and worktreeConfig is refused."""
    with (checkout / ".git" / "config").open("a") as config:
        config.write(text)

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == EXTENSION.format(checkout, key)


def test_build_isolation_refuses_a_ref_store_other_than_files(
    checkout: Path, scratch_parent: Path
) -> None:
    """A refStorage naming a files store at some path is refused for its value."""
    _set_real(checkout, "extensions.refStorage", "files:///elsewhere")

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == REF_STORE.format(checkout)


@pytest.mark.parametrize("value", [None, "true", "Yes", "1"])
def test_build_isolation_refuses_a_promisor_remote(
    checkout: Path, scratch_parent: Path, value: str | None
) -> None:
    """A remote marked promisor, as git clone --filter writes it, is refused."""
    setting = "\tpromisor\n" if value is None else f"\tpromisor = {value}\n"
    with (checkout / ".git" / "config").open("a") as config:
        config.write(f'[remote "origin"]\n{setting}')

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value) == PARTIAL.format(checkout)


@pytest.mark.parametrize("value", ["false", "Off", "0", ""])
def test_build_isolation_allows_a_remote_marked_not_promisor(
    checkout: Path, scratch_parent: Path, value: str
) -> None:
    """A promisor setting git reads as false marks no partial clone."""
    _set_real(checkout, "remote.origin.promisor", value)

    env = _build(checkout, scratch_parent)

    assert Path(env["GIT_COMMON_DIR"]).parent == scratch_parent


def test_build_isolation_allows_files_ref_storage_and_worktree_config(
    checkout: Path, scratch_parent: Path
) -> None:
    """The isolation supports refStorage = files and worktreeConfig."""
    with (checkout / ".git" / "config").open("a") as config:
        config.write("[Extensions]\n\tRefStorage = files\n\tworktreeConfig = true\n")

    env = _build(checkout, scratch_parent)

    assert Path(env["GIT_COMMON_DIR"]).parent == scratch_parent


def test_build_isolation_keeps_quoted_names_and_values_whole(
    checkout: Path, scratch_parent: Path
) -> None:
    """A quoted remote name and URL read back from the scratch config unchanged.

    The name holds a quote and a backslash; the URL adds a newline and a tab.
    """
    key = 'remote.we"i\\rd.url'
    value = 'https://github.com/octo/a"b\\c\n\tx = y'
    _host_git(checkout, "config", key, value)

    env = _build(checkout, scratch_parent)

    config = str(Path(env["GIT_COMMON_DIR"]) / "config")
    assert _host_git(checkout, "config", "--file", config, "-z", "--get", key) == (
        f"{value}\0"
    )


@pytest.mark.parametrize(
    ("environ", "committer"),
    [
        (
            {"GIT_AUTHOR_NAME": "Agent", "GIT_AUTHOR_EMAIL": "agent@example.com"},
            ("Agent", "agent@example.com"),
        ),
        (
            {
                "GIT_AUTHOR_NAME": "Agent",
                "GIT_AUTHOR_EMAIL": "agent@example.com",
                "GIT_COMMITTER_NAME": "Bot",
                "GIT_COMMITTER_EMAIL": "bot@example.com",
            },
            ("Bot", "bot@example.com"),
        ),
        ({}, (None, None)),
    ],
    ids=["author-only", "committer-set", "no-identity"],
)
def test_build_isolation_takes_an_unset_committer_from_the_author(
    checkout: Path,
    scratch_parent: Path,
    environ: dict[str, str],
    committer: tuple[str | None, str | None],
) -> None:
    """The committer defaults to the grant's author; a set committer is kept."""
    env = _build(checkout, scratch_parent, environ)

    assert (env.get("GIT_COMMITTER_NAME"), env.get("GIT_COMMITTER_EMAIL")) == committer


def test_build_isolation_refuses_a_dot_git_file_that_names_no_directory(
    tmp_path: Path, scratch_parent: Path
) -> None:
    """A .git file that does not begin with 'gitdir: ' is refused."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("nothing here\n")

    with pytest.raises(cage.RefusalError) as refused:
        _build(repo, scratch_parent)

    assert str(refused.value) == f"{repo}/.git names no git directory"


def test_build_isolation_refuses_a_dot_git_file_it_cannot_read(
    tmp_path: Path, scratch_parent: Path
) -> None:
    """A .git file the cage cannot read is refused, naming the file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    dot_git = repo / ".git"
    dot_git.write_text("gitdir: /elsewhere\n")
    dot_git.chmod(0)

    with pytest.raises(cage.RefusalError) as refused:
        _build(repo, scratch_parent)

    assert str(refused.value).startswith(f"{dot_git} could not be read: ")


def test_build_isolation_refuses_a_config_git_cannot_parse(
    checkout: Path, scratch_parent: Path
) -> None:
    """A real config git cannot read is refused with git's reason."""
    (checkout / ".git" / "config").write_text("[[[\n")

    with pytest.raises(cage.RefusalError) as refused:
        _build(checkout, scratch_parent)

    assert str(refused.value).startswith(
        f"the repository's config {checkout}/.git/config could not be read: fatal: "
    )


def test_wrapper_refuses_what_the_builder_refuses(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A refusal from the real builder is the wrapper's, before git runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("nothing here\n")
    environ = {"AGENT_PROXY_ROOTS": "[]"}

    exit_code = cage.wrapper(["git-cage", "status", "--repo", str(repo)], environ)

    assert exit_code == 2
    assert capsys.readouterr().err == (
        f"git plugin refused: {repo}/.git names no git directory\n"
    )
