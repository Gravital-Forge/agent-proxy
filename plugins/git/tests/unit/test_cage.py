import os
from itertools import pairwise
from pathlib import Path

import pytest
from git_plugin import cage

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
NO_REPOS_STDERR = (
    "git plugin refused: "
    "GITHUB_REPOS is not set or names no repository; the grant's env sets it\n"
)


class _ExecedError(Exception):
    """Raised in place of os.execvp; its args are execvp's file and argv."""


@pytest.fixture
def _no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make os.execvp raise _ExecedError instead of replacing the test process."""

    def fake_execvp(file: str, args: list[str]) -> None:
        raise _ExecedError(file, args)

    monkeypatch.setattr(cage.os, "execvp", fake_execvp)


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


def test_git_argv_orders_repo_roots_hooks_helper_signing_then_args() -> None:
    """git_argv gives -C, repo, two per root, hooks off, helper, signing, then args."""
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
        "log",
        "--oneline",
    ]


def test_git_argv_with_no_roots_trusts_no_directory() -> None:
    """With no roots, git_argv passes no safe.directory value."""
    argv = cage.git_argv("/repo", [], ["status"], {})

    assert argv == ["git", "-C", "/repo", *HOOKS_OFF, *HELPER, *SIGNING_OFF, "status"]


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
        "commit",
        "-m",
        "x",
    ]


def test_git_argv_without_a_signing_key_turns_signing_off() -> None:
    """With GIT_SIGNING_KEY unset, git_argv passes commit.gpgSign=false and no key."""
    argv = cage.git_argv("/repo", [], ["commit", "-m", "x"], {"GH_TOKEN": "t"})

    assert argv[-5:] == [*SIGNING_OFF, "commit", "-m", "x"]
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

    assert execed.value.args == (
        "git",
        cage.git_argv("/repo", [], ["fetch"], environ),
    )


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

    assert execed.value.args == (
        "git",
        [
            "git",
            "-C",
            "/repo",
            *HOOKS_OFF,
            *HELPER,
            *_signing_on(str(key)),
            "commit",
            "-m",
            "x",
        ],
    )
