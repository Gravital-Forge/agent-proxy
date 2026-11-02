"""A fake app.terraform.io and archivist.terraform.io for the hcp-terraform tests.

It runs in a container on the test's internal network, under both network
aliases, and serves TLS with a certificate the image's test CA signs for both
names.

It serves organization o with two workspaces, listed and other, a run in each,
the listed run's plan, apply, and ingress attributes, and the plan log and the
apply log.

It prints one JSON line for each request of any method, with the method, the
Host header, the path with its query, and the Authorization header or null. It
answers every method but GET with 405 and any unknown GET with 404. It prints
serving once its socket is bound.
"""

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ADDRESS = ("", 443)
CERTIFICATE_PATH = "/pki/server.crt"
KEY_PATH = "/pki/server.key"
API_HOST = "app.terraform.io"
ARCHIVIST_HOST = "archivist.terraform.io"
WORKSPACES_PATH = "/api/v2/organizations/o/workspaces"
PAGE_PARAMETER = "page[number]"
JSON_CONTENT_TYPE = "application/vnd.api+json"
NOT_FOUND = {"errors": [{"status": "404", "title": "not found"}]}
WORKSPACE_LISTED = {
    "id": "ws-listed",
    "type": "workspaces",
    "attributes": {"name": "listed"},
    "relationships": {
        "organization": {"data": {"id": "o", "type": "organizations"}},
        "current-run": {"data": {"id": "run-listed", "type": "runs"}},
    },
}
WORKSPACE_OTHER = {
    "id": "ws-other",
    "type": "workspaces",
    "attributes": {"name": "other"},
    "relationships": {
        "organization": {"data": {"id": "o", "type": "organizations"}},
        "current-run": {"data": {"id": "run-other", "type": "runs"}},
    },
}
RUN_LISTED = {
    "id": "run-listed",
    "type": "runs",
    "attributes": {
        "status": "applied",
        "created-at": "2026-11-01T10:00:00.000Z",
        "source": "tfe-configuration-version",
        "message": "Merge pull request #1",
        "plan-only": False,
        "has-changes": True,
        "status-timestamps": {
            "planned-at": "2026-11-01T10:01:00+00:00",
            "applied-at": "2026-11-01T10:02:00+00:00",
        },
    },
    "relationships": {
        "workspace": {"data": {"id": "ws-listed", "type": "workspaces"}},
        "plan": {"data": {"id": "plan-listed", "type": "plans"}},
        "apply": {"data": {"id": "apply-listed", "type": "applies"}},
        "configuration-version": {
            "data": {"id": "cv-listed", "type": "configuration-versions"}
        },
    },
}
RUN_OTHER = {
    "id": "run-other",
    "type": "runs",
    "attributes": {"status": "planned"},
    "relationships": {
        "workspace": {"data": {"id": "ws-other", "type": "workspaces"}},
    },
}
PLAN_LOG_URL = "https://archivist.terraform.io/v1/object/plan-listed-log"
APPLY_LOG_URL = "https://archivist.terraform.io/v1/object/apply-listed-log"
JSON_ANSWERS: dict[str, object] = {
    "/api/v2/organizations/o/workspaces/listed": {"data": WORKSPACE_LISTED},
    "/api/v2/organizations/o/workspaces/other": {"data": WORKSPACE_OTHER},
    "/api/v2/workspaces/ws-listed": {"data": WORKSPACE_LISTED},
    "/api/v2/workspaces/ws-other": {"data": WORKSPACE_OTHER},
    "/api/v2/workspaces/ws-listed/runs": {"data": [RUN_LISTED]},
    "/api/v2/workspaces/ws-other/runs": {"data": [RUN_OTHER]},
    "/api/v2/runs/run-listed": {"data": RUN_LISTED},
    "/api/v2/runs/run-other": {"data": RUN_OTHER},
    "/api/v2/plans/plan-listed": {
        "data": {
            "id": "plan-listed",
            "type": "plans",
            "attributes": {
                "status": "finished",
                "resource-additions": 1,
                "resource-changes": 2,
                "resource-destructions": 0,
                "log-read-url": PLAN_LOG_URL,
            },
        }
    },
    "/api/v2/applies/apply-listed": {
        "data": {
            "id": "apply-listed",
            "type": "applies",
            "attributes": {"status": "finished", "log-read-url": APPLY_LOG_URL},
        }
    },
    "/api/v2/configuration-versions/cv-listed/ingress-attributes": {
        "data": {
            "id": "ia-listed",
            "type": "ingress-attributes",
            "attributes": {
                "commit-sha": "abc1234",
                "branch": "main",
                "commit-url": "https://github.com/o/infra/commit/abc1234",
            },
        }
    },
}
LOGS = {
    "/v1/object/plan-listed-log": (
        "\x02Terraform will perform the following actions:\n"
        "Plan: 1 to add, 2 to change, 0 to destroy.\n\x03"
    ),
    "/v1/object/apply-listed-log": (
        "\x02Apply complete! Resources: 1 added, 2 changed, 0 destroyed.\n\x03"
    ),
}


def workspaces_page(page: str) -> dict[str, object] | None:
    """Return the workspaces list for the page, or None for a page not known."""
    if page == "1":
        return {
            "data": [WORKSPACE_OTHER],
            "meta": {"pagination": {"current-page": 1, "next-page": 2}},
        }
    if page == "2":
        return {
            "data": [WORKSPACE_LISTED],
            "meta": {"pagination": {"current-page": 2, "next-page": None}},
        }
    return None


def log_event(event: dict[str, object]) -> None:
    """Print event as one JSON line, for the test to read from the log."""
    print(json.dumps(event))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Answer a GET by its host and path."""
        self._log_request()
        host = self._host()
        url = urlsplit(self.path)
        if host == API_HOST and url.path == WORKSPACES_PATH:
            page = parse_qs(url.query).get(PAGE_PARAMETER, ["1"])[0]
            self._send_workspaces(page)
        elif host == API_HOST and url.path in JSON_ANSWERS:
            self._send_json(200, JSON_ANSWERS[url.path])
        elif host == ARCHIVIST_HOST and url.path in LOGS:
            self._send(200, "text/plain", LOGS[url.path].encode())
        else:
            self._send_json(404, NOT_FOUND)

    def do_HEAD(self) -> None:
        """Refuse a HEAD."""
        self._refuse_method()

    def do_POST(self) -> None:
        """Refuse a POST."""
        self._refuse_method()

    def do_PUT(self) -> None:
        """Refuse a PUT."""
        self._refuse_method()

    def do_PATCH(self) -> None:
        """Refuse a PATCH."""
        self._refuse_method()

    def do_DELETE(self) -> None:
        """Refuse a DELETE."""
        self._refuse_method()

    def _host(self) -> str:
        return self.headers.get("Host", "").split(":", 1)[0]

    def _log_request(self) -> None:
        log_event(
            {
                "method": self.command,
                "host": self._host(),
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )

    def _refuse_method(self) -> None:
        self._log_request()
        self.close_connection = True
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_workspaces(self, page: str) -> None:
        document = workspaces_page(page)
        if document is None:
            self._send_json(404, NOT_FOUND)
        else:
            self._send_json(200, document)

    def _send_json(self, status: int, document: object) -> None:
        self._send(status, JSON_CONTENT_TYPE, json.dumps(document).encode())

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
