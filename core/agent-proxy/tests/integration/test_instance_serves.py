"""The instance, started on the agent's own daemon, serves both plugins."""

import asyncio
import copy
import json
import os
import secrets
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from docker import DockerClient
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StreamableHttpTransport
from instance_harness import (
    docker_exec,
    free_port,
    removed_on_exit,
    run_start_script,
    wait_for_mcp,
)

from agent_proxy import egress

EXAMPLE_CONFIG = Path(__file__).parents[4] / "examples" / "instance.yaml"
EXAMPLE_TREE = "/home/agent-01/Projects"
UNTRACKED_FILE = "untracked.txt"
PROBE_GRANT = {
    "roots": [],
    "domains": [],
    "credentials": {"probe-secret": {"file": "/run/credentials/probe"}},
}
GIT_IDENTITY = (
    "-c",
    "user.name=apx-it",
    "-c",
    "user.email=apx-it@example.invalid",
    "-c",
    "commit.gpgsign=false",
)
STAT_SCRIPT = "import os, sys; s = os.stat(sys.argv[1]); print(s.st_dev, s.st_ino)"
CALL_TIMEOUT_SECONDS = 120
AUDIT_LINES = "50"
CALLED_TOOLS = ("git_status", "github_pr_view", "probe_read_credential")
UNBUILT_PLUGIN = "hcp-terraform"


@dataclass(frozen=True)
class _Tree:
    agent_tree: Path
    repo: Path
    state_dir: Path
    config_dir: Path


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603 - the test passes its own arguments
        ["git", "-C", str(repo), *arguments],  # noqa: S607 - the git CLI on PATH
        capture_output=True,
        check=True,
    )


def _make_tree(tmp_path: Path) -> _Tree:
    """Make the agent tree with its fixture repository, and the instance's directories.

    The state directory stays one short name under tmp_path: the control
    socket inside it must fit the Unix socket path limit.
    """
    projects = tmp_path / "Projects"
    projects.mkdir()
    agent_tree = projects.resolve(strict=True)
    (agent_tree / ".agent-proxy-overflow").mkdir()
    repo = agent_tree / "example-repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "README").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, *GIT_IDENTITY, "commit", "--quiet", "--message", "init")
    (repo / UNTRACKED_FILE).write_text("untracked\n", encoding="utf-8")
    state_dir = tmp_path / "s"
    state_dir.mkdir()
    config_dir = tmp_path / "c"
    config_dir.mkdir()
    return _Tree(
        agent_tree=agent_tree,
        repo=repo,
        state_dir=state_dir.resolve(strict=True),
        config_dir=config_dir.resolve(strict=True),
    )


def _full_config(
    agent_tree: Path, git_plugin: str, github_plugin: str, probe_plugin: str
) -> dict[str, object]:
    """Return the sample config for agent_tree, with the pushed plugins and a probe.

    The config leaves out the hcp-terraform plugin and every grant of its
    actions, because the test builds no image for it.
    """
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    config = yaml.safe_load(text.replace(EXAMPLE_TREE, str(agent_tree)))
    del config["plugins"][UNBUILT_PLUGIN]
    for scope in config["scopes"].values():
        scope["grants"] = {
            action: grant
            for action, grant in scope["grants"].items()
            if not action.startswith(f"{UNBUILT_PLUGIN}.")
        }
    config["plugins"]["git"] = git_plugin
    config["plugins"]["github"] = github_plugin
    config["plugins"]["probe"] = probe_plugin
    config["scopes"]["probe"] = {"grants": {"probe.read_credential": PROBE_GRANT}}
    return config


def _bootstrap_config(full: dict[str, object]) -> dict[str, object]:
    """Return full with every grant that names a credential removed."""
    bootstrap = copy.deepcopy(full)
    for scope in bootstrap["scopes"].values():
        scope["grants"] = {
            action: grant
            for action, grant in scope["grants"].items()
            if not grant["credentials"]
        }
    return bootstrap


def _write_yaml(path: Path, document: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def _exec_ok(name: str, command: Sequence[str], stdin: str | None = None) -> str:
    """Return the stdout of command in container name, which must exit 0."""
    completed = docker_exec(name, command, stdin)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _mint(name: str, scope: str) -> str:
    minted = _exec_ok(
        name, ["agent-proxy", "token", "mint", "--name", scope, "--scope", scope]
    )
    return minted.strip()


def _call(
    url: str, token: str, tool: str, arguments: dict[str, object]
) -> CallToolResult:
    """Call tool with arguments as token, after listing the tools."""

    async def calling() -> CallToolResult:
        transport = StreamableHttpTransport(url, auth=token)
        async with Client(transport, timeout=CALL_TIMEOUT_SECONDS) as client:
            await client.list_tools()
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(calling())


def _content(result: CallToolResult) -> dict[str, object]:
    """Return the structured content of a result that is not an error result."""
    assert result.is_error is False, result.content
    assert result.structured_content is not None, result.content
    return result.structured_content


def _audit_lines(name: str) -> list[dict[str, object]]:
    tailed = _exec_ok(name, ["agent-proxy", "audit", "tail", "-n", AUDIT_LINES])
    return [json.loads(line) for line in tailed.splitlines()]


def _assert_owned_with_mode(path: Path, mode: int) -> None:
    status = path.stat()
    assert status.st_uid == os.getuid(), path
    assert stat.S_IMODE(status.st_mode) == mode, (path, oct(status.st_mode))


def test_the_instance_serves_both_plugins_on_the_agents_daemon(
    docker_client: DockerClient,
    labelled_instance_image: str,
    git_plugin: str,
    github_stub_plugin: str,
    pinned_plugin: str,
    tmp_path: Path,
) -> None:
    """Bootstrap, store credentials, reload, then call git, github, and probe actions.

    The steps follow the runbook's first-start order, so their order matters.
    """
    tree = _make_tree(tmp_path)
    full = _full_config(tree.agent_tree, git_plugin, github_stub_plugin, pinned_plugin)
    config_file = tree.config_dir / "config.yaml"
    _write_yaml(config_file, _bootstrap_config(full))
    name = f"apx-it-serves-{secrets.token_hex(4)}"
    port = free_port()
    github_token = f"gh-{secrets.token_hex(8)}"
    probe_secret = f"probe-{secrets.token_hex(8)}"

    with removed_on_exit(docker_client, name):
        started = run_start_script(
            (
                labelled_instance_image,
                name,
                str(port),
                str(tree.config_dir),
                str(tree.state_dir),
                str(tree.agent_tree),
            )
        )
        assert started.returncode == 0, started.stderr
        url = wait_for_mcp(docker_client, name, port)

        for credential, value in (
            ("github-token", github_token),
            ("probe-secret", probe_secret),
        ):
            _exec_ok(name, ["agent-proxy", "credential", "set", credential], value)

        next_config = tree.config_dir / "config.next.yaml"
        _write_yaml(next_config, full)
        bootstrap_inode = config_file.stat().st_ino
        next_config.replace(config_file)
        assert config_file.stat().st_ino != bootstrap_inode
        assert _exec_ok(name, ["agent-proxy", "reload"]) == "reloaded\n"

        dev_token = _mint(name, "dev-agent")
        probe_token = _mint(name, "probe")

        status = _content(_call(url, dev_token, "git_status", {"repo": str(tree.repo)}))
        assert status["exit_code"] == 0
        assert UNTRACKED_FILE in status["stdout"]
        host_stat = tree.repo.stat()
        seen = _exec_ok(name, ["python", "-c", STAT_SCRIPT, str(tree.repo)]).split()
        assert seen == [str(host_stat.st_dev), str(host_stat.st_ino)]

        viewed = _content(
            _call(
                url, dev_token, "github_pr_view", {"repo": "example-org/n", "pr": "77"}
            )
        )
        assert json.loads(viewed["stdout"])["gh_token"] == github_token

        read = _content(_call(url, probe_token, "probe_read_credential", {}))
        assert read["stdout"] == probe_secret

        audit_lines = _audit_lines(name)
        for tool in CALLED_TOOLS:
            calls = [line for line in audit_lines if line["action"] == tool]
            assert len(calls) == 1, audit_lines
            assert calls[0]["refusal"] is None, calls

        proxy_name = egress.network_name(egress.list_key(["api.github.com"]))
        proxy = docker_client.containers.get(proxy_name)
        assert proxy.status == "running"
        assert proxy.labels["agent-proxy.test"] == "1"
        assert egress._READY_MESSAGE.encode() in proxy.logs()
        proxy_config = tree.state_dir / "egress" / proxy_name / "proxy.yaml"
        credential_file = tree.state_dir / "credentials" / "probe-secret"
        for path in (proxy_config, credential_file):
            _assert_owned_with_mode(path, 0o600)
            _assert_owned_with_mode(path.parent, 0o700)
