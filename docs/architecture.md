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
- `core/agent-proxy/tests/integration/` holds the integration tests.

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

### `cli.py`

This module is the admin command, and it is the one module that joins the others. It reads the
environment, opens each store under the state directory, and hands the credential store's names to
`load_config`. No store imports another; within the package, the stores import only `config.py` and
`errors.py`.

## Where state lives

Environment variables place everything the admin command reads and writes.

`AGENT_PROXY_STATE_DIR` names the state directory. The directory must already exist, and the command
does not change its mode. The command keeps its state beneath it:

- `tokens.sqlite3` is the token store. The store creates it with mode `0o600`.
- `credentials/` is the credential store. The store creates the directory, narrows it to mode
  `0o700`, and writes each credential file with mode `0o600`.
- `audit/` holds `audit.log` and its rotated files, `audit.log.1` onward. The writer creates the
  directory and narrows it to mode `0o700`. `tail` reads a missing directory or log as no lines.

`AGENT_PROXY_CONFIG` names the config file. Only `token mint` reads it. `docs/design.md` gives where
the file comes from under "Config".

An unset variable, or a state directory that does not exist, is a refusal that names the variable.

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

A listing prints a header row, then one tab-separated row per entry, with `-` for a time that is not
set.

The command exits 0 on success. A refusal prints one line per failure to stderr, each beginning
`agent-proxy:`, and exits 1. A usage error exits 2. `docs/design.md` gives who runs the command and
how under "Tokens", "Credentials", and "Audit".
