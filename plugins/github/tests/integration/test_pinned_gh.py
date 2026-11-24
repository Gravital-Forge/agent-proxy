import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from agent_proxy.config import Root
from agent_proxy.egress import CageNetwork
from docker.models.containers import Container
from harness import GITHUB_TOKEN, FakeApiLog, RunAction

FAILED_STEP_LOG = (
    "build\ttest\t2026-01-01T00:00:00.0000000Z FAILED: expected 1, got 2\n"
)
LISTED = {"GITHUB_REPOS": "o/n"}
ASSET_URL = "https://github.com/user-attachments/assets/fake-"
MEDIA_FILES = ("demo.png", "second.png", "demo.mp4", "upload-fails.png")
PROOF_BODY = """## Proof

The page shows the new button. Recorded at `abc1234`, 1280x720 viewport.

![]({media}/demo.mp4)

<details>
<summary>Screenshots (2)</summary>

1. The empty page

   ![The empty page]({media}/demo.png)

2. The page with the button

   ![The page with the button]({media}/second.png)

</details>
"""
PLANTED_TEXT = "32 bytes the agent planted here\n"
RELEASE_LOOKUP_COMMAND = ["touch", "/tmp/release-lookup"]  # noqa: S108 - a path in the fake's container
LOOKUP_HELD_SECONDS = 30
REST_REPOSITORY = re.compile(r"^[A-Z]+ /repos/(?P<owner>[^/]+)/(?P<name>[^/]+)")

type EditWithAttachments = Callable[[str, Sequence[str]], Mapping[str, object]]


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    """A directory of small non-empty files for gh to attach."""
    directory = tmp_path / "media"
    directory.mkdir()
    for name in MEDIA_FILES:
        (directory / name).write_bytes(b"not really " + name.encode())
    return directory


@pytest.fixture
def edit_with_attachments(
    run_action: RunAction,
    plugin_image: str,
    fake_api_network: str,
    fake_api_ca: Path,
    media_dir: Path,
) -> EditWithAttachments:
    """A runner of pr_edit on the fake's pull request 1, through the pinned gh.

    It takes the body and the names of the media_dir files to attach, in
    order, and returns the action's result. The cage's roots are the fake
    CA's directory and media_dir, both read-only.
    """
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    def edit(body: str, names: Sequence[str]) -> Mapping[str, object]:
        arguments = {
            "repo": "o/n",
            "pr_number": 1,
            "body": body,
            "attach": [{"path": str(media_dir / name)} for name in names],
        }
        outcome = run_action(
            "pr_edit",
            arguments,
            image=plugin_image,
            roots=[
                Root(path=str(fake_api_ca.parent), mode="ro"),
                Root(path=str(media_dir), mode="ro"),
            ],
            network=network,
            env=LISTED,
        )
        return outcome.content

    return edit


def logged_update_body(fake_api_log: FakeApiLog) -> str:
    """Return the body of the one update the fake logged."""
    (body,) = [
        entry["update_body"] for entry in fake_api_log() if "update_body" in entry
    ]
    assert isinstance(body, str)
    return body


def requested_repositories(fake_api_log: FakeApiLog) -> set[tuple[str, str]]:
    """Return each owner and name a request the fake logged names.

    A REST request names them in a path under /repos/, and a GraphQL request
    in its owner variable and its repo or name variable.
    """
    named: set[tuple[str, str]] = set()
    for entry in fake_api_log():
        request = entry.get("request")
        variables = entry.get("variables")
        if not isinstance(request, str):
            continue
        rest = REST_REPOSITORY.match(request)
        if rest is not None:
            named.add((rest["owner"], rest["name"]))
        if isinstance(variables, dict) and "owner" in variables:
            name = variables.get("repo", variables.get("name"))
            named.add((str(variables["owner"]), str(name)))
    return named


def wait_for_held_lookup(fake_api_log: FakeApiLog) -> None:
    """Return once the fake's log shows it holds a pull-request lookup.

    Raises:
        TimeoutError: the log shows no hold within LOOKUP_HELD_SECONDS.
    """
    deadline = time.monotonic() + LOOKUP_HELD_SECONDS
    while not any("lookup_held" in entry for entry in fake_api_log()):
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"the fake held no lookup in {LOOKUP_HELD_SECONDS}s; "
                f"its log: {fake_api_log()!r}"
            )
        time.sleep(0.1)


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
        env=LISTED,
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
        env=LISTED,
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
        env=LISTED,
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
        env=LISTED,
    )

    assert outcome.content == {
        "exit_code": 1,
        "stdout": "",
        "stderr": "GET /repos/o/n/actions/jobs/2/logs answered 404: "
        '{"message": "Not Found"}\n',
    }


@pytest.mark.parametrize(
    "pr",
    ["github.com/other/repo/pull/1", " https:x"],
    ids=["url-without-scheme", "leading-space"],
)
def test_pr_view_looks_up_a_pr_the_pattern_accepts_in_the_listed_repository(
    run_action: RunAction,
    plugin_image: str,
    fake_api_network: str,
    fake_api_ca: Path,
    fake_api_log: FakeApiLog,
    pr: str,
) -> None:
    """The pinned gh takes a pr with no http: or https: scheme as a branch of --repo.

    gh takes another repository from pr only when pr parses as an http or
    https URL, which the pattern refuses. A URL without its scheme, and a
    scheme after a leading space, which the pattern accepts, make gh send
    requests that name o/n alone.
    """
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    run_action(
        "pr_view",
        {"repo": "o/n", "pr": pr},
        image=plugin_image,
        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
        network=network,
        env=LISTED,
    )

    assert requested_repositories(fake_api_log) == {("o", "n")}


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
        env=LISTED,
    )

    assert outcome.content == {
        "exit_code": 0,
        "stdout": "https://github.com/o/n/pull/1\n",
        "stderr": "",
        "pr_url": "https://github.com/o/n/pull/1",
        "pr_number": 1,
    }


def test_pr_edit_uploads_each_attachment_and_rewrites_its_reference(
    edit_with_attachments: EditWithAttachments,
    fake_api_log: FakeApiLog,
    media_dir: Path,
) -> None:
    """The pinned gh uploads both files with the token and rewrites both references.

    The image keeps the alt text written in the body. The video, alone in its
    paragraph, becomes its bare asset URL.
    """
    body = f"![shot]({media_dir}/demo.png)\n\n![]({media_dir}/demo.mp4)\n"

    result = edit_with_attachments(body, ["demo.png", "demo.mp4"])

    uploads = [entry for entry in fake_api_log() if "upload" in entry]
    update_body = logged_update_body(fake_api_log)
    assert result["exit_code"] == 0
    assert result["pr_number"] == 1
    assert uploads == [
        {
            "upload": name,
            "host": "uploads.github.com",
            "authorization": f"token {GITHUB_TOKEN}",
        }
        for name in ("demo.png", "demo.mp4")
    ]
    assert f"![shot]({ASSET_URL}demo.png)" in update_body
    assert f"{ASSET_URL}demo.mp4" in update_body.splitlines()
    assert str(media_dir) not in update_body


def test_pr_edit_rewrites_a_proof_section_and_keeps_its_structure(
    edit_with_attachments: EditWithAttachments,
    fake_api_log: FakeApiLog,
    media_dir: Path,
) -> None:
    """The pinned gh rewrites every reference in a Proof section in place.

    The video stays a bare URL on its own line, each screenshot keeps its
    caption as alt text and its three-space list indent, and the details and
    summary lines stay as written.
    """
    body = PROOF_BODY.format(media=media_dir)

    result = edit_with_attachments(body, ["demo.mp4", "demo.png", "second.png"])

    update_body = logged_update_body(fake_api_log)
    lines = update_body.splitlines()
    assert result["exit_code"] == 0
    assert f"{ASSET_URL}demo.mp4" in lines
    assert f"   ![The empty page]({ASSET_URL}demo.png)" in lines
    assert f"   ![The page with the button]({ASSET_URL}second.png)" in lines
    assert "<details>" in lines
    assert "<summary>Screenshots (2)</summary>" in lines
    assert "</details>" in lines
    assert str(media_dir) not in update_body


def test_pr_edit_applies_the_edit_when_an_upload_fails_partway(
    edit_with_attachments: EditWithAttachments,
    fake_api_log: FakeApiLog,
    media_dir: Path,
) -> None:
    """The pinned gh stops at the failed upload and still applies the edit.

    The fake refuses upload-fails.png, so demo.mp4 after it never uploads,
    and both of their references stay local paths. The result still names
    the pull request.
    """
    body = (
        f"![one]({media_dir}/demo.png)\n\n"
        f"![two]({media_dir}/upload-fails.png)\n\n"
        f"![]({media_dir}/demo.mp4)\n"
    )

    result = edit_with_attachments(body, ["demo.png", "upload-fails.png", "demo.mp4"])

    uploaded = [entry["upload"] for entry in fake_api_log() if "upload" in entry]
    update_body = logged_update_body(fake_api_log)
    assert result["exit_code"] == 1
    assert result["pr_url"] == "https://github.com/o/n/pull/1"
    assert result["pr_number"] == 1
    assert f"could not upload {media_dir}/upload-fails.png" in result["stderr"]
    assert uploaded == ["demo.png", "upload-fails.png"]
    assert f"![one]({ASSET_URL}demo.png)" in update_body
    assert f"{media_dir}/upload-fails.png" in update_body
    assert f"{media_dir}/demo.mp4" in update_body


def test_a_file_swapped_during_the_lookup_still_uploads_the_planted_bytes(
    run_action: RunAction,
    plugin_image: str,
    fake_api_container: Container,
    fake_api_network: str,
    fake_api_ca: Path,
    fake_api_log: FakeApiLog,
    media_dir: Path,
) -> None:
    """A symlink to gh's environment, swapped in while gh waits, uploads nothing of it.

    gh stats the file at flag parse and opens it after the lookup, so a swap
    during the lookup lands between the two. The assertion reads upload_bytes
    alone, because the upload event logs the Authorization header by design.
    """
    planted = media_dir / "planted.png"
    planted.write_text(PLANTED_TEXT)
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        call = executor.submit(
            run_action,
            "pr_edit",
            {"repo": "o/n", "pr_number": 2, "attach": [{"path": str(planted)}]},
            image=plugin_image,
            roots=[
                Root(path=str(fake_api_ca.parent), mode="ro"),
                Root(path=str(media_dir), mode="ro"),
            ],
            network=network,
            env=LISTED,
        )
        wait_for_held_lookup(fake_api_log)
        planted.unlink()
        planted.symlink_to("/proc/self/environ")
        fake_api_container.exec_run(RELEASE_LOOKUP_COMMAND)
        outcome = call.result()

    uploaded = [
        entry["upload_bytes"] for entry in fake_api_log() if "upload_bytes" in entry
    ]
    assert uploaded == [PLANTED_TEXT]
    assert outcome.content["exit_code"] == 0
