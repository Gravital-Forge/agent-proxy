import dataclasses
import functools
import threading
from collections.abc import Callable, Collection, Iterable
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from agent_proxy.config import ConfigError, Grant, load_config
from agent_proxy.egress import EgressError
from agent_proxy.errors import ProxyError
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
OLD_DOMAINS = ("example.com",)
NEW_DOMAINS = ("example.org",)


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
        "overflow_dir": f"{AGENT_TREE}/overflow",
        "max_cages": 4,
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


def _egress_snapshot(tmp_path: Path, domains: tuple[str, ...], name: str) -> Snapshot:
    grant = _grant() | {"domains": list(domains)}
    path = _write(tmp_path, _config({"git.clone": grant}), name)
    return build_snapshot(path, CREDENTIAL_NAMES, LOAD_BOTH)


class RecordingEgress:
    """Records each ensure and prune call with the lists it was given."""

    def __init__(self, ensure_error: ProxyError | None = None) -> None:
        """Start with no calls; ensure raises ensure_error after recording, if set."""
        self.calls: list[tuple[str, set[tuple[str, ...]]]] = []
        self._ensure_error = ensure_error

    def ensure(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists, then raise ensure_error if one was given."""
        self.calls.append(("ensure", {tuple(domains) for domains in lists}))
        if self._ensure_error is not None:
            raise self._ensure_error

    def prune(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists."""
        self.calls.append(("prune", {tuple(domains) for domains in lists}))


class _CallFailedError(Exception):
    """Raised by a test's call while it holds a snapshot."""


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

    assert LiveConfig(snapshot, RecordingEgress()).current is snapshot


def test_reload_puts_the_built_snapshot_in_force(tmp_path: Path) -> None:
    """Reload returns the snapshot it built, and current then returns it."""
    live = LiveConfig(_snapshot(tmp_path, "60s", "old.yaml"), RecordingEgress())
    new = _snapshot(tmp_path, "120s", "new.yaml")

    assert live.reload(lambda: new) is new
    assert live.current is new


def test_a_call_keeps_the_snapshot_it_started_with(tmp_path: Path) -> None:
    """A call in flight during a reload uses the old grant; a later call, the new."""
    old = _snapshot(tmp_path, "60s", "old.yaml")
    new = _snapshot(tmp_path, "120s", "new.yaml")
    live = LiveConfig(old, RecordingEgress())
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
    live = LiveConfig(old, RecordingEgress())
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


def test_the_constructor_neither_ensures_nor_prunes(tmp_path: Path) -> None:
    """A new LiveConfig leaves egress to start and reload."""
    egress = RecordingEgress()

    LiveConfig(_snapshot(tmp_path, "60s", "old.yaml"), egress)

    assert egress.calls == []


def test_start_ensures_then_prunes_the_first_snapshots_lists(tmp_path: Path) -> None:
    """Start puts the built snapshot in force after ensuring and pruning its lists."""
    first = _egress_snapshot(tmp_path, OLD_DOMAINS, "first.yaml")
    egress = RecordingEgress()

    live = LiveConfig.start(lambda: first, egress)

    assert live.current is first
    assert egress.calls == [("ensure", {OLD_DOMAINS}), ("prune", {OLD_DOMAINS})]


def test_start_propagates_a_failed_ensure(tmp_path: Path) -> None:
    """An ensure that raises stops start with its error."""
    first = _egress_snapshot(tmp_path, OLD_DOMAINS, "first.yaml")
    egress = RecordingEgress(ensure_error=EgressError("network refused"))

    with pytest.raises(EgressError, match="network refused"):
        LiveConfig.start(lambda: first, egress)


def test_reload_ensures_the_new_lists_before_current_changes(tmp_path: Path) -> None:
    """Inside ensure, current is still the old snapshot; the prune follows the swap."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    new = _egress_snapshot(tmp_path, NEW_DOMAINS, "new.yaml")
    in_force_during_ensure: list[Snapshot] = []

    class ObservingEgress(RecordingEgress):
        def ensure(self, lists: Iterable[Collection[str]]) -> None:
            """Note the snapshot in force, then record the call."""
            in_force_during_ensure.append(live.current)
            super().ensure(lists)

    egress = ObservingEgress()
    live = LiveConfig(old, egress)

    live.reload(lambda: new)

    (seen,) = in_force_during_ensure
    assert seen is old
    assert live.current is new
    assert egress.calls == [("ensure", {NEW_DOMAINS}), ("prune", {NEW_DOMAINS})]


def test_a_failed_build_keeps_current_and_still_prunes(tmp_path: Path) -> None:
    """A refused build ensures nothing, and the prune keeps the lists in force."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    egress = RecordingEgress()
    live = LiveConfig(old, egress)
    invalid = _write(tmp_path, _config({"git.rebase": _grant()}), "invalid.yaml")

    with pytest.raises(ConfigError):
        live.reload(
            functools.partial(build_snapshot, invalid, CREDENTIAL_NAMES, LOAD_BOTH)
        )

    assert live.current is old
    assert egress.calls == [("prune", {OLD_DOMAINS})]


def test_a_failed_ensure_keeps_current_and_still_prunes(tmp_path: Path) -> None:
    """An ensure that raises refuses the reload; the prune drops the refused lists."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    new = _egress_snapshot(tmp_path, NEW_DOMAINS, "new.yaml")
    egress = RecordingEgress(ensure_error=EgressError("proxy did not start"))
    live = LiveConfig(old, egress)

    with pytest.raises(EgressError, match="proxy did not start"):
        live.reload(lambda: new)

    assert live.current is old
    assert egress.calls == [("ensure", {NEW_DOMAINS}), ("prune", {OLD_DOMAINS})]


def test_the_prune_after_a_swap_keeps_a_list_only_a_snapshot_in_use_names(
    tmp_path: Path,
) -> None:
    """A call holding the old snapshot keeps its list through the reload's prune."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    new = _egress_snapshot(tmp_path, NEW_DOMAINS, "new.yaml")
    egress = RecordingEgress()
    live = LiveConfig(old, egress)

    with live.use() as held:
        live.reload(lambda: new)

        assert held is old
        assert egress.calls == [
            ("ensure", {NEW_DOMAINS}),
            ("prune", {OLD_DOMAINS, NEW_DOMAINS}),
        ]


def test_the_last_use_of_a_retired_snapshot_prunes_its_list(tmp_path: Path) -> None:
    """The list stays until its snapshot's last use exits, then a prune drops it."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    new = _egress_snapshot(tmp_path, NEW_DOMAINS, "new.yaml")
    egress = RecordingEgress()
    live = LiveConfig(old, egress)
    after_reload = [("ensure", {NEW_DOMAINS}), ("prune", {OLD_DOMAINS, NEW_DOMAINS})]

    with live.use():
        with live.use():
            live.reload(lambda: new)
        assert egress.calls == after_reload

    assert egress.calls == [*after_reload, ("prune", {NEW_DOMAINS})]


def test_a_use_of_the_snapshot_in_force_never_prunes_on_exit(tmp_path: Path) -> None:
    """Leaving a use of the snapshot in force makes no egress call."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    egress = RecordingEgress()
    live = LiveConfig(old, egress)

    with live.use() as held:
        assert held is old

    assert egress.calls == []


def test_use_releases_its_snapshot_when_the_block_raises(tmp_path: Path) -> None:
    """A block that raises still ends its use, so its retired list is pruned."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    new = _egress_snapshot(tmp_path, NEW_DOMAINS, "new.yaml")
    egress = RecordingEgress()
    live = LiveConfig(old, egress)

    def failing_call() -> None:
        with live.use():
            live.reload(lambda: new)
            raise _CallFailedError

    with pytest.raises(_CallFailedError):
        failing_call()

    assert egress.calls[-1] == ("prune", {NEW_DOMAINS})


def test_a_retired_use_waits_for_a_reload_in_flight_then_prunes(
    tmp_path: Path,
) -> None:
    """The last use's exit blocks on a running reload rather than skip its prune."""
    old = _egress_snapshot(tmp_path, OLD_DOMAINS, "old.yaml")
    new = _egress_snapshot(tmp_path, NEW_DOMAINS, "new.yaml")
    egress = RecordingEgress()
    live = LiveConfig(old, egress)
    held = threading.Event()
    leave = threading.Event()
    build_started = threading.Event()
    release_build = threading.Event()

    def call() -> None:
        with live.use():
            held.set()
            leave.wait(WAIT_SECONDS)

    def blocked_build() -> Snapshot:
        build_started.set()
        release_build.wait(WAIT_SECONDS)
        return new

    with ThreadPoolExecutor(max_workers=2) as pool:
        in_flight_call = pool.submit(call)
        assert held.wait(WAIT_SECONDS)
        live.reload(lambda: new)
        second_reload = pool.submit(live.reload, blocked_build)
        assert build_started.wait(WAIT_SECONDS)
        leave.set()
        assert wait([in_flight_call], timeout=0.2).not_done == {in_flight_call}
        release_build.set()

    in_flight_call.result()
    assert second_reload.result() is new
    # The second reload's prune may still see the old snapshot in use if the
    # call thread had not woken yet, so only the call's own prune is exact.
    assert len(egress.calls) == 5
    assert egress.calls[:3] == [
        ("ensure", {NEW_DOMAINS}),
        ("prune", {OLD_DOMAINS, NEW_DOMAINS}),
        ("ensure", {NEW_DOMAINS}),
    ]
    assert egress.calls[3][0] == "prune"
    assert egress.calls[4] == ("prune", {NEW_DOMAINS})
