# Launch — operator steps

These are the steps the operator performs by hand, in order: from the merged seed to a running
pipeline, and on to its end. `<workspace root>` is the harness clone's absolute path.
`<state directory>` is baton's state directory; `BATON_STATE_DIR` names it, and it is
`~/.local/state/baton` when that variable is unset.

## 1 · Prerequisites

These hold before a worker launches. Each names the command that confirms it.

1. The `baton-worker` skill is in the harness, at `<workspace root>/.claude/skills/baton-worker/`.
   **Pass:** a workspace session lists `baton-worker` among its skills.
2. The repository merges by merge commit. The primary clone's config sets `merge.ff false` and
   `pull.ff only`, and every worktree shares that config. The sidecar's `git_merge` then makes a
   merge commit, and an item's completion reads from the first-parent log. **Pass:**
   `git -C <workspace root>/projects/agent-proxy config --get merge.ff` prints `false`, and
   `--get pull.ff` prints `only`.
3. `task/poc` tracks `origin/task/poc` in the project worktree. **Pass:**
   `git -C <workspace root>/projects/agent-proxy-poc status -sb` names `origin/task/poc`.
4. The baton daemon is up. **Pass:** `ss -ltn` lists the port that `<state directory>/mcp.json`
   names. The file alone proves nothing, because it persists from earlier runs.
5. Workers reach the agent's rootless Docker daemon. `DOCKER_HOST` names that daemon in the
   environment the baton daemon runs in, and every worker inherits that environment. **Pass:** in
   the shell that runs the baton daemon, `docker info` names `rootless` under its security options.
6. The seed merge is on `task/poc`. **Pass:** after
   `git -C <workspace root>/projects/agent-proxy-poc pull`,
   `git -C <workspace root>/projects/agent-proxy-poc log -1 --oneline` shows the seed's merge.

## 2 · Dry run

The dry run is a short project. It runs every command shape a job needs and records which ones auto
mode rejects.

1. Start the setup-agent session in `<workspace root>`:

   ```sh
   claude --mcp-config <state directory>/mcp.json
   ```

   This session is not a worker. It starts projects. **Pass:** the session's tool list shows
   `initialize_project`.
2. Paste this whole message into the setup-agent session:

   ```text
   Call `initialize_project` with these arguments:

   - project_path: <workspace root>
   - title: agent-proxy dry run
   - model: claude-opus-5-5
   - initial_prompt: the text between the two marker lines below, verbatim and without the
     marker lines themselves.

   --- initial_prompt begins ---
   Job: dry run
   Read first: <workspace root>/projects/agent-proxy-poc/docs/pipeline/launch.md, section 2.
   Create the worktree projects/agent-proxy-dry-run on branch task/dry-run through the sidecar
   on the project route, with the path in the route form .dev/version-control.md gives.
   In it, run every command in the dry-run list, in order, and after each record the command
   and whether it ran or auto mode rejected it. Write the record to
   <workspace root>/projects/agent-proxy-poc/.local/dry-run.md. Remove the worktree through
   the sidecar by the steps .dev/version-control.md gives, then report completed.
   --- initial_prompt ends ---

   Report the project id the call returns, then call `list_projects` and show the result.
   ```

   **Pass:** `list_projects` lists the project.
3. Wait for the dry-run worker to report. Then ask the setup agent to call `close_project` with the
   project id. **Pass:** `list_projects` no longer lists the project.

`project_path` is `<workspace root>` here and in section 3, not the project worktree. Workers start
in the harness root, where the `baton-worker` skill and the harness load.

The dry-run list, run from the dry-run worktree root:

```sh
uvx rumdl@0.2.78 check .
uv --version
poe --version
docker info
docker run --rm --label agent-proxy.test=1 alpine:3 true
docker network create --internal --label agent-proxy.test=1 apx-dry-run
docker network rm apx-dry-run
rm -rf .rumdl_cache
find . \( -name .venv -o -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .rumdl_cache \) -prune -print
docker image prune --all --force --filter label=agent-proxy.test=1
```

The last three lines are the worktree cleanup `.dev/version-control.md` sets, so the dry run shows
whether auto mode lets a job run it.

Then, through the sidecar on the dry-run worktree's route, the worker runs `git_status`, `git_log`,
`git_add` and `git_commit` of one scratch file, `git_push`, and `gh_pr_checks` against any open pull
request or a recorded refusal. On the project route it runs `git_worktree` remove. Last, the worker
records whether the `PushNotification` and `clover_reply` tools are present in its session, runs
`<workspace root>/.claude/skills/checking-usage/usage-check.sh`, and records its line.

A denial reaches the worker in one of two forms. The auto-mode classifier's own denial begins
`Permission for this action was denied by the Claude Code auto mode classifier`. The auto-deny
hook's denial reads `auto mode rejected that command`. The record names which form each denial took.

**Pass:** `<workspace root>/projects/agent-proxy-poc/.local/dry-run.md` exists with one line per
command. Every rejection in the record is either routed around in a job document or removed from
what a job runs.

## 3 · Start the pipeline

Paste this whole message into the same setup-agent session:

```text
Call `initialize_project` with these arguments:

- project_path: <workspace root>
- title: agent-proxy
- model: claude-opus-5-5
- initial_prompt: the text between the two marker lines below, verbatim and without the
  marker lines themselves.

--- initial_prompt begins ---
Job: PM
Slug: pick the first roadmap entry whose merge is not on task/poc.
Read first: <workspace root>/projects/agent-proxy-poc/docs/pipeline/index.md
--- initial_prompt ends ---

Report the project id the call returns, then call `list_projects` and show the result.
```

**Pass:** `tmux ls` lists `baton-agent-proxy`, and `get_project_status(project_id)` shows phase
`running` with a `milestone` event.

The project model is `claude-opus-5-5`, and every worker runs on it: the first PM, each worker a
handoff starts, and each diagnosis or resumed worker baton starts on its own. No handoff names a
model. `.dev/models.md` holds the rule.

## 4 · Operating

Three places say where the project stands:

- `docs/roadmap.md` on `task/poc` lists the items in order.
- `git log --first-parent --oneline task/poc` in the project worktree shows one merge for each
  finished item.
- The repository's pull request list holds one open item pull request at most.

`docs/pipeline/index.md` says how the pipeline notifies the operator.

To watch a worker, or to act on one that is stuck:

- **Attach.** Run `tmux attach -t =baton-agent-proxy`. The `=` is tmux's exact-match prefix. Detach
  with the tmux prefix, then `d`.
- **A quiet worker.** `get_project_status` shows phase `running` with a stale milestone. Attach and
  type `Report your lifecycle state through report_lifecycle now.`
- **A silent worker.** Send two Ctrl-C keystrokes in its pane. Baton reads the vanish and launches a
  diagnosis worker.
- **A blocked PM.** The phase reads `blocked`. Attach and type the answer into the pane.
- **A parked project.** The phase reads `failed`. Call `resume_project` with the project id and the
  PM prompt from section 3.

## 5 · Ending

1. PM reports `completed`. **Pass:** the first-parent log of `task/poc` carries one merge for every
   entry in `docs/roadmap.md`.
2. Open the pull request from `task/poc` to `main`. Call `gh_pr_create` on the project route with
   `base="main"`, or open it in the GitHub UI. **Pass:** the pull request lists on GitHub with base
   `main`.
3. Merge that pull request with a merge commit. **Pass:** `main`'s log carries the merge commit.
4. When the operator declares the proof of concept done, remove the project worktree with
   `/cleanup-worktrees`. The branch `task/poc` stays. **Pass:**
   `<workspace root>/projects/agent-proxy-poc` no longer exists.
