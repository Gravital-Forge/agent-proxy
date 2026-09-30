"""A test double for the Egress protocol of agent_proxy.reload."""

import threading
from collections.abc import Collection, Iterable

from agent_proxy.errors import ProxyError


class RecordingEgress:
    """Records each ensure and prune call, the lists it was given, and its thread."""

    def __init__(self, ensure_error: ProxyError | None = None) -> None:
        """Start with no calls; ensure raises ensure_error after recording, if set."""
        self.calls: list[tuple[str, set[tuple[str, ...]]]] = []
        self.thread_names: list[str] = []
        self._ensure_error = ensure_error
        self._recorded = threading.Condition()

    def ensure(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists, then raise ensure_error if one was given."""
        self._record("ensure", lists)
        if self._ensure_error is not None:
            raise self._ensure_error

    def prune(self, lists: Iterable[Collection[str]]) -> None:
        """Record the lists."""
        self._record("prune", lists)

    def wait_for_calls(self, count: int, timeout: float) -> bool:
        """Return whether count calls or more are recorded within timeout seconds."""
        with self._recorded:
            return self._recorded.wait_for(lambda: len(self.calls) >= count, timeout)

    def _record(self, verb: str, lists: Iterable[Collection[str]]) -> None:
        with self._recorded:
            self.calls.append((verb, {tuple(domains) for domains in lists}))
            self.thread_names.append(threading.current_thread().name)
            self._recorded.notify_all()
