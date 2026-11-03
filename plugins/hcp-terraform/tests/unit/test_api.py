import http
import io
import urllib.error
import urllib.request
import urllib.response
from email.message import Message

import pytest
from hcp_terraform_plugin import api
from hcp_terraform_plugin.errors import ApiError, RefusalError

BASE = "https://app.terraform.io/api/v2"
TOKEN = "unit-token"
LOG_URL = "https://archivist.terraform.io/v1/object/abc123"
NOT_FOUND = b'{"errors":[{"status":"404","title":"not found"}]}'
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


class StalledBody(io.RawIOBase):
    """A body stream that times out on its first read."""

    def readable(self) -> bool:
        """Report that the stream is readable."""
        return True

    def readinto(self, buffer: memoryview) -> int:
        """Time out instead of filling buffer."""
        raise TimeoutError("timed out")


class FakeTransport(urllib.request.HTTPSHandler):
    """An HTTPS handler that answers from a table by URL and records each request."""

    def __init__(self) -> None:
        """Start with no answers, no requests, and no failure."""
        super().__init__()
        self.answers: dict[str, tuple[int, dict[str, str], bytes | StalledBody]] = {}
        self.seen: list[urllib.request.Request] = []
        self.failure: urllib.error.URLError | None = None

    def https_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
        """Record req, then raise the failure or send the answer for its URL, or 404."""
        self.seen.append(req)
        if self.failure is not None:
            raise self.failure
        status, headers, body = self.answers.get(req.full_url, (404, {}, NOT_FOUND))
        message = Message()
        for name, value in headers.items():
            message[name] = value
        stream = io.BytesIO(body) if isinstance(body, bytes) else body
        response = urllib.response.addinfourl(stream, message, req.full_url, status)
        # urllib's error processor reads the reason phrase from msg.
        response.msg = http.HTTPStatus(status).phrase
        return response


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Put a FakeTransport in every opener, clear the proxies, and set TFE_TOKEN."""
    fake = FakeTransport()
    build_opener = urllib.request.build_opener
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: build_opener(*handlers, fake),
    )
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TFE_TOKEN", TOKEN)
    return fake


def test_the_hosts_are_constants(transport: FakeTransport) -> None:
    """The API base URL and the log host are fixed values."""
    assert api.BASE_URL == BASE
    assert api.LOG_HOST == "archivist.terraform.io"


def test_get_json_returns_the_decoded_body_of_a_2xx(transport: FakeTransport) -> None:
    """A 2xx answer with a JSON object body returns that object."""
    transport.answers[f"{BASE}/runs/run-1"] = (
        200,
        {},
        b'{"data": {"id": "run-1"}}',
    )

    assert api.get_json("/runs/run-1") == {"data": {"id": "run-1"}}


def test_get_json_sends_one_get_with_exactly_the_token_headers(
    transport: FakeTransport,
) -> None:
    """The request is one GET carrying the token, Accept, and User-Agent headers."""
    transport.answers[f"{BASE}/runs/run-1"] = (
        200,
        {},
        b'{"data": {"id": "run-1"}}',
    )

    api.get_json("/runs/run-1")

    [request] = transport.seen
    assert request.get_method() == "GET"
    assert request.full_url == f"{BASE}/runs/run-1"
    assert request.timeout == 30
    assert request.headers == {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.api+json",
        "User-agent": "agent-proxy-hcp-terraform-plugin",
    }


def test_get_json_appends_the_encoded_query(transport: FakeTransport) -> None:
    """A query is urlencoded onto the path after a question mark."""
    url = (
        f"{BASE}/workspaces/ws-1/runs"
        "?page%5Bsize%5D=20&filter%5Boperation%5D=plan_only%2Cplan_and_apply"
    )
    transport.answers[url] = (200, {}, b'{"data": []}')

    result = api.get_json(
        "/workspaces/ws-1/runs",
        {"page[size]": "20", "filter[operation]": "plan_only,plan_and_apply"},
    )

    assert result == {"data": []}
    [request] = transport.seen
    assert request.full_url == url


def test_get_json_does_not_follow_a_redirect(transport: FakeTransport) -> None:
    """A 302 fails with its status and body, and no second request goes out."""
    transport.answers[f"{BASE}/runs/run-1"] = (
        302,
        {"Location": f"{BASE}/runs/run-2"},
        b"moved",
    )
    transport.answers[f"{BASE}/runs/run-2"] = (200, {}, b'{"data": {}}')

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert str(raised.value) == "GET /runs/run-1 answered 302: moved"
    assert [request.full_url for request in transport.seen] == [f"{BASE}/runs/run-1"]


@pytest.mark.parametrize("status", [301, 404, 500])
def test_get_json_names_the_status_and_body_of_a_failed_answer(
    transport: FakeTransport, status: int
) -> None:
    """A non-2xx answer fails with its status and body."""
    transport.answers[f"{BASE}/runs/run-1"] = (status, {}, b"no")

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert str(raised.value) == f"GET /runs/run-1 answered {status}: no"


@pytest.mark.parametrize("status", [301, 404, 500])
def test_get_json_carries_the_status_of_a_failed_answer(
    transport: FakeTransport, status: int
) -> None:
    """A non-2xx answer's error holds the answer's status."""
    transport.answers[f"{BASE}/runs/run-1"] = (status, {}, b"no")

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert raised.value.status == status


def test_get_json_names_200_characters_of_a_failed_answers_body(
    transport: FakeTransport,
) -> None:
    """The failure reason holds the first 200 characters of the body."""
    transport.answers[f"{BASE}/runs/run-1"] = (404, {}, b"x" * 300)

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert str(raised.value) == f"GET /runs/run-1 answered 404: {'x' * 200}"


def test_get_json_names_a_request_that_got_no_answer(
    transport: FakeTransport,
) -> None:
    """A transport failure fails with the URL error's text."""
    transport.failure = urllib.error.URLError("refused")

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert str(raised.value) == "GET /runs/run-1 failed: <urlopen error refused>"


def test_get_json_carries_no_status_when_no_answer_came(
    transport: FakeTransport,
) -> None:
    """A transport failure's error holds no status."""
    transport.failure = urllib.error.URLError("refused")

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert raised.value.status is None


@pytest.mark.parametrize("status", [200, 500])
def test_get_json_names_an_answer_whose_body_never_arrives(
    transport: FakeTransport, status: int
) -> None:
    """A body read that times out fails as a request that got no answer."""
    transport.answers[f"{BASE}/runs/run-1"] = (status, {}, StalledBody())

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert str(raised.value) == "GET /runs/run-1 failed: timed out"


@pytest.mark.parametrize("body", [b"[]", b"not json", b"\xff"])
def test_get_json_refuses_a_2xx_body_that_is_not_a_json_object(
    transport: FakeTransport, body: bytes
) -> None:
    """A 2xx body that does not decode to a JSON object fails with that body."""
    transport.answers[f"{BASE}/runs/run-1"] = (200, {}, body)

    with pytest.raises(ApiError) as raised:
        api.get_json("/runs/run-1")

    assert str(raised.value) == (
        "GET /runs/run-1 answered a body that is not a JSON object: "
        f"{body.decode('utf-8', errors='replace')}"
    )


@pytest.mark.parametrize("token", [None, ""])
def test_get_json_refuses_without_a_token_before_any_request(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    """A missing or empty TFE_TOKEN is refused before the request goes out."""
    if token is None:
        monkeypatch.delenv("TFE_TOKEN")
    else:
        monkeypatch.setenv("TFE_TOKEN", token)

    with pytest.raises(RefusalError, match=r"^TFE_TOKEN is not set$"):
        api.get_json("/runs/run-1")

    assert transport.seen == []


@pytest.mark.parametrize("token", [f"{TOKEN}\n", f" {TOKEN}\r\n"])
def test_get_json_strips_the_space_around_the_token(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """A token stored with space around it is sent without that space."""
    monkeypatch.setenv("TFE_TOKEN", token)
    transport.answers[f"{BASE}/runs/run-1"] = (200, {}, b'{"data": {"id": "run-1"}}')

    api.get_json("/runs/run-1")

    [request] = transport.seen
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "token",
    [f"{TOKEN}\r\nX: y", f"{TOKEN} x", f"{TOKEN}\x7f", f"{TOKEN}\u00e9"],
)
def test_get_json_refuses_a_token_outside_visible_ascii_before_any_request(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """A token holding a character other than visible ASCII is refused unquoted."""
    monkeypatch.setenv("TFE_TOKEN", token)

    with pytest.raises(RefusalError) as raised:
        api.get_json("/runs/run-1")

    assert str(raised.value) == "TFE_TOKEN holds a character that is not visible ASCII"
    assert transport.seen == []


def test_get_log_returns_the_body_without_its_framing_characters(
    transport: FakeTransport,
) -> None:
    """The log text drops the STX and ETX markers and replaces invalid bytes."""
    transport.answers[LOG_URL] = (200, {}, b"\x02plan line\n\x03ok \xff\n")

    assert api.get_log(LOG_URL) == "plan line\nok \N{REPLACEMENT CHARACTER}\n"


def test_get_log_sends_one_get_with_no_authorization_header(
    transport: FakeTransport,
) -> None:
    """The log request is one GET and carries no token."""
    transport.answers[LOG_URL] = (200, {}, b"log")

    api.get_log(LOG_URL)

    [request] = transport.seen
    assert request.get_method() == "GET"
    assert request.full_url == LOG_URL
    assert request.timeout == 30
    assert not request.has_header("Authorization")
    assert TOKEN not in str(request.header_items())


def test_get_log_follows_a_redirect_without_a_token(
    transport: FakeTransport,
) -> None:
    """A redirected log request is followed, and no request carries the token."""
    moved = "https://archivist.terraform.io/v1/object/def456"
    transport.answers[LOG_URL] = (307, {"Location": moved}, b"")
    transport.answers[moved] = (200, {}, b"log")

    assert api.get_log(LOG_URL) == "log"
    assert [request.full_url for request in transport.seen] == [LOG_URL, moved]
    assert not any(request.has_header("Authorization") for request in transport.seen)


@pytest.mark.parametrize(
    "url",
    [
        "http://archivist.terraform.io/v1/object/a",
        "https://app.terraform.io/api/v2/runs",
        "https://archivist.terraform.io.evil.example/v1/object/a",
        "https://evil.example@archivist.terraform.io/v1/object/a",
        "https://archivist.terraform.io:8443/v1/object/a",
        "file:///etc/passwd",
        "",
    ],
)
def test_get_log_refuses_a_url_off_the_log_host_before_any_request(
    transport: FakeTransport, url: str
) -> None:
    """A URL that is not https on the log host is refused before any request."""
    with pytest.raises(RefusalError) as refused:
        api.get_log(url)

    assert str(refused.value) == (
        f"log-read-url {url!r} is not an https URL on archivist.terraform.io"
    )
    assert transport.seen == []


def test_get_log_names_the_status_and_body_of_a_failed_answer(
    transport: FakeTransport,
) -> None:
    """A failed log answer names its status and body."""
    transport.answers[LOG_URL] = (404, {}, NOT_FOUND)

    with pytest.raises(ApiError) as raised:
        api.get_log(LOG_URL)

    assert str(raised.value) == (
        f"GET /v1/object/abc123 answered 404: {NOT_FOUND.decode()}"
    )


def test_get_log_names_a_request_that_got_no_answer(
    transport: FakeTransport,
) -> None:
    """A transport failure on a log read fails with the URL error's text."""
    transport.failure = urllib.error.URLError("refused")

    with pytest.raises(ApiError) as raised:
        api.get_log(LOG_URL)

    assert str(raised.value) == "GET /v1/object/abc123 failed: <urlopen error refused>"


@pytest.mark.parametrize(
    "value", ["run-AbC123", "ws-x1", "apply-1", "cv-abc", "plan-Z9"]
)
def test_safe_id_returns_an_id(value: str) -> None:
    """A well-formed HCP Terraform ID comes back unchanged."""
    assert api.safe_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "run-",
        "-abc",
        "run_abc",
        "Run-1",
        "run-a/b",
        "run-a?b",
        "run-a.b",
        "run-1\n",
        "run-1-2",
        None,
        5,
    ],
)
def test_safe_id_refuses_anything_else(value: object) -> None:
    """Anything that is not a well-formed ID fails and names the value."""
    with pytest.raises(ApiError) as raised:
        api.safe_id(value)

    assert str(raised.value) == f"{value!r} is not an HCP Terraform ID"
