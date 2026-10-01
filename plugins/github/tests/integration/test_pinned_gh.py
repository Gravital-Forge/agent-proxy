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
