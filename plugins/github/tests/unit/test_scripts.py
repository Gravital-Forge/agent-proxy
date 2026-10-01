import json
import os
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


def test_pr_checks_appends_each_failed_runs_logs_once(gh_log: Path) -> None:
    """Run 101 fails twice and its logs are fetched once; passing run 102's never."""
    result = scripts.pr_checks({"repo": "o/n", "pr_number": 7})

    assert result == {
        "exit_code": 1,
        "stdout": f"{CHECKS_7}\n--- Failed logs for run 101 ---\nlog of 101\n",
        "stderr": "",
    }
    assert _calls(gh_log) == [
        ["pr", "checks", "7", "--repo", "o/n"],
        ["run", "view", "101", "--log-failed", "--repo", "o/n"],
    ]


def test_pr_checks_returns_a_failed_call_with_no_stdout(gh_log: Path) -> None:
    """A checks call failing with empty stdout is the result; no log is fetched."""
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

    result = scripts.run_script("pr_close", stdin, {})

    assert result == _echo("pr", "close", "5", "--repo", "o/n")


def test_run_script_returns_a_refusal_as_a_result() -> None:
    """A refusal is exit code 2, no stdout, and the prefixed reason on stderr."""
    stdin = json.dumps({"repo": "o/n", "pr_number": 9}).encode()

    result = scripts.run_script("pr_fetch", stdin, {})

    assert result == {
        "exit_code": 2,
        "stdout": "",
        "stderr": "github plugin refused: "
        "gh api repos/o/n/pulls/9/comments did not print JSON\n",
    }
