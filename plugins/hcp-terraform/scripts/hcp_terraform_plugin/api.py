"""The HTTP layer: GETs to the HCP Terraform API and to its log host.

Every opener keeps urllib's proxy handler, which reads HTTPS_PROXY, and its
default TLS context, which reads SSL_CERT_FILE.
"""

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from email.message import Message
from typing import IO

from hcp_terraform_plugin.errors import ApiError, RefusalError

BASE_URL = "https://app.terraform.io/api/v2"
LOG_HOST = "archivist.terraform.io"
CREDENTIAL_ENV_NAME = "TFE_TOKEN"
USER_AGENT = "agent-proxy-hcp-terraform-plugin"
TIMEOUT_SECONDS = 30
REASON_BODY_CHARACTERS = 200
ID_PATTERN = re.compile(r"[a-z]+-[A-Za-z0-9]+")
LOG_FRAMING = str.maketrans("", "", "\x02\x03")


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


def get_json(path: str, query: Mapping[str, str] | None = None) -> dict[str, object]:
    """Return the JSON object HCP Terraform answers GET path with.

    The request carries the token and follows no redirect, so the token goes
    to the API host and nowhere else.

    Raises:
        RefusalError: TFE_TOKEN is unset or empty.
        ApiError: the answer is not a 2xx holding a JSON object, or the
            request failed.
    """
    token = os.environ.get(CREDENTIAL_ENV_NAME)
    if not token:
        raise RefusalError(f"{CREDENTIAL_ENV_NAME} is not set")
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(  # noqa: S310 - the URL is built from the BASE_URL constant
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.api+json",
            "User-Agent": USER_AGENT,
        },
    )
    body = _send(urllib.request.build_opener(_NoRedirect()), request, path)
    return _json_object(path, body)


def get_log(url: str) -> str:
    """Return the log at url, a log-read-url, as text without its framing.

    The request carries no token and may follow redirects.

    Raises:
        RefusalError: url is not https on the log host.
        ApiError: the answer is not a 2xx, or the request failed.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != LOG_HOST:
        raise RefusalError(f"log-read-url {url!r} is not an https URL on {LOG_HOST}")
    request = urllib.request.Request(  # noqa: S310 - the scheme and host are checked above
        url, headers={"User-Agent": USER_AGENT}
    )
    body = _send(urllib.request.build_opener(), request, parts.path)
    return body.decode("utf-8", errors="replace").translate(LOG_FRAMING)


def safe_id(value: object) -> str:
    """Return value when it is an HCP Terraform ID, safe to place in a URL path.

    Raises:
        ApiError: value is not a string of the form prefix-alphanumerics.
    """
    if isinstance(value, str) and ID_PATTERN.fullmatch(value):
        return value
    raise ApiError(f"{value!r} is not an HCP Terraform ID")


def _send(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request, path: str
) -> bytes:
    """Send request through opener, and return the body of its 2xx answer.

    Raises:
        ApiError: the answer is not a 2xx, or the request failed.
    """
    try:
        return _answer_body(opener, request, path)
    except (OSError, http.client.HTTPException) as error:
        raise ApiError(f"GET {path} failed: {error}") from error


def _answer_body(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request, path: str
) -> bytes:
    """Send request through opener, and return the body of its 2xx answer.

    Raises:
        ApiError: the answer is not a 2xx.
    """
    try:
        response = opener.open(request, timeout=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as answer:
        with answer:
            raise ApiError(_answer_reason(path, answer.code, answer.read())) from answer
    with response:
        return response.read()


def _json_object(path: str, body: bytes) -> dict[str, object]:
    """Return body decoded as a JSON object.

    Raises:
        ApiError: body is not a JSON object.
    """
    try:
        decoded = json.loads(body)
    except ValueError:
        decoded = None
    if not isinstance(decoded, dict):
        text = _text(body)
        raise ApiError(f"GET {path} answered a body that is not a JSON object: {text}")
    return decoded


def _answer_reason(path: str, status: int, body: bytes) -> str:
    """Return why an answer to GET path is a failure."""
    return f"GET {path} answered {status}: {_text(body)}"


def _text(body: bytes) -> str:
    """Return the first characters of body that a reason quotes."""
    return body.decode("utf-8", errors="replace")[:REASON_BODY_CHARACTERS]
