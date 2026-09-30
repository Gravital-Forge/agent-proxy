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
  container that keeps its storage on a tmpfs, and removes the local image, so a test pulls the
  plugin by digest.
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

`load_config` reads one YAML file and returns an `InstanceConfig`: the agent tree, the plugins
pinned by image digest, and the scopes with their grants. `read_yaml` parses the file with a safe
loader that also refuses a key repeated in one mapping and a YAML merge key (`<<`). The pydantic
models refuse unknown fields and check each field's form. The `InstanceConfig` validator then checks
the rules that span fields, such as a root lying inside the agent tree. `docs/design.md` sets the
validation rules under "Config". The models and the validator together check each rule that the file
and the credential store can settle. For example, the models make a grant state `roots`, `domains`,
and `credentials`, because `Grant` gives none of them a default.

A config that fails raises `ConfigError`, which holds one message per failure. Each message names
the file, and a failure inside the file also names its location.

`load_config` takes the credential names as an argument and does not open the credential store.

### `credentials.py`

`CredentialStore` keeps each credential as one file in a directory, named for the credential and
holding its value verbatim. `set` writes a value to a staging file whose name begins with a dot,
then renames it into place, so `entries` never lists a half-written credential. `entries` reports
each name with its set time, read from the file's modification time. `names` returns the stored
names, which is the set `load_config` checks grants against. `docs/design.md` gives the credential
model under "Credentials".

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
rule each stage checks. A failure raises `ManifestError`, which holds one message per failure. Each
message from the schema stage or the rule stage is located by its JSON path. `Manifest.warnings`
returns one message per action that declares pass-through args.

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
