import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import docker.errors
import pytest
import yaml
from conftest import LABELLED_INSTANCE_IMAGE_TAG, TEST_LABELS
from docker import DockerClient
from instance_harness import (
    UNMINTED_TOKEN,
    docker_exec,
    free_port,
    list_tools,
    removed_on_exit,
    run_start_script,
    wait_for_mcp,
)

VENV_BIN = "/app/.venv/bin"
SCOPE = "s"
MINT = ("agent-proxy", "token", "mint", "--name", "t", "--scope", SCOPE)


@dataclass(frozen=True)
class _Tree:
    config_dir: Path
    state_dir: Path
    agent_tree: Path


@dataclass(frozen=True)
class _Served:
    name: str
    port: int
    url: str
    tree: _Tree
    arguments: tuple[str, ...]


def _tree(tmp_path: Path) -> _Tree:
    """Make a config directory, a state directory, and an agent tree under tmp_path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    agent_tree = tmp_path / "agent"
    (agent_tree / "overflow").mkdir(parents=True)
    tree = _Tree(
        config_dir=tmp_path / "config",
        state_dir=state_dir.resolve(strict=True),
        agent_tree=agent_tree.resolve(strict=True),
    )
    tree.config_dir.mkdir()
    document = {
        "agent_tree": str(tree.agent_tree),
        "overflow_dir": str(tree.agent_tree / "overflow"),
        "max_cages": 1,
        "plugins": {},
        "scopes": {SCOPE: {"grants": {}}},
    }
    (tree.config_dir / "config.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )
    return tree


def _instance_name() -> str:
    return f"apx-it-instance-{secrets.token_hex(4)}"


def _exists(client: DockerClient, name: str) -> bool:
    try:
        client.containers.get(name)
    except docker.errors.NotFound:
        return False
    return True


@pytest.fixture
def served(
    docker_client: DockerClient, labelled_instance_image: str, tmp_path: Path
) -> Iterator[_Served]:
    """An instance started through the script, accepting MCP sessions."""
    tree = _tree(tmp_path)
    name = _instance_name()
    port = free_port()
    arguments = (
        labelled_instance_image,
        name,
        str(port),
        str(tree.config_dir),
        str(tree.state_dir),
        str(tree.agent_tree),
    )
    with removed_on_exit(docker_client, name):
        started = run_start_script(arguments)
        assert started.returncode == 0, started.stderr
        url = wait_for_mcp(docker_client, name, port)
        yield _Served(name=name, port=port, url=url, tree=tree, arguments=arguments)


def test_the_image_serves_on_all_interfaces_with_the_instance_environment(
    docker_client: DockerClient, instance_image: str
) -> None:
    """Entrypoint agent-proxy, command serve on 0.0.0.0:8000, socket and config set."""
    config = docker_client.images.get(instance_image).attrs["Config"]
    environment = dict(entry.split("=", 1) for entry in config["Env"])

    assert config["Entrypoint"] == ["agent-proxy"]
    assert config["Cmd"] == ["serve", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104 - the image's own command, read back
    assert environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert environment["AGENT_PROXY_CONFIG"] == "/etc/agent-proxy/config.yaml"
    assert environment["PATH"].split(":")[0] == VENV_BIN


def test_the_image_prints_the_admin_usage_when_hardened(
    docker_client: DockerClient, instance_image: str
) -> None:
    """Root, read-only, no capabilities, no new privileges: --help exits 0."""
    output = docker_client.containers.run(
        instance_image,
        ["--help"],
        user="0:0",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        tmpfs={"/tmp": ""},  # noqa: S108 - the path is inside the container
        labels=TEST_LABELS,
        remove=True,
    ).decode()

    assert output.startswith("usage: agent-proxy")
    assert "serve" in output


def test_the_script_starts_a_hardened_instance(
    docker_client: DockerClient, served: _Served
) -> None:
    """Root, no capabilities, read-only, restarting, loopback port, four bind mounts."""
    attrs = docker_client.containers.get(served.name).attrs
    host_config = attrs["HostConfig"]
    socket_path = os.environ["DOCKER_HOST"].removeprefix("unix://")
    tree = served.tree
    mounts = {
        (mount["Type"], mount["Source"], mount["Target"], mount.get("ReadOnly", False))
        for mount in host_config["Mounts"]
    }

    assert attrs["Config"]["User"] == "0:0"
    assert host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in host_config["SecurityOpt"]
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["RestartPolicy"]["Name"] == "unless-stopped"
    assert host_config["PortBindings"] == {
        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(served.port)}]
    }
    assert mounts == {
        ("bind", socket_path, "/var/run/docker.sock", False),
        ("bind", str(tree.config_dir), "/etc/agent-proxy", True),
        ("bind", str(tree.state_dir), str(tree.state_dir), False),
        ("bind", str(tree.agent_tree), str(tree.agent_tree), False),
    }


def test_a_token_the_instance_never_minted_lists_no_tools(served: _Served) -> None:
    """The served instance lists nothing for a token that does not verify."""
    assert list_tools(served.url, UNMINTED_TOKEN) == []


def test_a_token_minted_through_docker_exec_is_printed(served: _Served) -> None:
    """agent-proxy token mint inside the container prints an apx_ token."""
    minted = docker_exec(served.name, MINT)

    assert minted.returncode == 0, minted.stderr
    assert minted.stdout.startswith("apx_")


def test_the_token_store_belongs_to_the_test_user(served: _Served) -> None:
    """After a mint, tokens.sqlite3 is owned by this process's uid."""
    minted = docker_exec(served.name, MINT)
    assert minted.returncode == 0, minted.stderr

    token_store = served.tree.state_dir / "tokens.sqlite3"
    assert token_store.stat().st_uid == os.getuid()


def test_a_second_run_leaves_the_running_instance_alone(
    docker_client: DockerClient, served: _Served
) -> None:
    """The same arguments again exit 0, and the container ID stays the same."""
    before = docker_client.containers.get(served.name).id

    again = run_start_script(served.arguments)

    assert again.returncode == 0, again.stderr
    assert docker_client.containers.get(served.name).id == before


def test_a_symlinked_state_directory_is_refused(
    docker_client: DockerClient, tmp_path: Path
) -> None:
    """A state directory that is a symbolic link exits 1 with one stderr line."""
    tree = _tree(tmp_path)
    link = tmp_path / "state-link"
    link.symlink_to(tree.state_dir)
    name = _instance_name()
    with removed_on_exit(docker_client, name):
        refused = run_start_script(
            (
                LABELLED_INSTANCE_IMAGE_TAG,
                name,
                str(free_port()),
                str(tree.config_dir),
                str(link),
                str(tree.agent_tree),
            )
        )

        assert refused.returncode == 1
        assert len(refused.stderr.splitlines()) == 1
        assert str(link) in refused.stderr
        assert not _exists(docker_client, name)


def test_too_few_arguments_print_the_usage(docker_client: DockerClient) -> None:
    """Fewer than six arguments exit 2 with a usage line."""
    name = _instance_name()
    with removed_on_exit(docker_client, name):
        # With the two words of the label option, the script sees five.
        refused = run_start_script((LABELLED_INSTANCE_IMAGE_TAG, name, "8000"))

        assert refused.returncode == 2
        assert refused.stderr.startswith("usage: ")
        assert not _exists(docker_client, name)
