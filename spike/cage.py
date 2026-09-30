# /// script
# requires-python = ">=3.12"
# dependencies = ["docker==7.2.0"]
# ///
"""Cage hardening checks and cage start cost on the daemon DOCKER_HOST names."""

import contextlib
import statistics
import time

import docker
from docker.errors import NotFound

IMAGE = "apx-spike-tools"
HARDEN_NAME = "apx-spike-harden"
COST_NAME = "apx-spike-cost"
COST_NETWORK = "apx-spike-net-cost"
WARMUP_CALLS = 3
TIMED_CALLS = 30
CAGE_TMP = "/tmp"  # noqa: S108 — the cage's own tmpfs, not a host path
CAGE_FLAGS: dict[str, object] = {
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges"],
    "read_only": True,
    "tmpfs": {CAGE_TMP: "rw,size=64m"},
    "environment": {"HOME": CAGE_TMP},
}
HARDEN_PROBE = (
    "if touch /probe 2>/dev/null; then echo H1=written; else echo H1=refused; fi; "
    "if touch /tmp/probe 2>/dev/null; then echo H2=written; else echo H2=refused; fi; "
    'awk \'/^CapEff:/ {print "H3=" $2} /^NoNewPrivs:/ {print "H4=" $2}\' '
    "/proc/self/status"
)
HARDEN_EXPECTED = {
    "H1": "refused",
    "H2": "written",
    "H3": "0000000000000000",
    "H4": "1",
}


def print_check(check_id: str, expected: str, observed: str) -> None:
    """Print one CHECK line, PASS when observed equals expected."""
    verdict = "PASS" if observed == expected else "FAIL"
    print(
        f"CHECK {check_id} expected={expected} observed={observed} {verdict}",
        flush=True,
    )


def remove_leftovers(client: docker.DockerClient) -> None:
    """Remove this script's containers and cost network if they exist."""
    for name in (HARDEN_NAME, COST_NAME):
        with contextlib.suppress(NotFound):
            client.containers.get(name).remove(force=True)
    for network in client.networks.list(names=[COST_NETWORK]):
        if network.name == COST_NETWORK:
            network.remove()


def run_hardening_checks(client: docker.DockerClient) -> None:
    """Run the H1-H4 probes in one cage and print a CHECK line for each."""
    output = client.containers.run(
        IMAGE, ["sh", "-c", HARDEN_PROBE], name=HARDEN_NAME, remove=True, **CAGE_FLAGS
    )
    observed = dict(line.split("=", 1) for line in output.decode().splitlines())
    for check_id, expected in HARDEN_EXPECTED.items():
        print_check(check_id, expected, observed.get(check_id, "missing"))


def time_cage_call(client: docker.DockerClient, network_mode: str) -> float:
    """Time one cage running true, from create to remove, in milliseconds."""
    started = time.perf_counter()
    container = client.containers.create(
        IMAGE, ["true"], name=COST_NAME, network_mode=network_mode, **CAGE_FLAGS
    )
    container.start()
    container.wait()
    container.remove()
    return (time.perf_counter() - started) * 1000


def measure_cage_calls(
    client: docker.DockerClient, measure_id: str, network_mode: str
) -> None:
    """Time cage calls after warm-up and print one MEASURE line."""
    for _ in range(WARMUP_CALLS):
        time_cage_call(client, network_mode)
    samples = [time_cage_call(client, network_mode) for _ in range(TIMED_CALLS)]
    p95 = statistics.quantiles(samples, n=20, method="inclusive")[18]
    print(
        f"MEASURE {measure_id} network={network_mode} calls={TIMED_CALLS} "
        f"min_ms={min(samples):.1f} median_ms={statistics.median(samples):.1f} "
        f"p95_ms={p95:.1f} max_ms={max(samples):.1f}",
        flush=True,
    )


def main() -> None:
    """Run the hardening checks and both measurements, cleaning up on exit."""
    client = docker.from_env()
    remove_leftovers(client)
    try:
        run_hardening_checks(client)
        measure_cage_calls(client, "C1", "none")
        client.networks.create(COST_NETWORK, driver="bridge", internal=True)
        measure_cage_calls(client, "C2", COST_NETWORK)
    finally:
        remove_leftovers(client)


if __name__ == "__main__":
    main()
