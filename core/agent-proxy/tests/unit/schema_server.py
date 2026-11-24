"""A loopback HTTP server that serves an empty schema and records each request."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class SchemaServer:
    """Where the server listens, and the path of each request it received."""

    url: str
    requested_paths: list[str] = field(default_factory=list)


@contextmanager
def serving_schema() -> Iterator[SchemaServer]:
    """Serve {} at every path, on a loopback port the system assigns."""
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested_paths.append(self.path)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/schema+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, message_format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        try:
            yield SchemaServer(f"http://{host}:{port}", requested_paths)
        finally:
            server.shutdown()
            thread.join()
