import base64
import json
import re
from pathlib import Path

import pytest
from github_app_plugin import app_jwt
from github_app_plugin.errors import ProviderError
from keys import GeneratedKey, verifies

NOW = 1_800_000_000
APP_ID = "12345"
URL_SAFE_UNPADDED = re.compile(r"[A-Za-z0-9_-]+")


def _decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _segments(jwt: str) -> list[str]:
    segments = jwt.split(".")
    assert len(segments) == 3
    return segments


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [(b"", ""), (b"a", "YQ"), (b"ab", "YWI"), (b"abc", "YWJj"), (b"\xfb\xff", "-_8")],
)
def test_base64url_is_url_safe_with_no_padding(raw: bytes, encoded: str) -> None:
    """The encoding uses - and _ for + and /, and drops every = of padding."""
    assert app_jwt.base64url(raw) == encoded


def test_jwt_header_is_rs256(pkcs1_key: GeneratedKey) -> None:
    """The header segment decodes to the RS256 JWT header, compact."""
    header, _, _ = _segments(app_jwt.build_jwt(APP_ID, NOW, str(pkcs1_key.private)))

    assert _decode(header) == b'{"alg":"RS256","typ":"JWT"}'


def test_jwt_claims_bracket_now_and_name_the_app(pkcs1_key: GeneratedKey) -> None:
    """The iat is 60s before now, exp 540s after, and iss the App ID as a string."""
    _, claims, _ = _segments(app_jwt.build_jwt(APP_ID, NOW, str(pkcs1_key.private)))

    assert json.loads(_decode(claims)) == {
        "iat": NOW - 60,
        "exp": NOW + 540,
        "iss": APP_ID,
    }


def test_every_segment_is_unpadded_base64url(pkcs1_key: GeneratedKey) -> None:
    """No segment holds a padding = or a character outside base64url's alphabet."""
    segments = _segments(app_jwt.build_jwt(APP_ID, NOW, str(pkcs1_key.private)))

    assert all(URL_SAFE_UNPADDED.fullmatch(segment) for segment in segments)


@pytest.mark.parametrize("key_fixture", ["pkcs1_key", "pkcs8_key"])
def test_signature_verifies_with_the_public_key(
    key_fixture: str, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The signature is the key's RS256 signature of header.claims, in either form."""
    key: GeneratedKey = request.getfixturevalue(key_fixture)
    header, claims, signature = _segments(
        app_jwt.build_jwt(APP_ID, NOW, str(key.private))
    )

    assert verifies(
        key.public, f"{header}.{claims}".encode(), _decode(signature), tmp_path
    )


def test_signature_fails_with_another_public_key(
    pkcs1_key: GeneratedKey, pkcs8_key: GeneratedKey, tmp_path: Path
) -> None:
    """The verification the tests rely on refuses a key that did not sign."""
    header, claims, signature = _segments(
        app_jwt.build_jwt(APP_ID, NOW, str(pkcs1_key.private))
    )

    assert not verifies(
        pkcs8_key.public, f"{header}.{claims}".encode(), _decode(signature), tmp_path
    )


def test_an_absent_key_fails_naming_its_path(tmp_path: Path) -> None:
    """A key file that cannot be opened fails before openssl runs."""
    absent = tmp_path / "absent-key"

    with pytest.raises(ProviderError) as raised:
        app_jwt.build_jwt(APP_ID, NOW, str(absent))

    assert str(raised.value) == (
        f"the App key at {absent} cannot be read: No such file or directory"
    )


def test_an_openssl_that_cannot_run_fails_naming_it(
    pkcs1_key: GeneratedKey, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An openssl command that does not start fails with its path and the error."""
    absent = tmp_path / "absent-openssl"
    monkeypatch.setattr(app_jwt, "OPENSSL", str(absent))

    with pytest.raises(ProviderError) as raised:
        app_jwt.build_jwt(APP_ID, NOW, str(pkcs1_key.private))

    assert str(raised.value) == (
        f"openssl at {absent} could not run: No such file or directory"
    )


def test_a_key_openssl_cannot_read_fails_without_its_content(tmp_path: Path) -> None:
    """A file that holds no key fails with openssl's exit code and first line."""
    not_a_key = tmp_path / "not-a-key"
    not_a_key.write_text("NOT-A-KEY-MARKER\n")

    with pytest.raises(ProviderError) as raised:
        app_jwt.build_jwt(APP_ID, NOW, str(not_a_key))

    message = str(raised.value)
    assert message.startswith(
        f"openssl could not sign the JWT with the App key at {not_a_key}: exit 1: "
    )
    assert "\n" not in message
    assert "NOT-A-KEY-MARKER" not in message
