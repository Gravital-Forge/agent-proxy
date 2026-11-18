import json
import shutil
import tempfile
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
import yaml
from egress_fakes import RecordingEgress

from agent_proxy.audit import tail
from agent_proxy.config import ConfigError
from agent_proxy.credentials import unset_message
from agent_proxy.mcp import Instance
from agent_proxy.plugins import CommandAction, Manifest
from agent_proxy.reload import (
    CONTROL_SOCKET_NAME,
    ControlError,
    request_check,
    request_reload,
)
from agent_proxy.selfcheck import SELF_CHECK_TOOL

SWEEPER_THREAD = "overflow-sweeper"
GITHUB_IMAGE = "ghcr.io/example/github@sha256:" + "c" * 64
CONFIG = {
    "agent_tree": "/srv/agents/alpha",
    "overflow_dir": "/srv/agents/alpha/overflow",
    "max_cages": 4,
    "plugins": {},
    "scopes": {"ci": {"grants": {}}},
}


@pytest.fixture
def state_dir() -> Iterator[Path]:
    """A state directory under /tmp, since an AF_UNIX path caps at 107 bytes."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    yield directory
    shutil.rmtree(directory)


@pytest.fixture
def egress(monkeypatch: pytest.MonkeyPatch) -> RecordingEgress:
    """Make the instance's egress a recording fake, and return the fake."""
    fake = RecordingEgress()
    monkeypatch.setattr(
        "agent_proxy.mcp.EgressProxies", lambda client, config_root: fake
    )
    return fake


def _config(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _sweeper_running() -> bool:
    return SWEEPER_THREAD in {thread.name for thread in threading.enumerate()}


def test_start_opens_the_stores_under_the_state_dir_and_serves_reloads(
    state_dir: Path, tmp_path: Path, egress: RecordingEgress
) -> None:
    """The stores live under the state directory, and the control socket reloads."""
    instance = Instance.start(state_dir, _config(tmp_path, CONFIG), mock.MagicMock())
    try:
        reply = request_reload(state_dir / CONTROL_SOCKET_NAME)
    finally:
        instance.close()

    assert reply.ok is True
    assert (state_dir / "tokens.sqlite3").is_file()
    assert (state_dir / "credentials").is_dir()
    assert (state_dir / "audit").is_dir()
    assert [verb for verb, _ in egress.calls[:2]] == ["ensure", "prune"]


def test_start_serves_checks_on_the_control_socket_and_audits_them(
    state_dir: Path, tmp_path: Path, egress: RecordingEgress
) -> None:
    """A check request reaches the instance's self-check and writes one audit line."""
    instance = Instance.start(state_dir, _config(tmp_path, CONFIG), mock.MagicMock())
    try:
        reply = request_check(state_dir / CONTROL_SOCKET_NAME, None)
        lines = list(tail(state_dir / "audit", 10))
    finally:
        instance.close()

    assert reply.ok is True
    [report] = reply.reports
    assert report["scope"] == "ci"
    assert [entry["check"] for entry in report["checks"]] == ["overflow_dir"]
    [line] = [json.loads(line) for line in lines]
    assert line["action"] == SELF_CHECK_TOOL
    assert line["scope"] == "ci"
    assert line["token_name"] is None


def test_start_from_a_config_naming_an_unstored_credential_warns_of_it(
    state_dir: Path,
    tmp_path: Path,
    egress: RecordingEgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance starts and serves a reload; its warnings name the credential."""
    manifest = Manifest(
        actions={
            "pr_view": CommandAction(
                name="pr_view",
                input_schema={"type": "object", "additionalProperties": False},
                credentials=(),
                timeout=timedelta(seconds=30),
                argv=("gh", "pr", "view"),
                parameters=(),
                passthrough_args=False,
            )
        }
    )
    monkeypatch.setattr(
        "agent_proxy.plugins.load_manifest", lambda client, reference: manifest
    )
    grant = {
        "roots": [],
        "domains": [],
        "credentials": {"github-token": {"env": "GH_TOKEN"}},
    }
    document = CONFIG | {
        "plugins": {"github": GITHUB_IMAGE},
        "scopes": {"ci": {"grants": {"github.pr_view": grant}}},
    }

    instance = Instance.start(state_dir, _config(tmp_path, document), mock.MagicMock())
    try:
        reply = request_reload(state_dir / CONTROL_SOCKET_NAME)
    finally:
        instance.close()

    assert instance.warnings == (unset_message("github-token"),)
    assert reply.ok is True
    assert reply.warnings == (unset_message("github-token"),)


def test_close_removes_the_control_socket_and_stops_the_sweeper(
    state_dir: Path, tmp_path: Path, egress: RecordingEgress
) -> None:
    """After close, the control socket is gone and the sweeper thread has ended."""
    instance = Instance.start(state_dir, _config(tmp_path, CONFIG), mock.MagicMock())

    instance.close()

    assert not (state_dir / CONTROL_SOCKET_NAME).exists()
    assert not _sweeper_running()


def test_a_config_the_build_refuses_raises_and_starts_nothing(
    state_dir: Path, tmp_path: Path, egress: RecordingEgress
) -> None:
    """A refused config raises its ConfigError before any egress or socket."""
    broken = {key: value for key, value in CONFIG.items() if key != "scopes"}

    with pytest.raises(ConfigError):
        Instance.start(state_dir, _config(tmp_path, broken), mock.MagicMock())

    assert egress.calls == []
    assert not (state_dir / CONTROL_SOCKET_NAME).exists()


def test_a_control_socket_that_cannot_bind_raises_and_starts_no_sweeper(
    state_dir: Path, tmp_path: Path, egress: RecordingEgress
) -> None:
    """A path that holds a directory refuses the control socket; no sweeper starts."""
    (state_dir / CONTROL_SOCKET_NAME).mkdir()

    with pytest.raises(ControlError):
        Instance.start(state_dir, _config(tmp_path, CONFIG), mock.MagicMock())

    assert not _sweeper_running()


def test_a_step_that_fails_closes_the_steps_started_before_it(
    state_dir: Path,
    tmp_path: Path,
    egress: RecordingEgress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the sweeper cannot start, the control socket already bound is closed."""

    def refuse(self: object) -> None:
        raise RuntimeError("the sweeper thread cannot start")

    monkeypatch.setattr("agent_proxy.mcp.OverflowSweeper.start", refuse)

    with pytest.raises(RuntimeError, match="sweeper"):
        Instance.start(state_dir, _config(tmp_path, CONFIG), mock.MagicMock())

    assert not (state_dir / CONTROL_SOCKET_NAME).exists()
