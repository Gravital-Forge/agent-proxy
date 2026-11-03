"""What the hcp-terraform plugin's integration tests share with their fixtures."""

from collections.abc import Callable, Mapping
from typing import Protocol

from agent_proxy.runner import Outcome

TFE_TOKEN = "test-tfe-token"

type FakeApiLog = Callable[[], list[dict[str, object]]]


class RunAction(Protocol):
    def __call__(
        self, name: str, arguments: Mapping[str, object], *, env: Mapping[str, str]
    ) -> Outcome:
        """Run the named action in a cage against the fake.

        The cage is granted hcp-terraform-token as TFE_TOKEN, and env.
        """
