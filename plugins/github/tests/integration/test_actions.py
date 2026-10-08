import json
from pathlib import Path

from agent_proxy.config import Root
from agent_proxy.egress import CageNetwork
from harness import RunAction

STUB_TOKEN = "stub-token"
CHECKS_7 = (
    "build\tfail\t1m\thttps://github.com/o/n/actions/runs/101/job/1\t\n"
    "lint\tpass\t10s\thttps://github.com/o/n/actions/runs/102/job/2\t\n"
    "test\tfail\t2m\thttps://github.com/o/n/actions/runs/101/job/3\t\n"
)
LOG_URL_1 = "https://results-receiver.invalid/job/1?sig=s"
LOG_URL_3 = "https://results-receiver.invalid/job/3?sig=s"


def echoed(*argv: str) -> dict[str, object]:
    """Return the result of a gh call the stub answers by echoing its argv."""
    echo = {"argv": list(argv), "gh_token": STUB_TOKEN}
    return {
        "exit_code": 0,
        "stdout": json.dumps(echo, separators=(",", ":")) + "\n",
        "stderr": "",
    }


def test_pr_create_passes_every_field_to_gh(run_action: RunAction) -> None:
    """The pr_create action calls gh pr create with each field as one flag."""
    arguments = {
        "repo": "o/n",
        "base": "main",
        "head": "task/x",
        "title": "t",
        "body": "b",
    }

    outcome = run_action("pr_create", arguments)

    assert outcome.content == echoed(
        "pr",
        "create",
        "--repo",
        "o/n",
        "--base=main",
        "--head=task/x",
        "--title=t",
        "--body=b",
    )


def test_pr_edit_passes_the_given_field(run_action: RunAction) -> None:
    """The pr_edit action calls gh pr edit with only the title it was given."""
    arguments = {"repo": "o/n", "pr_number": 3, "title": "t"}

    outcome = run_action("pr_edit", arguments)

    assert outcome.content == echoed("pr", "edit", "3", "--repo", "o/n", "--title=t")


def test_pr_view_asks_for_the_default_fields(run_action: RunAction) -> None:
    """The pr_view action with no fields asks gh for the default JSON fields."""
    outcome = run_action("pr_view", {"repo": "o/n", "pr": "3"})

    assert outcome.content == echoed(
        "pr",
        "view",
        "3",
        "--repo",
        "o/n",
        "--json=number,title,url,headRefName,state,statusCheckRollup",
    )


def test_pr_list_passes_repo_and_head(run_action: RunAction) -> None:
    """The pr_list action forms gh pr list with its JSON fields, repo, and head."""
    outcome = run_action("pr_list", {"repo": "o/n", "head": "task/x"})

    assert outcome.content == echoed(
        "pr",
        "list",
        "--json",
        "number,title,url,headRefName,state",
        "--repo",
        "o/n",
        "--head",
        "task/x",
    )


def test_pr_fetch_joins_the_three_comment_lists(run_action: RunAction) -> None:
    """The pr_fetch action on 5 returns the three lists the stub gives."""
    outcome = run_action("pr_fetch", {"repo": "o/n", "pr_number": 5})

    assert outcome.content == {
        "exit_code": 0,
        "stdout": (
            '{"inline_comments":[{"id":1}],"reviews":[{"id":2}],'
            '"conversation":[{"id":3}]}'
        ),
        "stderr": "",
    }


def test_pr_fetch_returns_the_first_failing_call(run_action: RunAction) -> None:
    """The pr_fetch action on 6 returns the failed reviews call's result."""
    outcome = run_action("pr_fetch", {"repo": "o/n", "pr_number": 6})

    assert outcome.content == {
        "exit_code": 1,
        "stdout": "",
        "stderr": "HTTP 404: Not Found\n",
    }


def test_pr_reply_posts_a_reply_to_a_comment(run_action: RunAction) -> None:
    """The pr_reply action with comment_id posts to that comment's replies."""
    arguments = {"repo": "o/n", "pr_number": 3, "body": "hi", "comment_id": 4}

    outcome = run_action("pr_reply", arguments)

    assert outcome.content == echoed(
        "api",
        "-X",
        "POST",
        "repos/o/n/pulls/3/comments/4/replies",
        "-f",
        "body=hi",
    )


def test_pr_checks_appends_each_failed_jobs_log_url(
    run_action: RunAction, fake_api_network: str, fake_api_ca: Path
) -> None:
    """The pr_checks action on 7 appends the fake's unfollowed URLs for jobs 1 and 3."""
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    outcome = run_action(
        "pr_checks",
        {"repo": "o/n", "pr_number": 7},
        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
        network=network,
    )

    assert outcome.content == {
        "exit_code": 1,
        "stdout": f"{CHECKS_7}Failed job 1 log: {LOG_URL_1}\n"
        f"Failed job 3 log: {LOG_URL_3}\n",
        "stderr": "",
    }


def test_pr_checks_returns_a_failed_call_with_no_output(
    run_action: RunAction,
) -> None:
    """The pr_checks action on 8 returns the checks call's result as it is."""
    outcome = run_action("pr_checks", {"repo": "o/n", "pr_number": 8})

    assert outcome.content == {
        "exit_code": 1,
        "stdout": "",
        "stderr": "no checks reported\n",
    }


def test_pr_close_passes_comment_and_delete_branch(run_action: RunAction) -> None:
    """The pr_close action adds the comment and the delete-branch flag."""
    arguments = {
        "repo": "o/n",
        "pr_number": 3,
        "comment": "done",
        "delete_branch": True,
    }

    outcome = run_action("pr_close", arguments)

    assert outcome.content == echoed(
        "pr", "close", "3", "--repo", "o/n", "--comment=done", "--delete-branch"
    )


def test_run_view_passes_the_run_and_log_failed(run_action: RunAction) -> None:
    """The run_view action forms gh run view with the run ID, repo, and flag."""
    arguments = {"repo": "o/n", "run_id": 2, "log_failed": True}

    outcome = run_action("run_view", arguments)

    assert outcome.content == echoed(
        "run", "view", "2", "--repo", "o/n", "--log-failed"
    )


def test_run_list_passes_the_repo(run_action: RunAction) -> None:
    """The run_list action forms gh run list with the repo."""
    outcome = run_action("run_list", {"repo": "o/n"})

    assert outcome.content == echoed("run", "list", "--repo", "o/n")
