import io

import pytest
from git_plugin import credential

HELPER = "/plugin/bin/git-credential-agent-proxy"
TOKEN = "dummy-token"
LISTED = {"GITHUB_REPOS": "octo/hello", "GH_TOKEN": TOKEN}
GRANTED = f"username=x-access-token\npassword={TOKEN}\n"
QUIT = "quit=1\n"
PREFIX = "git plugin refused: "
NO_REPOS = "GITHUB_REPOS is not set or names no repository; the grant's env sets it"
NO_PATH = (
    "credential request names no path; git sends one only under credential.useHttpPath"
)


def _request(path: str | None, host: str = "github.com") -> io.StringIO:
    """Return git's request on stdin, with every key git sends, path if given."""
    lines = [
        "capability[]=authtype",
        "capability[]=state",
        "protocol=https",
        f"host={host}",
    ]
    if path is not None:
        lines.append(f"path={path}")
    lines.append('wwwauth[]=Basic realm="GitHub"')
    return io.StringIO("".join(f"{line}\n" for line in lines))


def _answer(
    capsys: pytest.CaptureFixture[str],
    operation: str,
    stdin: io.StringIO,
    environ: dict[str, str],
) -> tuple[int, str, str]:
    """Run the helper for operation; return its exit code, stdout, and stderr."""
    exit_code = credential.helper([HELPER, operation], stdin, environ)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


@pytest.mark.parametrize(
    "path",
    ["octo/hello.git", "octo/hello", "octo/hello.git/info/lfs", "Octo/HELLO.git"],
)
def test_get_answers_the_token_for_a_listed_path(
    capsys: pytest.CaptureFixture[str], path: str
) -> None:
    """A listed repository gets the token, with or without .git, in any case."""
    assert _answer(capsys, "get", _request(path), LISTED) == (0, GRANTED, "")


def test_get_answers_the_token_for_any_repository_of_a_listed_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An owner/* entry lists every repository of that owner."""
    environ = {"GITHUB_REPOS": "octo/*", "GH_TOKEN": TOKEN}

    answer = _answer(capsys, "get", _request("octo/anything.git"), environ)

    assert answer == (0, GRANTED, "")


def test_get_ignores_the_host(capsys: pytest.CaptureFixture[str]) -> None:
    """The host plays no part in the answer: the grant's domains bound it."""
    request = _request("octo/hello.git", host="example.invalid")

    assert _answer(capsys, "get", request, LISTED) == (0, GRANTED, "")


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("other/hello.git", "repository 'other/hello' is not in GITHUB_REPOS"),
        ("octo/world.git", "repository 'octo/world' is not in GITHUB_REPOS"),
        ("octo/hello.git.git", "repository 'octo/hello.git' is not in GITHUB_REPOS"),
        ("octo", "credential path 'octo' names no owner/name"),
        ("hello.git", "credential path 'hello.git' names no owner/name"),
    ],
)
def test_get_refuses_a_path_that_names_no_listed_repository(
    capsys: pytest.CaptureFixture[str], path: str, reason: str
) -> None:
    """The refusal goes to stderr, and quit=1 makes git stop asking."""
    answer = _answer(capsys, "get", _request(path), LISTED)

    assert answer == (0, QUIT, f"{PREFIX}{reason}\n")


def test_get_refuses_a_request_with_no_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without useHttpPath git sends no path, and the helper refuses."""
    answer = _answer(capsys, "get", _request(None), LISTED)

    assert answer == (0, QUIT, f"{PREFIX}{NO_PATH}\n")


@pytest.mark.parametrize(
    "environ",
    [{"GITHUB_REPOS": "octo/hello"}, {"GITHUB_REPOS": "octo/hello", "GH_TOKEN": ""}],
    ids=["unset", "empty"],
)
def test_get_refuses_without_a_token(
    capsys: pytest.CaptureFixture[str], environ: dict[str, str]
) -> None:
    """An unset or empty GH_TOKEN is refused by name."""
    answer = _answer(capsys, "get", _request("octo/hello.git"), environ)

    assert answer == (0, QUIT, f"{PREFIX}GH_TOKEN is not set or empty\n")


@pytest.mark.parametrize(
    ("environ", "reason"),
    [
        ({"GH_TOKEN": TOKEN}, NO_REPOS),
        ({"GH_TOKEN": TOKEN, "GITHUB_REPOS": " , "}, NO_REPOS),
        (
            {"GH_TOKEN": TOKEN, "GITHUB_REPOS": "octo/hello,octo"},
            "GITHUB_REPOS entry 'octo' is not owner/name or owner/*",
        ),
    ],
    ids=["unset", "empty", "malformed"],
)
def test_get_refuses_a_list_that_is_unset_empty_or_malformed(
    capsys: pytest.CaptureFixture[str], environ: dict[str, str], reason: str
) -> None:
    """The list's own refusal is the helper's answer."""
    answer = _answer(capsys, "get", _request("octo/hello.git"), environ)

    assert answer == (0, QUIT, f"{PREFIX}{reason}\n")


def test_get_reads_the_request_to_its_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The helper reads all of git's request before it answers."""
    stdin = _request("octo/hello.git")

    _answer(capsys, "get", stdin, LISTED)

    assert stdin.read() == ""


@pytest.mark.parametrize("operation", ["store", "erase", "capability"])
def test_any_other_operation_reads_the_request_and_answers_nothing(
    capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    """store, erase, and any other operation get no answer and exit 0."""
    stdin = _request("octo/hello.git")

    assert _answer(capsys, operation, stdin, LISTED) == (0, "", "")
    assert stdin.read() == ""


def test_a_helper_with_no_operation_answers_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run with no operation, the helper answers nothing and exits 0."""
    exit_code = credential.helper([HELPER], _request("octo/hello.git"), LISTED)

    captured = capsys.readouterr()
    assert (exit_code, captured.out, captured.err) == (0, "", "")
