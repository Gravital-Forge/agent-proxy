# version-control binding

Two checkouts serve the project, each at a fixed place under `<workspace root>/projects/`:

- The **primary clone**, `projects/agent-proxy`, stays on `main` and takes no edits.
- An **item worktree**, `projects/agent-proxy-<slug>`, is on `task/<slug>`, cut from the primary
  clone's freshly pulled `main`. Each slug is new: no existing `task/` branch carries it.

## How to use it

- Native `git` and `gh` carry every operation, as the workspace root's `version-control` topic sets
  out. Run each `git` command as `git -C <checkout>` on the checkout it acts on, by absolute path.
- Every pull of `main` and every item worktree add and remove run in the primary clone, because an
  item branch is cut from the primary clone's `main`. Every command on the item's branch runs in the
  item worktree: status, add, commit, push, and diff.
- The CI watch runs on the root topic's inconclusive threshold, because this project's CI waits on
  no outside build.

## Removing an item worktree

Before you remove an item worktree:

1. Remove its dependency, build, test, and cache outputs by plain file removal: every `.venv`,
   `__pycache__`, `.pytest_cache`, `.ruff_cache`, and `.rumdl_cache` anywhere under the worktree.
   This step is done when this command prints nothing:

   ```sh
   find <worktree> \( -name .venv -o -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .rumdl_cache \) -prune -print
   ```

2. Run `docker image prune --all --force --filter label=agent-proxy.test=1`. `--all` is required,
   because a tagged test image survives the prune without it.

Then remove the worktree with `git -C <primary clone> worktree remove <worktree>`. Its branch stays.

Everything else resolves to the workspace root's `version-control` topic: the credential model and
the refusals that stop the work, the review-bundle path, the `task/` branch prefix, the one pull
request to `main` that lands an item and that only the operator merges, staging from explicit file
lists, keeping every merged branch, and the CI watch's timer-and-poller shape.
