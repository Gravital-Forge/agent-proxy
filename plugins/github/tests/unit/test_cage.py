import pytest
from github_plugin import cage

LISTED = {"GITHUB_REPOS": "o/n"}
NO_REPOS_STDERR = (
    "github plugin refused: "
    "GITHUB_REPOS is not set or names no repository; the grant's env sets it\n"
)


class _ExecedError(Exception):
    """Raised in place of os.execvp; its args are execvp's file and argv."""


@pytest.fixture(autouse=True)
def _no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make os.execvp raise _ExecedError instead of replacing the test process."""

    def fake_execvp(file: str, args: list[str]) -> None:
        raise _ExecedError(file, args)

    monkeypatch.setattr(cage.os, "execvp", fake_execvp)


def test_wrapper_becomes_gh_with_its_args_when_the_repo_is_listed() -> None:
    """A listed repo replaces the wrapper with gh and the same args, --repo kept."""
    with pytest.raises(_ExecedError) as execed:
        cage.wrapper(["gh-cage", "run", "list", "--repo", "o/n"], LISTED)

    assert execed.value.args == ("gh", ["gh", "run", "list", "--repo", "o/n"])


def test_wrapper_matches_the_repo_without_regard_to_case() -> None:
    """An entry names the repo in any case, and the args reach gh unchanged."""
    args = ["run", "view", "2", "--repo", "o/N", "--log-failed"]

    with pytest.raises(_ExecedError) as execed:
        cage.wrapper(["gh-cage", *args], {"GITHUB_REPOS": "O/*"})

    assert execed.value.args == ("gh", ["gh", *args])


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["run", "list"], "--repo is missing"),
        (
            ["run", "list", "--repo", "o/n", "--repo", "o/n"],
            "--repo must appear once, got 2",
        ),
        (["run", "list", "--repo"], "--repo has no value"),
        (
            ["run", "list", "--repo", "other/n"],
            "repository 'other/n' is not in GITHUB_REPOS",
        ),
    ],
)
def test_wrapper_refuses_with_exit_2_before_gh_runs(
    capsys: pytest.CaptureFixture[str], args: list[str], reason: str
) -> None:
    """A missing, repeated, or valueless --repo, or an unlisted repo, is refused."""
    exit_code = cage.wrapper(["gh-cage", *args], LISTED)

    captured = capsys.readouterr()
    assert (exit_code, captured.out, captured.err) == (
        2,
        "",
        f"github plugin refused: {reason}\n",
    )


@pytest.mark.parametrize("environ", [{}, {"GITHUB_REPOS": ""}])
def test_wrapper_refuses_without_a_repository_list(
    capsys: pytest.CaptureFixture[str], environ: dict[str, str]
) -> None:
    """An unset or empty GITHUB_REPOS refuses before gh runs."""
    exit_code = cage.wrapper(["gh-cage", "run", "list", "--repo", "o/n"], environ)

    captured = capsys.readouterr()
    assert (exit_code, captured.out, captured.err) == (2, "", NO_REPOS_STDERR)


def test_wrapper_refuses_a_malformed_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed GITHUB_REPOS entry refuses before gh runs, naming the entry."""
    environ = {"GITHUB_REPOS": "o/n,o/api-*"}

    exit_code = cage.wrapper(["gh-cage", "run", "list", "--repo", "o/n"], environ)

    captured = capsys.readouterr()
    assert (exit_code, captured.out, captured.err) == (
        2,
        "",
        "github plugin refused: "
        "GITHUB_REPOS entry 'o/api-*' is not owner/name or owner/*\n",
    )
