# agent-proxy design

This is the operator's closed design for the agent-proxy proof of concept, revised after the spike
that `spike/FINDINGS.md` records. The Plan job prices its consequences and does not reopen it. Who
may change this document, and how, is set in `.dev/documentation.md`.

## The ask

The operator's opening request:

> i want to build a new project - general purpose agent proxy. Similar to the git sidecar but more
> customization and really for any kind of proxy. No sidecar-token, that was a bad idea, rather you
> mint an Auth token with specific scopes you select. Scopes are configurable, they bind credential
> to action where action is a script or cli command. Thisd run in a similar pattern as the sidecar -
> on the same machine as a root user so agent user can't access it. Optional volume mounts - yes, so
> that git and github ops work.
>
> After the core is ready, we add specific things as plugins. E.g. Git and github plugin and you
> pass the github token and ssh key as credentials for those. You then either allow list commands
> with pass through arguments or define scripts where each script would be a command with some
> filtering or special logic or maybe the plugin is a python package or some other script-y package
> that follows a predefined schema or exposes schema-listing commands.

Lines from the grilling that sharpened it:

> The new idea is that the admin defines a list of commands available upfront. The agent still can't
> execute arbitrary.
>
> You shouldn't list "gh" it should be more granular than that.
>
> If we can bind operation to which files it can see per token - that'd be great. Not just paths,
> maybe broader through landlock or nono? This would become the ultimate cage - each scope can
> configure a command plus which files it can touch plus which domains it can hit.
>
> the proxy may change files for the agent user so agent user needs to be able to see and read and
> write those files. Please also consider multiple agent users on one machine and I don't want them
> seeing each other's files
>
> Let's do separate, one instance per agent.
>
> the config file describes e..g. A "dev-agent" scope which sets up what this scope can do. I then
> as admin mint a "dev-agent" token. Point being - tokens are not assigned all the scope on minting.
> Scope "policy" is defined in advance and assigned to tokens.
>
> Per action. Maybe even without the defaults. Longer config and repetition but agents write that
> and it's explicit and clean to check per-op
>
> Let's not dump the first 256 kib if the output goes into the file. Let the agent decide how to
> read it.

## Why

git-sidecar authorizes by a `.git-sidecar-token` file inside each repository: any reader of the
repository holds the token, and the token cannot be narrowed to particular operations. Its tool
surface is hardcoded to git and GitHub, and the git it runs executes repository-controlled code
(hooks, `.git/config` drivers) with the sidecar's full reach. agent-proxy replaces it with minted,
policy-bound tokens, a plugin surface for any credentialed tool, and a per-action cage that bounds
what executed code can touch.

## Concepts

These terms carry one meaning each through the plan, the code, and the docs.

- **Instance** — one running agent-proxy serving exactly one agent user. Every agent gets its own
  instance; nothing is shared between instances.
- **Scope** — a named policy in the instance's config file (for example `dev-agent`): the complete
  list of grants a token holding it may use.
- **Token** — a bearer secret that names exactly one scope. It carries no policy of its own.
- **Grant** — one entry in a scope: an action, plus that action's roots, domains, credentials, and
  optional timeout override.
- **Action** — one operation a plugin offers, of kind `command` or `script`.
- **Plugin** — an OCI image, pinned by digest, whose manifest declares its actions.
- **Manifest** — the JSON a plugin image's `describe` entrypoint prints.
- **Cage** — the short-lived container one action call runs in, built from its grant: only the
  grant's roots mounted, only the grant's domains reachable, only the grant's credentials present.
- **Egress proxy** — an iron-proxy container serving one distinct domain list.

## Surface

The instance is an MCP server over Streamable HTTP, published on a loopback port unique to the
instance. The agent's MCP client sends the token as `Authorization: Bearer <token>`. `tools/list`
returns only the actions the token's scope grants. MCP tool names are `<plugin>_<action>`.

## Tokens

- Format: `apx_` followed by 256 random bits, URL-safe encoded. The prefix lets secret scanners
  recognize a leaked token.
- The instance stores the SHA-256 of each token with its name, scope, created time, last-used time,
  and optional expiry. Verification is a constant-time comparison of hashes.
- Tokens never expire unless minted with `--expires`.
- Only the admin manages tokens, through `docker exec` into the instance container:
  `token mint --name <name> --scope <scope> [--expires <duration>]` prints the token once and
  refuses a name already in use; `token list` shows metadata and never the token;
  `token revoke <name>` takes effect on the next call. Rotation is revoke then mint.
- The admin delivers the token by writing it into the agent's MCP client config as the
  `Authorization` header.
- Editing a scope changes the reach of every token naming it, from the next reload.

## Config

- One YAML file per instance, kept in a separate config repository (`agent-proxy-config`). Agents
  draft changes as pull requests; the operator reviews and merges; the admin installs the file
  root-owned on the host and bind-mounts it read-only into the instance.
- `agent-proxy reload` (through `docker exec`) validates the file and swaps it in atomically. A file
  that fails validation is refused and the running config stays. Calls already running finish under
  the config they started with. Reload starts every egress proxy the new config needs before the
  swap, and removes a proxy no grant names once no cage runs on its network.
- Validation checks: every grant names an action a pinned plugin's manifest declares; every grant
  states `roots`, `domains`, and `credentials` explicitly, empty values included — an omitted field
  is an error; every credential a grant names exists in the credential store; every root lies inside
  the instance's agent tree; every plugin image is present, pulled at reload by digest; every action
  declaring pass-through `args` is reported as a warning.
- There are no scope-level defaults and no layering between files. Repetition across grants and
  across agents is accepted.

A grant, for illustration:

```yaml
scopes:
  dev-agent:
    grants:
      git.status:
        roots: [{path: /home/agent-01/Projects, mode: rw}]
        domains: []
        credentials: {}
      github.pr_create:
        roots: []
        domains: [api.github.com]
        credentials: {github-token: {env: GH_TOKEN}}
        timeout: 120s
```

## Credentials

- Intake is admin-only: `credential set <name>` reads the value from stdin, so it never appears in
  process arguments or shell history. `credential list` shows names and set-times;
  `credential rm <name>` deletes.
- A credential is an opaque named blob stored as a file in the instance's credential volume,
  readable only by the instance's service user.
- A grant maps each credential it uses to an environment variable (`{env: NAME}`, the default form)
  or to a file path inside the cage (`{file: /run/credentials/<name>}`) for tools that only read
  files. The cage receives only its grant's credentials.
- At rest, credentials are plain files protected by uid, as in git-sidecar.

## Plugins and actions

- A plugin is an OCI image whose `describe` entrypoint prints its manifest. The manifest declares
  each action's name, kind, input JSON Schema (which becomes the MCP tool's input schema), output
  schema for `script` actions, credentials it expects, and default timeout.
- **`command`** actions: a fixed argv prefix at subcommand granularity (`gh pr create`, never bare
  `gh`), with typed parameters mapped to positions or flags and validated by the schema. A trailing
  pass-through `args: string[]` parameter is allowed only when the manifest declares it explicitly.
- **`script`** actions: an executable in the image receiving the validated input as JSON on stdin
  and printing JSON on stdout. Filtering and special logic live here.
- The manifest schema ships inside core as package data. `agent-proxy manifest check <image>` runs
  an image's `describe` and validates the output; each plugin's CI runs it.
- Plugins carry no path metadata. The cage is the only file boundary: a path outside the grant's
  roots does not exist inside the container, and the tool fails with its own error.
- A plugin's source is a uv package under `plugins/`, so one CI job covers it. The executables in
  its image may be written in any language.
- The image-based plugin form is provisional: it is reviewed once the git and github plugins are
  built.

## The cage

Every action call runs in a fresh sibling container on the instance's rootless Docker daemon, never
inside the proxy process.

- **Files:** each root in the grant is bind-mounted at its own host path with the grant's mode.
  Same-path mounting keeps git worktrees working. Nothing else of the host is visible. Files the
  agent owns are not owned inside the cage's user namespace, so the cage writes them through the ACL
  but cannot change their mode or owner.
- **Network:** the instance runs one egress proxy per distinct domain list across its grants.
  - An egress proxy is immutable once started and is named by a hash of the sorted domain list. It
    sits on its own `--internal` Docker network under the network alias `egress`, and every egress
    proxy also joins one shared egress bridge (`E1` to `E4`, `E7a`, `E7b`).
  - A cage joins its grant's network and gets `HTTPS_PROXY=http://egress:1080`. A process that
    ignores that variable has no route out. A grant with `domains: []` gets an internal network with
    no proxy, and so no egress at all (`E6a`).
  - A cage reaches only the proxy's tunnel listener, `proxy.tunnel_listen`, where CONNECT requests
    arrive. Every other listener the proxy binds — DNS, plain HTTP, TLS, and management — is
    unreachable from the internal network or enforces the same allowlist.
  - The core never uses iron-proxy's management reload and never writes a `source_ip` rule:
    iron-proxy accepts such a rule and ignores it, which widens access (`E9`).
  - iron-proxy is pinned by digest. An upgrade repeats `E9` first.
  - Config: the core bind-mounts a config directory, never a single file, because a replaced file
    under a single-file mount keeps its old inode. It sets `dns.proxy_ip`, which iron-proxy requires
    even without DNS interception, and passes `-config` on every run, because the image's entrypoint
    has no default arguments. It sets `tls.mode: "sni-only"` with no CA, so TLS passes through by
    SNI and is never intercepted. The allowlist is
    `transforms: [{name: allowlist, config: {domains: [...]}}]`. The core validates a proxy's config
    before it starts that proxy, so a config iron-proxy refuses (`E8d`) never sits on disk behind a
    running proxy.
  - Egress tests assert the proxy's refusal, never curl's exit code, which changes between curl
    versions. They use curl, never busybox `wget`, which ignores `HTTPS_PROXY`.
  - iron-proxy rejects non-HTTP traffic, so git reaches GitHub over HTTPS with the token, not over
    SSH.
- **Hardening:** container root (mapped by rootless Docker to the instance's service user), all
  capabilities dropped, `no-new-privileges`, read-only root filesystem, a bounded tmpfs for scratch.
- **Concurrency:** a per-instance cap on simultaneously running cages; calls over the cap wait.
- **Timeout:** the manifest's default, overridable per grant. On timeout the cage is killed and
  removed.

## Results

- A `command` action returns `{exit_code, stdout, stderr}`; a non-zero exit is a normal result.
- A `script` action returns the JSON it printed as the tool's structured content.
- MCP errors are reserved for the proxy's own refusals — bad token, ungranted action, input failing
  the schema, timeout, cage failing to start — each with a message saying the proxy refused.
- When stdout or stderr exceeds the cap (256 KiB each), the proxy writes that full stream to a file
  in the instance's overflow directory and returns only the pointer and size in its place, for
  example `{truncated: true, stdout_file: "<dir>/<call-id>.stdout", stdout_bytes: 4194304}` — no
  head of the stream. The overflow directory is a host path in the agent's tree set in the instance
  config; the proxy writes it, cages never mount it. Files older than 7 days are deleted at startup
  and daily.

## Audit

One JSON line per call in the instance's volume: timestamp, call ID, token name, scope, action, full
input arguments, exit code, duration, output sizes, truncation, and the refusal reason for refused
calls. Credential values and output contents are never recorded. The log rotates by size. Only the
admin reads it, with `agent-proxy audit tail` through `docker exec`.

## Deployment and isolation

One instance per agent user:

- A service user `agentproxy-<agent>` with its own subuid/subgid range, linger enabled, and its own
  rootless `dockerd`. The instance container, its egress proxies, and its cages all run on that
  daemon. The agent cannot reach the daemon's socket, and compromising the instance yields
  `agentproxy-<agent>`, not root. Root only provisions.
- The agent's `Projects` tree carries: a user ACL entry `user:agentproxy-<agent>:rwx` with a
  matching default ACL; `default:user:<agent>:rwx`, so the agent keeps access even when a file's
  group goes wrong; setgid with the agent's own group. The agent's home stays mode `700` and gains
  only traverse (`x`) for `agentproxy-<agent>`.
- The instance reaches the agent's tree through the `user:agentproxy-<agent>` ACL entry alone.
  Supplementary groups do not reach a rootless container (`S4`), so provisioning never adds the
  service user to a group, the agent's group included. Setgid on every root is the only thing that
  groups cage files to the agent (`S8`): provisioning sets it on every root, and the core never
  relies on a cage to fix a file's group.
- Files the instance creates in the agent's tree are owned by `agentproxy-<agent>`, grouped to the
  agent, and read-write for the agent; files the agent creates are read-write for the instance.
  Another agent is `other` on every such file and cannot traverse the home.
- Running cages under the agent's own uid is rejected: a same-uid process can read another's
  `/proc/<pid>/environ` and `/proc/<pid>/root`, which would expose the cage's credentials to the
  agent.
- Provisioning is an idempotent script,
  `core/docker/provision-instance.sh <agent> <port> <config-file>`, run with sudo. It creates the
  service user, sets up its rootless daemon, applies the ACLs, creates the overflow directory, and
  starts the instance. Provisioning stops at a started instance: the instance starts its egress
  proxies itself. It stops before minting, so the admin sees the token printed.
  `docs/runbooks/per-agent-instance.md` explains each step so the admin can run or debug it by hand.

## The git plugin's constraints

- The cage process is uid 0 in its user namespace, and the agent's repositories belong to a uid that
  namespace does not map, so git refuses them as dubious ownership (`S6`). The git plugin passes
  `-c safe.directory=<root>` on the command line for each root (`S7`). The check this turns off
  guards against configuration a repository plants; inside the cage that risk is the known gap about
  planted code, and the cage bounds it.
- `gh` reads `GH_TOKEN` as it is. git does not, so the git plugin supplies `GH_TOKEN` through a
  credential helper in the form `E12` used, which clears every inherited helper before it adds its
  own:

```text
-c credential.helper= -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
```

## Repository layout

A monorepo with no root `pyproject.toml`; every directory carrying a `pyproject.toml` is its own uv
package with its tests inside it.

```text
agent-proxy/
├── README.md
├── docs/                 # architecture, writing-a-plugin, runbooks/
├── examples/             # a sample instance config
├── core/
│   ├── agent-proxy/      # the core uv package (Python, FastMCP, Docker SDK)
│   │   ├── agent_proxy/  # cli, config, auth, plugins, mcp, runner, egress, results, audit, schemas
│   │   └── tests/        # unit/, integration/
│   └── docker/           # the proxy's Dockerfile, iron-proxy pin and base config, provisioning
└── plugins/
    ├── git/              # Dockerfile, manifest.yaml, scripts/, tests/, README.md
    └── github/
```

The core is Python on uv and FastMCP, reusing git-sidecar's auth, transport, and test patterns.
Plugins may be written in any language. CI builds each plugin image and runs `manifest check` on it.
Publishing images to a registry is deployment, and out of scope.

## Verification

### Project checks

These run on the agent's own rootless Docker daemon, with no root. `.dev/verification.md` sets the
bars that run them.

- Core unit tests with Docker mocked; integration tests against a real rootless daemon that
  demonstrate each cage property: an unmounted path is absent, an unlisted domain is unreachable, an
  ungranted credential is absent, a direct connection to an outside address fails, and a cage cannot
  change the mode of a file its user namespace does not own.
- A transport test that starts the real server and drives it with an MCP client, as git-sidecar's
  `test_transport.py` does.
- Every plugin passes `agent-proxy manifest check` in CI, plus its own tests against fixture
  repositories.

### Deployment acceptance checks

`.dev/verification.md` defines a deployment acceptance check and how the project handles one. These
are the design's, and `docs/runbooks/per-agent-instance.md` carries them as steps.

- The provisioning script is run twice on a clean host with no change on the second run.
- A cage on the service user's daemon writes the agent's tree with the owner, group, and mode that
  the Deployment and isolation section gives.
- A second agent cannot read created files. With traverse granted on the agent's home, it still
  cannot read a cage-made file, which separates the file's own barrier from the home's (`S11`).
- The agent cannot reach the service user's Docker socket.
- `H1` to `H4` hold on the service user's daemon, and the cage cost that `C1` and `C2` measure is
  measured there.
- `E12` passes with a real credential: git over HTTPS, through the credential helper, reaches a
  private repository.

The `S` checks need no counterpart on the agent's daemon: in a deployment, cages never run there.

## Out of scope

- git-sidecar itself, including the credential exposure of its per-agent same-uid deployment.
- Token self-service: agents cannot mint, narrow, or refresh tokens.
- Streaming output as MCP progress notifications.
- Config layering or shared config across instances.
- Encryption of credentials at rest.
- Credential masking or TLS interception in the egress proxy.
- Deployment. The operator, on 2026-09-30: "deployment is out of scope. Focus on implementation".
  The project builds the application and its deployment artifacts, and runs neither on a host.
  Provisioning a host, running the deployment acceptance checks, configuring an instance for a real
  agent, switching the harness to agent-proxy, and retiring git-sidecar come after the project.

## Known gaps

- A bug in the instance's own code reaches that agent's tree and that instance's credentials.
- Code planted in a repository and executed inside a cage can read that cage's credential; the
  domain allowlist bounds where it can send it, not whether it can read it.
- Traverse on the agent's home lets `agentproxy-<agent>` open any world-readable file there by path.
- A file created with an explicit restrictive mode (for example a mode-preserving copy of a `0644`
  file) is read-only to the other side; the ACL cannot widen it.
- Every call pays a cage start: a median of about half a second on the agent's daemon (`C2`).

## Decision directives

The fork walk applies these in order.

1. A fork that widens what a token, a cage, or the instance can reach loses to one that does not.
2. Explicit over defaulted: a value a grant or manifest could state is stated, never inferred.
3. What the design does not name is not built.
4. A claim about Docker, iron-proxy, git, or `gh` behavior that a decision rests on gets a test that
   runs it on the pinned version.
5. git-sidecar's auth, transport, and test patterns are reused where this design does not differ.
6. A new vendor, credential, or runtime dependency outside this design goes to the operator.
