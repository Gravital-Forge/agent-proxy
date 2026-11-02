import dataclasses

import pytest
from hcp_terraform_plugin import workspaces
from hcp_terraform_plugin.errors import RefusalError

NO_WORKSPACES = (
    r"^HCP_TERRAFORM_WORKSPACES is not set or names no workspace; "
    r"the grant's env sets it$"
)
MALFORMED = (
    "HCP_TERRAFORM_WORKSPACES entry '{}' is not organization/name or organization/*"
)


def test_the_workspaces_variable_is_hcp_terraform_workspaces() -> None:
    """The workspace list comes from HCP_TERRAFORM_WORKSPACES, set by the grant."""
    assert workspaces.WORKSPACES_ENV_NAME == "HCP_TERRAFORM_WORKSPACES"


@pytest.mark.parametrize(
    ("value", "entries"),
    [
        ("lemonbrand/inbox", [("lemonbrand", "inbox")]),
        ("lemonbrand/*", [("lemonbrand", "*")]),
        ("Org-1/a_B-9", [("Org-1", "a_B-9")]),
        ("o_x/a", [("o_x", "a")]),
        (" o/a , ,p/*,", [("o", "a"), ("p", "*")]),
    ],
)
def test_allowed_workspaces_parses_each_entry_form(
    value: str, entries: list[tuple[str, str]]
) -> None:
    """Entries are organization/name or organization/*; the value splits and strips."""
    parsed = workspaces.allowed_workspaces({"HCP_TERRAFORM_WORKSPACES": value})

    assert parsed == [
        workspaces.WorkspaceEntry(organization=organization, name=name)
        for organization, name in entries
    ]


def test_a_workspace_entry_is_frozen() -> None:
    """A parsed entry cannot be changed after the parse."""
    entry = workspaces.WorkspaceEntry(organization="o", name="a")

    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.organization = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "environ",
    [{}, {"HCP_TERRAFORM_WORKSPACES": ""}, {"HCP_TERRAFORM_WORKSPACES": " , "}],
)
def test_allowed_workspaces_refuses_when_no_workspace_is_named(
    environ: dict[str, str],
) -> None:
    """An unset variable, or one that names no entry, is refused by name."""
    with pytest.raises(RefusalError, match=NO_WORKSPACES):
        workspaces.allowed_workspaces(environ)


@pytest.mark.parametrize(
    "entry",
    [
        "o",
        "*",
        "*/a",
        "o/a-*",
        "o/a/b",
        "-o/a",
        "_o/a",
        "o/",
        "/a",
        "o/a.b",
        "o.x/a",
        "o/a b",
        "o/-a",
        "o/_a",
    ],
)
def test_allowed_workspaces_refuses_a_malformed_entry(entry: str) -> None:
    """Any entry but organization/name or organization/* refuses the whole list."""
    with pytest.raises(RefusalError) as refused:
        workspaces.allowed_workspaces({"HCP_TERRAFORM_WORKSPACES": f"o/a,{entry}"})

    assert str(refused.value) == MALFORMED.format(entry)


@pytest.mark.parametrize(
    ("organization", "name", "value"),
    [
        ("o", "a", "o/a"),
        ("o", "anything", "o/*"),
        ("o", "a", "p/*,o/a"),
    ],
)
def test_check_workspace_accepts_a_listed_workspace(
    organization: str, name: str, value: str
) -> None:
    """A workspace an entry names passes."""
    workspaces.check_workspace(
        organization,
        name,
        workspaces.allowed_workspaces({"HCP_TERRAFORM_WORKSPACES": value}),
    )


@pytest.mark.parametrize(
    ("organization", "name", "value"),
    [
        ("o", "a", "o/b"),
        ("o", "a", "p/*"),
        ("o", "a", "o/ab"),
        ("o", "ab", "o/a"),
        ("O", "a", "o/a"),
        ("o", "A", "o/a"),
        ("op", "a", "o/*"),
    ],
)
def test_check_workspace_refuses_an_unlisted_workspace(
    organization: str, name: str, value: str
) -> None:
    """Names are compared exactly, case included; an unlisted workspace is refused."""
    with pytest.raises(RefusalError) as refused:
        workspaces.check_workspace(
            organization,
            name,
            workspaces.allowed_workspaces({"HCP_TERRAFORM_WORKSPACES": value}),
        )

    assert (
        str(refused.value)
        == f"workspace '{organization}/{name}' is not in HCP_TERRAFORM_WORKSPACES"
    )


def test_organizations_lists_each_organization_once_in_entry_order() -> None:
    """The organizations of the entries come out once each, in first-seen order."""
    entries = workspaces.allowed_workspaces(
        {"HCP_TERRAFORM_WORKSPACES": "o/a,p/*,o/b,q/c,p/d"}
    )

    assert workspaces.organizations(entries) == ["o", "p", "q"]
