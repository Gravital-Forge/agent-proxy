# Merge

Merge decides whether one pull request is ready, and on ready lands it on `task/poc` and removes its
worktree.

`index.md` in this directory carries the rules every job obeys, the standing approvals, and the
terms. Read it first. This document adds what Merge alone does.

## Model

Opus 5.5, the project model that `launch.md` sets.

## In

- The pull request number the prompt carries.
- The pull request's prior verdict comments, read with `github_pr_fetch`, so a second not-ready
  shows as one.
- The item worktree `projects/agent-proxy-<slug>` with its spec, its plan, and its branch.
- The item's entry in `docs/roadmap.md`, and `.dev/documentation.md`.

## Does

### Run the checks

Every check below is named in the verdict:

- **CI green on the pull request's head commit** — `github_pr_checks`.
- **The full bar** — the light bar and the heavy bar run in the item worktree, with CI green above,
  as `.dev/verification.md` defines them.
- **Every task under the pull request body's `## Plan tasks` present in the diff** — `git_diff` with
  `repo` set to the item worktree and its `ref` and `output` arguments, so the diff body streams to
  a file and stays out of context.
- **Nothing outside the plan** — outside means a changed file no task names, other than a lockfile,
  a fixture, or a sibling test of a planned file.
- **The kept documents the change affects updated** — `.dev/documentation.md` closes that set and
  says which `docs/design.md` sections an item may edit.
- **The roadmap entry's done-when met**, clause by clause, each clause named with its evidence.

### Post the verdict

`github_pr_reply` with `Ready` or `Not ready` and one line per check above: the check, its result,
and the evidence.

Not ready hands off to Build with the findings in that comment. A second not-ready on the same pull
request is a blocker to PM. A check red for a reason outside the item is something broken under
`index.md`.

### Merge, on ready

1. `github_pr_view` with `fields="headRefOid"` — confirm the pull request's head commit equals the
   branch tip.
2. `git_pull` of `task/poc`, with `repo` set to the project worktree. A non-fast-forward pull is a
   not-ready verdict.
3. `git_merge(branch="task/<slug>")`, with `repo` set to the project worktree.
   `.dev/version-control.md` makes it a merge commit. On a merge failure run the local
   `git merge --abort` in the project worktree, one of the plain git writes that binding sanctions,
   post not ready naming the conflict, and hand off to Build.
4. `git_push`, with `repo` set to the project worktree.
5. `github_pr_view` with `fields="state"`, up to five times ten seconds apart, until it reads
   `MERGED`.

Once `git_push` has landed the merge commit on `task/poc`, a failure is never a not-ready verdict. A
pull request still open after the fifth read, or any later step that fails, is something broken
under `index.md`, and goes to PM.

### Remove the worktree

Merge reaches this step once step 5 above reads `MERGED`. PM reaches it when it drops an item.

1. Clear the worktree's outputs by the steps under Removing an item worktree in
   `.dev/version-control.md`, each to its done-when.
2. Confirm the worktree's status is clean with `git_status`, with `repo` set to the item worktree.
3. `git_worktree(action="remove", path=<the worktree's absolute path>)`, with `repo` set to the
   project worktree. On a proxy timeout, run the local `git worktree prune` in the project worktree
   and report what remains.

The branch stays, local and remote.

### Notify

Notify that the item merged. Notify means the three things `index.md` names, and a missing tool is
not a stop.

## Out

- `task/poc` carrying the merge commit, and pushed.
- The pull request read back as merged.
- The worktree gone, its branch kept.
- The verdict comment and the notification sent.

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

Merge writes one prompt: `Job: PM`, the slug of the item it just merged, and `PR: #<number>`.

A not-ready verdict instead sends `Job: Build` with the slug and the `PR` line. The findings stay in
the verdict comment, and the prompt repeats none of them.

A second not-ready on the same pull request, or something broken, sends `Job: PM` with the slug, the
`PR` line, and one added line, `Blocker: <one sentence>`, naming the pull request comment where the
blocker is written.
