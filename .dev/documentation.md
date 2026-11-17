# documentation binding

Kept documentation is exactly these living documents, each path from the repository root:

- `README.md`
- `docs/architecture.md`
- `docs/deployment.md`
- `docs/acceptance-checks.md`
- `docs/reference.md`
- `docs/writing-a-plugin.md`
- `plugins/<plugin>/README.md`, one for each plugin
- `CONTRIBUTING.md`
- `CHANGELOG.md`
- `LICENSE`
- the third-party notice for what the published images ship that the MIT licence does not cover

`docs/design.md` and `docs/runbooks/per-agent-instance.md` are also kept documents while they exist.

Update a kept document whenever a change affects what it describes. The enumeration is the rule, and
the set is closed. Only the operator adds a kept document; never add one as a default home for new
prose.

Every document in the list above is kept whether or not it exists. The first pull request with
something to describe creates it.

## What each document holds

- `README.md` says what agent-proxy is, why it exists, how a call works (a diagram), and why the
  setup it needs is necessary. It holds a plugin table, links, and the licence.
- `docs/architecture.md` describes what is built and cites the code by path: concepts, the call
  path, the cage, egress, tokens, credentials, audit, the isolation model and why each part of it is
  needed, and the known limits.
- `docs/deployment.md` gives the requirements, provisioning, the first start, the self-check,
  teardown, and upgrading to a new release.
- `docs/acceptance-checks.md` holds the checks that need root, a second user, or a real credential.
- `docs/reference.md` gives the config file's fields, the operator command, and the results and
  refusals.
- `docs/writing-a-plugin.md` tells a plugin author what the image holds, and how to write and check
  its manifest.
- Each `plugins/<plugin>/README.md` describes one plugin and stands alone.
- `CONTRIBUTING.md` gives the development setup, the packages, the test bars, and the repository
  layout.
- `CHANGELOG.md` holds one entry per release.
- `LICENSE` is the MIT licence.
- The third-party notice covers what the published images ship that the MIT licence does not cover.
- `docs/design.md` holds the operator's closed decisions. An item's pull request edits only the
  `docs/design.md` sections the operator's ask names, and a change to a closed decision needs the
  operator.
- `docs/runbooks/per-agent-instance.md` tells the operator how to provision, start, and tear down
  one agent's instance, and how to run the deployment acceptance checks.

Everything else resolves to the workspace root: the root `documentation` topic for retrospective
reports, the `implementation` skill for a work item's working documents, the `task-management` topic
for the pull request as the item's record, and the workspace root `CLAUDE.md`'s Writing section for
the style every kept document must satisfy.
