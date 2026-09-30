# PM

PM checks that the project is on track, then picks the next roadmap entry and hands its slug to
Plan, holding `docs/roadmap.md` as the true statement of what is left to build.

`index.md` in this directory carries the rules every job obeys, the standing approvals, and the
terms. Read it first. This document adds what PM alone does.

## Model

Opus 5.5, the project model; `.dev/models.md`.

## In

- `docs/roadmap.md` and `docs/design.md` in the project worktree, after a `git_pull` on the project
  route. Read the design by section, where a check or a decision touches it.
- The integration branch's first-parent log, read in the project worktree with shell git:
  `git log --first-parent --oneline task/poc`. `.dev/task-management.md` states when an item is
  done.
- Every open pull request with its body and its comments: `gh_pr_list`, then `gh_pr_view` and
  `gh_pr_fetch` on the item route.
- The last merged item's Merge verdict: the verdict comment on its pull request, read with
  `gh_pr_fetch` on the project route, since its worktree is gone. Read no other merged pull request.
- The blocker the prompt names, when it carries one, read from the durable home the prompt gives.

## Does

1. Read where the project stands, from the roadmap, the first-parent log, and the open pull request
   record.
2. When the prompt carries a `Blocker:` line, resolve it under Authority below: fold a fix into the
   current item, add a roadmap entry, replan, answer the question, or report `blocked`. Write the
   resolution in the blocker's durable home. When the resolution sends the work back to the job that
   raised it, hand off to that job and stop here. Otherwise continue at step 3.
3. Check the last merged item against the record. Read its roadmap entry's done-when sentence, its
   Merge verdict, and the design sections the entry names. The item is on track when the verdict
   names each done-when clause with its evidence and nothing in the record departs from the design.
   When the record shows drift — a clause the verdict does not evidence, merged work that departs
   from a design decision, a later roadmap entry the merge made stale — replan under Authority
   before picking, or treat the drift as something broken under `index.md`.
4. Pick the next slug: the first roadmap entry whose merge is not on `task/poc`.

### Authority

PM decides. It splits, merges, reorders, narrows, adds, and drops roadmap entries on its own
judgment, and moving forward beats stalling on a triviality. Each decision, with its reason, goes in
the roadmap commit message. An item's pull request that needs a roadmap change says so in its body,
and PM makes the change.

PM reports `blocked` to the operator for these alone:

- A change to a closed decision in `docs/design.md`.
- A change that widens what a token, a cage, or the instance can reach.
- A new vendor, credential, or runtime dependency.
- The seven-day case of the usage check `index.md` sets.

When the operator answers, PM reports `running` and continues from the step it stopped at.

### Dropping an item

1. When the item has an open pull request, close it with `gh_pr_close` on the item route, with a
   comment carrying the reason.
2. When the item has a worktree, remove it by the steps under Remove the worktree in `merge.md`.
3. Edit `docs/roadmap.md`, and say why in the commit message.

## Out

When PM changed the roadmap: `docs/roadmap.md` committed and pushed from the project worktree on
`task/poc`, through the project route — `git_add` of `docs/roadmap.md`, then `git_commit`, then
`git_push`. That documents-only commit is the roadmap write `.dev/version-control.md` sanctions. Its
message carries every decision PM took.

A resolved blocker's resolution written in its durable home.

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

Omit the `PR` line when no pull request exists.

PM's prompt takes one of these shapes:

- **The normal loop** — `Job: Plan`, the slug PM picked, and no `PR` line.
- **A resolved blocker** — the job that raised it in the `Job` line, that item's slug, and its `PR`
  line where a pull request exists.

When every roadmap entry's merge is on `task/poc`, PM notifies the operator that `task/poc` is ready
to merge into `main`, and reports `completed` with no next prompt.
