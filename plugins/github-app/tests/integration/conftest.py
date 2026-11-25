import json
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path

import docker
import pytest
from agent_proxy import plugins, runner
from agent_proxy.config import FileMount, Grant, Root
from agent_proxy.credentials import CredentialStore
from agent_proxy.egress import CageNetwork
from agent_proxy.plugins import Manifest
from agent_proxy.results import new_call_id
from agent_proxy.runner import CageCall, CageLimiter, CageRunner
from docker import DockerClient
from docker.models.containers import Container
from harness import GeneratedKey, Provide, Provided, generate_key

TEST_LABELS = {"agent-proxy.test": "1"}
PLUGIN_DIR = Path(__file__).parents[2]
PLUGIN_IMAGE_TAG = "apx-github-app-plugin:it"
FAKE_API_DIR = Path(__file__).parents[1] / "fake-api"
FAKE_API_IMAGE_TAG = "apx-github-app-fake-api:it"
FAKE_API_ALIASES = ["api.github.com"]
FAKE_API_START_SECONDS = 30
VERIFIER_DIR = Path(__file__).parents[4] / "core" / "verifier"
VERIFIER_BUILD_TAG = "apx-github-app-verifier-build:it"
VERIFIER_IMAGE_TAG = "apx-github-app-verifier:it"
MAX_CAGES = 4
ACTION = "installation_token"
CREDENTIAL_NAME = "github-app-key"
KEY_PATH = "/run/credentials/github-app-key"


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
    """The manifest of the plugin image, built from plugins/github-app and described."""
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
    """The tag of the fake api.github.com image, built from tests/fake-api/.

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


@pytest.fixture(scope="session")
def fake_api_ca(
    docker_client: DockerClient,
    fake_api_image: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """The fake's test CA certificate, copied from its image to a pki directory."""
    certificate = docker_client.containers.run(
        fake_api_image,
        ["cat", "/pki/ca.crt"],
        network_mode="none",
        labels=TEST_LABELS,
        remove=True,
    )
    ca_file = tmp_path_factory.mktemp("pki") / "ca.crt"
    ca_file.write_bytes(certificate)
    return ca_file


@pytest.fixture(scope="session")
def pkcs1_key(tmp_path_factory: pytest.TempPathFactory) -> GeneratedKey:
    """A PKCS#1 key from openssl genrsa -traditional, the form GitHub issues."""
    key = generate_key(tmp_path_factory.mktemp("keys") / "pkcs1", traditional=True)
    assert key.private.startswith(b"-----BEGIN RSA PRIVATE KEY-----\n")
    return key


@pytest.fixture(scope="session")
def pkcs8_key(tmp_path_factory: pytest.TempPathFactory) -> GeneratedKey:
    """A PKCS#8 key from openssl genrsa."""
    key = generate_key(tmp_path_factory.mktemp("keys") / "pkcs8", traditional=False)
    assert key.private.startswith(b"-----BEGIN PRIVATE KEY-----\n")
    return key


@pytest.fixture
def provide(
    docker_client: DockerClient,
    plugin_manifest: Manifest,
    fake_api_image: str,
    fake_api_ca: Path,
    tmp_path: Path,
) -> Iterator[Provide]:
    """A runner of installation_token in a cage, each run against a new fake.

    The cage sits on the fake's internal network, trusts the fake's CA
    through SSL_CERT_FILE, and reads it from a read-only root.
    """
    overflow_dir = tmp_path / "overflow"
    overflow_dir.mkdir()

    with ExitStack() as cleanup:

        def run(
            *,
            signing_key: GeneratedKey,
            trusted_key: GeneratedKey,
            env: Mapping[str, str],
        ) -> Provided:
            container = cleanup.enter_context(
                _fake_api(docker_client, fake_api_image, trusted_key)
            )
            call_id = new_call_id()
            store = CredentialStore(tmp_path / f"credentials-{call_id}")
            store.set(CREDENTIAL_NAME, signing_key.private)
            (network_name,) = container.attrs["NetworkSettings"]["Networks"]
            outcome = CageRunner(docker_client, store, CageLimiter()).run(
                CageCall(
                    call_id=call_id,
                    image=PLUGIN_IMAGE_TAG,
                    action=plugin_manifest.actions[ACTION],
                    grant=Grant(
                        roots=[Root(path=str(fake_api_ca.parent), mode="ro")],
                        domains=[],
                        credentials={CREDENTIAL_NAME: FileMount(file=KEY_PATH)},
                        env=dict(env),
                    ),
                    arguments={},
                    network=CageNetwork(
                        name=network_name,
                        environment={"SSL_CERT_FILE": str(fake_api_ca)},
                    ),
                    overflow_dir=str(overflow_dir),
                    max_cages=MAX_CAGES,
                )
            )
            return Provided(outcome=outcome, log=_log(container))

        yield run


@contextmanager
def _fake_api(
    client: DockerClient, image: str, trusted_key: GeneratedKey
) -> Iterator[Container]:
    """Yield the fake, trusting trusted_key, on an internal network of its own.

    Each run gets a new one, so its log holds only that run's requests.
    """
    network = client.networks.create(
        f"apx-github-app-fake-{secrets.token_hex(4)}",
        internal=True,
        labels=TEST_LABELS,
    )
    try:
        container = client.containers.run(
            image,
            detach=True,
            network=network.name,
            networking_config={
                network.name: client.api.create_endpoint_config(
                    aliases=FAKE_API_ALIASES
                )
            },
            environment={"TRUSTED_PUBLIC_KEY": trusted_key.public.decode()},
            labels=TEST_LABELS,
        )
        try:
            _wait_for_serving(container)
            yield container
        finally:
            container.remove(force=True)
    finally:
        network.remove()


def _log(container: Container) -> list[dict[str, object]]:
    """Return each line of the fake's log that begins with {, decoded as JSON."""
    lines = container.logs().decode().splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


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
