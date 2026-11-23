# Changelog

This file holds one entry per release, newest first.

## 0.1.0 (unreleased)

- A per-agent instance: an MCP server that runs each tool call in a cage built from the call's
  grant, as [the architecture](docs/architecture.md) gives. The operator manages its tokens,
  credentials, audit log, and config reloads with the `agent-proxy` command, as
  [The operator command](docs/reference.md#the-operator-command) gives.
- The [git](plugins/git/README.md), [github](plugins/github/README.md), and
  [hcp-terraform](plugins/hcp-terraform/README.md) plugins.
- The instance starts and reloads its config, and `token mint` mints a token, before the operator
  stores a grant's credential, as [Failures and warnings](docs/reference.md#failures-and-warnings)
  gives.
- The self-check, which the agent runs as the MCP tool `agent-proxy_self_check` and the operator
  runs as `agent-proxy check`. [The self-check](docs/architecture.md#the-self-check) gives what it
  covers.
- Images published to GHCR for each release: the instance, the verifier, and one for each plugin, as
  [Published images](docs/architecture.md#published-images) gives.
