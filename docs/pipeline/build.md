# Build

Build implements the approved plan in the item worktree and leaves one pull request open against
`task/poc` with CI green.

`index.md` in this directory carries the rules every job obeys, the standing approvals, and the
terms. Read it first. This document adds what Build alone does.

## Model

Opus 5.5, the project model; `.dev/models.md`.

## In

- The item worktree `projects/agent-proxy-<slug>`, holding `.local/impl/spec.md` and
  `.local/impl/plan.md`.
- The pull request's comments, when the prompt carries a `PR` line: read them with
  `github_pr_fetch`. A not-ready Merge verdict lives there, and the handoff repeats none of it.

A plan whose `## Review` heading lacks an Approved line from every reviewer `writing-plans`
dispatches is refused: hand off to PM with that blocker rather than implement it.

## Does

### Triage

Open `<workspace root>/projects/agent-proxy-<slug>/CLAUDE.md` with the `Read` tool. Run
`/implementation` into the existing worktree. The spec and the plan are already there.

### Implement

Run `/multiplayer` with one implementer-lead at a time. Each dispatch carries the lead's own task
and the plan's global constraints, names the *How to use it* section of `.dev/verification.md` for
the light bar, and gives the model `.dev/models.md` sets for the lead and for every dev-worker it
dispatches. Each lead's checkpoint runs the light bar. After a dispatch, end the turn and wait for
the completion notification. A proxy index-lock error is a retry, not an anomaly.

### Close out

Run step 1 of `/wrap` alone: the closing checkpoint on the full changeset, with code review and
documentation review, and the light bar on every fix commit. Dispatch each reviewer under
`index.md`'s rules *Reviewers delegate retrieval and keep judgment* and
*A later review round is a follow-up*. Build's own steps below replace wrap's submit, CI-watch, and
notify steps, and run in this order.

1. **Heavy bar.** After the last fix, run the heavy bar once, as `.dev/verification.md` defines it,
   leftover check included. A red inside the item is fixed, checkpointed, and the heavy bar runs
   again. A red for a reason outside the item is something broken under `index.md`.
2. **Pull request.** With no `PR` line in the prompt, open the pull request with `github_pr_create`,
   with `base="task/poc"` and `head="task/<slug>"`. With a `PR` line, that pull request exists: push
   the fixes to `task/<slug>` with `git_push`, with `repo` set to the item worktree, and rewrite its
   body with `github_pr_edit`. Create no second pull request. The body carries the fixed headings
   `.dev/task-management.md` sets: `## Fork log` copied from `.local/impl/spec.md`, `## Plan tasks`
   copied from `.local/impl/plan.md`, and `## Decisions` copied from `.local/impl/spec.md` when the
   spec has one.
3. **Notify.** Notify that the pull request opened, or that it was updated when the prompt carried a
   `PR` line. Notify means what `index.md` defines.
4. **Watch CI.** Watch the pull request's checks to green with `github_pr_checks`, by the CI watch
   `.dev/version-control.md` sets. Count each red round under `index.md`'s rule
   *CI rounds are counted*.

## Out

A pull request open against `task/poc`, the heavy bar and CI green on its head commit, carrying the
fixed headings `.dev/task-management.md` sets in its body.

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

Build writes one prompt: `Job: Merge`, the slug, and `PR: #<number>`.

A blocker instead sends `Job: PM` with the slug, the `PR` line where a pull request exists, and one
added line, `Blocker: <one sentence>`, naming the durable home `index.md` sets for the blocker.
