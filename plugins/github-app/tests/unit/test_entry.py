import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest
from fake_transport import EXPIRES_AT, ISSUED, TOKEN, Answer, FakeTransport
from github_app_plugin import app_jwt, entry
from keys import GeneratedKey

ENTRY = Path(__file__).parents[2] / "scripts" / "entry"
APP_ID = "12345"
INSTALLATION_ID = "42"
URL = "https://api.github.com/app/installations/42/access_tokens"
PATH = "/app/installations/42/access_tokens"
PREFIX = "github-app plugin failed: "


@pytest.fixture
def inputs(monkeypatch: pytest.MonkeyPatch, pkcs1_key: GeneratedKey) -> GeneratedKey:
    """Set both IDs, and point the key path at the PKCS#1 test key."""
    monkeypatch.setenv("GITHUB_APP_ID", APP_ID)
    monkeypatch.setenv("GITHUB_INSTALLATION_ID", INSTALLATION_ID)
    monkeypatch.setattr(app_jwt, "KEY_PATH", str(pkcs1_key.private))
    return pkcs1_key


def _run(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    exit_code = entry.main()
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _claims(authorization: str) -> dict[str, object]:
    claims = authorization.removeprefix("Bearer ").split(".")[1]
    return json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))


def test_a_201_prints_the_value_and_expiry_as_one_compact_line(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The token and GitHub's expires_at print as one JSON line, with exit 0."""
    assert _run(capsys) == (
        0,
        f'{{"value":"{TOKEN}","expires_at":"{EXPIRES_AT}"}}\n',
        "",
    )


def test_the_request_names_the_installation_and_the_app(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The POST goes to the installation's URL with a JWT issued by the App."""
    _run(capsys)

    [request] = transport.seen
    assert request.full_url == URL
    assert _claims(transport.authorization())["iss"] == APP_ID


@pytest.mark.parametrize("name", ["GITHUB_APP_ID", "GITHUB_INSTALLATION_ID"])
@pytest.mark.parametrize("unset", [None, ""])
def test_an_unset_id_fails_before_any_request(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    unset: str | None,
) -> None:
    """An absent or empty ID exits 1 with one line, and sends nothing."""
    if unset is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, unset)

    assert _run(capsys) == (
        1,
        "",
        f"{PREFIX}{name} is not set; the provided credential's env sets it\n",
    )
    assert transport.seen == []


@pytest.mark.parametrize("name", ["GITHUB_APP_ID", "GITHUB_INSTALLATION_ID"])
@pytest.mark.parametrize("value", ["12a", " 12", "12\n", "-1", "١٢"])
def test_an_id_that_is_not_digits_fails_before_any_request(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """An ID holding anything but ASCII digits exits 1 with one line."""
    monkeypatch.setenv(name, value)

    assert _run(capsys) == (
        1,
        "",
        f"{PREFIX}{name} is not a string of digits: {value!r}\n",
    )
    assert transport.seen == []


def test_an_unreadable_key_fails_before_any_request(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A key that cannot be opened exits 1 with one line naming its path."""
    absent = tmp_path / "github-app-key"
    monkeypatch.setattr(app_jwt, "KEY_PATH", str(absent))

    assert _run(capsys) == (
        1,
        "",
        f"{PREFIX}the App key at {absent} cannot be read: No such file or directory\n",
    )
    assert transport.seen == []


def test_a_refusal_prints_its_status_and_githubs_message(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 401 from GitHub exits 1 with one line holding its status and message."""
    transport.answer = (401, {}, b'{"message": "Bad credentials"}')

    assert _run(capsys) == (
        1,
        "",
        f"{PREFIX}POST {PATH} answered 401: Bad credentials\n",
    )


def test_an_unreachable_host_prints_the_error(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A request with no answer exits 1 with one line holding the error."""
    transport.failure = urllib.error.URLError("Tunnel connection failed: 403")

    assert _run(capsys) == (
        1,
        "",
        f"{PREFIX}POST {PATH} failed: <urlopen error Tunnel connection failed: 403>\n",
    )


def _echo_authorization(request: urllib.request.Request) -> Answer:
    authorization = request.headers["Authorization"]
    return 401, {}, json.dumps({"message": f"Authorization: {authorization}"}).encode()


@pytest.mark.parametrize(
    "answer",
    [
        _echo_authorization,
        (201, {}, json.dumps({"token": TOKEN}).encode()),
        (200, {}, ISSUED),
        (500, {}, ISSUED),
    ],
)
def test_no_failure_line_holds_the_key_the_jwt_or_a_token(
    inputs: GeneratedKey,
    transport: FakeTransport,
    capsys: pytest.CaptureFixture[str],
    answer: Answer | Callable[[urllib.request.Request], Answer],
) -> None:
    """Whatever the answer holds, the one stderr line never quotes a secret."""
    transport.answer = answer

    exit_code, out, err = _run(capsys)

    key_lines = inputs.private.read_text().splitlines()[1:-1]
    secrets = [transport.authorization().removeprefix("Bearer "), TOKEN, *key_lines]
    assert (exit_code, out, err.count("\n"), err.endswith("\n")) == (1, "", 1, True)
    assert [secret for secret in secrets if secret in err] == []


def test_the_entry_script_runs_main_as_a_program(pkcs1_key: GeneratedKey) -> None:
    """The image's executable prints main's one failure line and exits with it."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in {"GITHUB_APP_ID", "GITHUB_INSTALLATION_ID"}
    }

    completed = subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [sys.executable, str(ENTRY)],
        capture_output=True,
        check=False,
        env=environment,
        input="{}",
        text=True,
    )

    assert (completed.returncode, completed.stdout, completed.stderr) == (
        1,
        "",
        f"{PREFIX}GITHUB_APP_ID is not set; the provided credential's env sets it\n",
    )
