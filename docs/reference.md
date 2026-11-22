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

No field has a default except a grant's `timeout` and `env`. There are no scope-level defaults and
no layering between files. Each grant states its own fields, so a grant reads correctly on its own,
and repetition across grants and across instances is accepted.

### Top-level fields

- `agent_tree` is the agent's tree: an absolute path with no `..` segment. Every root and the
  overflow directory lie inside it.
- `overflow_dir` is the directory that takes a stream over the output cap, as
  [Output past the cap](#output-past-the-cap) gives. It is an absolute path with no `..` segment,
  inside `agent_tree`, and not equal to `agent_tree`.
- `max_cages` is the number of cages that may run at one time. It is an integer of 1 or more, given
  as a number. A call over the cap waits for a cage to end.
- `plugins` maps each plugin name to its image. A plugin name matches `^[a-z][a-z0-9-]*$`. The image
  is a reference pinned by digest, `<name>@sha256:<64 hex digits>`. The name `agent-proxy` is
  refused, so no tool name can take the self-check tool's name.
- `scopes` maps each scope name to a scope. A scope name matches `^[a-z0-9][a-z0-9-]{0,63}$`, the
  pattern that token and credential names also match. A scope has one field, `grants`.

### A scope's grants

`grants` maps each grant key to a grant. A grant key is `<plugin>.<action>`: `<plugin>` is a name in
`plugins`, and `<action>` matches `^[a-z][a-z0-9_]*$` and is an action that the plugin's manifest
declares. The agent sees the grant as the MCP tool `<plugin>_<action>`.

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
  its grant's credentials.
- `timeout` is optional. It overrides the action's default timeout. It is a duration: a positive
  count with no leading zero, then a unit of `s`, `m`, `h`, or `d`, such as `120s`.
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

### Failures and warnings

The instance also checks each plugin at start and at `reload`. It pulls each plugin image that its
Docker daemon lacks, by digest, and reads the image's manifest. An image that cannot be pulled, a
manifest that fails its checks, and a grant whose action the manifest does not declare are failures.
`token mint` loads no plugin.

A config that breaks any rule is refused whole, with one line per failure. Each line names the
config file, and a failure inside the file also names its location, such as
`scopes.dev-agent.grants.git.push.env`. A refused `reload` leaves the running config in force.

A config can also give warnings. A warning stops nothing: the instance starts, a `reload` takes
effect, and `token mint` mints.

- Each action that declares pass-through `args` gives a warning at start and at `reload`.
- Each credential that a grant names and the credential store lacks gives a warning at start, at
  `reload`, and at `token mint`. Its text is the text of the refusal that a call under that grant
  gets, as [Refusals](#refusals) gives. So the operator can start the instance and mint tokens
  before storing the credentials.

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
  without `--expires` never expires. Before the token, it prints a warning for each credential that
  the config names and the store lacks, and it still mints the token.
- `token list` prints each token's name, scope, created time, last-used time, and expiry, sorted by
  name. It never prints a token.
- `token revoke NAME` deletes a token and prints `revoked NAME`. The next call with that token is
  refused. It refuses a name that no token holds, and names the stored tokens. To rotate a token,
  revoke it, then mint it again.
- `credential set NAME` reads the value from stdin, so the value never appears in a process's
  arguments or in shell history. It stores the value verbatim, replacing any stored value, and
  prints `stored NAME (SIZE bytes)`. It refuses an empty value. The next call uses the new value,
  with no reload.
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
- `reload` asks the running instance to load the config file again. It prints each warning, then
  `reloaded` once the new config is in force. When the instance refuses the config, it prints each
  failure, and the running config stays.
- `check [--scope NAME]` asks the running instance to run the self-check on one scope, or on every
  scope without `--scope`. It prints a header row, then one row for each entry of
  [The self-check report](#the-self-check-report), with five fields: the scope, the kind of check,
  the subject, `pass` or `fail`, and the detail. When an entry fails, it then prints
  `agent-proxy: FAILED of TOTAL checks failed` and exits 1. It refuses a scope that the config in
  force does not define, and names the defined scopes.
- `serve --host HOST --port PORT` is the instance image's process; the operator does not run it by
  hand. `PORT` is an integer from 1 to 65535. It prints the warnings of the start, which the
  container's log shows, then serves the MCP surface at `/mcp` on that address. On SIGINT or SIGTERM
  it stops and exits 0.

`reload` and `check` reach the instance through its control socket, `control.sock` in the state
directory. Each waits at most 600 seconds for the reply. A socket with nothing listening, no reply
in time, and a reply of another form are each a refusal that names the socket.

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

The output cap is 256 KiB (262,144 bytes) for each stream. A stream over the cap goes, in full, to a
new file in `overflow_dir`, named for the call: `<call-id>.stdout` or `<call-id>.stderr`, where the
call ID is 32 lower-case hex digits. The result holds the file's path and the stream's size in place
of the stream, and `truncated: true`. It holds no part of the stream, so the agent decides how to
read the file. For example:

```json
{"exit_code": 0, "stdout_file": "/home/agent-01/Projects/.agent-proxy-overflow/0123456789abcdef0123456789abcdef.stdout", "stdout_bytes": 4194304, "stderr": "", "truncated": true}
```

A script's stdout over the cap fails the script, so its result is the command form above.

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
  a credential that is not stored is the text of its refusal.
- `root`, whose subject is a root's path.
- `overflow_dir`, whose subject is the overflow directory.
- `egress`, whose subject is an egress proxy's container name. The detail names the domains it
  allows. A grant with `domains: []` has no proxy, so it has no entry.
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
- a credential that the grant names is not stored, as in the example above;
- a credential that the grant gives as an environment variable is not valid UTF-8, or holds a NUL;
- the cage cannot start, or the action's executable cannot run in it;
- the action does not finish within its timeout;
- the cage fails after it starts;
- an overflow file cannot be created or written;
- the audit log cannot record the call, and the result is withheld.

The detail of any other error is masked.
