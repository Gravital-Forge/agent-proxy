# The hcp-terraform plugin

The hcp-terraform plugin reads HCP Terraform runs and their logs, for the workspaces a grant lists,
and does nothing else. Each of its actions is a script action. The core's architecture defines the
cage, the action, the grant, and the other terms this README uses, under
[Concepts](../../docs/architecture.md#concepts). [Writing a plugin](../../docs/writing-a-plugin.md)
gives the plugin model this follows.

## Why it only reads

On HCP Terraform's Free plan, an organization has no team but its owners and no custom workspace
permissions, so every token it mints is broad. An owner's user token can queue and apply runs, read
state and variables, and change workspaces, and HCP Terraform enforces nothing narrower. So the
plugin's tool surface is the whole limit. It is read-only by construction: every request it sends is
a GET, and no action reads state, state outputs, variables, or plan JSON.

## The actions

A config that pins the image as `hcp-terraform`, as [A grant](#a-grant) does, serves `run_list` as
the MCP tool `hcp-terraform_run_list`, by the rule that the core's reference gives under
[A scope's grants](../../docs/reference.md#a-scopes-grants). `manifest.json` declares every action,
its input schema, its credential, and its timeout of `2m`.

### `workspace_list`

It takes no input. It returns `workspaces`, a list of objects, each with `organization`, `name`,
`id`, and `current_run_id`. It reads every workspace of each organization the workspace list names,
100 to a page and page after page, and keeps those the list allows. It fails with exit code 1 when a
page's `next-page` is not a later page number than the page it read.

### `run_list`

It takes `organization` and `workspace`, both required, and `limit`, an integer from 1 to 100 whose
default is 20. It returns `runs`, the workspace's recent runs in the order HCP Terraform answers
them, newest first. Each run has `id`, `status`, `created_at`, `source`, `message`, `plan_only`, and
`has_changes`. It asks for every run operation, so speculative (plan-only) runs appear, as a pull
request's plan is one.

### `run_view`

It takes `run_id`, which matches `^run-[A-Za-z0-9]+$`. It returns `run`, an object with `id`,
`status`, `status_timestamps` (HCP Terraform's object as it answers it), `workspace` as
`organization/name`, `commit`, `plan`, and `apply`. `commit` holds `sha`, `branch`, and `url`, and
is null when the run has no configuration version or its ingress attributes answer 404. `plan` holds
`id`, `status`, `resource_additions`, `resource_changes`, and `resource_destructions`. `apply` holds
`id` and `status`. Each is null when the run has none.

### `plan_log` and `apply_log`

Each takes `run_id` and returns `log`, the plan's or the apply's log as text: decoded as UTF-8, with
each invalid byte replaced by U+FFFD, and with the framing characters `\x02` and `\x03` removed. HCP
Terraform does not document the log's format. The acceptance check "The workspace list with a real
token" in [the acceptance checks](../../docs/acceptance-checks.md) confirms it against a real run.
Each fails with exit code 1 when the run has no such log yet.

### What an action returns

Every result holds `exit_code`, `stdout`, and `stderr`, as each action's `output_schema` in
`manifest.json` states, and `stdout` is always empty. A success is exit code 0 with its data in the
member named above. In that data, a member that HCP Terraform's answer lacks is null, and
`workspace_list` leaves out a workspace whose answer holds no name. A refusal is exit code 2, as
[Refusals](#refusals) gives. A failure is exit code 1 with the reason on `stderr`.

A failed request gives `GET <path> answered <status>: <body>`, with the first 200 characters of the
answer's body, or `GET <path> failed: <error>` when no answer came, or when the answer's status came
but its body did not. `<path>` is the request's path below `https://app.terraform.io/api/v2`, or the
path of the `log-read-url` for a log. It never holds the token.

Other failures are an answer that is not a JSON object or holds no `data` list, a workspace that
answers no organization and name, and an ID that is not an HCP Terraform ID. Every ID the plugin
places in a URL path, from the input or from an answer, must match `^[a-z]+-[A-Za-z0-9]+$` first.
Each request times out after 30 seconds.

## The workspace list

The grant's `env` sets `HCP_TERRAFORM_WORKSPACES`: a comma-separated list of entries, each
`organization/name` or `organization/*`. The plugin strips the space around each entry and drops
empty entries. An organization or a name matches `^[A-Za-z0-9][A-Za-z0-9_-]*$`, and the input
schemas take the same form. Names compare exactly, case included. `workspaces.py` holds the list.

- Every action refuses when the variable is unset, names no entry, or holds an entry of another
  form.
- `run_list` refuses a workspace the list does not name, before it sends any request.
- `run_view`, `plan_log`, and `apply_log` read the run, then its workspace, and refuse the run when
  the list does not name that workspace. They send no further request.
- `workspace_list` returns only the workspaces the list allows.

## Refusals

The plugin refuses a call with exit code 2 and a `stderr` that begins
`hcp-terraform plugin refused: `, then the reason. The plugin refuses these:

- The workspace list is unset or empty:
  `HCP_TERRAFORM_WORKSPACES is not set or names no workspace; the grant's env sets it`
- An entry has another form:
  `HCP_TERRAFORM_WORKSPACES entry '<entry>' is not organization/name or organization/*`
- A workspace is not in the list:
  `workspace '<organization>/<name>' is not in HCP_TERRAFORM_WORKSPACES`
- `TFE_TOKEN` is unset, or empty once the plugin strips the whitespace around it:
  `TFE_TOKEN is not set`
- `TFE_TOKEN` holds a character other than visible ASCII, `!` through `~`:
  `TFE_TOKEN holds a character that is not visible ASCII`. The reason never quotes the token.
- A `log-read-url` is not `https` on `archivist.terraform.io`:
  `log-read-url '<url>' is not an https URL on archivist.terraform.io`
- The image's executable runs under a name that is no action's: `no action is named '<name>'`. This
  one is the executable's own exit code 2 with the reason on its `stderr`, not a result.

The core refuses a call before a cage starts when the input fails the action's input schema. These
calls fail it: a `run_id`, `organization`, or `workspace` in another form, a `limit` outside 1 to
100, or a member the schema does not name.

## Where the token goes

- The credential is `TFE_TOKEN`. Each action declares `{"env": "TFE_TOKEN"}` in `manifest.json`;
  give each grant the token as `TFE_TOKEN`.
- The plugin strips the whitespace around the token before it uses it, so a token stored with a
  trailing newline still works. It refuses a token that then holds any character other than visible
  ASCII, before it sends a request. A token holding a CR or an LF would make Python's HTTP library
  raise an error that quotes the token. No result holds the token.
- The token travels only to `app.terraform.io`, in the `Authorization` header of a GET.
  `https://app.terraform.io/api/v2` is a constant in `api.py`, never read from input or the
  environment.
- A request that carries the token follows no redirect: a 3xx answer is a failure, so the token
  never follows a redirect to another host.
- A log comes from the plan's or the apply's `log-read-url`, a signed URL on
  `archivist.terraform.io`. The log request carries no `Authorization` header, and may follow a
  redirect. The plugin sends it only when the URL's scheme is `https` and its host is exactly
  `archivist.terraform.io`, and refuses any other URL.
- When a log read fails, its `stderr` quotes the path of the signed `log-read-url`. The agent can
  read that log through `plan_log` or `apply_log` anyway, so the quoted path gives it no further
  reach.

## Egress

The plugin uses urllib's default proxy handler, so it reaches the web only through the cage's egress
proxy, which `HTTPS_PROXY` names. Its default TLS context reads `SSL_CERT_FILE`. The egress proxy
refuses a host the grant does not name.

`workspace_list`, `run_list`, and `run_view` reach only `app.terraform.io`. `plan_log` and
`apply_log` reach `app.terraform.io` and `archivist.terraform.io`. The egress proxy blocks a log
redirect to any other host, and the action then fails with exit code 1. [A grant](#a-grant) names
both hosts for every action, as `examples/instance.yaml` does. A grant of the first three may name
`app.terraform.io` alone.

## A grant

A config pins the plugin's image as `hcp-terraform`, and grants each action under
`hcp-terraform.<action>`. In the form the core's reference gives under
[The config file](../../docs/reference.md#the-config-file):

```yaml
scopes:
  dev-agent:
    grants:
      hcp-terraform.run_list:
        roots: []
        domains: [app.terraform.io, archivist.terraform.io]
        credentials: {hcp-terraform-token: {env: TFE_TOKEN}}
        env: {HCP_TERRAFORM_WORKSPACES: "example-org/example-workspace"}
      hcp-terraform.plan_log:
        roots: []
        domains: [app.terraform.io, archivist.terraform.io]
        credentials: {hcp-terraform-token: {env: TFE_TOKEN}}
        env: {HCP_TERRAFORM_WORKSPACES: "example-org/example-workspace"}
```

Every grant sets `HCP_TERRAFORM_WORKSPACES` in its `env`, as
[The workspace list](#the-workspace-list) gives. Every grant has `roots: []`, because no action
reads a file.

## Layout

- `scripts/hcp_terraform_plugin/` is the module. `errors.py` holds the refusal and the failure,
  `workspaces.py` the workspace list, `api.py` the HTTP layer and its checks of the token, the log
  URL, and each ID, `actions.py` the actions and `run_script`, which turns their errors into
  results, and `entry.py` runs the action the executable's name names.
- `scripts/entry` is the one executable every action runs.
- `Dockerfile` builds the image on the Alpine base the git plugin pins, with Python 3 from apk. The
  module uses Python's standard library alone: no HCP Terraform CLI, SDK, or third-party HTTP
  library. The image links each action's `executable` under `/plugin/bin/` to `/plugin/entry`.
- `describe` prints `manifest.json`.
- `tests/unit/` runs the module with its HTTP layer faked, and checks the manifest. It needs no
  Docker.
- `tests/fake-api/` builds a fake `app.terraform.io` and `archivist.terraform.io`, which runs on an
  internal network under both names, with a certificate for both from its own test CA. It serves the
  organization `o` with the workspaces `listed` and `other`, answers HEAD, POST, PUT, PATCH, and
  DELETE with 405, and logs each request's method, host, path, and `Authorization` header.
- `tests/integration/` builds the plugin image, the core's verifier image, and the fake, and runs
  each action in a real cage through the core's runner, a cage that trusts the fake's CA through
  `SSL_CERT_FILE`. It shows that every request is a GET, that the token reaches only the API host,
  that no log request carries an `Authorization` header, and that a run or a workspace in `o/other`
  is refused after at most its run and workspace reads.

## Build and test

Build the image from this directory, then check its manifest from `core/agent-proxy/`:

```sh
docker build -t agent-proxy-hcp-terraform .
cd ../../core/agent-proxy
uv run agent-proxy manifest check agent-proxy-hcp-terraform
```

Run the checks from this directory:

```sh
poe lint
poe test
poe test-integration
```

`poe test` runs the unit tests and needs no Docker. `poe test-integration` runs on the Docker daemon
that `DOCKER_HOST` names. [`.dev/verification.md`](../../.dev/verification.md) gives which bar runs
each check, and what the integration tests must leave behind.
