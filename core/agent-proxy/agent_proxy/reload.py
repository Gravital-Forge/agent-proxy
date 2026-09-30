"""Snapshots of the config in force, and the reference a reload swaps."""

import threading
from collections.abc import Callable, Iterator, Mapping, Set
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from agent_proxy.config import ConfigError, InstanceConfig, load_config
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import Manifest


@dataclass(frozen=True)
class Snapshot:
    """A config, the manifest of each of its plugins, and their warnings."""

    config: InstanceConfig
    manifests: MappingProxyType[str, Manifest]
    warnings: tuple[str, ...]


def build_snapshot(
    path: Path,
    credential_names: Set[str],
    load_manifest: Callable[[str], Manifest],
) -> Snapshot:
    """Return the snapshot of the config file at path.

    Args:
        path: the config file.
        credential_names: the names the credential store holds.
        load_manifest: returns the manifest of the image at a pinned
            reference, making the image present first.

    Raises:
        ConfigError: load_config refuses the file, and its error propagates
            unchanged with no plugin loaded; or a plugin fails to load, or a
            grant names an action its plugin's manifest does not declare. It
            holds one message per failure.
    """
    config = load_config(path, credential_names)
    manifests, failures = _load_manifests(config, load_manifest)
    failures.extend(_undeclared_action_failures(config, manifests))
    if failures:
        source = f"config file {str(path)!r}"
        raise ConfigError(*(f"{source}: {failure}" for failure in failures))
    return Snapshot(
        config=config,
        manifests=MappingProxyType(manifests),
        warnings=tuple(_warnings(manifests)),
    )


def _load_manifests(
    config: InstanceConfig, load_manifest: Callable[[str], Manifest]
) -> tuple[dict[str, Manifest], list[str]]:
    """Return each loaded plugin's manifest, and one message per failure line."""
    manifests: dict[str, Manifest] = {}
    failures: list[str] = []
    for plugin, image in config.plugins.items():
        try:
            manifests[plugin] = load_manifest(image)
        except ProxyError as exc:
            failures.extend(
                f"plugins.{plugin}: {line}" for line in str(exc).splitlines()
            )
    return manifests, failures


def _undeclared_action_failures(
    config: InstanceConfig, manifests: Mapping[str, Manifest]
) -> Iterator[str]:
    for scope_name, scope in config.scopes.items():
        for key in scope.grants:
            plugin, action = key.split(".", 1)
            # A plugin that failed to load is already reported, with no
            # manifest to check its grants against.
            if plugin in manifests and action not in manifests[plugin].actions:
                yield (
                    f"scopes.{scope_name}.grants.{key}: action {action!r} "
                    f"is not declared by plugin {plugin!r}"
                )


def _warnings(manifests: Mapping[str, Manifest]) -> Iterator[str]:
    for plugin, manifest in manifests.items():
        for warning in manifest.warnings():
            yield f"plugins.{plugin}.{warning}"


class LiveConfig:
    """The snapshot in force, which a reload replaces whole."""

    def __init__(self, snapshot: Snapshot) -> None:
        """Put snapshot in force."""
        self._current = snapshot
        self._reload_lock = threading.Lock()

    @property
    def current(self) -> Snapshot:
        """The snapshot in force.

        A call reads this once when it starts and uses that snapshot to its end.
        """
        return self._current

    def reload(self, build: Callable[[], Snapshot]) -> Snapshot:
        """Build a snapshot and put it in force, one reload at a time.

        Returns:
            The snapshot now in force.

        Raises:
            ConfigError: build refuses the config file; the snapshot in force
                is unchanged.
        """
        with self._reload_lock:
            snapshot = build()
            self._current = snapshot
        return snapshot
