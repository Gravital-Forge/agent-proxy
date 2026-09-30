# task-management binding

Every work item's pull request targets `task/poc`, this repository's integration branch. The root
topic allows that base only on the operator's explicit request; this binding is that request.

The Merge job merges a ready pull request into `task/poc`. An item is done when
`git log --first-parent --oneline task/poc`, run in the project worktree, carries a merge commit
naming `task/<slug>`.

A pull request body carries two fixed headings, `## Fork log` and `## Plan tasks`, copied from the
item worktree's spec and plan.

`docs/pipeline/index.md` defines what notification means inside the pipeline.

Everything else resolves to the workspace root's `task-management` topic: the pull request as a work
item's record, an item's size and a plan's sizing, and the slug that names the item's `task/<slug>`
branch.
