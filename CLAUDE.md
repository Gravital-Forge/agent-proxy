# agent-proxy — agent instructions

This repository is operated from the dev-harness-gravital workspace; start sessions there. The
project bindings are in `.dev/`, and `docs/pipeline/index.md` governs every baton session.

The operator's decisions, which no binding holds because the rule each one displaces lives in the
standing orders or in skill text:

- Plan approval is every reviewer `writing-plans` dispatches returning Approved.
- The Plan job runs a fork walk against `docs/design.md` in place of interactive grilling.
- A failing baseline at setup is fixed inside the item when the fix is small, and is a blocker
  otherwise.
- The Merge job merges a ready item pull request into `task/poc` and removes the merged item
  worktree.
- `main` is the operator's. Only the operator merges into it.
- One implementer-lead runs at a time.
- `docs/roadmap.md` is an ordered roster by design and stores no status.
