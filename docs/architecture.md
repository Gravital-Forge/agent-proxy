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
  tag, and creates an internal network that each of their cages joins. For the egress tests, it
  builds the probe image from `core/agent-proxy/tests/egress-probe/`, the pinned Alpine base with
  curl added, and every egress test cage runs that image. It builds the verifier image from
  `core/verifier/`, and points the runner's `VERIFIER_IMAGE` at it for every test.
  `test_verifier.py` there runs the verifier as the entrypoint of a hardened cage, outside the
  runner. `test_transport.py` there starts an `Instance` from `mcp.py`, serves its app on a loopback
  port, and drives the MCP surface over HTTP with a FastMCP client.
- `core/agent-proxy/tests/test-plugin/` holds the source of the test plugin image. The test plugin
  holds an action for each thing the tests reach, and its `manifest.json` declares them.

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

Two rules of the validator serve egress. A grant must not name one domain more than once. A grant's
credential must not take the environment name `PROXY_ENV_NAME`, which is `HTTPS_PROXY`, because the
cage's network sets that variable. `DOMAIN_PATTERN` is the form of a domain, and `egress.py` checks
it again before it writes a proxy config.

A config that fails raises `ConfigError`, which holds one message per failure. Each message names
the file, and a failure inside the file also names its location.

`load_config` takes the credential names as an argument and does not open the credential store.

### `credentials.py`

`CredentialStore` keeps each credential as one file in a directory, named for the credential and
holding its value verbatim. `set` writes a value through `write_owner_only`, which writes a staging
file whose name begins with a dot, with mode `0o600`, then renames it over the target, so `entries`
never lists a half-written credential. `egress.py` writes each proxy config through the same
function. `entries` reports each name with its set time, read from the file's modification time.
`names` returns the stored names, which is the set `load_config` checks grants against. `read`
returns a stored value as bytes, and `path` returns the file that holds it. Each refuses a name that
breaks `NAME_PATTERN` with `InvalidNameError`, and a name the store does not hold with
`UnknownCredentialError`. `docs/design.md` gives the credential model under "Credentials".

### `auth.py`

`TokenStore` keeps tokens in one SQLite file, one row per token. `mint` generates a token, stores
only its SHA-256 digest with the name, scope, and optional expiry, and returns the token. The caller
sees the token once, and the store cannot produce it again. `verify` compares the presented token's
digest with every stored digest in constant time, and returns the record of a match that has not
expired. `verify` writes nothing. `authenticate` matches as `verify` does, and on a match writes the
token's last-used time and returns the record with that time. The match and the write are one
`BEGIN IMMEDIATE` transaction, so the store holds its write lock from before the match. Any SQLite
failure in `verify` or `authenticate`, a read or a write, raises `TokenStoreError`. `records` lists
the stored tokens, and `revoke` deletes one. `docs/design.md` gives the token model under "Tokens".

The store reads the clock through the `now` callable it takes, so tests pass a fixed clock.

### `audit.py`

`AuditRecord` holds the fields of one call. `AuditWriter` appends each record to `audit.log` as one
JSON line, and rotates the file by size; `AUDIT_MAX_BYTES` and `AUDIT_BACKUP_COUNT` set the limits.
Each write opens the log with `O_APPEND`, appends the line with `os.write`, and closes the log, so
no file stays open between writes. Writes from several threads take turns. A write that fails part
way truncates the log back to its size before the line, so the log holds no byte of that line. When
that truncation also fails, the writer latches: `available` turns false, and every later write
fails. Every failure raises `AuditError` to the caller and prints nothing. When an `AuditWriter` is
created, it cuts a regular-file log back to just after its last newline, so a fragment never
prefixes a later line. `tail` returns the newest lines in write order, and reads the rotated files
only while it needs more lines. `docs/design.md` gives what a record holds and what it never holds
under "Audit".

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
the rule `docs/writing-a-plugin.md` gives under "How an action runs". A failed script's result is
the command result. `docs/design.md` gives the result forms and the cap under "Results".

`sweep_overflow` removes each entry directly in a directory that is not a directory itself and that
was last modified more than `OVERFLOW_MAX_AGE` before the time it is given. It judges and removes a
symbolic link as itself, never its target, and skips an entry that is gone before it acts.
`OverflowSweeper` runs the sweep on a daemon thread once at start, then every `SWEEP_INTERVAL`. It
reads the directory and the time through the callables it takes, fresh for each sweep. A sweep that
fails logs a warning, and the thread keeps its schedule. `docs/design.md` gives the retention under
"Results".

### `egress.py`

This module gives each cage the egress its grant names: one proxy for each distinct domain list,
each on its own internal network. `docs/design.md` gives the egress model under "The cage".

`list_key` returns the key of a domain list, a SHA-256 digest of the sorted list cut short, so two
grants that name the same domains in any order share one key. `network_name` forms, from the key,
the name of the list's internal network and of its proxy container. `domain_lists` returns each
distinct sorted list that the grants of a config name. The empty list has a key and a network like
any other list.

`CageNetwork` holds the network a cage joins and the environment that network gives it.
`cage_network` returns it for a grant's domains. For a non-empty list, the environment holds one
variable, `HTTPS_PROXY=http://egress:1080`, which names the proxy's tunnel. For the empty list, the
environment is empty.

`render_config` returns, as YAML, the iron-proxy config that allows exactly the domains of a list.
It refuses an empty list, and a domain that does not match `DOMAIN_PATTERN`, with
`EgressConfigError`. Every listener but the tunnel binds loopback, so a cage on the network reaches
only the tunnel on `TUNNEL_PORT`. The config has no management block, which keeps the management
listener off. The proxy passes TLS through by SNI and holds no CA.

`start_proxy` creates the proxy of a list from `IRON_PROXY_IMAGE`, joins it to the list's network
under the alias `PROXY_ALIAS` and then to `BRIDGE_NETWORK`, starts it, and returns it once it
serves. Both networks must already exist. The proxy runs as uid 0 with a read-only root filesystem,
every capability dropped, and `no-new-privileges`. The daemon bind-mounts the proxy's config
directory into it read-only, and the proxy's command names the config file. It is ready when its log
holds the tunnel's start line and it still runs. A proxy that stops, or is not ready within
`PROXY_READY_TIMEOUT_SECONDS`, raises `EgressStartError`, which carries the tail of its log, and is
removed. A Docker failure raises `EgressError`. `IRON_PROXY_IMAGE` pins the proxy image by digest.

`EgressProxies` manages the egress networks and proxies on one daemon. `ensure` takes domain lists,
and makes the proxy image, the bridge, and each list's network and proxy present. The empty list
gets its network and no proxy. `ensure` keeps a proxy that runs, on `IRON_PROXY_IMAGE`, from a
config file that holds exactly what `render_config` renders, and replaces any other proxy of that
name. A proxy that fails to start is removed with its config directory. A failed image lookup or
pull raises `PluginImageError`, and any other failure raises `EgressError` or `EgressStartError`.
`prune` takes the lists to keep. It removes every other egress proxy with its config directory, then
every other egress network, and the bridge always stays. `prune` never raises: a failure logs a
warning, and the next prune tries again.

Every egress network and proxy carries the label `agent-proxy.egress`, set to the list's key, or to
`bridge` on the bridge, and `prune` finds them by that label. Each also carries `LABELS`, which the
integration tests set to the test label.

### `runner.py`

This module runs one action call in one cage, a container that exists for that call only.
`docs/design.md` gives the cage under "The cage".

A `CageCall` holds the call ID, the image, the action, the grant, the arguments, the cage's network
as a `CageNetwork`, and the config's `overflow_dir` and `max_cages`. `CageRunner.run` takes a call
and returns an `Outcome`. It refuses a bad call before it takes a slot under the cap:

1. The call ID must match `CALL_ID_PATTERN`.
2. `validate_input` checks the arguments against the action's input schema, draft 2020-12, and
   raises `InputError` with one message per violation.
3. For a command action, `form_argv` forms argv as `docs/writing-a-plugin.md` gives under "How argv
   is formed", and raises `InputError` for a value that begins with `-`. For a script action, the
   entrypoint is the executable, and `script_stdin` gives the arguments as compact UTF-8 JSON.
4. `hold_roots` opens each root's directory one component at a time from `/`, with `O_DIRECTORY` at
   every step and `O_NOFOLLOW` on each component after `/`, so it follows no symbolic link anywhere
   in the path. The daemon follows a link in a bind source. A component that is a symbolic link, is
   not a directory, or does not exist raises `RootError`. For each root, `hold_roots` returns a
   `RootIdentity`: the path, and the device and inode of the directory it opened. It holds each
   directory's descriptor open until the cage is removed, or until the call is refused. A released
   inode can be reused, so a root deleted and recreated in the meantime could match the recorded
   pair; a held descriptor keeps its inode from any other directory.
5. `cage_environment` reads each credential the grant gives as an environment variable.
   `cage_mounts` binds each root at its own path with the grant's mode, then binds each credential
   the grant gives as a file, read-only, from its file in the credential store. A credential that is
   not stored, or an environment value that is not UTF-8 or holds a NUL, raises `CredentialError`.
   `_create` adds one more mount to these: the verifier image, read-only, at `VERIFIER_MOUNT`.

The run then waits for a slot, creates the cage, starts it, and removes it by force whatever
happens. The slot stays taken until the cage is removed. The cage's entrypoint is the verifier's
argv wrapped around the action's: `VERIFIER_PATH`, a nonce that is new for each call, each root's
path, device, and inode as decimal text, `--`, then the argv or the executable. The entrypoint also
clears the image's command. "The root verifier" below gives what the verifier checks. The cage runs
as `CONTAINER_USER`, with a read-only root filesystem, every capability dropped,
`no-new-privileges`, a tmpfs at `TMPFS_PATH` with `TMPFS_OPTIONS`, `CONTAINER_LABELS`, and the
`LOG_DRIVER` log driver. The runner applies the call's `CageNetwork` as given: the cage joins its
network, and gets its environment beside the grant's credentials. A script's input goes to the
cage's stdin, then end of file. A script that stops reading its input is not a failure in itself.

The timeout is the grant's, or else the action's. It runs from the start, and covers the input and
the wait for the exit. After the exit, an exit code of `VERIFIER_REFUSED_EXIT` sends the run to the
cage's stderr first. `_first_stderr_line` reads stderr up to its first newline, at most
`_VERIFIER_LINE_BYTES` of it. A line that begins with the call's nonce and `root` is the verifier's
refusal of a root, and raises `RootError`. A line that begins with the nonce and `exec` raises
`CageStartError`. Any other line leaves the exit code to the action. An action can exit with that
code itself, but it never sees the nonce, so it cannot forge a refusal. A root refused in the cage
is refused after the call takes its slot, unlike the refusals listed above. Otherwise the run reads
stdout and stderr from the cage's log through `capture_stream`, and forms the result in
`results.py`. The `Outcome` holds the result, the exit code, the duration in milliseconds, the size
of each stream, and whether a stream overflowed.

Every refusal and failure that `runner.py` raises itself is a `RunnerError`. A create, an attach of
stdin, or a start that fails raises `CageStartError`. The verifier's `exec` refusal comes after the
start, and still raises `CageStartError`, as any action that cannot start does. The verifier's
`root` refusal raises `RootError`. A cage past its timeout raises `CageTimeoutError`. Any other
failure after the start, a failed removal included, raises `CageFailedError`. An
`OverflowWriteError` passes through unchanged.

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

`LiveConfig` holds the snapshot in force and an egress collaborator, an object with the `ensure` and
`prune` of the `Egress` protocol, as `EgressProxies` has. `LiveConfig.start` builds the first
snapshot, ensures the egress its grants name, prunes every other egress resource, and returns the
`LiveConfig`. A call takes its snapshot through `use`, which holds the snapshot in use from the
call's start to its end, so the egress that snapshot names stays through a reload. `current` returns
the snapshot in force.

`reload` builds a snapshot, ensures its egress, and only then puts it in force with one reference
assignment, one reload at a time. A build or an ensure that fails raises, and the snapshot in force
stays. A prune keeps each domain list that the snapshot in force or a snapshot in use names. A
reload prunes after it ends, whether it succeeded or failed. The last use of a snapshot no longer in
force also starts a prune when it ends. That prune runs on a daemon thread named `egress-prune` once
no reload holds the reload lock, and the use returns without waiting for it. `docs/design.md` gives
the reload decisions under "Config".

`ControlServer` serves reload requests for a `LiveConfig` on a Unix socket, from a daemon thread. It
binds the socket, sets its mode to `0o600`, and only then listens. It replaces a socket left at the
path, and refuses a path that holds anything else. A path it cannot bind raises `ControlError`. A
request is the line `reload`. The reply is one JSON line, `{"ok": true, "warnings": [...]}` or
`{"ok": false, "errors": [...]}`.

`request_reload` is the client. It sends the request, waits at most `RELOAD_REPLY_TIMEOUT_SECONDS`
for the reply, and returns it as a `ReloadReply`. A socket with nothing listening or out of reach,
no reply in time, a connection that closes with no reply, and a reply of another form each raise
`ControlError`, which names the socket path.

### `mcp.py`

This module is the MCP surface: the call path, its glue to FastMCP, and the instance that serves it.
`docs/design.md` gives the surface under "Surface", the result forms under "Results", and the audit
line under "Audit".

`CallGateway` is the call path, and it is synchronous. `list_tools` takes the request's
`Authorization` header, verifies its bearer token, and returns one `ToolSpec` per grant of the
token's scope in the config in force, sorted by name. A tool's input schema is the action's
`input_schema`. A listing does not record the token's use. A token that does not verify, a scope
that the config in force does not define, and a token store that cannot be read each list no tools.

`CallGateway.call` runs one call and audits it in one line. It authenticates the token, which
records its use. It then holds the snapshot in force through `LiveConfig.use`, finds the grant that
the tool names, and runs the cage through `CageRunner`. Its reply holds the result's content. A
failed script's reply is the command result, as a normal result. The only error results are
refusals.

Every refusal is a `CallRefusedError`, whose message is the refusal. A bad token, a token store that
cannot be read, a scope that the config in force does not define, a tool that the scope does not
grant, and each error that the runner raises are refusals. The audit line of a refused call holds
that message in its `refusal` field. A call whose audit line cannot be written is refused, and its
result is withheld. While the audit writer is not `available`, each call is refused before its token
is checked, and writes no line.

`ScopedTools` and `ProxyTool` connect the gateway to FastMCP. `ScopedTools` is a FastMCP `Provider`.
It lists the tools of each request's bearer token, and resolves every tool name to a `ProxyTool`, so
a call of a tool that the token does not list still reaches the gateway, which refuses and audits
it. `ProxyTool.run` hands the call to the gateway on a worker thread. It returns the reply as a
normal result, which holds one compact JSON text block and the same content as structured content.
It turns a `CallRefusedError` into a FastMCP `ToolError` whose text is `REFUSAL_PREFIX`, which is
`agent-proxy refused: `, followed by the refusal. An MCP client receives that as an error result.
The audit line's `refusal` field holds the refusal without the prefix.

`build_app` returns the ASGI app that serves the gateway's tools over MCP's Streamable HTTP
transport at `/mcp`. The app gives each input schema as the manifest gives it, and masks the detail
of each error that is not a refusal.

`snapshot_builder` returns the build that `LiveConfig.start` and each reload call. Each build reads
the config file, reads the credential names afresh, and loads each plugin's manifest through the
Docker client.

`Instance.start` opens the token store, the credential store, and the audit writer under the state
directory. It then starts the live config with `EgressProxies` over `egress/`, the control server on
`control.sock`, and the overflow sweeper, in that order. A step that fails closes what the steps
before it started, except that the egress proxies that `LiveConfig.start` made ready stay, and then
its error propagates. The calls of one instance share one `CageLimiter`. `Instance.app` is the app
that `build_app` returns. `Instance.close` closes the control socket, stops the sweeper, and closes
the audit writer. The egress proxies stay, and the next `LiveConfig.start` keeps or prunes them.

`serve` in `cli.py` runs the app under uvicorn through `_Server`. `_Server` routes SIGINT and
SIGTERM to uvicorn's shutdown on the main thread, restores the previous handlers when the server
stops, and re-raises neither signal.

### `cli.py`

This module is the admin command, and it is the one module that reads the environment. Outside
`serve`, it opens each store under the state directory itself, and hands the credential store's
names to `load_config`. It opens the Docker client for `manifest check` and `serve`, and calls
`request_reload` for `reload`. For `serve --host HOST --port PORT`, it starts an `Instance` from
`mcp.py`, which opens the stores, and serves its app on that address. No store imports another;
within the package, the stores import only `config.py` and `errors.py`.

## The root verifier

The verifier closes the race between the runner's check of a root and the daemon's mount of it. The
daemon resolves a bind source when it creates the cage, after the check and after the wait for a
slot. In that time the agent can swap a root, or a directory above it, for a symbolic link, or
delete the root and create a new directory at its path. The verifier runs in the cage after the
daemon has fixed the cage's mounts, so it checks what the action will see.

The verifier is the Go program in `core/verifier/main.go`. Its argv is
`verify <nonce> [<root-path> <st_dev> <st_ino>]... -- <action argv...>`. It stats each root path,
following links as the daemon does when it places a mount, and compares the device and inode with
the pair the runner recorded. When every pair matches, it looks up the first item of the action's
argv on `PATH` and replaces itself with the action through `execve`, with the environment unchanged.
Otherwise it writes one line to stderr and exits 125, the value of `VERIFIER_REFUSED_EXIT`:

- `<nonce> root <reason>` when the argv is malformed, or a root's stat fails or gives another pair.
  A reason about a root names its path, and the reason for a mismatch gives both pairs.
- `<nonce> exec <reason>` when the lookup or the exec fails.

`runner.py` above gives how the runner reads each line.

`core/verifier/Dockerfile` builds the verifier with `CGO_ENABLED=0` on a Go image pinned by digest.
Its build stage runs `gofmt`, `go vet`, and the tests in `core/verifier/main_test.go`, so a failing
check fails the build. The final stage is `FROM scratch` and holds the one file `/verify`. The image
must be on the instance's daemon under the reference `VERIFIER_IMAGE` before the first call, because
an image mount never pulls; a missing image fails the create with `CageStartError`. The cage sees
that one file, read-only, at `VERIFIER_PATH`.

## Where state lives

Environment variables place everything the admin command reads and writes. The config file places
the one directory outside the state directory that the cage runner writes.

`AGENT_PROXY_STATE_DIR` names the state directory. The directory must already exist, and the command
does not change its mode. The command keeps its state beneath it:

- `tokens.sqlite3` is the token store. The store creates it with mode `0o600`.
- `credentials/` is the credential store. The store creates the directory, narrows it to mode
  `0o700`, and writes each credential file with mode `0o600`.
- `audit/` holds `audit.log` and its rotated files, `audit.log.1` onward. The writer creates the
  directory and narrows it to mode `0o700`. `tail` reads a missing directory or log as no lines.
- `control.sock` is the control socket. `serve` binds it, and `reload` connects to it. A
  `ControlServer` from `reload.py` serves a socket at the path it is given, with mode `0o600`.
- `egress/` is the config root that `serve` gives `EgressProxies`. The paragraph on `EgressProxies`
  below gives what it keeps there.

`AGENT_PROXY_CONFIG` names the config file. `token mint` and `serve` read it. `docs/design.md` gives
where the file comes from under "Config".

An unset `AGENT_PROXY_STATE_DIR` or `AGENT_PROXY_CONFIG`, or a state directory that does not exist,
is a refusal that names the variable.

`manifest check` and `serve` reach the Docker daemon through `DOCKER_HOST`, which the Docker SDK
reads from the environment. With `DOCKER_HOST` unset, the SDK uses its default socket. A daemon it
cannot reach is a refusal that names `DOCKER_HOST` and its value, or says that it is unset.

The cage runner mounts credential files from the credential store's directory, as `runner.py` gives
above. It writes each overflow file under the config's `overflow_dir`, in the agent's tree, and the
sweep removes old entries there. The runner creates neither directory.

`EgressProxies` keeps the config of each proxy in its own directory, named for the proxy, under the
config root it is given. It creates each directory with mode `0o700`, and writes the config file in
it with mode `0o600`. The daemon bind-mounts that directory into the proxy by its path, so the path
must name the same directory for the daemon as for the instance. `prune` removes a directory with
its proxy.

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
- `serve --host HOST --port PORT` starts the instance and serves its MCP surface at `/mcp` on that
  address until SIGINT or SIGTERM, then closes the instance and exits 0. `PORT` is an integer from 1
  to 65535.

A listing prints a header row, then one tab-separated row per entry, with `-` for a time that is not
set.

The command exits 0 on success. A refusal prints one line per failure to stderr, each beginning
`agent-proxy:`, and exits 1. A usage error exits 2. A warning is not a failure: `manifest check` and
`reload` print each warning to stderr as a line beginning `agent-proxy: warning:`, and exit 0.
`docs/design.md` gives who runs the command and how under "Tokens", "Credentials", "Audit",
"Config", and "Plugins and actions".
