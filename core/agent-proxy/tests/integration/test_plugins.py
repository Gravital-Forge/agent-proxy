import functools
from pathlib import Path

import pytest
import yaml
from docker import DockerClient

from agent_proxy import cli
from agent_proxy.plugins import CommandAction, ScriptAction, load_manifest
from agent_proxy.reload import build_snapshot


def test_build_snapshot_pulls_a_pinned_plugin_and_loads_its_manifest(
    docker_client: DockerClient, pinned_plugin: str, tmp_path: Path
) -> None:
    """The real loader pulls the absent image by digest and runs its describe."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "agent_tree": "/srv/agents/it",
                "overflow_dir": "/srv/agents/it/overflow",
                "max_cages": 4,
                "plugins": {"apx-test": pinned_plugin},
                "scopes": {
                    "it": {
                        "grants": {
                            "apx-test.echo": {
                                "roots": [],
                                "domains": [],
                                "credentials": {},
                            }
                        }
                    }
                },
            }
        )
    )

    snapshot = build_snapshot(
        config_path, frozenset(), functools.partial(load_manifest, docker_client)
    )

    actions = snapshot.manifests["apx-test"].actions
    assert isinstance(actions["echo"], CommandAction)
    assert isinstance(actions["count"], ScriptAction)
    assert snapshot.warnings == ()


def test_manifest_check_passes_the_pulled_plugin(
    docker_client: DockerClient,
    pinned_plugin: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator command runs a local plugin's describe and prints the pass line."""
    docker_client.images.pull(pinned_plugin)

    status = cli.main(["manifest", "check", pinned_plugin])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out == f"manifest ok: {pinned_plugin} declares 5 actions\n"
    assert captured.err == ""
