"""Test keys, generated at test time with the host's openssl, and checks on them."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

OPENSSL = "/usr/bin/openssl"


@dataclass(frozen=True)
class GeneratedKey:
    """A key the test generated: its private key file and its public key file."""

    private: Path
    public: Path


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
    return GeneratedKey(private=private, public=public)


def verifies(public: Path, signing_input: bytes, signature: bytes, tmp: Path) -> bool:
    """Return whether signature is public's RS256 signature of signing_input."""
    signature_file = tmp / "signature.bin"
    signature_file.write_bytes(signature)
    completed = subprocess.run(  # noqa: S603 - fixed arguments the test chooses
        [
            OPENSSL,
            "dgst",
            "-sha256",
            "-verify",
            str(public),
            "-signature",
            str(signature_file),
        ],
        input=signing_input,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0
