"""The rules every proxy module shares."""

import re

from agent_proxy.errors import ProxyError

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
PLUGIN_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
ACTION_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class InvalidNameError(ProxyError):
    """A scope, token, or credential name does not match NAME_PATTERN."""


def check_name(name: str) -> str:
    """Return name unchanged if it matches NAME_PATTERN.

    Raises:
        InvalidNameError: name does not match NAME_PATTERN.
    """
    if re.fullmatch(NAME_PATTERN, name) is None:
        raise InvalidNameError(f"name must match {NAME_PATTERN!r}, got {name!r}")
    return name
