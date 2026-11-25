"""A provided credential, issued in a provider's own cage, reaches a call's cage."""

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml
from docker import DockerClient
from instance_harness import (
    call_tool,
    exec_ok,
    free_port,
    read_audit_lines,
    removed_on_exit,
    run_start_script,
    tool_content,
    tool_refusal,
    wait_for_mcp,
)

from agent_proxy.mcp import REFUSAL_PREFIX
from agent_proxy.selfcheck import SELF_CHECK_TOOL

PROVIDER = "probe.issue_value"
PROVIDER_TOOL = "probe_issue_value"
READ_TOOL = "probe_read_env"
SEED = "probe-seed"
ISSUED = "issued-token"
FAILING = "failing-token"
ISSUE_FAILURE = (
    "it exited 4, stderr 'issue-value: ISSUE_FAIL is set, so no value is issued\\n'"
)


@dataclass(frozen=True)
class _Tree:
    agent_tree: Path
    state_dir: Path
    config_dir: Path


def _make_tree(tmp_path: Path) -> _Tree:
    """Make the agent tree with its overflow directory, and the instance's directories.

    The state directory stays one short name under tmp_path: the control
    socket inside it must fit the Unix socket path limit.
    """
    agent_tree = tmp_path / "Projects"
    (agent_tree / ".agent-proxy-overflow").mkdir(parents=True)
    state_dir = tmp_path / "s"
    state_dir.mkdir()
    config_dir = tmp_path / "c"
    config_dir.mkdir()
    return _Tree(
        agent_tree=agent_tree.resolve(strict=True),
        state_dir=state_dir.resolve(strict=True),
        config_dir=config_dir.resolve(strict=True),
    )


def _read_grant(credential: str) -> dict[str, object]:
    return {"roots": [], "domains": [], "credentials": {credential: {"env": "PROBE"}}}


def _config(tree: _Tree, probe_plugin: str) -> dict[str, object]:
    """Return a config whose scope issued reads a working provider's value.

    The scope failing reads the value of a provider that always fails.
    """
    return {
        "agent_tree": str(tree.agent_tree),
        "overflow_dir": str(tree.agent_tree / ".agent-proxy-overflow"),
        "max_cages": 2,
        "plugins": {"probe": probe_plugin},
        "provided_credentials": {
            ISSUED: {
                "provider": PROVIDER,
                "credentials": {SEED: {"file": "/run/credentials/seed"}},
                "env": {"ISSUE_LIFETIME_SECONDS": "3600"},
                "domains": [],
                "refresh_before": "10m",
            },
            FAILING: {
                "provider": PROVIDER,
                "credentials": {},
                "env": {"ISSUE_FAIL": "1"},
                "domains": [],
                "refresh_before": "10m",
            },
        },
        "scopes": {
            "issued": {"grants": {"probe.read_env": _read_grant(ISSUED)}},
            "failing": {"grants": {"probe.read_env": _read_grant(FAILING)}},
        },
    }


def _start(client: DockerClient, image: str, name: str, tree: _Tree) -> str:
    """Start instance name and return its MCP URL once it serves."""
    port = free_port()
    started = run_start_script(
        (
            image,
            name,
            str(port),
            str(tree.config_dir),
            str(tree.state_dir),
            str(tree.agent_tree),
        )
    )
    assert started.returncode == 0, started.stderr
    return wait_for_mcp(client, name, port)


def _mint(name: str, scope: str) -> str:
    mint = ["agent-proxy", "token", "mint", "--name", scope, "--scope", scope]
    return exec_ok(name, mint).strip()


def _lines_of(lines: list[dict[str, object]], action: str) -> list[dict[str, object]]:
    return [line for line in lines if line["action"] == action]


def test_a_provided_value_reaches_a_later_calls_cage_and_serves_from_the_cache(
    docker_client: DockerClient,
    labelled_instance_image: str,
    pinned_plugin: str,
    tmp_path: Path,
) -> None:
    """Two calls print the value the provider issued, and the provider runs once.

    The self-check between them reports the provided credential and runs no
    provider.
    """
    tree = _make_tree(tmp_path)
    (tree.config_dir / "config.yaml").write_text(
        yaml.safe_dump(_config(tree, pinned_plugin)), encoding="utf-8"
    )
    name = f"apx-it-provided-{secrets.token_hex(4)}"
    seed = f"seed-{secrets.token_hex(8)}"

    with removed_on_exit(docker_client, name):
        url = _start(docker_client, labelled_instance_image, name, tree)
        exec_ok(name, ["agent-proxy", "credential", "set", SEED], seed)
        token = _mint(name, "issued")

        first = tool_content(call_tool(url, token, READ_TOOL, {}))
        report = tool_content(call_tool(url, token, SELF_CHECK_TOOL, {}))
        second = tool_content(call_tool(url, token, READ_TOOL, {}))
        lines = read_audit_lines(name)

    assert first["stdout"] == f"{seed}\n"
    assert second["stdout"] == f"{seed}\n"
    [credential_entry] = [
        entry for entry in report["checks"] if entry["check"] == "credential"
    ]
    assert credential_entry == {
        "check": "credential",
        "subject": ISSUED,
        "ok": True,
        "detail": f"provided by {PROVIDER!r}; every stored credential it takes"
        " is stored",
    }
    [provider_line] = _lines_of(lines, PROVIDER_TOOL)
    assert provider_line["credential"] == ISSUED
    assert provider_line["token_name"] is None
    assert provider_line["scope"] is None
    assert provider_line["arguments"] is None
    assert provider_line["exit_code"] == 0
    assert provider_line["refusal"] is None
    read_lines = _lines_of(lines, READ_TOOL)
    assert [line["refusal"] for line in read_lines] == [None, None]
    assert [line["credential"] for line in read_lines] == [None, None]
    assert seed not in json.dumps(lines)


def test_a_failing_provider_with_no_cached_value_refuses_the_call(
    docker_client: DockerClient,
    labelled_instance_image: str,
    pinned_plugin: str,
    tmp_path: Path,
) -> None:
    """The call is refused with the provider's failure, and never reaches its cage."""
    tree = _make_tree(tmp_path)
    (tree.config_dir / "config.yaml").write_text(
        yaml.safe_dump(_config(tree, pinned_plugin)), encoding="utf-8"
    )
    name = f"apx-it-provider-fails-{secrets.token_hex(4)}"
    refusal = (
        f"provided credential {FAILING!r} has no unexpired value: provider"
        f" {PROVIDER!r} failed: {ISSUE_FAILURE}"
    )

    with removed_on_exit(docker_client, name):
        url = _start(docker_client, labelled_instance_image, name, tree)
        token = _mint(name, "failing")

        refused = tool_refusal(call_tool(url, token, READ_TOOL, {}))
        lines = read_audit_lines(name)

    assert refused == REFUSAL_PREFIX + refusal
    [provider_line] = _lines_of(lines, PROVIDER_TOOL)
    assert provider_line["credential"] == FAILING
    assert provider_line["exit_code"] == 4
    assert provider_line["refusal"] == ISSUE_FAILURE
    [read_line] = _lines_of(lines, READ_TOOL)
    assert read_line["token_name"] == "failing"
    assert read_line["exit_code"] is None
    assert read_line["refusal"] == refusal
