import secrets
import shlex
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
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
from docker.models.containers import Container
from harness import (
    FIXTURE_EMAIL,
    FIXTURE_NAME,
    GrantedCredentials,
    OtherUidRepo,
    RemotePair,
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
AUTH_SERVER_PATH = Path(__file__).with_name("auth_server.py")
AUTH_ALIAS = "auth"
AUTH_START_SECONDS = 30


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


@pytest.fixture
def run_as_other_uid(
    docker_client: DockerClient, fixture_image: str, other_uid_repo: OtherUidRepo
) -> Callable[[str], None]:
    """A runner of a shell script as uid 1000, with other_uid_repo's root bound.

    The runner raises docker's ContainerError when the script exits non-zero.
    """

    def run(script: str) -> None:
        _run_fixture(
            docker_client, fixture_image, other_uid_repo.root, OTHER_USER, script
        )

    return run


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


@pytest.fixture
def remote_pair(tmp_path: Path) -> RemotePair:
    """A bare remote, a clone work that pushed main, and a clone peer, in one root.

    work's origin is the plain path of the remote, and both clones carry an identity.
    """
    root = tmp_path / "pair-root"
    root.mkdir()
    pair = RemotePair(
        root=root,
        remote=root / "remote.git",
        work=root / "work",
        peer=root / "peer",
    )
    host_git(root, "init", "-q", "--bare", "-b", "main", str(pair.remote))
    host_git(root, "init", "-q", "-b", "main", str(pair.work))
    _set_identity(pair.work)
    (pair.work / "a.txt").write_text("one\n")
    host_git(pair.work, "add", "a.txt")
    host_git(pair.work, "commit", "-q", "-m", "first")
    host_git(pair.work, "remote", "add", "origin", str(pair.remote))
    host_git(pair.work, "push", "-q", "-u", "origin", "main")
    host_git(root, "clone", "-q", str(pair.remote), str(pair.peer))
    _set_identity(pair.peer)
    return pair


@pytest.fixture
def lfs_remote_pair(
    docker_client: DockerClient, fixture_image: str, tmp_path: Path
) -> RemotePair:
    """A remote pair whose work committed a 2 KiB big.bin tracked by git LFS.

    The fixture image builds it as uid 0, so the files are the host user's.
    peer is cloned with smudging skipped, so its big.bin is an LFS pointer.
    """
    root = tmp_path / "lfs-root"
    root.mkdir()
    pair = RemotePair(
        root=root,
        remote=root / "remote.git",
        work=root / "work",
        peer=root / "peer",
    )
    remote = shlex.quote(str(pair.remote))
    work = shlex.quote(str(pair.work))
    peer = shlex.quote(str(pair.peer))
    _run_fixture(
        docker_client,
        fixture_image,
        root,
        ROOT_USER,
        " && ".join(
            [
                f"git init -q --bare -b main {remote}",
                f"git init -q -b main {work}",
                f"cd {work}",
                "git lfs install --local",
                f"git config user.name {FIXTURE_NAME}",
                f"git config user.email {FIXTURE_EMAIL}",
                "head -c 2048 /dev/urandom > big.bin",
                "git lfs track big.bin",
                "git add .gitattributes big.bin",
                "git commit -q -m big",
                f"git remote add origin {remote}",
                "git lfs push origin main",
                "git push -q -u origin main",
                f"GIT_LFS_SKIP_SMUDGE=1 git clone -q {remote} {peer}",
                f"cd {peer}",
                "git lfs install --local",
            ]
        ),
    )
    return pair


@pytest.fixture
def auth_server(
    docker_client: DockerClient, plugin_manifest: Manifest, cage_network: str
) -> Iterator[Container]:
    """A server that answers 401 until the credential arrives, reached as auth:8080.

    It runs from the plugin image, which plugin_manifest builds, on the network
    every cage joins.
    """
    container = docker_client.containers.run(
        PLUGIN_IMAGE_TAG,
        ["python3", "-u", "-c", AUTH_SERVER_PATH.read_text()],
        detach=True,
        network=cage_network,
        networking_config={
            cage_network: docker_client.api.create_endpoint_config(aliases=[AUTH_ALIAS])
        },
        labels=TEST_LABELS,
    )
    try:
        _wait_for_ready(container)
        yield container
    finally:
        container.remove(force=True)


def _set_identity(repo: Path) -> None:
    """Give repo the fixture's commit identity in its own config."""
    host_git(repo, "config", "user.name", FIXTURE_NAME)
    host_git(repo, "config", "user.email", FIXTURE_EMAIL)


def _wait_for_ready(container: Container) -> None:
    """Return once the server's log holds READY.

    Raises:
        TimeoutError: READY did not appear within AUTH_START_SECONDS.
    """
    deadline = time.monotonic() + AUTH_START_SECONDS
    while b"READY" not in container.logs():
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"the auth server printed no READY in {AUTH_START_SECONDS}s; "
                f"its log: {container.logs()!r}"
            )
        time.sleep(0.1)
