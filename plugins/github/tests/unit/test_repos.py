import dataclasses

import pytest
from github_plugin import repos
from github_plugin.scripts import RefusalError

NO_REPOS = r"^GITHUB_REPOS is not set or names no repository; the grant's env sets it$"
MALFORMED = "GITHUB_REPOS entry '{}' is not owner/name or owner/*"


def test_the_repos_variable_is_github_repos() -> None:
    """The repository list comes from GITHUB_REPOS, which the grant's env sets."""
    assert repos.REPOS_ENV_NAME == "GITHUB_REPOS"


@pytest.mark.parametrize(
    ("value", "entries"),
    [
        ("octo/hello", [("octo", "hello")]),
        ("octo/*", [("octo", "*")]),
        ("octo/.github", [("octo", ".github")]),
        ("Octo-Org/a_b-c.d", [("Octo-Org", "a_b-c.d")]),
        (" octo/hello , ,other/*,", [("octo", "hello"), ("other", "*")]),
    ],
)
def test_allowed_repos_parses_each_entry_form(
    value: str, entries: list[tuple[str, str]]
) -> None:
    """Entries are owner/name or owner/*; the value splits, strips, drops empties."""
    parsed = repos.allowed_repos({"GITHUB_REPOS": value})

    assert parsed == [
        repos.RepoEntry(owner=owner, name=name) for owner, name in entries
    ]


def test_a_repo_entry_is_frozen() -> None:
    """A parsed entry cannot be changed after the parse."""
    entry = repos.RepoEntry(owner="octo", name="hello")

    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.owner = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "environ",
    [{}, {"GITHUB_REPOS": ""}, {"GITHUB_REPOS": " , "}],
)
def test_allowed_repos_refuses_when_no_repository_is_named(
    environ: dict[str, str],
) -> None:
    """An unset variable, or one that names no entry, is refused by name."""
    with pytest.raises(RefusalError, match=NO_REPOS):
        repos.allowed_repos(environ)


@pytest.mark.parametrize(
    "entry",
    [
        "octo",
        "*",
        "*/hello",
        "octo/api-*",
        "octo/a/b",
        "-octo/hello",
        "oc_to/hello",
        "octo/",
        "/hello",
        "octo/..",
        "octo/hel lo",
    ],
)
def test_allowed_repos_refuses_a_malformed_entry(entry: str) -> None:
    """Any entry but owner/name or owner/* refuses the whole list, naming it."""
    with pytest.raises(RefusalError) as refused:
        repos.allowed_repos({"GITHUB_REPOS": f"octo/hello,{entry}"})

    assert str(refused.value) == MALFORMED.format(entry)


@pytest.mark.parametrize(
    ("repo", "value"),
    [
        ("octo/hello", "octo/hello"),
        ("OCTO/Hello", "octo/hello"),
        ("octo/hello", "Octo/HELLO"),
        ("octo/anything", "octo/*"),
        ("Octo/anything", "octo/*"),
        ("octo/hello", "other/*,octo/hello"),
    ],
)
def test_check_repo_accepts_a_listed_repository(repo: str, value: str) -> None:
    """A repository an entry names, compared without regard to case, passes."""
    repos.check_repo(repo, repos.allowed_repos({"GITHUB_REPOS": value}))


@pytest.mark.parametrize(
    ("repo", "value"),
    [
        ("octo/hello", "octo/world"),
        ("octo/hello", "other/*"),
        ("octo/hello", "octo/hello-world"),
        ("octo/hello-world", "octo/hello"),
        ("octocat/hello", "octo/*"),
    ],
)
def test_check_repo_refuses_an_unlisted_repository(repo: str, value: str) -> None:
    """A repository no entry names is refused, naming it."""
    with pytest.raises(RefusalError) as refused:
        repos.check_repo(repo, repos.allowed_repos({"GITHUB_REPOS": value}))

    assert str(refused.value) == f"repository '{repo}' is not in GITHUB_REPOS"
