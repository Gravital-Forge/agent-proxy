import contextlib
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import docker
import docker.errors
import pytest
from docker import DockerClient
from docker.models.containers import Container

from agent_proxy import plugins, runner

TEST_LABELS = {"agent-proxy.test": "1"}
REGISTRY_IMAGE = (
    "registry:3@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"
)
PLUGIN_DIR = Path(__file__).parent.parent / "test-plugin"
PLUGIN_NAME = "apx-test-plugin"
PLUGIN_TAG = "it"
PUSH_ATTEMPTS = 30
PUSH_RETRY_SECONDS = 0.5
RUNNER_IMAGE_TAG = f"{PLUGIN_NAME}:runner"
# The extra label gives this build its own image ID, apart from the image the
# pinned_plugin fixture force-removes by ID.
RUNNER_IMAGE_LABELS = TEST_LABELS | {"agent-proxy.test.fixture": "runner"}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Pass an empty run only while the directory holds no test module."""
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and not any(
        Path(__file__).parent.glob("test_*.py")
    ):
        session.exitstatus = pytest.ExitCode.OK


@pytest.fixture(autouse=True)
def _label_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "CONTAINER_LABELS", TEST_LABELS)
    monkeypatch.setattr(runner, "CONTAINER_LABELS", TEST_LABELS)


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
def labelled_containers(
    docker_client: DockerClient,
) -> Callable[[], list[Container]]:
    """A lister of the labelled containers, the session registry excluded."""

    def listed() -> list[Container]:
        return [
            container
            for container in docker_client.containers.list(
                all=True, filters={"label": "agent-proxy.test=1"}
            )
            if container.attrs["Config"]["Image"] != REGISTRY_IMAGE
        ]

    return listed


@pytest.fixture(scope="session")
def runner_image(docker_client: DockerClient) -> Iterator[str]:
    """The tag of the test plugin image, built locally for the cage tests."""
    image, _ = docker_client.images.build(
        path=str(PLUGIN_DIR),
        tag=RUNNER_IMAGE_TAG,
        labels=RUNNER_IMAGE_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        yield RUNNER_IMAGE_TAG
    finally:
        docker_client.images.remove(image.id, force=True)


@pytest.fixture(scope="session")
def cage_network(docker_client: DockerClient) -> Iterator[str]:
    """The name of the internal network every cage in these tests joins."""
    network = docker_client.networks.create(
        f"apx-it-cages-{secrets.token_hex(4)}", internal=True, labels=TEST_LABELS
    )
    try:
        yield network.name
    finally:
        network.remove()


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
