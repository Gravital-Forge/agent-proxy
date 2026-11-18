# agent-proxy design

This is the operator's closed design for agent-proxy. Who may change this document, and how, is set
in `.dev/documentation.md`.

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

A token kept as a file inside a repository is held by any reader of that repository, and it cannot
be narrowed to particular operations. A tool surface hardcoded to git and GitHub serves nothing
else. A git that runs repository-controlled code (hooks, `.git/config` drivers) gives that code the
proxy's full reach. agent-proxy answers with minted, policy-bound tokens, a plugin surface for any
credentialed tool, and a per-action cage that bounds what executed code can touch.

## Concepts

These terms carry one meaning each through the plan, the code, and the docs.

- **Instance** — one running agent-proxy serving exactly one agent user. Every agent gets its own
  instance; nothing is shared between instances.
- **Scope** — a named policy in the instance's config file (for example `dev-agent`): the complete
  list of grants a token holding it may use.
- **Token** — a bearer secret that names exactly one scope. It carries no policy of its own.
- **Grant** — one entry in a scope: an action, plus that action's roots, domains, and credentials,
  an optional timeout override, and an optional `env`.
- **Action** — one operation a plugin offers, of kind `command` or `script`.
- **Plugin** — an OCI image, pinned by digest, whose manifest declares its actions.
- **Manifest** — the JSON a plugin image's `describe` entrypoint prints.
- **Cage** — the short-lived container one action call runs in, built from its grant: only the
  grant's roots mounted, only the grant's domains reachable, only the grant's credentials present.
- **Egress proxy** — an iron-proxy container serving one distinct domain list.

## Surface

The instance is an MCP server over Streamable HTTP, published on a loopback port unique to the
instance. The agent's MCP client sends the token as `Authorization: Bearer <token>`. `tools/list`
returns the actions the token's scope grants, and the self-check tool. MCP tool names are
`<plugin>_<action>`.

The self-check is one check with two entrances: the MCP tool `agent-proxy_self_check`, and the
operator command `agent-proxy check [--scope NAME]` through `docker exec`, which runs it for every
scope or for one.

- The tool takes no input. Every token that verifies lists it, with no grant, and it reports on that
  token's scope alone.
- It checks the local wiring, the things that can be wrong while the instance looks healthy:
  - each credential a grant names is stored, reported by name, never by value;
  - each root a grant names exists, and the instance opens it;
  - the overflow directory exists and is writable;
  - each egress proxy that the scope's domain lists need is running;
  - one cage per plugin the scope uses starts through the normal runner path, verifier included,
    with no roots and no network.
- It leaves out what the config load already refuses, such as an image that does not load, a grant
  that names an undeclared action, or a root outside the agent tree. It never tests a credential
  against its service.
- The result is one object: an overall `ok`, and one entry per check that names what it checked,
  whether it passed, and a detail. A failed check is a normal result, not a refusal. The command
  prints one row per entry, and exits 1 when any check fails.

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
- A grant may set `env`, a map of plain environment variables that every cage under the grant
  receives. Its values are settings, not secrets: they live in the config file, and the credential
  store holds none of them.
- Validation checks: every grant names an action a pinned plugin's manifest declares; every grant
  states `roots`, `domains`, and `credentials` explicitly, empty values included — an omitted field
  is an error; every root lies inside the instance's agent tree; every plugin image is present,
  pulled at reload by digest; every `env` name begins with an upper-case letter or `_`, holds only
  upper-case letters, digits, and `_`, and is neither `HTTPS_PROXY`, `AGENT_PROXY_ROOTS`, nor an env
  name that a credential of the same grant takes; no `env` value holds a NUL; every action declaring
  pass-through `args` is reported as a warning; every credential a grant names that the credential
  store lacks is reported as a warning at start, at `reload`, and at `token mint`, so the instance
  starts and tokens mint before the operator stores it.
- The config refuses a plugin named `agent-proxy`, so no `<plugin>_<action>` tool name can take the
  self-check tool's name.
- There are no scope-level defaults and no layering between files. Repetition across grants and
  across agents is accepted.

A grant, for illustration:

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

## Credentials

- Intake is admin-only: `credential set <name>` reads the value from stdin, so it never appears in
  process arguments or shell history. `credential list` shows names and set-times;
  `credential rm <name>` deletes.
- A credential is an opaque named blob stored as a file in the instance's credential volume,
  readable only by the instance's service user.
- A grant maps each credential it uses to an environment variable (`{env: NAME}`, the default form)
  or to a file path inside the cage (`{file: /run/credentials/<name>}`) for tools that only read
  files. The cage receives only its grant's credentials.
- A call under a grant whose credential is not set is refused before a cage starts, and audited like
  any other refusal. The refusal names the credential and the command the operator stores it with:

  ```text
  agent-proxy refused: credential 'github-token' is not set; the operator stores it with 'agent-proxy credential set github-token'
  ```

  The grant's tools stay listed for the token. A credential that the operator stores with
  `credential set` takes effect on the next call, with no reload.
- At rest, credentials are plain files protected by uid.

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
- The image-based plugin form is settled: the git and github plugins are built on it.
- The github plugin's `pr_create` and `pr_edit` upload image and video attachments through gh's
  `--attach`, each file confined to the grant's roots. The grant setting `GITHUB_PR_ATTACHMENTS`
  governs them: they are on when a grant leaves it unset or sets it `on`, and refused under any
  other value. A grant that allows them adds a root that holds the files, and `uploads.github.com`.
  The plugin's image takes gh from its release tarball, pinned by its SHA-256, because Alpine's
  `github-cli` package lacks `--attach`. `plugins/github/README.md` gives the detail under
  "Attachments".
- The hcp-terraform plugin only reads: its actions read HCP Terraform runs and their logs for the
  workspaces a grant lists, and send nothing but GETs. Its tool surface is the whole read-only
  limit, because every token an HCP Terraform Free organization mints is broad.
  `plugins/hcp-terraform/README.md` gives why, its actions, its workspace list, and where the token
  goes.

## The cage

Every action call runs in a fresh sibling container on the instance's rootless Docker daemon, never
inside the proxy process.

- **Files:** each root in the grant is bind-mounted at its own host path with the grant's mode.
  Same-path mounting keeps git worktrees working. Nothing else of the host is visible. The cage's
  environment names those paths in `AGENT_PROXY_ROOTS`, a JSON array, so a plugin acts on exactly
  its roots without inferring them; no credential may take that name. Files the agent owns are not
  owned inside the cage's user namespace, so the cage writes them through the ACL but cannot change
  their mode or owner.
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
    iron-proxy accepts such a rule and ignores it, which widens access.
  - iron-proxy is pinned by digest. An upgrade runs `test_iron_proxy_ignores_a_rules_source_ip`
    first.
  - Config: the core bind-mounts a config directory, never a single file, because a replaced file
    under a single-file mount keeps its old inode. It sets `dns.proxy_ip`, which iron-proxy requires
    while its DNS listener is enabled, and passes `-config` on every run, because the image's
    entrypoint has no default arguments. It sets `tls.mode: "sni-only"` with no CA, so TLS passes
    through by SNI and is never intercepted. The allowlist is
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

Each self-check writes one line, with `agent-proxy_self_check` as its action, and with no exit code
and no output sizes. The line of a tool call names the call's token. The command writes one line for
each scope it checks, with no token name. The cages that a check runs write no line of their own.

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
  `-c safe.directory=<root>` and `-c safe.directory=<root>/*` on the command line for each root
  (`S7`): the first trusts a repository whose top level is the root, and the second every repository
  beneath it, linked worktrees included. The check this turns off guards against configuration a
  repository plants, and git in a cage reads none: the config isolation below gives why.
- `gh` reads `GH_TOKEN` as it is. git does not, so the git plugin supplies `GH_TOKEN` through its
  own credential helper. Every git run clears every inherited helper, then names the plugin's, with
  `credential.useHttpPath=true`, so each request carries the path of the URL git connects to. The
  helper answers with `GH_TOKEN` only when that path names a repository the grant's `GITHUB_REPOS`
  lists, and otherwise answers `quit=1`, so git stops. The helper checks that path, and not the
  remote a repository's config names, because git builds the URL after `remote.<name>.pushurl`
  rewrites it: a rewritten remote cannot carry the token to an unlisted repository.
  `plugins/git/README.md` gives the helper's rule and its refusals.
- The git plugin passes `-c core.hooksPath=/dev/null` on every git run, so no hook runs in a cage. A
  hook planted in a repository would run with the cage's credential and domains: the `push` cage
  holds `GH_TOKEN` and reaches GitHub. A value on the command line outranks the repository's config,
  so the repository cannot turn hooks back on.
- git in a cage never takes its settings from a repository's own config. The agent writes
  `.git/config` with its file tools, and a key there can name a program for git to run, such as a
  filter driver or `core.fsmonitor`. That program would run with the cage's credential. So every git
  action, in a cage with a credential or without one, runs with `GIT_COMMON_DIR` set to a scratch
  directory in the cage's tmpfs. `GIT_DIR`, the objects, and the refs stay the real ones. The refs
  stay real through `extensions.refStorage = files://<path>`, which needs git 2.54 or later, and a
  test runs it on the image's git. The global and system config are off. The plugin builds the
  scratch config and never filters the agent's: it holds a fixed set of keys the plugin supplies,
  and the remote and branch keys that carry a remote's URLs and refspec and a branch's upstream,
  copied from the real config as data. A key the plugin does not name never reaches git. The
  plugin's code fixes the copied set, and no instance config changes it.
- Every git run also denies the `file`, `ext`, and `fd` transports. `ext` runs a program in the
  cage, and `fd` talks to a file descriptor in place of a remote. `file` starts a local
  `upload-pack` or `receive-pack` without the plugin's settings, so the target repository's own
  config and hooks would run. Network transports stay allowed, because the egress proxy bounds where
  they go. These pins govern git's transports only: an `lfs.url` in a `.lfsconfig` can name a local
  path, which git LFS serves with its built-in standalone file adapter, and that adapter runs no
  program.
- Commit identity comes from the grant's env alone, for every action that creates a commit. The
  plugin refuses a repository whose shape the scratch directory cannot reproduce.
  `plugins/git/README.md` gives the scratch config and the transport pins under "Config isolation",
  the identity rule under "Commit identity", and each refused shape under "Repository shapes".

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
│   │   ├── agent_proxy/  # cli, config, auth, credentials, plugins, reload, mcp, selfcheck, runner, egress, results, audit, errors, schemas
│   │   └── tests/        # unit/, integration/
│   ├── docker/           # the proxy's Dockerfile and provisioning; the iron-proxy pin is in egress
│   └── verifier/         # the in-cage root verifier: Go source, its tests, and the Dockerfile that builds it
└── plugins/
    ├── git/              # Dockerfile, describe and the manifest it prints, scripts/, tests/, README.md
    ├── github/           # Dockerfile, describe, manifest.json, scripts/, tests/ with a stub gh and a fake API, README.md
    └── hcp-terraform/    # Dockerfile, describe, manifest.json, scripts/, tests/ with a fake API, README.md
```

The core is Python on uv and FastMCP. One part of the core is Go: the in-cage root verifier, built
with `CGO_ENABLED=0` into a static executable, so it runs in any image with no libc or runtime in
the cage. The Go toolchain is a build dependency for that verifier alone. Plugins may be written in
any language. CI builds each plugin image and runs `manifest check` on it. Publishing images to a
registry is deployment, and out of scope.

## Verification

### Project checks

These run on the agent's own rootless Docker daemon, with no root. `.dev/verification.md` sets the
bars that run them.

- Core unit tests with Docker mocked; integration tests against a real rootless daemon that
  demonstrate each cage property: an unmounted path is absent, an unlisted domain is unreachable, an
  ungranted credential is absent, a direct connection to an outside address fails, and a cage cannot
  change the mode of a file its user namespace does not own.
- A transport test that starts the real server and drives it with an MCP client.
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
- On the service user's daemon, the cage's root filesystem refuses a write, `/tmp` takes one, the
  cage holds no effective capability, and `no_new_privs` is set. The cage cost is measured there.
- `E12` passes with a real credential: git over HTTPS, through the credential helper, reaches a
  private repository.
- With a real credential, the github plugin's `run_view` with `log_failed` returns a failed run's
  log under a grant that names the storage host GitHub redirects the fetch to.
- With a real credential and grants whose `GITHUB_REPOS` names one repository, the git plugin's
  `push` and the github plugin's `pr_view` succeed on that repository, and each is refused on a
  repository the token reaches that the list does not name.
- With a real credential and `GITHUB_PR_ATTACHMENTS` on, the github plugin's `pr_edit` attaches a
  PNG and an H.264 MP4 to a scratch pull request in a private organization repository, and both
  render for the operator on the pull request's page. The agent writes both files itself, in a
  worktree below the grant's root, so the check also shows that the cage reads the agent's own files
  there. With the setting `off`, the same call is refused.
- With a real token and a grant whose `HCP_TERRAFORM_WORKSPACES` names one workspace, the
  hcp-terraform plugin's `run_list` and `plan_log` succeed on that workspace, and each is refused on
  another workspace the token reaches.

The `S` checks need no counterpart on the agent's daemon: in a deployment, cages never run there.

## Out of scope

- Token self-service: agents cannot mint, narrow, or refresh tokens.
- Streaming output as MCP progress notifications.
- Config layering or shared config across instances.
- Encryption of credentials at rest.
- Credential masking or TLS interception in the egress proxy.
- Deployment. The operator, on 2026-09-30: "deployment is out of scope. Focus on implementation".
  The project builds the application and its deployment artifacts, and runs neither on a host.
  Provisioning a host, running the deployment acceptance checks, configuring an instance for a real
  agent, and switching the harness to agent-proxy come after the project.

## Known gaps

- A bug in the instance's own code reaches that agent's tree and that instance's credentials.
- Traverse on the agent's home lets `agentproxy-<agent>` open any world-readable file there by path.
- A file created with an explicit restrictive mode (for example a mode-preserving copy of a `0644`
  file) is read-only to the other side; the ACL cannot widen it.
- Every call pays a cage start: a median of about half a second on the agent's daemon (`C2`), and
  the in-cage root verifier adds about a tenth of a second more.
- A run's log through `run_view` with `log_failed` is out of reach under a grant that names only
  GitHub's hosts. gh follows GitHub's redirect to a storage host GitHub picks, and a domain takes no
  wildcard, so a grant that wants such a log names each storage host. Under a grant that names only
  `api.github.com`, `pr_checks` and `job_log_url` return a failed job's signed log URL instead, and
  the agent fetches the log itself. `plugins/github/README.md` gives the detail.

## Decision directives

The fork walk applies these in order.

1. A fork that widens what a token, a cage, or the instance can reach loses to one that does not.
2. Explicit over defaulted: a value a grant or manifest could state is stated, never inferred.
3. What the design does not name is not built.
4. A claim about Docker, iron-proxy, git, or `gh` behavior that a decision rests on gets a test that
   runs it on the pinned version.
5. A new vendor, credential, or runtime dependency outside this design goes to the operator.
