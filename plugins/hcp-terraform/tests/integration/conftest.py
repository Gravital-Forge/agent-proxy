import json
import secrets
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import docker
import pytest
from agent_proxy import plugins, runner
from agent_proxy.config import EnvMount, Grant, Root
from agent_proxy.credentials import CredentialStore
from agent_proxy.egress import CageNetwork
from agent_proxy.plugins import Manifest
from agent_proxy.results import new_call_id
from agent_proxy.runner import CageCall, CageLimiter, CageRunner, Outcome
from docker import DockerClient
from docker.models.containers import Container
from harness import TFE_TOKEN, FakeApiLog, RunAction

TEST_LABELS = {"agent-proxy.test": "1"}
PLUGIN_DIR = Path(__file__).parents[2]
PLUGIN_IMAGE_TAG = "apx-hcp-terraform-plugin:it"
FAKE_API_DIR = Path(__file__).parents[1] / "fake-api"
FAKE_API_IMAGE_TAG = "apx-hcp-terraform-fake-api:it"
FAKE_API_ALIASES = ["app.terraform.io", "archivist.terraform.io"]
FAKE_API_START_SECONDS = 30
VERIFIER_DIR = Path(__file__).parents[4] / "core" / "verifier"
VERIFIER_BUILD_TAG = "apx-hcp-terraform-verifier-build:it"
VERIFIER_IMAGE_TAG = "apx-hcp-terraform-verifier:it"
MAX_CAGES = 4
CREDENTIAL_NAME = "hcp-terraform-token"
CREDENTIAL_VALUE = TFE_TOKEN.encode()


@pytest.fixture(autouse=True)
def _label_containers(monkeypatch: pytest.MonkeyPatch, verifier_image: str) -> None:
    monkeypatch.setattr(plugins, "CONTAINER_LABELS", TEST_LABELS)
    monkeypatch.setattr(runner, "CONTAINER_LABELS", TEST_LABELS)
    monkeypatch.setattr(runner, "VERIFIER_IMAGE", verifier_image)


@pytest.fixture(scope="session")
def docker_client() -> Iterator[DockerClient]:
    """A client for the daemon DOCKER_HOST names."""
    client = docker.from_env()
    yield client
    client.close()


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
def plugin_manifest(docker_client: DockerClient) -> Iterator[Manifest]:
    """The manifest of the plugin image.

    The image is built from plugins/hcp-terraform and described.
    """
    image, _ = docker_client.images.build(
        path=str(PLUGIN_DIR),
        tag=PLUGIN_IMAGE_TAG,
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        # A session fixture is set up before the autouse fixture that labels
        # the describe container.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(plugins, "CONTAINER_LABELS", TEST_LABELS)
            manifest = plugins.check_image(docker_client, PLUGIN_IMAGE_TAG)
        yield manifest
    finally:
        docker_client.images.remove(image.id, force=True)


@pytest.fixture(scope="session")
def fake_api_image(docker_client: DockerClient) -> Iterator[str]:
    """The tag of the fake HCP Terraform image, built from tests/fake-api/.

    It builds without the cache, so its certificates, which last one day, are
    always new.
    """
    image, _ = docker_client.images.build(
        path=str(FAKE_API_DIR),
        tag=FAKE_API_IMAGE_TAG,
        labels=TEST_LABELS,
        nocache=True,
        rm=True,
        forcerm=True,
    )
    try:
        yield FAKE_API_IMAGE_TAG
    finally:
        docker_client.images.remove(image.id, force=True)


@pytest.fixture
def run_action(
    docker_client: DockerClient,
    plugin_manifest: Manifest,
    fake_api_network: str,
    fake_api_ca: Path,
    tmp_path: Path,
) -> RunAction:
    """A runner of one plugin action in a cage on the fake's network.

    The cage trusts the fake's CA through SSL_CERT_FILE, reads it from a
    read-only root, and holds the stored test credential as TFE_TOKEN.
    """
    store = CredentialStore(tmp_path / "credentials")
    store.set(CREDENTIAL_NAME, CREDENTIAL_VALUE)
    overflow_dir = tmp_path / "overflow"
    overflow_dir.mkdir()
    cage_runner = CageRunner(docker_client, store, CageLimiter())
    network = CageNetwork(
        name=fake_api_network, environment={"SSL_CERT_FILE": str(fake_api_ca)}
    )

    def run(
        name: str, arguments: Mapping[str, object], *, env: Mapping[str, str]
    ) -> Outcome:
        grant = Grant(
            roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
            domains=[],
            credentials={CREDENTIAL_NAME: EnvMount(env="TFE_TOKEN")},
            env=dict(env),
        )
        return cage_runner.run(
            CageCall(
                call_id=new_call_id(),
                image=PLUGIN_IMAGE_TAG,
                action=plugin_manifest.actions[name],
                grant=grant,
                arguments=arguments,
                network=network,
                overflow_dir=str(overflow_dir),
                max_cages=MAX_CAGES,
            )
        )

    return run


@pytest.fixture
def fake_api_container(
    docker_client: DockerClient, fake_api_image: str
) -> Iterator[Container]:
    """The fake, running on an internal network of its own under FAKE_API_ALIASES.

    Each test gets a new one, so its log holds only that test's requests.
    """
    network = docker_client.networks.create(
        f"apx-hcp-terraform-fake-{secrets.token_hex(4)}",
        internal=True,
        labels=TEST_LABELS,
    )
    try:
        container = docker_client.containers.run(
            fake_api_image,
            detach=True,
            network=network.name,
            networking_config={
                network.name: docker_client.api.create_endpoint_config(
                    aliases=FAKE_API_ALIASES
                )
            },
            labels=TEST_LABELS,
        )
        try:
            _wait_for_serving(container)
            yield container
        finally:
            container.remove(force=True)
    finally:
        network.remove()


@pytest.fixture
def fake_api_network(fake_api_container: Container) -> str:
    """The name of the internal network on which the fake answers as both hosts."""
    (name,) = fake_api_container.attrs["NetworkSettings"]["Networks"]
    return name


@pytest.fixture
def fake_api_log(fake_api_container: Container) -> FakeApiLog:
    """A reader of the fake's log: each line that begins with {, decoded as JSON."""

    def read() -> list[dict[str, object]]:
        lines = fake_api_container.logs().decode().splitlines()
        return [json.loads(line) for line in lines if line.startswith("{")]

    return read


@pytest.fixture
def fake_api_ca(
    docker_client: DockerClient, fake_api_image: str, tmp_path: Path
) -> Path:
    """The fake's test CA certificate, copied from its image to tmp_path/pki/ca.crt."""
    certificate = docker_client.containers.run(
        fake_api_image,
        ["cat", "/pki/ca.crt"],
        network_mode="none",
        labels=TEST_LABELS,
        remove=True,
    )
    ca_file = tmp_path / "pki" / "ca.crt"
    ca_file.parent.mkdir()
    ca_file.write_bytes(certificate)
    return ca_file


def _wait_for_serving(container: Container) -> None:
    """Return once the fake's log holds serving.

    Raises:
        TimeoutError: serving did not appear within FAKE_API_START_SECONDS.
    """
    deadline = time.monotonic() + FAKE_API_START_SECONDS
    while b"serving" not in container.logs():
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"the fake api printed no serving in {FAKE_API_START_SECONDS}s; "
                f"its log: {container.logs()!r}"
            )
        time.sleep(0.1)
