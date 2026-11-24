# verification binding

Verification here runs as three named bars. Each bar is a fixed set of checks, and each names who
runs it and when. `CONTRIBUTING.md` under "The packages" says what a package is.

## How to use it

- The **light bar** is `poe lint` and `poe test` in every package, then the Markdown lint, each run
  as `CONTRIBUTING.md` gives under "The checks". An implementer-lead runs it at every task
  checkpoint. A package that declares fewer than `lint` and `test` is a gap the bar reports, never a
  pass.
- The **heavy bar** is `poe test-integration` in each package a task changes, run as
  `CONTRIBUTING.md` gives under "The checks", against the agent's own rootless Docker daemon. The
  implementer-lead runs it at the task's checkpoint. A package that declares no `test-integration`
  is a gap the bar reports, never a pass.
- The **full bar** is the light bar, the heavy bar, and CI green on the head commit.

## Constraints

- One heavy run at a time on the host. A second heavy run waits for the one in flight to finish.
- After a heavy run, each label check in `CONTRIBUTING.md` under "What a test leaves behind" prints
  nothing, or the heavy bar is red.
- Every check runs as the agent's own user.
- An acceptance check, as the opening of `docs/acceptance-checks.md` defines it, is written into
  that document and never run in the project.

Everything else resolves to the workspace root's `verification` topic, which owns how a package
declares its checks.
