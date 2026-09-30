"""The rules every proxy module shares."""

import re
from collections.abc import Hashable, Mapping
from datetime import timedelta
from pathlib import Path

import yaml

from agent_proxy.errors import ProxyError

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
PLUGIN_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
ACTION_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
DURATION_PATTERN = r"^[1-9][0-9]*[smhd]$"

_DURATION_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


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


class DurationError(ProxyError, ValueError):
    """A duration does not match DURATION_PATTERN or does not fit a timedelta."""


def parse_duration(text: str) -> timedelta:
    """Return the timedelta a duration such as "120s" or "7d" names.

    Raises:
        DurationError: text does not match DURATION_PATTERN, or its count is too
            large for a timedelta.
    """
    if re.fullmatch(DURATION_PATTERN, text) is None:
        raise DurationError(f"duration must match {DURATION_PATTERN!r}, got {text!r}")
    try:
        return int(text[:-1]) * _DURATION_UNITS[text[-1]]
    # int() raises ValueError past the interpreter's digit limit for str conversion.
    except (OverflowError, ValueError) as exc:
        raise DurationError(f"duration must fit in a timedelta, got {text!r}") from exc


class ConfigError(ProxyError):
    """A config file cannot be read or does not hold a valid config."""

    def __init__(self, *errors: str) -> None:
        """Hold one message per failure; str() prints them one per line."""
        self.errors = errors
        super().__init__("\n".join(errors))


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a key repeated in one mapping."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Hashable, object]:
    seen: set[Hashable] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, Hashable):
            continue
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def read_yaml(path: Path) -> Mapping[str, object]:
    """Return the top-level mapping of the YAML file at path.

    Raises:
        ConfigError: the file cannot be read, is not valid YAML, repeats a key in
            one mapping, or holds something other than a mapping at the top level.
    """
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise ConfigError(
            f"config file {str(path)!r} cannot be read: {exc.strerror}"
        ) from exc
    try:
        document = yaml.load(source, Loader=_UniqueKeyLoader)  # noqa: S506 — a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"config file {str(path)!r} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ConfigError(
            f"config file {str(path)!r} must hold a mapping at the top level, "
            f"got {type(document).__name__}"
        )
    return document
