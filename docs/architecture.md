# agent-proxy architecture

An agent that needs a credential usually gets the credential. A token kept as a file the agent can
read is held by anything that reads it, and it cannot be narrowed to particular operations. A tool
surface hardcoded to one service serves nothing else. And a tool such as git runs code that a
repository controls, such as hooks and the programs that `.git/config` names, so that code gets
every reach the tool has.

agent-proxy answers each problem. The operator mints tokens bound to a policy, and the agent holds
only a token. Plugins put any credentialed tool behind the same surface. Each call runs in a cage
that bounds what the executed code can touch: only the files, the hosts, and the credentials its
grant names.

This document describes what is built and why, and cites the code by path. The
[reference](reference.md) gives the config file's fields, the operator command, and the results and
refusals of a call.

## Concepts

These terms carry one meaning each through the code and the documents.

- **Operator** — the person who runs an instance: who writes its config, stores its credentials, and
  mints its tokens.
- **Instance** — one running agent-proxy serving exactly one agent user. Every agent gets its own
  instance; nothing is shared between instances.
- **Scope** — a named policy in the instance's config file (for example `dev-agent`): the complete
  list of grants a token holding it may use.
- **Token** — a bearer secret that names exactly one scope. It carries no policy of its own.
- **Grant** — one entry in a scope: an action, plus that action's roots, domains, and credentials,
  an optional timeout override, an optional memory limit, and an optional `env`.
- **Action** — one operation a plugin offers, of one of three kinds:
  - a **command** action runs a fixed argv prefix at subcommand granularity (`gh pr create`, never
    bare `gh`), extended by typed parameters;
  - a **script** action runs an executable in the image that takes its input as JSON on stdin and
    prints JSON on stdout, so filtering and special logic live there;
  - a **provider** action runs an executable in the image that prints the value of a provided
    credential. It is never a tool, as [Provided credentials](#provided-credentials) gives.
- **Provided credential** — a credential whose value a provider action gives, in place of a value
  the operator stores.
- **Plugin** — an OCI image, pinned by digest, whose manifest declares its actions.
  [Writing a plugin](writing-a-plugin.md) gives what the image holds.
- **Manifest** — the JSON a plugin image's `describe` entrypoint prints.
  [The manifest](writing-a-plugin.md#the-manifest) gives its members and rules.
- **Cage** — the short-lived container one action call runs in, built from its grant: only the
  grant's roots mounted, only the grant's domains reachable, only the grant's credentials present. A
  provider run's cage is built from its `provided_credentials` entry in the same way.
- **Egress proxy** — an iron-proxy container serving one distinct domain list.

## The call path

The instance is an MCP server over the Streamable HTTP transport, at `/mcp` on a loopback port that
is unique to the instance. The agent's MCP client sends its token as `Authorization: Bearer <token>`
(`core/agent-proxy/agent_proxy/mcp.py`).

A listing returns the self-check tool and one tool for each grant of the token's scope in the config
in force, sorted by name, each named as [A scope's grants](reference.md#a-scopes-grants) gives. A
tool's input schema is the action's input schema from the manifest, unchanged. A listing does not
record the token's use. A token that does not verify, and a token store that cannot be read, each
list no tools. A token whose scope the config in force does not define lists the self-check tool
alone.

A call goes through these steps:

1. The instance verifies the bearer token and records its use.
2. It takes the config in force and holds it until the call ends, so a reload during the call does
   not change the grant the call runs under.
3. It finds the grant that the tool names. A call of a tool that the token's scope does not grant
   still reaches this step, so it is refused and audited like any other refusal.
4. It resolves the value of each provided credential the grant names, as
   [Provided credentials](#provided-credentials) gives. A provider run takes a cage of its own, and
   the call holds no cage while it waits.
5. It refuses a bad call before the call waits for a cage: input that fails the action's input
   schema, a value for argv that begins with `-`, a root it cannot open, and a stored credential
   that the store lacks.
6. It runs the action in a cage, as [The cage](#the-cage) gives, and forms the result.
7. It writes the call's audit line, as [Audit](#audit) gives, and only then returns the result.

The reference gives the result forms and every refusal under
[Results and refusals](reference.md#results-and-refusals).

### Reload

The config file is the operator's. The operator installs it root-owned on the host, and the instance
reads it through a read-only mount, so neither the agent nor the instance can change it.

`agent-proxy reload` asks the running instance to load the file again
(`core/agent-proxy/agent_proxy/reload.py`). The instance validates the new config whole, loads the
manifest of each of its plugins, and starts every egress proxy the new config needs. Only then does
it put the new config in force, in one step, and one reload runs at a time. A config that fails any
of these steps is refused, and the running config stays. Calls already running finish under the
config they started with.

After a reload, and after the last call under a config that is no longer in force, the instance
removes each egress proxy and network that neither the config in force nor a config still in use
names.

`reload` and `check` reach the instance through its control socket, which serves one request at a
time. Editing a scope changes the reach of every token naming it, from the next reload.

## The cage

Every action call runs in a fresh sibling container on the instance's rootless Docker daemon, never
inside the instance's process (`core/agent-proxy/agent_proxy/runner.py`).

- **Files:** each root in the grant is bind-mounted at its own host path with the grant's mode.
  Same-path mounting keeps git worktrees working. Nothing else of the host is visible. A plugin
  carries no path metadata: the cage is the only file boundary, and a path outside the grant's roots
  does not exist in it, so the tool fails with its own error. The cage's environment names the roots
  in `AGENT_PROXY_ROOTS`, a JSON array in grant order, so a plugin acts on exactly its roots without
  inferring them.
- **Credentials:** the cage receives each credential its grant gives, as an environment variable or
  as a read-only file mounted from the credential store, and no other.
- **Hardening:** the cage runs as container root, which the rootless daemon maps to the instance's
  service user. It drops every capability, sets `no-new-privileges`, and has a read-only root
  filesystem with a 64 MiB tmpfs at `/tmp` for scratch. It runs under a seccomp profile that refuses
  the setuid and setgid bits, as [The seccomp profile](#the-seccomp-profile) gives.
- **Limits:** the cage may run at most 1024 processes, `CAGE_PIDS_LIMIT` in `runner.py`. Its memory
  limit is the grant's `memory`, or 4 GiB, `CAGE_MEMORY_BYTES`, when the grant gives none. The swap
  limit equals the memory limit, so the cage gets no swap. The pages of `/tmp` count against the
  memory limit. The instance warns at start when its daemon cannot enforce either limit, as
  [The instance](#the-instance) gives, because Docker then drops the limit and still runs the cage.
- **Log:** the cage's output reaches the instance through the cage's log, which keeps at most two
  files of 512 MiB each, `LOG_OPTIONS` in `runner.py`. So one call cannot fill the service user's
  disk. The cap bounds the log's encoded size, not the output's. Docker's `json-file` log driver
  writes each line of output as a JSON object that also holds the line's stream and time, with the
  line's text escaped, and the one log holds stdout and stderr together. So a call can pass the cap
  with well under 1 GiB of output, and soonest when its output comes in short lines.
  [Known limits](#known-limits) gives what a call past the cap returns.
- **Label:** every cage carries the label `agent-proxy.cage=1`, and no other container does. The
  start finds the cages a killed instance left by that label, as [The instance](#the-instance)
  gives.
- **Concurrency:** the config's `max_cages` caps the cages that run at one time. A call over the cap
  waits, with no bound, for a cage to end. Every call, provider run, and self-check of an instance
  shares one count, so calls that started under different configs still count together.
- **Timeout:** the grant's `timeout`, or else the action's. It runs from the cage's start and covers
  a script's input and the wait for the exit. On timeout the cage is killed.

The instance removes each cage by force whatever the call's outcome, and the cage holds its place
under the cap until it is removed. A kill of the instance's process while calls run leaves their
cages until the next start, as [Known limits](#known-limits) gives. Files the agent owns belong to a
uid that the cage's user namespace does not map, so the cage writes them through the ACL but cannot
change their mode or owner.

Before it creates a cage, the instance opens each root's directory one path component at a time from
`/`, and follows no symbolic link anywhere in the path. A root that is a symbolic link, is not a
directory, or does not exist is refused. The instance records the device and inode of each
directory, and holds each one open until the cage is removed. A held directory keeps its inode from
any other directory, so a root deleted and created again cannot match the recorded pair.

### The root verifier

The verifier closes the race between the instance's check of a root and the daemon's mount of it.
The daemon resolves a bind source when it starts the cage, after the check and after the wait for a
cage. In that time the agent can swap a root, or a directory above it, for a symbolic link, or
delete the root and create a new directory at its path. The verifier runs in the cage after the
daemon has fixed the cage's mounts, so it checks what the action will see.

The verifier is the Go program in `core/verifier/main.go`. It is built with `CGO_ENABLED=0` into a
static executable, so it runs in any plugin image, with no libc or runtime in the cage. The cage's
entrypoint is the verifier, wrapped around the action:
`verify <nonce> [<root-path> <st_dev> <st_ino>]... -- <action argv...>`. The nonce is new for each
call.

The verifier stats each root path, following links as the daemon does when it places a mount, and
compares the device and inode with the pair the instance recorded. When every pair matches, it finds
the action's executable, a bare name on `PATH` or a name with a slash as given, and replaces itself
with the action through `execve`. The action then runs as PID 1, with its argv, stdin, and
environment unchanged. Otherwise the verifier writes one line to stderr and exits 125:

- `<nonce> root <reason>` when the argv is malformed, or a root's stat fails or gives another pair.
  A reason about a root names its path, and the reason for a mismatch gives both pairs. The call is
  refused for its root.
- `<nonce> exec <reason>` when the lookup or the exec fails. The call is refused because the action
  cannot run.

An action can exit 125 itself, but it never sees the nonce, so it cannot forge a refusal. Any other
first line of stderr leaves the exit code to the action.

`core/verifier/Dockerfile` builds the verifier on a Go image pinned by digest. Its build stage runs
`gofmt`, `go vet`, and the verifier's tests, so a failing check fails the build. The final stage is
`FROM scratch` and holds the one file `/verify`. Each cage mounts that image read-only and sees the
one file at `/.agent-proxy/verify`.

An image mount never pulls, so the verifier image must be on the instance's daemon before the first
call; a missing image fails the cage's start. The instance image names its verifier in
`AGENT_PROXY_VERIFIER_IMAGE`. A reference pinned by digest, as a published instance image carries,
is pulled at the instance's start when the daemon lacks it. A tag reference, such as the source
build's `agent-proxy-verifier:1`, must already be on the daemon.

### The seccomp profile

The rootless daemon maps the cage's root to the service user, so a file a cage creates in a root of
mode `rw` belongs to the service user on the host. With the setuid bit, such a file would run as the
service user for whoever runs it. With the setgid bit, it would run with the service user's group,
which reaches parts of the service user's home on a host whose homes are mode `750`. Neither the
dropped capabilities nor `no-new-privileges` stops a cage from setting either bit. So every cage
runs under a seccomp profile that refuses both bits
(`core/agent-proxy/agent_proxy/cage_seccomp.py`).

The profile is Docker's default profile with one change. The default ships verbatim in
`core/agent-proxy/agent_proxy/seccomp/default.json`, and the instance derives the cage's profile
from it once per process:

- `chmod`, `fchmod`, `fchmodat`, `fchmodat2`, `creat`, `mknod`, and `mknodat` are allowed only when
  the mode argument holds neither bit.
- `open` and `openat` are allowed when the mode holds neither bit, or when the flags hold neither
  `O_CREAT` nor `O_TMPFILE`, because the kernel reads the mode only with one of those flags. The
  flag values are those of `amd64`, the one platform the images are built for.
- `openat2` fails with `ENOSYS`, because seccomp cannot read the mode inside its struct. A caller
  that gets `ENOSYS` falls back to `openat`.

Every other call keeps the default profile's rule. The default already refuses `io_uring`, which
could otherwise pass a mode that seccomp never sees. A refused call fails with `EPERM`, so a `chmod`
that keeps a directory's setgid bit fails too, as
[How an action runs](writing-a-plugin.md#how-an-action-runs) gives.

## Egress

The instance runs one egress proxy for each distinct domain list across its grants and its provided
credentials (`core/agent-proxy/agent_proxy/egress.py`).

- A proxy is named for its list: `apx-egress-` and the first 16 hex digits of the SHA-256 of the
  sorted list, so two grants that name the same domains in any order share one proxy. The proxy sits
  on its own internal Docker network of the same name, under the network alias `egress`. Every proxy
  also joins one shared bridge, `apx-egress-bridge`, which is its only route out.
- A cage joins its grant's network and gets `HTTPS_PROXY=http://egress:1080`. A process that ignores
  that variable has no route out. A grant with `domains: []` gets an internal network with no proxy,
  and so no egress at all.
- A cage reaches only the proxy's tunnel listener, on port 1080, where CONNECT requests arrive.
  Every other listener (DNS, plain HTTP, TLS, and metrics) binds loopback inside the proxy. The
  config has no management block, so the management listener stays off.
- TLS passes through by SNI with `tls.mode: "sni-only"` and no CA, so the proxy never intercepts it.
  The allowlist is `transforms: [{name: allowlist, config: {domains: [...]}}]`.
- iron-proxy rejects traffic that is not HTTP, so git reaches GitHub over HTTPS with a token, never
  over SSH.

A proxy never changes once it starts. The instance never uses iron-proxy's management reload: a
proxy whose config or image differs from what its list needs is replaced. The instance never writes
a `source_ip` rule, because iron-proxy accepts such a rule and ignores it, which widens access. A
test in `core/agent-proxy/tests/integration/test_egress.py` pins that behaviour on the pinned image.
iron-proxy is pinned by digest.

Each proxy's config lives in its own directory under the state directory's `egress/`, and the daemon
bind-mounts the directory, never a single file, read-only into the proxy. A replaced file under a
single-file mount keeps its old inode. The config sets `dns.proxy_ip`, which iron-proxy requires
while its DNS listener is on, and the proxy's command passes `-config` on every run, because the
image's entrypoint has no default arguments.

The proxy runs as uid 0 in its container with a read-only root filesystem, every capability dropped,
and `no-new-privileges`. It is ready when its log holds the tunnel's start line and it still runs:
iron-proxy exits 0 when a listener fails to bind, so no exit code marks a good start. A proxy that
stops, or is not ready within 10 seconds, is removed with its config directory, so a config that
iron-proxy refuses never sits behind a running proxy.

The instance starts every proxy its config needs at start and at each reload, before the config goes
in force, and removes the proxies and networks no config names, as [Reload](#reload) gives. The
bridge always stays. A removal that fails logs a warning, and the next one tries again.

## Tokens

A token is `apx_` followed by 256 random bits, URL-safe encoded. The prefix lets secret scanners
recognize a leaked token (`core/agent-proxy/agent_proxy/auth.py`).

The instance stores the SHA-256 of each token, never the token, with its name, scope, created time,
last-used time, and optional expiry, in `tokens.sqlite3`. The operator sees a token once, when it is
minted, and the store cannot produce it again. A presented token's digest is compared with every
stored digest in constant time. An expired token does not verify. A call matches its token with a
read that takes no write lock, so a token that matches nothing never waits for another call's write.
A match records the token's last-used time in a transaction of its own, which updates the row only
while it still holds the matched digest. A token revoked between the match and that update does not
verify.

Only the operator manages tokens, with the `token` verbs of
[The operator command](reference.md#the-operator-command). Agents cannot mint, narrow, or refresh a
token. The operator delivers a token by writing it into the agent's MCP client config as the
`Authorization` header.

## Credentials

A credential is an opaque named value. A stored credential is stored verbatim as one file in the
state directory's `credentials/`, which only the instance's service user can read
(`core/agent-proxy/agent_proxy/credentials.py`). A write goes to a staging file first, then renames
it over the target, so a listing never shows a half-written credential. A provided credential's
value never enters the store, as [Provided credentials](#provided-credentials) gives.

Only the operator manages stored credentials, with the `credential` verbs of
[The operator command](reference.md#the-operator-command). A grant gives each stored credential it
uses to the cage as an environment variable or as a file, in the forms of
[A scope's grants](reference.md#a-scopes-grants).

The instance reads the store on every call, so a stored credential takes effect on the next call
with no reload. A call under a grant whose stored credential the store lacks is refused before a
cage starts, and audited like any other refusal. The grant's tools stay listed for the token. The
config load never consults the store: a stored credential that a grant or a provided credential
names and the store lacks is a warning, as
[Failures and warnings](reference.md#failures-and-warnings) gives.

### Provided credentials

A provided credential's value comes from a provider action, not from the store
(`core/agent-proxy/agent_proxy/providers.py`). The config names each one under
`provided_credentials`, with its provider and the provider's inputs, as
[Provided credentials](reference.md#provided-credentials) gives. A grant names a provided credential
as it names a stored one, and its cage receives the value as an environment variable. The store is
never read for a provided name. A provider's inputs are stored credentials only, so no provided
credential is the input of another provider.

No token can run a provider. A grant that names a provider action is refused at config load, so a
provider is never a tool, and only a `provided_credentials` entry runs one.

The gateway resolves each provided credential that a call's grant names before the call's cage takes
its slot, and hands the values to the runner with the call (`mcp.py`). The runner places each value
as it places a stored credential given as an environment variable, through the same UTF-8 and NUL
checks, and knows nothing of where a provided value comes from or of the cache.

The instance holds one cache of provided values, in memory, so a restart empties it. Each value has
an expiry: the earlier of the provider's `expires_at` and the start of its run plus the entry's
`max_age`. A value is fresh until `refresh_before` before its expiry, usable until its expiry, and
never handed out at or after it. Each cached value also holds what it came from: the config entry
and the provider plugin's image reference. A lookup under a config whose entry or image differs
treats the value as absent.

A call refreshes a value when the value is not fresh, never on a timer:

- A fresh value serves the call, and no provider runs.
- Otherwise the provider runs under a lock of that credential's own. A call that needs the same
  value waits on the lock, then takes the value the run left when that value is fresh. Otherwise the
  waiting call runs the provider itself. Runs for different credentials go side by side.
- When the run fails and the cached value has not expired, the call uses that value, and the next
  call runs the provider again.
- When the run fails and no unexpired value is cached, the call is refused before its cage starts,
  as [Refusals](reference.md#refusals) gives.

A reload keeps each cached value whose entry and provider image the new config leaves unchanged, and
drops the rest, once the new config is in force (`reload.py`).

A provider run is a cage of its own, through the normal path of [The cage](#the-cage): the root
verifier, the cage cap, the cage limits, and the seccomp profile. It has no roots. It receives the
entry's stored credentials and its `env`, and reaches the hosts of the entry's `domains`. Its
timeout is the entry's `timeout`, or else the action's. Its stdin is `{}`. The instance counts every
byte of both streams, and the run's audit line gives the full sizes. It keeps the stdout up to the
output cap and never writes it to an overflow file, because the overflow directory lies in the
agent's tree. A stdout past the cap fails the run. Of the stderr, it keeps only the first 1,024
bytes, for the failure's message. [A provider action](writing-a-plugin.md#a-provider-action) gives
what a run must print.

## Audit

The instance writes one JSON line per call to `audit/audit.log` in the state directory
(`core/agent-proxy/agent_proxy/audit.py`), except for the calls with no verified token that the
60-second rule below holds back. A line holds `timestamp`, `call_id`, `token_name`, `scope`,
`action`, `arguments`, `arguments_cut`, `exit_code`, `duration_ms`, `stdout_bytes`, `stderr_bytes`,
`truncated`, for a refused call the `refusal` without its MCP prefix, and `credential`. `credential`
names the provided credential on a provider run's line, and is `null` on every other line. A line
never holds a credential's value or any output's content, except the stderr of a failed provider
run. The `refusal` of the run's line quotes it as the stderr excerpt, and so does the `refusal` of
the call that the failure refuses.

The caller chooses the arguments, the tool name, and any input that a refusal quotes, so the line
caps each of them where it is rendered:

- When the compact JSON of the input arguments holds at most 16,384 characters,
  `ARGUMENTS_CAP_CHARACTERS`, `arguments` is the input object and `arguments_cut` is `null`.
- When it holds more, `arguments` is `null`, and `arguments_cut` is
  `{"bytes": <the UTF-8 size of the whole JSON>, "head": "<its first 16,384 characters>"}`.
- When the call has no verified token, both are `null`.
- An `action` longer than 256 characters, `ACTION_CAP_CHARACTERS`, becomes its first 256 characters,
  a space, and the marker `[cut]`. A `refusal` longer than 4,096 characters,
  `REFUSAL_CAP_CHARACTERS`, is cut the same way. The refusal that the caller receives is never cut.

Anyone who reaches the instance's port can make a call with no verified token, so the gateway bounds
how often such calls write a line (`core/agent-proxy/agent_proxy/mcp.py`). A call with no verified
token, or one that meets a token store that cannot be read, writes a line only when no such line was
written yet or 60 seconds, `TOKENLESS_LINE_INTERVAL`, have passed since the last one. Every such
call still gets its refusal. The next such line counts the calls that wrote none: its `refusal` ends
with `; <n> more calls with no verified token were refused since the last such line`.

The log rotates at 10 MiB and keeps five rotated files, `audit.log.1` onward. Only the operator
reads it, with `audit tail` from [The operator command](reference.md#the-operator-command).

The log never holds part of a line. Each line is appended in one write. A write that fails part way
cuts the log back to its size before the line. When that cut also fails, the writer stops: every
later call is refused before its token is checked, and writes no line. When the instance starts, it
cuts a log that ends in a fragment back to its last whole line.

A call whose audit line cannot be written is refused, and its result is withheld. So every result an
agent receives has its line. Once a call's cage has started, its action may already have changed
something, such as a pushed branch or an edited pull request. So when the line of an action's call
whose cage started cannot be written, the refusal says that the action ran and may have taken
effect, and that the caller must check what it changed before a retry. That holds whether the cage
exited, timed out, or failed, and when its output passed the cap and the overflow file could not be
written. [Refusals](reference.md#refusals) gives the text.

Each self-check writes one line, with `agent-proxy_self_check` as its action, and with no exit code
and no output sizes, because no single cage's output is the result. The line of a tool call names
the call's token. The operator's command writes one line for each scope it checks, with no token
name. The cages that a check runs write no line of their own.

Each provider run writes one line of its own, with a call ID of its own. Its `action` is the
provider's `<plugin>_<action>`, and its `credential` names the provided credential. Its
`token_name`, `scope`, `arguments`, and `arguments_cut` are `null`. It carries the exit code, the
duration, the stream sizes, and `truncated`, and when the run failed, the failure as `refusal`. A
run whose cage the runner refused or failed, for any reason, has no exit code and no stream sizes.
The call that needed the value writes its own line, as every call does. When a run's line cannot be
written, its value is neither cached nor used, and the call that started the run is refused with the
audit refusal of the run's call ID.

## The self-check

The self-check is one check with two entrances: the MCP tool `agent-proxy_self_check`, which checks
the calling token's scope alone, and the operator's `check`, which checks one scope or every scope
(`core/agent-proxy/agent_proxy/selfcheck.py`). It holds the config in force for the whole check.

It checks the local wiring, the things that can be wrong while the instance looks healthy:

- each stored credential a grant names is in the store, read afresh on each check, and reported by
  name, never by value;
- for each provided credential a grant names, every stored credential its provider takes is stored.
  The entry reports the provided credential by name, and names its provider;
- each root a grant names opens as it opens for a call;
- the overflow directory exists, and the instance can create a file in it;
- each egress proxy that the scope's domain lists need is running, the lists of the provided
  credentials its grants name included;
- for each plugin the scope's grants name, a cage runs the image's `describe` and exits 0. The cage
  goes through the normal path, root verifier and cage cap included, with no roots, no credentials,
  no `env`, and no network, within 30 seconds. The check discards the output. A `describe` whose
  output passes the cap still writes an overflow file in `overflow_dir` on every check, as any
  call's output does, and the sweep removes it, as
  [Output past the cap](reference.md#output-past-the-cap) gives. The instance never pulls for a
  cage, so an image that has left the daemon since the config loaded fails the check. A provider's
  plugin that no grant names gets no such cage.

It leaves out what the config load already refuses, such as an image that does not load, a grant
that names an undeclared action, or a root outside the agent tree. It never tests a credential
against its service, and it never runs a provider.

A check that fails is an entry in the report, never an error. The reference gives the report under
[The self-check report](reference.md#the-self-check-report), and the `check` verb under
[The operator command](reference.md#the-operator-command).

## The instance

The instance image is built by `core/docker/Dockerfile` from `core/agent-proxy/`. Its build stage
installs the package with uv from the lockfile, and its final stage holds only the environment that
stage made. The image's process is `agent-proxy serve` on container port 8000. The image sets
`DOCKER_HOST` to the socket path the run line mounts, `AGENT_PROXY_CONFIG` to
`/etc/agent-proxy/config.yaml`, and `AGENT_PROXY_VERIFIER_IMAGE` to the build argument
`VERIFIER_IMAGE`, which defaults to `agent-proxy-verifier:1`.

At start the instance opens its stores and its audit log, makes the verifier present, reads which
cage limits its daemon enforces, removes the cages that a killed instance left, loads the config and
starts its egress, then serves the control socket and starts the daily sweep of old overflow files.
A step that fails stops the start, and the steps before it are undone, except that the egress
proxies stay for the next start to keep or remove. On SIGINT or SIGTERM the instance stops, and its
egress proxies stay too.

One instance owns its daemon, so every cage on the daemon at start is one that a killed instance
left. The start removes by force each container that carries the cage label, running or stopped,
before the instance serves (`remove_left_cages` in `runner.py`). The start reports each cage it
removes on stderr, by name. A left cage can be a call that the killed instance never finished, and
such a call never got its audit line, so the report keeps a record of it in the instance's log. A
removal that fails gives a warning that names the container, and the start goes on. A container that
is already gone when the start removes it is no failure, and gives neither a report nor a warning. A
listing that fails stops the start.

The start reads the daemon's info, and gives a warning for each cage limit that the daemon cannot
enforce, as [Failures and warnings](reference.md#failures-and-warnings) gives. A daemon that does
not report a limit counts as one that cannot enforce it. An info call that fails stops the start.
`serve` prints the start's reports and warnings, as the `serve` verb under
[Verbs](reference.md#verbs) gives: first the removal reports, then the limit warnings, then the
removal warnings, then the config's own.

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
starts it as it was created, or leaves it running. So a new image, port, or path takes effect only
in a new container.

The state directory holds:

- `tokens.sqlite3`, the token store, with mode `0600`;
- `credentials/`, the credential store, with mode `0700`, each credential file with mode `0600`;
- `audit/`, with mode `0700`, which holds `audit.log` and its rotated files;
- `control.sock`, the control socket, with mode `0600`;
- `egress/`, which holds each egress proxy's config directory, with mode `0700`, and its config file
  in it, with mode `0600`.

The one directory outside the state directory that the instance writes is the config's
`overflow_dir`, in the agent's tree. The instance creates neither it nor the state directory.

`core/docker/provision-instance.sh` sets up everything an instance needs on the host and starts it,
and `core/docker/teardown-instance.sh` removes it, as the deployment guide gives under
[Provisioning](deployment.md#provisioning) and [Teardown](deployment.md#teardown).

## Published images

A release publishes its images on GHCR: the instance image, `ghcr.io/gravital-forge/agent-proxy`;
the verifier image, `ghcr.io/gravital-forge/agent-proxy-verifier`; and
`ghcr.io/gravital-forge/agent-proxy-plugin-<plugin>` for each directory under `plugins/` that holds
a `Dockerfile`. One version covers every image: the repository tag `vX.Y.Z` tags every image
`X.Y.Z`. Images carry version tags only, never `latest`, because a config pins each plugin by
digest. The images are built for `linux/amd64` only; the github plugin's image checks gh's
`linux_amd64` tarball against one SHA-256 (`plugins/github/Dockerfile`).

`core/docker/build-images.sh <version> <registry> <source-url>` builds the images and pushes each
one to the registry, tagged with the version, which is `<major>.<minor>.<patch>`. It finds the
repository from its own path, so it runs from any working directory. It builds in this order:

1. `agent-proxy-verifier` from `core/verifier/`.
2. `agent-proxy`, the instance, from `core/agent-proxy/` with `core/docker/Dockerfile`. The build
   argument `VERIFIER_IMAGE` takes the verifier's digest reference, so the instance image names
   exactly the verifier built with it.
3. `agent-proxy-plugin-<plugin>` from each directory under `plugins/` that holds a `Dockerfile`, in
   name order.

Every build is for `linux/amd64` with no provenance attestation, so each pushed digest names one
image manifest. Every image carries the label `org.opencontainers.image.source`, set to the source
URL, and `org.opencontainers.image.version`, set to the version. After each push, the script prints
the image's digest reference, `<registry>/<name>@sha256:<digest>`, as one line on stdout. The build
and push output goes to stderr, so stdout holds only the image references.

`.github/workflows/release.yml` runs when the project's maintainers publish a GitHub release. Its
one job, `images`, refuses a release marked as a pre-release, and a tag that is not
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
[Pins](../CONTRIBUTING.md#pins) in the contributing guide gives which pins Dependabot updates.

## The isolation model

Each agent user gets its own instance, and each part of the setup below closes one way that the
agent, the instance, or another agent could reach too far. Tests in
`core/agent-proxy/tests/integration/` demonstrate each cage property on a real rootless daemon.

### The service user and its daemon

Each instance runs as a service user, `agentproxy-<agent>`, with its own subordinate uid and gid
range, linger enabled, and its own rootless `dockerd`. The instance container, its egress proxies,
and its cages all run on that daemon. Root only provisions.

- Whoever reaches a Docker socket can start any container with any mount the daemon can make, so the
  socket gives everything the daemon reaches. The agent cannot reach the service user's socket, so
  it cannot run a container that skips its grants.
- A rootless daemon maps container root to the service user. So a compromised instance or cage holds
  `agentproxy-<agent>`, not root, and reaches only what that user reaches: one agent's tree and one
  instance's credentials.
- Running cages under the agent's own uid is rejected. A process of the same uid can read another's
  `/proc/<pid>/environ` and `/proc/<pid>/root`, which would expose a cage's credentials to the
  agent.

### The agent's tree

The agent's tree carries three things, and provisioning applies each:

- A user ACL entry `user:agentproxy-<agent>:rwx`, with a matching default ACL. The instance reaches
  the agent's tree through this entry alone.
- A default entry `default:user:<agent>:rwx`, so the agent keeps access to a file even when the
  file's group goes wrong.
- Setgid with the agent's own group on every directory, so a file a cage creates is grouped to the
  agent. Supplementary groups do not reach a rootless container, so provisioning never adds the
  service user to a group, the agent's group included. Setgid is the only thing that groups cage
  files to the agent, and the instance never relies on a cage to fix a file's group.

Only the agent and `agentproxy-<agent>` may traverse the agent's home. Provisioning gives
`agentproxy-<agent>` traverse (`x`) on the home, and changes nothing else there. It then reads the
home's ACL, and warns about each entry that lets anyone else traverse the home: `other`, a named
user, a named group, and the owning group when that group has a member other than the agent. A group
that provisioning cannot look up counts as having another member. Each warning gives the command
that closes its entry, and provisioning goes on, as [Provisioning](deployment.md#provisioning)
gives. A home of mode `700` draws no warning, and neither does a home of mode `750` whose group
holds the agent alone.

So files the instance creates in the agent's tree are owned by `agentproxy-<agent>`, grouped to the
agent, and read-write for the agent; files the agent creates are read-write for the instance.
Another agent is `other` on every such file, and cannot traverse a home that draws no warning.

### The tree inside the home

The tree's realpath lies strictly inside the realpath of the agent's home, and the service user
gains traverse on each directory between the home and the tree. That keeps the home's guard: the
service user gains traverse alone on the home, so another agent still cannot traverse it. A tree
outside the home loses that guard, and a tree equal to the home would put the service user's ACL on
the whole home. Teardown applies the same rule, because it removes ACL entries recursively, as root,
from the tree it is given.

## Known limits

- A bug in the instance's own code reaches that agent's tree and that instance's credentials.
- Traverse on the agent's home lets `agentproxy-<agent>` open any world-readable file there by path.
- A file created with an explicit restrictive mode, such as a mode-preserving copy of a `0644` file,
  is read-only to the other side; the ACL cannot widen it.
- Every call pays a cage start: a median of about half a second on a rootless daemon, and the root
  verifier adds about a tenth of a second more.
- A bare `agent-proxy check` runs one `describe` cage for each plugin of each scope, one after
  another. Each takes up to 30 seconds plus any wait for a free cage, and the command waits at most
  600 seconds for the whole reply. With about 20 or more scope-and-plugin pairs and slow describes,
  the command can time out while the instance still finishes the check and audits it. `--scope NAME`
  bounds a run to one scope.
- The github plugin's `run_view` with `log_failed` works only under a grant that names each storage
  host, as [Egress](../plugins/github/README.md#egress) in the plugin's README gives.
- Credentials are not encrypted at rest: each is a plain file protected by the service user's uid.
- A call's cage outlives the instance when the instance's process is killed while the call runs. The
  process removes each cage when its call ends, and a stop on SIGINT or SIGTERM waits for the
  running calls to end. These kill the process while a call runs:
  - `docker rm --force` or `docker kill` on the instance container;
  - `docker stop` when a call outlasts Docker's stop grace period, 10 seconds by default, after
    which Docker kills the instance;
  - the kernel's out-of-memory kill of the instance's process;
  - a stop or restart of the service user's Docker daemon, which sends the instance SIGTERM and
    kills it after the same grace period.

  While no instance is up, a cage that has started runs until its action ends or the daemon stops,
  with no timeout, because the instance holds the timeout. It then stays on the daemon as a stopped
  container. A cage stays as a created container when the instance was killed between creating it
  and starting it. The container config of each such cage still holds the value of every credential
  that its grant passes as an environment variable, and `docker inspect` shows it. A file credential
  leaves only its mount. The next start removes every such cage before the instance serves, as
  [The instance](#the-instance) gives. After an out-of-memory kill, or a stop or restart of the
  daemon, the restart policy `unless-stopped` makes that start as soon as the daemon runs. The
  operator finds the cages sooner with the first command below, and removes them with the second.
  The label filter also matches a running instance's cages, so the second command also ends that
  instance's calls. Both commands run on the service user's daemon, as every `docker` command in
  [the deployment guide](deployment.md) does.

  ```sh
  docker ps -a --filter label=agent-proxy.cage
  docker ps -aq --filter label=agent-proxy.cage | xargs -r docker rm --force
  ```

- A token holder can push old lines out of the audit log by making many calls. Each of those calls
  writes a line under its token's name, and the caps of [Audit](#audit) bound each line.
- A cage's log keeps two files of 512 MiB each. A call whose log passes that size returns only the
  last part of its output, with nothing to mark the loss. The cap bounds the log's encoded size, so
  the loss can begin well under 1 GiB of output, most of all for output in short lines, as
  [The cage](#the-cage) gives.
- A cage has no CPU limit, so one call can take every CPU the service user's daemon reaches. A
  rootless daemon enforces a CPU limit only after the host delegates the `cpu` cgroup controller to
  the service user, which provisioning does not do.
- The egress proxy neither masks credentials nor intercepts TLS. It allows or refuses a host by
  name, so a cage can send its credential to any host its grant names.
- A call's output returns when the call ends, never as it streams.
- Each instance has one config file. Configs do not layer, and instances share no config.
- Teardown takes the agent's tree from its `--tree` option, not from the installed config.
  [Teardown](deployment.md#teardown) gives what it leaves when the operator gives it a tree other
  than the one provisioning took.
