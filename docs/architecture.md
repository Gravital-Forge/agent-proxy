# agent-proxy architecture

This document describes what is built, and cites the code by path. The decisions behind it live in
`docs/design.md`; this document names the section that holds each one and does not restate it.

## The core package

The core is the uv package in `core/agent-proxy/`, placed as `docs/design.md` sets out under
"Repository layout".

- `core/agent-proxy/pyproject.toml` declares the dependencies, the `agent-proxy` console script, and
  the poe tasks that lint and test the package.
- `core/agent-proxy/agent_proxy/` holds the source.
- `core/agent-proxy/tests/unit/` holds the unit tests. They are plain pytest with `unittest.mock`,
  and none of them touches Docker.
- `core/agent-proxy/tests/integration/` holds the integration tests. They run on the Docker daemon
  that `DOCKER_HOST` names. `conftest.py` there builds the test plugin, pushes it to a `registry:3`
  container that keeps its storage on a tmpfs, and removes the local image, so a plugin test pulls
  the plugin by digest. For the cage tests, it also builds the test plugin locally under its own
  tag, and creates an internal network that each of their cages joins.
- `core/agent-proxy/tests/test-plugin/` holds the source of the test plugin image.

## Modules

Each module below is a file in `core/agent-proxy/agent_proxy/`.

### `errors.py`

`ProxyError` is the base of every error a caller can act on. Each module that refuses input raises
its own subclass, and the message names the refused value and what the module expected. The admin
command catches `ProxyError` and prints it as a refusal.

### `config.py`

This module holds the rules the other modules share, and the instance config those rules govern.

The shared rules are the name patterns and the checks that apply them. `NAME_PATTERN` governs scope,
token, and credential names, and `check_name` refuses a name that does not match it with
`InvalidNameError`. `PLUGIN_NAME_PATTERN` governs plugin names. `parse_duration` turns a duration
such as `120s` or `7d` into a `timedelta`, and refuses anything else with `DurationError`.

`load_config` reads one YAML file and returns an `InstanceConfig`: the agent tree, the overflow
directory, the cap on running cages, the plugins pinned by image digest, and the scopes with their
grants. `read_yaml` parses the file with a safe loader that also refuses a key repeated in one
mapping and a YAML merge key (`<<`). The pydantic models refuse unknown fields and check each
field's form. The `InstanceConfig` validator then checks the rules that span fields, such as a root
lying inside the agent tree. `docs/design.md` sets the validation rules under "Config". The models
and the validator together check each rule that the file and the credential store can settle. For
example, the models make a grant state `roots`, `domains`, and `credentials`, because `Grant` gives
none of them a default.

Two fields of `InstanceConfig` serve the cage runner. `max_cages` is the instance's cap on running
cages, an integer of 1 or more; `docs/design.md` gives the cap under "The cage". `overflow_dir` is
the directory that takes a stream over the output cap; `docs/design.md` gives it under "Results". It
is an absolute path with no `..` segment, inside the agent tree, and it must not equal the agent
tree. A grant's root must not equal the overflow directory, contain it, or lie inside it, so no cage
mounts it and the sweep never reaches a root.

A config that fails raises `ConfigError`, which holds one message per failure. Each message names
the file, and a failure inside the file also names its location.

`load_config` takes the credential names as an argument and does not open the credential store.

### `credentials.py`

`CredentialStore` keeps each credential as one file in a directory, named for the credential and
holding its value verbatim. `set` writes a value to a staging file whose name begins with a dot,
then renames it into place, so `entries` never lists a half-written credential. `entries` reports
each name with its set time, read from the file's modification time. `names` returns the stored
names, which is the set `load_config` checks grants against. `read` returns a stored value as bytes,
and `path` returns the file that holds it. Each refuses a name that breaks `NAME_PATTERN` with
`InvalidNameError`, and a name the store does not hold with `UnknownCredentialError`.
`docs/design.md` gives the credential model under "Credentials".

### `auth.py`

`TokenStore` keeps tokens in one SQLite file, one row per token. `mint` generates a token, stores
only its SHA-256 digest with the name, scope, and optional expiry, and returns the token. The caller
sees the token once, and the store cannot produce it again. `verify` compares the presented token's
digest with every stored digest in constant time, and returns the record of a match that has not
expired. `records` lists the stored tokens, and `revoke` deletes one. `docs/design.md` gives the
token model under "Tokens".

The store reads the clock through the `now` callable it takes, so tests pass a fixed clock.

### `audit.py`

`AuditRecord` holds the fields of one call. `AuditWriter` appends each record to `audit.log` as one
JSON line, and rotates the file by size; `AUDIT_MAX_BYTES` and `AUDIT_BACKUP_COUNT` set the limits.
A write that fails raises its error to the caller and prints nothing. `tail` returns the newest
lines in write order, and reads the rotated files only while it needs more lines. `docs/design.md`
gives what a record holds and what it never holds under "Audit".

### `schemas/manifest.schema.json`

This file is the manifest schema, a JSON Schema that ships in the package as data. It fixes the
members of a manifest and of each action kind, their types, and the patterns their strings match.
`plugins.py` checks the rules the schema cannot state. `docs/writing-a-plugin.md` explains the
manifest to a plugin author, and `docs/design.md` gives what a manifest declares under "Plugins and
actions".

### `plugins.py`

This module reads a plugin's manifest from its image and checks it.

`parse_manifest` takes the bytes a `describe` printed and returns a `Manifest`, which holds each
action by name as a `CommandAction` or a `ScriptAction`. It checks in three stages and stops after
the first stage that fails. First it decodes the bytes as a JSON object. Then it validates the
document against the manifest schema. Last it checks the rules the schema cannot state, reusing the
name, duration, and credential-path checks of `config.py`. `docs/writing-a-plugin.md` gives every
rule each stage checks; among them, no two parameters of one action share a flag. A failure raises
`ManifestError`, which holds one message per failure. Each message from the schema stage or the rule
stage is located by its JSON path. `Manifest.warnings` returns one message per action that declares
pass-through args. `decode_json_object` is the first stage, and `results.py` also uses it to read a
script's output.

`describe` runs an image's `describe` entrypoint in a container and returns its stdout. The
container has no network, a read-only root filesystem, no capabilities, and `no-new-privileges`, and
it is removed whatever happens. `DESCRIBE_TIMEOUT_SECONDS` bounds the run, and
`DESCRIBE_STDOUT_LIMIT` bounds the stdout. A non-zero exit is a refusal that carries the exit status
and the start of stderr, at most `STDERR_EXCERPT_LIMIT` bytes. Every describe container carries
`CONTAINER_LABELS`, which the integration tests set to the test label.

`ensure_image` pulls an image by its reference only when the daemon does not hold it.
`load_manifest` makes the image present, then describes it and parses the output. `check_image`
describes a local image and parses the output, and never pulls. A Docker failure raises
`PluginImageError`. Each function takes the Docker client as an argument; the module opens none.
`DOCKER_FAILURES` holds the error classes that a Docker call can raise. `WAIT_TIMEOUTS` holds the
error classes that a container wait raises when its timeout passes, which depend on the transport.
`runner.py` catches both.

### `results.py`

This module turns what a cage printed into a call's result, and removes old overflow files.

`capture_stream` reads one stream of a cage as chunks and returns a `CapturedStream`. A stream of at
most `OUTPUT_CAP_BYTES` stays inline. A stream over the cap goes in full to a new file named
`<call_id>.stdout` or `<call_id>.stderr` in the overflow directory, and the `CapturedStream` holds
that path and the stream's size in place of the bytes. The module holds at most the cap and one more
chunk in memory. It opens the overflow directory one component at a time from `/`, and refuses a
symbolic link at any component. It creates the file with mode `0o660` and refuses a file that
already exists. A file it cannot create or write raises `OverflowWriteError`. `new_call_id` returns
a fresh ID that matches `CALL_ID_PATTERN`, the form that names the file.

`command_content` forms a command's result from the exit code and the two streams. An inline stream
appears as text, decoded as UTF-8 with each bad byte replaced. An overflowed stream appears as its
file and size, and the result then carries `truncated`. `script_content` forms a script's result by
the rule `docs/writing-a-plugin.md` gives under "How an action runs", and reports whether the script
failed. A failed script's result is the command result. `docs/design.md` gives the result forms and
the cap under "Results".

`sweep_overflow` removes each entry directly in a directory that is not a directory itself and that
was last modified more than `OVERFLOW_MAX_AGE` before the time it is given. It judges and removes a
symbolic link as itself, never its target, and skips an entry that is gone before it acts.
`OverflowSweeper` runs the sweep on a daemon thread once at start, then every `SWEEP_INTERVAL`. It
reads the directory and the time through the callables it takes, fresh for each sweep. A sweep that
fails logs a warning, and the thread keeps its schedule. `docs/design.md` gives the retention under
"Results".

### `runner.py`

This module runs one action call in one cage, a container that exists for that call only.
`docs/design.md` gives the cage under "The cage".

A `CageCall` holds the call ID, the image, the action, the grant, the arguments, the Docker network,
and the config's `overflow_dir` and `max_cages`. `CageRunner.run` takes a call and returns an
`Outcome`. It refuses a bad call before it takes a slot under the cap:

1. The call ID must match `CALL_ID_PATTERN`.
2. `validate_input` checks the arguments against the action's input schema, draft 2020-12, and
   raises `InputError` with one message per violation.
3. For a command action, `form_argv` forms argv as `docs/writing-a-plugin.md` gives under "How argv
   is formed", and raises `InputError` for a value that begins with `-`. For a script action, the
   entrypoint is the executable, and `script_stdin` gives the arguments as compact UTF-8 JSON.
4. `check_roots` raises `RootError` for a root that has a symbolic link at any component of its
   path, because the daemon follows a link in a bind source.
5. `cage_environment` reads each credential the grant gives as an environment variable.
   `cage_mounts` binds each root at its own path with the grant's mode, then binds each credential
   the grant gives as a file, read-only, from its file in the credential store. A credential that is
   not stored, or an environment value that is not UTF-8 or holds a NUL, raises `CredentialError`.

The run then waits for a slot, creates the cage, starts it, and removes it by force whatever
happens. The slot stays taken until the cage is removed. The cage's entrypoint is the argv or the
executable, which also clears the image's command. The cage runs as `CONTAINER_USER`, with a
read-only root filesystem, every capability dropped, `no-new-privileges`, a tmpfs at `TMPFS_PATH`
with `TMPFS_OPTIONS`, the call's network, `CONTAINER_LABELS`, and the `LOG_DRIVER` log driver. A
script's input goes to the cage's stdin, then end of file. A script that stops reading its input is
not a failure in itself.

The timeout is the grant's, or else the action's. It runs from the start, and covers the input and
the wait for the exit. After the exit, the run reads stdout and stderr from the cage's log through
`capture_stream`, and forms the result in `results.py`. The `Outcome` holds the result, whether a
script failed, the exit code, the duration in milliseconds, the size of each stream, and whether a
stream overflowed.

Every refusal and failure that `runner.py` raises itself is a `RunnerError`. A create, an attach of
stdin, or a start that fails raises `CageStartError`. A cage past its timeout raises
`CageTimeoutError`. Any other failure after the start, a failed removal included, raises
`CageFailedError`. An `OverflowWriteError` passes through unchanged.

`CageLimiter` counts the running cages of an instance, and every call shares one. `slot` waits with
no bound until fewer cages run than the limit it is given. Each call brings its own limit, so calls
that started under different configs still share one count. `CageRunner` takes the Docker client,
the credential store, and the limiter; the module opens none of them.

### `reload.py`

This module holds the config in force and replaces it on request.

A `Snapshot` holds an `InstanceConfig`, the manifest of each of its plugins, and the warnings of
those manifests. `build_snapshot` loads the config file with `load_config`, then loads each plugin's
manifest through the `load_manifest` callable it takes. A `load_config` refusal propagates
unchanged, and no plugin loads. Otherwise `build_snapshot` gathers each plugin that fails to load
and each grant whose action its plugin's manifest does not declare into one `ConfigError`, and every
message names the config file. It checks a grant only against a plugin whose manifest loaded. The
callable is the seam to Docker: a caller binds `plugins.load_manifest` to a client, and a unit test
passes a fake.

`LiveConfig` holds the snapshot in force. A call reads `current` once when it starts and uses that
snapshot to its end. `reload` builds a snapshot and puts it in force with one reference assignment,
one reload at a time. A build that fails raises, and the snapshot in force stays. `docs/design.md`
gives the reload decisions under "Config".

`ControlServer` serves reload requests for a `LiveConfig` on a Unix socket, from a daemon thread. It
binds the socket, sets its mode to `0o600`, and only then listens. It replaces a socket left at the
path, and refuses a path that holds anything else. A path it cannot bind raises `ControlError`. A
request is the line `reload`. The reply is one JSON line, `{"ok": true, "warnings": [...]}` or
`{"ok": false, "errors": [...]}`.

`request_reload` is the client. It sends the request, waits at most `RELOAD_REPLY_TIMEOUT_SECONDS`
for the reply, and returns it as a `ReloadReply`. A socket with nothing listening or out of reach,
no reply in time, a connection that closes with no reply, and a reply of another form each raise
`ControlError`, which names the socket path.

### `cli.py`

This module is the admin command, and it is the one module that reads the environment. It opens each
store under the state directory, and hands the credential store's names to `load_config`. It opens
the Docker client for `manifest check`, and calls `request_reload` for `reload`. No store imports
another; within the package, the stores import only `config.py` and `errors.py`.

## Where state lives

Environment variables place everything the admin command reads and writes.

`AGENT_PROXY_STATE_DIR` names the state directory. The directory must already exist, and the command
does not change its mode. The command keeps its state beneath it:

- `tokens.sqlite3` is the token store. The store creates it with mode `0o600`.
- `credentials/` is the credential store. The store creates the directory, narrows it to mode
  `0o700`, and writes each credential file with mode `0o600`.
- `audit/` holds `audit.log` and its rotated files, `audit.log.1` onward. The writer creates the
  directory and narrows it to mode `0o700`. `tail` reads a missing directory or log as no lines.
- `control.sock` is the control socket. `reload` connects to it. A `ControlServer` from `reload.py`
  serves a socket at the path it is given, with mode `0o600`.

`AGENT_PROXY_CONFIG` names the config file. Only `token mint` reads it. `docs/design.md` gives where
the file comes from under "Config".

An unset `AGENT_PROXY_STATE_DIR` or `AGENT_PROXY_CONFIG`, or a state directory that does not exist,
is a refusal that names the variable.

The cage runner reaches into the credential store's directory, and into one place outside the state
directory. It bind-mounts each credential that a grant gives as a file, read-only, from its file in
the credential store. It writes each overflow file under the config's `overflow_dir`, in the agent's
tree, and the sweep removes old entries there. The runner creates neither directory.

`manifest check` reaches the Docker daemon through `DOCKER_HOST`, which the Docker SDK reads from
the environment. With `DOCKER_HOST` unset, the SDK uses its default socket. A daemon it cannot reach
is a refusal that names `DOCKER_HOST` and its value, or says that it is unset.

## The admin command

The `agent-proxy` command is `agent_proxy.cli:run`. It takes a noun and a verb:

- `token mint --name NAME --scope SCOPE [--expires DURATION]` loads the config, refuses a scope the
  config does not define, and prints the new token. A token minted without `--expires` never
  expires.
- `token list` prints each token's name, scope, created time, last-used time, and expiry, never the
  token.
- `token revoke NAME` deletes a token.
- `credential set NAME` reads the value from stdin, stores it, and prints its size in bytes.
- `credential list` prints each credential's name and set time, never the value.
- `credential rm NAME` deletes a credential.
- `audit tail [-n COUNT]` prints the newest audit lines, ten unless `-n` says otherwise.
- `manifest check IMAGE` runs the `describe` of a local image and checks the manifest it prints. It
  never pulls, so an absent image is a refusal. On a pass it prints a line that names the image and
  the number of actions it declares.
- `reload` connects to `control.sock` beneath the state directory and asks the server there to
  reload its config. It prints `reloaded` when the new config is in force. When the server refuses,
  the command prints each error it sent as a refusal.

A listing prints a header row, then one tab-separated row per entry, with `-` for a time that is not
set.

The command exits 0 on success. A refusal prints one line per failure to stderr, each beginning
`agent-proxy:`, and exits 1. A usage error exits 2. A warning is not a failure: `manifest check` and
`reload` print each warning to stderr as a line beginning `agent-proxy: warning:`, and exit 0.
`docs/design.md` gives who runs the command and how under "Tokens", "Credentials", "Audit",
"Config", and "Plugins and actions".
