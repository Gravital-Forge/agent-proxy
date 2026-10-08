import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from github_plugin import scripts

STUB_DIR = Path(__file__).parents[1] / "stub"
TOKEN = "unit-token"
DEFAULT_FIELDS = "number,title,url,headRefName,state,statusCheckRollup"
CHECKS_7 = (
    "build\tfail\t1m\thttps://github.com/o/n/actions/runs/101/job/1\t\n"
    "lint\tpass\t10s\thttps://github.com/o/n/actions/runs/102/job/2\t\n"
    "test\tfail\t2m\thttps://github.com/o/n/actions/runs/101/job/3\t\n"
)
JOB_1_PATH = "/repos/o/n/actions/jobs/1/logs"
JOB_2_PATH = "/repos/o/n/actions/jobs/2/logs"
JOB_3_PATH = "/repos/o/n/actions/jobs/3/logs"
LOG_URL_1 = "https://results-receiver.invalid/job/1?sig=s"
LOG_URL_3 = "https://results-receiver.invalid/job/3?sig=s"
NOT_FOUND_BODY = b'{"message":"Not Found"}'
RECORDED_HEADERS = ("Authorization", "Accept", "X-GitHub-Api-Version", "User-Agent")
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@pytest.fixture(autouse=True)
def gh_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the stub gh first on PATH, and return the file it logs each argv to."""
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{STUB_DIR}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_STUB_LOG", str(log))
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    return log


def _calls(log: Path) -> list[list[str]]:
    """Return each argv the stub gh logged, in the order it ran."""
    return [json.loads(line) for line in log.read_text().splitlines()]


def _echo(*argv: str) -> scripts.ScriptResult:
    """Return the result of a call the stub gh has no canned answer for."""
    echo = json.dumps({"argv": list(argv), "gh_token": TOKEN}, separators=(",", ":"))
    return {"exit_code": 0, "stdout": f"{echo}\n", "stderr": ""}


@dataclass
class FakeApi:
    """What the local API answers for each path, and each request it saw."""

    answers: dict[str, tuple[int, dict[str, str], bytes]] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, str | None]]] = field(default_factory=list)


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    """Serve a plain-HTTP API on 127.0.0.1 and point scripts.API_URL at it.

    A path with no answer gets 404 and NOT_FOUND_BODY. No proxy variable is
    set, so urllib connects directly.
    """
    api = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            headers = {name: self.headers.get(name) for name in RECORDED_HEADERS}
            api.requests.append((self.path, headers))
            status, answer_headers, body = api.answers.get(
                self.path, (404, {}, NOT_FOUND_BODY)
            )
            self.send_response(status)
            for name, value in answer_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Keep the server's request log out of the test output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]
    monkeypatch.setattr(scripts, "API_URL", f"http://{host}:{port}")
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _closed_port_url() -> str:
    """Return an http URL on 127.0.0.1 whose port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_actions_holds_each_script_action() -> None:
    """The table names every script action this plugin runs."""
    assert set(scripts.ACTIONS) == {
        "pr_create",
        "pr_edit",
        "pr_view",
        "pr_fetch",
        "pr_reply",
        "pr_checks",
        "job_log_url",
        "pr_close",
    }


def test_run_gh_returns_ghs_exit_code_and_streams() -> None:
    """run_gh returns gh's exit code, stdout, and stderr."""
    result = scripts.run_gh(["pr", "checks", "8", "--repo", "o/n"])

    assert result == {"exit_code": 1, "stdout": "", "stderr": "no checks reported\n"}


def test_run_gh_runs_gh_in_the_process_environment() -> None:
    """The stub gh sees GH_TOKEN from the process environment."""
    result = scripts.run_gh(["auth", "status"])

    assert result == _echo("auth", "status")


def test_pr_create_passes_each_text_as_one_flag_item() -> None:
    """Each text is one --flag=value item, so a leading '-' stays a value."""
    result = scripts.pr_create(
        {
            "repo": "o/n",
            "base": "main",
            "head": "task/x",
            "title": "-t",
            "body": "- one\n- two",
        }
    )

    assert result == _echo(
        "pr",
        "create",
        "--repo",
        "o/n",
        "--base=main",
        "--head=task/x",
        "--title=-t",
        "--body=- one\n- two",
    )


def test_pr_edit_passes_each_field_given() -> None:
    """Title, body, and base each become a --flag=value item after the number."""
    result = scripts.pr_edit(
        {"repo": "o/n", "pr_number": 5, "title": "t", "body": "b", "base": "main"}
    )

    assert result == _echo(
        "pr", "edit", "5", "--repo", "o/n", "--title=t", "--body=b", "--base=main"
    )


def test_pr_edit_leaves_out_each_field_absent() -> None:
    """A field absent from the input adds nothing to the argv."""
    result = scripts.pr_edit({"repo": "o/n", "pr_number": 5, "title": "t"})

    assert result == _echo("pr", "edit", "5", "--repo", "o/n", "--title=t")


def test_pr_edit_passes_an_empty_field_through() -> None:
    """An empty body is still passed, so gh clears the body."""
    result = scripts.pr_edit({"repo": "o/n", "pr_number": 5, "body": ""})

    assert result == _echo("pr", "edit", "5", "--repo", "o/n", "--body=")


def test_a_number_is_written_as_decimal_digits() -> None:
    """A whole float, which the schema accepts as an integer, becomes digits."""
    result = scripts.pr_edit({"repo": "o/n", "pr_number": 5.0, "title": "t"})

    assert result == _echo("pr", "edit", "5", "--repo", "o/n", "--title=t")


def test_pr_view_asks_for_the_fields_given() -> None:
    """The fields input becomes the --json value."""
    result = scripts.pr_view({"repo": "o/n", "pr": "7", "fields": "number,url"})

    assert result == _echo("pr", "view", "7", "--repo", "o/n", "--json=number,url")


def test_pr_view_asks_for_the_default_fields_without_fields() -> None:
    """With no fields input, gh is asked for the default field list."""
    result = scripts.pr_view({"repo": "o/n", "pr": "7"})

    assert result == _echo(
        "pr", "view", "7", "--repo", "o/n", f"--json={DEFAULT_FIELDS}"
    )


def test_pr_view_asks_for_the_default_fields_on_empty_fields() -> None:
    """An empty fields input asks for the default field list too."""
    result = scripts.pr_view({"repo": "o/n", "pr": "7", "fields": ""})

    assert result == _echo(
        "pr", "view", "7", "--repo", "o/n", f"--json={DEFAULT_FIELDS}"
    )


def test_pr_fetch_returns_the_three_lists_as_one_object(gh_log: Path) -> None:
    """The three GETs run in order, and stdout holds their lists by name."""
    result = scripts.pr_fetch({"repo": "o/n", "pr_number": 5})

    assert result == {
        "exit_code": 0,
        "stdout": '{"inline_comments":[{"id":1}],"reviews":[{"id":2}],'
        '"conversation":[{"id":3}]}',
        "stderr": "",
    }
    assert _calls(gh_log) == [
        ["api", "repos/o/n/pulls/5/comments"],
        ["api", "repos/o/n/pulls/5/reviews"],
        ["api", "repos/o/n/issues/5/comments"],
    ]


def test_pr_fetch_stops_at_the_first_failed_get(gh_log: Path) -> None:
    """A failed second GET is the result, and the third never runs."""
    result = scripts.pr_fetch({"repo": "o/n", "pr_number": 6})

    assert result == {"exit_code": 1, "stdout": "", "stderr": "HTTP 404: Not Found\n"}
    assert _calls(gh_log) == [
        ["api", "repos/o/n/pulls/6/comments"],
        ["api", "repos/o/n/pulls/6/reviews"],
    ]


def test_pr_fetch_refuses_a_get_that_prints_no_json() -> None:
    """A GET that exits 0 with stdout that is not JSON is refused, naming its path."""
    with pytest.raises(scripts.RefusalError, match="repos/o/n/pulls/9/comments"):
        scripts.pr_fetch({"repo": "o/n", "pr_number": 9})


def test_pr_reply_answers_the_comment_given() -> None:
    """With comment_id, the body is posted as a reply to that inline comment."""
    result = scripts.pr_reply(
        {"repo": "o/n", "pr_number": 5, "body": "- yes", "comment_id": 42}
    )

    assert result == _echo(
        "api",
        "-X",
        "POST",
        "repos/o/n/pulls/5/comments/42/replies",
        "-f",
        "body=- yes",
    )


def test_pr_reply_comments_on_the_pull_request_without_comment_id() -> None:
    """Without comment_id, the body is posted to the pull request's conversation."""
    result = scripts.pr_reply({"repo": "o/n", "pr_number": 5, "body": "b"})

    assert result == _echo(
        "api", "-X", "POST", "repos/o/n/issues/5/comments", "-f", "body=b"
    )


def test_job_log_location_returns_the_redirects_location(fake_api: FakeApi) -> None:
    """A 302's Location is returned, unfollowed, and the request carries the token."""
    fake_api.answers[JOB_1_PATH] = (302, {"Location": LOG_URL_1}, b"")

    location = scripts.job_log_location("o/n", 1)

    assert location == LOG_URL_1
    assert fake_api.requests == [
        (
            JOB_1_PATH,
            {
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "agent-proxy-github-plugin",
            },
        )
    ]


def test_job_log_location_writes_a_whole_float_job_id_as_digits(
    fake_api: FakeApi,
) -> None:
    """A job ID of 1.0, which the schema accepts as an integer, is 1 in the path."""
    fake_api.answers[JOB_1_PATH] = (302, {"Location": LOG_URL_1}, b"")

    assert scripts.job_log_location("o/n", 1.0) == LOG_URL_1


def test_job_log_location_fails_on_a_404_naming_the_status_and_body(
    fake_api: FakeApi,
) -> None:
    """A 404 is a LogUrlError holding the path, the status, and the body."""
    with pytest.raises(scripts.LogUrlError) as raised:
        scripts.job_log_location("o/n", 2)

    assert str(raised.value) == (
        f"GET {JOB_2_PATH} answered 404: {NOT_FOUND_BODY.decode()}"
    )


def test_job_log_location_keeps_the_first_200_characters_of_the_body(
    fake_api: FakeApi,
) -> None:
    """A long error body is cut to its first 200 characters."""
    fake_api.answers[JOB_1_PATH] = (500, {}, b"x" * 300)

    with pytest.raises(scripts.LogUrlError) as raised:
        scripts.job_log_location("o/n", 1)

    assert str(raised.value) == f"GET {JOB_1_PATH} answered 500: {'x' * 200}"


def test_job_log_location_fails_on_a_redirect_without_location(
    fake_api: FakeApi,
) -> None:
    """A 302 with no Location header is a LogUrlError naming the status."""
    fake_api.answers[JOB_1_PATH] = (302, {}, b"moved")

    with pytest.raises(scripts.LogUrlError) as raised:
        scripts.job_log_location("o/n", 1)

    assert str(raised.value) == f"GET {JOB_1_PATH} answered 302: moved"


def test_job_log_location_fails_on_a_200(fake_api: FakeApi) -> None:
    """A 200 is no redirect, so it is a LogUrlError naming the status and body."""
    fake_api.answers[JOB_1_PATH] = (200, {}, b"log text")

    with pytest.raises(scripts.LogUrlError) as raised:
        scripts.job_log_location("o/n", 1)

    assert str(raised.value) == f"GET {JOB_1_PATH} answered 200: log text"


def test_job_log_location_fails_on_a_refused_connection(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection nothing accepts is a LogUrlError naming the path."""
    monkeypatch.setattr(scripts, "API_URL", _closed_port_url())

    with pytest.raises(scripts.LogUrlError, match=f"^GET {JOB_1_PATH} failed: "):
        scripts.job_log_location("o/n", 1)


@pytest.mark.parametrize("token", [None, ""])
def test_job_log_location_refuses_without_gh_token(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    """An unset or empty GH_TOKEN is refused before any request."""
    if token is None:
        monkeypatch.delenv("GH_TOKEN")
    else:
        monkeypatch.setenv("GH_TOKEN", token)

    with pytest.raises(scripts.RefusalError, match="^GH_TOKEN is not set$"):
        scripts.job_log_location("o/n", 1)
    assert fake_api.requests == []


def test_job_log_url_returns_the_url_on_one_line(fake_api: FakeApi) -> None:
    """The job_log_url action prints the redirect's Location and a newline."""
    fake_api.answers[JOB_1_PATH] = (302, {"Location": LOG_URL_1}, b"")

    result = scripts.job_log_url({"repo": "o/n", "job_id": 1})

    assert result == {"exit_code": 0, "stdout": f"{LOG_URL_1}\n", "stderr": ""}


def test_job_log_url_returns_a_failure_as_exit_code_1(fake_api: FakeApi) -> None:
    """A LogUrlError is exit code 1, no stdout, and the reason on stderr."""
    result = scripts.job_log_url({"repo": "o/n", "job_id": 2})

    assert result == {
        "exit_code": 1,
        "stdout": "",
        "stderr": f"GET {JOB_2_PATH} answered 404: {NOT_FOUND_BODY.decode()}\n",
    }


def test_pr_checks_appends_each_failed_jobs_log_url_once(
    gh_log: Path, fake_api: FakeApi
) -> None:
    """Failed jobs 1 and 3 each get a URL line; passing job 2 is never fetched."""
    fake_api.answers[JOB_1_PATH] = (302, {"Location": LOG_URL_1}, b"")
    fake_api.answers[JOB_3_PATH] = (302, {"Location": LOG_URL_3}, b"")

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result == {
        "exit_code": 1,
        "stdout": f"{CHECKS_7}Failed job 1 log: {LOG_URL_1}\n"
        f"Failed job 3 log: {LOG_URL_3}\n",
        "stderr": "",
    }
    assert _calls(gh_log) == [["pr", "checks", "7", "--repo", "o/n"]]
    assert [path for path, _ in fake_api.requests] == [JOB_1_PATH, JOB_3_PATH]


def test_pr_checks_names_a_job_whose_log_url_is_unavailable(
    fake_api: FakeApi,
) -> None:
    """A failed fetch for job 3 appends its reason in place of a URL."""
    fake_api.answers[JOB_1_PATH] = (302, {"Location": LOG_URL_1}, b"")

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result["stdout"] == (
        f"{CHECKS_7}Failed job 1 log: {LOG_URL_1}\n"
        f"Failed job 3 log unavailable: GET {JOB_3_PATH} answered 404: "
        f"{NOT_FOUND_BODY.decode()}\n"
    )


def test_pr_checks_ends_checks_without_a_final_newline_before_the_url_lines(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checks lacking a final newline gain one; a job failing twice gets one line."""
    checks = (
        "a\tfail\t1m\thttps://github.com/o/n/actions/runs/9/job/1\t\n"
        "b\tFAIL\t1m\thttps://github.com/o/n/actions/runs/9/job/1"
    )
    monkeypatch.setattr(
        scripts,
        "run_gh",
        lambda args: {"exit_code": 1, "stdout": checks, "stderr": "e"},
    )
    fake_api.answers[JOB_1_PATH] = (302, {"Location": LOG_URL_1}, b"")

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result == {
        "exit_code": 1,
        "stdout": f"{checks}\nFailed job 1 log: {LOG_URL_1}\n",
        "stderr": "e",
    }


def test_pr_checks_leaves_checks_with_no_failed_job_as_they_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checks with no failed job and no final newline come back unchanged."""
    checks = "a\tpass\t1m\thttps://github.com/o/n/actions/runs/9/job/1"
    monkeypatch.setattr(
        scripts,
        "run_gh",
        lambda args: {"exit_code": 0, "stdout": checks, "stderr": ""},
    )

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result == {"exit_code": 0, "stdout": checks, "stderr": ""}


def test_pr_checks_returns_a_failed_call_with_no_stdout(gh_log: Path) -> None:
    """A checks call failing with empty stdout is the result; no log URL is fetched."""
    result = scripts.pr_checks({"repo": "o/n", "pr_number": 8})

    assert result == {"exit_code": 1, "stdout": "", "stderr": "no checks reported\n"}
    assert _calls(gh_log) == [["pr", "checks", "8", "--repo", "o/n"]]


def test_pr_checks_returns_checks_with_no_failed_run_as_they_are() -> None:
    """Checks that name no failing run come back unchanged."""
    result = scripts.pr_checks({"repo": "o/n", "pr_number": 3})

    assert result == _echo("pr", "checks", "3", "--repo", "o/n")


def test_pr_close_passes_comment_and_delete_branch() -> None:
    """A comment becomes --comment=value, and delete_branch adds --delete-branch."""
    result = scripts.pr_close(
        {"repo": "o/n", "pr_number": 5, "comment": "-done", "delete_branch": True}
    )

    assert result == _echo(
        "pr", "close", "5", "--repo", "o/n", "--comment=-done", "--delete-branch"
    )


def test_pr_close_leaves_out_an_empty_comment_and_a_false_delete_branch() -> None:
    """An empty comment and a false delete_branch add nothing."""
    result = scripts.pr_close(
        {"repo": "o/n", "pr_number": 5, "comment": "", "delete_branch": False}
    )

    assert result == _echo("pr", "close", "5", "--repo", "o/n")


def test_pr_close_without_options_closes_only() -> None:
    """With neither option, gh only closes the pull request."""
    result = scripts.pr_close({"repo": "o/n", "pr_number": 5})

    assert result == _echo("pr", "close", "5", "--repo", "o/n")


def test_run_script_runs_the_named_action_on_its_json_input() -> None:
    """The input on stdin is decoded and given to the action of that name."""
    stdin = json.dumps({"repo": "o/n", "pr_number": 5}).encode()

    result = scripts.run_script("pr_close", stdin)

    assert result == _echo("pr", "close", "5", "--repo", "o/n")


def test_run_script_returns_a_refusal_as_a_result() -> None:
    """A refusal is exit code 2, no stdout, and the prefixed reason on stderr."""
    stdin = json.dumps({"repo": "o/n", "pr_number": 9}).encode()

    result = scripts.run_script("pr_fetch", stdin)

    assert result == {
        "exit_code": 2,
        "stdout": "",
        "stderr": "github plugin refused: "
        "gh api repos/o/n/pulls/9/comments did not print JSON\n",
    }


def test_run_script_refuses_job_log_url_without_gh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """job_log_url with GH_TOKEN unset is a refusal naming the variable."""
    monkeypatch.delenv("GH_TOKEN")
    stdin = json.dumps({"repo": "o/n", "job_id": 1}).encode()

    result = scripts.run_script("job_log_url", stdin)

    assert result == {
        "exit_code": 2,
        "stdout": "",
        "stderr": "github plugin refused: GH_TOKEN is not set\n",
    }
