import json
import urllib.error

import pytest
from fake_transport import EXPIRES_AT, ISSUED, TOKEN, FakeTransport
from github_app_plugin import api
from github_app_plugin.api import InstallationToken
from github_app_plugin.errors import ProviderError

JWT = "header.claims.signature"
INSTALLATION_ID = "42"
URL = "https://api.github.com/app/installations/42/access_tokens"
PATH = "/app/installations/42/access_tokens"


def _failure(transport: FakeTransport) -> str:
    with pytest.raises(ProviderError) as raised:
        api.request_token(INSTALLATION_ID, JWT)
    return str(raised.value)


def test_the_api_host_is_a_constant() -> None:
    """The API base URL is a fixed value, never read from input."""
    assert api.API_URL == "https://api.github.com"


def test_a_201_gives_the_token_and_its_expiry(transport: FakeTransport) -> None:
    """GitHub's 201 answer gives its token and its expires_at."""
    assert api.request_token(INSTALLATION_ID, JWT) == InstallationToken(
        value=TOKEN, expires_at=EXPIRES_AT
    )


def test_the_request_is_one_post_with_the_app_headers(
    transport: FakeTransport,
) -> None:
    """One POST to the installation's access_tokens URL carries the JWT."""
    api.request_token(INSTALLATION_ID, JWT)

    [request] = transport.seen
    assert (request.get_method(), request.full_url, request.timeout) == (
        "POST",
        URL,
        20,
    )
    assert request.headers == {
        "Authorization": f"Bearer {JWT}",
        "Accept": "application/vnd.github+json",
        "X-github-api-version": "2022-11-28",
        "User-agent": "agent-proxy-github-app-plugin",
    }


def test_the_opener_keeps_the_proxy_handler(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request goes through HTTPS_PROXY, as the cage's egress proxy needs."""
    monkeypatch.setenv("HTTPS_PROXY", "http://egress.invalid:3128")

    api.request_token(INSTALLATION_ID, JWT)

    [request] = transport.seen
    assert request.host == "egress.invalid:3128"


def test_a_redirect_is_not_followed(transport: FakeTransport) -> None:
    """A 3xx is a failure after one request, so the JWT goes nowhere else."""
    transport.answer = (302, {"Location": "https://elsewhere.invalid/"}, b"")

    assert _failure(transport) == f"POST {PATH} answered 302 with no message"
    assert len(transport.seen) == 1


def test_a_refusal_quotes_githubs_message(transport: FakeTransport) -> None:
    """A 4xx names its status and the message member of GitHub's answer."""
    transport.answer = (
        401,
        {},
        b'{"message": "A JSON web token could not be decoded", '
        b'"documentation_url": "https://docs.github.com/rest"}',
    )

    assert _failure(transport) == (
        f"POST {PATH} answered 401: A JSON web token could not be decoded"
    )


@pytest.mark.parametrize(
    "body", [b"", b"<html>bad gateway</html>", b'{"message": 7}', b"[1]"]
)
def test_an_answer_with_no_message_string_says_so(
    transport: FakeTransport, body: bytes
) -> None:
    """A failed answer whose body holds no message string never quotes the body."""
    transport.answer = (502, {}, body)

    assert _failure(transport) == f"POST {PATH} answered 502 with no message"


def test_a_message_becomes_one_line_of_at_most_200_characters(
    transport: FakeTransport,
) -> None:
    """Whitespace runs in the message become one space, and the rest is cut."""
    message = "first\nsecond\r\n\tthird " + "x" * 300
    transport.answer = (403, {}, json.dumps({"message": message}).encode())

    reason = _failure(transport).removeprefix(f"POST {PATH} answered 403: ")

    assert reason == ("first second third " + "x" * 300)[:200]


def test_a_2xx_that_is_not_201_is_a_failure(transport: FakeTransport) -> None:
    """Only a 201 gives a token; a 200 holding one is still a failure."""
    transport.answer = (200, {}, ISSUED)

    reason = _failure(transport)

    assert reason == f"POST {PATH} answered 200 with no message"
    assert TOKEN not in reason


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"token": TOKEN}).encode(),
        json.dumps({"expires_at": EXPIRES_AT}).encode(),
        json.dumps({"token": "", "expires_at": EXPIRES_AT}).encode(),
        json.dumps({"token": TOKEN, "expires_at": 7}).encode(),
    ],
)
def test_a_201_without_a_token_and_expiry_fails_without_quoting_it(
    transport: FakeTransport, body: bytes
) -> None:
    """A 201 that lacks a non-empty token or a string expires_at is a failure."""
    transport.answer = (201, {}, body)

    reason = _failure(transport)

    assert reason == (
        f"POST {PATH} answered 201 without a token and an expires_at string"
    )
    assert TOKEN not in reason


def test_an_unreachable_host_fails_with_the_error(transport: FakeTransport) -> None:
    """A request that gets no answer names the path and the error."""
    transport.failure = urllib.error.URLError("Tunnel connection failed: 403")

    assert _failure(transport) == (
        f"POST {PATH} failed: <urlopen error Tunnel connection failed: 403>"
    )
