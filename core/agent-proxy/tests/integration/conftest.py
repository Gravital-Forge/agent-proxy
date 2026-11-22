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
from docker.models.images import Image

from agent_proxy import egress, plugins, runner

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
PROBE_DIR = Path(__file__).parent.parent / "egress-probe"
PROBE_IMAGE_TAG = "apx-egress-probe:it"
VERIFIER_DIR = Path(__file__).parents[3] / "verifier"
VERIFIER_BUILD_TAG = "apx-verifier-build:it"
VERIFIER_IMAGE_TAG = "apx-verifier:it"
PINNED_VERIFIER_NAME = "apx-pinned-verifier"
# As with the runner image, the extra label keeps this build's image ID apart
# from the session verifier's, which the pinned_verifier fixture would otherwise
# force-remove with its own.
PINNED_VERIFIER_LABELS = TEST_LABELS | {"agent-proxy.test.fixture": "pinned-verifier"}
INSTANCE_CONTEXT = Path(__file__).parents[2]
INSTANCE_DOCKERFILE = Path(__file__).parents[3] / "docker" / "Dockerfile"
INSTANCE_BUILD_TAG = "apx-instance-build:it"
INSTANCE_IMAGE_TAG = "apx-instance:it"
LABELLED_INSTANCE_DIR = Path(__file__).parent.parent / "instance-image"
LABELLED_INSTANCE_IMAGE_TAG = "apx-instance-labelled:it"
PLUGINS_DIR = Path(__file__).parents[4] / "plugins"
GIT_PLUGIN_NAME = "apx-git-plugin"
GITHUB_PLUGIN_BUILD_TAG = "apx-instance-github-plugin:it"
GITHUB_STUB_NAME = "apx-github-stub"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Pass an empty run only while the directory holds no test module."""
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and not any(
        Path(__file__).parent.glob("test_*.py")
    ):
        session.exitstatus = pytest.ExitCode.OK


@pytest.fixture(autouse=True)
def _label_containers(monkeypatch: pytest.MonkeyPatch, verifier_image: str) -> None:
    monkeypatch.setattr(plugins, "CONTAINER_LABELS", TEST_LABELS)
    monkeypatch.setattr(runner, "CONTAINER_LABELS", TEST_LABELS)
    monkeypatch.setattr(runner, "VERIFIER_IMAGE", verifier_image)
    monkeypatch.setattr(egress, "LABELS", TEST_LABELS)


def remove_bridge(client: DockerClient) -> None:
    """Remove the egress bridge network, when it exists."""
    with contextlib.suppress(docker.errors.NotFound):
        client.networks.get(egress.BRIDGE_NETWORK).remove()


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
def probe_image(docker_client: DockerClient) -> Iterator[str]:
    """The tag of the curl probe image every egress test cage runs."""
    image, _ = docker_client.images.build(
        path=str(PROBE_DIR),
        tag=PROBE_IMAGE_TAG,
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        yield PROBE_IMAGE_TAG
    finally:
        docker_client.images.remove(image.id, force=True)


@pytest.fixture(scope="session")
def verifier_image(docker_client: DockerClient) -> Iterator[str]:
    """The tag of the verifier image, built from core/verifier/."""
    # The classic builder leaves the build stage's images untagged and
    # unlabelled; building that stage under its own tag lets teardown remove
    # them with it.
    build_stage, _ = docker_client.images.build(
        path=str(VERIFIER_DIR),
        target="build",
        tag=VERIFIER_BUILD_TAG,
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        image, _ = docker_client.images.build(
            path=str(VERIFIER_DIR),
            tag=VERIFIER_IMAGE_TAG,
            labels=TEST_LABELS,
            rm=True,
            forcerm=True,
        )
        try:
            yield VERIFIER_IMAGE_TAG
        finally:
            docker_client.images.remove(image.id, force=True)
    finally:
        docker_client.images.remove(build_stage.id, force=True)


@pytest.fixture(scope="session")
def instance_image(docker_client: DockerClient) -> Iterator[str]:
    """The tag of the instance image, built from core/docker/Dockerfile."""
    build_stage, _ = docker_client.images.build(
        path=str(INSTANCE_CONTEXT),
        dockerfile=str(INSTANCE_DOCKERFILE),
        target="build",
        tag=INSTANCE_BUILD_TAG,
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        image, _ = docker_client.images.build(
            path=str(INSTANCE_CONTEXT),
            dockerfile=str(INSTANCE_DOCKERFILE),
            tag=INSTANCE_IMAGE_TAG,
            labels=TEST_LABELS,
            rm=True,
            forcerm=True,
        )
        try:
            yield INSTANCE_IMAGE_TAG
        finally:
            docker_client.images.remove(image.id, force=True)
    finally:
        docker_client.images.remove(build_stage.id, force=True)


@pytest.fixture(scope="session")
def labelled_instance_image(
    docker_client: DockerClient, instance_image: str, verifier_image: str
) -> Iterator[str]:
    """The tag of the instance image whose processes label what they create."""
    # The instance's cages run the verifier image by its tag, so the fixture
    # holds that image for as long as this one.
    image, _ = docker_client.images.build(
        path=str(LABELLED_INSTANCE_DIR),
        tag=LABELLED_INSTANCE_IMAGE_TAG,
        buildargs={"INSTANCE_IMAGE": instance_image},
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        yield LABELLED_INSTANCE_IMAGE_TAG
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
        reference = _pinned_reference(docker_client, image, repository)
        for tag in image.tags:
            docker_client.images.remove(tag)
        with pytest.raises(docker.errors.ImageNotFound):
            docker_client.images.get(reference)
        yield reference
    finally:
        # The image is already gone when the test failed before it pulled.
        with contextlib.suppress(docker.errors.ImageNotFound):
            docker_client.images.remove(image.id, force=True)


@pytest.fixture
def pinned_verifier(docker_client: DockerClient, registry: str) -> Iterator[str]:
    """The pinned reference of a verifier image, pushed and absent from the daemon."""
    repository = f"{registry}/{PINNED_VERIFIER_NAME}"
    image, _ = docker_client.images.build(
        path=str(VERIFIER_DIR),
        tag=f"{repository}:{PLUGIN_TAG}",
        labels=PINNED_VERIFIER_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        reference = _pinned_reference(docker_client, image, repository)
        for tag in image.tags:
            docker_client.images.remove(tag)
        with pytest.raises(docker.errors.ImageNotFound):
            docker_client.images.get(reference)
        yield reference
    finally:
        with contextlib.suppress(docker.errors.ImageNotFound):
            docker_client.images.remove(image.id, force=True)


@pytest.fixture(scope="session")
def git_plugin(docker_client: DockerClient, registry: str) -> Iterator[str]:
    """The pinned reference of the git plugin, built from plugins/git and pushed."""
    repository = f"{registry}/{GIT_PLUGIN_NAME}"
    image, _ = docker_client.images.build(
        path=str(PLUGINS_DIR / "git"),
        tag=f"{repository}:{PLUGIN_TAG}",
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        yield _pinned_reference(docker_client, image, repository)
    finally:
        docker_client.images.remove(image.id, force=True)


@pytest.fixture(scope="session")
def github_stub_plugin(docker_client: DockerClient, registry: str) -> Iterator[str]:
    """The pinned reference of the github plugin with the stub gh, pushed."""
    plugin, _ = docker_client.images.build(
        path=str(PLUGINS_DIR / "github"),
        tag=GITHUB_PLUGIN_BUILD_TAG,
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        repository = f"{registry}/{GITHUB_STUB_NAME}"
        stub, _ = docker_client.images.build(
            path=str(PLUGINS_DIR / "github" / "tests" / "stub"),
            tag=f"{repository}:{PLUGIN_TAG}",
            buildargs={"PLUGIN_IMAGE": GITHUB_PLUGIN_BUILD_TAG},
            labels=TEST_LABELS,
            rm=True,
            forcerm=True,
        )
        try:
            yield _pinned_reference(docker_client, stub, repository)
        finally:
            docker_client.images.remove(stub.id, force=True)
    finally:
        docker_client.images.remove(plugin.id, force=True)


def _pinned_reference(client: DockerClient, image: Image, repository: str) -> str:
    """Push image to repository and return its repository@digest reference."""
    _push(client, repository)
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
    return reference


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
