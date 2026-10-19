"""A fake api.github.com and uploads.github.com for the tests that run the pinned gh.

It runs in a container on the test's internal network, under the network
aliases api.github.com and uploads.github.com, and serves TLS with a
certificate the image's test CA signs for both names. It prints serving once
its socket is bound.

Its GET answers serve run 1 of repository o/n: the run, its workflow, its jobs,
of which one failed, and the run's log archive. It answers two jobs' log
requests with a redirect to a host the test network cannot reach, so a client
that follows it fails.

On /graphql it answers gh's repository query, and the three requests gh's pr
edit sends for pull request 1: the pull-request lookup, the project-items
query, and the update. The lookup's answer names the pull request's
repository, which gh asks for when it attaches files. It refuses a lookup
that asks for the classic-Projects field projectCards with the error GitHub
gives for that field.

A POST to /user-attachments/assets uploads the file its name query parameter
names: upload-fails.png answers 422, and any other name answers 201 with the
asset URL https://github.com/user-attachments/assets/fake-<name>.

It prints one JSON line for each upload, with the file's name and the
request's Host and Authorization headers, and one for each update, with the
body it sets. It answers any other request with 404.
"""

import io
import json
import ssl
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ADDRESS = ("", 443)
CERTIFICATE_PATH = "/pki/server.crt"
KEY_PATH = "/pki/server.key"
LOGS_PATH = "/repos/o/n/actions/runs/1/logs"
STEP_LOG_NAME = "build/1_test.txt"
STEP_LOG = "2026-01-01T00:00:00.0000000Z FAILED: expected 1, got 2\n"
JOB_LOG_REDIRECTS = {
    f"/repos/o/n/actions/jobs/{job_id}/logs": (
        f"https://results-receiver.invalid/job/{job_id}?sig=s"
    )
    for job_id in (1, 3)
}
JSON_ANSWERS: dict[str, object] = {
    "/repos/o/n/actions/runs/1": {
        "id": 1,
        "status": "completed",
        "jobs_url": "https://api.github.com/repos/o/n/actions/runs/1/jobs",
    },
    "/repos/o/n/actions/workflows/0": {},
    "/repos/o/n/actions/runs/1/jobs": {
        "jobs": [
            {
                "name": "build",
                "conclusion": "failure",
                "steps": [{"name": "test", "number": 1, "conclusion": "failure"}],
            }
        ]
    },
}
NOT_FOUND = {"message": "Not Found"}
GRAPHQL_PATH = "/graphql"
REPOSITORY_QUERY = "query RepositoryInfo("
REPOSITORY = {
    "id": "R_1",
    "name": "n",
    "owner": {"login": "o"},
    "hasIssuesEnabled": True,
    "description": "",
    "hasWikiEnabled": False,
    "viewerPermission": "ADMIN",
    "defaultBranchRef": {"name": "main"},
    "parent": None,
    "mergeCommitAllowed": True,
    "rebaseMergeAllowed": True,
    "squashMergeAllowed": True,
}
PULL_REQUEST_QUERY = "query PullRequestByNumber("
PROJECT_CARDS_FIELD = "projectCards"
PROJECT_ITEMS_QUERY = "query PullRequestProjectItems("
UPDATE_MUTATION = "mutation PullRequestUpdate("
PULL_REQUEST = {
    "id": "PR_1",
    "number": 1,
    "url": "https://github.com/o/n/pull/1",
    "title": "t",
    "body": "old",
    "baseRefName": "main",
    "author": {"login": "a"},
    "reviewRequests": {"nodes": []},
    "labels": {"nodes": [], "totalCount": 0},
    "assignedActors": {"nodes": [], "totalCount": 0},
    "milestone": None,
    "repository": {
        "id": "R_1",
        "name": "n",
        "nameWithOwner": "o/n",
        "databaseId": 1,
        "viewerPermission": "ADMIN",
    },
}
CLASSIC_PROJECTS_ERROR = {
    "data": {"repository": {"pullRequest": None}},
    "errors": [
        {
            "type": "DEPRECATED",
            "path": ["repository", "pullRequest", "projectCards"],
            "message": (
                "Projects (classic) is being deprecated in favor of the new"
                " Projects experience, see:"
                " https://github.blog/changelog/2024-05-23-sunset-notice-projects-classic/."
            ),
        }
    ],
}
PROJECT_ITEMS = {
    "data": {
        "repository": {
            "pullRequest": {
                "projectItems": {
                    "totalCount": 0,
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }
}
UPDATE_PAYLOAD = {
    "data": {"updatePullRequest": {"__typename": "UpdatePullRequestPayload"}}
}
UPLOAD_PATH = "/user-attachments/assets"
FAILING_UPLOAD_NAME = "upload-fails.png"
ASSET_URL_PREFIX = "https://github.com/user-attachments/assets/fake-"
VALIDATION_FAILED = {"message": "Validation Failed"}


def logs_archive() -> bytes:
    """Return the run's log archive, a zip holding the one failed step's log."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(STEP_LOG_NAME, STEP_LOG)
    return buffer.getvalue()


def graphql_answer(body: str) -> object | None:
    """Return the answer to the GraphQL request body, or None for one not known."""
    if REPOSITORY_QUERY in body:
        return {"data": {"repository": REPOSITORY}}
    if PULL_REQUEST_QUERY in body:
        if PROJECT_CARDS_FIELD in body:
            return CLASSIC_PROJECTS_ERROR
        return {"data": {"repository": {"pullRequest": PULL_REQUEST}}}
    if PROJECT_ITEMS_QUERY in body:
        return PROJECT_ITEMS
    if UPDATE_MUTATION in body:
        return UPDATE_PAYLOAD
    return None


def log_event(event: dict[str, object]) -> None:
    """Print event as one JSON line, for the test to read from the log."""
    print(json.dumps(event))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Answer a GET by its path before any query."""
        path = self.path.split("?", 1)[0]
        if path == LOGS_PATH:
            self._send(200, "application/zip", logs_archive())
        elif path in JOB_LOG_REDIRECTS:
            self._send_redirect(JOB_LOG_REDIRECTS[path])
        elif path in JSON_ANSWERS:
            self._send_json(200, JSON_ANSWERS[path])
        else:
            self._send_json(404, NOT_FOUND)

    def do_POST(self) -> None:
        """Answer uploads, gh's GraphQL requests, and any other POST with 404."""
        body = self._read_body()
        path, _, query = self.path.partition("?")
        if path == UPLOAD_PATH:
            self._answer_upload(parse_qs(query).get("name", [""])[0])
        elif path == GRAPHQL_PATH:
            self._answer_graphql(body.decode())
        else:
            self._send_json(404, NOT_FOUND)

    def _read_body(self) -> bytes:
        """Return the request body, whether it is sized or chunked."""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked_body()
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _read_chunked_body(self) -> bytes:
        """Return a chunked request body, reading through its trailer."""
        chunks = []
        while size := int(self.rfile.readline().split(b";", 1)[0], 16):
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        return b"".join(chunks)

    def _answer_upload(self, name: str) -> None:
        log_event(
            {
                "upload": name,
                "host": self.headers.get("Host"),
                "authorization": self.headers.get("Authorization"),
            }
        )
        if name == FAILING_UPLOAD_NAME:
            self._send_json(422, VALIDATION_FAILED)
        else:
            self._send_json(201, {"url": ASSET_URL_PREFIX + name})

    def _answer_graphql(self, body: str) -> None:
        if UPDATE_MUTATION in body:
            log_event(
                {"update_body": json.loads(body)["variables"]["input"].get("body")}
            )
        answer = graphql_answer(body)
        if answer is None:
            self._send_json(404, NOT_FOUND)
        else:
            self._send_json(200, answer)

    def _send_json(self, status: int, document: object) -> None:
        self._send(status, "application/json", json.dumps(document).encode())

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(ADDRESS, Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(CERTIFICATE_PATH, KEY_PATH)
server.socket = context.wrap_socket(server.socket, server_side=True)
print("serving")
server.serve_forever()
