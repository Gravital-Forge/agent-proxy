from datetime import timedelta
from pathlib import Path

import pytest
from agent_proxy.config import FileMount
from agent_proxy.plugins import Manifest, ProviderAction, parse_manifest
from github_app_plugin import app_jwt

PLUGIN_DIR = Path(__file__).parents[2]
MANIFEST_PATH = PLUGIN_DIR / "manifest.json"
DOCKERFILE_PATH = PLUGIN_DIR / "Dockerfile"
GITHUB_DOCKERFILE_PATH = PLUGIN_DIR.parent / "github" / "Dockerfile"


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    """The plugin's manifest.json, parsed as the core parses describe's output."""
    return parse_manifest(MANIFEST_PATH.read_bytes())


def test_manifest_parses_with_no_warning(manifest: Manifest) -> None:
    """The core parses manifest.json and warns of nothing."""
    assert manifest.warnings() == ()


def test_manifest_declares_one_provider_and_no_tool(manifest: Manifest) -> None:
    """The one action is installation_token, a provider, so no grant can name it."""
    assert (set(manifest.actions), dict(manifest.tools)) == (
        {"installation_token"},
        {},
    )


def test_installation_token_reads_the_key_as_a_file(manifest: Manifest) -> None:
    """The provider runs its executable for 30s with the App key at its fixed path."""
    assert manifest.actions["installation_token"] == ProviderAction(
        name="installation_token",
        credentials=(FileMount(file=app_jwt.KEY_PATH),),
        timeout=timedelta(seconds=30),
        executable="/plugin/bin/installation_token",
    )


def test_the_key_path_is_beneath_run_credentials() -> None:
    """The App key's path is the fixed one the README names."""
    assert app_jwt.KEY_PATH == "/run/credentials/github-app-key"


def test_dockerfile_links_the_executable_to_the_entry() -> None:
    """The image links /plugin/bin/installation_token to the one entry script."""
    assert (
        "ln -s /plugin/entry /plugin/bin/installation_token"
        in DOCKERFILE_PATH.read_text()
    )


def test_dockerfile_starts_from_the_github_plugins_pinned_alpine() -> None:
    """The image's base is the same digest-pinned Alpine as the github plugin's."""
    assert (
        DOCKERFILE_PATH.read_text().splitlines()[0]
        == GITHUB_DOCKERFILE_PATH.read_text().splitlines()[0]
    )


def test_dockerfile_installs_python_and_openssl() -> None:
    """The image adds Python 3 to run the script and openssl to sign the JWT."""
    assert "RUN apk add --no-cache python3 openssl\n" in DOCKERFILE_PATH.read_text()
