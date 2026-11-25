import urllib.request

import pytest
from fake_transport import FakeTransport
from keys import GeneratedKey, generate_key

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@pytest.fixture(scope="session")
def pkcs1_key(tmp_path_factory: pytest.TempPathFactory) -> GeneratedKey:
    """A PKCS#1 key, the form GitHub issues an App's private key in."""
    key = generate_key(tmp_path_factory.mktemp("keys") / "pkcs1", traditional=True)
    assert key.private.read_text().startswith("-----BEGIN RSA PRIVATE KEY-----\n")
    return key


@pytest.fixture(scope="session")
def pkcs8_key(tmp_path_factory: pytest.TempPathFactory) -> GeneratedKey:
    """A PKCS#8 key, the form openssl genrsa writes by default."""
    key = generate_key(tmp_path_factory.mktemp("keys") / "pkcs8", traditional=False)
    assert key.private.read_text().startswith("-----BEGIN PRIVATE KEY-----\n")
    return key


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Put a FakeTransport in every opener, and clear the proxies."""
    fake = FakeTransport()
    build_opener = urllib.request.build_opener
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: build_opener(*handlers, fake),
    )
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return fake
