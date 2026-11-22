# verification binding

Verification here runs as three named bars. Each bar is a fixed set of checks, and each names who
runs it and when.

## How to use it

- A **package** is a directory under `core/` or `plugins/` that holds a `pyproject.toml`. Run each
  package's checks from the package's own directory.
- The **light bar** is `poe lint` and `poe test` in every package, then `uvx rumdl@0.2.78 check .`
  from the repository root. An implementer-lead runs it at every task checkpoint. A package that
  declares fewer than `lint` and `test` is a gap the bar reports, never a pass.
- The **heavy bar** is `poe test-integration` in each package a task changes, run against the
  agent's own rootless Docker daemon, which `DOCKER_HOST` names. The implementer-lead runs it at the
  task's checkpoint. A package that declares no `test-integration` is a gap the bar reports, never a
  pass.
- The **full bar** is the light bar, the heavy bar, and CI green on the head commit.

## Constraints

- One heavy run at a time on the host. A second heavy run waits for the one in flight to finish.
- Every container, network, volume, and image a test creates carries the label `agent-proxy.test=1`.
  After a heavy run, each of these commands prints nothing, or the heavy bar is red:

  ```sh
  docker ps -aq --filter label=agent-proxy.test=1
  docker network ls -q --filter label=agent-proxy.test=1
  docker volume ls -q --filter label=agent-proxy.test=1
  ```

- Every check runs as the agent's own user. A **deployment acceptance check** is a check that needs
  root, a second user, or a real credential. Write it into `docs/acceptance-checks.md` as a check,
  and never run it in the project.

Everything else resolves to the workspace root's `verification` topic, which owns how a package
declares its checks.
