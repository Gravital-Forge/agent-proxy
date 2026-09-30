import contextlib
import json
from collections.abc import Callable
from types import MappingProxyType
from unittest import mock

import docker.errors
import pytest
import requests

from agent_proxy import plugins
from agent_proxy.errors import ProxyError
from agent_proxy.plugins import (
    CONTAINER_LABELS,
    DESCRIBE_STDOUT_LIMIT,
    DESCRIBE_TIMEOUT_SECONDS,
    STDERR_EXCERPT_LIMIT,
    CommandAction,
    ManifestError,
    PluginImageError,
    check_image,
    describe,
    ensure_image,
    load_manifest,
)

IMAGE = "ghcr.io/example/echo@sha256:" + "a" * 64
ECHO_ACTION = {
    "kind": "command",
    "input_schema": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "additionalProperties": False,
    },
    "credentials": [],
    "timeout": "30s",
    "argv": ["echo"],
    "parameters": [{"name": "message", "as": "positional"}],
    "passthrough_args": False,
}
MANIFEST = json.dumps({"actions": {"echo": ECHO_ACTION}}).encode()
DOCKER_FAILURES = [
    docker.errors.DockerException("daemon said no"),
    docker.errors.APIError("daemon said no"),
    requests.exceptions.ConnectionError("daemon said no"),
]


def _client(
    *, status: int = 0, out: bytes = MANIFEST, err: bytes = b""
) -> mock.MagicMock:
    """Return a client whose describe container exits with status."""
    client = mock.MagicMock()
    container = client.containers.create.return_value
    container.wait.return_value = {"StatusCode": status}

    def logs(*, stdout: bool, stderr: bool) -> bytes:
        if stdout and not stderr:
            return out
        if stderr and not stdout:
            return err
        raise AssertionError(
            f"logs must read one stream, got stdout={stdout} stderr={stderr}"
        )

    container.logs.side_effect = logs
    return client


def _container(client: mock.MagicMock) -> mock.MagicMock:
    return client.containers.create.return_value


def _exiting_non_zero() -> mock.MagicMock:
    return _client(status=1, err=b"boom")


def _timing_out() -> mock.MagicMock:
    client = _client()
    _container(client).wait.side_effect = requests.exceptions.ConnectionError(
        "read timed out"
    )
    return client


def _over_the_cap() -> mock.MagicMock:
    return _client(out=b"x" * (DESCRIBE_STDOUT_LIMIT + 1))


def _failing_start() -> mock.MagicMock:
    client = _client()
    _container(client).start.side_effect = docker.errors.APIError("cannot start")
    return client


def _refusal(client: mock.MagicMock) -> str:
    """Run describe, expecting a PluginImageError, and return its message."""
    with pytest.raises(PluginImageError) as excinfo:
        describe(client, IMAGE)
    return str(excinfo.value)


def test_the_describe_limits_are_the_spec_s() -> None:
    """Thirty seconds, 1 MiB of stdout, 1 KiB of stderr, and no product labels."""
    assert DESCRIBE_TIMEOUT_SECONDS == 30
    assert DESCRIBE_STDOUT_LIMIT == 1024 * 1024
    assert STDERR_EXCERPT_LIMIT == 1024
    assert isinstance(CONTAINER_LABELS, MappingProxyType)
    assert dict(CONTAINER_LABELS) == {}


def test_a_plugin_image_error_is_a_proxy_error() -> None:
    """The admin command reports every image failure as a refusal."""
    assert issubclass(PluginImageError, ProxyError)


def test_describe_creates_a_hardened_container_and_never_runs_one() -> None:
    """Entrypoint describe, no network, read-only, no capabilities, no escalation."""
    client = _client()

    describe(client, IMAGE)

    client.containers.create.assert_called_once_with(
        IMAGE,
        entrypoint=["describe"],
        network_mode="none",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        labels=dict(CONTAINER_LABELS),
    )
    client.containers.run.assert_not_called()


def test_describe_labels_the_container_with_the_labels_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module constant is read when describe runs, so a test can patch it."""
    labels = {"agent-proxy.test": "1"}
    monkeypatch.setattr(plugins, "CONTAINER_LABELS", MappingProxyType(labels))
    client = _client()

    describe(client, IMAGE)

    assert client.containers.create.call_args.kwargs["labels"] == labels


def test_describe_starts_the_container_and_waits_at_most_the_timeout() -> None:
    """The container starts once, and wait is bounded by the describe timeout."""
    client = _client()

    describe(client, IMAGE)

    _container(client).start.assert_called_once_with()
    _container(client).wait.assert_called_once_with(timeout=DESCRIBE_TIMEOUT_SECONDS)


def test_describe_returns_stdout_on_exit_0() -> None:
    """A zero exit returns the container's stdout unchanged."""
    assert describe(_client(out=b"hello"), IMAGE) == b"hello"


def test_describe_refuses_a_non_zero_exit_with_the_status_and_stderr() -> None:
    """The stderr excerpt is quoted, so a multi-line stderr stays on one line."""
    message = _refusal(_client(status=3, err=b"boom\nsecond line"))

    assert IMAGE in message
    assert "status 3" in message
    assert repr("boom\nsecond line") in message
    assert "\n" not in message


def test_describe_keeps_only_the_first_kib_of_stderr() -> None:
    """The refusal quotes at most STDERR_EXCERPT_LIMIT bytes of stderr."""
    message = _refusal(_client(status=1, err=b"x" * STDERR_EXCERPT_LIMIT + b"TAIL"))

    assert "x" * STDERR_EXCERPT_LIMIT in message
    assert "TAIL" not in message


def test_describe_replaces_undecodable_stderr_bytes() -> None:
    """Stderr that is not UTF-8 is decoded with replacement characters."""
    message = _refusal(_client(status=1, err=b"bad \xff byte"))

    assert "bad � byte" in message


@pytest.mark.parametrize(
    "timeout",
    [
        requests.exceptions.ConnectionError("read timed out"),
        requests.exceptions.ReadTimeout("read timed out"),
    ],
)
def test_describe_refuses_a_timeout(timeout: Exception) -> None:
    """The SDK raises ConnectionError over a Unix socket, ReadTimeout elsewhere."""
    client = _client()
    _container(client).wait.side_effect = timeout

    message = _refusal(client)

    assert IMAGE in message
    assert "did not exit within 30 seconds" in message


def test_describe_refuses_stdout_over_the_cap() -> None:
    """Stdout one byte over the cap is refused, naming the image and the cap."""
    message = _refusal(_client(out=b"x" * (DESCRIBE_STDOUT_LIMIT + 1)))

    assert IMAGE in message
    assert str(DESCRIBE_STDOUT_LIMIT) in message


def test_describe_accepts_stdout_at_the_cap() -> None:
    """Stdout exactly at the cap is returned."""
    out = b"x" * DESCRIBE_STDOUT_LIMIT

    assert describe(_client(out=out), IMAGE) == out


@pytest.mark.parametrize(
    "make_client",
    [_client, _exiting_non_zero, _timing_out, _over_the_cap, _failing_start],
)
def test_describe_removes_the_container_whatever_happens(
    make_client: Callable[[], mock.MagicMock],
) -> None:
    """Every outcome, success or refusal, ends with a forced removal."""
    client = make_client()

    with contextlib.suppress(PluginImageError):
        describe(client, IMAGE)

    _container(client).remove.assert_called_once_with(force=True)


def test_describe_refuses_an_absent_image_without_pulling() -> None:
    """An absent image is refused as not present, and nothing is pulled."""
    client = _client()
    client.containers.create.side_effect = docker.errors.ImageNotFound("No such image")

    assert _refusal(client) == f"image {IMAGE!r} is not present"
    client.images.pull.assert_not_called()
    client.containers.run.assert_not_called()


@pytest.mark.parametrize("error", DOCKER_FAILURES)
def test_describe_refuses_a_docker_failure_on_create(error: Exception) -> None:
    """A Docker or transport error on create names the image and the error."""
    client = _client()
    client.containers.create.side_effect = error

    message = _refusal(client)

    assert IMAGE in message
    assert "daemon said no" in message


@pytest.mark.parametrize("error", DOCKER_FAILURES)
@pytest.mark.parametrize("call", ["start", "logs", "remove"])
def test_describe_refuses_a_docker_failure_on_the_container(
    call: str, error: Exception
) -> None:
    """An error from start, logs, or remove names the image and the error."""
    client = _client()
    getattr(_container(client), call).side_effect = error

    message = _refusal(client)

    assert IMAGE in message
    assert "daemon said no" in message


def test_describe_refuses_a_docker_failure_while_waiting() -> None:
    """A Docker error from wait that is not a timeout keeps the error's text."""
    client = _client()
    _container(client).wait.side_effect = docker.errors.APIError("daemon said no")

    message = _refusal(client)

    assert IMAGE in message
    assert "daemon said no" in message


def test_ensure_image_does_not_pull_a_present_image() -> None:
    """A present image is used as it is."""
    client = mock.MagicMock()

    ensure_image(client, IMAGE)

    client.images.get.assert_called_once_with(IMAGE)
    client.images.pull.assert_not_called()


def test_ensure_image_pulls_an_absent_image_by_its_reference() -> None:
    """An absent image is pulled by the reference it was asked for."""
    client = mock.MagicMock()
    client.images.get.side_effect = docker.errors.ImageNotFound("No such image")

    ensure_image(client, IMAGE)

    client.images.pull.assert_called_once_with(IMAGE)


@pytest.mark.parametrize(
    "error",
    [
        docker.errors.APIError("manifest unknown"),
        requests.exceptions.ConnectionError("manifest unknown"),
    ],
)
def test_ensure_image_refuses_a_failed_pull_naming_the_reference(
    error: Exception,
) -> None:
    """A failed pull is refused, naming the reference and the error."""
    client = mock.MagicMock()
    client.images.get.side_effect = docker.errors.ImageNotFound("No such image")
    client.images.pull.side_effect = error

    with pytest.raises(PluginImageError) as excinfo:
        ensure_image(client, IMAGE)

    message = str(excinfo.value)
    assert IMAGE in message
    assert "pulled" in message
    assert "manifest unknown" in message


@pytest.mark.parametrize("error", DOCKER_FAILURES)
def test_ensure_image_refuses_a_failed_lookup_without_pulling(
    error: Exception,
) -> None:
    """A failed lookup is refused, and nothing is pulled."""
    client = mock.MagicMock()
    client.images.get.side_effect = error

    with pytest.raises(PluginImageError) as excinfo:
        ensure_image(client, IMAGE)

    message = str(excinfo.value)
    assert IMAGE in message
    assert "daemon said no" in message
    client.images.pull.assert_not_called()


def test_load_manifest_pulls_an_absent_image_before_describing_it() -> None:
    """The image is made present before describe runs, then its output parsed."""
    client = _client()
    client.images.get.side_effect = docker.errors.ImageNotFound("No such image")

    manifest = load_manifest(client, IMAGE)

    client.images.pull.assert_called_once_with(IMAGE)
    names = [name for name, _, _ in client.mock_calls]
    assert names.index("images.pull") < names.index("containers.create")
    assert list(manifest.actions) == ["echo"]
    assert isinstance(manifest.actions["echo"], CommandAction)


def test_load_manifest_uses_a_present_image_as_it_is() -> None:
    """A present image is described without a pull."""
    client = _client()

    manifest = load_manifest(client, IMAGE)

    client.images.pull.assert_not_called()
    assert list(manifest.actions) == ["echo"]


def test_load_manifest_refuses_a_manifest_that_breaks_the_format() -> None:
    """Describe output that breaks the manifest format raises ManifestError."""
    with pytest.raises(ManifestError):
        load_manifest(_client(out=b"[]"), IMAGE)


def test_check_image_returns_the_manifest_without_pulling() -> None:
    """Checking a local image neither looks it up nor pulls it."""
    client = _client()

    manifest = check_image(client, IMAGE)

    assert list(manifest.actions) == ["echo"]
    client.images.get.assert_not_called()
    client.images.pull.assert_not_called()


def test_check_image_refuses_an_absent_image_without_pulling() -> None:
    """An absent image is refused, and nothing is pulled."""
    client = _client()
    client.containers.create.side_effect = docker.errors.ImageNotFound("No such image")

    with pytest.raises(PluginImageError, match="is not present"):
        check_image(client, IMAGE)

    client.images.pull.assert_not_called()


def test_check_image_refuses_a_manifest_that_breaks_the_format() -> None:
    """Describe output that breaks the manifest format raises ManifestError."""
    with pytest.raises(ManifestError):
        check_image(_client(out=b"[]"), IMAGE)
