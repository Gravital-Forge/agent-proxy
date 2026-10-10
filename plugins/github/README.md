# The github plugin

The github plugin runs gh, the GitHub CLI, in a cage. It offers one action for each GitHub tool that
git-sidecar offers, and `job_log_url`, which git-sidecar lacks. `docs/writing-a-plugin.md` gives the
plugin model this follows.

## The actions

Each action that git-sidecar has is named as its git-sidecar tool, without the `gh_` prefix:
`gh_pr_create` becomes `pr_create`, and `gh_run_view` becomes `run_view`. The name a config pins the
image as sets the MCP tool names, which `docs/design.md` gives as `<plugin>_<action>`. A config that
pins the image as `github`, as "A grant" does, serves `github_pr_create` and the rest. A config that
pins it as `gh` instead serves the names that git-sidecar serves, and `gh_job_log_url`.
`manifest.json` declares every action, its input schema, its credentials, and its timeout.

An action is a command action when its argv is `gh-cage`, a fixed gh prefix, and typed parameters,
as for `pr_list`, `run_view`, and `run_list`. "The repository list" gives what `gh-cage` does. The
others are script actions, because each does more than that:

- It runs more than one gh command, as `pr_fetch` does.
- It reads a job's log URL from GitHub's API itself, as `pr_checks` and `job_log_url` do. "Job log
  URLs" gives why gh cannot.
- It applies a default that a command action cannot state, as `pr_view` does with its field list.
- It takes free text, such as a title, a body, or a comment, that may begin with `-`. The core
  refuses such a value for a command action, and a Markdown body that opens with a list item begins
  with `-`. `pr_create`, `pr_edit`, `pr_reply`, and `pr_close` pass each text value as one argv
  item, `--flag=value` or gh api's `-f body=<text>`, so gh never reads it as an option.

A value that a script places alone in argv cannot begin with `-` either: `pr_view`'s `pr` must not,
and each number is an integer from 1 up.

No action takes pass-through `args`. Its items would be exempt from the core's refusal of a value
that begins with `-`.

### The `repo` input

Every action takes a required `repo`, the GitHub repository as `owner/name`, and passes it to gh as
`--repo`. gh needs no checkout for that. So every grant of the plugin has `roots: []`: a cage with
no roots reaches less than one that mounts the agent's tree.

The input schema takes an owner of letters, digits, and `-` that begins with a letter or a digit,
then one slash, then a name of letters, digits, `_`, `.`, and `-` that is not only dots. A script
builds each `gh api` path from `repo`, so the path stays under `repos/<owner>/<name>/`.

`pr_create` takes a required `head`, the branch the pull request comes from. git-sidecar takes that
branch from the checkout, and the cage has no checkout.

### The repository list

Every action checks its `repo` against `GITHUB_REPOS` in the cage's environment before gh runs. The
grant's `env` sets the variable. The rule is the git plugin's, and `repos.py` holds this plugin's
own copy of it. `plugins/git/README.md` gives the entry forms, the match, and the refusals of an
unset, empty, or malformed list under "The repository list".

A script action checks `repo` before the action runs. A command action runs `gh-cage`, a wrapper
that finds the one `--repo` in its argv and checks the value after it. On a match, `gh-cage`
replaces itself with gh on the same argv, `--repo` kept.

`pr_create` checks `repo` alone, never `head`. `head` may name a branch on another owner's fork,
which the pull request only reads.

The cage mounts no repository, so nothing inside the plugin gets past the check.

### What an action returns

A command action returns `{exit_code, stdout, stderr}`, as `docs/design.md` gives under "Results". A
script action prints the same three members, and its `output_schema` states them. The script exits 0
whenever it runs to its end, and gh's exit code goes in `exit_code`. `job_log_url` runs no gh, so
its exit code is its own, as given below. Each action returns gh's stdout as gh prints it, except
for `pr_fetch`, `pr_checks`, and `job_log_url`.

`pr_fetch` makes three `gh api` calls in this order: the pull request's inline comments, its
reviews, and its conversation comments. Each call fetches one page, as gh api does without
`--paginate`. The script stops at the first call that fails, and returns that call's result. When
all three succeed, `exit_code` is 0, `stderr` is empty, and `stdout` is one compact JSON object:
`{"inline_comments": [...], "reviews": [...], "conversation": [...]}`.

`pr_checks` runs `gh pr checks`. When that call fails with an empty stdout, its result is the
action's result. Otherwise the script reads its stdout line by line. A line that holds `fail`, in
any case, and a link with `/actions/runs/<run>/job/<job_id>` names a failed job. For each failed
job, once and in the order first seen, the script reads the job's log URL as `job_log_url` does, and
appends one line to the checks' stdout: `Failed job <job_id> log: <url>`, or
`Failed job <job_id> log unavailable: <reason>` when it gets no URL. Each appended line ends with a
newline. When the checks' stdout does not end with a newline and a line follows it, the script adds
the newline first. The result holds the checks' exit code, the built stdout, and the checks' stderr.

`job_log_url` takes `repo` and `job_id`, an integer from 1 up, and returns a fresh URL to the job's
plain-text log. On success, `exit_code` is 0, `stdout` is the URL and a newline, and `stderr` is
empty. When it gets no URL, `exit_code` is 1, `stdout` is empty, and `stderr` gives the reason:
`GET <path> answered <status>: <body>`, with the first 200 characters of the answer's body, or
`GET <path> failed: <error>`.

The URL is signed and short-lived. Fetch it soon after the call returns, and call `job_log_url`
again for a fresh URL when it has expired. The agent fetches the URL itself, outside the cage, as
"Egress" gives.

### Job log URLs

GitHub answers `GET /repos/<repo>/actions/jobs/<job_id>/logs` with a redirect, and the redirect's
`Location` header holds the job's signed log URL. gh's HTTP client follows a redirect, so `gh api`
would go on to the storage host the URL names, and a grant that names only `api.github.com` refuses
that host. The script therefore sends the request itself with Python's `urllib`, through an opener
whose redirect handler follows nothing, and reads `Location` from the answer. It sends `GH_TOKEN` as
a bearer token. urllib reaches the API through the proxy that `HTTPS_PROXY` names, which is the
cage's egress proxy, and its default TLS context reads `SSL_CERT_FILE`.

### Refusals

The plugin refuses a call with exit code 2 and a stderr that begins `github plugin refused: `, then
gives the reason. A script refuses through its printed `exit_code`, and the image's executable
refuses as its own exit. The plugin refuses these:

- A `pr_fetch` call exits 0 but prints what is not JSON. The reason names the call's path.
- The image's executable runs under a name that is neither `gh-cage` nor a script action's.
- `job_log_url` runs, or `pr_checks` finds a failed job, while `GH_TOKEN` is unset or empty.
- `GITHUB_REPOS` is unset, names no entry, or holds an entry that is neither `owner/name` nor
  `owner/*`.
- `repo` names a repository that `GITHUB_REPOS` does not list.
- `gh-cage` finds `--repo` missing, repeated, or with no value after it.

The core refuses a call before a cage starts when the input fails the action's input schema. These
calls fail it:

- `repo` is not in the form "The `repo` input" gives.
- `pr_view`'s `pr` begins with `-`, or a number is below 1.
- `pr_edit` has none of `title`, `body`, and `base`.
- `pr_create` has an empty `base`, `head`, or `title`, or `pr_list` has an empty `head`.

### Output past the cap

`pr_fetch` on a busy pull request is the call most likely to pass the core's cap. It is a script
action. `pr_checks` returns no log, only one line for each failed job. `docs/writing-a-plugin.md`
gives what happens to a script whose output passes the cap, and `docs/design.md` gives the cap and
the result forms under "Results". `run_view` with `log_failed` fetches one run's failed logs as a
command action, whose stdout past the cap goes to an overflow file.

### Optional inputs

- `pr_edit` passes each of `title`, `body`, and `base` that the input holds, an empty string
  included.
- `pr_view` asks gh for `number,title,url,headRefName,state,statusCheckRollup` when `fields` is
  absent or empty.
- `pr_reply` replies to the inline comment that `comment_id` names. Without `comment_id`, it
  comments on the pull request's conversation.
- `pr_close` passes `comment` only when it is not empty, and passes `--delete-branch` when
  `delete_branch` is true.

## In the cage

### `GH_TOKEN`

gh reads its token from `GH_TOKEN`, and `pr_checks` and `job_log_url` send it to GitHub's API
themselves. Each action declares `{"env": "GH_TOKEN"}` in its `credentials` in `manifest.json`. The
core does not compare a grant's credentials with that declaration. Give each grant a credential as
`GH_TOKEN`.

### `XDG_CACHE_HOME`

The image sets `XDG_CACHE_HOME=/tmp/cache`, on the cage's tmpfs. `gh run view --log-failed` writes
the run's log to its cache under `$XDG_CACHE_HOME/gh` before it prints it, and the cage's root
filesystem is read-only. Without the variable, gh tries `~/.cache` and fails. gh writes nothing else
under its home for these actions, so the image sets no other directory variable.

### git

The image holds git, which the `github-cli` package from apk depends on. `gh pr create --head` runs
git. It tolerates git's failure outside a repository, but it fails when git is missing, so the image
needs git.

## Egress

Each gh command that these actions run calls GitHub's API at `api.github.com`, and so does each log
URL request of `pr_checks` and `job_log_url`. The grant below names that host alone, because no
action calls `github.com`. The cage reaches the web only through its egress proxy, which refuses a
host the grant does not name.

`pr_checks` and `job_log_url` need only `api.github.com` for a job's log URL, because they read the
redirect and do not follow it. The agent fetches the URL itself, outside the cage.

`run_view` with `log_failed` needs one more host. gh asks `api.github.com` for the run's log at
`repos/<repo>/actions/runs/<id>/logs`, and follows GitHub's redirect to a storage host GitHub picks,
such as `productionresultssa<N>.blob.core.windows.net`. A grant that names only `api.github.com`
refuses that host, so `run_view` with `log_failed` fails under it. The core's domain pattern takes
no wildcard. For `run_view` with `log_failed` to work, a grant names each storage host, one by one.

## A grant

A config pins the plugin's image as `github`, and grants each action under `github.<action>`. In the
form `docs/design.md` gives under "Config":

```yaml
scopes:
  dev-agent:
    grants:
      github.pr_create:
        roots: []
        domains: [api.github.com]
        credentials: {github-token: {env: GH_TOKEN}}
        env: {GITHUB_REPOS: "example-org/*"}
      github.pr_checks:
        roots: []
        domains: [api.github.com]
        credentials: {github-token: {env: GH_TOKEN}}
        env: {GITHUB_REPOS: "example-org/*"}
      github.job_log_url:
        roots: []
        domains: [api.github.com]
        credentials: {github-token: {env: GH_TOKEN}}
        env: {GITHUB_REPOS: "example-org/*"}
```

Every grant sets `GITHUB_REPOS` in its `env`, as "The repository list" gives.

"Egress" gives the hosts a grant must add for `run_view` with `log_failed`.

## Layout

- `scripts/github_plugin/` is the module. `scripts.py` holds the script actions, `repos.py` holds
  the repository list, `cage.py` holds the `gh-cage` wrapper, and `entry.py` runs the wrapper or the
  script action that the executable's name names.
- `scripts/entry` is the one executable every action runs.
- `Dockerfile` builds the image on the Alpine base the git plugin pins, with gh and Python 3 from
  apk. It sets `XDG_CACHE_HOME`, and links `gh-cage` on the `PATH` and each script action's
  `executable` under `/plugin/bin/` to `/plugin/entry`. A command action runs `gh-cage`, which runs
  the image's gh.
- `describe` prints `manifest.json`.
- `tests/stub/gh` stands in for gh. It writes its argv to the file `GH_STUB_LOG` names, and answers
  from a table of canned results. It answers any other argv with that argv and the value of
  `GH_TOKEN`.
- `tests/unit/` runs the module against the stub gh, first on the host's `PATH`, and against a local
  plain-HTTP server that stands in for GitHub's API. It also checks the manifest. It needs no Docker
  and no gh.
- `tests/integration/` builds the plugin image and the core's verifier image, and runs actions in a
  real cage through the core's `CageRunner`. `tests/fake-api/` builds a fake `api.github.com`, which
  runs on an internal network. It answers a known job's log request with a redirect to a host the
  network cannot resolve, so a client that follows the redirect fails. It also answers gh's
  pull-request lookup, project-items query, and update. `test_actions.py` runs every action in an
  image that puts the stub gh ahead of the real one, built from `tests/stub/Dockerfile`. Its
  `pr_checks` test joins the fake's network and gets the fake's log URLs. `test_pinned_gh.py` runs
  the plugin image against the fake, and checks claims this plugin rests on. `pr_create` with `head`
  and no checkout gets past the steps that need the current branch and git, and fails at the fake's
  answer to its next request. `run_view` with `log_failed` writes its log cache under the image's
  `XDG_CACHE_HOME` while the root filesystem is read-only. `job_log_url` returns the fake's
  `Location` without following it, and the fake's 404 for a job the fake does not know. `pr_edit`
  gets past gh's pull-request lookup, which does not ask for the classic-Projects field GitHub
  refuses, and applies the edit.

## Build and test

Build the image from this directory, then check its manifest from `core/agent-proxy/`:

```sh
docker build -t agent-proxy-github .
cd ../../core/agent-proxy
uv run agent-proxy manifest check agent-proxy-github
```

Run the checks from this directory:

```sh
poe lint
poe test
poe test-integration
```

`poe test` runs the unit tests and needs neither Docker nor gh. `poe test-integration` runs on the
Docker daemon that `DOCKER_HOST` names. `.dev/verification.md` gives which bar runs each check, and
what the integration tests must leave behind.
