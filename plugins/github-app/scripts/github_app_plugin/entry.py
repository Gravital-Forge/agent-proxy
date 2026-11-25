"""The provider's executable: it prints one installation token, or one failure line."""

import json
import os
import re
import sys
import time
from collections.abc import Mapping

from github_app_plugin import api, app_jwt
from github_app_plugin.errors import ProviderError

FAILURE_PREFIX = "github-app plugin failed: "
FAILURE_EXIT = 1
APP_ID_NAME = "GITHUB_APP_ID"
INSTALLATION_ID_NAME = "GITHUB_INSTALLATION_ID"
DIGITS = re.compile(r"[0-9]+")


def main() -> int:
    """Print the installation's token and expiry as one JSON line, and return 0.

    Any failure prints one line on stderr, which never holds the key, the
    JWT, or a token, and returns 1.
    """
    try:
        token = installation_token(os.environ, int(time.time()))
    except ProviderError as error:
        print(f"{FAILURE_PREFIX}{error}", file=sys.stderr)
        return FAILURE_EXIT
    output = {"value": token.value, "expires_at": token.expires_at}
    print(json.dumps(output, separators=(",", ":")))
    return 0


def installation_token(environ: Mapping[str, str], now: int) -> api.InstallationToken:
    """Return the token GitHub issues to the App's installation at Unix time now.

    Raises:
        ProviderError: an ID is unset or not digits, the key cannot sign, or
            GitHub gives no token.
    """
    app_id = _id(environ, APP_ID_NAME)
    installation_id = _id(environ, INSTALLATION_ID_NAME)
    jwt = app_jwt.build_jwt(app_id, now, app_jwt.KEY_PATH)
    return api.request_token(installation_id, jwt)


def _id(environ: Mapping[str, str], name: str) -> str:
    """Return the ID the variable name holds.

    Raises:
        ProviderError: the variable is unset or empty, or holds anything but
            ASCII digits.
    """
    value = environ.get(name, "")
    if not value:
        raise ProviderError(f"{name} is not set; the provided credential's env sets it")
    if not DIGITS.fullmatch(value):
        raise ProviderError(f"{name} is not a string of digits: {value!r}")
    return value
