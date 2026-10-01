import secrets
import shlex
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import docker
import docker.types
import pytest
from agent_proxy import plugins, runner
from agent_proxy.config import EnvMount, FileMount, Grant, Root
from agent_proxy.credentials import CredentialStore
from agent_proxy.egress import CageNetwork
from agent_proxy.plugins import Manifest
from agent_proxy.results import new_call_id
from agent_proxy.runner import CageCall, CageLimiter, CageRunner, Outcome
from docker import DockerClient
from harness import (
    FIXTURE_EMAIL,
    FIXTURE_NAME,
    GrantedCredentials,
    OtherUidRepo,
    RunAction,
    host_git,
)

TEST_LABELS = {"agent-proxy.test": "1"}
PLUGIN_DIR = Path(__file__).parents[2]
PLUGIN_IMAGE_TAG = "apx-git-plugin:it"
FIXTURE_IMAGE_DIR = Path(__file__).parents[1] / "fixture-image"
FIXTURE_IMAGE_TAG = "apx-git-fixture:it"
VERIFIER_DIR = Path(__file__).parents[4] / "core" / "verifier"
VERIFIER_BUILD_TAG = "apx-git-verifier-build:it"
VERIFIER_IMAGE_TAG = "apx-git-verifier:it"
MAX_CAGES = 4
ROOT_USER = "0:0"
OTHER_USER = "1000:1000"


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
    """The manifest of the plugin image, built from plugins/git and described."""
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
def fixture_image(docker_client: DockerClient) -> Iterator[str]:
    """The tag of the image that builds the other-uid repository."""
    image, _ = docker_client.images.build(
        path=str(FIXTURE_IMAGE_DIR),
        tag=FIXTURE_IMAGE_TAG,
        labels=TEST_LABELS,
        rm=True,
        forcerm=True,
    )
    try:
        yield FIXTURE_IMAGE_TAG
    finally:
        docker_client.images.remove(image.id, force=True)


@pytest.fixture(scope="session")
def cage_network(docker_client: DockerClient) -> Iterator[str]:
    """The name of the internal network every cage in these tests joins."""
    network = docker_client.networks.create(
        f"apx-git-it-{secrets.token_hex(4)}", internal=True, labels=TEST_LABELS
    )
    try:
        yield network.name
    finally:
        network.remove()


@pytest.fixture
def run_action(
    docker_client: DockerClient,
    plugin_manifest: Manifest,
    cage_network: str,
    tmp_path: Path,
) -> RunAction:
    """A runner of one plugin action in a cage, its credentials stored in tmp_path."""
    store = CredentialStore(tmp_path / "credentials")
    overflow_dir = tmp_path / "overflow"
    overflow_dir.mkdir()
    cage_runner = CageRunner(docker_client, store, CageLimiter())

    def run(
        name: str,
        arguments: Mapping[str, object],
        roots: Sequence[Path],
        credentials: GrantedCredentials | None = None,
    ) -> Outcome:
        mounts: dict[str, EnvMount | FileMount] = {}
        for credential_name, (mount, value) in (credentials or {}).items():
            store.set(credential_name, value)
            mounts[credential_name] = mount
        grant = Grant.model_validate(
            {
                "roots": [Root(path=str(root), mode="rw") for root in roots],
                "domains": [],
                "credentials": mounts,
            }
        )
        return cage_runner.run(
            CageCall(
                call_id=new_call_id(),
                image=PLUGIN_IMAGE_TAG,
                action=plugin_manifest.actions[name],
                grant=grant,
                arguments=arguments,
                network=CageNetwork(name=cage_network, environment={}),
                overflow_dir=str(overflow_dir),
                max_cages=MAX_CAGES,
            )
        )

    return run


@pytest.fixture
def host_repo(tmp_path: Path) -> Path:
    """A repository the host user owns, with a bare origin beside it in its root."""
    root = tmp_path / "root"
    root.mkdir()
    repo = root / "repo"
    origin = root / "origin.git"
    host_git(root, "init", "-q", "-b", "main", str(repo))
    host_git(root, "init", "-q", "--bare", str(origin))
    host_git(repo, "config", "user.name", FIXTURE_NAME)
    host_git(repo, "config", "user.email", FIXTURE_EMAIL)
    (repo / "a.txt").write_text("one\n")
    host_git(repo, "add", "a.txt")
    host_git(repo, "commit", "-q", "-m", "first")
    (repo / "a.txt").write_text("two\n")
    host_git(repo, "commit", "-q", "-am", "second")
    host_git(repo, "tag", "v1")
    host_git(repo, "remote", "add", "origin", str(origin))
    return repo


@pytest.fixture
def other_uid_repo(
    docker_client: DockerClient, fixture_image: str, tmp_path: Path
) -> Iterator[OtherUidRepo]:
    """A repository and linked worktree uid 1000 owns, in a root the host user owns.

    The ACL lets the cage, uid 0 with no capabilities, write what it does not
    own, so only safe.directory stands between the cage and the repository.
    """
    root = tmp_path / "other-root"
    root.mkdir()
    fixture = OtherUidRepo(root=root, repo=root / "repo", worktree=root / "wt")
    repo = shlex.quote(str(fixture.repo))
    worktree = shlex.quote(str(fixture.worktree))
    _run_fixture(
        docker_client,
        fixture_image,
        root,
        ROOT_USER,
        f"mkdir {repo} {worktree} && chown {OTHER_USER} {repo} {worktree}",
    )
    try:
        _run_fixture(
            docker_client,
            fixture_image,
            root,
            OTHER_USER,
            " && ".join(
                [
                    f"cd {repo}",
                    "git init -q -b main",
                    f"git config user.name {FIXTURE_NAME}",
                    f"git config user.email {FIXTURE_EMAIL}",
                    "echo one > a.txt",
                    "git add a.txt",
                    "git commit -q -m first",
                    "echo two > a.txt",
                    "git commit -q -am second",
                    "git tag v1",
                    f"git worktree add -q -b task/wt {worktree}",
                    f"setfacl -R -m u:0:rwX,d:u:0:rwX,d:u:1000:rwX {repo} {worktree}",
                ]
            ),
        )
        yield fixture
    finally:
        _run_fixture(
            docker_client, fixture_image, root, ROOT_USER, f"rm -rf {repo} {worktree}"
        )


def _run_fixture(
    client: DockerClient, image: str, root: Path, user: str, script: str
) -> None:
    """Run script in the fixture image as user, with root bound at its own path."""
    client.containers.run(
        image,
        ["sh", "-c", script],
        user=user,
        mounts=[docker.types.Mount(target=str(root), source=str(root), type="bind")],
        network_mode="none",
        labels=TEST_LABELS,
        remove=True,
    )
