"""What the tests that start an instance through core/docker/start-instance.sh share."""

import asyncio
import contextlib
import json
import socket
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import docker.errors
import pytest
from conftest import remove_bridge
from docker import DockerClient
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StreamableHttpTransport

START_SCRIPT = Path(__file__).parents[3] / "docker" / "start-instance.sh"
TEST_LABEL_OPTION = ("--label", "agent-proxy.test=1")
EGRESS_TEST_LABELS = ["agent-proxy.test=1", "agent-proxy.egress"]
UNMINTED_TOKEN = "apx_" + "x" * 43
SERVE_SECONDS = 60
SERVE_POLL_SECONDS = 0.5
CLIENT_TIMEOUT_SECONDS = 5
CALL_TIMEOUT_SECONDS = 120
LOG_TAIL_LINES = 50
AUDIT_LINES = "50"


def free_port() -> int:
    """Return a loopback TCP port that nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_start_script(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the start script with arguments, then the test label option."""
    return subprocess.run(  # noqa: S603 - the tests pass their own arguments
        [str(START_SCRIPT), *arguments, *TEST_LABEL_OPTION],
        capture_output=True,
        text=True,
        check=False,
    )


def list_tools(url: str, token: str) -> list[str]:
    """Return the names of the tools token lists at url, sorted."""

    async def listing() -> list[str]:
        transport = StreamableHttpTransport(url, auth=token)
        async with Client(transport, timeout=CLIENT_TIMEOUT_SECONDS) as client:
            return sorted(tool.name for tool in await client.list_tools())

    return asyncio.run(listing())


def wait_for_mcp(client: DockerClient, name: str, port: int) -> str:
    """Return the instance's MCP URL once it accepts a session.

    The test fails, with the container's log tail, when no session opens
    within SERVE_SECONDS.
    """
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + SERVE_SECONDS
    while time.monotonic() < deadline:
        # The client reports a refused or dropped connection as RuntimeError.
        with contextlib.suppress(RuntimeError):
            list_tools(url, UNMINTED_TOKEN)
            return url
        time.sleep(SERVE_POLL_SECONDS)
    logs = client.containers.get(name).logs(tail=LOG_TAIL_LINES).decode()
    pytest.fail(
        f"instance {name!r} opened no MCP session at {url} within "
        f"{SERVE_SECONDS} seconds; log tail:\n{logs}"
    )


def docker_exec(
    name: str, command: Sequence[str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run command in container name through the docker CLI, feeding it stdin."""
    interactive = ["--interactive"] if stdin is not None else []
    return subprocess.run(  # noqa: S603 - the tests pass their own arguments
        ["docker", "exec", *interactive, name, *command],  # noqa: S607 - the docker CLI on PATH, as the start script runs it
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def exec_ok(name: str, command: Sequence[str], stdin: str | None = None) -> str:
    """Return the stdout of command in container name, which must exit 0."""
    completed = docker_exec(name, command, stdin)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def call_tool(
    url: str, token: str, tool: str, arguments: dict[str, object]
) -> CallToolResult:
    """Call tool with arguments as token, after listing the tools."""

    async def calling() -> CallToolResult:
        transport = StreamableHttpTransport(url, auth=token)
        async with Client(transport, timeout=CALL_TIMEOUT_SECONDS) as client:
            await client.list_tools()
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(calling())


def tool_content(result: CallToolResult) -> dict[str, object]:
    """Return the structured content of a result that is not an error result."""
    assert result.is_error is False, result.content
    assert result.structured_content is not None, result.content
    return result.structured_content


def tool_refusal(result: CallToolResult) -> str:
    """Return the text of an error result."""
    assert result.is_error is True, result.content
    [text] = result.content
    return text.text


def read_audit_lines(name: str) -> list[dict[str, object]]:
    """Return the last AUDIT_LINES audit lines of instance name, oldest first."""
    tailed = exec_ok(name, ["agent-proxy", "audit", "tail", "-n", AUDIT_LINES])
    return [json.loads(line) for line in tailed.splitlines()]


@contextlib.contextmanager
def removed_on_exit(client: DockerClient, name: str) -> Iterator[None]:
    """On exit, remove the instance container and the egress objects it created."""
    try:
        yield
    finally:
        with contextlib.suppress(docker.errors.NotFound):
            client.containers.get(name).remove(force=True)
        egress_filter = {"label": EGRESS_TEST_LABELS}
        for container in client.containers.list(all=True, filters=egress_filter):
            container.remove(force=True)
        for network in client.networks.list(filters=egress_filter):
            network.remove()
        remove_bridge(client)
