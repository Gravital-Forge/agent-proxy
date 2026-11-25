"""The rules every proxy module shares, and the instance config they govern."""

import re
from collections import Counter
from collections.abc import Hashable, Iterator, Mapping, Sequence
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
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
ENV_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]*$"
PINNED_IMAGE_PATTERN = r"^[^@\s]+@sha256:[0-9a-f]{64}$"
DOMAIN_PATTERN = (
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
PROXY_ENV_NAME = "HTTPS_PROXY"
ROOTS_ENV_NAME = "AGENT_PROXY_ROOTS"
MEMORY_PATTERN = r"^[1-9][0-9]*[mg]$"
# A cage's /tmp is a 64 MiB tmpfs, whose pages count against the cage's memory.
MEMORY_FLOOR_BYTES = 64 * 2**20

_DURATION_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}
_MEMORY_UNITS = {"m": 2**20, "g": 2**30}

_RESERVED_ENV_SETTERS = {
    PROXY_ENV_NAME: "the egress proxy",
    ROOTS_ENV_NAME: "the core",
}
_RESERVED_PLUGIN_NAME = "agent-proxy"


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
PinnedImage = Annotated[str, StringConstraints(pattern=PINNED_IMAGE_PATTERN)]
Domain = Annotated[str, StringConstraints(max_length=253, pattern=DOMAIN_PATTERN)]
EnvName = Annotated[str, StringConstraints(pattern=ENV_NAME_PATTERN)]


def _parse_duration_field(value: object, info: ValidationInfo) -> timedelta:
    """Return the timedelta the duration string of a field names.

    Raises:
        ValueError: value is not a string; the message names the field.
        DurationError: value is a string parse_duration refuses.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"{info.field_name} must be a duration string such as '120s', got {value!r}"
        )
    return parse_duration(value)


Duration = Annotated[timedelta, BeforeValidator(_parse_duration_field)]
OptionalDuration = Annotated[timedelta | None, BeforeValidator(_parse_duration_field)]


def _parse_memory(value: object) -> int:
    """Return the bytes a memory string such as '8g' names.

    Raises:
        ValueError: value is not a string of MEMORY_PATTERN, or names less
            than MEMORY_FLOOR_BYTES.
    """
    refusal = ValueError(
        f"memory must match {MEMORY_PATTERN!r}, a count of MiB or GiB such as "
        f"'8g', and be at least {MEMORY_FLOOR_BYTES // 2**20}m, "
        f"got {value!r}"
    )
    if not isinstance(value, str) or re.fullmatch(MEMORY_PATTERN, value) is None:
        raise refusal
    try:
        memory_bytes = int(value[:-1]) * _MEMORY_UNITS[value[-1]]
    # int() raises ValueError past the interpreter's digit limit for str conversion.
    except ValueError:
        raise refusal from None
    if memory_bytes < MEMORY_FLOOR_BYTES:
        raise refusal
    return memory_bytes


Memory = Annotated[int | None, BeforeValidator(_parse_memory)]


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
    timeout: OptionalDuration = None
    memory: Memory = None
    env: dict[EnvName, str] = Field(default_factory=dict)


class Scope(BaseModel):
    model_config = _STRICT

    grants: dict[GrantKey, Grant]


class ProvidedCredential(BaseModel):
    """A credential whose value a provider action gives, and the provider's inputs."""

    model_config = _STRICT

    provider: GrantKey
    credentials: dict[Name, EnvMount | FileMount]
    env: dict[EnvName, str] = Field(default_factory=dict)
    domains: list[Domain]
    refresh_before: Duration
    max_age: OptionalDuration = None
    timeout: OptionalDuration = None


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


def _inside_tree_failures(field: str, path_text: str, agent_tree: str) -> Iterator[str]:
    path = PurePosixPath(path_text)
    tree = PurePosixPath(agent_tree)
    if not _is_clean_absolute(path):
        yield (
            f"{field} must be an absolute path with no '..' segment, got {path_text!r}"
        )
    elif path != tree and tree not in path.parents:
        yield f"{field} must lie inside agent_tree {agent_tree!r}, got {path_text!r}"


def _overflow_dir_failures(overflow_dir: str, agent_tree: str) -> Iterator[str]:
    if PurePosixPath(overflow_dir) == PurePosixPath(agent_tree):
        yield (
            f"overflow_dir must differ from agent_tree {agent_tree!r}, "
            f"got {overflow_dir!r}"
        )
    else:
        yield from _inside_tree_failures("overflow_dir", overflow_dir, agent_tree)


def _overflow_overlap_failures(
    field: str, path_text: str, overflow_dir: str
) -> Iterator[str]:
    path = PurePosixPath(path_text)
    overflow = PurePosixPath(overflow_dir)
    # A path with a '..' segment is already refused, and its text says nothing
    # about where it resolves.
    if not (_is_clean_absolute(path) and _is_clean_absolute(overflow)):
        return
    if path == overflow:
        yield f"{field} must not equal overflow_dir {overflow_dir!r}, got {path_text!r}"
    elif path in overflow.parents:
        yield (
            f"{field} must not contain overflow_dir {overflow_dir!r}, got {path_text!r}"
        )
    elif overflow in path.parents:
        yield (
            f"{field} must not lie inside overflow_dir {overflow_dir!r}, "
            f"got {path_text!r}"
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


def _duplicate_domain_failures(where: str, domains: Sequence[str]) -> Iterator[str]:
    for domain, count in Counter(domains).items():
        if count > 1:
            yield f"{where}: {domain!r} is named more than once"


def _reserved_env_failures(
    where: str, credentials: Mapping[str, EnvMount | FileMount]
) -> Iterator[str]:
    for name, mount in credentials.items():
        if isinstance(mount, EnvMount) and mount.env in _RESERVED_ENV_SETTERS:
            yield (
                f"{where}.{name}.env must not be {mount.env!r}, "
                f"which {_RESERVED_ENV_SETTERS[mount.env]} sets"
            )


def _grant_env_failures(
    where: str,
    env: Mapping[str, str],
    credentials: Mapping[str, EnvMount | FileMount],
) -> Iterator[str]:
    credential_envs = {
        mount.env: name
        for name, mount in credentials.items()
        if isinstance(mount, EnvMount)
    }
    for env_name, value in env.items():
        if env_name in _RESERVED_ENV_SETTERS:
            yield (
                f"{where}.env must not name {env_name!r}, "
                f"which {_RESERVED_ENV_SETTERS[env_name]} sets"
            )
        if env_name in credential_envs:
            yield (
                f"{where}.env must not name {env_name!r}, "
                f"which credential {credential_envs[env_name]!r} takes"
            )
        if "\0" in value:
            yield f"{where}.env.{env_name} must not hold a NUL"


class InstanceConfig(BaseModel):
    model_config = _STRICT

    agent_tree: str
    overflow_dir: str
    max_cages: Annotated[StrictInt, Field(ge=1)]
    plugins: dict[PluginName, PinnedImage]
    scopes: dict[Name, Scope]
    provided_credentials: dict[Name, ProvidedCredential] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        """Refuse a field-valid config that breaks a rule across its fields.

        Raises:
            _RuleFailuresError: one or more rules fail; it holds every failure.
        """
        failures = tuple(self._rule_failures())
        if failures:
            raise _RuleFailuresError(failures)
        return self

    def _rule_failures(self) -> Iterator[str]:
        yield from _agent_tree_failures(self.agent_tree)
        yield from _overflow_dir_failures(self.overflow_dir, self.agent_tree)
        if _RESERVED_PLUGIN_NAME in self.plugins:
            yield (
                f"plugins.{_RESERVED_PLUGIN_NAME}: the plugin name "
                f"{_RESERVED_PLUGIN_NAME!r} is reserved, "
                "because the tool 'agent-proxy_self_check' takes it"
            )
        for scope_name, scope in self.scopes.items():
            for key, grant in scope.grants.items():
                yield from self._grant_failures(
                    f"scopes.{scope_name}.grants.{key}", key, grant
                )
        for name, entry in self.provided_credentials.items():
            yield from self._provided_failures(f"provided_credentials.{name}", entry)

    def _grant_failures(self, where: str, key: str, grant: Grant) -> Iterator[str]:
        plugin = key.split(".", 1)[0]
        if plugin not in self.plugins:
            yield f"{where}: plugin {plugin!r} is not in plugins"
        for index, root in enumerate(grant.roots):
            field = f"{where}.roots.{index}.path"
            yield from _inside_tree_failures(field, root.path, self.agent_tree)
            yield from _overflow_overlap_failures(field, root.path, self.overflow_dir)
        yield from _duplicate_domain_failures(f"{where}.domains", grant.domains)
        yield from _mount_failures(f"{where}.credentials", grant.credentials)
        yield from _reserved_env_failures(f"{where}.credentials", grant.credentials)
        yield from _grant_env_failures(where, grant.env, grant.credentials)
        for name, mount in grant.credentials.items():
            if name in self.provided_credentials and isinstance(mount, FileMount):
                yield (
                    f"{where}.credentials.{name}: provided credential {name!r} "
                    f"must reach a cage as env, got file {mount.file!r}"
                )

    def _provided_failures(
        self, where: str, entry: ProvidedCredential
    ) -> Iterator[str]:
        plugin = entry.provider.split(".", 1)[0]
        if plugin not in self.plugins:
            yield f"{where}.provider: plugin {plugin!r} is not in plugins"
        for name in entry.credentials:
            if name in self.provided_credentials:
                yield (
                    f"{where}.credentials.{name}: a provider takes only stored "
                    f"credentials, got provided credential {name!r}"
                )
        yield from _duplicate_domain_failures(f"{where}.domains", entry.domains)
        yield from _mount_failures(f"{where}.credentials", entry.credentials)
        yield from _reserved_env_failures(f"{where}.credentials", entry.credentials)
        yield from _grant_env_failures(where, entry.env, entry.credentials)
        if entry.max_age is not None and entry.max_age <= entry.refresh_before:
            yield (
                f"{where}.max_age must be longer than refresh_before "
                f"{entry.refresh_before}, got {entry.max_age}"
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


def load_config(path: Path) -> InstanceConfig:
    """Return the config held in the YAML file at path.

    Raises:
        ConfigError: the file cannot be read or parsed, or its config breaks a
            field constraint or a rule. It holds one message per failure.
    """
    document = read_yaml(path)
    try:
        return InstanceConfig.model_validate(document)
    except ValidationError as exc:
        source = f"config file {str(path)!r}"
        raise ConfigError(
            *(
                f"{source}: {message}"
                for error in exc.errors()
                for message in _error_messages(error)
            )
        ) from exc
