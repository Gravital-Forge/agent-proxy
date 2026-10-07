"""The script actions: each reads its input as JSON and returns gh's result."""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from typing import IO, NotRequired, TypedDict

REFUSAL_EXIT = 2
REFUSAL_PREFIX = "github plugin refused: "
DEFAULT_VIEW_FIELDS = "number,title,url,headRefName,state,statusCheckRollup"
API_URL = "https://api.github.com"
USER_AGENT = "agent-proxy-github-plugin"
LOG_URL_TIMEOUT_SECONDS = 30
REASON_BODY_CHARACTERS = 200
FAILED_JOB_ID = re.compile(r"/actions/runs/\d+/job/(\d+)")


class RefusalError(Exception):
    """A call the plugin refuses; the message is the reason."""


class LogUrlError(Exception):
    """A job's log URL could not be read; the message is the reason."""


class ScriptResult(TypedDict):
    exit_code: int
    stdout: str
    stderr: str


class CreateInput(TypedDict):
    repo: str
    base: str
    head: str
    title: str
    body: str


class EditInput(TypedDict):
    repo: str
    pr_number: float
    title: NotRequired[str]
    body: NotRequired[str]
    base: NotRequired[str]


class ViewInput(TypedDict):
    repo: str
    pr: str
    fields: NotRequired[str]


class NumberInput(TypedDict):
    repo: str
    pr_number: float


class ReplyInput(TypedDict):
    repo: str
    pr_number: float
    body: str
    comment_id: NotRequired[float]


class CloseInput(TypedDict):
    repo: str
    pr_number: float
    comment: NotRequired[str]
    delete_branch: NotRequired[bool]


class JobLogInput(TypedDict):
    repo: str
    job_id: float


def run_gh(args: Sequence[str]) -> ScriptResult:
    """Run gh with args, and return its exit code and streams.

    gh inherits the process environment. Each stream is decoded as UTF-8, a
    byte that is not UTF-8 becoming U+FFFD.
    """
    completed = subprocess.run(  # noqa: S603 - each input is one argv item after gh's subcommand
        ["gh", *args],  # noqa: S607 - the first gh on PATH, so a test's stub can stand in
        capture_output=True,
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
    }


def _decimal(number: float) -> str:
    """Return number as decimal digits, since JSON Schema counts 5.0 an integer."""
    return str(int(number))


def pr_create(arguments: CreateInput) -> ScriptResult:
    """Open a pull request from head into base."""
    return run_gh(
        [
            "pr",
            "create",
            "--repo",
            arguments["repo"],
            f"--base={arguments['base']}",
            f"--head={arguments['head']}",
            f"--title={arguments['title']}",
            f"--body={arguments['body']}",
        ]
    )


def pr_edit(arguments: EditInput) -> ScriptResult:
    """Change a pull request's title, body, or base.

    Each of the three in the input is passed, an empty string included.
    """
    number = _decimal(arguments["pr_number"])
    args = ["pr", "edit", number, "--repo", arguments["repo"]]
    if "title" in arguments:
        args.append(f"--title={arguments['title']}")
    if "body" in arguments:
        args.append(f"--body={arguments['body']}")
    if "base" in arguments:
        args.append(f"--base={arguments['base']}")
    return run_gh(args)


def pr_view(arguments: ViewInput) -> ScriptResult:
    """Show a pull request's fields as JSON, a default set when fields is empty."""
    fields = arguments.get("fields") or DEFAULT_VIEW_FIELDS
    repo = arguments["repo"]
    return run_gh(["pr", "view", arguments["pr"], "--repo", repo, f"--json={fields}"])


def pr_fetch(arguments: NumberInput) -> ScriptResult:
    """Return a pull request's inline comments, reviews, and conversation as JSON.

    The three gh api calls run in order, and the first that fails is the
    result.

    Raises:
        RefusalError: a call exits 0 but prints what is not JSON.
    """
    repo = arguments["repo"]
    number = _decimal(arguments["pr_number"])
    paths = {
        "inline_comments": f"repos/{repo}/pulls/{number}/comments",
        "reviews": f"repos/{repo}/pulls/{number}/reviews",
        "conversation": f"repos/{repo}/issues/{number}/comments",
    }
    fetched: dict[str, object] = {}
    for key, path in paths.items():
        result = run_gh(["api", path])
        if result["exit_code"] != 0:
            return result
        fetched[key] = _decode_json(path, result["stdout"])
    stdout = json.dumps(fetched, separators=(",", ":"))
    return {"exit_code": 0, "stdout": stdout, "stderr": ""}


def _decode_json(path: str, stdout: str) -> object:
    """Return what gh api printed for path, decoded as JSON.

    Raises:
        RefusalError: stdout is not JSON.
    """
    try:
        return json.loads(stdout)
    except (ValueError, RecursionError) as error:
        raise RefusalError(f"gh api {path} did not print JSON") from error


def pr_reply(arguments: ReplyInput) -> ScriptResult:
    """Reply to the inline comment comment_id, or else comment on the pull request."""
    repo = arguments["repo"]
    number = _decimal(arguments["pr_number"])
    if "comment_id" in arguments:
        comment_id = _decimal(arguments["comment_id"])
        path = f"repos/{repo}/pulls/{number}/comments/{comment_id}/replies"
    else:
        path = f"repos/{repo}/issues/{number}/comments"
    return run_gh(["api", "-X", "POST", path, "-f", f"body={arguments['body']}"])


def pr_checks(arguments: NumberInput) -> ScriptResult:
    """Show a pull request's checks, then a signed log URL for each failed job.

    A failed checks call with nothing on stdout is the result. Otherwise each
    failed job adds one line, its log URL or the reason it has none, and the
    result keeps the checks call's exit code and stderr.

    Raises:
        RefusalError: a job failed and GH_TOKEN is unset or empty.
    """
    repo = arguments["repo"]
    number = _decimal(arguments["pr_number"])
    checks = run_gh(["pr", "checks", number, "--repo", repo])
    if checks["exit_code"] != 0 and not checks["stdout"]:
        return checks
    log_lines = [
        _job_log_line(repo, job_id) for job_id in _failed_job_ids(checks["stdout"])
    ]
    stdout = checks["stdout"]
    if log_lines and not stdout.endswith("\n"):
        stdout += "\n"
    return {
        "exit_code": checks["exit_code"],
        "stdout": stdout + "".join(log_lines),
        "stderr": checks["stderr"],
    }


def _failed_job_ids(checks_stdout: str) -> list[int]:
    """Return each failing check's job ID, once, in first-seen order."""
    job_ids: dict[int, None] = {}
    for line in checks_stdout.splitlines():
        found = FAILED_JOB_ID.search(line)
        if found and "fail" in line.lower():
            job_ids[int(found.group(1))] = None
    return list(job_ids)


def _job_log_line(repo: str, job_id: int) -> str:
    """Return the line naming a failed job's log URL, or why it has none."""
    try:
        url = job_log_location(repo, job_id)
    except LogUrlError as error:
        return f"Failed job {job_id} log unavailable: {error}\n"
    return f"Failed job {job_id} log: {url}\n"


def job_log_url(arguments: JobLogInput) -> ScriptResult:
    """Return a fresh signed URL to a job's log, or the reason there is none."""
    try:
        url = job_log_location(arguments["repo"], arguments["job_id"])
    except LogUrlError as error:
        return {"exit_code": 1, "stdout": "", "stderr": f"{error}\n"}
    return {"exit_code": 0, "stdout": f"{url}\n", "stderr": ""}


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that follows nothing, so a 3xx raises HTTPError."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        """Decline every redirect."""


def job_log_location(repo: str, job_id: float) -> str:
    """Return the signed URL GitHub redirects a job's log request to.

    The redirect is not followed: its host lies outside a grant that names
    only GitHub's API, and the agent fetches the URL itself. The opener keeps
    urllib's proxy handler, which reads HTTPS_PROXY, and its default TLS
    context, which reads SSL_CERT_FILE.

    Raises:
        RefusalError: GH_TOKEN is unset or empty.
        LogUrlError: the answer is not a redirect with a Location, or the
            request failed.
    """
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RefusalError("GH_TOKEN is not set")
    path = f"/repos/{repo}/actions/jobs/{_decimal(job_id)}/logs"
    request = urllib.request.Request(  # noqa: S310 - the URL is built from the API_URL constant
        f"{API_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(_KeepRedirect())
    try:
        response = opener.open(request, timeout=LOG_URL_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as answer:
        with answer:
            return _redirect_location(path, answer)
    except OSError as error:
        raise LogUrlError(f"GET {path} failed: {error}") from error
    with response:
        raise LogUrlError(_answer_reason(path, response.status, response.read()))


def _redirect_location(path: str, answer: urllib.error.HTTPError) -> str:
    """Return the Location of the redirect GET path answered with.

    Raises:
        LogUrlError: the answer is not a 3xx with a Location.
    """
    location = answer.headers.get("Location")
    if 300 <= answer.code < 400 and location:
        return location
    raise LogUrlError(_answer_reason(path, answer.code, answer.read()))


def _answer_reason(path: str, status: int, body: bytes) -> str:
    """Return why an answer to GET path is not a log URL."""
    text = body.decode("utf-8", errors="replace")[:REASON_BODY_CHARACTERS]
    return f"GET {path} answered {status}: {text}"


def pr_close(arguments: CloseInput) -> ScriptResult:
    """Close a pull request, with comment when it is not empty.

    With delete_branch, gh also deletes the pull request's branch.
    """
    number = _decimal(arguments["pr_number"])
    args = ["pr", "close", number, "--repo", arguments["repo"]]
    if arguments.get("comment"):
        args.append(f"--comment={arguments['comment']}")
    if arguments.get("delete_branch"):
        args.append("--delete-branch")
    return run_gh(args)


# Each takes its own input type, which the core validated against the action's
# input_schema before the cage started.
ACTIONS: Mapping[str, Callable[..., ScriptResult]] = {
    "pr_create": pr_create,
    "pr_edit": pr_edit,
    "pr_view": pr_view,
    "pr_fetch": pr_fetch,
    "pr_reply": pr_reply,
    "pr_checks": pr_checks,
    "pr_close": pr_close,
    "job_log_url": job_log_url,
}


def run_script(name: str, stdin: bytes) -> ScriptResult:
    """Run the script action name on the JSON input stdin holds.

    A refusal is returned as the result: exit code 2, no stdout, and the
    refusal's reason on stderr.
    """
    try:
        return ACTIONS[name](json.loads(stdin))
    except RefusalError as refusal:
        return {
            "exit_code": REFUSAL_EXIT,
            "stdout": "",
            "stderr": f"{REFUSAL_PREFIX}{refusal}\n",
        }
