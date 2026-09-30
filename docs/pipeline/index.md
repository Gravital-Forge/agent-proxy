# Pipeline — agent-proxy

The pipeline is four jobs — PM, Plan, Build, and Merge — one worker each, supervised by baton, with
the repository's files on `task/poc`, each item worktree's `.local/impl/` files, the pull request
record, and baton's event log as the only memory between jobs. Every job document in this directory
repeats the rules its job needs, so a worker reads its own job document and this one.

The cycle is PM, then Plan, then Build, then Merge, then PM again. PM checks the project is on track
and picks the next item; Plan writes the item's spec and plan; Build implements them and opens the
pull request; Merge grades and merges it; PM checks the result and picks again.

These documents are committed on `task/poc`. Nothing in the pipeline lives in `.local/` except each
item's own `.local/impl/spec.md` and `.local/impl/plan.md`, which git ignores and whose decisions
the item's pull request body carries.

## Where things are

- `<workspace root>` is the harness clone. Run `pwd` from a workspace session to read it.
- `.dev/version-control.md` places the primary clone, the project worktree, and every item worktree,
  names their branches, and owns the sidecar routes, the worktree cleanup rule, and the CI watch
  threshold. Every job reads those there.
- The **project worktree**, `<workspace root>/projects/agent-proxy-poc` on `task/poc`, holds this
  directory, the **design** `docs/design.md`, and the **roadmap** `docs/roadmap.md`.
- An **item worktree**, `<workspace root>/projects/agent-proxy-<slug>` on `task/<slug>`, holds the
  item's own `.local/impl/spec.md` and `.local/impl/plan.md`.
- The project's `CLAUDE.md` and its bindings under `.dev/` are in every worktree. Open `CLAUDE.md`
  with the `Read` tool before the first action under a worktree, as the standing orders require.
  Each binding states what differs from the workspace root's binding of the same name, and resolves
  the rest there. Where this document answers a stop those bindings make, it is the operator's
  answer, given in advance.

## Items

The roadmap is an ordered list of entries, each a slug, a scope, and a **Done when** sentence. PM
alone edits it. An entry's slug names its branch `task/<slug>` and its worktree
`projects/agent-proxy-<slug>`.

`.dev/task-management.md` states when an item is done: a merge commit naming `task/<slug>` on the
first-parent log of `task/poc`. Read that log with shell git in the project worktree, because the
sidecar's `git_log` has no first-parent option. The roadmap stores no status.

## Rules every job obeys

- **A worker never ends a turn without a lifecycle report.** Every point where a skill says stop,
  wait, raise, or ask the operator is either covered by a standing approval below or becomes a
  `success` report whose next prompt hands off to PM with the blocker named. A denied tool call is
  routed around or becomes such a handoff, never a silent end of turn.
- **The report is the last act.** Baton terminates the worker a short grace period after a terminal
  report. A job lands every artifact, confirms each one by reading it back through git or the pull
  request, and then calls `report_lifecycle(worker_id, state="success", message, next_prompt)` once.
  `worker_id` is the `BATON_WORKER_ID` environment variable. The call omits `model`, so the next
  worker runs on the project model `.dev/models.md` names. Nothing follows the call.
- **Usage is checked before every `success` report.** Once every artifact is confirmed, a job runs
  `.claude/skills/checking-usage/usage-check.sh` from the workspace root. It reads `utilization` and
  `resetsAt` for the `seven_day` and `five_hour` windows, and the job applies the first case that
  matches:
  - **The seven-day window is at 0.8 or more.** Notify the operator with both windows and the
    seven-day reset time. Report `blocked` in place of `success` and hold the handoff. When the
    operator answers, report `running`, then make the held `success` report under the five-hour
    case.
  - **The five-hour window is at 0.8 or more.** The `success` report carries `delay_seconds`: the
    seconds from now to that window's `resetsAt`, plus 60.
  - **The check prints no line.** The report message says the windows were not in hand, and the
    report carries no delay.
  - **Otherwise** the report carries no delay.
- **Blockers go to PM.** PM alone reports `blocked` for a blocker, and only when it cannot resolve
  that blocker itself. Any job reports `blocked` under the usage check. PM alone reports
  `completed`.
- **Something broken goes to PM.** A job that finds something broken — a merged item's defect, a
  design claim that does not hold, a tool failure it cannot route around, a bar that goes red for a
  reason outside its item — writes it in the current durable home and hands off to PM with a
  `Blocker:` line. PM decides: fold a fix into the current item, add a roadmap entry, replan, or
  report `blocked`.
- **Deployment is out of scope.** A check that needs root, a second user, or a real credential is a
  deployment acceptance check: it is written into `docs/runbooks/per-agent-instance.md` and is not
  run. A job never asks the operator to run one.
- **Decisions have durable homes.** A pull request body carries two fixed headings, `## Fork log`
  and `## Plan tasks`, copied from the item's spec and plan. Blockers, their resolutions, and Merge
  verdicts are pull request comments. Before a pull request exists, a blocker and its resolution go
  under a dated `## Decisions` heading in the item's spec. A roadmap change and its reason go in
  PM's roadmap commit message. Handoff prompts point at these homes and repeat nothing from them.
- **Handoff prompts are short**, one line each:

  ```text
  Job: <PM | Plan | Build | Merge>
  Slug: <slug>
  PR: #<number>
  Read first: <workspace root>/projects/agent-proxy-poc/docs/pipeline/index.md
  ```

  Omit the `PR` line when no pull request exists. A handoff to PM carries the slug of the job that
  just finished; PM picks the next one itself. The setup agent's initial prompt, which no job wrote,
  may carry an instruction in the `Slug` line instead. A handoff to PM that raises a blocker adds
  one line, `Blocker: <one sentence>`, and names the durable home where the blocker is written.
- **A job may continue itself.** When a worker's context runs low, it checkpoints, confirms, and
  hands off to a fresh worker of the same job with the same artifacts. Implementation triage resumes
  from the worktree, the spec, the plan, the pull request, and git history.
- **Recovery routes to PM.** Baton launches a diagnosis worker after a failure or a vanished pane.
  That worker reports `success` with a PM handoff naming the failure. Its `blocked` is acceptable
  only when the repository state is unreadable.
- **Notify means three things,** and a missing tool is not a stop: `PushNotification` when present;
  `mcp__clover-shim__clover_reply` when present; and the pull request comment or the roadmap commit
  as the durable record.
- **CI rounds are counted.** A round is one red verdict answered by a fix, a checkpoint, and a push.
  A third red round on one pull request is a blocker to PM. A red by timeout counts toward no round.
- **Terms are fixed.** Every term under Terms below carries that meaning in every job document.

## Rules that keep a context small

These come from the retrospective on the saas-starter run. Each is an instruction, not a tool.

- **Read sections, not documents.** A dispatch names the section it wants read: the *How to use it*
  section of `.dev/verification.md`, one task of the plan. A lead receives its own task and the
  plan's global constraints, never the whole plan and spec. A job reads the design sections its item
  needs, and the item's roadmap entry, not either file end to end.
- **The prompt and the files are the state.** A job never calls `get_project_status` to orient
  itself; its payload is narrative history. Answer a reconciliation request from what the job knows.
- **A later review round is a follow-up.** When a reviewer returns issues, batch the fixes, then
  send the changed material back to that same reviewer with `SendMessage` inside five minutes,
  asking it to diff against its own prior round. Never spawn a fresh reviewer for a later round, and
  never message a reviewer's own subagent.
- **Reviewers delegate retrieval and keep judgment.** Every reviewer dispatch says: locate files and
  fetch lines through the `source-reader` agent, which returns each hit as a path, a line range, and
  the lines; choose what to examine, and decide what it means, yourself.
- **Package archaeology is delegated.** Reading a dependency's source or a container image's
  contents is a subagent's job; the coordinator receives the conclusion and a citation, never the
  dump.
- **Wait by ending the turn.** After a background dispatch, an agent with nothing else to do ends
  its turn; the completion notification re-invokes it. A poll turn is never how to wait.
- **Mutate tracked files with the edit tools.** Read however you like, batched. Change a tracked
  file only with `Edit` or `Write`, which refuse and report on a mismatch; a heredoc or inline
  script that asserts and exits discards the batch silently. Read a multi-edit batch back before
  calling it landed.
- **Check the sidecar before the first write.** A job's first sidecar call on any route is
  `git_status`. A bare `Invalid request parameters` is a stale connection: reconnect with `/mcp` and
  retry before concluding anything else. Load the sidecar tools the job needs in one `ToolSearch`
  call at the start.
- **Compose from the pinned shape.** A pull request body, a verdict, or a handoff is written from
  the shape this document pins. Never read a prior pull request body to match its form.
- **Leads run one at a time.** Implementer-leads share the item worktree, and concurrent leads
  collide in `uv.lock` and in each other's checkpoint commits. Dispatch one lead, wait for it, then
  the next.
- **A kept document cites; it does not restate.** `.dev/documentation.md` closes the set of kept
  documents. A kept document holds structure, boundaries, and seams. Reasoning about one component
  stays in that component's code and is cited by path.

## Standing approvals

These are the operator's answers, given here in advance, to every point where a harness skill or
binding stops for a person.

- **`writing-plans`, the stop after the plan reviewers approve** — every reviewer `writing-plans`
  dispatches returning Approved is the approval. The Plan job appends a `## Review` heading to the
  plan with each reviewer's status line, and continues.
- **`writing-plans`, "when you cannot confirm something, stop and tell the operator"** — the
  unconfirmed claim becomes a spike task that opens the plan, or a blocker to PM when a spike cannot
  settle it.
- **`grilling`** — never invoked, because it waits for a person. The operator closed the design; the
  Plan job's fork walk takes grilling's place. A fork the walk cannot resolve is a blocker to PM.
- **`setup`, the failing baseline** — fixed inside the item when the fix is small, a blocker to PM
  otherwise.
- **`checkpoint`, "raise to the operator", and `wrap`, "raise only substantial issues"** — a
  Consider finding is posted as a pull request comment and does not stop the job. A finding that
  needs a decision the design does not close is a blocker to PM.
- **`receiving-code-review`, "conflicts with the operator's prior decisions"** — a review suggestion
  that conflicts with the design is declined with the decision cited. One that conflicts with the
  plan is a blocker to PM only when it needs a decision the design does not close.
- **The `task-management` binding's "base a pull request on another branch only when the operator
  asks"** — this project is that request, and `.dev/task-management.md` records it: every item's
  pull request targets `task/poc`.
- **The `version-control` binding's "only the operator merges" and "never advance a pull request
  past open", and `cleanup-worktrees`, "only when the operator says so"** — the Merge job merges a
  ready pull request into `task/poc` and removes the merged item worktree. `main` stays the
  operator's: nothing here merges into it.
- **The `task-management` binding's "tell the operator twice"** — notify means the three things
  above, and a missing tool is not a stop.
- **The binding-topics rule's "stop and ask the operator" for an unbound topic, and the
  `version-control` binding's "stop and ask the operator" for a missing repository** — a blocker to
  PM. PM answers from the design and the bindings or reports `blocked`.
- **A new vendor, credential, or runtime dependency outside the design** — the design's Decision
  directives send it to the operator: a blocker to PM, which reports `blocked`.
- **The `version-control` binding's inconclusive threshold for the CI watch** — the threshold
  `.dev/version-control.md` gives.
- **A denied tool call, as the auto-mode classifier's own denial or as the hook's
  `auto mode rejected that command`** — route around it: vary the call, or reach the outcome another
  sanctioned way. When no route exists, a blocker to PM. Never a silent end of turn.
- **`retrospective` and `reflect`** — never invoked in the pipeline.

## Terms

- **Job** — one of PM, Plan, Build, or Merge: one worker, one concrete goal, starting from named
  artifacts and ending by leaving named artifacts.
- **Primary clone**, **project worktree**, **item worktree** — the three checkouts
  `.dev/version-control.md` places.
- **Project route**, **item route**, **primary route** — the sidecar routes
  `.dev/version-control.md` defines.
- **Integration branch** — `task/poc`. Every item's pull request targets it, the project worktree
  stays checked out on it, and the operator merges it into `main` when the roadmap is done.
- **Design** — `docs/design.md` on `task/poc`: the operator's closed decisions for the proof of
  concept, closing with its Decision directives.
- **Roadmap** — `docs/roadmap.md` on `task/poc`: the ordered entries, each with its done-when
  sentence.
- **Light bar** — the light bar `.dev/verification.md` defines.
- **Heavy bar** — the heavy bar `.dev/verification.md` defines.
- **Full bar** — the full bar `.dev/verification.md` defines.
- **Deployment acceptance checks** — the checks that need root, a second user, or a real credential.
  The design lists them, `docs/runbooks/per-agent-instance.md` carries them as steps, and no job
  runs them.
- **Fork walk** — the Plan job's enumeration of every fork an interactive grilling would have put to
  the operator, each resolved against the design's closed decisions, then its Decision directives in
  order, then `docs/architecture.md` when it exists.
- **Fork log** — what the fork walk writes under `## Fork log` in the item's spec: each fork, its
  resolution, and the section or directive that decided it. The pull request body copies it.
- **Blocker** — a decision no job can take from the design, the roadmap, and the code, or something
  broken, which goes to PM.
- **Standing approval** — the operator's answer, given in advance above, to a point where a harness
  skill or binding stops for a person.
