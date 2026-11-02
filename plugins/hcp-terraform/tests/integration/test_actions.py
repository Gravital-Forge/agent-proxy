import pytest
from harness import TFE_TOKEN, FakeApiLog, RunAction

API_HOST = "app.terraform.io"
LOG_HOST = "archivist.terraform.io"
LISTED = {"HCP_TERRAFORM_WORKSPACES": "o/listed"}
LISTED_RUN = {"run_id": "run-listed"}
OTHER_RUN = {"run_id": "run-other"}
OTHER_RUN_READS = ["/api/v2/runs/run-other", "/api/v2/workspaces/ws-other"]
UNLISTED_STDERR = (
    "hcp-terraform plugin refused: "
    "workspace 'o/other' is not in HCP_TERRAFORM_WORKSPACES\n"
)
SUCCESSFUL_CALLS = [
    ("workspace_list", {}),
    ("run_list", {"organization": "o", "workspace": "listed"}),
    ("run_view", LISTED_RUN),
    ("plan_log", LISTED_RUN),
    ("apply_log", LISTED_RUN),
]
REFUSED_CALLS = [
    ("run_list", {"organization": "o", "workspace": "other"}),
    ("run_view", OTHER_RUN),
    ("plan_log", OTHER_RUN),
]


def succeeded(**data: object) -> dict[str, object]:
    """Return a successful result holding data."""
    return {"exit_code": 0, "stdout": "", "stderr": "", **data}


@pytest.fixture
def every_call_logged(
    run_action: RunAction, fake_api_log: FakeApiLog
) -> list[dict[str, object]]:
    """The fake's log after each successful call and each refused call ran once."""
    exit_codes = [
        run_action(name, arguments, env=LISTED).content["exit_code"]
        for name, arguments in SUCCESSFUL_CALLS + REFUSED_CALLS
    ]
    assert exit_codes == [0] * len(SUCCESSFUL_CALLS) + [2] * len(REFUSED_CALLS)
    return fake_api_log()


def test_workspace_list_pages_to_the_listed_workspace(run_action: RunAction) -> None:
    """The workspace_list action follows the fake's two pages and keeps only listed."""
    outcome = run_action("workspace_list", {}, env=LISTED)

    assert outcome.content == succeeded(
        workspaces=[
            {
                "organization": "o",
                "name": "listed",
                "id": "ws-listed",
                "current_run_id": "run-listed",
            }
        ]
    )


def test_run_list_returns_the_listed_workspaces_run(run_action: RunAction) -> None:
    """The run_list action on o/listed returns its one run's fields."""
    outcome = run_action(
        "run_list", {"organization": "o", "workspace": "listed"}, env=LISTED
    )

    assert outcome.content == succeeded(
        runs=[
            {
                "id": "run-listed",
                "status": "applied",
                "created_at": "2026-11-01T10:00:00.000Z",
                "source": "tfe-configuration-version",
                "message": "Merge pull request #1",
                "plan_only": False,
                "has_changes": True,
            }
        ]
    )


def test_run_view_returns_the_run_its_commit_plan_and_apply(
    run_action: RunAction,
) -> None:
    """The run_view action on the listed run joins the run, commit, plan, and apply."""
    outcome = run_action("run_view", LISTED_RUN, env=LISTED)

    assert outcome.content == succeeded(
        run={
            "id": "run-listed",
            "status": "applied",
            "status_timestamps": {
                "planned-at": "2026-11-01T10:01:00+00:00",
                "applied-at": "2026-11-01T10:02:00+00:00",
            },
            "workspace": "o/listed",
            "commit": {
                "sha": "abc1234",
                "branch": "main",
                "url": "https://github.com/o/infra/commit/abc1234",
            },
            "plan": {
                "id": "plan-listed",
                "status": "finished",
                "resource_additions": 1,
                "resource_changes": 2,
                "resource_destructions": 0,
            },
            "apply": {"id": "apply-listed", "status": "finished"},
        }
    )


def test_plan_log_returns_the_log_without_its_framing(run_action: RunAction) -> None:
    """The plan_log action returns the archivist's plan log less its framing bytes."""
    outcome = run_action("plan_log", LISTED_RUN, env=LISTED)

    assert outcome.content == succeeded(
        log=(
            "Terraform will perform the following actions:\n"
            "Plan: 1 to add, 2 to change, 0 to destroy.\n"
        )
    )


def test_apply_log_returns_the_log_without_its_framing(run_action: RunAction) -> None:
    """The apply_log action returns the archivist's apply log less its framing bytes."""
    outcome = run_action("apply_log", LISTED_RUN, env=LISTED)

    assert outcome.content == succeeded(
        log="Apply complete! Resources: 1 added, 2 changed, 0 destroyed.\n"
    )


@pytest.mark.parametrize(
    ("name", "arguments", "paths_read"),
    [
        ("run_list", {"organization": "o", "workspace": "other"}, []),
        ("run_view", OTHER_RUN, OTHER_RUN_READS),
        ("plan_log", OTHER_RUN, OTHER_RUN_READS),
    ],
)
def test_the_other_workspace_is_refused_before_any_further_read(
    run_action: RunAction,
    fake_api_log: FakeApiLog,
    name: str,
    arguments: dict[str, object],
    paths_read: list[str],
) -> None:
    """A run or name in o/other is refused after at most its run and workspace reads."""
    outcome = run_action(name, arguments, env=LISTED)

    assert outcome.content == {"exit_code": 2, "stdout": "", "stderr": UNLISTED_STDERR}
    assert [request["path"] for request in fake_api_log()] == paths_read


def test_every_request_is_a_get(every_call_logged: list[dict[str, object]]) -> None:
    """Every request the five actions send, to either host, is a GET."""
    assert {request["method"] for request in every_call_logged} == {"GET"}


def test_the_token_goes_only_to_the_api_host(
    every_call_logged: list[dict[str, object]],
) -> None:
    """Every API request carries the token, and no log request carries any."""
    authorization_by_host = {
        host: {
            request["authorization"]
            for request in every_call_logged
            if request["host"] == host
        }
        for host in {request["host"] for request in every_call_logged}
    }

    assert authorization_by_host == {
        API_HOST: {f"Bearer {TFE_TOKEN}"},
        LOG_HOST: {None},
    }
