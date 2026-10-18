"""What the github plugin's integration tests share with their fixtures."""

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from agent_proxy.config import Root
from agent_proxy.egress import CageNetwork
from agent_proxy.runner import Outcome

# gh uploads attachments only with a token whose type it can tell from its
# prefix; ghp_ marks a classic personal access token.
GITHUB_TOKEN = "ghp_" + "0" * 36

type FakeApiLog = Callable[[], list[dict[str, object]]]


class RunAction(Protocol):
    def __call__(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        image: str | None = None,
        roots: Sequence[Root] = (),
        network: CageNetwork | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Outcome:
        """Run the named action in a cage granted github-token as GH_TOKEN, and env.

        The image defaults to the stub image, the network to the cage network
        with an empty environment, and env to none.
        """
