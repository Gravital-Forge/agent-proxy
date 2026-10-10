import dataclasses
import re
from collections.abc import Sequence

import pytest
from git_plugin import rules
from git_plugin.cage import RefusalError

TASK = ["task/"]
TASK_AND_DEPENDABOT = ["task/", "dependabot/"]
NO_PREFIXES = (
    r"^GIT_BRANCH_PREFIXES is not set or names no prefix; the grant's env sets it$"
)
NO_REPOS = r"^GITHUB_REPOS is not set or names no repository; the grant's env sets it$"
MALFORMED = "GITHUB_REPOS entry '{}' is not owner/name or owner/*"


def _prefix_refusal(branch: str, prefixes: Sequence[str] = ("task/",)) -> str:
    """Return the pattern of the refusal for a branch outside the prefixes."""
    named = re.escape(branch)
    allowed = re.escape(", ".join(prefixes))
    return rf"^Branch '{named}' does not match any allowed prefix: {allowed}$"


def test_protected_branches_are_main_and_master() -> None:
    """Only main and master are protected."""
    assert rules.PROTECTED_BRANCHES == {"main", "master"}


def test_the_prefixes_variable_is_git_branch_prefixes() -> None:
    """The prefixes come from GIT_BRANCH_PREFIXES, which the grant's env sets."""
    assert rules.PREFIXES_ENV_NAME == "GIT_BRANCH_PREFIXES"


@pytest.mark.parametrize(
    ("value", "prefixes"),
    [
        ("task/", ["task/"]),
        ("task/, dependabot/", ["task/", "dependabot/"]),
        (" ,task/,,", ["task/"]),
    ],
)
def test_branch_prefixes_splits_strips_and_drops_empty_entries(
    value: str, prefixes: list[str]
) -> None:
    """The value splits on commas; each entry is stripped and empties are dropped."""
    assert rules.branch_prefixes({"GIT_BRANCH_PREFIXES": value}) == prefixes


@pytest.mark.parametrize(
    "environ",
    [{}, {"GIT_BRANCH_PREFIXES": ""}, {"GIT_BRANCH_PREFIXES": " , "}],
)
def test_branch_prefixes_refuses_when_no_prefix_is_named(
    environ: dict[str, str],
) -> None:
    """An unset variable, or one that names no prefix, is refused by name."""
    with pytest.raises(RefusalError, match=NO_PREFIXES):
        rules.branch_prefixes(environ)


@pytest.mark.parametrize("branch", ["task/p", "task/a/b", "task/main"])
def test_push_branch_accepts_a_task_branch(branch: str) -> None:
    """A branch under task/ may be pushed."""
    rules.check_push_branch(branch, TASK)


def test_push_branch_accepts_a_branch_under_a_second_prefix() -> None:
    """A branch under any of the prefixes may be pushed."""
    rules.check_push_branch("dependabot/x", TASK_AND_DEPENDABOT)


@pytest.mark.parametrize("branch", ["main", "master"])
def test_push_branch_refuses_a_protected_branch(branch: str) -> None:
    """A protected branch is refused before the prefix is checked."""
    with pytest.raises(
        RefusalError, match=rf"^Cannot push to protected branch: {branch}$"
    ):
        rules.check_push_branch(branch, TASK)


@pytest.mark.parametrize(
    "branch",
    ["feature/x", "dependabot/x", "task", "tasks/x", "Task/x", "x/task/y", "HEAD", ""],
)
def test_push_branch_refuses_a_branch_without_the_prefix(branch: str) -> None:
    """A branch outside task/, a detached HEAD's 'HEAD' included, is refused."""
    with pytest.raises(RefusalError, match=_prefix_refusal(branch)):
        rules.check_push_branch(branch, TASK)


def test_push_branch_refusal_names_every_prefix() -> None:
    """The refusal lists the prefixes joined by a comma and a space."""
    with pytest.raises(
        RefusalError, match=_prefix_refusal("feature/x", TASK_AND_DEPENDABOT)
    ):
        rules.check_push_branch("feature/x", TASK_AND_DEPENDABOT)


@pytest.mark.parametrize("target", ["main", "master", "task/c", "task/a/b"])
def test_checkout_target_accepts_a_protected_or_task_branch(target: str) -> None:
    """A protected branch or a branch under task/ may be checked out."""
    rules.check_checkout_target(target, TASK)


def test_checkout_target_accepts_a_branch_under_a_second_prefix() -> None:
    """A branch under any of the prefixes may be checked out."""
    rules.check_checkout_target("dependabot/x", TASK_AND_DEPENDABOT)


@pytest.mark.parametrize(
    "target",
    [
        "feature/x",
        "dependabot/x",
        "tasks/x",
        "HEAD",
        "HEAD~1",
        "v1",
        "origin/main",
        "-",
        "",
    ],
)
def test_checkout_target_refuses_any_other_target(target: str) -> None:
    """A tag, a commit, a remote branch, or any other branch is refused."""
    with pytest.raises(RefusalError, match=_prefix_refusal(target)):
        rules.check_checkout_target(target, TASK)


def test_checkout_target_refusal_names_every_prefix() -> None:
    """The refusal lists the prefixes joined by a comma and a space."""
    with pytest.raises(
        RefusalError, match=_prefix_refusal("feature/x", TASK_AND_DEPENDABOT)
    ):
        rules.check_checkout_target("feature/x", TASK_AND_DEPENDABOT)


def test_the_repos_variable_is_github_repos() -> None:
    """The repository list comes from GITHUB_REPOS, which the grant's env sets."""
    assert rules.REPOS_ENV_NAME == "GITHUB_REPOS"


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
    parsed = rules.allowed_repos({"GITHUB_REPOS": value})

    assert parsed == [
        rules.RepoEntry(owner=owner, name=name) for owner, name in entries
    ]


def test_a_repo_entry_is_frozen() -> None:
    """A parsed entry cannot be changed after the parse."""
    entry = rules.RepoEntry(owner="octo", name="hello")

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
        rules.allowed_repos(environ)


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
        rules.allowed_repos({"GITHUB_REPOS": f"octo/hello,{entry}"})

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
    rules.check_repo(repo, rules.allowed_repos({"GITHUB_REPOS": value}))


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
        rules.check_repo(repo, rules.allowed_repos({"GITHUB_REPOS": value}))

    assert str(refused.value) == f"repository '{repo}' is not in GITHUB_REPOS"
