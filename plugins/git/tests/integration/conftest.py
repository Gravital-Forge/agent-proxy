import os
import secrets
import shlex
import shutil
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    GitHubRemote,
    GrantedCredentials,
    OtherUidRepo,
    RemotePair,
    RootMode,
    RunAction,
    RunInImage,
    host_git,
    lfs_pointer,
)

TEST_LABELS = {"agent-proxy.test": "1"}
PLUGIN_DIR = Path(__file__).parents[2]
PLUGIN_IMAGE_TAG = "apx-git-plugin:it"
PLUGIN_PYTHONPATH = "/plugin"
FIXTURE_IMAGE_DIR = Path(__file__).parents[1] / "fixture-image"
FIXTURE_IMAGE_TAG = "apx-git-fixture:it"
VERIFIER_DIR = Path(__file__).parents[4] / "core" / "verifier"
VERIFIER_BUILD_TAG = "apx-git-verifier-build:it"
VERIFIER_IMAGE_TAG = "apx-git-verifier:it"
MAX_CAGES = 4
ROOT_USER = "0:0"
OTHER_USER = "1000:1000"
AUTH_SERVER_PATH = Path(__file__).with_name("auth_server.py")
AUTH_ALIASES = ["github.com", "auth"]
GIT_HTTP_SERVER_PATH = Path(__file__).with_name("git_http_server.py")
GITHUB_ALIAS = "github.com"
SERVER_CERTIFICATE_NAME = "server-certificate.pem"
SERVER_KEY_NAME = "server-key.pem"
GIT_DAEMON_ALIAS = "git-remote"
SERVER_START_SECONDS = 30
COMMIT_CREATING_ACTIONS = frozenset({"commit", "merge", "pull", "stash"})
FIXTURE_IDENTITY = {"GIT_AUTHOR_NAME": FIXTURE_NAME, "GIT_AUTHOR_EMAIL": FIXTURE_EMAIL}


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
    """A runner of one plugin action in a cage, its credentials stored in tmp_path.

    A commit-creating action's grant env names the fixture's identity, which
    the caller's env overrides, since the cage reads no repository user.*.
    """
    store = CredentialStore(tmp_path / "credentials")
    overflow_dir = tmp_path / "overflow"
    overflow_dir.mkdir()
    cage_runner = CageRunner(docker_client, store, CageLimiter())

    def run(
        name: str,
        arguments: Mapping[str, object],
        roots: Sequence[Path],
        credentials: GrantedCredentials | None = None,
        env: Mapping[str, str] | None = None,
        mode: RootMode = "rw",
    ) -> Outcome:
        mounts: dict[str, EnvMount | FileMount] = {}
        for credential_name, (mount, value) in (credentials or {}).items():
            store.set(credential_name, value)
            mounts[credential_name] = mount
        identity = FIXTURE_IDENTITY if name in COMMIT_CREATING_ACTIONS else {}
        grant = Grant.model_validate(
            {
                "roots": [Root(path=str(root), mode=mode) for root in roots],
                "domains": [],
                "credentials": mounts,
                "env": {**identity, **(env or {})},
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
def run_in_plugin_image(
    docker_client: DockerClient, plugin_manifest: Manifest
) -> RunInImage:
    """A runner of a command in the plugin image, bypassing git-cage and the cage.

    The container runs as the image's root with no network, root bound at its
    own path and /plugin on PYTHONPATH, so a command can import git_plugin.
    The runner raises docker's ContainerError when the command exits non-zero.
    """

    def run(root: Path, command: Sequence[str]) -> str:
        output = docker_client.containers.run(
            PLUGIN_IMAGE_TAG,
            list(command),
            environment={"PYTHONPATH": PLUGIN_PYTHONPATH},
            mounts=[
                docker.types.Mount(target=str(root), source=str(root), type="bind")
            ],
            network_mode="none",
            labels=TEST_LABELS,
            remove=True,
        )
        return output.decode()

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
def remote_pair(
    docker_client: DockerClient, fixture_image: str, cage_network: str, tmp_path: Path
) -> Iterator[RemotePair]:
    """A bare remote, a clone work that pushed main, and a clone peer, in one root.

    A git daemon on the cage network serves the remote, and work's origin is
    its git:// URL, which only a cage reaches. peer's origin is the plain path
    of the remote, for the host's git. Both clones carry an identity.
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
    host_git(pair.work, "remote", "set-url", "origin", _daemon_url(pair))
    with _git_daemon(docker_client, fixture_image, cage_network, pair):
        yield pair


@pytest.fixture
def lfs_remote_pair(
    docker_client: DockerClient, fixture_image: str, cage_network: str, tmp_path: Path
) -> Iterator[RemotePair]:
    """A remote pair whose work committed a 2 KiB big.bin tracked by git LFS.

    The fixture image builds it as uid 0, so the files are the host user's.
    peer is cloned with smudging skipped, so its big.bin is an LFS pointer.
    A git daemon serves the remote's refs, and both clones' origin is its
    git:// URL. Each clone's untracked, excluded .lfsconfig sets lfs.url to
    the remote's file:// URL, so git LFS copies objects to and from the
    remote's lfs/objects with no server.
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
    daemon_url = shlex.quote(_daemon_url(pair))
    lfs_url = shlex.quote(f"file://{pair.remote}")
    point_at_daemon = [
        f"git remote set-url origin {daemon_url}",
        f"git config --file .lfsconfig lfs.url {lfs_url}",
        "echo .lfsconfig >> .git/info/exclude",
    ]
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
                *point_at_daemon,
                f"cd {peer}",
                "git lfs install --local",
                *point_at_daemon,
            ]
        ),
    )
    with _git_daemon(docker_client, fixture_image, cage_network, pair):
        yield pair


@pytest.fixture(scope="session")
def server_tls_dir(
    docker_client: DockerClient,
    fixture_image: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """A directory that holds the auth server's self-signed certificate and its key.

    The certificate names each of AUTH_ALIASES as a subject alternative name,
    so it is its own trust anchor for both.
    """
    tls_dir = tmp_path_factory.mktemp("server-tls")
    names = ",".join(f"DNS:{alias}" for alias in AUTH_ALIASES)
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-noenc",
        "-days",
        "1",
        "-subj",
        f"/CN={AUTH_ALIASES[0]}",
        "-addext",
        f"subjectAltName={names}",
        "-keyout",
        str(tls_dir / SERVER_KEY_NAME),
        "-out",
        str(tls_dir / SERVER_CERTIFICATE_NAME),
    ]
    _run_fixture(docker_client, fixture_image, tls_dir, ROOT_USER, shlex.join(command))
    return tls_dir


@pytest.fixture
def trust_auth_server(server_tls_dir: Path) -> Callable[[Path], dict[str, str]]:
    """A function that puts the auth server's certificate in root, for a cage to trust.

    It returns the grant env that names the copy in GIT_SSL_CAINFO, which git
    and git LFS both read. The copy lies under root, so a cage granted root
    reads it.
    """

    def trust(root: Path) -> dict[str, str]:
        certificate = root / SERVER_CERTIFICATE_NAME
        shutil.copyfile(server_tls_dir / SERVER_CERTIFICATE_NAME, certificate)
        return {"GIT_SSL_CAINFO": str(certificate)}

    return trust


@pytest.fixture
def auth_server(
    docker_client: DockerClient,
    plugin_manifest: Manifest,
    cage_network: str,
    server_tls_dir: Path,
) -> Iterator[Container]:
    """A server that answers 401 until the credential arrives, over TLS on port 443.

    It runs from the plugin image, which plugin_manifest builds, on the network
    every cage joins, reached under each of AUTH_ALIASES. A cage trusts its
    certificate through trust_auth_server.
    """
    tls_dir = str(server_tls_dir)
    container = docker_client.containers.run(
        PLUGIN_IMAGE_TAG,
        [
            "python3",
            "-u",
            "-c",
            AUTH_SERVER_PATH.read_text(),
            str(server_tls_dir / SERVER_CERTIFICATE_NAME),
            str(server_tls_dir / SERVER_KEY_NAME),
        ],
        detach=True,
        mounts=[
            docker.types.Mount(
                target=tls_dir, source=tls_dir, type="bind", read_only=True
            )
        ],
        network=cage_network,
        networking_config={
            cage_network: docker_client.api.create_endpoint_config(aliases=AUTH_ALIASES)
        },
        labels=TEST_LABELS,
    )
    try:
        _wait_for_log(container, b"READY")
        yield container
    finally:
        container.remove(force=True)


@pytest.fixture
def git_http_server(
    docker_client: DockerClient,
    fixture_image: str,
    cage_network: str,
    server_tls_dir: Path,
    tmp_path: Path,
) -> Iterator[GitHubRemote]:
    """A git server over TLS on port 443 that answers 401 until the credential arrives.

    It serves the bare listed-org/repo.git, whose HEAD is main, under
    GITHUB_ALIAS on the network every cage joins; a cage trusts its
    certificate through trust_auth_server. main holds a.txt, and big.bin, which
    .gitattributes gives to git LFS, committed as its pointer; task/b adds
    b.txt. No test requests this fixture and auth_server both, so the alias is
    never held twice.
    """
    projects = tmp_path / "git-projects"
    remote = GitHubRemote(
        repository=projects / "listed-org" / "repo.git",
        pointer=lfs_pointer(os.urandom(2048)),
    )
    _commit_github_remote(tmp_path / "git-work", remote)
    tls_dir = str(server_tls_dir)
    container = docker_client.containers.run(
        fixture_image,
        [
            "python3",
            "-u",
            "-c",
            GIT_HTTP_SERVER_PATH.read_text(),
            str(server_tls_dir / SERVER_CERTIFICATE_NAME),
            str(server_tls_dir / SERVER_KEY_NAME),
            str(projects),
        ],
        detach=True,
        mounts=[
            docker.types.Mount(
                target=tls_dir, source=tls_dir, type="bind", read_only=True
            ),
            docker.types.Mount(target=str(projects), source=str(projects), type="bind"),
        ],
        network=cage_network,
        networking_config={
            cage_network: docker_client.api.create_endpoint_config(
                aliases=[GITHUB_ALIAS]
            )
        },
        labels=TEST_LABELS,
    )
    try:
        _wait_for_log(container, b"READY")
        yield remote
    finally:
        container.remove(force=True)


def _commit_github_remote(work: Path, remote: GitHubRemote) -> None:
    """Fill remote's bare repository from a new repository at work.

    The host's git runs with no LFS filter, so big.bin's blob is the pointer
    text as written.
    """
    host_git(work.parent, "init", "-q", "--bare", "-b", "main", str(remote.repository))
    host_git(work.parent, "init", "-q", "-b", "main", str(work))
    _set_identity(work)
    (work / "a.txt").write_text("one\n")
    (work / ".gitattributes").write_text(
        "big.bin filter=lfs diff=lfs merge=lfs -text\n"
    )
    (work / "big.bin").write_text(remote.pointer)
    host_git(work, "add", ".")
    host_git(work, "commit", "-q", "-m", "first")
    host_git(work, "checkout", "-q", "-b", "task/b")
    (work / "b.txt").write_text("b\n")
    host_git(work, "add", "b.txt")
    host_git(work, "commit", "-q", "-m", "second")
    host_git(work, "push", "-q", str(remote.repository), "main", "task/b")


@contextmanager
def _git_daemon(
    client: DockerClient, image: str, network: str, pair: RemotePair
) -> Iterator[None]:
    """Serve pair's remote over git://, as GIT_DAEMON_ALIAS on network, until exit.

    The daemon sees only the remote, bound at its own path, and accepts an
    anonymous push.
    """
    remote = str(pair.remote)
    container = client.containers.run(
        image,
        [
            "git",
            "daemon",
            "--reuseaddr",
            "--verbose",
            "--export-all",
            "--enable=receive-pack",
            f"--base-path={pair.root}",
            remote,
        ],
        detach=True,
        mounts=[docker.types.Mount(target=remote, source=remote, type="bind")],
        network=network,
        networking_config={
            network: client.api.create_endpoint_config(aliases=[GIT_DAEMON_ALIAS])
        },
        labels=TEST_LABELS,
    )
    try:
        _wait_for_log(container, b"Ready to rumble")
        yield
    finally:
        container.remove(force=True)


def _daemon_url(pair: RemotePair) -> str:
    """Return the git:// URL at which _git_daemon serves pair's remote."""
    return f"git://{GIT_DAEMON_ALIAS}/{pair.remote.relative_to(pair.root)}"


def _set_identity(repo: Path) -> None:
    """Give repo the fixture's commit identity in its own config."""
    host_git(repo, "config", "user.name", FIXTURE_NAME)
    host_git(repo, "config", "user.email", FIXTURE_EMAIL)


def _wait_for_log(container: Container, marker: bytes) -> None:
    """Return once the server's log holds marker.

    Raises:
        TimeoutError: marker did not appear within SERVER_START_SECONDS.
    """
    deadline = time.monotonic() + SERVER_START_SECONDS
    while marker not in container.logs():
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"the server printed no {marker!r} in {SERVER_START_SECONDS}s; "
                f"its log: {container.logs()!r}"
            )
        time.sleep(0.1)
