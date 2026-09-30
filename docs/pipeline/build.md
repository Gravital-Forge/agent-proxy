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
- The pull request's comments, when the prompt carries a `PR` line: read them with `gh_pr_fetch` on
  the item route. A not-ready Merge verdict lives there, and the handoff repeats none of it.

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
the completion notification. A sidecar index-lock error is a retry, not an anomaly.

### Wrap

Run `/wrap`: the closing checkpoint with code review and documentation review, the light bar on
every fix commit. Each reviewer dispatch tells the reviewer to fetch files through the
`source-reader` agent and keep its own judgment; a later round goes back to the same reviewer with
`SendMessage` inside five minutes.

After wrap's last fix, run the heavy bar once, as `.dev/verification.md` defines it, leftover check
included. A red inside the item is fixed, checkpointed, and the heavy bar runs again. A red for a
reason outside the item is something broken under `index.md`.

Open the pull request on the item route with `gh_pr_create` and `base="task/poc"`. Its body carries
the two fixed headings: `## Fork log` copied from `.local/impl/spec.md`, and `## Plan tasks` copied
from `.local/impl/plan.md`.

### Watch CI

Watch the pull request's checks to green with `gh_pr_checks` on the item route, by the CI watch
`.dev/version-control.md` sets. A round is one red verdict answered by a fix, a checkpoint, and a
push. A third red round on one pull request is a blocker to PM. A red by timeout counts toward no
round.

## Out

A pull request open against `task/poc`, the heavy bar and CI green on its head commit, carrying
`## Fork log` and `## Plan tasks` in its body.

## Handoff

The report is the last act. A job lands every artifact, confirms each one by reading it back through
git or the pull request, and then calls
`report_lifecycle(worker_id, state="success", message, next_prompt)` once. `worker_id` is the
`BATON_WORKER_ID` environment variable. The call omits `model`. Nothing follows the call.

Before that call, run the usage check `index.md` sets.

The next prompt takes the pinned shape, one line each:

```text
Job: <PM | Plan | Build | Merge>
Slug: <slug>
PR: #<number>
Read first: <workspace root>/projects/agent-proxy-poc/docs/pipeline/index.md
```

Build writes one prompt: `Job: Merge`, the slug, and `PR: #<number>`.

A blocker instead sends `Job: PM` with the slug, the `PR` line where a pull request exists, and one
added line, `Blocker: <one sentence>`, naming the durable home where the blocker is written: a pull
request comment once one exists, and a dated `## Decisions` heading in `.local/impl/spec.md` before
that.
