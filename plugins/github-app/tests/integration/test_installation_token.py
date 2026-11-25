import json

import pytest
from harness import GeneratedKey, Provide

APP_ID = "12345"
INSTALLATION_ID = "42"
IDS = {"GITHUB_APP_ID": APP_ID, "GITHUB_INSTALLATION_ID": INSTALLATION_ID}
TOKEN_PATH = "/app/installations/42/access_tokens"
PREFIX = "github-app plugin failed: "


@pytest.mark.parametrize("key_fixture", ["pkcs1_key", "pkcs8_key"])
def test_a_trusted_key_gives_the_token_and_its_expiry(
    provide: Provide, request: pytest.FixtureRequest, key_fixture: str
) -> None:
    """A key the fake trusts, in either form, prints the issued token and expiry."""
    key: GeneratedKey = request.getfixturevalue(key_fixture)

    provided = provide(signing_key=key, trusted_key=key, env=IDS)

    [logged] = provided.log
    assert provided.outcome.content["exit_code"] == 0, provided.outcome.content
    stdout = provided.outcome.content["stdout"]
    assert isinstance(stdout, bytes)
    assert json.loads(stdout) == {
        "value": logged["issued"]["token"],
        "expires_at": logged["issued"]["expires_at"],
    }


def test_the_request_is_the_apps_signed_post(
    provide: Provide, pkcs1_key: GeneratedKey
) -> None:
    """One POST reaches api.github.com with GitHub's headers and the App's JWT."""
    provided = provide(signing_key=pkcs1_key, trusted_key=pkcs1_key, env=IDS)

    [logged] = provided.log
    claims = logged["jwt_claims"]
    assert isinstance(claims, dict)
    assert {
        name: logged[name]
        for name in (
            "method",
            "host",
            "path",
            "accept",
            "api_version",
            "user_agent",
            "jwt_header",
            "signature_valid",
        )
    } == {
        "method": "POST",
        "host": "api.github.com",
        "path": TOKEN_PATH,
        "accept": "application/vnd.github+json",
        "api_version": "2022-11-28",
        "user_agent": "agent-proxy-github-app-plugin",
        "jwt_header": {"alg": "RS256", "typ": "JWT"},
        "signature_valid": True,
    }
    assert (claims["iss"], claims["exp"] - claims["iat"]) == (APP_ID, 600)


def test_a_key_the_fake_does_not_trust_gives_githubs_message(
    provide: Provide, pkcs1_key: GeneratedKey, pkcs8_key: GeneratedKey
) -> None:
    """A JWT signed by another key exits 1 with the 401 and GitHub's message."""
    provided = provide(signing_key=pkcs8_key, trusted_key=pkcs1_key, env=IDS)

    assert provided.outcome.content == {
        "exit_code": 1,
        "stdout": b"",
        "stderr": (
            f"{PREFIX}POST {TOKEN_PATH} answered 401: "
            "A JSON web token could not be decoded\n"
        ),
    }
    assert [logged["signature_valid"] for logged in provided.log] == [False]


def test_a_missing_installation_id_fails_with_no_request(
    provide: Provide, pkcs1_key: GeneratedKey
) -> None:
    """With no GITHUB_INSTALLATION_ID, the run exits 1 and sends nothing."""
    provided = provide(
        signing_key=pkcs1_key, trusted_key=pkcs1_key, env={"GITHUB_APP_ID": APP_ID}
    )

    assert provided.outcome.content == {
        "exit_code": 1,
        "stdout": b"",
        "stderr": (
            f"{PREFIX}GITHUB_INSTALLATION_ID is not set; "
            "the provided credential's env sets it\n"
        ),
    }
    assert provided.log == []
