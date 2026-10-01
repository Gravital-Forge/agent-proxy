from conftest import TEST_LABELS
from docker import DockerClient

VENV_BIN = "/app/.venv/bin"


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
