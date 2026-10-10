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
before a cage starts.

### What an action returns

A command action returns `{exit_code, stdout, stderr}`, as `docs/design.md` gives under "Results". A
script action prints the same three members, and its `output_schema` states them. The script exits 0
whenever it runs to its end, and git's exit code goes in `exit_code`. `diff` with `output` is the
one exception to both rules: "`diff`" gives its result.

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
- A call breaks a branch rule.
- `push` or `checkout` runs while `GIT_BRANCH_PREFIXES` is unset or names no prefix.
- A call runs in a cage that holds `GH_TOKEN` while `GITHUB_REPOS` is unset, names no entry, or
  holds an entry that is neither `owner/name` nor `owner/*`.
- The credential helper is asked for a repository that `GITHUB_REPOS` does not name.
- The credential helper gets a request with no path or with a path of fewer than two segments, or
  runs while `GH_TOKEN` is unset or empty.
- `diff`'s `output` does not resolve strictly beneath the repository's `.agent-proxy` directory.
- `worktree` with `add` names a `path` inside the repository it comes from.
- `commit` finds no identity, or nothing staged.
- The image's executable runs under a name that is not `git-cage`, `git-credential-agent-proxy`, or
  a script action's.

The refusal text of a broken branch rule uses git-sidecar's wording. The credential helper refuses
in its own way: it writes the refusal to stderr and answers git `quit=1`, so git fails with its own
exit code, and the action's `stderr` holds the refusal.

### Output past the cap

A large `diff` is the usual call whose output passes the core's cap. Give it `output`, and the diff
goes to a file while the result holds only the file's path and a stat. `docs/writing-a-plugin.md`
gives what happens to a script whose output passes the cap, and `docs/design.md` gives the cap and
the result forms under "Results".

### `diff`

`diff` shows the working tree's changes, or with `staged` the index's, against `ref`, limited to
`files` when it is given. Without `output`, the diff is the result's `stdout`.

With `output`, the diff goes to a file instead. `output` is a path relative to the repository's
`.agent-proxy` directory, `<repo>/.agent-proxy/`, with the symlinks in `repo` resolved. The input
schema fails an `output` that is empty, begins with `/`, or holds a `..` segment, so the core
refuses the call before a cage starts. In the cage, the script resolves the path with its symlinks
followed, and refuses a path that does not lie strictly beneath the directory.

The script runs `git diff` on the selection, then the same selection with `--stat=1000,900`, a width
that keeps long paths whole. When either git call fails, its result is the action's result, and
nothing is written. Then the script writes git's stdout to the file, byte for byte:

- When the directory is missing, the script makes it, with a `.gitignore` that holds `*`, so git
  reports nothing in it. An existing directory is left as found.
- The script makes each missing directory along `output`.
- An existing file is overwritten. A selection with no changes writes an empty file.
- Each step opens its directory or file without following a symlink, so a symlink planted on the
  path fails the write. A file that is not a regular file, or that has other hard links, fails the
  write and keeps its contents.

On success, the result is `{exit_code, stderr, path, stat}`: git's exit code and stderr, the
absolute path of the file, and the stat's stdout. It has no `stdout`. A failed write gives exit code
1, an empty `stdout`, and a `stderr` of `diff could not be written to <path>: <error>`.

The write needs a root of mode `rw`. Under a grant whose root is `ro`, the write fails with the
read-only file system error, and `diff` returns that failure.

### `worktree`

`worktree` takes an `action`: `add`, `list`, or `remove`. `list` runs `git worktree list`, and
`remove` removes the worktree at `path`.

`add` makes a linked worktree at `path`:

- With `branch`, and `create_branch` true or absent, it creates the branch `branch` there.
- With `branch` and `create_branch` false, it checks out the existing branch `branch` there.
- Without `branch`, git uses a branch named after the last component of `path`, and creates that
  branch from `HEAD` when it is missing.

The input schema takes only an absolute `path`, because git resolves a relative one against `repo`.
It also fails a `branch` that begins with `-`. `add` refuses a `path` that resolves, its symlinks
followed, to the repository or to a path beneath it. `add` or `remove` without `path` runs git
without one, and git's error is the result.

The new worktree's path must lie under one of the grant's roots, as `repo` must, because no other
host path exists in the cage. A `git.worktree` grant names a root of mode `rw` above both.

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

Every git run clears every inherited credential helper, then names the plugin's own helper,
`/plugin/bin/git-credential-agent-proxy`, and sets `credential.useHttpPath=true`.
`scripts/git_plugin/credential.py` holds the helper. With `useHttpPath` set, git hands the helper
the path of the URL it connects to, after `url.<base>.insteadOf` and `remote.<name>.pushurl` rewrite
that URL. `docs/design.md` gives why the helper checks this path under "The git plugin's
constraints".

On git's `get`, the helper reads the path's first two segments as `owner/name`, with one trailing
`.git` dropped from the name, and checks that repository against `GITHUB_REPOS`, as "The repository
list" gives. When the list names it, the helper answers with `x-access-token` and the value of
`GH_TOKEN`. Otherwise the helper refuses: it writes the reason to stderr and answers `quit=1`, so
git stops and fails. A request with no path, a path of fewer than two segments, and an unset or
empty `GH_TOKEN` are refused the same way. The helper answers `store` and `erase` with nothing. It
reads the path and never the host, because the grant's `domains` bound the host.

GitHub serves a public repository without a credential, so git fetches an unlisted public repository
without asking the helper. The list bounds where the token goes. Every push to GitHub asks the
helper.

Before git runs, the wrapper and each script action check the cage's environment. In a cage that
holds `GH_TOKEN`, they refuse the call when `GITHUB_REPOS` is unset, names no entry, or holds a
malformed entry, whether or not git would ask the helper. So a grant that lacks the list fails on
its first call, a fetch from a public repository or a local path included. A cage without `GH_TOKEN`
reads no list.

Each action that reaches a remote declares `{"env": "GH_TOKEN"}` in its `credentials` in
`manifest.json`. The core does not compare a grant's credentials with that declaration, so the check
keys on `GH_TOKEN` and not on the action. Give each grant of such an action a credential as
`GH_TOKEN`, and set `GITHUB_REPOS` in its `env`.

### Branch rules

`scripts/git_plugin/rules.py` holds git-sidecar's rules. The protected branches are `main` and
`master`, and no config changes them. The allowed branch prefixes come from `GIT_BRANCH_PREFIXES` in
the cage's environment, which the grant's `env` sets. Its value is a comma-separated list: each
entry is stripped of whitespace, and an empty entry is ignored, so `task/, dependabot/` names
`task/` and `dependabot/`. `push` and `checkout` read the variable on every call, before git runs.
When it is unset or names no prefix, they refuse the call, even for `main`.

- `push` takes only `repo`, and pushes the current branch. It refuses a protected branch, a branch
  that begins with no allowed prefix, and a detached HEAD.
- `push` names both sides of its refspec and uses no `+`, so no push refspec in the repository's
  config can force the update. A `force` input fails the input schema.
- When the repository tracks LFS files, `push` uploads them with `git lfs push` before the ref push.
- `checkout` lets a protected target through. Any other target must begin with an allowed prefix,
  with or without `create`.
- `worktree`, `merge`, and `commit` check no branch.

### The repository list

`GITHUB_REPOS` in the cage's environment names the GitHub repositories the plugin hands the token
for. The grant's `env` sets it. Its value is split as `GIT_BRANCH_PREFIXES` is: a comma-separated
list, each entry stripped of whitespace, and an empty entry ignored. An entry takes one of two
forms:

- `owner/name` names one repository.
- `owner/*` names every repository of one user or organization.

An owner is letters, digits, and `-`, and begins with a letter or a digit. A name is letters,
digits, `_`, `.`, and `-`, and is not only dots. These are the two halves of the github plugin's
`repo` pattern. Any other entry is malformed: a bare `owner`, `*`, `*/name`, or a partial glob such
as `owner/api-*`.

A repository matches an entry when the owners are equal and the names are equal, or the entry's name
is `*`. Each comparison ignores case, as GitHub's does. A renamed or transferred repository is
judged by the name in the URL git connects to, never by the name GitHub redirects to.

The list fails closed. In a cage that holds `GH_TOKEN`, an unset list, a list with no entry, and a
list with a malformed entry each refuse the call. `rules.py` holds the rule. The github plugin
applies the same rule with its own copy.

The list guards against an agent's mistake. It is not a boundary: `docs/design.md` gives under
"Known gaps" how code planted in a repository gets past it.

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
        env: {GIT_BRANCH_PREFIXES: "task/", GITHUB_REPOS: "example-org/*"}
```

A grant's `domains` name each host its remote reaches. For a repository that tracks LFS files, they
also name each host the LFS server sends objects to, which can differ from the remote's host. The
`git.push` and `git.checkout` grants set `GIT_BRANCH_PREFIXES` in their `env`, as "Branch rules"
gives. Every grant whose `credentials` give `GH_TOKEN`, such as `git.push`, sets `GITHUB_REPOS` in
its `env`, as "The repository list" gives.

## Layout

- `scripts/git_plugin/` is the module. `cage.py` holds the wrapper and `git_argv`, `scripts.py`
  holds the script actions, `rules.py` holds the branch rules and the repository list, and
  `credential.py` holds the credential helper.
- `scripts/entry` is the one executable every action runs. `entry.py` acts by the name the
  executable runs under: `git-cage` runs the wrapper, `git-credential-agent-proxy` runs the
  credential helper, and the name of a script action runs that action.
- `Dockerfile` builds the image on the Alpine base the core's test plugin pins, with git, git LFS,
  and Python 3 from apk. It links `git-cage` on the `PATH`, and `git-credential-agent-proxy` and
  each script action's `executable` under `/plugin/bin/`, to `/plugin/entry`.
- `describe` prints `manifest.json`.
- `tests/unit/` runs the module with the host's git and git LFS, and checks the manifest. It needs
  no Docker.
- `tests/integration/` builds the plugin image and the core's verifier image, and runs each action
  in a real cage through the core's `CageRunner`. `conftest.py` there builds the fixture
  repositories, one of them owned by uid 1000 from the image in `tests/fixture-image/`.
  `auth_server.py` there serves the credential helper's tests.

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
