"""The App's JWT: RS256, signed with the App key through the openssl command."""

import base64
import json
import subprocess
from pathlib import Path

from github_app_plugin.errors import ProviderError

KEY_PATH = "/run/credentials/github-app-key"
OPENSSL = "/usr/bin/openssl"
HEADER = {"alg": "RS256", "typ": "JWT"}
# GitHub refuses an exp more than ten minutes in its future; iat sits a minute
# back for a clock that runs ahead of GitHub's.
ISSUED_BEFORE_SECONDS = 60
EXPIRES_AFTER_SECONDS = 540


def build_jwt(app_id: str, now: int, key_path: str) -> str:
    """Return the App's JWT for the Unix time now, signed with the key at key_path.

    Raises:
        ProviderError: the key cannot be read, openssl cannot run, or it
            cannot sign with the key.
    """
    claims = {
        "iat": now - ISSUED_BEFORE_SECONDS,
        "exp": now + EXPIRES_AFTER_SECONDS,
        "iss": app_id,
    }
    signing_input = f"{_segment(HEADER)}.{_segment(claims)}"
    signature = _sign(signing_input.encode(), key_path)
    return f"{signing_input}.{base64url(signature)}"


def base64url(raw: bytes) -> str:
    """Return raw in base64url with no padding, as a JWT segment holds it."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _segment(part: dict[str, object]) -> str:
    return base64url(json.dumps(part, separators=(",", ":")).encode())


def _sign(signing_input: bytes, key_path: str) -> bytes:
    """Return the RS256 signature of signing_input with the key at key_path.

    Raises:
        ProviderError: the key cannot be read, openssl cannot run, or it
            cannot sign with the key. The reason quotes openssl's first
            stderr line, which names the file and never quotes its content.
    """
    try:
        with Path(key_path).open("rb"):
            pass
    except OSError as error:
        raise ProviderError(
            f"the App key at {key_path} cannot be read: {error.strerror}"
        ) from error
    try:
        completed = subprocess.run(  # noqa: S603 - the arguments are constants and the key path
            [OPENSSL, "dgst", "-sha256", "-sign", key_path],
            input=signing_input,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ProviderError(
            f"openssl at {OPENSSL} could not run: {error.strerror}"
        ) from error
    if completed.returncode != 0:
        first_line = completed.stderr.decode(errors="replace").partition("\n")[0]
        raise ProviderError(
            f"openssl could not sign the JWT with the App key at {key_path}: "
            f"exit {completed.returncode}: {first_line}"
        )
    return completed.stdout
