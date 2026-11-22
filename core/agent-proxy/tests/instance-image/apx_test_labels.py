"""Label every container and network the instance creates, for the tests' cleanup.

The .pth line beside this module imports it at every interpreter start in the
image, before the agent-proxy console script runs.
"""

from agent_proxy import egress, plugins, runner

TEST_LABELS = {"agent-proxy.test": "1"}

egress.LABELS = TEST_LABELS
plugins.CONTAINER_LABELS = TEST_LABELS
runner.CONTAINER_LABELS = TEST_LABELS
