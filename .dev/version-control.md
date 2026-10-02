# version-control binding

`task/poc` is this repository's integration branch. Three checkouts serve the project, each at a
fixed place under `<workspace root>/projects/`:

- The **primary clone**, `projects/agent-proxy`, stays on `main`. No job writes to it.
- The **project worktree**, `projects/agent-proxy-poc`, stays on `task/poc`. A session confirms that
  branch, then pulls.
- An **item worktree**, `projects/agent-proxy-<slug>`, is on `task/<slug>`, cut from the project
  worktree's HEAD. No slug is `poc`, `spike`, or `pipeline-seed`.

## How to use it

- A `repo` or `path` argument is the path from the sidecar's projects root down, in the form the
  workspace's `CLAUDE.local.md` records. Three routes exist, each with its own token file:
  - The **project route** names the project worktree. It carries every call that reads or advances
    `task/poc`: pull, merge, push, and every `git_worktree` add and remove, because an item branch
    is cut from that worktree's HEAD.
  - The **item route** names an item worktree. It carries every call that acts on the item's branch:
    status, add, commit, push, diff, pull request creation, and every pull request comment, edit,
    and check read.
  - The **primary route** names the primary clone. No job uses it.
- Pull request reads and comments take the item route while the item worktree exists, and the
  project route once it is gone. PM's reads of a merged item's pull request take the project route.
- An item lands by one pull request to `task/poc`. The Merge job merges it in the project worktree,
  through the project route, with the sidecar's `git_merge`, and pushes.
- Every merge is a merge commit. The primary clone's config sets `merge.ff false`, which every
  worktree shares, so the sidecar's `git_merge` makes one.
- PM commits and pushes `docs/roadmap.md` and `docs/design.md` from the project worktree on
  `task/poc`, through the project route. That documents-only commit and the Merge job's merge are
  the only writes in the project worktree; every other change gets its item branch and worktree
  before the first edit. PM's design edits follow the rule `.dev/documentation.md` sets for
  `docs/design.md`, under which a change to a closed decision needs the operator.
- Three paths reach the remote: a checkpoint's persist step, PM's documents push, and the Merge
  job's push.
- Two plain git writes are sanctioned: `git merge --abort` after a failed sidecar merge, and
  `git worktree prune` after a sidecar timeout on a worktree removal.
- The CI watch runs on the root topic's inconclusive threshold, because this project's CI waits on
  no outside build.

## Removing an item worktree

Before the sidecar removes an item worktree:

1. Remove its dependency, build, test, and cache outputs by plain file removal: every `.venv`,
   `__pycache__`, `.pytest_cache`, `.ruff_cache`, and `.rumdl_cache` anywhere under the worktree.
   This step is done when this command prints nothing:

   ```sh
   find <worktree> \( -name .venv -o -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .rumdl_cache \) -prune -print
   ```

2. Run `docker image prune --all --force --filter label=agent-proxy.test=1`. `--all` is required,
   because a tagged test image survives the prune without it.

Then remove the worktree with `git_worktree` on the project route. Its branch stays.

## Constraints

`task/poc` advances by the Merge job's merge commits and PM's documents commits. PM and the Merge
job are the only jobs that push to it. CI runs on pull requests and on pushes to `main`.

Everything else resolves to the workspace root's `version-control` topic: the sidecar as the
credentialed path to git and GitHub, the token read and the route form, `git_diff` with its `output`
argument, the `task/` branch prefix, staging from explicit file lists, keeping every merged branch,
and the CI watch's timer-and-poller shape.
