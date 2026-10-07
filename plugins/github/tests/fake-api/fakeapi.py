"""A fake api.github.com for the tests that run the pinned gh.

It runs in a container on the test's internal network, under the network alias
api.github.com, and serves TLS with a certificate the image's test CA signs. It
answers two jobs' log requests with a redirect to a host the test network
cannot reach, so a client that follows it fails. It prints serving once its
socket is bound.
"""

import io
import json
import ssl
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def logs_archive() -> bytes:
    """Return the run's log archive, a zip holding the one failed step's log."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(STEP_LOG_NAME, STEP_LOG)
    return buffer.getvalue()


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
        """Answer gh's repository query on /graphql, and any other POST with 404."""
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        path = self.path.split("?", 1)[0]
        if path == GRAPHQL_PATH and REPOSITORY_QUERY in body:
            self._send_json(200, {"data": {"repository": REPOSITORY}})
        else:
            self._send_json(404, NOT_FOUND)

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
