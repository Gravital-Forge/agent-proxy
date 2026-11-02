import http.client
import json
from collections.abc import Mapping

import pytest
from hcp_terraform_plugin import actions, api
from hcp_terraform_plugin.errors import ApiError, RefusalError

REFUSED_LIST = (
    "hcp-terraform plugin refused: HCP_TERRAFORM_WORKSPACES is not set or names "
    "no workspace; the grant's env sets it\n"
)
REFUSED_OTHER = (
    "hcp-terraform plugin refused: workspace 'o/other' is not in "
    "HCP_TERRAFORM_WORKSPACES\n"
)
OPERATIONS = "plan_only,plan_and_apply,save_plan,refresh_only,destroy,empty_apply"
FIRST_PAGE = {"page[size]": "100", "page[number]": "1"}
PLAN_LOG_URL = "https://archivist.terraform.io/v1/object/plan-log"
APPLY_LOG_URL = "https://archivist.terraform.io/v1/object/apply-log"
INGRESS_PATH = "/configuration-versions/cv-1/ingress-attributes"
TIMESTAMPS = {"plan-queued-at": "2026-11-02T09:00:00+00:00"}
LOG_TEXT = "Terraform will perform the following actions:\n"
VALID_INPUTS: dict[str, dict[str, object]] = {
    "workspace_list": {},
    "run_list": {"organization": "o", "workspace": "listed"},
    "run_view": {"run_id": "run-1"},
    "plan_log": {"run_id": "run-1"},
    "apply_log": {"run_id": "run-1"},
}
RUN_ACTIONS = ["run_view", "plan_log", "apply_log"]
SECRET = "secret-token"
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

Query = tuple[tuple[str, str], ...]


class FakeApi:
    """Stands in for api.get_json and api.get_log, and records each request."""

    def __init__(self) -> None:
        """Start with no answers, no logs, and no requests."""
        self.answers: dict[tuple[str, Query], dict[str, object] | ApiError] = {}
        self.logs: dict[str, str | RefusalError] = {}
        self.requests: list[str] = []

    def answer(
        self,
        path: str,
        body: dict[str, object] | ApiError,
        query: Mapping[str, str] | None = None,
    ) -> None:
        """Answer GET path with query by body: a JSON object, or an error to raise."""
        self.answers[(path, _key(query))] = body

    def get_json(
        self, path: str, query: Mapping[str, str] | None = None
    ) -> dict[str, object]:
        """Record path, then return or raise its answer, or fail with a 404."""
        self.requests.append(path)
        body = self.answers.get(
            (path, _key(query)), ApiError(f"GET {path} answered 404: no", status=404)
        )
        if isinstance(body, ApiError):
            raise body
        return body

    def get_log(self, url: str) -> str:
        """Record url, then return or raise its log."""
        self.requests.append(url)
        log = self.logs[url]
        if isinstance(log, RefusalError):
            raise log
        return log


def _key(query: Mapping[str, str] | None) -> Query:
    return tuple(sorted((query or {}).items()))


def _ref(kind: str, related_id: str | None) -> dict[str, object]:
    return {"data": None if related_id is None else {"id": related_id, "type": kind}}


def _run(
    run_id: str = "run-1",
    workspace_id: str = "ws-listed",
    *,
    plan_id: str | None = "plan-1",
    apply_id: str | None = "apply-1",
    version_id: str | None = "cv-1",
) -> dict[str, object]:
    relationships: dict[str, object] = {"workspace": _ref("workspaces", workspace_id)}
    if plan_id is not None:
        relationships["plan"] = _ref("plans", plan_id)
    if apply_id is not None:
        relationships["apply"] = _ref("applies", apply_id)
    if version_id is not None:
        relationships["configuration-version"] = _ref(
            "configuration-versions", version_id
        )
    return {
        "data": {
            "id": run_id,
            "type": "runs",
            "attributes": {"status": "applied", "status-timestamps": TIMESTAMPS},
            "relationships": relationships,
        }
    }


def _workspace(
    workspace_id: str = "ws-listed", name: str = "listed"
) -> dict[str, object]:
    return {
        "data": {
            "id": workspace_id,
            "type": "workspaces",
            "attributes": {"name": name},
            "relationships": {"organization": _ref("organizations", "o")},
        }
    }


def _listed_workspace(
    workspace_id: str, name: str, current_run_id: str | None
) -> dict[str, object]:
    return {
        "id": workspace_id,
        "type": "workspaces",
        "attributes": {"name": name},
        "relationships": {"current-run": _ref("runs", current_run_id)},
    }


def _page(items: list[dict[str, object]], next_page: object) -> dict[str, object]:
    return {"data": items, "meta": {"pagination": {"next-page": next_page}}}


def _serve_listed_run(fake: FakeApi) -> None:
    fake.answer("/runs/run-1", _run())
    fake.answer("/workspaces/ws-listed", _workspace())
    fake.answer(
        "/plans/plan-1",
        {
            "data": {
                "id": "plan-1",
                "type": "plans",
                "attributes": {
                    "status": "finished",
                    "resource-additions": 1,
                    "resource-changes": 2,
                    "resource-destructions": 0,
                    "log-read-url": PLAN_LOG_URL,
                },
            }
        },
    )
    fake.answer(
        "/applies/apply-1",
        {
            "data": {
                "id": "apply-1",
                "type": "applies",
                "attributes": {"status": "finished", "log-read-url": APPLY_LOG_URL},
            }
        },
    )
    fake.answer(
        INGRESS_PATH,
        {
            "data": {
                "id": "ia-1",
                "type": "ingress-attributes",
                "attributes": {
                    "commit-sha": "abc123",
                    "branch": "main",
                    "commit-url": "https://github.com/o/r/commit/abc123",
                },
            }
        },
    )


def _call(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return actions.run_script(name, json.dumps(arguments).encode())


def _success(**data: object) -> dict[str, object]:
    return {"exit_code": 0, "stdout": "", "stderr": "", **data}


def _failure(stderr: str) -> dict[str, object]:
    return {"exit_code": 1, "stdout": "", "stderr": stderr}


def _refusal(stderr: str) -> dict[str, object]:
    return {"exit_code": 2, "stdout": "", "stderr": stderr}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    """Put a FakeApi in place of the HTTP layer, with o/listed as the workspace list."""
    fake_api = FakeApi()
    monkeypatch.setattr(api, "get_json", fake_api.get_json)
    monkeypatch.setattr(api, "get_log", fake_api.get_log)
    monkeypatch.setenv("HCP_TERRAFORM_WORKSPACES", "o/listed")
    return fake_api


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail every HTTPS connection, clear the proxies, and list o/listed.

    The real HTTP layer still builds each request, so a header the token
    cannot form fails here as it would on the network.
    """

    def refuse(_connection: http.client.HTTPSConnection) -> None:
        raise OSError("no network")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse)
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HCP_TERRAFORM_WORKSPACES", "o/listed")


def test_the_actions_are_the_five_reads() -> None:
    """ACTIONS names exactly the five read actions."""
    assert set(actions.ACTIONS) == set(VALID_INPUTS)


@pytest.mark.parametrize("name", sorted(VALID_INPUTS))
def test_every_action_refuses_without_a_workspace_list_before_any_request(
    fake: FakeApi, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """An unset HCP_TERRAFORM_WORKSPACES is a refusal, and nothing is read."""
    monkeypatch.delenv("HCP_TERRAFORM_WORKSPACES")

    assert _call(name, VALID_INPUTS[name]) == _refusal(REFUSED_LIST)
    assert fake.requests == []


def test_an_api_error_becomes_exit_code_1(fake: FakeApi) -> None:
    """A failed read is exit code 1 with its reason on stderr."""
    assert _call("run_view", {"run_id": "run-1"}) == _failure(
        "GET /runs/run-1 answered 404: no\n"
    )


@pytest.mark.usefixtures("offline")
def test_a_token_with_a_trailing_newline_reaches_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token stored with a trailing newline is used, and no result quotes it."""
    monkeypatch.setenv("TFE_TOKEN", f"{SECRET}\n")

    result = _call("run_view", {"run_id": "run-abc"})

    assert result == _failure("GET /runs/run-abc failed: <urlopen error no network>\n")
    assert SECRET not in result["stderr"]


@pytest.mark.usefixtures("offline")
def test_a_token_that_could_split_a_header_is_refused_unquoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token holding CR LF is a refusal whose stderr does not quote it."""
    monkeypatch.setenv("TFE_TOKEN", f"{SECRET}\r\nX: y")

    result = _call("run_view", {"run_id": "run-abc"})

    assert result == _refusal(
        "hcp-terraform plugin refused: TFE_TOKEN holds a character that is not "
        "visible ASCII\n"
    )
    assert SECRET not in result["stderr"]


def test_workspace_list_pages_through_each_organization(
    fake: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every page of every named organization is read; only allowed ones return."""
    monkeypatch.setenv("HCP_TERRAFORM_WORKSPACES", "o/listed,o/also,p/*")
    fake.answer(
        "/organizations/o/workspaces",
        _page(
            [
                _listed_workspace("ws-listed", "listed", "run-1"),
                _listed_workspace("ws-other", "other", "run-2"),
            ],
            2,
        ),
        FIRST_PAGE,
    )
    fake.answer(
        "/organizations/o/workspaces",
        _page([_listed_workspace("ws-also", "also", None)], None),
        {"page[size]": "100", "page[number]": "2"},
    )
    fake.answer(
        "/organizations/p/workspaces",
        _page([_listed_workspace("ws-any", "any", "run-9")], None),
        FIRST_PAGE,
    )

    assert _call("workspace_list", {}) == _success(
        workspaces=[
            {
                "organization": "o",
                "name": "listed",
                "id": "ws-listed",
                "current_run_id": "run-1",
            },
            {
                "organization": "o",
                "name": "also",
                "id": "ws-also",
                "current_run_id": None,
            },
            {
                "organization": "p",
                "name": "any",
                "id": "ws-any",
                "current_run_id": "run-9",
            },
        ]
    )
    assert fake.requests == [
        "/organizations/o/workspaces",
        "/organizations/o/workspaces",
        "/organizations/p/workspaces",
    ]


@pytest.mark.parametrize("next_page", [1, 0, "2"])
def test_workspace_list_fails_on_a_next_page_that_does_not_advance(
    fake: FakeApi, next_page: object
) -> None:
    """A next-page that is not a later page number fails instead of looping."""
    fake.answer("/organizations/o/workspaces", _page([], next_page), FIRST_PAGE)

    assert _call("workspace_list", {}) == _failure(
        f"GET /organizations/o/workspaces answered next-page {next_page!r}, "
        "not a page after 1\n"
    )


def test_workspace_list_fails_on_an_answer_with_no_data_list(fake: FakeApi) -> None:
    """A page whose data is not a list fails."""
    fake.answer("/organizations/o/workspaces", {"data": {}}, FIRST_PAGE)

    assert _call("workspace_list", {}) == _failure(
        "GET /organizations/o/workspaces answered no data list\n"
    )


def test_run_list_returns_the_workspaces_runs_in_api_order(fake: FakeApi) -> None:
    """Each run's fields come from its attributes, null when absent."""
    fake.answer("/organizations/o/workspaces/listed", _workspace())
    fake.answer(
        "/workspaces/ws-listed/runs",
        {
            "data": [
                {
                    "id": "run-2",
                    "type": "runs",
                    "attributes": {
                        "status": "planned",
                        "created-at": "2026-11-02T09:00:00.000Z",
                        "source": "tfe-api",
                        "message": "Triggered via API",
                        "plan-only": True,
                        "has-changes": False,
                    },
                },
                {"id": "run-1", "type": "runs", "attributes": {"status": "applied"}},
            ]
        },
        {"page[size]": "20", "filter[operation]": OPERATIONS},
    )

    assert _call("run_list", {"organization": "o", "workspace": "listed"}) == _success(
        runs=[
            {
                "id": "run-2",
                "status": "planned",
                "created_at": "2026-11-02T09:00:00.000Z",
                "source": "tfe-api",
                "message": "Triggered via API",
                "plan_only": True,
                "has_changes": False,
            },
            {
                "id": "run-1",
                "status": "applied",
                "created_at": None,
                "source": None,
                "message": None,
                "plan_only": None,
                "has_changes": None,
            },
        ]
    )
    assert fake.requests == [
        "/organizations/o/workspaces/listed",
        "/workspaces/ws-listed/runs",
    ]


def test_run_list_asks_for_limit_runs(fake: FakeApi) -> None:
    """The limit input is the page size of the run list request."""
    fake.answer("/organizations/o/workspaces/listed", _workspace())
    fake.answer(
        "/workspaces/ws-listed/runs",
        {"data": []},
        {"page[size]": "5", "filter[operation]": OPERATIONS},
    )

    assert _call(
        "run_list", {"organization": "o", "workspace": "listed", "limit": 5}
    ) == _success(runs=[])


def test_run_list_refuses_an_unlisted_workspace_before_any_request(
    fake: FakeApi,
) -> None:
    """A workspace the list does not name is refused, and nothing is read."""
    assert _call("run_list", {"organization": "o", "workspace": "other"}) == (
        _refusal(REFUSED_OTHER)
    )
    assert fake.requests == []


def test_run_list_fails_on_a_workspace_id_unsafe_in_a_path(fake: FakeApi) -> None:
    """A workspace ID that is not an HCP Terraform ID fails before the run read."""
    fake.answer("/organizations/o/workspaces/listed", _workspace("../x"))

    assert _call("run_list", {"organization": "o", "workspace": "listed"}) == (
        _failure("'../x' is not an HCP Terraform ID\n")
    )
    assert fake.requests == ["/organizations/o/workspaces/listed"]


def test_run_view_returns_the_run_with_its_commit_plan_and_apply(
    fake: FakeApi,
) -> None:
    """The run, its plan, its apply, and its commit are read in that order."""
    _serve_listed_run(fake)

    assert _call("run_view", {"run_id": "run-1"}) == _success(
        run={
            "id": "run-1",
            "status": "applied",
            "status_timestamps": TIMESTAMPS,
            "workspace": "o/listed",
            "commit": {
                "sha": "abc123",
                "branch": "main",
                "url": "https://github.com/o/r/commit/abc123",
            },
            "plan": {
                "id": "plan-1",
                "status": "finished",
                "resource_additions": 1,
                "resource_changes": 2,
                "resource_destructions": 0,
            },
            "apply": {"id": "apply-1", "status": "finished"},
        }
    )
    assert fake.requests == [
        "/runs/run-1",
        "/workspaces/ws-listed",
        "/plans/plan-1",
        "/applies/apply-1",
        INGRESS_PATH,
    ]


def test_run_view_returns_null_for_what_a_run_does_not_have(fake: FakeApi) -> None:
    """A run with no plan, apply, or configuration version reads nothing more."""
    fake.answer("/runs/run-1", _run(plan_id=None, apply_id=None, version_id=None))
    fake.answer("/workspaces/ws-listed", _workspace())

    assert _call("run_view", {"run_id": "run-1"}) == _success(
        run={
            "id": "run-1",
            "status": "applied",
            "status_timestamps": TIMESTAMPS,
            "workspace": "o/listed",
            "commit": None,
            "plan": None,
            "apply": None,
        }
    )
    assert fake.requests == ["/runs/run-1", "/workspaces/ws-listed"]


def test_run_view_reads_a_404_for_ingress_attributes_as_no_commit(
    fake: FakeApi,
) -> None:
    """A configuration version with no ingress attributes means no commit."""
    _serve_listed_run(fake)
    fake.answer(
        INGRESS_PATH, ApiError(f"GET {INGRESS_PATH} answered 404: no", status=404)
    )

    result = _call("run_view", {"run_id": "run-1"})

    assert (result["exit_code"], result["run"]["commit"]) == (0, None)


def test_run_view_fails_on_any_other_ingress_attributes_failure(
    fake: FakeApi,
) -> None:
    """An ingress attributes failure other than 404 fails the action."""
    _serve_listed_run(fake)
    fake.answer(
        INGRESS_PATH, ApiError(f"GET {INGRESS_PATH} answered 500: boom", status=500)
    )

    assert _call("run_view", {"run_id": "run-1"}) == _failure(
        f"GET {INGRESS_PATH} answered 500: boom\n"
    )


def test_run_view_fails_on_a_run_id_unsafe_in_a_path(fake: FakeApi) -> None:
    """A run ID that is not an HCP Terraform ID fails before any request."""
    assert _call("run_view", {"run_id": "run-1/../x"}) == _failure(
        "'run-1/../x' is not an HCP Terraform ID\n"
    )
    assert fake.requests == []


@pytest.mark.parametrize("name", RUN_ACTIONS)
def test_a_run_in_an_unlisted_workspace_is_refused_after_two_reads(
    fake: FakeApi, name: str
) -> None:
    """Only the run and its workspace are read before the workspace is refused."""
    fake.answer("/runs/run-2", _run("run-2", "ws-other"))
    fake.answer("/workspaces/ws-other", _workspace("ws-other", "other"))

    assert _call(name, {"run_id": "run-2"}) == _refusal(REFUSED_OTHER)
    assert fake.requests == ["/runs/run-2", "/workspaces/ws-other"]


@pytest.mark.parametrize("name", RUN_ACTIONS)
def test_a_run_whose_workspace_names_no_organization_fails(
    fake: FakeApi, name: str
) -> None:
    """A workspace answer without an organization and a name fails."""
    fake.answer("/runs/run-1", _run())
    fake.answer(
        "/workspaces/ws-listed",
        {"data": {"id": "ws-listed", "type": "workspaces", "attributes": {}}},
    )

    assert _call(name, {"run_id": "run-1"}) == _failure(
        "workspace 'ws-listed' answered no organization and name\n"
    )


@pytest.mark.parametrize("name", RUN_ACTIONS)
def test_a_run_whose_workspace_id_is_unsafe_fails_before_reading_it(
    fake: FakeApi, name: str
) -> None:
    """A run's workspace ID that is not an HCP Terraform ID is never requested."""
    fake.answer("/runs/run-1", _run(workspace_id="ws-1/../x"))

    assert _call(name, {"run_id": "run-1"}) == _failure(
        "'ws-1/../x' is not an HCP Terraform ID\n"
    )
    assert fake.requests == ["/runs/run-1"]


@pytest.mark.parametrize(
    ("name", "path", "url"),
    [
        ("plan_log", "/plans/plan-1", PLAN_LOG_URL),
        ("apply_log", "/applies/apply-1", APPLY_LOG_URL),
    ],
)
def test_a_log_action_returns_the_log_text(
    fake: FakeApi, name: str, path: str, url: str
) -> None:
    """The log comes from the plan's or apply's log-read-url."""
    _serve_listed_run(fake)
    fake.logs[url] = LOG_TEXT

    assert _call(name, {"run_id": "run-1"}) == _success(log=LOG_TEXT)
    assert fake.requests == ["/runs/run-1", "/workspaces/ws-listed", path, url]


@pytest.mark.parametrize(
    ("name", "noun"), [("plan_log", "plan"), ("apply_log", "apply")]
)
def test_a_log_action_fails_on_a_run_with_no_such_relationship(
    fake: FakeApi, name: str, noun: str
) -> None:
    """A run with no plan or apply has no log yet."""
    fake.answer("/runs/run-1", _run(plan_id=None, apply_id=None))
    fake.answer("/workspaces/ws-listed", _workspace())

    assert _call(name, {"run_id": "run-1"}) == _failure(
        f"run 'run-1' has no {noun} log yet\n"
    )
    assert fake.requests == ["/runs/run-1", "/workspaces/ws-listed"]


@pytest.mark.parametrize(
    ("name", "noun", "path"),
    [("plan_log", "plan", "/plans/plan-1"), ("apply_log", "apply", "/applies/apply-1")],
)
def test_a_log_action_fails_on_a_plan_or_apply_with_no_log_url(
    fake: FakeApi, name: str, noun: str, path: str
) -> None:
    """A plan or apply without a log-read-url has no log yet."""
    fake.answer("/runs/run-1", _run())
    fake.answer("/workspaces/ws-listed", _workspace())
    fake.answer(path, {"data": {"id": "x-1", "attributes": {"status": "pending"}}})

    assert _call(name, {"run_id": "run-1"}) == _failure(
        f"run 'run-1' has no {noun} log yet\n"
    )


def test_a_log_url_refusal_becomes_a_refusal_result(fake: FakeApi) -> None:
    """get_log's refusal of a log-read-url is exit code 2 with the prefix."""
    _serve_listed_run(fake)
    fake.logs[PLAN_LOG_URL] = RefusalError("log-read-url is off the log host")

    assert _call("plan_log", {"run_id": "run-1"}) == _refusal(
        "hcp-terraform plugin refused: log-read-url is off the log host\n"
    )
