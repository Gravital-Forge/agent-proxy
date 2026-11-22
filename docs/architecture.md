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
  port, and drives the MCP surface over HTTP with a FastMCP client. For the instance tests,
  `conftest.py` builds the instance image from `core/docker/Dockerfile`, and its test variant from
  `core/agent-proxy/tests/instance-image/`. That variant's `.pth` startup hook sets the test label,
  and its Dockerfile sets `AGENT_PROXY_VERIFIER_IMAGE` to the verifier image the tests build. It
  also pushes the git plugin, and the github plugin with its stub gh, to the registry.
  `instance_harness.py` there holds the helpers the instance tests share: running the start script,
  waiting for the MCP surface, `docker exec`, and cleanup. `test_instance_image.py` checks the image
  and the run line that the start script gives it. `test_instance_serves.py` starts an instance on
  the agent's own daemon and drives it through MCP and `docker exec`. It also drives the self-check
  through both entrances, the tool and the command, and makes the credential, root, egress, and
  plugin checks fail on purpose. Its `test_an_instance_pulls_its_verifier_by_digest_at_start` starts
  an instance whose verifier is referenced by digest and absent from the daemon, and checks that a
  call succeeds after the instance pulls the verifier.
- `core/agent-proxy/tests/test-plugin/` holds the source of the test plugin image. The test plugin
  holds an action for each thing the tests reach, and its `manifest.json` declares them.

## Modules

Each module below is a file in `core/agent-proxy/agent_proxy/`.

### `config.py`

This module holds the rules the other modules share, and loads the instance config those rules
govern. The reference gives every field and rule of the config under
[The config file](reference.md#the-config-file). The pydantic models check each field's form, and
the `InstanceConfig` validator checks the rules that span fields, such as a root lying inside the
agent tree. `runner.py` matches the verifier's reference against `PINNED_IMAGE_PATTERN`, the form a
plugin's image takes, and `egress.py` checks `DOMAIN_PATTERN` again before it writes a proxy config.

Config validation does not consult the credential store. A credential that a grant names and the
store lacks is a warning, not a failure, and the callers of `load_config` compute it with
`unset_warnings` from `credentials.py`.

### `credentials.py`

`CredentialStore` keeps each credential as one file in a directory, named for the credential and
holding its value verbatim. `set` writes a value through `write_owner_only`, which writes a staging
file whose name begins with a dot, with mode `0o600`, then renames it over the target, so `entries`
never lists a half-written credential. `egress.py` writes each proxy config through the same
function. `entries` reports each name with its set time, read from the file's modification time.
`names` returns the stored names. `read` returns a stored value as bytes, and `path` returns the
file that holds it. Each refuses a name that breaks `NAME_PATTERN` with `InvalidNameError`, and a
name the store does not hold with `UnknownCredentialError`. `docs/design.md` gives the credential
model under "Credentials".

`unset_message` forms the one text that says a credential is not set and gives the command the
operator stores it with. The warnings at start, at reload, and at token mint use it, and so does the
runner's refusal. The reference gives the text under [Refusals](reference.md#refusals).

`unset_warnings` takes scopes and the stored names. It returns `unset_message` of each credential a
grant of the scopes names that the stored names lack, each name once, in the order the scopes and
their grants first name it.

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

This module turns what a cage printed into a call's result, and removes old overflow files. The
reference gives the result forms, the cap, and the retention under
[Results and refusals](reference.md#results-and-refusals).

`capture_stream` reads one stream of a cage as chunks and returns a `CapturedStream`, which holds
the bytes of a stream within the cap, or the overflow file's path and the stream's size. The module
holds at most the cap and one more chunk in memory. It opens the overflow directory one component at
a time from `/`, and refuses a symbolic link at any component. It refuses an overflow file that
already exists. A file it cannot create or write raises `OverflowWriteError`. `check_overflow_dir`
walks a directory the same way, then opens a file with no name in it through `O_TMPFILE` and closes
it, so it leaves no file, and raises `OverflowWriteError` when it cannot.

`command_content` and `script_content` form a command's and a script's result.

`sweep_overflow` judges and removes a symbolic link as itself, never its target, and skips an entry
that is gone before it acts. `OverflowSweeper` runs the sweep on a daemon thread. It reads the
directory and the time through the callables it takes, fresh for each sweep. A sweep that fails logs
a warning, and the thread keeps its schedule.

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

`proxy_status` returns the Docker status of a domain list's proxy, or `None` when the daemon holds
no proxy of that name, and raises `EgressError` when the lookup fails in any other way.

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
   directory's descriptor open until the run ends, whether it returns or raises. A cage, if there is
   one, is removed before the descriptors close. A released inode can be reused, so a root deleted
   and recreated in the meantime could match the recorded pair; a held descriptor keeps its inode
   from any other directory.
5. `roots_environment` gives the cage `ROOTS_ENV_NAME`, which holds the grant's root paths in grant
   order as a compact JSON array. `cage_environment` reads each credential the grant gives as an
   environment variable. The grant's `env` adds each of its pairs as given. `cage_mounts` binds each
   root at its own path with the grant's mode, then binds each credential the grant gives as a file,
   read-only, from its file in the credential store. A credential that is not stored raises
   `CredentialError` with `unset_message` of its name. When several are not stored, the refusal
   names the first env credential in grant order that is not stored, else the first such file
   credential. A credential's environment value that is not UTF-8 or holds a NUL also raises
   `CredentialError`. The runner reads the store on every call. `_create` adds one more mount to
   these: the verifier image, read-only, at `VERIFIER_MOUNT`.

The run then waits for a slot, creates the cage, starts it, and removes it by force whatever
happens. The slot stays taken until the cage is removed. The cage's entrypoint is the verifier's
argv wrapped around the action's: `VERIFIER_PATH`, a nonce that is new for each call, each root's
path, device, and inode as decimal text, `--`, then the argv or the executable. The entrypoint also
clears the image's command. "The root verifier" below gives what the verifier checks. The cage runs
as `CONTAINER_USER`, with a read-only root filesystem, every capability dropped,
`no-new-privileges`, a tmpfs at `TMPFS_PATH` with `TMPFS_OPTIONS`, `CONTAINER_LABELS`, and the
`LOG_DRIVER` log driver. The runner applies the call's `CageNetwork` as given: the cage joins its
network, and gets its environment beside the roots variable, the grant's credentials, and the
grant's `env`. A script's input goes to the cage's stdin, then end of file. A script that stops
reading its input is not a failure in itself.

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

`VERIFIER_IMAGE` is the reference of the verifier image that every cage mounts. The module sets it
when it loads, with `verifier_image(os.environ)`: the value of `AGENT_PROXY_VERIFIER_IMAGE`, or
`SOURCE_VERIFIER_IMAGE`, which is `agent-proxy-verifier:1`, when the variable is unset or empty.
`ensure_verifier` takes the Docker client and makes a verifier pinned by digest present: when
`VERIFIER_IMAGE` matches `PINNED_IMAGE_PATTERN`, it calls `ensure_image` from `plugins.py`, which
pulls the image when the daemon does not hold it and raises `PluginImageError` on a Docker failure.
It neither looks up nor pulls a tag reference.

### `reload.py`

This module holds the config in force and replaces it on request.

A `Snapshot` holds an `InstanceConfig`, the manifest of each of its plugins, and its warnings. The
warnings are those of the manifests, then one per credential a grant names that the credential store
lacks, from `unset_warnings` over the stored names that `build_snapshot` takes. `build_snapshot`
loads the config file with `load_config`, then loads each plugin's manifest through the
`load_manifest` callable it takes. A `load_config` refusal propagates unchanged, and no plugin
loads. Otherwise `build_snapshot` gathers each plugin that fails to load and each grant whose action
its plugin's manifest does not declare into one `ConfigError`, and every message names the config
file. It checks a grant only against a plugin whose manifest loaded. The callable is the seam to
Docker: a caller binds `plugins.load_manifest` to a client, and a unit test passes a fake.

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

`ControlServer` serves reload and check requests on a Unix socket, one at a time, from a daemon
thread. It takes the `LiveConfig` to reload, the build that a reload calls, and the check to run: a
callable that takes a scope name or `None` and returns one report per scope it checked. It binds the
socket, sets its mode to `0o600`, and only then listens. It replaces a socket left at the path, and
refuses a path that holds anything else. A path it cannot bind raises `ControlError`. A request is
one line of at most 128 bytes: `reload`, `check`, or `check <scope>`. The reply is one JSON line. A
reload's reply is `{"ok": true, "warnings": [...]}` or `{"ok": false, "errors": [...]}`. A check's
reply is `{"ok": true, "reports": [...]}`, which holds the report of each scope checked, or
`{"ok": false, "errors": [...]}` when the check is refused. The outer `ok` says whether the server
served the request; each report's own `ok` says whether its checks passed. Any other request gets an
error that names it and the requests the server takes.

`request_reload` and `request_check` are the clients. `request_check` takes a scope name, or `None`
for every scope. Each sends its request, waits at most `CONTROL_REPLY_TIMEOUT_SECONDS` for the
reply, and returns it as a `ReloadReply` or a `CheckReply`. A socket with nothing listening or out
of reach, no reply in time, a connection that closes with no reply, and a reply of another form each
raise `ControlError`, which names the socket path. A check reply whose report or entry lacks a field
of the report's form is a reply of another form.

### `selfcheck.py`

This module is the self-check. It checks one scope's local wiring against a snapshot: the things
that can be wrong while the instance looks healthy. `docs/design.md` gives what it checks, and what
it leaves out, under "Surface". The reference gives the report's form under
[The self-check report](reference.md#the-self-check-report).

`SelfCheck` takes the instance's Docker client, credential store, and `CageRunner`. `SelfCheck.run`
takes a `Snapshot` and the name of a scope that its config defines, and returns a `CheckReport`.
Each kind of check does this:

- `credential` — each credential that a grant names is among the store's names, read afresh on each
  run.
- `root` — each root path opens through `hold_roots` from `runner.py`, as it opens for a call, and
  closes again. A root that several grants name is checked once, by its path.
- `overflow_dir` — the config's overflow directory passes `check_overflow_dir` from `results.py`.
- `egress` — the proxy of each distinct non-empty domain list has the status `running`, which
  `proxy_status` from `egress.py` reads.
- `plugin` — for each plugin that the scope's grants use, the instance's `CageRunner` runs the
  image's `describe` in a cage. The cage gets an empty grant, so it has no roots, no credentials,
  and no `env`. It joins the Docker network `none`, so it has no network, and
  `DESCRIBE_TIMEOUT_SECONDS` from `plugins.py` bounds it. It goes through the root verifier and
  counts against `max_cages`, as the cage of a call does. The check passes when `describe` exits 0,
  and it discards the output. The runner never pulls, so an image that has left the daemon since the
  config loaded fails the check. A `describe` whose output passes the output cap writes an overflow
  file on each check, as the output of any call does, and the sweep removes it.

`run` turns every failed check into an entry, never an error. The refusal or Docker failure that a
check meets becomes the detail of an entry whose `ok` is false.

### `mcp.py`

This module is the MCP surface: the call path, its glue to FastMCP, and the instance that serves it.
`docs/design.md` gives the surface under "Surface" and the audit line under "Audit". The reference
gives the result forms and the refusals under
[Results and refusals](reference.md#results-and-refusals).

`CallGateway` is the call path, and it is synchronous. `list_tools` takes the request's
`Authorization` header, verifies its bearer token, and returns the self-check tool and one
`ToolSpec` per grant of the token's scope in the config in force, sorted by name. A tool's input
schema is the action's `input_schema`, and the self-check tool's is `SELF_CHECK_INPUT_SCHEMA`. A
listing does not record the token's use. A token that does not verify and a token store that cannot
be read each list no tools. A token whose scope the config in force does not define lists the
self-check tool alone.

`CallGateway.call` runs one call and audits it in one line. It authenticates the token, which
records its use. It then holds the snapshot in force through `LiveConfig.use`, finds the grant that
the tool names, and runs the cage through `CageRunner`. Its reply holds the result's content.

A call of the self-check tool goes to `SelfCheck.run` from `selfcheck.py` instead of a cage. It
holds the snapshot in force through `LiveConfig.use` for the whole check, refuses a call that
carries any argument, and returns the report's content. Its audit line names the token and its
scope, holds `agent-proxy_self_check` as the action and the call's arguments, and holds no exit code
and no output size, because no single cage's output is the result. The cages that the check runs
write no line of their own.

`CallGateway.check_scopes` serves the control socket's check request. It takes a scope name, or
`None` for every scope that the config in force defines, sorted by name, and holds the snapshot in
force through `LiveConfig.use` for the whole request. It returns the content of each scope's report,
and writes one audit line for each scope it checks, with no token name. While the audit writer is
not `available`, it refuses the request. It refuses a scope that the config in force does not
define, and names the scopes it defines. When a scope's line cannot be written, it refuses the
request and withholds every report.

Every refusal is a `CallRefusedError`, whose message is the refusal. Each error that the runner
raises is a refusal. The audit line of a refused call holds that message in its `refusal` field. A
call whose audit line cannot be written is refused, and its result is withheld. While the audit
writer is not `available`, each call is refused before its token is checked, and writes no line.

`ScopedTools` and `ProxyTool` connect the gateway to FastMCP. `ScopedTools` is a FastMCP `Provider`.
It lists the tools of each request's bearer token, and resolves every tool name to a `ProxyTool`, so
a call of a tool that the token does not list still reaches the gateway, which refuses and audits
it. `ProxyTool.run` hands the call to the gateway on a worker thread. It returns the reply as a
normal result, and turns a `CallRefusedError` into a FastMCP `ToolError` whose text is the refusal
behind its prefix. The audit line's `refusal` field holds the refusal without the prefix.

`build_app` returns the ASGI app that serves the gateway's tools over MCP's Streamable HTTP
transport at `/mcp`. The app gives each input schema as the listing gives it, unchanged.

`snapshot_builder` returns the build that `LiveConfig.start` and each reload call. Each build reads
the config file, reads the credential names afresh, and loads each plugin's manifest through the
Docker client.

`Instance.start` opens the token store, the credential store, and the audit writer under the state
directory. It then makes the verifier present with `ensure_verifier` from `runner.py`, and starts
the live config with `EgressProxies` over `egress/`, the control server on `control.sock`, and the
overflow sweeper, in that order. A verifier that cannot be pulled raises `PluginImageError`, and
nothing after it starts. Before the control server, it builds the one `CageRunner` and the one
`CallGateway`, with a `SelfCheck` over that runner, and the control server serves the gateway's
`check_scopes`. A step that fails closes what the steps before it started, except that the egress
proxies that `LiveConfig.start` made ready stay, and then its error propagates. The calls of one
instance and its checks share one `CageLimiter`. `Instance.app` is the app that `build_app` returns.
`Instance.warnings` holds the warnings of the snapshot that the live config started with.
`Instance.close` closes the control socket, stops the sweeper, and closes the audit writer. The
egress proxies stay, and the next `LiveConfig.start` keeps or prunes them.

### `cli.py`

This module is the operator command, which the reference gives under
[The operator command](reference.md#the-operator-command). It reads the environment, and the one
other module that does is `runner.py`, which reads only `AGENT_PROXY_VERIFIER_IMAGE`, as `runner.py`
above gives. Outside `serve`, it opens each store under the state directory itself, and `token mint`
hands the credential store's names to `unset_warnings`. It opens the Docker client for
`manifest check` and `serve`, and calls `request_reload` for `reload` and `request_check` for
`check`. For `serve --host HOST --port PORT`, it starts an `Instance` from `mcp.py`, which opens the
stores, and serves its app on that address. No store imports another; within the package, the stores
import only `config.py` and `errors.py`.

## The root verifier

The verifier closes the race between the runner's check of a root and the daemon's mount of it. The
daemon resolves a bind source when it starts the cage, after the check and after the wait for a
slot. In that time the agent can swap a root, or a directory above it, for a symbolic link, or
delete the root and create a new directory at its path. The verifier runs in the cage after the
daemon has fixed the cage's mounts, so it checks what the action will see.

The verifier is the Go program in `core/verifier/main.go`. Its argv is
`verify <nonce> [<root-path> <st_dev> <st_ino>]... -- <action argv...>`. It stats each root path,
following links as the daemon does when it places a mount, and compares the device and inode with
the pair the runner recorded. When every pair matches, it finds the action's executable from the
first item of the action's argv. It looks up a bare name on `PATH`, and uses a name that holds a
slash as given. It then replaces itself with the action through `execve`, with the environment
unchanged. Otherwise it writes one line to stderr and exits 125, the value of
`VERIFIER_REFUSED_EXIT`:

- `<nonce> root <reason>` when the argv is malformed, or a root's stat fails or gives another pair.
  A reason about a root names its path, and the reason for a mismatch gives both pairs.
- `<nonce> exec <reason>` when the lookup or the exec fails.

`runner.py` above gives how the runner reads each line.

`core/verifier/Dockerfile` builds the verifier with `CGO_ENABLED=0` on a Go image pinned by digest.
Its build stage runs `gofmt`, `go vet`, and the tests in `core/verifier/main_test.go`, so a failing
check fails the build. The final stage is `FROM scratch` and holds the one file `/verify`. The cage
sees that one file, read-only, at `VERIFIER_PATH`.

The cage mounts the image under the reference `VERIFIER_IMAGE`, which comes from the instance
image's environment, as `runner.py` above gives. An image mount never pulls, so the image must be on
the instance's daemon before the first call; a missing image fails the create with `CageStartError`.
A reference pinned by digest, as a published instance image carries, is pulled at the instance's
start by `ensure_verifier` when the daemon lacks it. A tag reference, such as the source build's
`agent-proxy-verifier:1`, must already be on the daemon.

## The git plugin

The git plugin is the uv package in `plugins/git/`, placed as `docs/design.md` sets out under
"Repository layout". The `manifest-check` job in `.github/workflows/ci.yml` builds the image of each
plugin under `plugins/` and runs `agent-proxy manifest check` on it. `plugins/git/README.md` gives
everything about the plugin itself: its actions, its rules, commit signing, its layout, and its
tests.

## The github plugin

The github plugin is the uv package in `plugins/github/`, placed as `docs/design.md` sets out under
"Repository layout". The `manifest-check` job in `.github/workflows/ci.yml` checks its image as it
checks the git plugin's. `plugins/github/README.md` gives everything about the plugin itself: its
actions, its attachments, its egress, its layout, and its tests.

## The hcp-terraform plugin

The hcp-terraform plugin is the uv package in `plugins/hcp-terraform/`, placed as `docs/design.md`
sets out under "Repository layout". The `manifest-check` job in `.github/workflows/ci.yml` checks
its image as it checks the git plugin's. Its code is in
`plugins/hcp-terraform/scripts/hcp_terraform_plugin/`. `plugins/hcp-terraform/README.md` gives
everything about the plugin itself: its actions, its workspace list, where the token goes, its
egress, its layout, and its tests.

## Where state lives

The operator command keeps its state in the state directory, which the environment names, as the
reference gives under [Environment](reference.md#environment). The config file places the one
directory outside the state directory that the cage runner writes.

The state directory holds:

- `tokens.sqlite3` is the token store. The store creates it with mode `0o600`.
- `credentials/` is the credential store. The store creates the directory, narrows it to mode
  `0o700`, and writes each credential file with mode `0o600`.
- `audit/` holds `audit.log` and its rotated files, `audit.log.1` onward. The writer creates the
  directory and narrows it to mode `0o700`. `tail` reads a missing directory or log as no lines.
- `control.sock` is the control socket. `serve` binds it, and `reload` and `check` connect to it. A
  `ControlServer` from `reload.py` serves a socket at the path it is given, with mode `0o600`.
- `egress/` is the config root that `serve` gives `EgressProxies`. The paragraph on `EgressProxies`
  below gives what it keeps there.

`docs/design.md` gives where the config file comes from under "Config".

The cage runner mounts credential files from the credential store's directory, as `runner.py` gives
above. It writes each overflow file under the config's `overflow_dir`, in the agent's tree, and the
sweep removes old entries there. The runner creates neither directory.

`EgressProxies` keeps the config of each proxy in its own directory, named for the proxy, under the
config root it is given. It creates each directory with mode `0o700`, and writes the config file in
it with mode `0o600`. The daemon bind-mounts that directory into the proxy by its path, so the path
must name the same directory for the daemon as for the instance. `prune` removes a directory with
its proxy.

## The instance

The instance image is built by `core/docker/Dockerfile` from `core/agent-proxy/`. Its build stage
installs the package with uv from the lockfile, and its final stage holds only the environment that
stage made. The image's process is `agent-proxy serve` on container port 8000, on all interfaces.
The image sets `DOCKER_HOST` to the socket path the run line mounts, `AGENT_PROXY_CONFIG` to
`/etc/agent-proxy/config.yaml`, and `AGENT_PROXY_VERIFIER_IMAGE` to the build argument
`VERIFIER_IMAGE`, which defaults to `agent-proxy-verifier:1`.

`serve` starts an `Instance` from `mcp.py` and serves its MCP surface, as the reference gives under
[The operator command](reference.md#the-operator-command), and closes the instance when it stops.
`serve` in `cli.py` runs the app under uvicorn through `_Server` from `mcp.py`. `_Server` routes
SIGINT and SIGTERM to uvicorn's shutdown on the main thread, restores the previous handlers when the
server stops, and re-raises neither signal.

`core/docker/start-instance.sh` holds the run line. It takes the image, the container name, the host
port, the config directory, the state directory, and the agent tree, and passes any further
arguments to `docker run`. The line gives the container:

- the user 0, which the rootless daemon maps to the user that runs it;
- the hardening: every capability dropped, `no-new-privileges`, a read-only root filesystem with a
  tmpfs at `/tmp`, and the restart policy `unless-stopped`;
- container port 8000, published on `127.0.0.1` at the host port;
- the daemon's socket, from `DOCKER_HOST`, at `/var/run/docker.sock`;
- the config directory, read-only, at `/etc/agent-proxy`;
- the state directory and the agent tree, each at its own host path, with `AGENT_PROXY_STATE_DIR`
  naming the state directory.

The state directory and the agent tree keep their host paths because the daemon resolves every bind
source the instance names on the host: each egress config directory under the state directory, and
each root and credential file a cage mounts. The script refuses a state directory or an agent tree
that is not an absolute path equal to its realpath. When a container of that name exists, the script
starts it as it was created, or leaves it running.

`core/docker/provision-instance.sh` sets up everything the instance needs on the host and starts it,
and `core/docker/teardown-instance.sh` removes it. Provisioning builds the verifier and instance
images from the checkout. With `--release VERSION`, it pulls the published instance image of that
release in their place, and tags it `agent-proxy:1`. Both scripts take `--tree PATH`, the agent's
tree, which defaults to `<home>/Projects` and must lie inside the agent's home.
`docs/runbooks/per-agent-instance.md` explains each of their steps.

## Published images

`core/docker/build-images.sh <version> <registry> <source-url>` builds the images and pushes each
one to the registry, tagged with the version, which is `<major>.<minor>.<patch>`. It finds the
repository from its own path, so it runs from any working directory. It builds in this order:

1. `agent-proxy-verifier` from `core/verifier/`.
2. `agent-proxy`, the instance, from `core/agent-proxy/` with `core/docker/Dockerfile`. The build
   argument `VERIFIER_IMAGE` takes the verifier's digest reference.
3. `agent-proxy-plugin-<plugin>` from each directory under `plugins/` that holds a `Dockerfile`, in
   name order.

Every build is for `linux/amd64` with no provenance attestation, so each pushed digest names one
image manifest. Every image carries the label `org.opencontainers.image.source`, set to the source
URL, and `org.opencontainers.image.version`, set to the version. After each push, the script prints
the image's digest reference, `<registry>/<name>@sha256:<digest>`, as one line on stdout. The build
and push output goes to stderr, so stdout holds only the image references.

`.github/workflows/release.yml` runs when the operator publishes a GitHub release. Its one job,
`images`, refuses a release marked as a pre-release, and a tag that is not
`v<major>.<minor>.<patch>`, before it checks out, so neither publishes an image. It logs in to GHCR
with the job's token, and runs `build-images.sh` with the tag's version without the `v`, the
registry `ghcr.io/gravital-forge`, and the repository's URL. It then writes each image's reference
into the release notes under a `## Images` heading, one bullet each. On a re-run, it replaces that
heading and everything after it. The workflow holds `contents: read`; the job holds
`contents: write`, to edit the release notes, and `packages: write`, to push. Event data reaches
each script through the step's environment, never through the script's text.

The `images` job in `.github/workflows/ci.yml` runs `build-images.sh` with the version `0.0.0` on
each pull request and each push to `main`. It pushes to a `registry:3` service container that lives
only for the job, and no pull-request job logs in to a registry. The job then checks that the
instance image's `AGENT_PROXY_VERIFIER_IMAGE` equals the first reference the script printed, the
verifier's.

Every action in the workflows is pinned by commit SHA, with its version in a trailing comment.
`.github/dependabot.yml` asks for weekly updates of those actions, of the base images that the
Dockerfiles pin by tag and digest, and of the uv lockfiles. `docs/design.md` gives the decisions
behind the published images under "Deployment and isolation".
