# Roadmap

This roadmap is the ordered list of the pipeline's work items. Each entry carries a slug that is
unique across the roadmap, and the slug names both the item's branch, `task/<slug>`, and its item
worktree, `projects/agent-proxy-<slug>`. PM is the only job that edits this file. No entry records
status: done derives from git, as `docs/pipeline/index.md` says. `docs/design.md` holds the
decisions each entry builds on.

1. **core-skeleton** — Stand up `core/agent-proxy`, a uv package on Python 3.12 with FastMCP and the
   Docker SDK as declared dependencies, and `poe` tasks `lint`, `test`, and `test-integration`. The
   config model loads an instance's YAML and applies every Config validation rule that needs no
   plugin manifest. The item also builds the token store, the credential store, the audit writer,
   and the admin CLI's `token`, `credential`, and `audit tail` commands.

   **Done when** a unit test refuses a config for each such rule; `token mint` prints an `apx_`
   token once and refuses a used name; `token list` output holds no token; a revoked or expired
   token fails verification in constant time over the hash; `credential set` reads only stdin and
   stores the value readable by its owner alone; the audit log rotates by size and records no
   credential value or output content; CI runs the package's lint and test; and
   `poe test-integration` exits 0 with no integration test yet.
2. **plugins** — Ship the manifest JSON Schema as package data,
   `agent-proxy manifest check <image>`, plugin loading by digest through `describe`, and the rest
   of Config validation. Build the trivial test plugin with one `command` action and one `script`
   action, and `agent-proxy reload` with its atomic swap.

   **Done when** `manifest check` passes the test plugin and refuses a manifest that breaks each
   schema rule; a grant naming an undeclared action is refused; a declared pass-through `args`
   warns; a reload of an invalid file leaves the running config serving; a call started before a
   reload finishes under its old config; and an integration test builds the test plugin, pulls it by
   digest, and loads its manifest.
3. **cage** — Build the cage runner and results: roots mounted at their host paths with their modes,
   a read-only root filesystem, every capability dropped, `no-new-privileges`, a bounded tmpfs,
   credentials as environment variables or files, the concurrency cap, the timeout, the result
   shapes, and the overflow directory with its seven-day cleanup. The cage joins a network it is
   given; this item gives it a no-proxy internal network.

   **Done when**, on the agent's daemon, an unmounted path is absent; an `ro` root refuses a write;
   an ungranted credential is in neither the environment nor the filesystem; `H1` to `H4` hold; a
   timed-out cage leaves no container; an output over 256 KiB lands in the overflow directory and
   the result carries only its pointer and size; a call over the cap waits; and the heavy bar's
   output prints the cage cost median over 30 calls.
4. **egress** — Build the egress proxies and their networks as `docs/design.md` states them, reload
   reconciliation included.

   **Done when**, on the agent's daemon, a cage reaches its list's domain and is refused another,
   asserted on the proxy's refusal; a `domains: []` cage has no route out; a cage reaches no other
   list's proxy by name or by address; a direct connection to an outside IP address fails; every
   proxy listener but the tunnel is unreachable from a cage or enforces the allowlist; a reload that
   drops a list removes its proxy only after the last cage on its network ends; and the pinned
   iron-proxy still ignores `source_ip` (`E9`) while no config the core writes carries one.
5. **mcp-surface** — Serve MCP over Streamable HTTP on the instance's loopback port, with bearer
   auth, `tools/list` filtered to the scope, `<plugin>_<action>` names with the manifests' input
   schemas, the proxy's refusal errors, last-used updates, and one audit line per call.

   **Done when** a transport test starts the real server and, through an MCP client, lists only the
   granted tools; runs a test-plugin action in a real cage and gets its result; gets a refusal
   naming the proxy for a bad token, a revoked token, an ungranted action, and an input that fails
   its schema; and finds one audit line per call.
6. **git-plugin** — Build `plugins/git`: an action for every git and git LFS tool git-sidecar
   offers, `safe.directory` per root, the credential helper, and git-sidecar's rules — protected
   branches unpushable, force-push refused, the `task/` branch prefix — as `script` actions.

   **Done when** each action passes against fixture repositories, one of them owned by another uid;
   a push to `main`, a force push, and a branch without the prefix are refused; the behavior of a
   checkout that changes an agent-owned file's executable bit is tested, and recorded in
   `docs/design.md`'s Known gaps when it fails; and `manifest check` passes in CI.
7. **github-plugin** — Build `plugins/github`: an action for every GitHub tool git-sidecar offers,
   with `api.github.com` and `github.com` as its grant's domains and `GH_TOKEN` as its credential.
   Then review the image-based plugin form against the two built plugins; the outcome replaces the
   provisional line in `docs/design.md`.

   **Done when** each action passes against a stubbed `gh`; `manifest check` passes in CI; and
   `docs/design.md` records the plugin form as settled.
8. **packaging** — Build the deployment artifacts and exercise them without root: the instance
   image, `core/docker/provision-instance.sh <agent> <port> <config-file>` and its teardown, a
   sample instance config under `examples/`, and `docs/runbooks/per-agent-instance.md`, which
   explains each provisioning step and carries the deployment acceptance checks `docs/design.md`
   lists. No script runs as root in this item.

   **Done when** `shellcheck` passes both scripts; the instance image builds; started on the agent's
   own daemon with the sample config and that daemon's socket, the instance serves an MCP client
   that runs a git-plugin action on a fixture repository and a github-plugin action against a
   stubbed `gh`, each with its audit line; and the runbook names every acceptance check with the
   command that runs it.
