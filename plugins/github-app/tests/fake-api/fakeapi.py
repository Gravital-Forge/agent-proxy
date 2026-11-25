"""A fake api.github.com for the github-app plugin's integration tests.

It runs in a container on the test's internal network, under the alias
api.github.com, and serves TLS with a certificate the image's test CA signs
for that name.

It answers POST /app/installations/<id>/access_tokens. When the request's
bearer JWT is an RS256 signature by the key whose public PEM the
TRUSTED_PUBLIC_KEY variable holds, it answers 201 with a token that expires an
hour later; otherwise it answers 401 with GitHub's message. It answers any
other request with 404.

It prints one JSON line for each request: the method, the Host header, the
path, the Accept, X-GitHub-Api-Version, and User-Agent headers, the JWT's
decoded header and claims, whether its signature verified, and what it issued.
It prints serving once its socket is bound.
"""

import base64
import json
import os
import re
import ssl
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ADDRESS = ("", 443)
CERTIFICATE_PATH = "/pki/server.crt"
KEY_PATH = "/pki/server.key"
TRUSTED_KEY_PATH = "/tmp/trusted.pem"  # noqa: S108 - the fake's own container
OPENSSL = "/usr/bin/openssl"
TOKEN_PATH = re.compile(r"/app/installations/[0-9]+/access_tokens")
ISSUED_TOKEN = "fake-installation-token"
TOKEN_LIFETIME = timedelta(hours=1)
UNDECODABLE = {
    "message": "A JSON web token could not be decoded",
    "documentation_url": "https://docs.github.com/rest",
    "status": "401",
}
NOT_FOUND = {"message": "Not Found", "status": "404"}


def log_event(event: dict[str, object]) -> None:
    """Print event as one JSON line, for the test to read from the log."""
    print(json.dumps(event))


def decode_segment(segment: str) -> bytes:
    """Return a base64url JWT segment, which has no padding, as bytes."""
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def decoded_part(segment: str) -> object:
    """Return a JWT segment decoded as JSON, or None when it is not JSON."""
    try:
        return json.loads(decode_segment(segment))
    except ValueError:
        return None


def signature_verifies(signing_input: str, signature: bytes) -> bool:
    """Return whether signature is the trusted key's RS256 signature."""
    with tempfile.NamedTemporaryFile() as signature_file:
        signature_file.write(signature)
        signature_file.flush()
        completed = subprocess.run(  # noqa: S603 - fixed arguments the fake chooses
            [
                OPENSSL,
                "dgst",
                "-sha256",
                "-verify",
                TRUSTED_KEY_PATH,
                "-signature",
                signature_file.name,
            ],
            input=signing_input.encode(),
            capture_output=True,
            check=False,
        )
    return completed.returncode == 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        """Answer a POST to an installation's access_tokens with a token or a 401."""
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not TOKEN_PATH.fullmatch(self.path):
            self._log_request({})
            self._send_json(404, NOT_FOUND)
            return
        checked = self._check_jwt()
        if not checked["signature_valid"]:
            self._log_request(checked)
            self._send_json(401, UNDECODABLE)
            return
        expires_at = (datetime.now(UTC) + TOKEN_LIFETIME).strftime("%Y-%m-%dT%H:%M:%SZ")
        issued = {"token": ISSUED_TOKEN, "expires_at": expires_at}
        self._log_request({**checked, "issued": issued})
        self._send_json(201, {**issued, "repository_selection": "all"})

    def do_GET(self) -> None:
        """Answer a GET with 404."""
        self._log_request({})
        self._send_json(404, NOT_FOUND)

    def _check_jwt(self) -> dict[str, object]:
        authorization = self.headers.get("Authorization", "")
        segments = authorization.removeprefix("Bearer ").split(".")
        if not authorization.startswith("Bearer ") or len(segments) != 3:
            return {"jwt_header": None, "jwt_claims": None, "signature_valid": False}
        header, claims, signature = segments
        try:
            signature_bytes = decode_segment(signature)
        except ValueError:
            signature_bytes = b""
        return {
            "jwt_header": decoded_part(header),
            "jwt_claims": decoded_part(claims),
            "signature_valid": signature_verifies(
                f"{header}.{claims}", signature_bytes
            ),
        }

    def _log_request(self, details: dict[str, object]) -> None:
        log_event(
            {
                "method": self.command,
                "host": self.headers.get("Host", "").split(":", 1)[0],
                "path": self.path,
                "accept": self.headers.get("Accept"),
                "api_version": self.headers.get("X-GitHub-Api-Version"),
                "user_agent": self.headers.get("User-Agent"),
                **details,
            }
        )

    def _send_json(self, status: int, document: object) -> None:
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


Path(TRUSTED_KEY_PATH).write_text(os.environ["TRUSTED_PUBLIC_KEY"])
server = ThreadingHTTPServer(ADDRESS, Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(CERTIFICATE_PATH, KEY_PATH)
server.socket = context.wrap_socket(server.socket, server_side=True)
print("serving")
server.serve_forever()
