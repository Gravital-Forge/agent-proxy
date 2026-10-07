# Plan

Plan turns one roadmap entry into an item worktree holding an approved spec and plan that Build can
implement without reopening a decision.

`index.md` in this directory carries the rules every job obeys, the standing approvals, and the
terms. Read it first. This document adds what Plan alone does.

## Model

Opus 5.5, the project model; `.dev/models.md`.

## In

- The slug the prompt carries, and its entry in `docs/roadmap.md`.
- The `docs/design.md` sections the entry names or touches, and the design's Decision directives.
  Read those sections, not the whole file.
- `docs/architecture.md`, by section, where the item touches what it describes, when it exists.

## Does

### Set up the worktree

Open `<workspace root>/projects/agent-proxy-poc/CLAUDE.md` with the `Read` tool. Run `/setup`. Pull
the project worktree first with `git_pull`, with `repo` set to the project worktree; its HEAD is
`task/poc`, and the new branch cuts from there. Create the worktree with `repo` set to the project
worktree:

```text
git_worktree(action="add", path=<workspace root>/projects/agent-proxy-<slug>, branch="task/<slug>", create_branch=True)
```

Run `uv sync` in each package directory that exists in the worktree: every directory under `core/`
or `plugins/` holding a `pyproject.toml`. With no package yet, nothing is synced.

### Take the baseline

Run the light bar in the worktree. Read the *How to use it* section of `.dev/verification.md` for
the commands. A failing baseline is fixed inside the item when the fix is small, and is a blocker to
PM otherwise.

### Walk the forks

Enumerate every fork an interactive grilling would have put to the operator, and resolve each
against the design's closed decisions, then its Decision directives in their order, then
`docs/architecture.md` when it exists. The `grilling` skill is never invoked. Write the item's
`.local/impl/spec.md` in the worktree: what the item builds, the done-when sentence copied from the
roadmap entry, a pointer to the roadmap entry by slug and to the design by section, and a
`## Fork log` heading carrying each fork, its resolution, and the section or directive that decided
it. Copy nothing else from the design; cite it. A fork nothing resolves is a blocker to PM.

A done-when clause that is a deployment acceptance check, as `.dev/verification.md` defines one, is
placed by the spec in `docs/runbooks/per-agent-instance.md`, and no task runs it.

### Write the plan

Run `/writing-plans` to produce `.local/impl/plan.md`. Every task names the paths it writes. A claim
about a dependency's API, or about Docker, iron-proxy, git, or `gh` behavior, is confirmed by a
subagent that reads the source or runs the pinned version and returns the finding with a citation;
the job worker never reads a dependency's source or a container image's contents itself. A claim no
subagent can confirm becomes a spike task that opens the plan, or a blocker to PM when a spike
cannot settle it.

Dispatch every reviewer `writing-plans` names, in parallel, each with the model `.dev/models.md`
gives, under `index.md`'s rules *Reviewers delegate retrieval and keep judgment* and
*A later review round is a follow-up*. Every reviewer returning Approved is the approval: append a
`## Review` heading to the plan carrying each reviewer's status line, and continue.

## Out

- `.local/impl/spec.md` in the item worktree, carrying `## Fork log`.
- `.local/impl/plan.md` in the item worktree, carrying `## Review` with an Approved line from every
  reviewer `writing-plans` dispatched.

## Handoff

Hand off under `index.md`'s rules *The report is the last act* and
*Usage is checked before every `success` report*. The next prompt takes the pinned shape, one line
each:

```text
Job: <PM | Plan | Build | Merge>
Slug: <slug>
PR: #<number>
Read first: <workspace root>/projects/agent-proxy-poc/docs/pipeline/index.md
```

Plan writes one prompt: `Job: Build`, the slug, and no `PR` line.

A blocker instead sends `Job: PM` with the slug and one added line, `Blocker: <one sentence>`,
naming the durable home where the blocker is written: a dated `## Decisions` heading in the item's
`.local/impl/spec.md`, as `index.md` sets.
