# The github plugin

The github plugin runs gh, the GitHub CLI, in a cage. It offers actions on pull requests, workflow
runs, and job logs. The core's architecture defines the cage, the action, the grant, and the other
terms this README uses, under [Concepts](../../docs/architecture.md#concepts).
[Writing a plugin](../../docs/writing-a-plugin.md) gives the plugin model this follows.

## The actions

A config that pins the image as `github`, as [A grant](#a-grant) does, serves `pr_create` as the MCP
tool `github_pr_create`, by the rule that the core's reference gives under
[A scope's grants](../../docs/reference.md#a-scopes-grants). A config that pins it as `gh` instead
serves it as `gh_pr_create`. `manifest.json` declares every action, its input schema, its
credentials, and its timeout.

An action is a command action when its argv is `gh-cage`, a fixed gh prefix, and typed parameters,
as for `pr_list`, `run_view`, and `run_list`. [The repository list](#the-repository-list) gives what
`gh-cage` does. The others are script actions, because each does more than that:

- It runs more than one gh command, as `pr_fetch` does.
- It reads a job's log URL from GitHub's API itself, as `pr_checks` and `job_log_url` do.
  [Job log URLs](#job-log-urls) gives why gh cannot.
- It applies a default that a command action cannot state, as `pr_view` does with its field list.
- It takes free text, such as a title, a body, or a comment, that may begin with `-`. The core
  refuses such a value for a command action, as
  [How argv is formed](../../docs/writing-a-plugin.md#how-argv-is-formed) gives, and a Markdown body
  that opens with a list item begins with `-`. `pr_create`, `pr_edit`, `pr_reply`, and `pr_close`
  pass each text value as one argv item, `--flag=value` or gh api's `-f body=<text>`, so gh never
  reads it as an option.

A value that a script places alone in argv cannot begin with `-` either: `pr_view`'s `pr` must not,
and each number is an integer from 1 up.

No action takes pass-through `args`. Its items would be exempt from the core's refusal of a value
that begins with `-`.

### The `repo` input

Every action takes a required `repo`, the GitHub repository as `owner/name`, and passes it to gh as
`--repo`. gh needs no checkout for that. So a grant of the plugin has `roots: []`, because a cage
with no roots reaches less than one that mounts the agent's tree. The one exception is a grant of
`pr_create` or `pr_edit` that allows `attach`: it mounts a root that holds the files to upload, as
[the grant for attachments](#the-grant) gives.

The input schema takes an owner of letters, digits, and `-` that begins with a letter or a digit,
then one slash, then a name of letters, digits, `_`, `.`, and `-` that is not only dots. A script
builds each `gh api` path from `repo`, so the path stays under `repos/<owner>/<name>/`.

`pr_create` takes a required `head`, the branch the pull request comes from. gh cannot read that
branch from a checkout, because it runs outside any checkout: neither the core nor the image sets a
working directory.

### The repository list

Every action checks its `repo` against `GITHUB_REPOS` in the cage's environment before gh runs. The
grant's `env` sets the variable. Its value is a comma-separated list: each entry is stripped of
whitespace, and an empty entry is ignored. An entry takes one of two forms:

- `owner/name` names one repository.
- `owner/*` names every repository of one user or organization.

An owner and a name take the forms that [The `repo` input](#the-repo-input) gives. Any other entry
is malformed: a bare `owner`, `*`, `*/name`, or a partial glob such as `owner/api-*`.

A `repo` matches an entry when the owners are equal and the names are equal, or the entry's name is
`*`. Each comparison ignores case, as GitHub's does.

The list fails closed. In every cage, whether or not it holds `GH_TOKEN`, an unset list, a list with
no entry, and a list with a malformed entry each refuse the call, and so does a `repo` that no entry
matches. `repos.py` holds the rule.

The git plugin applies the same rule with its own copy, and differs in two points: it refuses a bad
list only in a cage that holds `GH_TOKEN`, and it judges a repository by the URL git connects to,
not by a `repo` input. Its README gives its use under
[The repository list](../git/README.md#the-repository-list).

A script action checks `repo` before the action runs. A command action runs `gh-cage`, a wrapper
that finds the one `--repo` in its argv and checks the value after it. On a match, `gh-cage`
replaces itself with gh on the same argv, `--repo` kept.

`pr_create` checks `repo` alone, never `head`. `head` may name a branch on another owner's fork,
which the pull request only reads.

No action runs git inside a repository, so nothing inside the plugin gets past the check. A root
that `attach` needs supplies only the files that gh uploads.

### What an action returns

A command action returns the core's result, as
[A command action's result](../../docs/reference.md#a-command-actions-result) gives. A script action
prints the same three members, `exit_code`, `stdout`, and `stderr`, and its `output_schema` states
them. The script exits 0 whenever it runs to its end, and gh's exit code goes in `exit_code`.
`job_log_url` runs no gh, so its exit code is its own, as given below. Each action returns gh's
stdout as gh prints it, except for `pr_fetch`, `pr_checks`, and `job_log_url`, and for `pr_create`
and `pr_edit` with `attach`, where the script replaces each private path, as
[Attachments](#attachments) gives.

`pr_create` and `pr_edit` add two members when the last non-empty line of gh's stdout is a pull
request's URL, `https://github.com/<owner>/<name>/pull/<number>`: `pr_url`, that URL, and
`pr_number`, the number as an integer. gh prints that URL on success, and also after an upload fails
once the pull request exists. The rule holds with or without `attach`, and whatever the exit code,
so a call that fails after gh opened or changed the pull request still names it. Both actions'
`output_schema` states the two members.

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
[Egress](#egress) gives.

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
- A call passes `attach`, and the setting, the roots, or an entry fails a check.
  [What `attach` refuses](#what-attach-refuses) gives each reason and the order of the checks.

The core refuses a call before a cage starts when the input fails the action's input schema. These
calls fail it:

- `repo` is not in the form [The `repo` input](#the-repo-input) gives.
- `pr_view`'s `pr` begins with `-`, or a number is below 1.
- `pr_edit` has none of `title`, `body`, `base`, and `attach`.
- `pr_create` has an empty `base`, `head`, or `title`, or `pr_list` has an empty `head`.
- `attach` is not in the form [The `attach` input](#the-attach-input) gives: it is empty or holds
  more than 50 entries, or an entry lacks `path`, has a `path` that does not begin with `/`, has a
  `#` in `path` or in `alt`, or has a member other than these two.

### Output past the cap

`pr_fetch` on a busy pull request is the call most likely to pass the core's cap. It is a script
action. `pr_checks` returns no log, only one line for each failed job. `run_view` with `log_failed`
fetches one run's failed logs as a command action. The core's reference gives the cap, and what
becomes of a command's or a script's output that passes it, under
[Output past the cap](../../docs/reference.md#output-past-the-cap).

### Optional inputs

- `pr_edit` passes each of `title`, `body`, and `base` that the input holds, an empty string
  included.
- `pr_view` asks gh for `number,title,url,headRefName,state,statusCheckRollup` when `fields` is
  absent or empty.
- `pr_reply` replies to the inline comment that `comment_id` names. Without `comment_id`, it
  comments on the pull request's conversation.
- `pr_close` passes `comment` only when it is not empty, and passes `--delete-branch` when
  `delete_branch` is true.
- `pr_create` and `pr_edit` pass each `attach` entry as one `--attach=` item, after every other
  flag, as [Attachments](#attachments) gives. A call without `attach` passes no `--attach=` item.

## Attachments

`pr_create` and `pr_edit` take an optional `attach`, a list of local image and video files. gh's
`--attach` uploads each file to GitHub, and rewrites the body's references to it as the uploaded
asset's URL. The script checks every entry against the grant before gh runs, and passes each entry
as one argv item after every other flag: `--attach=<private path>`, or
`--attach=<private path>#<alt>` when the entry has `alt`. `attachments.py` holds the checks. Both
actions' timeout is `10m`, because a video of up to 100 MiB uploads through the egress proxy within
the call.

The script holds each checked file open until it exits. A private path lies on the cage's tmpfs,
which the agent cannot reach, and leads to the open file. So a later change to what the agent's name
leads to does not change what uploads. A private path cleans lexically to the agent's path, so gh's
body matching and upload name work as [Body references](#body-references) gives. The script replaces
each private path with the entry's `path` in `stdout` and `stderr`, so a result names only the
agent's paths.

### The `attach` input

`attach` is an array of 1 to 50 objects; 50 is gh's own limit. Each object has these members, and no
other:

- `path`, required: the file's absolute path, as the agent sees it on the host. It begins with `/`
  and holds no `#`. The grant's root mounts the file at the same path in the cage.
- `alt`, optional: alt text for an image that the body does not reference. It holds no `#`. A video
  takes none.

gh splits `<path>#<alt>` by scanning for the longest prefix that exists as a file, so a `#` in
either member could let gh pick a file the script never checked. The input schema refuses it for
that reason.

`pr_edit` accepts a call whose only change is `attach`. gh then appends each file to the body that
the pull request already has.

### The setting

The grant's `env` member `GITHUB_PR_ATTACHMENTS` governs `attach`:

- Unset, or `on`: a call may pass `attach`.
- `off`: a call that passes `attach` is refused.
- Any other value: a call that passes `attach` is refused, and the reason names the value.

A call without `attach` runs under every value.

### What `attach` refuses

The script refuses a call before gh runs, as [Refusals](#refusals) gives, with one of these reasons.
`<path>` is the entry's `path` as given:

- `attach is off: GITHUB_PR_ATTACHMENTS is 'off'`
- `GITHUB_PR_ATTACHMENTS must be 'on' or 'off', got '<value>'`
- `attach needs the grant's roots: AGENT_PROXY_ROOTS is not set`
- `AGENT_PROXY_ROOTS must be a JSON array of absolute paths, got '<value>'`
- `attach path '<path>' resolves outside the grant's roots`
- `attach path '<path>' is not a regular file`
- `attach path '<path>' is not a png, jpg, jpeg, gif, webp, mp4, mov, or webm file`
- `attach path '<path>' is a video, and a video takes no alt`

The checks run in that order: the setting, the roots, then each entry in order. The first failure is
the refusal, with one exception. The refusal of two entries that need one name to be both a file and
a directory comes after every entry has passed its own checks, and its reason names the later of the
two entries, the one that met the conflict.

The script resolves each path with symlinks followed. The real path must lie below the real path of
one of the roots that the core passes in `AGENT_PROXY_ROOTS`, so a symlink that leads out of every
root is refused. The script then opens the path and judges the file it opened: that file's own path
must lie below a root, and the file must be regular. A path the kernel will not open is refused as
not a regular file, and so is an entry whose normalized path needs one name to be both a file and a
directory, given another entry. The extension check applies to the path as given and to the opened
file's path, compared lowercase, because gh picks the content type from the name it is given. SVG is
not accepted. The type list keeps a source file or an env file from reaching GitHub by mistake.

### Body references

gh matches a body reference to an attachment by absolute path. It parses the body as Markdown, and
compares each image or link destination, made absolute, with each attached `path`, made absolute. It
never compares base names. So:

- Reference each file by its exact `path` as the destination: `![<alt>](<path>)` for an image, and
  `![](<path>)` for a video, alone in its paragraph, with a blank line on each side.
- gh replaces an image reference with `![<alt>](<asset URL>)`, and keeps the alt text written in the
  body. It replaces a video reference that stands alone in its paragraph with the bare asset URL,
  which GitHub renders as a player. A video reference anywhere else becomes a link.
- gh appends each attached file that the body does not reference to the end of the body, in its own
  paragraph. An image becomes `![<alt>](<asset URL>)`, with the file name as the alt when the entry
  has no `alt`. A video becomes the bare URL.
- A reference to a file that did not upload stays in the body as the local path.
- Keep paths free of spaces and of Markdown punctuation. gh also tries a destination's unescaped
  forms, but a plain path needs none.

### What an upload returns

gh uploads the files in order, and stops at the first failure. Its failure exit is 1, and its errors
go to `stderr`. [What an action returns](#what-an-action-returns) gives `pr_url` and `pr_number`. A
call that passes `attach` ends in one of three ways:

- **All uploaded.** `exit_code` is 0, and the result has `pr_url` and `pr_number`. `pr_create`
  opened the pull request, and `pr_edit` changed it.
- **Partial.** At least one file uploaded, and then one failed. `exit_code` is 1, and the result has
  `pr_url` and `pr_number`. `pr_create` still opened the pull request, and `pr_edit` still changed
  it, with the uploaded files' references rewritten. The failed file and every file after it did not
  upload, so their references stay local paths. `stderr` names the failed file:
  `could not upload <path>`, then GitHub's reason, such as `: rate limited; wait and try again` or
  `: attaching files requires write access to the repository`.
- **None uploaded.** `exit_code` is 1, and the result has no `pr_url`. `pr_create` opened nothing,
  and `stderr` ends `no pull request was created`. `pr_edit` changed nothing, and `stderr` ends
  `the pull request was not changed`.

The caller's rules:

- A result with `pr_number` means the pull request exists. Never call `pr_create` again for it.
- To re-attach after a partial failure, read the body with `pr_view`, with `fields` set to `body`.
  The files still missing are the attached `path`s that still appear in it. Call `pr_edit` with that
  body as `body`, and with `attach` naming only the missing files. gh rewrites the remaining local
  references in place, and leaves the uploaded URLs as they are.
- After "none uploaded" on `pr_create`, the caller may call `pr_create` again.

### The grant

A grant of `pr_create` or `pr_edit` that allows `attach` needs these members. [A grant](#a-grant)
shows one.

- `roots`: a root that holds every attached file. Mode `ro` suffices. A grant with `roots: []`
  refuses every `attach` with the outside-the-roots reason, because the core then passes `[]`.
- `domains`: `api.github.com` and `uploads.github.com`, as [Egress](#egress) gives.
- `credentials`: the GitHub token as `GH_TOKEN`, with write access to the repository. gh uploads
  only with a classic or fine-grained personal access token, an OAuth token, or a user-to-server
  token. It checks the viewer's permission on the repository first, and refuses below `WRITE`.
- `env`: `GITHUB_REPOS`, as for every grant of the plugin, and `GITHUB_PR_ATTACHMENTS`, as
  [The setting](#the-setting) gives.

A grant that turns attachments off sets `GITHUB_PR_ATTACHMENTS: "off"`, and may drop the root and
`uploads.github.com`. The grant then loses the reach along with the input.

### Size and type limits

gh checks these before it uploads, and its error is the result: an image of at most 10 MiB, a video
of at most 100 MiB, a non-empty regular file, and no file attached twice. `maxImageBytes` and
`maxVideoBytes` in gh's `internal/attachments/` at `v2.102.0` set the two sizes. GitHub's own limits
follow the organization's plan.

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

The image installs git from apk. `gh pr create --head` runs git. It tolerates git's failure outside
a repository, but it fails when git is missing, so the image needs git.

## Egress

Each gh command that these actions run calls GitHub's API at `api.github.com`, and so does each log
URL request of `pr_checks` and `job_log_url`. No action calls `github.com`. The cage reaches the web
only through its egress proxy, which refuses a host the grant does not name.

`pr_checks` and `job_log_url` need only `api.github.com` for a job's log URL, because they read the
redirect and do not follow it. The agent fetches the URL itself, outside the cage.

`attach` needs one more host. gh sends each file in one
`POST https://uploads.github.com/user-attachments/assets`, with the token in its `Authorization`
header, and calls no storage host. A grant of `pr_create` or `pr_edit` that allows `attach` names
`uploads.github.com` beside `api.github.com`, as [A grant](#a-grant) shows. When an upload reaches a
host the grant does not name, the egress proxy's refusal names that host, and the fix is a grant
edit.

`run_view` with `log_failed` needs one more host. gh asks `api.github.com` for the run's log at
`repos/<repo>/actions/runs/<id>/logs`, and follows GitHub's redirect to a storage host GitHub picks,
such as `productionresultssa<N>.blob.core.windows.net`. A grant that names only `api.github.com`
refuses that host, so `run_view` with `log_failed` fails under it. The core's domain pattern takes
no wildcard. For `run_view` with `log_failed` to work, a grant names each storage host, one by one.

## A grant

A config pins the plugin's image as `github`, and grants each action under `github.<action>`. In the
form the core's reference gives under [The config file](../../docs/reference.md#the-config-file):

```yaml
scopes:
  dev-agent:
    grants:
      github.pr_create:
        roots: [{path: /home/agent-01/Projects/example-repo, mode: ro}]
        domains: [api.github.com, uploads.github.com]
        credentials: {github-token: {env: GH_TOKEN}}
        env: {GITHUB_REPOS: "example-org/*", GITHUB_PR_ATTACHMENTS: "on"}
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

Every grant sets `GITHUB_REPOS` in its `env`, as [The repository list](#the-repository-list) gives.

The `pr_create` grant allows `attach`: its root holds the files to upload, and it names
`uploads.github.com`. [The grant for attachments](#the-grant) gives what each member is for.

[Egress](#egress) gives the hosts a grant must add for `run_view` with `log_failed`.

## Layout

- `scripts/github_plugin/` is the module. `scripts.py` holds the script actions, `attachments.py`
  holds the checks of `attach` and builds its private paths, `repos.py` holds the repository list,
  `cage.py` holds the `gh-cage` wrapper, and `entry.py` runs the wrapper or the script action that
  the executable's name names.
- `scripts/entry` is the one executable every action runs.
- `Dockerfile` builds the image on the Alpine base the git plugin pins, with git and Python 3 from
  apk. It installs gh from gh's release tarball, which it checks against a pinned SHA-256: the
  `GH_VERSION` and `GH_SHA256` build arguments pin the version and the hash, and a tarball whose
  hash differs fails the build. gh does not come from apk, because Alpine's `github-cli` package
  lacks `--attach`. The image sets `XDG_CACHE_HOME`, and links `gh-cage` on the `PATH` and each
  script action's `executable` under `/plugin/bin/` to `/plugin/entry`. A command action runs
  `gh-cage`, which runs the image's gh.
- `describe` prints `manifest.json`.
- `tests/stub/gh` stands in for gh. It writes its argv to the file `GH_STUB_LOG` names, and answers
  from a table of canned results. It answers an `--attach=` item whose file is named
  `upload-fails.png` with gh's result for a partial upload failure. It answers any other argv with
  that argv and the value of `GH_TOKEN`.
- `tests/unit/` runs the module against the stub gh, first on the host's `PATH`, and against a local
  plain-HTTP server that stands in for GitHub's API. It also checks the manifest. It needs no Docker
  and no gh.
- `tests/integration/` builds the plugin image and the core's verifier image, and runs actions in a
  real cage through the core's `CageRunner`. `harness.py` holds what the tests share with their
  fixtures, `GITHUB_TOKEN` among them: the value every cage gets as `GH_TOKEN`, in a classic token's
  form, because gh uploads only with a token whose type it can tell from its prefix.
  `tests/fake-api/` builds a fake `api.github.com` and `uploads.github.com`, which runs on an
  internal network under both names, with a certificate for both. It answers a known job's log
  request with a redirect to a host the network cannot resolve, so a client that follows the
  redirect fails. It also answers gh's pull-request lookup, project-items query, and update, and
  each upload, which it refuses for a file named `upload-fails.png`. It logs each upload, each
  upload's bytes, and each update's body as JSON lines, which a test reads back. It holds its answer
  to a lookup of pull request 2 until the test releases it. `test_actions.py` runs every action in
  an image whose gh is the stub, in place of the real one, built from `tests/stub/Dockerfile`. Its
  `pr_checks` test joins the fake's network and gets the fake's log URLs. `test_actions.py` also
  shows `attach` refused in a real cage, with the setting `off` and with a path outside the roots.
  `test_pinned_gh.py` runs the plugin image against the fake, and checks claims this plugin rests
  on. `pr_create` with `head` and no checkout gets past the steps that need the current branch and
  git, and fails at the fake's answer to its next request. `run_view` with `log_failed` writes its
  log cache under the image's `XDG_CACHE_HOME` while the root filesystem is read-only. `job_log_url`
  returns the fake's `Location` without following it, and the fake's 404 for a job the fake does not
  know. `pr_edit` gets past gh's pull-request lookup, which does not ask for the classic-Projects
  field GitHub refuses, and applies the edit. `pr_edit` with `attach` uploads each file to
  `uploads.github.com` with the token, and rewrites the body's references in place, and a body that
  nests images in a numbered list inside a `<details>` block keeps its structure and indents. When
  an upload fails, gh uploads no later file, still applies the edit, and the result holds `pr_url`
  and `pr_number`. A file swapped for a symlink to `/proc/self/environ` during gh's lookup still
  uploads the bytes the script checked.

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
Docker daemon that `DOCKER_HOST` names. The contributing guide gives under
[The checks](../../CONTRIBUTING.md#the-checks) what each check needs, and what the integration tests
must leave behind.
