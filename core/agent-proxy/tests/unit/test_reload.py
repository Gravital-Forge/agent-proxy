import dataclasses
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from agent_proxy.config import ConfigError, Grant, load_config
from agent_proxy.plugins import (
    CommandAction,
    Manifest,
    ManifestError,
    PluginImageError,
)
from agent_proxy.reload import LiveConfig, Snapshot, build_snapshot

AGENT_TREE = "/srv/agents/alpha"
GIT_IMAGE = "ghcr.io/example/git@sha256:" + "a" * 64
FS_IMAGE = "ghcr.io/example/fs@sha256:" + "b" * 64
CREDENTIAL_NAMES = frozenset({"gh-token"})
WAIT_SECONDS = 5


def _action(name: str, *, passthrough_args: bool = False) -> CommandAction:
    return CommandAction(
        name=name,
        input_schema={"type": "object", "additionalProperties": False},
        credentials=(),
        timeout=timedelta(seconds=30),
        argv=("git", name),
        parameters=(),
        passthrough_args=passthrough_args,
    )


GIT_MANIFEST = Manifest(actions={"clone": _action("clone"), "push": _action("push")})
FS_MANIFEST = Manifest(actions={"read": _action("read")})


def _grant(timeout: str = "120s") -> dict[str, object]:
    return {"roots": [], "domains": [], "credentials": {}, "timeout": timeout}


def _config(grants: dict[str, object]) -> dict[str, object]:
    return {
        "agent_tree": AGENT_TREE,
        "plugins": {"git": GIT_IMAGE, "fs": FS_IMAGE},
        "scopes": {"dev": {"grants": grants}},
    }


def _write(tmp_path: Path, document: object, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document))
    return path


def _source(path: Path) -> str:
    return f"config file {str(path)!r}"


def _loader(results: dict[str, Manifest | Exception]) -> Callable[[str], Manifest]:
    """Return a load_manifest stub that returns or raises the result for a reference."""

    def load_manifest(reference: str) -> Manifest:
        result = results[reference]
        if isinstance(result, Exception):
            raise result
        return result

    return load_manifest


LOAD_BOTH = _loader({GIT_IMAGE: GIT_MANIFEST, FS_IMAGE: FS_MANIFEST})
GIT_PULL_FAILURE = PluginImageError(f"image {GIT_IMAGE!r} could not be pulled: denied")


def _failures(path: Path, load_manifest: Callable[[str], Manifest]) -> tuple[str, ...]:
    with pytest.raises(ConfigError) as caught:
        build_snapshot(path, CREDENTIAL_NAMES, load_manifest)
    return caught.value.errors


def _snapshot(tmp_path: Path, timeout: str, name: str) -> Snapshot:
    path = _write(tmp_path, _config({"git.clone": _grant(timeout)}), name)
    return build_snapshot(path, CREDENTIAL_NAMES, LOAD_BOTH)


def _clone_grant(snapshot: Snapshot) -> Grant:
    return snapshot.config.scopes["dev"].grants["git.clone"]


def test_build_snapshot_holds_the_config_and_each_plugin_manifest(
    tmp_path: Path,
) -> None:
    """The snapshot carries load_config's result and one manifest per plugin."""
    path = _write(tmp_path, _config({"git.clone": _grant(), "fs.read": _grant()}))

    snapshot = build_snapshot(path, CREDENTIAL_NAMES, LOAD_BOTH)

    assert snapshot.config == load_config(path, CREDENTIAL_NAMES)
    assert dict(snapshot.manifests) == {"git": GIT_MANIFEST, "fs": FS_MANIFEST}
    assert snapshot.warnings == ()


def test_build_snapshot_loads_each_plugin_by_its_pinned_reference(
    tmp_path: Path,
) -> None:
    """The loader is called once with each image reference the config pins."""
    path = _write(tmp_path, _config({"git.clone": _grant()}))
    references: list[str] = []

    def load_manifest(reference: str) -> Manifest:
        references.append(reference)
        return LOAD_BOTH(reference)

    build_snapshot(path, CREDENTIAL_NAMES, load_manifest)

    assert sorted(references) == sorted([GIT_IMAGE, FS_IMAGE])


def test_a_snapshot_cannot_be_changed(tmp_path: Path) -> None:
    """Snapshot is frozen, and its manifests mapping is read-only."""
    snapshot = build_snapshot(
        _write(tmp_path, _config({"git.clone": _grant()})),
        CREDENTIAL_NAMES,
        LOAD_BOTH,
    )

    assert isinstance(snapshot.manifests, MappingProxyType)
    with pytest.raises(TypeError):
        snapshot.manifests["git"] = FS_MANIFEST
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.warnings = ("changed",)


def test_a_grant_naming_an_undeclared_action_is_refused(tmp_path: Path) -> None:
    """The message names the grant, the action, and the plugin."""
    path = _write(tmp_path, _config({"git.rebase": _grant()}))

    assert _failures(path, LOAD_BOTH) == (
        f"{_source(path)}: scopes.dev.grants.git.rebase: "
        "action 'rebase' is not declared by plugin 'git'",
    )


def test_a_plugin_whose_loader_raises_is_refused(tmp_path: Path) -> None:
    """The loader's message is reported under plugins.<name>."""
    path = _write(tmp_path, _config({"fs.read": _grant()}))
    load_manifest = _loader({GIT_IMAGE: GIT_PULL_FAILURE, FS_IMAGE: FS_MANIFEST})

    assert _failures(path, load_manifest) == (
        f"{_source(path)}: plugins.git: {GIT_PULL_FAILURE}",
    )


def test_the_grants_of_a_plugin_that_failed_to_load_are_not_reported(
    tmp_path: Path,
) -> None:
    """With no manifest to check against, only the plugin's own failure is reported."""
    path = _write(tmp_path, _config({"git.rebase": _grant()}))
    load_manifest = _loader({GIT_IMAGE: GIT_PULL_FAILURE, FS_IMAGE: FS_MANIFEST})

    assert _failures(path, load_manifest) == (
        f"{_source(path)}: plugins.git: {GIT_PULL_FAILURE}",
    )


def test_each_line_of_a_manifest_error_is_one_failure(tmp_path: Path) -> None:
    """Every manifest failure is reported under plugins.<name> on its own line."""
    path = _write(tmp_path, _config({"fs.read": _grant()}))
    manifest_error = ManifestError("$.actions: first", "$.actions.clone: second")
    load_manifest = _loader({GIT_IMAGE: manifest_error, FS_IMAGE: FS_MANIFEST})

    assert _failures(path, load_manifest) == (
        f"{_source(path)}: plugins.git: $.actions: first",
        f"{_source(path)}: plugins.git: $.actions.clone: second",
    )


def test_every_failure_appears_in_one_config_error(tmp_path: Path) -> None:
    """A failed plugin and another plugin's undeclared action are reported together."""
    path = _write(
        tmp_path,
        _config({"git.clone": _grant(), "git.rebase": _grant(), "fs.read": _grant()}),
    )
    fs_failure = PluginImageError(f"image {FS_IMAGE!r} is not present")
    load_manifest = _loader({GIT_IMAGE: GIT_MANIFEST, FS_IMAGE: fs_failure})

    assert sorted(_failures(path, load_manifest)) == sorted(
        [
            f"{_source(path)}: plugins.fs: {fs_failure}",
            f"{_source(path)}: scopes.dev.grants.git.rebase: "
            "action 'rebase' is not declared by plugin 'git'",
        ]
    )


def test_a_load_config_error_propagates_with_no_plugin_loaded(tmp_path: Path) -> None:
    """load_config's own failures come out as they are, and no loader call is made."""
    path = _write(
        tmp_path, _config({"git.clone": _grant()}) | {"agent_tree": "relative"}
    )
    with pytest.raises(ConfigError) as expected:
        load_config(path, CREDENTIAL_NAMES)
    references: list[str] = []

    def load_manifest(reference: str) -> Manifest:
        references.append(reference)
        return LOAD_BOTH(reference)

    assert _failures(path, load_manifest) == expected.value.errors
    assert references == []


def test_a_manifest_with_passthrough_args_yields_its_warning(tmp_path: Path) -> None:
    """Each pass-through action warns under plugins.<name>., granted or not."""
    path = _write(tmp_path, _config({"git.clone": _grant()}))
    git_manifest = Manifest(
        actions={
            "clone": _action("clone"),
            "run": _action("run", passthrough_args=True),
        }
    )
    load_manifest = _loader({GIT_IMAGE: git_manifest, FS_IMAGE: FS_MANIFEST})

    snapshot = build_snapshot(path, CREDENTIAL_NAMES, load_manifest)

    assert snapshot.warnings == ("plugins.git.actions.run: declares pass-through args",)


def test_current_is_the_snapshot_live_config_was_given(tmp_path: Path) -> None:
    """A new LiveConfig puts its snapshot in force."""
    snapshot = _snapshot(tmp_path, "60s", "old.yaml")

    assert LiveConfig(snapshot).current is snapshot


def test_reload_puts_the_built_snapshot_in_force(tmp_path: Path) -> None:
    """Reload returns the snapshot it built, and current then returns it."""
    live = LiveConfig(_snapshot(tmp_path, "60s", "old.yaml"))
    new = _snapshot(tmp_path, "120s", "new.yaml")

    assert live.reload(lambda: new) is new
    assert live.current is new


def test_a_reload_of_an_invalid_file_raises_and_keeps_current(tmp_path: Path) -> None:
    """A failed build leaves the snapshot before it in force."""
    old = _snapshot(tmp_path, "60s", "old.yaml")
    live = LiveConfig(old)
    invalid = _write(tmp_path, _config({"git.rebase": _grant()}), "invalid.yaml")

    with pytest.raises(ConfigError):
        live.reload(
            functools.partial(build_snapshot, invalid, CREDENTIAL_NAMES, LOAD_BOTH)
        )

    assert live.current is old


def test_a_call_keeps_the_snapshot_it_started_with(tmp_path: Path) -> None:
    """A call in flight during a reload uses the old grant; a later call, the new."""
    old = _snapshot(tmp_path, "60s", "old.yaml")
    new = _snapshot(tmp_path, "120s", "new.yaml")
    live = LiveConfig(old)
    taken = threading.Event()
    reloaded = threading.Event()

    def call() -> Grant:
        snapshot = live.current
        taken.set()
        reloaded.wait(WAIT_SECONDS)
        return _clone_grant(snapshot)

    with ThreadPoolExecutor(max_workers=1) as pool:
        before = pool.submit(call)
        assert taken.wait(WAIT_SECONDS)
        live.reload(lambda: new)
        reloaded.set()
        after = pool.submit(call)

    assert _clone_grant(old) != _clone_grant(new)
    assert before.result() == _clone_grant(old)
    assert after.result() == _clone_grant(new)


def test_reloads_run_one_at_a_time(tmp_path: Path) -> None:
    """A second reload does not start its build until the first reload returns."""
    old = _snapshot(tmp_path, "60s", "old.yaml")
    new = _snapshot(tmp_path, "120s", "new.yaml")
    live = LiveConfig(old)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def first_build() -> Snapshot:
        first_started.set()
        release_first.wait(WAIT_SECONDS)
        return old

    def second_build() -> Snapshot:
        second_started.set()
        return new

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(live.reload, first_build)
        assert first_started.wait(WAIT_SECONDS)
        second = pool.submit(live.reload, second_build)
        assert not second_started.wait(0.2)
        release_first.set()

    assert first.result() is old
    assert second.result() is new
    assert live.current is new
