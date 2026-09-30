"""The rules every proxy module shares, and the instance config they govern."""

import re
from collections.abc import Hashable, Iterator, Mapping, Set
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic_core import ErrorDetails

from agent_proxy.errors import ProxyError

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
PLUGIN_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
ACTION_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
GRANT_KEY_PATTERN = (
    f"^{PLUGIN_NAME_PATTERN.strip('^$')}\\.{ACTION_NAME_PATTERN.strip('^$')}$"
)
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


_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a key repeated in one mapping, and merge keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Hashable, object]:
    seen: set[Hashable] = set()
    for key_node, _ in node.value:
        # An explicit key silently overrides a merged one, which would hide a
        # grant field from a reader of the file.
        if key_node.tag == _YAML_MERGE_TAG:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "YAML merge keys ('<<') are not supported",
                key_node.start_mark,
            )
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
            one mapping, uses a merge key, or holds something other than a
            mapping at the top level.
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
        # PyYAML spreads one error over several lines, and each line of a
        # refusal must be one failure.
        reason = "; ".join(
            line.strip() for line in str(exc).splitlines() if line.strip()
        )
        raise ConfigError(
            f"config file {str(path)!r} is not valid YAML: {reason}"
        ) from exc
    if not isinstance(document, dict):
        raise ConfigError(
            f"config file {str(path)!r} must hold a mapping at the top level, "
            f"got {type(document).__name__}"
        )
    return document


_CREDENTIALS_DIR = PurePosixPath("/run/credentials")
_STRICT = ConfigDict(extra="forbid", frozen=True)

Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
PluginName = Annotated[str, StringConstraints(pattern=PLUGIN_NAME_PATTERN)]
GrantKey = Annotated[str, StringConstraints(pattern=GRANT_KEY_PATTERN)]
PinnedImage = Annotated[
    str, StringConstraints(pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$")
]
Domain = Annotated[
    str,
    StringConstraints(
        max_length=253,
        pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
    ),
]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")]


def _parse_timeout(value: object) -> timedelta:
    """Return the timedelta a timeout string names.

    Raises:
        ValueError: value is not a string.
        DurationError: value is a string parse_duration refuses.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"timeout must be a duration string such as '120s', got {value!r}"
        )
    return parse_duration(value)


Timeout = Annotated[timedelta | None, BeforeValidator(_parse_timeout)]


class Root(BaseModel):
    model_config = _STRICT

    path: str
    mode: Literal["ro", "rw"]


class EnvMount(BaseModel):
    model_config = _STRICT

    env: EnvName


class FileMount(BaseModel):
    model_config = _STRICT

    file: str


class Grant(BaseModel):
    model_config = _STRICT

    roots: list[Root]
    domains: list[Domain]
    credentials: dict[Name, EnvMount | FileMount]
    timeout: Timeout = None


class Scope(BaseModel):
    model_config = _STRICT

    grants: dict[GrantKey, Grant]


class _RuleFailuresError(ValueError):
    """Every rule a field-valid config breaks, carried out of pydantic as one error."""

    def __init__(self, failures: tuple[str, ...]) -> None:
        """Hold one message per broken rule."""
        self.failures = failures
        super().__init__("\n".join(failures))


def _is_clean_absolute(path: PurePosixPath) -> bool:
    return path.is_absolute() and ".." not in path.parts


def _agent_tree_failures(agent_tree: str) -> Iterator[str]:
    if not _is_clean_absolute(PurePosixPath(agent_tree)):
        yield (
            "agent_tree must be an absolute path with no '..' segment, "
            f"got {agent_tree!r}"
        )


def _root_failures(where: str, root: Root, agent_tree: str) -> Iterator[str]:
    path = PurePosixPath(root.path)
    tree = PurePosixPath(agent_tree)
    if not _is_clean_absolute(path):
        yield (
            f"{where}.path must be an absolute path with no '..' segment, "
            f"got {root.path!r}"
        )
    elif path != tree and tree not in path.parents:
        yield (
            f"{where}.path must lie inside agent_tree {agent_tree!r}, got {root.path!r}"
        )


def _mount_failures(
    where: str, credentials: Mapping[str, EnvMount | FileMount]
) -> Iterator[str]:
    env_owners: dict[str, str] = {}
    file_owners: dict[PurePosixPath, str] = {}
    for name, mount in credentials.items():
        if isinstance(mount, EnvMount):
            owner = env_owners.setdefault(mount.env, name)
            if owner != name:
                yield f"{where}: {owner!r} and {name!r} share env {mount.env!r}"
            continue
        path = PurePosixPath(mount.file)
        if not _is_clean_absolute(path) or _CREDENTIALS_DIR not in path.parents:
            yield (
                f"{where}.{name}.file must be an absolute path beneath "
                f"'/run/credentials/' with no '..' segment, got {mount.file!r}"
            )
        owner = file_owners.setdefault(path, name)
        if owner != name:
            yield f"{where}: {owner!r} and {name!r} share file {mount.file!r}"


def _unknown_credential_failures(
    where: str, credentials: Mapping[str, EnvMount | FileMount], known: Set[str]
) -> Iterator[str]:
    for name in credentials:
        if name not in known:
            yield f"{where}: credential {name!r} is not in the credential store"


class InstanceConfig(BaseModel):
    model_config = _STRICT

    agent_tree: str
    plugins: dict[PluginName, PinnedImage]
    scopes: dict[Name, Scope]

    @model_validator(mode="after")
    def _check_rules(self, info: ValidationInfo) -> Self:
        """Refuse a field-valid config that breaks a rule across its fields.

        Raises:
            _RuleFailuresError: one or more rules fail; it holds every failure.
        """
        failures = tuple(self._rule_failures(info.context["credential_names"]))
        if failures:
            raise _RuleFailuresError(failures)
        return self

    def _rule_failures(self, credential_names: Set[str]) -> Iterator[str]:
        yield from _agent_tree_failures(self.agent_tree)
        for scope_name, scope in self.scopes.items():
            for key, grant in scope.grants.items():
                yield from self._grant_failures(
                    f"scopes.{scope_name}.grants.{key}", key, grant, credential_names
                )

    def _grant_failures(
        self, where: str, key: str, grant: Grant, credential_names: Set[str]
    ) -> Iterator[str]:
        plugin = key.split(".", 1)[0]
        if plugin not in self.plugins:
            yield f"{where}: plugin {plugin!r} is not in plugins"
        for index, root in enumerate(grant.roots):
            yield from _root_failures(f"{where}.roots.{index}", root, self.agent_tree)
        yield from _mount_failures(f"{where}.credentials", grant.credentials)
        yield from _unknown_credential_failures(
            f"{where}.credentials", grant.credentials, credential_names
        )


def _error_messages(error: ErrorDetails) -> list[str]:
    """Return the messages one pydantic error stands for, without the file."""
    cause = error.get("ctx", {}).get("error")
    if isinstance(cause, _RuleFailuresError):
        return list(cause.failures)
    location = ".".join(str(part) for part in error["loc"])
    # A missing field's input is its whole parent mapping, which names nothing.
    if error["type"] == "missing":
        return [f"{location}: {error['msg']}"]
    # A validator's own message already names the value it refused.
    if isinstance(cause, ValueError):
        return [f"{location}: {cause}"]
    return [f"{location}: {error['msg']}, got {error['input']!r}"]


def load_config(path: Path, credential_names: Set[str]) -> InstanceConfig:
    """Return the config held in the YAML file at path.

    Args:
        path: the config file.
        credential_names: the names the credential store holds; every
            credential a grant uses must be one of them.

    Raises:
        ConfigError: the file cannot be read or parsed, or its config breaks a
            field constraint or a rule. It holds one message per failure.
    """
    document = read_yaml(path)
    try:
        return InstanceConfig.model_validate(
            document, context={"credential_names": credential_names}
        )
    except ValidationError as exc:
        source = f"config file {str(path)!r}"
        raise ConfigError(
            *(
                f"{source}: {message}"
                for error in exc.errors()
                for message in _error_messages(error)
            )
        ) from exc
