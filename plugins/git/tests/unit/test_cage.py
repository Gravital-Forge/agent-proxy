import pytest
from git_plugin import cage

HELPER = (
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!f() { echo username=x-access-token; "
    'echo "password=$GH_TOKEN"; }; f',
)
SHAPE_REFUSAL = "AGENT_PROXY_ROOTS must be a JSON array of absolute paths, got {!r}"


def test_git_argv_orders_repo_roots_helper_then_args() -> None:
    """git_argv gives -C and the repo, two values per root, the helper, the args."""
    argv = cage.git_argv("/work/repo", ["/work", "/other"], ["log", "--oneline"])

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
        *HELPER,
        "log",
        "--oneline",
    ]


def test_git_argv_with_no_roots_trusts_no_directory() -> None:
    """With no roots, git_argv passes no safe.directory value."""
    argv = cage.git_argv("/repo", [], ["status"])

    assert argv == ["git", "-C", "/repo", *HELPER, "status"]


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
