"""A fake HTTPS transport for urllib, which the unit tests put in every opener."""

import http
import io
import json
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable
from email.message import Message

TOKEN = "unit-installation-token"
EXPIRES_AT = "2026-11-24T13:00:00Z"
ISSUED = json.dumps(
    {"token": TOKEN, "expires_at": EXPIRES_AT, "repository_selection": "all"}
).encode()


type Answer = tuple[int, dict[str, str], bytes]


class FakeTransport(urllib.request.HTTPSHandler):
    """An HTTPS handler that sends one set answer and records each request.

    The answer is a status, headers, and body, or a function of the request
    that returns them.
    """

    def __init__(self) -> None:
        """Start with a 201 holding a token, no requests, and no failure."""
        super().__init__()
        self.answer: Answer | Callable[[urllib.request.Request], Answer] = (
            201,
            {},
            ISSUED,
        )
        self.seen: list[urllib.request.Request] = []
        self.failure: urllib.error.URLError | None = None

    def https_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
        """Record req, then raise the failure or send the answer."""
        self.seen.append(req)
        if self.failure is not None:
            raise self.failure
        answer = self.answer(req) if callable(self.answer) else self.answer
        status, headers, body = answer
        message = Message()
        for name, value in headers.items():
            message[name] = value
        response = urllib.response.addinfourl(
            io.BytesIO(body), message, req.full_url, status
        )
        # urllib's error processor reads the reason phrase from msg.
        response.msg = http.HTTPStatus(status).phrase
        return response

    def authorization(self) -> str:
        """Return the Authorization header of the one request seen."""
        [request] = self.seen
        return request.headers["Authorization"]
