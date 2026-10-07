import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.message import Message
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
FAILED_LINE = "build\tfail\t1m\thttps://github.com/o/n/actions/runs/101/job/1\t"
JOB_LOGS_PATH = "/repos/o/n/actions/jobs/{job_id}/logs"
LOG_URL = "https://results-receiver.invalid/job/{job_id}?sig=s"
NOT_FOUND = b'{"message":"Not Found"}'
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@dataclass
class FakeApi:
    """A plain-HTTP API: its answers by path, and each request it saw."""

    answers: dict[str, tuple[int, dict[str, str], bytes]] = field(default_factory=dict)
    seen: list[tuple[str, Message]] = field(default_factory=list)

    def redirect(self, job_id: int) -> str:
        """Answer job_id's log request with a 302 to its log URL; return the URL."""
        url = LOG_URL.format(job_id=job_id)
        path = JOB_LOGS_PATH.format(job_id=job_id)
        self.answers[path] = (302, {"Location": url}, b"")
        return url


@pytest.fixture(autouse=True)
def gh_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the stub gh first on PATH, and return the file it logs each argv to."""
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{STUB_DIR}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_STUB_LOG", str(log))
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    return log


@pytest.fixture
def direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the proxy variables, so urllib connects straight to API_URL."""
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, direct: None) -> Iterator[FakeApi]:
    """Serve a FakeApi on 127.0.0.1, and point scripts.API_URL at it."""
    fake = FakeApi()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Record the request, then send the answer for its path, or 404."""
            fake.seen.append((self.path, self.headers))
            status, headers, body = fake.answers.get(self.path, (404, {}, NOT_FOUND))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Log nothing, so the test output stays clean."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(scripts, "API_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _calls(log: Path) -> list[list[str]]:
    """Return each argv the stub gh logged, in the order it ran."""
    return [json.loads(line) for line in log.read_text().splitlines()]


def _echo(*argv: str) -> scripts.ScriptResult:
    """Return the result of a call the stub gh has no canned answer for."""
    echo = json.dumps({"argv": list(argv), "gh_token": TOKEN}, separators=(",", ":"))
    return {"exit_code": 0, "stdout": f"{echo}\n", "stderr": ""}


def test_actions_holds_each_script_action() -> None:
    """The table names every script action this plugin runs."""
    assert set(scripts.ACTIONS) == {
        "pr_create",
        "pr_edit",
        "pr_view",
        "pr_fetch",
        "pr_reply",
        "pr_checks",
        "pr_close",
        "job_log_url",
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


def test_pr_checks_appends_each_failed_jobs_log_url(api: FakeApi, gh_log: Path) -> None:
    """Failed jobs 1 and 3 each get one URL line; passing job 2 gets none."""
    first = api.redirect(1)
    third = api.redirect(3)

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result == {
        "exit_code": 1,
        "stdout": f"{CHECKS_7}Failed job 1 log: {first}\nFailed job 3 log: {third}\n",
        "stderr": "",
    }
    assert _calls(gh_log) == [["pr", "checks", "7", "--repo", "o/n"]]
    assert [path for path, _ in api.seen] == [
        "/repos/o/n/actions/jobs/1/logs",
        "/repos/o/n/actions/jobs/3/logs",
    ]


def test_pr_checks_names_why_a_failed_jobs_log_url_is_unavailable(
    api: FakeApi,
) -> None:
    """A job whose log request fails gets an unavailable line with the reason."""
    first = api.redirect(1)

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result["stdout"] == (
        f"{CHECKS_7}Failed job 1 log: {first}\n"
        "Failed job 3 log unavailable: "
        f"GET /repos/o/n/actions/jobs/3/logs answered 404: {NOT_FOUND.decode()}\n"
    )


def test_pr_checks_ends_checks_without_a_newline_before_the_url_lines(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checks output with no final newline gains one before the first URL line."""
    url = api.redirect(1)
    monkeypatch.setattr(
        scripts,
        "run_gh",
        lambda _args: {"exit_code": 1, "stdout": FAILED_LINE, "stderr": "e\n"},
    )

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result == {
        "exit_code": 1,
        "stdout": f"{FAILED_LINE}\nFailed job 1 log: {url}\n",
        "stderr": "e\n",
    }


def test_pr_checks_names_a_job_failing_on_two_lines_once(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job named by two failing lines gets one URL line and one request."""
    url = api.redirect(1)
    checks = f"{FAILED_LINE}\n{FAILED_LINE}\n"
    monkeypatch.setattr(
        scripts,
        "run_gh",
        lambda _args: {"exit_code": 1, "stdout": checks, "stderr": ""},
    )

    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result["stdout"] == f"{checks}Failed job 1 log: {url}\n"
    assert len(api.seen) == 1


def test_pr_checks_returns_a_failed_call_with_no_stdout(gh_log: Path) -> None:
    """A checks call failing with empty stdout is the result; no log is fetched."""
    result = scripts.pr_checks({"repo": "o/n", "pr_number": 8})

    assert result == {"exit_code": 1, "stdout": "", "stderr": "no checks reported\n"}
    assert _calls(gh_log) == [["pr", "checks", "8", "--repo", "o/n"]]


def test_pr_checks_returns_checks_with_no_failed_run_as_they_are() -> None:
    """Checks that name no failing run come back unchanged."""
    result = scripts.pr_checks({"repo": "o/n", "pr_number": 3})

    assert result == _echo("pr", "checks", "3", "--repo", "o/n")


def test_job_log_location_returns_the_redirects_location(api: FakeApi) -> None:
    """A 302's Location is returned; the request carries the token and API headers."""
    url = api.redirect(5)

    location = scripts.job_log_location("o/n", 5.0)

    assert location == url
    [(path, headers)] = api.seen
    assert path == "/repos/o/n/actions/jobs/5/logs"
    assert (
        headers["Authorization"],
        headers["Accept"],
        headers["X-GitHub-Api-Version"],
        headers["User-Agent"],
    ) == (
        f"Bearer {TOKEN}",
        "application/vnd.github+json",
        "2022-11-28",
        "agent-proxy-github-plugin",
    )


def test_job_log_location_names_the_status_and_body_of_a_404(api: FakeApi) -> None:
    """A job the API does not know fails with the answer's status and body."""
    with pytest.raises(scripts.LogUrlError) as raised:
        scripts.job_log_location("o/n", 2)

    assert str(raised.value) == (
        f"GET /repos/o/n/actions/jobs/2/logs answered 404: {NOT_FOUND.decode()}"
    )


def test_job_log_location_refuses_a_redirect_without_a_location(
    api: FakeApi,
) -> None:
    """A 302 with no Location header fails with its status and body."""
    api.answers[JOB_LOGS_PATH.format(job_id=1)] = (302, {}, b"moved")

    with pytest.raises(scripts.LogUrlError) as raised:
        scripts.job_log_location("o/n", 1)

    assert str(raised.value) == "GET /repos/o/n/actions/jobs/1/logs answered 302: moved"


def test_job_log_location_refuses_a_200_naming_200_characters_of_its_body(
    api: FakeApi,
) -> None:
    """A 200 is not a log URL; the reason holds the first 200 body characters."""
    api.answers[JOB_LOGS_PATH.format(job_id=1)] = (200, {}, b"x" * 300)

    with pytest.raises(scripts.LogUrlError) as raised:
        scripts.job_log_location("o/n", 1)

    assert str(raised.value) == (
        f"GET /repos/o/n/actions/jobs/1/logs answered 200: {'x' * 200}"
    )


@pytest.mark.usefixtures("direct")
def test_job_log_location_names_a_refused_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port nothing listens on fails with the connection error."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(scripts, "API_URL", f"http://127.0.0.1:{port}")

    with pytest.raises(
        scripts.LogUrlError,
        match=r"^GET /repos/o/n/actions/jobs/1/logs failed: .*Connection refused",
    ):
        scripts.job_log_location("o/n", 1)


def test_job_log_location_refuses_without_gh_token(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH_TOKEN unset is refused before any request."""
    monkeypatch.delenv("GH_TOKEN")

    with pytest.raises(scripts.RefusalError, match="^GH_TOKEN is not set$"):
        scripts.job_log_location("o/n", 1)

    assert api.seen == []


def test_job_log_location_refuses_an_empty_gh_token(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GH_TOKEN set to the empty string is refused before any request."""
    monkeypatch.setenv("GH_TOKEN", "")

    with pytest.raises(scripts.RefusalError, match="^GH_TOKEN is not set$"):
        scripts.job_log_location("o/n", 1)

    assert api.seen == []


def test_job_log_url_returns_the_url_as_its_stdout(api: FakeApi) -> None:
    """The job_log_url action prints the URL and a newline, and exits 0."""
    url = api.redirect(1)

    result = scripts.job_log_url({"repo": "o/n", "job_id": 1})

    assert result == {"exit_code": 0, "stdout": f"{url}\n", "stderr": ""}


def test_job_log_url_returns_the_reason_on_stderr(api: FakeApi) -> None:
    """A failed request is exit code 1 with the reason and a newline on stderr."""
    result = scripts.job_log_url({"repo": "o/n", "job_id": 2})

    assert result == {
        "exit_code": 1,
        "stdout": "",
        "stderr": (
            f"GET /repos/o/n/actions/jobs/2/logs answered 404: {NOT_FOUND.decode()}\n"
        ),
    }


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
