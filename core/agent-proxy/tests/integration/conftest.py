import contextlib
import time
from collections.abc import Iterator
from pathlib import Path

import docker
import docker.errors
import pytest
from docker import DockerClient

from agent_proxy import plugins

TEST_LABELS = {"agent-proxy.test": "1"}
REGISTRY_IMAGE = (
    "registry:3@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"
)
PLUGIN_DIR = Path(__file__).parent.parent / "test-plugin"
PLUGIN_NAME = "apx-test-plugin"
PLUGIN_TAG = "it"
PUSH_ATTEMPTS = 30
PUSH_RETRY_SECONDS = 0.5


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Pass an empty run only while the directory holds no test module."""
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and not any(
        Path(__file__).parent.glob("test_*.py")
    ):
        session.exitstatus = pytest.ExitCode.OK


@pytest.fixture(autouse=True)
def _label_describe_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "CONTAINER_LABELS", TEST_LABELS)


@pytest.fixture(scope="session")
def docker_client() -> Iterator[DockerClient]:
    """A client for the daemon DOCKER_HOST names."""
    client = docker.from_env()
    yield client
    client.close()


@pytest.fixture(scope="session")
def registry(docker_client: DockerClient) -> Iterator[str]:
    """The host and port of a registry container on the daemon."""
    # The image declares a volume at its storage path; a tmpfs there keeps
    # Docker from creating an anonymous volume, which would carry no label.
    container = docker_client.containers.run(
        REGISTRY_IMAGE,
        detach=True,
        labels=TEST_LABELS,
        ports={"5000/tcp": ("127.0.0.1", None)},
        tmpfs={"/var/lib/registry": ""},
    )
    try:
        container.reload()
        host_port = container.ports["5000/tcp"][0]["HostPort"]
        yield f"localhost:{host_port}"
    finally:
        container.remove(force=True)


@pytest.fixture
def pinned_plugin(docker_client: DockerClient, registry: str) -> Iterator[str]:
    """The pinned reference of the test plugin, pushed and absent from the daemon."""
    repository = f"{registry}/{PLUGIN_NAME}"
    image, _ = docker_client.images.build(
        path=str(PLUGIN_DIR),
        tag=f"{repository}:{PLUGIN_TAG}",
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        _push(docker_client, repository)
        image.reload()
        repo_digests = image.attrs["RepoDigests"]
        reference = next(
            (digest for digest in repo_digests if digest.startswith(f"{repository}@")),
            None,
        )
        if reference is None:
            pytest.fail(
                f"RepoDigests holds no digest for {repository!r}, got {repo_digests!r}"
            )
        for tag in image.tags:
            docker_client.images.remove(tag)
        with pytest.raises(docker.errors.ImageNotFound):
            docker_client.images.get(reference)
        yield reference
    finally:
        # The image is already gone when the test failed before it pulled.
        with contextlib.suppress(docker.errors.ImageNotFound):
            docker_client.images.remove(image.id, force=True)


def _push(client: DockerClient, repository: str) -> None:
    # A push error comes back in the progress text, not as an exception, and
    # the registry refuses pushes until it has started.
    progress = ""
    for _ in range(PUSH_ATTEMPTS):
        progress = client.images.push(repository, tag=PLUGIN_TAG)
        if "digest: sha256:" in progress:
            return
        time.sleep(PUSH_RETRY_SECONDS)
    pytest.fail(
        f"push of {repository}:{PLUGIN_TAG} printed no digest after "
        f"{PUSH_ATTEMPTS} attempts, last progress {progress!r}"
    )
