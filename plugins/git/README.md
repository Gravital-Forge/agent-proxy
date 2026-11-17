# The git plugin

The git plugin runs git and git LFS in a cage. It offers git and git LFS actions, and it applies
branch rules to `push` and `checkout`. `docs/writing-a-plugin.md` gives the plugin model this
follows, and `docs/design.md` gives the decisions behind it under "The git plugin's constraints".

## The actions

Each action's MCP tool name is `<plugin>_<action>`, where `<plugin>` is the name a config pins the
image as. A config that pins the image as `git`, as "A grant" does, serves `status` as `git_status`
and `lfs_pull` as `git_lfs_pull`. `manifest.json` declares every action, its input schema, its
credentials, and its timeout.

An action is a command action when its git argv is a fixed prefix plus typed parameters. It is a
script action when it does more than that:

- It carries a rule, as `push`, `checkout`, and `worktree` do.
- It runs more than one git command, as `commit` and `push` do.
- It takes a list, as each action with a `files` or `patterns` input does.
- It names a file git reads, as `config_get` does with the repository's config file.

No action takes pass-through `args`. Its items would be exempt from the core's refusal of a value
that begins with `-`, so they could pass git an option such as `diff --output`.

### The `repo` input

Every action takes a required `repo`, the absolute path of the repository. git runs as
`git -C <repo>`. The path must lie under one of the grant's roots: no other host path exists in the
cage, so git fails with its own error. A command action passes `repo` to the wrapper as the flag
`--repo`, and the wrapper takes the flag and its value out before git runs.

`repo` names the top level of a checkout: a main checkout or a linked worktree. The plugin refuses a
`repo` that is a directory with no `.git`, such as a directory inside a checkout. A `repo` that does
not exist reaches git, and git's error is the result. "Repository shapes" gives the checkouts the
plugin refuses for their shape.

A file or pattern input with a `..` segment fails the input schema, so the core refuses the call
before a cage starts.

### What an action returns

A command action returns `{exit_code, stdout, stderr}`, as `docs/design.md` gives under "Results". A
script action prints the same three members, and its `output_schema` states them. The script exits 0
whenever it runs to its end, and git's exit code goes in `exit_code`. `diff` with `output` is the
one exception to both rules: "`diff`" gives its result.

When a git step inside a script fails for a reason other than the outcome that step checks, the
script stops and returns that step's git result. For example, `push` returns git's own result when
`repo` does not exist. One step is the exception: `push` reads a failed `git lfs ls-files` as no LFS
file, and goes on.

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
- `GIT_SIGNING_KEY` is set, but is not an absolute path or names no readable regular file.
- The credential helper is asked for a repository that `GITHUB_REPOS` does not name.
- The credential helper gets a request whose path does not name `owner/name`, or runs while
  `GH_TOKEN` is unset or empty.
- `diff`'s `output` does not resolve strictly beneath the repository's `.agent-proxy` directory.
- `worktree` with `add` names a `path` inside the repository it comes from.
- `commit` runs while `GIT_AUTHOR_NAME` or `GIT_AUTHOR_EMAIL` is unset or empty, or finds nothing
  staged.
- `repo` is a directory with no `.git`, its `.git` file names no git directory, or the plugin cannot
  read a file it reads to find the repository's git directories or config.
- `repo` has a shape that "Repository shapes" refuses.
- The image's executable runs under a name that is not `git-cage`, `git-credential-agent-proxy`, or
  a script action's.

The credential helper refuses in its own way: it writes the refusal to stderr and answers git
`quit=1`, so git fails with its own exit code, and the action's `stderr` holds the refusal.

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
`remove` removes the worktree at `path`. git names the main worktree after the common directory it
reads, which in a cage is the scratch directory that "Config isolation" gives, so `list` puts the
main worktree's real path in its place.

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
does not take, such as a `message` with `pop`.

### `config_get`

`config_get` prints the value of `key` from the repository's own config file: the main repository's
`.git/config`, for a linked worktree too. git reads that file as data, with
`git config --file <file> --no-includes --get`. So the answer is what the file holds, and never a
value the plugin passes with `-c`, a value from a file the config includes, or a value from a
`config.worktree`. An unset key gives git's exit code 1 and an empty `stdout`.

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
the path of the URL it connects to, after `remote.<name>.pushurl` rewrites that URL. git in a cage
reads no `url.<base>.insteadOf`, as "Config isolation" gives. `docs/design.md` gives why the helper
checks this path under "The git plugin's constraints".

On git's `get`, the helper reads the path's first two segments as `owner/name`, with one trailing
`.git` dropped from the name, and checks that repository against `GITHUB_REPOS`, as "The repository
list" gives. When the list names it, the helper answers with `x-access-token` and the value of
`GH_TOKEN`. Otherwise the helper refuses: it writes the reason to stderr and answers `quit=1`, so
git stops and fails. The helper refuses the same way when `GH_TOKEN` is unset or empty, or when the
path does not name `owner/name`:

- The request has no path, or a path of fewer than two segments.
- A segment anywhere in the path is `.` or `..`.
- The first segment is not an owner, or the second, its `.git` dropped, is not a name, by the
  patterns "The repository list" gives.

git decodes percent escapes in the path it hands the helper, but sends the request still encoded. A
`%2F..` in a URL therefore reaches the helper as a `..` segment that the request never resolves, so
the helper refuses every `.` and `..` segment.

The helper answers `store` and `erase` with nothing. It reads the path and never the host, because
the grant's `domains` bound the host.

GitHub serves a public repository without a credential, so git fetches an unlisted public repository
without asking the helper. The list bounds where the token goes. Every push to GitHub asks the
helper.

Before git runs, the wrapper and each script action check the cage's environment. In a cage that
holds `GH_TOKEN`, they refuse the call when `GITHUB_REPOS` is unset, names no entry, or holds a
malformed entry, whether or not git would ask the helper. So a grant that lacks the list fails on
its first call, a fetch from a public repository or a local path included. In a cage without
`GH_TOKEN`, this check reads no list.

Each action that reaches a remote declares `{"env": "GH_TOKEN"}` in its `credentials` in
`manifest.json`. The core does not compare a grant's credentials with that declaration, so the check
keys on `GH_TOKEN` and not on the action. Give each grant of such an action a credential as
`GH_TOKEN`, and set `GITHUB_REPOS` in its `env`.

### Branch rules

`scripts/git_plugin/rules.py` holds the branch rules. The protected branches are `main` and
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
- Once the ref push succeeds, `push` makes the branch track origin's branch of the same name. It
  writes `branch.<branch>.remote` and `branch.<branch>.merge` to the repository's real config file
  with `git config --file <file> --no-includes`, so a later `pull` finds the upstream. A failed
  write makes the result's `exit_code` non-zero, and its stderr follows the push's.
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

The list guards against an agent's mistake. No code a repository plants runs in a git cage to get
past it, as "Config isolation" gives.

### Commit identity

Every action that creates a commit takes its identity from the grant's `env` alone: `commit`, a
`merge` that makes a merge commit, and `stash`. git takes the author from `GIT_AUTHOR_NAME` and
`GIT_AUTHOR_EMAIL`. For each of `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` that the grant leaves
unset, the plugin sets the author's value, so the committer is the author. A grant that sets all
four keeps its own committer. git takes no `user.name` or `user.email` from the repository's config,
as "Config isolation" gives, and the cage holds no global config of the agent's.

`commit` refuses when `GIT_AUTHOR_NAME` or `GIT_AUTHOR_EMAIL` is unset or empty. `merge` and `stash`
check no identity themselves, so their grants set the two variables too.

`pull` creates no commit. git in a cage takes no `pull.rebase` or `pull.ff` from the repository, so
`pull` only fast-forwards: when the branches have diverged, git fails and asks how to reconcile
them.

A `git.commit` grant that gives every repository under its roots one identity:

```yaml
git.commit:
  roots: [{path: /home/agent-01/Projects/example-repo, mode: rw}]
  domains: []
  credentials: {}
  env: {GIT_AUTHOR_NAME: Agent One, GIT_AUTHOR_EMAIL: agent-01@example.com}
```

### Commit signing

A grant can make the plugin sign each commit it creates with an SSH key. The setting has two halves:

- The key is a credential. After the instance starts, the admin stores the private key with
  `agent-proxy credential set <name>`, as any other credential, and the grant gives it in the file
  form, `{file: /run/credentials/<name>}`. `docs/runbooks/per-agent-instance.md` gives the command
  under "The first start".
- `GIT_SIGNING_KEY` in the grant's `env` names that file's path in the cage.

The plugin signs in the SSH format only. The key must have no passphrase, because the cage has no
terminal to ask for one and no SSH agent to hold the key.

When `GIT_SIGNING_KEY` is set, `git_argv` in `scripts/git_plugin/cage.py` passes these values on
every git run, after the credential helper's values and before the action's arguments:

```text
-c gpg.format=ssh
-c gpg.ssh.program=/usr/bin/ssh-keygen
-c user.signingKey=<the value of GIT_SIGNING_KEY>
-c commit.gpgSign=true
```

When `GIT_SIGNING_KEY` is unset, `git_argv` passes `-c commit.gpgSign=false` instead. A value given
with `-c` wins over the repository's config, so the grant alone decides: a repository's config can
neither turn signing on nor turn it off. `config_get` reads the repository's own config file, so it
reports none of these values.

With signing on, git signs each commit that an action creates: the commit that `commit` makes, and
the merge commit that `merge` makes. git does not sign the commits that `stash` makes.

Before git runs, the wrapper and each script action check `GIT_SIGNING_KEY`, whatever the action.
They refuse the call when the value is set but is not an absolute path, or names no readable regular
file. An empty value counts as set, so it is refused too. A grant with a wrong path therefore fails
on its first call, a `status` call included.

GitHub shows a signed commit as Verified when both of these hold:

- The public key is on a GitHub account as a signing key.
- The commit's committer email is a verified email of that same account. "Commit identity" gives
  where `commit` takes the committer from.

A `git.commit` grant that signs each commit:

```yaml
git.commit:
  roots: [{path: /home/agent-01/Projects/example-repo, mode: rw}]
  domains: []
  credentials: {git-signing-key: {file: /run/credentials/git-signing-key}}
  env:
    GIT_AUTHOR_NAME: Agent One
    GIT_AUTHOR_EMAIL: agent-01@example.com
    GIT_SIGNING_KEY: /run/credentials/git-signing-key
```

A `git.merge` grant signs its commits only when it also gives the key and sets `GIT_SIGNING_KEY`.

The key stays a file mounted in each cage whose grant gives it. No code that a repository plants can
run in the cage to read it: hooks are off, as "Hooks" gives, and git runs no program that the
repository's config names, as "Config isolation" gives.

### Hooks

`git_argv` passes `-c core.hooksPath=/dev/null` on every git run, so git runs no hook of the
repository's, `git lfs` hooks included. `push` uploads LFS objects with its own `git lfs push`, so
it needs no LFS `pre-push` hook. `docs/design.md` gives why hooks are off under "The git plugin's
constraints".

### Config isolation

git in a cage never takes its settings from the repository's config. The agent writes `.git/config`
with its own file tools, and a key there can name a program for git to run: a filter driver, a
`diff` textconv, `core.fsmonitor`, or an LFS custom transfer. So every action, through the wrapper
or as a script, runs git against a config that the plugin builds for that action. `build_isolation`
in `scripts/git_plugin/cage.py` builds it, and `docs/design.md` gives the decision under "The git
plugin's constraints".

Before git runs, the plugin finds the repository's real git directories by reading files, with no
git run. The git directory is `<repo>/.git`, or the directory a `.git` file names after `gitdir: `.
A `commondir` file in the git directory names the common directory, the main repository's `.git`;
with no such file, the git directory is the common directory. The plugin then reads the real config
once, as data: `git config --file <common directory>/config --no-includes --list -z`, run from `/`
with no `GIT_` variable, so git finds no repository and follows no include.

The plugin makes a scratch common directory in the cage's `/tmp`. It holds the scratch config, a
stub `HEAD`, an empty `refs/`, and links `worktrees`, `info`, and `lfs` to the real common
directory's entries. git runs with:

- `GIT_DIR` at the real git directory, so `HEAD`, the index, and merge state are the real files.
- `GIT_COMMON_DIR` at the scratch directory, so git reads the scratch config.
- `GIT_OBJECT_DIRECTORY` at the real common directory's `objects`.
- `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` at `/dev/null`.
- No `GIT_ALLOW_PROTOCOL`, so the transport pins govern.

The scratch config holds two kinds of key, and no other key reaches git:

- Fixed keys that the plugin supplies. `core.repositoryformatversion = 1` and `core.bare = false`.
  `extensions.refStorage = files://<real common directory>`, so refs, reflogs, and `packed-refs`
  resolve in the real store; this needs git 2.54 or later. The `filter.lfs` keys `clean`, `smudge`,
  `process`, and `required`, because the image's system config that holds them is off. The first
  three name `/usr/bin/git-lfs`, and `required` is `true`.
- Transport keys copied from the real config: `url`, `fetch`, and `pushurl` of each `remote.<name>`,
  and `remote` and `merge` of each `branch.<name>`. None of them names a program for git to run: a
  remote URL in a form that runs one fails on the transport pins below. Each copied value is quoted,
  so no value or name can open a key of its own.

The plugin's code fixes both sets. A key it does not name stays in the file git does not read,
whatever the key does. An `include.path`, a `url.<base>.insteadOf`, a `credential.*` key, or a
`config.worktree` therefore has no effect in a cage.

git LFS still reads a working-tree `.lfsconfig`, but takes from it only a restricted set of keys,
and none of them names a program. git LFS reads its other keys from git's config, which is the
scratch config.

git writes each config change that an action makes to the scratch config, and the cage discards it.
`push` writes the branch's tracking to the real config itself, as "Branch rules" gives.

#### Transport pins

`git_argv` passes these values on every git run, after the signing values:

```text
-c protocol.file.allow=never
-c protocol.ext.allow=never
-c protocol.fd.allow=never
```

An `ext::` URL runs a program in the cage, and an `fd::` URL talks to a file descriptor in place of
a remote. A `file://` URL or a local path starts a local `upload-pack` or `receive-pack` that git
gives none of the plugin's `-c` values and none of the isolation's environment, so the target
repository's own config and hooks would run in the cage. A remote URL in one of these forms fails on
the denied transport. Network transports stay allowed: the egress proxy bounds where they go, as
`docs/design.md` gives under "The cage".

The pins govern git's transports only. A `.lfsconfig` can set `lfs.url` to a local path. git LFS
then copies objects with its built-in standalone file adapter, which runs no program.

### Repository shapes

The plugin acts on a main checkout or a linked worktree, LFS files included. Before git runs, every
action checks the repository's shape against what the scratch directory reproduces, and refuses the
rest. The refusal names the shape:

- A shallow clone: the common directory holds a `shallow` file.
- Refs not in the files store: the common directory holds `reftable/`, or the config holds an
  `extensions.refStorage` other than the bare value `files`, a `files://<path>` included.
- Submodules: the working tree holds a `.gitmodules`, tracked or not, or the config holds a
  `submodule.*` key.
- A sparse checkout: the git directory holds `info/sparse-checkout`, or the config holds a
  `core.sparseCheckout` that git reads as true. `git sparse-checkout` writes its
  `core.sparseCheckout` to `config.worktree`, which git in a cage does not read, so `checkout` would
  fill the whole working tree. A checkout whose sparse mode is off but whose patterns file remains
  is refused too.
- A partial clone: the config holds a `remote.<name>.promisor` that git reads as true.
- Any other extension: the config holds an `extensions.*` key other than `refStorage = files` and
  `worktreeConfig`. A SHA-256 repository and a repository with relative worktree links are refused
  by this rule, for example.

A repository that sets `extensions.worktreeConfig` passes, and git reads none of its
`config.worktree` files. The check reads the config entries that the isolation's one config read
gives, and the files in the repository, with no further git run.

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

Grant a root of mode `rw` to each action that can run the LFS clean filter on a working-tree file:
`status`, `blame`, and `lfs_status`. The filter writes a temporary file under the `lfs` directory of
the repository's git directory. For a linked worktree, that is the main repository's git directory.
Under a root of mode `ro`, the action fails with a read-only file system error.

- `blame` runs the file it names through the filter on every call.
- `status` and `lfs_status` run a file through the filter when git cannot trust the file's cached
  stat data. A new worktree triggers this: git cannot trust the stat data of a file written in the
  same second as the index.

## Layout

- `scripts/git_plugin/` is the module. `cage.py` holds the wrapper, `git_argv`, the isolation
  builder, and the shape check. `scripts.py` holds the script actions, `rules.py` holds the branch
  rules and the repository list, and `credential.py` holds the credential helper.
- `scripts/entry` is the one executable every action runs. `entry.py` acts by the name the
  executable runs under: `git-cage` runs the wrapper, `git-credential-agent-proxy` runs the
  credential helper, and the name of a script action runs that action.
- `Dockerfile` builds the image on the Alpine base the core's test plugin pins, with git, git LFS,
  `openssh-keygen`, and Python 3 from apk. It links `git-cage` on the `PATH`, and
  `git-credential-agent-proxy` and each script action's `executable` under `/plugin/bin/`, to
  `/plugin/entry`.
- `describe` prints `manifest.json`.
- `tests/unit/` runs the module with the host's git, git LFS, and `ssh-keygen` at
  `/usr/bin/ssh-keygen`, and checks the manifest. It needs no Docker.
- `tests/integration/` builds the plugin image and the core's verifier image, and runs each action
  in a real cage through the core's `CageRunner`. `conftest.py` there builds the fixture
  repositories, one of them owned by uid 1000 from the image in `tests/fixture-image/`, and serves
  the remotes that cages reach over `git://` from a git daemon in that image. `auth_server.py` there
  serves the credential helper's tests.

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

`poe test` runs the unit tests and needs only the host's git, git LFS, and `ssh-keygen` at
`/usr/bin/ssh-keygen`. `poe test-integration` runs on the Docker daemon that `DOCKER_HOST` names.
`.dev/verification.md` gives which bar runs each check, and what the integration tests must leave
behind.
