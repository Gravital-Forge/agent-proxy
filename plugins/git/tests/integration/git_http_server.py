"""An HTTPS git server that answers 401 until the git plugin's credential arrives.

It runs in a container from the fixture image, on the tests' internal network.
Its arguments are the paths of its certificate, of its key, and of the
directory that holds its repositories. It serves TLS on port 443 with them,
and prints READY once it listens. A request that carries the Basic credential
x-access-token:dummy goes to git http-backend, whose answer it relays.
"""

import base64
import os
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ADDRESS = ("", 443)
USERNAME = "x-access-token"
EXPECTED_AUTHORIZATION = (
    "Basic " + base64.b64encode(f"{USERNAME}:dummy".encode()).decode()
)
BACKEND = "/usr/libexec/git-core/git-http-backend"
CGI_HEADER_END = b"\r\n\r\n"
OK_STATUS = 200


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        """Answer a GET."""
        self._answer()

    def do_POST(self) -> None:
        """Answer a POST."""
        self._answer()

    def log_message(self, *args: object) -> None:
        """Print nothing for a request, so the log holds only READY."""

    def _answer(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Authorization") != EXPECTED_AUTHORIZATION:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._relay(self._backend(body))

    def _backend(self, body: bytes) -> bytes:
        """Return what git http-backend writes for this request and its body."""
        url = urlsplit(self.path)
        completed = subprocess.run(  # noqa: S603 - the backend, with the request as CGI variables
            [BACKEND],
            input=body,
            capture_output=True,
            check=False,
            env={
                "PATH": os.environ["PATH"],
                "GIT_PROJECT_ROOT": PROJECT_ROOT,
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": url.path,
                "QUERY_STRING": url.query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_USER": USERNAME,
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            },
        )
        return completed.stdout

    def _relay(self, answer: bytes) -> None:
        """Send the backend's CGI answer: its Status, its other headers, its body."""
        head, _, payload = answer.partition(CGI_HEADER_END)
        status = OK_STATUS
        headers: list[tuple[str, str]] = []
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


certificate_path, key_path, PROJECT_ROOT = sys.argv[1:]
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(certificate_path, key_path)
server = ThreadingHTTPServer(ADDRESS, Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
print("READY")
server.serve_forever()
