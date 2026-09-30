import asyncio
import contextlib
import json
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import docker.errors
import pytest
import uvicorn
import yaml
from conftest import remove_bridge
from docker import DockerClient
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.http import StarletteWithLifespan

from agent_proxy import audit, cli, egress, plugins
from agent_proxy.auth import TokenStore
from agent_proxy.mcp import Instance
from agent_proxy.plugins import Manifest

PREFIX = "agent-proxy refused: "
UNKNOWN_TOKEN = "apx_" + "x" * 43
UNAUTHENTICATED = "the bearer token is missing, unknown, revoked, or expired"
EXAMPLE_COM = ("example.com",)
FULL: Mapping[str, Sequence[str]] = MappingProxyType(
    {"echo": (), "count": (), "fail": (), "sleep": EXAMPLE_COM}
)
NARROW: Mapping[str, Sequence[str]] = MappingProxyType({"echo": ()})
NO_PLUGINS: Mapping[str, str] = MappingProxyType({})
FULL_TOOLS = ["test_count", "test_echo", "test_fail", "test_sleep"]
SLEPT = {"exit_code": 0, "stdout": "", "stderr": ""}
MAX_CAGES = 2
OVERFLOW_BYTES = 300000
AUDIT_LINES = 10
SERVER_SECONDS = 10
SERVER_POLL_SECONDS = 0.01
CAGE_START_SECONDS = 30
PRUNE_SECONDS = 30
HELD_CALL_SECONDS = 40
BUILD_START_SECONDS = 30
RELOAD_SECONDS = 120
HOLD_SECONDS = 300
POLL_SECONDS = 0.2


@dataclass(frozen=True)
class _Instance:
    url: str
    state_dir: Path
    config_path: Path
    agent_tree: Path
    plugin: str
    tokens: TokenStore
    full_token: str
    narrow_token: str

    @property
    def overflow_dir(self) -> Path:
        return self.agent_tree / "overflow"

    def write_config(
        self,
        full: Mapping[str, Sequence[str]] = FULL,
        narrow: Mapping[str, Sequence[str]] = NARROW,
        extra_plugins: Mapping[str, str] = NO_PLUGINS,
    ) -> None:
        """Rewrite the config file with these grants and plugins beside test."""
        _write_config(
            self.config_path,
            self.agent_tree,
            {"test": self.plugin, **extra_plugins},
            full,
            narrow,
        )


@pytest.fixture
def instance(
    docker_client: DockerClient,
    pinned_plugin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Instance]:
    """A started instance, served on a loopback port, with one token per scope."""
    agent_tree = tmp_path / "agent"
    (agent_tree / "overflow").mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, agent_tree, {"test": pinned_plugin}, FULL, NARROW)
    # ExitStack runs its callbacks last-registered first, and runs every one
    # even after an earlier one raised, then re-raises.
    with contextlib.ExitStack() as teardown:
        # Under tmp_path the control socket's path comes near the AF_UNIX limit.
        state_dir = Path(tempfile.mkdtemp(dir="/tmp"))
        teardown.callback(shutil.rmtree, state_dir)
        tokens = TokenStore(state_dir / "tokens.sqlite3")
        full_token = tokens.mint("full-bot", "full", None)
        narrow_token = tokens.mint("narrow-bot", "narrow", None)
        monkeypatch.setenv(cli.STATE_DIR_VARIABLE, str(state_dir))
        monkeypatch.setenv(cli.CONFIG_VARIABLE, str(config_path))
        teardown.callback(remove_bridge, docker_client)
        teardown.callback(
            egress.EgressProxies(docker_client, state_dir / "egress").prune, ()
        )
        started = Instance.start(state_dir, config_path, docker_client)
        teardown.callback(started.close)
        url = teardown.enter_context(_serving(started.app))
        yield _Instance(
            url=url,
            state_dir=state_dir,
            config_path=config_path,
            agent_tree=agent_tree,
            plugin=pinned_plugin,
            tokens=tokens,
            full_token=full_token,
            narrow_token=narrow_token,
        )


def _write_config(
    path: Path,
    agent_tree: Path,
    pinned: Mapping[str, str],
    full: Mapping[str, Sequence[str]],
    narrow: Mapping[str, Sequence[str]],
) -> None:
    """Write a config granting each scope the test actions mapped to their domains."""
    document = {
        "agent_tree": str(agent_tree),
        "overflow_dir": str(agent_tree / "overflow"),
        "max_cages": MAX_CAGES,
        "plugins": dict(pinned),
        "scopes": {
            "full": {"grants": _grants(full)},
            "narrow": {"grants": _grants(narrow)},
        },
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def _grants(actions: Mapping[str, Sequence[str]]) -> dict[str, object]:
    return {
        f"test.{action}": {"roots": [], "domains": list(domains), "credentials": {}}
        for action, domains in actions.items()
    }


@contextlib.contextmanager
def _serving(app: StarletteWithLifespan) -> Iterator[str]:
    """Serve app on a free loopback port on a uvicorn thread; yield its MCP URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail(
                f"the test server did not start within {SERVER_SECONDS} seconds"
            )
        time.sleep(SERVER_POLL_SECONDS)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(SERVER_SECONDS)
        if thread.is_alive():
            pytest.fail(f"the test server did not stop within {SERVER_SECONDS} seconds")


def _list(url: str, token: str) -> list[str]:
    """Return the names of the tools token lists, sorted."""

    async def listing() -> list[str]:
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            return sorted(tool.name for tool in await client.list_tools())

    return asyncio.run(listing())


def _call(
    url: str,
    token: str,
    tool: str,
    arguments: dict[str, object],
    *,
    raise_on_error: bool = True,
) -> CallToolResult:
    """Call tool with arguments as token, after listing the tools."""

    async def calling() -> CallToolResult:
        async with Client(StreamableHttpTransport(url, auth=token)) as client:
            await client.list_tools()
            return await client.call_tool(
                tool, arguments, raise_on_error=raise_on_error
            )

    return asyncio.run(calling())


def _refusal(url: str, token: str, tool: str, arguments: dict[str, object]) -> str:
    """Return the text of the ToolError the call raises."""
    with pytest.raises(ToolError) as refused:
        _call(url, token, tool, arguments)
    return str(refused.value)


def _joined[T](future: Future[T], seconds: float, what: str) -> T:
    """Return future's result, failing the test when it takes over seconds."""
    done, _ = wait([future], timeout=seconds)
    if not done:
        pytest.fail(f"{what} did not finish within {seconds} seconds")
    return future.result()


def _wait_for_cage(client: DockerClient, image: str) -> None:
    """Return once a labelled container on image is running."""
    deadline = time.monotonic() + CAGE_START_SECONDS
    while time.monotonic() < deadline:
        running = client.containers.list(
            filters={"label": "agent-proxy.test=1"}, ignore_removed=True
        )
        if any(container.attrs["Config"]["Image"] == image for container in running):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(f"no cage on {image!r} ran within {CAGE_START_SECONDS} seconds")


def _container_exists(client: DockerClient, name: str) -> bool:
    try:
        client.containers.get(name)
    except docker.errors.NotFound:
        return False
    return True


def _wait_for_removal(client: DockerClient, name: str) -> None:
    """Return once the named container is gone."""
    deadline = time.monotonic() + PRUNE_SECONDS
    while time.monotonic() < deadline:
        if not _container_exists(client, name):
            return
        time.sleep(POLL_SECONDS)
    pytest.fail(f"container {name!r} was not removed within {PRUNE_SECONDS} seconds")


def test_each_scope_lists_its_granted_tools(instance: _Instance) -> None:
    """The full token lists four tools, and the narrow token lists test_echo."""
    assert _list(instance.url, instance.full_token) == FULL_TOOLS
    assert _list(instance.url, instance.narrow_token) == ["test_echo"]


def test_a_granted_call_returns_the_cages_result(instance: _Instance) -> None:
    """test_echo runs in a real cage and returns its streams as structured content."""
    result = _call(instance.url, instance.full_token, "test_echo", {"text": "hi"})

    assert result.structured_content == {"exit_code": 0, "stdout": "hi\n", "stderr": ""}


@pytest.mark.parametrize(
    ("caller", "tool", "arguments", "refusal"),
    [
        pytest.param(
            "unknown", "test_echo", {"text": "hi"}, UNAUTHENTICATED, id="unknown-token"
        ),
        pytest.param(
            "narrow",
            "test_count",
            {},
            "tool 'test_count' is not granted to scope 'narrow'",
            id="ungranted-tool",
        ),
        pytest.param(
            "full",
            "test_echo",
            {"text": 5},
            "$.text: 5 is not of type 'string'",
            id="invalid-arguments",
        ),
    ],
)
def test_a_refused_call_raises_a_tool_error_naming_its_refusal(
    instance: _Instance,
    caller: str,
    tool: str,
    arguments: dict[str, object],
    refusal: str,
) -> None:
    """A refused call's ToolError text is the refusal prefix, then its refusal."""
    token = {
        "unknown": UNKNOWN_TOKEN,
        "full": instance.full_token,
        "narrow": instance.narrow_token,
    }[caller]

    assert _refusal(instance.url, token, tool, arguments) == PREFIX + refusal


def test_a_revoked_token_is_refused_after_a_granted_call(instance: _Instance) -> None:
    """A token's call succeeds, and after its revocation the next call is refused."""
    token = instance.tokens.mint("revoked-bot", "full", None)
    _call(instance.url, token, "test_echo", {"text": "hi"})
    instance.tokens.revoke("revoked-bot")

    assert (
        _refusal(instance.url, token, "test_echo", {"text": "hi"})
        == PREFIX + UNAUTHENTICATED
    )


def test_a_failed_script_is_a_normal_result_with_its_overflow_file(
    instance: _Instance,
) -> None:
    """test_fail is a normal result, unprefixed, whose stderr went to a file."""
    result = _call(
        instance.url, instance.full_token, "test_fail", {}, raise_on_error=False
    )

    assert result.is_error is False
    [text] = result.content
    assert not text.text.startswith(PREFIX)
    content = result.structured_content
    assert content is not None
    assert content["exit_code"] == 3
    assert content["truncated"] is True
    stderr_file = Path(str(content["stderr_file"]))
    assert stderr_file.parent == instance.overflow_dir
    assert re.fullmatch(r"[0-9a-f]{32}\.stderr", stderr_file.name)
    assert stderr_file.stat().st_size == OVERFLOW_BYTES


def test_each_call_writes_one_audit_line_in_call_order(instance: _Instance) -> None:
    """A granted, a refused, a bad-token, and a failed call give four audit lines."""
    url = instance.url
    _call(url, instance.full_token, "test_echo", {"text": "hi"})
    ungranted = _refusal(url, instance.narrow_token, "test_count", {})
    unknown = _refusal(url, UNKNOWN_TOKEN, "test_echo", {"text": "hi"})
    failed = _call(url, instance.full_token, "test_fail", {}, raise_on_error=False)

    lines = [
        json.loads(line)
        for line in audit.tail(instance.state_dir / "audit", AUDIT_LINES)
    ]

    assert [
        (line["action"], line["token_name"], line["refusal"]) for line in lines
    ] == [
        ("test_echo", "full-bot", None),
        ("test_count", "narrow-bot", ungranted.removeprefix(PREFIX)),
        ("test_echo", None, unknown.removeprefix(PREFIX)),
        ("test_fail", "full-bot", None),
    ]
    assert failed.structured_content is not None
    stderr_file = Path(str(failed.structured_content["stderr_file"]))
    assert lines[3]["call_id"] == stderr_file.stem
    assert lines[3]["truncated"] is True


def test_a_dropped_lists_proxy_stays_until_its_call_ends(
    instance: _Instance, docker_client: DockerClient
) -> None:
    """A reload dropping example.com keeps its proxy until the call on it ends."""
    proxy = egress.network_name(egress.list_key(EXAMPLE_COM))
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = pool.submit(
            _call, instance.url, instance.full_token, "test_sleep", {"seconds": 15}
        )
        _wait_for_cage(docker_client, instance.plugin)
        instance.write_config(full={**FULL, "sleep": ()})

        assert cli.main(["reload"]) == 0
        assert not call.done()
        assert _container_exists(docker_client, proxy)
        result = _joined(call, HELD_CALL_SECONDS, "the test_sleep call")

    assert result.structured_content == SLEPT
    _wait_for_removal(docker_client, proxy)


def test_a_retired_call_replies_while_a_reload_is_held(
    instance: _Instance, docker_client: DockerClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call on a retired snapshot replies while a later reload waits in its build."""
    build_started = threading.Event()
    release_build = threading.Event()
    load_manifest = plugins.load_manifest

    def held_load_manifest(client: DockerClient, reference: str) -> Manifest:
        build_started.set()
        # The bound only keeps a failed test from holding the reload forever.
        release_build.wait(HOLD_SECONDS)
        return load_manifest(client, reference)

    with ThreadPoolExecutor(max_workers=2) as pool:
        call = pool.submit(
            _call, instance.url, instance.full_token, "test_sleep", {"seconds": 20}
        )
        _wait_for_cage(docker_client, instance.plugin)
        instance.write_config(
            full={
                action: domains for action, domains in FULL.items() if action != "count"
            }
        )
        assert cli.main(["reload"]) == 0
        monkeypatch.setattr(plugins, "load_manifest", held_load_manifest)
        try:
            reload = pool.submit(cli.main, ["reload"])
            if not build_started.wait(BUILD_START_SECONDS):
                pytest.fail(
                    "the second reload did not start its build within "
                    f"{BUILD_START_SECONDS} seconds"
                )

            assert not call.done()
            result = _joined(call, HELD_CALL_SECONDS, "the retired test_sleep call")
            assert not reload.done()
        finally:
            release_build.set()
        assert _joined(reload, RELOAD_SECONDS, "the held reload") == 0

    assert result.structured_content == SLEPT


def test_a_reload_swaps_in_a_changed_config(
    capsys: pytest.CaptureFixture[str], instance: _Instance
) -> None:
    """A reload adding test.count to narrow prints reloaded; narrow then lists it."""
    # capsys comes before instance, so uvicorn's log handlers, bound when its
    # config is built, write to capsys until the server stops.
    instance.write_config(narrow={**NARROW, "count": ()})

    assert cli.main(["reload"]) == 0
    assert "reloaded" in capsys.readouterr().out.splitlines()
    assert _list(instance.url, instance.narrow_token) == ["test_count", "test_echo"]


def test_a_reload_that_hits_a_docker_error_keeps_the_config_in_force(
    capsys: pytest.CaptureFixture[str], instance: _Instance, registry: str
) -> None:
    """A reload pinning an absent image returns 1, and full still lists its tools."""
    instance.write_config(
        extra_plugins={"absent": f"{registry}/apx-absent@sha256:" + "0" * 64}
    )
    prefix = f"agent-proxy: config file {str(instance.config_path)!r}: plugins.absent: "

    assert cli.main(["reload"]) == 1
    errors = capsys.readouterr().err.splitlines()
    assert any(line.startswith(prefix) for line in errors), errors
    assert _list(instance.url, instance.full_token) == FULL_TOOLS
