"""The script actions: each reads its input as JSON and returns gh's result."""

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import NotRequired, TypedDict

REFUSAL_EXIT = 2
REFUSAL_PREFIX = "github plugin refused: "
DEFAULT_VIEW_FIELDS = "number,title,url,headRefName,state,statusCheckRollup"
RUN_ID = re.compile(r"/runs/(\d+)")


class RefusalError(Exception):
    """A call the plugin refuses; the message is the reason."""


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
    """Show a pull request's checks, then the failed logs of each run that failed.

    A failed checks call with nothing on stdout is the result. Otherwise each
    log fetch adds its stdout whatever its exit code, and the result keeps
    the checks call's exit code and stderr.
    """
    repo = arguments["repo"]
    number = _decimal(arguments["pr_number"])
    checks = run_gh(["pr", "checks", number, "--repo", repo])
    if checks["exit_code"] != 0 and not checks["stdout"]:
        return checks
    stdout = checks["stdout"]
    for run_id in _failed_run_ids(checks["stdout"]):
        log = run_gh(["run", "view", run_id, "--log-failed", "--repo", repo])
        stdout += f"\n--- Failed logs for run {run_id} ---\n{log['stdout']}"
    return {
        "exit_code": checks["exit_code"],
        "stdout": stdout,
        "stderr": checks["stderr"],
    }


def _failed_run_ids(checks_stdout: str) -> list[str]:
    """Return each failing check's run ID, once, in first-seen order."""
    run_ids: dict[str, None] = {}
    for line in checks_stdout.splitlines():
        found = RUN_ID.search(line)
        if found and "fail" in line.lower():
            run_ids[found.group(1)] = None
    return list(run_ids)


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
