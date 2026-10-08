# The git plugin

The git plugin runs git and git LFS in a cage. It offers one action for each git and git LFS tool
that git-sidecar offers, and it keeps git-sidecar's branch rules. `docs/writing-a-plugin.md` gives
the plugin model this follows, and `docs/design.md` gives the decisions behind it under "The git
plugin's constraints".

## The actions

Each action is named as its git-sidecar tool, without the `git_` prefix: `git_status` becomes
`status`, and `git_lfs_pull` becomes `lfs_pull`. A config that pins the plugin as `git` therefore
serves the MCP tool names that git-sidecar serves. `manifest.json` declares every action, its input
schema, its credentials, and its timeout.

An action is a command action when its git argv is a fixed prefix plus typed parameters. It is a
script action when it does more than that:

- It carries a rule, as `push`, `checkout`, and `worktree` do.
- It runs more than one git command, as `commit` and `push` do.
- It takes a list, as each action with a `files` or `patterns` input does.

No action takes pass-through `args`. Its items would be exempt from the core's refusal of a value
that begins with `-`, so they could pass git an option such as `diff --output`.

### The `repo` input

Every action takes a required `repo`, the absolute path of the repository. git runs as
`git -C <repo>`. The path must lie under one of the grant's roots: no other host path exists in the
cage, so git fails with its own error. A command action passes `repo` to the wrapper as the flag
`--repo`, and the wrapper takes the flag and its value out before git runs.

A file or pattern input with a `..` segment fails the input schema, so the core refuses the call
before a cage starts. So does a `diff` `output` that is empty, begins with `/`, or holds a `..`
segment.

### What an action returns

A command action returns `{exit_code, stdout, stderr}`, as `docs/design.md` gives under "Results". A
script action prints the same three members, and its `output_schema` states them. `diff` with
`output` is the one exception: "`diff` with `output`" gives its result. The script exits 0 whenever
it runs to its end, and git's exit code goes in `exit_code`.

When a git step inside a script fails for a reason other than the outcome that step checks, the
script stops and returns that step's git result. For example, `push` returns git's own result when
`repo` is not a repository. One step is the exception: `push` reads a failed `git lfs ls-files` as
no LFS file, as git-sidecar does, and goes on.

### Refusals

The plugin refuses a call with exit code 2 and a stderr that begins `git plugin refused: `, then
gives the reason. The wrapper refuses as its own exit; a script refuses through its printed
`exit_code`. The plugin refuses these:

- `AGENT_PROXY_ROOTS` is missing, or is not a JSON array of absolute paths.
- `--repo` is missing, repeated, or has no value.
- `push` or `checkout` finds `GIT_BRANCH_PREFIXES` unset, or naming no prefix. The reason names the
  variable.
- A call breaks a branch rule.
- `commit` finds no identity, or nothing staged.
- `diff`'s `output` resolves to the `.agent-proxy/` directory itself, or to a path outside it.
- `worktree` `add` names a `path` that is `repo` or lies inside it.
- The image's executable runs under a name that is neither `git-cage` nor a script action's.

The refusal of a branch that breaks a branch rule uses git-sidecar's wording.

### Output past the cap

A large `diff` is the usual call whose output passes the core's cap. Call `diff` with `output` to
keep the diff out of the result: the result then holds the file's path and a `--stat`, not the diff.
`docs/writing-a-plugin.md` gives what happens to a script whose output passes the cap, and
`docs/design.md` gives the cap and the result forms under "Results".

### `diff` with `output`

`output` is a path relative to the `.agent-proxy/` directory at the top of `repo`. With `output`,
`diff` writes the diff to that file, and the diff never enters the result.

- The file holds git's diff bytes as git prints them. A file that exists at the path is overwritten,
  and a selection with no change writes an empty file.
- The first call makes `.agent-proxy/`, with a `.gitignore` that holds `*`, so git ignores the
  directory and all it holds. A directory that exists stays as it is. A missing directory below it
  on the path is made.
- The result is `{exit_code, stderr, path, stat}`: git's exit code and stderr, the absolute path of
  the file with every symlink resolved, and `git diff --stat=1000,900` of the same selection. The
  width keeps a long path whole, as git-sidecar does. The result has no `stdout`, and the action's
  `output_schema` states both result forms.
- When git fails on the diff or on the stat, the result is that git run's
  `{exit_code, stdout, stderr}`, and the script writes no file.
- The write follows no symlink. It fails on a file that is not a regular file or has another hard
  link, and it empties the file only after both checks. A failed write returns `exit_code` 1, an
  empty `stdout`, and a `stderr` that begins `diff could not be written to <path>: `.

The grant must give the repository an `rw` root. Under an `ro` root, the write fails with the
read-only filesystem error, in the form the last item gives.

### `worktree`

`worktree` takes an `action` of `add`, `list`, or `remove`. `path` must be absolute, because git
resolves a relative path against `repo`, which surprises the caller.

- `list` runs `git worktree list`, and `remove` runs `git worktree remove <path>`.
- `add` with `branch` makes that branch for the new worktree, as
  `git worktree add -b <branch> <path>`. With `create_branch` false, `add` attaches the branch that
  exists instead, as `git worktree add -- <path> <branch>`. `create_branch` is true when the input
  leaves it out.
- `add` without `branch` lets git name the branch after the last component of `path`. git makes that
  branch when it is missing, and attaches it when it exists.
- `add` refuses a `path` that is `repo` or lies inside it. The check resolves every symlink in both.

git's own error answers `add` or `remove` without `path`.

### `stash`

`stash` passes `message` and `index` to git whatever its `action` is. git refuses a combination it
does not take, such as a `message` with `pop`. git-sidecar ignores such a member instead.

## In the cage

### Trusted roots

The core gives every cage `AGENT_PROXY_ROOTS`, the grant's root paths as a compact JSON array.
`git_argv` in `scripts/git_plugin/cage.py` passes git two `safe.directory` values for each root:
`<root>` and `<root>/*`. The first trusts a repository at the root itself. The second trusts every
repository beneath it, linked worktrees included. The function strips trailing slashes from each
root first, so `/x/` gives `/x` and `/x/*`.

### The credential helper

Every git run passes the credential helper that `docs/design.md` gives under "The git plugin's
constraints". It clears every inherited helper, then answers with `x-access-token` and the value of
`GH_TOKEN`.

Each action that reaches a remote declares `{"env": "GH_TOKEN"}` in its `credentials` in
`manifest.json`. The core does not compare a grant's credentials with that declaration. Give each
grant of such an action a credential as `GH_TOKEN`.

### Branch rules

`scripts/git_plugin/rules.py` holds git-sidecar's rules. The protected branches are `main` and
`master`, and no grant changes them. The allowed branch prefixes come from the cage's environment
variable `GIT_BRANCH_PREFIXES`, which the grant's `env` sets. Its value is a comma-separated list.
The plugin strips the whitespace from each entry and ignores an empty entry, so
`"task/, dependabot/"` allows `task/` and `dependabot/`.

- `push` and `checkout` read the prefixes before any other step. When the variable is unset or names
  no prefix, each refuses the call, a checkout of `main` included.
- `push` takes only `repo`, and pushes the current branch. It refuses a protected branch, a branch
  that begins with no allowed prefix, and a detached HEAD.
- `push` names both sides of its refspec and uses no `+`, so no push refspec in the repository's
  config can force the update. A `force` input fails the input schema.
- When the repository tracks LFS files, `push` uploads them with `git lfs push` before the ref push.
- `checkout` lets a protected target through. Any other target needs an allowed prefix, with or
  without `create`.
- `merge` and `commit` check no branch. Nor does `worktree`: `add` makes or attaches any branch its
  input schema accepts, so it can make a branch that `push` refuses. Its one rule is on `path`.

### Commit identity

`commit` takes its author and committer from the repository's `user.name` and `user.email`. The cage
sees no global git config of the agent's. For a value that the repository leaves unset or empty,
`commit` takes `GIT_AUTHOR_NAME` or `GIT_AUTHOR_EMAIL` from the cage's environment, which the
grant's `env` sets. The repository's own value wins, and each value resolves on its own. `commit`
refuses when a value is unset or empty in both places. The author and the committer are always the
same identity.

A `git.commit` grant that gives every repository under its roots one identity:

```yaml
git.commit:
  roots: [{path: /home/agent-01/Projects/example-repo, mode: rw}]
  domains: []
  credentials: {}
  env: {GIT_AUTHOR_NAME: Agent One, GIT_AUTHOR_EMAIL: agent-01@example.com}
```

`merge` reads no identity itself. git takes a merge commit's author and committer from
`GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` when they are
set, and these variables win over the repository's config. So a `git.merge` grant whose `env` sets
all four makes merge commits as that identity.

### Hooks

`git_argv` passes `-c core.hooksPath=/dev/null` on every git run, so git runs no hook of the
repository's, `git lfs` hooks included. `push` uploads LFS objects with its own `git lfs push`, so
it needs no LFS `pre-push` hook. `docs/design.md` gives why hooks are off under "The git plugin's
constraints", and what planted code in a cage can still reach under "Known gaps".

## A grant

A config pins the plugin's image as `git`, and grants each action under `git.<action>`. In the form
`docs/design.md` gives under "Config":

```yaml
scopes:
  dev-agent:
    grants:
      git.status:
        roots: [{path: /home/agent-01/Projects/example-repo, mode: rw}]
        domains: []
        credentials: {}
      git.push:
        roots: [{path: /home/agent-01/Projects/example-repo, mode: rw}]
        domains: [github.com]
        credentials: {github-token: {env: GH_TOKEN}}
        env: {GIT_BRANCH_PREFIXES: "task/"}
```

Each `git.push` and `git.checkout` grant sets `GIT_BRANCH_PREFIXES` in its `env`, as "Branch rules"
gives. "`diff` with `output`" gives the root a `git.diff` grant needs for `output`.

A grant's `domains` name each host its remote reaches. For a repository that tracks LFS files, they
also name each host the LFS server sends objects to, which can differ from the remote's host.

## Layout

- `scripts/git_plugin/` is the module. `cage.py` holds the wrapper and `git_argv`, `scripts.py`
  holds the script actions, and `rules.py` holds the branch rules.
- `scripts/entry` is the one executable every action runs. `entry.py` acts by the name the
  executable runs under: `git-cage` runs the wrapper, and the name of a script action runs that
  action.
- `Dockerfile` builds the image on the Alpine base the core's test plugin pins, with git, git LFS,
  and Python 3 from apk. It links `git-cage` on the `PATH`, and each script action's `executable`
  under `/plugin/bin/`, to `/plugin/entry`.
- `describe` prints `manifest.json`.
- `tests/unit/` runs the module with the host's git and git LFS, and checks the manifest. It needs
  no Docker.
- `tests/integration/` builds the plugin image and the core's verifier image, and runs each action
  in a real cage through the core's `CageRunner`. `conftest.py` there builds the fixture
  repositories, one of them owned by uid 1000 from the image in `tests/fixture-image/`.
  `auth_server.py` there serves the credential helper's test.

## Build and test

Build the image from this directory, then check its manifest from `core/agent-proxy/`:

```sh
docker build -t agent-proxy-git .
cd ../../core/agent-proxy
uv run agent-proxy manifest check agent-proxy-git
```

Run the checks from this directory:

```sh
poe lint
poe test
poe test-integration
```

`poe test` runs the unit tests and needs only the host's git and git LFS. `poe test-integration`
runs on the Docker daemon that `DOCKER_HOST` names. `.dev/verification.md` gives which bar runs each
check, and what the integration tests must leave behind.
