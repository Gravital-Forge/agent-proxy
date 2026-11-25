"""The HTTP layer: the one POST that asks GitHub for an installation token.

The opener keeps urllib's proxy handler, which reads HTTPS_PROXY, and its
default TLS context, which reads SSL_CERT_FILE.
"""

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import IO

from github_app_plugin.errors import ProviderError

API_URL = "https://api.github.com"
USER_AGENT = "agent-proxy-github-app-plugin"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 20
ISSUED_STATUS = 201
# An RS256 JWT's signature alone is longer than this, so a message that echoes
# the request's Authorization header can never quote a whole JWT.
REASON_CHARACTERS = 200


@dataclass(frozen=True)
class InstallationToken:
    """An installation token and its expiry, as GitHub's answer gives them."""

    value: str
    expires_at: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
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


def request_token(installation_id: str, jwt: str) -> InstallationToken:
    """Return the token GitHub issues for the installation, asked with the App's JWT.

    The request follows no redirect, so the JWT goes to the API host and
    nowhere else.

    Raises:
        ProviderError: the answer is not a 201 holding a token and its
            expiry, or the request failed. The reason never quotes the JWT
            or a token.
    """
    path = f"/app/installations/{installation_id}/access_tokens"
    request = urllib.request.Request(  # noqa: S310 - the URL is built from the API_URL constant
        f"{API_URL}{path}",
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        status, body = _answer(request)
    except (OSError, http.client.HTTPException) as error:
        raise ProviderError(f"POST {path} failed: {error}") from error
    if status != ISSUED_STATUS:
        raise ProviderError(f"POST {path} answered {status}{_reason(body)}")
    return _issued_token(path, body)


def _answer(request: urllib.request.Request) -> tuple[int, bytes]:
    """Send request, and return its answer's status and body, a 4xx or 5xx included."""
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as answer:
        with answer:
            return answer.code, answer.read()
    with response:
        return response.status, response.read()


def _issued_token(path: str, body: bytes) -> InstallationToken:
    """Return the token and expiry a 201's body holds.

    Raises:
        ProviderError: the body holds no non-empty token string, or no
            expires_at string. The reason never quotes the body.
    """
    answer = _json_object(body)
    token = answer.get("token")
    expires_at = answer.get("expires_at")
    if not isinstance(token, str) or not token or not isinstance(expires_at, str):
        raise ProviderError(
            f"POST {path} answered {ISSUED_STATUS} "
            "without a token and an expires_at string"
        )
    return InstallationToken(value=token, expires_at=expires_at)


def _reason(body: bytes) -> str:
    """Return the tail of a failure's reason: GitHub's message, or its absence."""
    message = _json_object(body).get("message")
    if not isinstance(message, str):
        return " with no message"
    return f": {' '.join(message.split())[:REASON_CHARACTERS]}"


def _json_object(body: bytes) -> dict[str, object]:
    """Return body decoded as a JSON object, or an empty one when it is not one."""
    try:
        decoded = json.loads(body)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
