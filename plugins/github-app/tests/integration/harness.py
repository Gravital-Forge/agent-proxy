"""What the github-app plugin's integration tests share with their fixtures."""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_proxy.runner import Outcome

OPENSSL = "/usr/bin/openssl"


@dataclass(frozen=True)
class GeneratedKey:
    """A key the test generated: its private key PEM and its public key PEM."""

    private: bytes
    public: bytes


def generate_key(directory: Path, *, traditional: bool) -> GeneratedKey:
    """Generate a 2048-bit RSA key in directory, as PKCS#1 when traditional."""
    directory.mkdir()
    private = directory / "key.pem"
    public = directory / "public.pem"
    form = ["-traditional"] if traditional else []
    subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [OPENSSL, "genrsa", *form, "-out", str(private), "2048"],
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [OPENSSL, "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
        capture_output=True,
    )
    return GeneratedKey(private=private.read_bytes(), public=public.read_bytes())


@dataclass(frozen=True)
class Provided:
    """What one provider run gave: the cage's outcome, and the fake's log after it."""

    outcome: Outcome
    log: list[dict[str, object]]


class Provide(Protocol):
    def __call__(
        self,
        *,
        signing_key: GeneratedKey,
        trusted_key: GeneratedKey,
        env: Mapping[str, str],
    ) -> Provided:
        """Run installation_token in a cage against a new fake.

        The fake trusts trusted_key. The cage holds signing_key as the stored
        credential github-app-key at the action's file path, and env.
        """
