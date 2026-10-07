from pathlib import Path

from agent_proxy.config import Root
from agent_proxy.egress import CageNetwork
from harness import RunAction

FAILED_STEP_LOG = (
    "build\ttest\t2026-01-01T00:00:00.0000000Z FAILED: expected 1, got 2\n"
)


def test_pr_create_with_a_head_and_no_checkout_gets_past_the_branch_and_git_steps(
    run_action: RunAction,
    plugin_image: str,
    fake_api_network: str,
    fake_api_ca: Path,
) -> None:
    """The pinned gh's pr create with --head and no checkout needs no branch or git.

    The fake answers gh's first request, the repository query, so gh passes
    the steps that need the current branch and git, then fails at its next
    request, the check for an existing pull request, which the fake answers
    with 404.
    """
    arguments = {
        "repo": "o/n",
        "base": "main",
        "head": "task/x",
        "title": "t",
        "body": "b",
    }
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    outcome = run_action(
        "pr_create",
        arguments,
        image=plugin_image,
        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
        network=network,
    )

    stderr = outcome.content["stderr"]
    assert outcome.content["exit_code"] != 0
    assert "api.github.com" in stderr
    assert "error checking for existing pull request" in stderr
    assert "could not determine the current branch" not in stderr
    assert "unable to find git executable" not in stderr


def test_run_view_log_failed_caches_its_log_on_a_read_only_root(
    run_action: RunAction,
    plugin_image: str,
    fake_api_network: str,
    fake_api_ca: Path,
) -> None:
    """The pinned gh's run view --log-failed prints the failed step's log.

    gh writes the log to its cache before it prints it, so the run succeeds
    only where the image's XDG_CACHE_HOME is writable under the read-only root.
    """
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    outcome = run_action(
        "run_view",
        {"repo": "o/n", "run_id": 1, "log_failed": True},
        image=plugin_image,
        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
        network=network,
    )

    assert outcome.content == {
        "exit_code": 0,
        "stdout": FAILED_STEP_LOG,
        "stderr": "",
    }


def test_job_log_url_returns_the_fakes_location_without_following_it(
    run_action: RunAction,
    plugin_image: str,
    fake_api_network: str,
    fake_api_ca: Path,
) -> None:
    """The job_log_url action for job 1 returns the fake's 302 Location.

    The Location's host does not resolve on the test network, so a followed
    redirect would fail the call.
    """
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    outcome = run_action(
        "job_log_url",
        {"repo": "o/n", "job_id": 1},
        image=plugin_image,
        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
        network=network,
    )

    assert outcome.content == {
        "exit_code": 0,
        "stdout": "https://results-receiver.invalid/job/1?sig=s\n",
        "stderr": "",
    }


def test_job_log_url_names_the_404_for_a_job_the_api_does_not_know(
    run_action: RunAction,
    plugin_image: str,
    fake_api_network: str,
    fake_api_ca: Path,
) -> None:
    """The job_log_url action for job 2 returns exit code 1 and the fake's 404."""
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    outcome = run_action(
        "job_log_url",
        {"repo": "o/n", "job_id": 2},
        image=plugin_image,
        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
        network=network,
    )

    assert outcome.content == {
        "exit_code": 1,
        "stdout": "",
        "stderr": "GET /repos/o/n/actions/jobs/2/logs answered 404: "
        '{"message": "Not Found"}\n',
    }


def test_pr_edit_gets_past_the_lookup_and_applies_the_edit(
    run_action: RunAction,
    plugin_image: str,
    fake_api_network: str,
    fake_api_ca: Path,
) -> None:
    """The pinned gh's pr edit applies the edit and prints the pull request's URL.

    The fake refuses a pull-request lookup that asks for the classic-Projects
    field projectCards, as GitHub does, so the edit succeeds only on a gh whose
    lookup no longer asks for that field.
    """
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    outcome = run_action(
        "pr_edit",
        {"repo": "o/n", "pr_number": 1, "body": "b"},
        image=plugin_image,
        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
        network=network,
    )

    assert outcome.content == {
        "exit_code": 0,
        "stdout": "https://github.com/o/n/pull/1\n",
        "stderr": "",
    }
