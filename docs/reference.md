# agent-proxy reference

This document holds what an operator or an agent's client looks up: the config file's fields, the
operator command, and the results and refusals of a call.

## The config file

One YAML file configures one instance. The instance reads it at start and at each `reload`, and
`token mint` reads it too. `examples/instance.yaml` is a full example.

The file is read with a safe YAML loader. Anchors and aliases work. A key repeated in one mapping is
refused, and so is a merge key (`<<`), because an explicit key overrides a merged one without a
trace. The top level is a mapping, and every mapping the format defines refuses a field it does not
name.

No field has a default except `provided_credentials`, a grant's `timeout`, `memory`, and `env`, and
a provided credential's `env`, `max_age`, and `timeout`. There are no scope-level defaults and no
layering between files. Each grant states its own fields, so a grant reads correctly on its own, and
repetition across grants and across instances is accepted.

### Top-level fields

- `agent_tree` is the agent's tree: an absolute path with no `..` segment. Every root and the
  overflow directory lie inside it.
- `overflow_dir` is the directory that takes a stream over the output cap, as
  [Output past the cap](#output-past-the-cap) gives. It is an absolute path with no `..` segment,
  inside `agent_tree`, and not equal to `agent_tree`.
- `max_cages` is the number of cages that may run at one time. It is an integer of 1 or more, given
  as a number. [The cage](architecture.md#the-cage) gives what a call over the cap does.
- `plugins` maps each plugin name to its image. A plugin name matches `^[a-z][a-z0-9-]*$`. The image
  is a reference pinned by digest, `<name>@sha256:<64 hex digits>`. The name `agent-proxy` is
  refused, so no tool name can take the self-check tool's name.
- `scopes` maps each scope name to a scope. A scope name matches `^[a-z0-9][a-z0-9-]{0,63}$`, the
  pattern that token and credential names also match. A scope has one field, `grants`.
- `provided_credentials` is optional, and defaults to an empty map. It maps each provided
  credential's name to its entry, as [Provided credentials](#provided-credentials) gives.

### A scope's grants

`grants` maps each grant key to a grant. A grant key is `<plugin>.<action>`: `<plugin>` is a name in
`plugins`, and `<action>` matches `^[a-z][a-z0-9_]*$` and is an action that the plugin's manifest
declares, other than a provider action. The agent sees the grant as the MCP tool
`<plugin>_<action>`.

A grant has these fields:

- `roots` is required. It is a list, possibly empty, of `{path, mode}`, where `mode` is `ro` or
  `rw`. Each path is absolute, has no `..` segment, and lies inside `agent_tree`. A root must not
  equal `overflow_dir`, contain it, or lie inside it. So no cage mounts the overflow directory, the
  sweep of old overflow files never reaches a root, and no root is the agent's tree itself.
- `domains` is required. It is a list, possibly empty, of the hosts the cage may reach. Each is a
  lower-case DNS name of two or more labels and at most 253 characters, with no wildcard. A grant
  names each domain once. A grant with `domains: []` reaches no host.
- `credentials` is required. It is a map, possibly empty, from a credential name to the way the cage
  receives that credential:
  - `{env: NAME}` sets the environment variable `NAME` to the credential's value. `NAME` follows the
    rule for an `env` name below, and is neither `HTTPS_PROXY` nor `AGENT_PROXY_ROOTS`.
  - `{file: PATH}` mounts the credential read-only as the file `PATH`, for a tool that only reads a
    file. `PATH` is an absolute path beneath `/run/credentials/` with no `..` segment.

  No two credentials of one grant share an environment name or a file path. The cage receives only
  its grant's credentials. A provided credential takes the `{env: NAME}` form only.
- `timeout` is optional. It overrides the action's default timeout. It is a duration: a positive
  count with no leading zero, then a unit of `s`, `m`, `h`, or `d`, such as `120s`.
- `memory` is optional. It sets the memory limit of every cage under the grant, which is 4 GiB
  without it. It is a string: a positive count with no leading zero, then `m` for MiB or `g` for
  GiB, such as `8g`. It is at least `64m`, the size of the cage's `/tmp`, whose pages count against
  the limit. [The cage](architecture.md#the-cage) gives the cage's limits.
- `env` is optional, and defaults to an empty map. It maps each name to a string that every cage
  under the grant receives as an environment variable. A name begins with an upper-case letter or
  `_`, and holds only upper-case letters, digits, and `_`. It is neither `HTTPS_PROXY`,
  `AGENT_PROXY_ROOTS`, nor a name that a credential of the same grant takes. A value holds no NUL.
  The values are settings, not secrets: they live in the config file, and the credential store holds
  none of them. Each plugin's README gives the settings it reads:
  [git](../plugins/git/README.md#a-grant), [github](../plugins/github/README.md#a-grant), and
  [hcp-terraform](../plugins/hcp-terraform/README.md#a-grant).

A grant, for example:

```yaml
scopes:
  dev-agent:
    grants:
      git.status:
        roots: [{path: /home/agent-01/Projects/example-repo, mode: rw}]
        domains: []
        credentials: {}
      github.pr_create:
        roots: []
        domains: [api.github.com]
        credentials: {github-token: {env: GH_TOKEN}}
        timeout: 120s
```

### Provided credentials

`provided_credentials` maps each provided credential's name to its entry. A name matches the
credential name pattern of [Top-level fields](#top-level-fields). A grant names a provided
credential in its `credentials` as it names a stored one, in the `{env: NAME}` form, and the
instance never reads the store for that name.
[Provided credentials](architecture.md#provided-credentials) gives how the instance fetches, caches,
and refreshes each value.

An entry has these fields:

- `provider` is required. It is the `<plugin>.<action>` of a provider action: `<plugin>` is a name
  in `plugins`, and `<action>` is an action that the plugin's manifest declares with the kind
  `provider`.
- `credentials` is required. It is a map, possibly empty, from a stored credential's name to the way
  the provider's cage receives it, in a grant's forms and under a grant's rules. It names no
  provided credential.
- `env` is optional, and defaults to an empty map. It holds the provider's settings, under a grant's
  rules.
- `domains` is required. It lists the hosts the provider's cage may reach, under a grant's rules.
- `refresh_before` is required. It is a duration, in the form of a grant's `timeout`. A call runs
  the provider again when the cached value expires within `refresh_before`.
- `max_age` is optional. It is a duration, in the same form, and is longer than `refresh_before`. A
  value expires at most `max_age` after the start of the run that fetched it. A provider that gives
  no `expires_at` needs it.
- `timeout` is optional. It overrides the provider action's default timeout, as a grant's does.

A `max_age` that is not longer than `refresh_before` is a failure whose line gives both durations in
Python's `timedelta` form, hours, minutes, and seconds, not as the file wrote them:

```text
config file '/etc/agent-proxy/config.yaml': provided_credentials.github-app-token.max_age must be longer than refresh_before 0:10:00, got 0:05:00
```

Each provider plugin's README gives the entry it needs, under "A provided credential".

### Failures and warnings

The instance also checks each plugin at start and at `reload`. It pulls each plugin image that its
Docker daemon lacks, by digest, and reads the image's manifest. These are failures: an image that
cannot be pulled, a manifest that fails its checks, a grant whose action the manifest does not
declare or declares as a provider action, and a provided credential whose provider the manifest does
not declare or declares as another kind. `token mint` loads no plugin.

A config that breaks any rule is refused whole, with one line per failure. Each line names the
config file, and a failure inside the file also names its location, such as
`scopes.dev-agent.grants.git.push.env`. A refused `reload` leaves the running config in force.

A config can also give warnings. A warning stops nothing: the instance starts, a `reload` takes
effect, and `token mint` mints.

- Each action that declares pass-through `args` gives a warning at start and at `reload`.
- Each stored credential that a grant names and the credential store lacks gives a warning at start,
  at `reload`, and at `token mint`. Its text is the text of the refusal that a call under that grant
  gets, as [Refusals](#refusals) gives. So the operator can start the instance and mint tokens
  before storing the credentials. A provided credential gives no such warning.
- Each stored credential that a provided credential's entry names and the store lacks gives the same
  warning, at the same times, after those of the grants.

The start also gives warnings of its own, which `serve` prints with the config's. None of them stops
the start:

- `the Docker daemon cannot enforce a cage's memory limit, so each cage runs without one`, when the
  daemon's info does not report that it enforces a memory limit.
- `the Docker daemon cannot enforce a cage's process limit, so each cage runs without one`, when the
  daemon's info does not report that it enforces a process limit.
- `the left cage '<name>' could not be removed: <Docker's error>`, for each cage that a killed
  instance left and the start cannot remove. [The instance](architecture.md#the-instance) gives the
  removal.

Before any warning, `serve` prints one report line on stderr for each left cage that the start
removes: `agent-proxy: removed the left cage '<name>'`. A report is not a warning, and it stops
nothing.

Two failures of Docker stop the start:

- `the Docker daemon's info could not be read, so the cage limits cannot be checked: <Docker's error>`,
  when the start cannot read the daemon's info.
- `the left cages could not be listed: <Docker's error>`, when the start cannot list the left cages.

## The operator command

The operator runs the `agent-proxy` command inside the instance container, through `docker exec`
against the service user's Docker daemon. The container is named `agent-proxy`:

```sh
docker exec agent-proxy agent-proxy token list
docker exec -i agent-proxy agent-proxy credential set github-token
```

`credential set` reads stdin, so it needs `docker exec -i`.

### Verbs

- `token mint --name NAME --scope SCOPE [--expires DURATION]` prints a new token once, on stdout.
  `NAME` and `SCOPE` match the scope-name pattern of [Top-level fields](#top-level-fields). It
  refuses a name that a stored token already holds. It reads the config file, refuses a config that
  breaks a rule of [The config file](#the-config-file), and refuses a scope that the config does not
  define, naming the defined scopes. `DURATION` has the form of a grant's `timeout`. A token minted
  without `--expires` never expires. Before the token, it prints a warning for each stored
  credential that the config names and the store lacks, as
  [Failures and warnings](#failures-and-warnings) gives, and it still mints the token.
- `token list` prints each token's name, scope, created time, last-used time, and expiry, sorted by
  name. It never prints a token.
- `token revoke NAME` deletes a token and prints `revoked NAME`. The next call with that token is
  refused. It refuses a name that no token holds, and names the stored tokens. To rotate a token,
  revoke it, then mint it again.
- `credential set NAME` reads the value from stdin, so the value never appears in a process's
  arguments or in shell history. It stores the value verbatim, replacing any stored value, and
  prints `stored NAME (SIZE bytes)`. It refuses an empty value.
  [Credentials](architecture.md#credentials) gives when the new value takes effect.
- `credential list` prints each credential's name and the time it was set, sorted by name. It never
  prints a value.
- `credential rm NAME` deletes a credential and prints `removed NAME`. It refuses a name that the
  store does not hold, and names the stored credentials.
- `audit tail [-n COUNT]` prints the newest audit lines in the order they were written: ten, or
  `COUNT`, a positive integer. A missing log prints nothing. The architecture gives what a line
  holds, under [Audit](architecture.md#audit).
- `manifest check IMAGE` runs the `describe` of a local image and checks the manifest it prints, by
  the rules of [Writing a plugin](writing-a-plugin.md#the-manifest). It never pulls, so an absent
  image is a refusal. It prints a warning for each action that declares pass-through `args`. On a
  pass it prints `manifest ok: IMAGE declares COUNT actions`.
- `reload` asks the running instance to load the config file again. When the instance accepts the
  config, the command prints each warning on stderr, then `reloaded` on stdout. When the instance
  refuses the config, the command prints each failure on stderr and exits 1, and the running config
  stays.
- `check [--scope NAME]` asks the running instance to run the self-check on one scope, or on every
  scope without `--scope`. It prints a header row, then one row for each entry of
  [The self-check report](#the-self-check-report), with five fields: the scope, the kind of check,
  the subject, `pass` or `fail`, and the detail. When an entry fails, it then prints
  `agent-proxy: FAILED of TOTAL checks failed` and exits 1. It refuses a scope that the config in
  force does not define, and names the defined scopes.
- `serve --host HOST --port PORT` is the instance image's process; the operator does not run it by
  hand. `PORT` is an integer from 1 to 65535. It prints the reports and the warnings of the start,
  which the container's log shows, then serves the MCP surface at `/mcp` on that address. On SIGINT
  or SIGTERM it stops and exits 0.

[Reload](architecture.md#reload) gives how `reload` and `check` reach the instance. Each waits at
most 600 seconds for the reply. A socket with nothing listening, no reply in time, and a reply of
another form are each a refusal that names the socket.

### Output and exit status

A listing prints a header row, then one row per entry, with the fields separated by tabs. A time is
ISO 8601 in UTC, to the second, and `-` marks a time that is not set.

- On success, the command prints its output on stdout and exits 0.
- On a refusal, it prints one line per failure on stderr, each beginning `agent-proxy:`, and exits
  1. Each refusal names the value it refused and what it expected.
- On a usage error, it prints the usage message on stderr and exits 2.
- A warning prints on stderr as a line beginning `agent-proxy: warning:`, and does not change the
  exit status.

### Environment

- `AGENT_PROXY_STATE_DIR` names the state directory. Every verb but `manifest check` reads it. The
  directory must exist, and the command does not change its mode.
- `AGENT_PROXY_CONFIG` names the config file. `token mint` and `serve` read it.
- `DOCKER_HOST` names the Docker daemon that `manifest check` and `serve` use. When it is unset, the
  command uses Docker's default socket.

An unset `AGENT_PROXY_STATE_DIR` or `AGENT_PROXY_CONFIG`, or a state directory that does not exist,
is a refusal that names the variable. A daemon that the command cannot reach is a refusal that names
`DOCKER_HOST` and its value, or says that it is unset. Inside the instance container all three are
set, as the architecture gives under [The instance](architecture.md#the-instance).

## Results and refusals

A call's result reaches the MCP client as a normal tool result: one compact JSON text block, and the
same object as structured content. A refusal is the only error result.

### A command action's result

A command action returns `{exit_code, stdout, stderr}`. Each stream is text, decoded as UTF-8 with
each bad byte replaced. A non-zero exit code is a normal result.

### A script action's result

A script succeeds when it exits 0 and prints on stdout one JSON object that its `output_schema`
validates. The result is then that object. Otherwise the script has failed, and the result is a
command action's result. [Writing a plugin](writing-a-plugin.md#how-an-action-runs) gives what a
script receives.

### Output past the cap

The output cap is 256 KiB (262,144 bytes) for each stream. A stream over the cap goes to a new file
in `overflow_dir`, named for the call: `<call-id>.stdout` or `<call-id>.stderr`, where the call ID
is 32 lower-case hex digits. The instance reads each stream back from the cage's log, and that log
has a size cap of its own. So the file holds the stream as the log kept it, and a call whose output
passes the log's cap loses the start of it, as [Known limits](architecture.md#known-limits) gives.
The result holds the file's path and the stream's size in place of the stream, and
`truncated: true`. It holds no part of the stream, so the agent decides how to read the file. For
example:

```json
{"exit_code": 0, "stdout_file": "/home/agent-01/Projects/.agent-proxy-overflow/0123456789abcdef0123456789abcdef.stdout", "stdout_bytes": 4194304, "stderr": "", "truncated": true}
```

A script's stdout over the cap fails the script, so its result is the command form above. A provider
run writes no overflow file, as [Provided credentials](architecture.md#provided-credentials) gives.

The instance writes each file with mode `0660`. No cage mounts the overflow directory. The instance
removes each file there that is more than 7 days old, at start and then once a day.

### The self-check report

Every token that verifies lists the tool `agent-proxy_self_check`, whatever its scope grants. The
tool takes no input: its input schema is an object that allows no property. It checks the scope of
the calling token, and returns the report as its result. The architecture gives what each check
covers, under [The self-check](architecture.md#the-self-check).

The report is one object:

- `ok` is true when every entry passed.
- `scope` names the scope checked.
- `checks` lists the entries. Each entry holds `check`, the kind of check; `subject`, what it
  checked; `ok`, whether it passed; and `detail`, one line that says what it found.

The kinds come in this order, and within a kind each subject comes once, in the order the scope's
grants first name it:

- `credential`, whose subject is a credential's name. The entry never holds the value. The detail of
  a stored credential that the store lacks is the text of its refusal. A provided credential's
  detail begins `provided by '<plugin>.<action>'`. It passes when every stored credential its entry
  names is stored, and its detail ends `every stored credential it takes is stored`. Otherwise it
  fails, and its detail follows with the refusal text of each one that is not stored, separated by
  `; `.
- `root`, whose subject is a root's path.
- `overflow_dir`, whose subject is the overflow directory.
- `egress`, whose subject is an egress proxy's container name. The detail names the domains it
  allows. The lists come from the grants, then from the provided credentials the grants name. A list
  of no domains has no proxy, so it has no entry.
- `plugin`, whose subject is a plugin's name.

A failed check is an entry whose `ok` is false, in a normal result. It is not a refusal.

### Refusals

The proxy refuses a call it will not run, or whose result it cannot give. An MCP client receives a
refusal as an error result whose text is `agent-proxy refused:`, a space, and the refusal:

```text
agent-proxy refused: credential 'github-token' is not set; the operator stores it with 'agent-proxy credential set github-token'
```

The proxy refuses a call when:

- the bearer token is missing, unknown, revoked, or expired;
- the token store cannot be read;
- the token's scope is not defined in the config in force;
- the scope does not grant the tool;
- a call of the self-check tool carries an argument;
- the input fails the action's input schema, with one line per violation, each located by its JSON
  path;
- a value that a parameter places in argv begins with `-`;
- a root is missing, is not a directory, has a symbolic link in its path, or was replaced before the
  cage started;
- a stored credential that the grant names is not in the store, as in the example above;
- a credential that the grant gives as an environment variable is not valid UTF-8, or holds a NUL;
- a provided credential that the grant names has no unexpired value, because its provider's run
  failed, as [A provider's failure](#a-providers-failure) gives;
- the audit log cannot record the run of a provider that the call started;
- the cage cannot start, or the action's executable cannot run in it;
- the action does not finish within its timeout;
- the cage fails after it starts;
- an overflow file cannot be created or written;
- the audit log cannot record the call, and the result is withheld.

When the audit log cannot record an action's call whose cage started, the refusal also says that the
action ran, because the action may have changed something before the cage ended:

```text
agent-proxy refused: the action ran and may have taken effect, but the audit log could not record call <call-id>, so its result is withheld; check what it changed before a retry
```

[Audit](architecture.md#audit) gives which calls get this refusal.

The detail of any other error is masked.

### A provider's failure

When a provider's run fails and no unexpired value is cached, each call that needs the value is
refused before its cage starts. The refusal names the provided credential, the provider, and the
run's failure:

```text
agent-proxy refused: provided credential 'github-app-token' has no unexpired value: provider 'github-app.installation_token' failed: it exited 1, stderr 'github-app plugin failed: POST /app/installations/00000000/access_tokens answered 401: A JSON web token could not be decoded\n'
```

The provider's audit line holds the same failure as its `refusal`, as [Audit](architecture.md#audit)
gives. A run fails when:

- the runner refuses or fails its cage, for any reason a call's cage can be refused or fail in the
  list above, such as a stored credential the entry names that is not stored, or a run past its
  timeout. The failure is that refusal's text.
- it exits non-zero: `it exited <code>, stderr <stderr>`, where `<stderr>` is the first 1,024 bytes
  of its stderr, decoded as UTF-8 with each bad byte replaced, and quoted as a Python string
  literal.
- its stdout passes the output cap: `its stdout passed the output cap of 262144 bytes`.
- its stdout is not UTF-8 JSON, is not a JSON object, or holds a key other than `value` and
  `expires_at`.
- its `value` is not a non-empty string, or cannot be an environment value because it holds a NUL or
  does not encode as UTF-8.
- its `expires_at` is not a string, is not an ISO 8601 date and time, or carries no time zone.
- it gives no `expires_at` and the entry sets no `max_age`.
- the value it gives has already expired: `the value it gave expired at <expiry>`, in ISO 8601.

No failure that its stdout causes quotes a value from it. The failure for an extra key names the
keys. [A provider action](writing-a-plugin.md#a-provider-action) gives what a run must print.
