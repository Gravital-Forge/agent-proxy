"""The workspace list, from the grant's env.

The workspaces come from HCP_TERRAFORM_WORKSPACES, which the grant's env sets.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hcp_terraform_plugin.errors import RefusalError

WORKSPACES_ENV_NAME = "HCP_TERRAFORM_WORKSPACES"
ANY_NAME = "*"
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class WorkspaceEntry:
    """One entry: a workspace, or with name '*', all of organization's."""

    organization: str
    name: str


def allowed_workspaces(environ: Mapping[str, str]) -> list[WorkspaceEntry]:
    """Return the entries environ's HCP_TERRAFORM_WORKSPACES names, in order.

    The value is split on commas, each entry stripped, and empty entries
    dropped.

    Raises:
        RefusalError: the variable is unset, names no entry, or holds an
            entry that is not organization/name or organization/*.
    """
    entries = environ.get(WORKSPACES_ENV_NAME, "").split(",")
    stripped = [entry.strip() for entry in entries if entry.strip()]
    if not stripped:
        raise RefusalError(
            f"{WORKSPACES_ENV_NAME} is not set or names no workspace; "
            "the grant's env sets it"
        )
    return [_workspace_entry(entry) for entry in stripped]


def _workspace_entry(entry: str) -> WorkspaceEntry:
    """Return entry parsed as organization/name or organization/*.

    Raises:
        RefusalError: entry has any other form.
    """
    organization, slash, name = entry.partition("/")
    if not (
        slash
        and NAME_PATTERN.fullmatch(organization)
        and (name == ANY_NAME or NAME_PATTERN.fullmatch(name))
    ):
        raise RefusalError(
            f"{WORKSPACES_ENV_NAME} entry '{entry}' is not "
            "organization/name or organization/*"
        )
    return WorkspaceEntry(organization=organization, name=name)


def is_allowed(organization: str, name: str, entries: Sequence[WorkspaceEntry]) -> bool:
    """Return whether one of entries names the workspace organization/name.

    Names are compared exactly: a refusal is the safe side of a case mismatch.
    """
    return any(
        entry.organization == organization and entry.name in (ANY_NAME, name)
        for entry in entries
    )


def check_workspace(
    organization: str, name: str, entries: Sequence[WorkspaceEntry]
) -> None:
    """Return if one of entries names the workspace organization/name.

    Raises:
        RefusalError: no entry names the workspace.
    """
    if not is_allowed(organization, name, entries):
        raise RefusalError(
            f"workspace '{organization}/{name}' is not in {WORKSPACES_ENV_NAME}"
        )


def organizations(entries: Sequence[WorkspaceEntry]) -> list[str]:
    """Return each organization entries name, once, in entry order."""
    return list(dict.fromkeys(entry.organization for entry in entries))
