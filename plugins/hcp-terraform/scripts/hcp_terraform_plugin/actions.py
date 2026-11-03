"""The five script actions, and run_script, which turns their errors into results.

Every action reads the workspace list first. An action that takes a run ID
reads only the run and its workspace before it checks that workspace.
"""

import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import NotRequired, TypedDict

from hcp_terraform_plugin import api, workspaces
from hcp_terraform_plugin.errors import ApiError, RefusalError
from hcp_terraform_plugin.workspaces import WorkspaceEntry

REFUSAL_EXIT = 2
FAILURE_EXIT = 1
REFUSAL_PREFIX = "hcp-terraform plugin refused: "
WORKSPACE_PAGE_SIZE = 100
DEFAULT_RUN_LIMIT = 20
RUN_OPERATIONS = "plan_only,plan_and_apply,save_plan,refresh_only,destroy,empty_apply"
NOT_FOUND = 404


class RunListInput(TypedDict):
    organization: str
    workspace: str
    limit: NotRequired[int]


class RunInput(TypedDict):
    run_id: str


def workspace_list(
    arguments: Mapping[str, object], entries: Sequence[WorkspaceEntry]
) -> dict[str, object]:
    """Return every workspace entries allow, across the organizations they name."""
    allowed = []
    for organization in workspaces.organizations(entries):
        for workspace in _organization_workspaces(organization):
            name = _member(workspace, "attributes", "name")
            if isinstance(name, str) and workspaces.is_allowed(
                organization, name, entries
            ):
                allowed.append(
                    {
                        "organization": organization,
                        "name": name,
                        "id": _member(workspace, "id"),
                        "current_run_id": _member(
                            workspace, "relationships", "current-run", "data", "id"
                        ),
                    }
                )
    return {"workspaces": allowed}


def _organization_workspaces(organization: str) -> Iterator[object]:
    """Yield each workspace of organization, page by page.

    Raises:
        ApiError: a read failed, a page holds no data list, or a page's
            next-page is not a later page number.
    """
    path = f"/organizations/{organization}/workspaces"
    page = 1
    while True:
        answer = api.get_json(
            path, {"page[size]": str(WORKSPACE_PAGE_SIZE), "page[number]": str(page)}
        )
        yield from _data_items(path, answer)
        next_page = _member(answer, "meta", "pagination", "next-page")
        if next_page is None:
            return
        if not isinstance(next_page, int) or next_page <= page:
            raise ApiError(
                f"GET {path} answered next-page {next_page!r}, not a page after {page}"
            )
        page = next_page


def run_list(
    arguments: RunListInput, entries: Sequence[WorkspaceEntry]
) -> dict[str, object]:
    """Return the runs of the workspace arguments name, newest first."""
    organization = arguments["organization"]
    name = arguments["workspace"]
    workspaces.check_workspace(organization, name, entries)
    workspace = api.get_json(f"/organizations/{organization}/workspaces/{name}")
    workspace_id = api.safe_id(_member(workspace, "data", "id"))
    path = f"/workspaces/{workspace_id}/runs"
    limit = arguments.get("limit", DEFAULT_RUN_LIMIT)
    answer = api.get_json(
        path, {"page[size]": str(limit), "filter[operation]": RUN_OPERATIONS}
    )
    return {"runs": [_run_summary(run) for run in _data_items(path, answer)]}


def _run_summary(run: object) -> dict[str, object]:
    """Return the fields run_list gives for run."""
    return {
        "id": _member(run, "id"),
        "status": _member(run, "attributes", "status"),
        "created_at": _member(run, "attributes", "created-at"),
        "source": _member(run, "attributes", "source"),
        "message": _member(run, "attributes", "message"),
        "plan_only": _member(run, "attributes", "plan-only"),
        "has_changes": _member(run, "attributes", "has-changes"),
    }


def run_view(
    arguments: RunInput, entries: Sequence[WorkspaceEntry]
) -> dict[str, object]:
    """Return the run arguments name: its status, workspace, commit, plan, and apply."""
    run, workspace = _allowed_run(arguments["run_id"], entries)
    plan = _related(run, "plan", "plans")
    apply = _related(run, "apply", "applies")
    commit = _commit(run)
    return {
        "run": {
            "id": _member(run, "id"),
            "status": _member(run, "attributes", "status"),
            "status_timestamps": _member(run, "attributes", "status-timestamps"),
            "workspace": workspace,
            "commit": commit,
            "plan": _plan_summary(plan),
            "apply": _apply_summary(apply),
        }
    }


def _plan_summary(plan: object) -> dict[str, object] | None:
    """Return the fields run_view gives for plan, or None when there is none."""
    if plan is None:
        return None
    return {
        "id": _member(plan, "id"),
        "status": _member(plan, "attributes", "status"),
        "resource_additions": _member(plan, "attributes", "resource-additions"),
        "resource_changes": _member(plan, "attributes", "resource-changes"),
        "resource_destructions": _member(plan, "attributes", "resource-destructions"),
    }


def _apply_summary(apply: object) -> dict[str, object] | None:
    """Return the fields run_view gives for apply, or None when there is none."""
    if apply is None:
        return None
    return {
        "id": _member(apply, "id"),
        "status": _member(apply, "attributes", "status"),
    }


def _commit(run: object) -> dict[str, object] | None:
    """Return the commit that triggered run, or None when no commit did.

    Raises:
        ApiError: the ingress attributes read failed other than with a 404.
    """
    version_id = _member(run, "relationships", "configuration-version", "data", "id")
    if version_id is None:
        return None
    path = f"/configuration-versions/{api.safe_id(version_id)}/ingress-attributes"
    try:
        answer = api.get_json(path)
    except ApiError as failure:
        if failure.status == NOT_FOUND:
            return None
        raise
    return {
        "sha": _member(answer, "data", "attributes", "commit-sha"),
        "branch": _member(answer, "data", "attributes", "branch"),
        "url": _member(answer, "data", "attributes", "commit-url"),
    }


def plan_log(
    arguments: RunInput, entries: Sequence[WorkspaceEntry]
) -> dict[str, object]:
    """Return the log of the plan of the run arguments name."""
    return _log(arguments["run_id"], entries, "plan", "plans")


def apply_log(
    arguments: RunInput, entries: Sequence[WorkspaceEntry]
) -> dict[str, object]:
    """Return the log of the apply of the run arguments name."""
    return _log(arguments["run_id"], entries, "apply", "applies")


def _log(
    run_id: str, entries: Sequence[WorkspaceEntry], relationship: str, collection: str
) -> dict[str, object]:
    """Return the log of the plan or apply that run_id's relationship names.

    Raises:
        RefusalError: entries do not allow the run's workspace, or the
            log-read-url is off the log host.
        ApiError: a read failed, or the run has no such object or no
            log-read-url yet.
    """
    run, _workspace = _allowed_run(run_id, entries)
    related = _related(run, relationship, collection)
    url = _member(related, "attributes", "log-read-url")
    if not isinstance(url, str):
        raise ApiError(f"run {run_id!r} has no {relationship} log yet")
    return {"log": api.get_log(url)}


def _allowed_run(run_id: str, entries: Sequence[WorkspaceEntry]) -> tuple[object, str]:
    """Return the run run_id names, and its workspace as organization/name.

    Only the run and its workspace are read before entries are checked.

    Raises:
        RefusalError: entries do not allow the run's workspace.
        ApiError: a read failed, an ID is unsafe in a path, or the workspace
            answered no organization and name.
    """
    run = _member(api.get_json(f"/runs/{api.safe_id(run_id)}"), "data")
    workspace_id = api.safe_id(_member(run, "relationships", "workspace", "data", "id"))
    workspace = _member(api.get_json(f"/workspaces/{workspace_id}"), "data")
    organization = _member(workspace, "relationships", "organization", "data", "id")
    name = _member(workspace, "attributes", "name")
    if not (isinstance(organization, str) and isinstance(name, str)):
        raise ApiError(f"workspace {workspace_id!r} answered no organization and name")
    workspaces.check_workspace(organization, name, entries)
    return run, f"{organization}/{name}"


def _related(run: object, relationship: str, collection: str) -> object:
    """Return the object run's relationship names, or None when run names none.

    Raises:
        ApiError: the read failed, or the object's ID is unsafe in a path.
    """
    related_id = _member(run, "relationships", relationship, "data", "id")
    if related_id is None:
        return None
    return _member(api.get_json(f"/{collection}/{api.safe_id(related_id)}"), "data")


def _member(value: object, *keys: str) -> object:
    """Return the value at keys in nested JSON objects, or None where one is absent."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _data_items(path: str, answer: Mapping[str, object]) -> list[object]:
    """Return the data list of answer, the answer to GET path.

    Raises:
        ApiError: answer's data is not a list.
    """
    items = answer.get("data")
    if not isinstance(items, list):
        raise ApiError(f"GET {path} answered no data list")
    return items


ACTIONS: Mapping[str, Callable[..., dict[str, object]]] = {
    "workspace_list": workspace_list,
    "run_list": run_list,
    "run_view": run_view,
    "plan_log": plan_log,
    "apply_log": apply_log,
}


def run_script(name: str, stdin: bytes) -> dict[str, object]:
    """Run the script action name on the JSON input stdin holds, and return its result.

    A refusal is exit code 2 and an API failure exit code 1, each with no
    stdout and its reason on stderr. A success is exit code 0 with the
    action's data member.
    """
    arguments = json.loads(stdin)
    try:
        entries = workspaces.allowed_workspaces(os.environ)
        data = ACTIONS[name](arguments, entries)
    except RefusalError as refusal:
        return _result(REFUSAL_EXIT, f"{REFUSAL_PREFIX}{refusal}\n")
    except ApiError as failure:
        return _result(FAILURE_EXIT, f"{failure}\n")
    return {**_result(0, ""), **data}


def _result(exit_code: int, stderr: str) -> dict[str, object]:
    """Return a result with exit_code, no stdout, and stderr."""
    return {"exit_code": exit_code, "stdout": "", "stderr": stderr}
