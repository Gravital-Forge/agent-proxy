"""An HTTP server that answers 401 until the git plugin's credential arrives.

It runs in a container from the plugin image, on the tests' internal network.
It prints READY once it listens. A request that carries the Basic credential
x-access-token:dummy gets the line AUTH OK <method> <path> printed and a 404.
"""

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer

ADDRESS = ("", 8080)
EXPECTED_AUTHORIZATION = "Basic " + base64.b64encode(b"x-access-token:dummy").decode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        """Answer a GET."""
        self._answer()

    def do_POST(self) -> None:
        """Answer a POST."""
        self._answer()

    def log_message(self, *args: object) -> None:
        """Print nothing for a request, so the log holds only READY and AUTH OK."""

    def _answer(self) -> None:
        # An unread body at close can reset the connection before the client
        # reads the answer.
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Authorization") == EXPECTED_AUTHORIZATION:
            print(f"AUTH OK {self.command} {self.path}")
            self.send_response(404)
        else:
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Basic realm=x")
        self.send_header("Content-Length", "0")
        self.end_headers()


server = HTTPServer(ADDRESS, Handler)
print("READY")
server.serve_forever()
