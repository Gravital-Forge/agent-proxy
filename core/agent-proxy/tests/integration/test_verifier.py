import secrets
from collections.abc import Sequence
from pathlib import Path

import docker.types
from conftest import TEST_LABELS
from docker import DockerClient

from agent_proxy.runner import (
    CONTAINER_USER,
    LOG_DRIVER,
    TMPFS_OPTIONS,
    TMPFS_PATH,
    VERIFIER_MOUNT,
    VERIFIER_PATH,
    VERIFIER_REFUSED_EXIT,
)

CAGE_WAIT_SECONDS = 60


def _root_args(root: Path, *, inode_offset: int = 0) -> list[str]:
    """The verifier's path, st_dev, and st_ino arguments for root, as on the host."""
    status = root.stat()
    return [str(root), str(status.st_dev), str(status.st_ino + inode_offset)]


def _make_root(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir()
    return root


def _run_cage(
    client: DockerClient,
    image: str,
    verifier_image: str,
    entrypoint: Sequence[str],
    roots: Sequence[Path] = (),
) -> tuple[int, bytes, bytes]:
    """Run a hardened cage to its end; return its exit code, stdout, and stderr."""
    container = client.containers.create(
        image,
        entrypoint=list(entrypoint),
        user=CONTAINER_USER,
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        tmpfs={TMPFS_PATH: TMPFS_OPTIONS},
        mounts=[
            *(
                docker.types.Mount(target=str(root), source=str(root), type="bind")
                for root in roots
            ),
            {
                "Type": "image",
                "Source": verifier_image,
                "Target": VERIFIER_MOUNT,
                "ReadOnly": True,
            },
        ],
        network_mode="none",
        labels=TEST_LABELS,
        log_config=docker.types.LogConfig(type=LOG_DRIVER, config={}),
    )
    try:
        container.start()
        exit_code = container.wait(timeout=CAGE_WAIT_SECONDS)["StatusCode"]
        stdout = container.logs(stdout=True, stderr=False)
        stderr = container.logs(stdout=False, stderr=True)
    finally:
        container.remove(force=True)
    return exit_code, stdout, stderr


def test_the_cage_sees_one_read_only_file(
    docker_client: DockerClient, runner_image: str, verifier_image: str
) -> None:
    """The mount holds verify alone, and the cage cannot write it."""
    nonce = secrets.token_hex(8)
    probe = f"ls -A {VERIFIER_MOUNT}; touch {VERIFIER_PATH} || echo refused"
    exit_code, stdout, _ = _run_cage(
        docker_client,
        runner_image,
        verifier_image,
        [VERIFIER_PATH, nonce, "--", "sh", "-c", probe],
    )
    assert exit_code == 0
    assert stdout == b"verify\nrefused\n"


def test_a_match_runs_the_action_unchanged(
    docker_client: DockerClient,
    runner_image: str,
    verifier_image: str,
    tmp_path: Path,
) -> None:
    """With the true pair, the action's exit code and streams come through."""
    nonce = secrets.token_hex(8)
    root = _make_root(tmp_path, "root")
    action = ["sh", "-c", "echo out; echo err >&2; exit 7"]
    result = _run_cage(
        docker_client,
        runner_image,
        verifier_image,
        [VERIFIER_PATH, nonce, *_root_args(root), "--", *action],
        [root],
    )
    assert result == (7, b"out\n", b"err\n")


def test_a_mismatch_refuses_before_the_action(
    docker_client: DockerClient,
    runner_image: str,
    verifier_image: str,
    tmp_path: Path,
) -> None:
    """A wrong inode stops the cage before the action writes its marker."""
    nonce = secrets.token_hex(8)
    checked = _make_root(tmp_path, "checked")
    written = _make_root(tmp_path, "written")
    marker = written / "marker"
    exit_code, _, stderr = _run_cage(
        docker_client,
        runner_image,
        verifier_image,
        [
            VERIFIER_PATH,
            nonce,
            *_root_args(checked, inode_offset=1),
            *_root_args(written),
            "--",
            "touch",
            str(marker),
        ],
        [checked, written],
    )
    assert exit_code == VERIFIER_REFUSED_EXIT
    assert stderr.startswith(f"{nonce} root ".encode())
    assert not marker.exists()


def test_a_missing_executable_is_an_exec_refusal(
    docker_client: DockerClient, runner_image: str, verifier_image: str
) -> None:
    """An action not on PATH ends the cage with an exec line."""
    nonce = secrets.token_hex(8)
    exit_code, _, stderr = _run_cage(
        docker_client,
        runner_image,
        verifier_image,
        [VERIFIER_PATH, nonce, "--", "no-such-command"],
    )
    assert exit_code == VERIFIER_REFUSED_EXIT
    assert stderr.startswith(f"{nonce} exec ".encode())


def test_a_scratch_cage_refuses_a_wrong_pair(
    docker_client: DockerClient, verifier_image: str, tmp_path: Path
) -> None:
    """The verifier checks roots in an image with no libc."""
    nonce = secrets.token_hex(8)
    root = _make_root(tmp_path, "root")
    exit_code, _, stderr = _run_cage(
        docker_client,
        verifier_image,
        verifier_image,
        [VERIFIER_PATH, nonce, *_root_args(root, inode_offset=1), "--", "/absent"],
        [root],
    )
    assert exit_code == VERIFIER_REFUSED_EXIT
    assert stderr.startswith(f"{nonce} root ".encode())


def test_a_scratch_cage_execs_the_action(
    docker_client: DockerClient, verifier_image: str, tmp_path: Path
) -> None:
    """Only the image's own /verify, run as the action, can print the inner line."""
    nonce = secrets.token_hex(8)
    root = _make_root(tmp_path, "root")
    action = ["/verify", "inner", "--", "/absent"]
    exit_code, _, stderr = _run_cage(
        docker_client,
        verifier_image,
        verifier_image,
        [VERIFIER_PATH, nonce, *_root_args(root), "--", *action],
        [root],
    )
    assert exit_code == VERIFIER_REFUSED_EXIT
    assert stderr.startswith(b"inner exec ")
