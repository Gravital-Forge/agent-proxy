# Writing a plugin

A plugin is an OCI image that declares actions in a manifest. The proxy runs each action in a cage
built from that image. The architecture defines each term under
[Concepts](architecture.md#concepts), and gives the cage under [The cage](architecture.md#the-cage).
This document tells a plugin author what the image holds, how to write its manifest, how an action
runs, and how to check the manifest.

## The image

The image holds an executable named `describe` on its `PATH`. `describe` prints the manifest as
UTF-8 JSON on stdout and exits 0.

The proxy runs `describe` with the image's entrypoint set to `["describe"]` and no command, in a
container with no network, a read-only root filesystem, no capabilities, and `no-new-privileges`,
which it removes whatever happens. So `describe` reads only what the image holds and writes nothing
but its output. It must exit within 30 seconds, and its stdout must stay within 1 MiB. A non-zero
exit shows the first 1,024 bytes of stderr, so write a diagnostic there.

The self-check also runs `describe`, in a cage that the instance builds as
[How an action runs](#how-an-action-runs) gives for an action. The cage starts behind the root
verifier, as user `0:0`, on the Docker network `none`, with no roots, no credentials, and no grant
`env`. So `describe` must also exit 0 there. [The self-check](architecture.md#the-self-check) gives
the check.

The simplest `describe` prints a manifest file that the image holds.

## The manifest

The manifest schema is `core/agent-proxy/agent_proxy/schemas/manifest.schema.json`, a JSON Schema of
draft 2020-12. `parse_manifest` in `core/agent-proxy/agent_proxy/plugins.py` checks the schema, then
the rules the schema cannot state. This section gives both. Each name pattern it cites is a constant
in `core/agent-proxy/agent_proxy/config.py` or in `plugins.py`.

The manifest is a JSON object. No key repeats in one object, and every number is finite. Every
object that the manifest format defines refuses a member it does not name. `input_schema` and
`output_schema` are JSON Schemas, so they take any member that JSON Schema allows.

Each schema holds every schema it refers to, because the proxy fetches nothing. Each `$ref` and
`$dynamicRef` in `input_schema` or `output_schema` must resolve inside that same schema, such as
`#/$defs/item`. The parser refuses a reference to anything else, a remote URL and the meta-schema's
URL included. A reference must also not point at an object or an array inside the value of `const`,
`enum`, `default`, or `examples`, because the parser does not check the references there.

The parser refuses what it cannot check. The manifest and each schema it embeds must nest shallowly
enough for Python's recursion limit, and each integer must stay within Python's digit limit for
converting text to an integer.

The manifest has one member, `actions`. It is an object with at least one member. Each key is an
action name, which matches `ACTION_NAME_PATTERN` in `config.py`. Each value is an action. An
operator grants an action to a scope under the key `<plugin>.<action>`, where `<plugin>` is the name
the config pins the image under. The reference gives the config under
[The config file](reference.md#the-config-file).

### Every action

Every action states each of these members. None has a default.

- `kind` is `"command"` or `"script"`.
- `input_schema` is a JSON Schema whose `type` is `"object"`, and it passes the draft 2020-12
  meta-schema check.
- `credentials` is an array, possibly empty, of the credentials the action expects. Each item is
  `{"env": NAME}`, with `NAME` matching `ENV_NAME_PATTERN` in `config.py`, or `{"file": PATH}`, with
  `PATH` an absolute path beneath `/run/credentials/` with no `..` segment. The reference gives how
  a grant supplies a credential in these forms under
  [A scope's grants](reference.md#a-scopes-grants).
- `timeout` is the action's default timeout, a duration string matching `DURATION_PATTERN` in
  `config.py`, such as `30s` or `5m`, whose count fits a Python `timedelta`. A grant's `timeout`
  overrides it, as [A scope's grants](reference.md#a-scopes-grants) gives.

### A command action

A command action runs a fixed argv prefix, extended from its input. It states these members, and
does not state `executable` or `output_schema`:

- `argv` is the fixed argv prefix, a non-empty array of non-empty strings.
- `parameters` is an ordered array, possibly empty. Each item names one input property with `name`,
  and says how it reaches argv with `as`. `"flag"` requires a `flag` member, a string matching
  `FLAG_PATTERN` in `plugins.py`. `"positional"` forbids `flag`.
- `passthrough_args` is a boolean.

A command action also keeps these rules:

- `input_schema` sets `additionalProperties` to `false`.
- Each parameter's `name` is a property of `input_schema`. No two parameters share a name, and no
  parameter is named `args`.
- No two parameters share a `flag`.
- Each property of `input_schema` is a parameter, or is `args`.
- A flag parameter's property sets `type` to one of `"boolean"`, `"string"`, or `"integer"`. A
  positional parameter's property sets `type` to `"string"` or `"integer"`.
- `passthrough_args` is true exactly when `input_schema` has an `args` property. That property is
  exactly `{"type": "array", "items": {"type": "string"}}`.

Each action whose `passthrough_args` is true draws a warning. The manifest still passes.

### A script action

A script action runs an executable in the image. It states these members, and does not state `argv`,
`parameters`, or `passthrough_args`:

- `executable` is an absolute path in the image.
- `output_schema` is a JSON Schema whose `type` is `"object"`, and it passes the draft 2020-12
  meta-schema check.

[How an action runs](#how-an-action-runs) gives how a script receives its input and returns its
output.

## How argv is formed

A command action's argv is the `argv` prefix, then each parameter in the order `parameters` lists
it, then the items of `args`:

- A boolean flag adds the flag when it is true, and adds nothing when it is false.
- A string or integer flag adds the flag, then the value.
- A positional parameter adds the value.
- An optional property that the input leaves out adds nothing.

An integer value goes into argv as its decimal text. A value that a parameter places in argv must
not begin with `-`, so it cannot pass as an option. A value that does begin with `-` refuses the
call, and no cage starts; a negative integer is such a value. The items of pass-through `args` are
exempt, and go into argv as the input gives them.

## How an action runs

An agent sees each action that its token's scope grants as an MCP tool, with the manifest's
`input_schema` as the tool's input schema. [A scope's grants](reference.md#a-scopes-grants) gives
the tool's name, and [The call path](architecture.md#the-call-path) gives how a call reaches its
cage.

The proxy runs each call in a new cage built from the plugin's image, as
[The cage](architecture.md#the-cage) gives. The cage's entrypoint replaces the image's entrypoint
and command:

- A command action's entrypoint is the argv above.
- A script action's entrypoint is its `executable`, with no arguments. The script reads its input on
  stdin as one compact UTF-8 JSON object, then end of file.

The proxy's root verifier runs first in every cage, and then replaces itself with that entrypoint.
So the action still runs as PID 1, with its argv, stdin, and environment unchanged. A bare
executable name is found on the image's `PATH`. The proxy owns `/.agent-proxy` in the cage, so an
image must not rely on that path. [The root verifier](architecture.md#the-root-verifier) gives the
verifier.

Inside the cage:

- The root filesystem is read-only. Write scratch files under `/tmp`, a small tmpfs.
- The environment holds `AGENT_PROXY_ROOTS`: the grant's root paths, in grant order, as a compact
  JSON array of strings, `[]` for a grant with no roots. The config reserves that name, as
  [A scope's grants](reference.md#a-scopes-grants) gives.
- The environment also holds each variable that the grant's `env` sets.
- The action cannot set the setuid or setgid bit, as
  [The seccomp profile](architecture.md#the-seccomp-profile) gives. A `chmod`, a `mknod`, or a file
  create whose mode holds either bit fails with `EPERM`. That holds on a directory too, and every
  directory of the agent's tree is setgid. So a `chmod` that keeps a directory's setgid bit fails,
  such as GNU `chmod 755 <dir>`, which keeps the bit, or a copy that keeps each mode. A directory
  the action creates still takes setgid from its parent, with no `chmod`. `openat2` fails with
  `ENOSYS`, so an action that calls it must fall back to `openat`.
- The action runs under a process limit and a memory limit, and files under `/tmp` count against the
  memory limit. [The cage](architecture.md#the-cage) gives the limits.

A script's stdout must decode as the manifest must, by the rules under
[The manifest](#the-manifest). The reference gives when a script succeeds and what its call returns
under [A script action's result](reference.md#a-script-actions-result), and what an output over the
cap does to a script under [Output past the cap](reference.md#output-past-the-cap).

The cage's network follows the grant's `domains`, as [Egress](architecture.md#egress) gives.

- The cage reaches the web only through the proxy that `HTTPS_PROXY` names, so use a tool that
  honours that variable. The proxy passes each host the grant names and refuses a host the grant
  does not name.
- The cage gets no proxy variable for plain HTTP.
- The config reserves the name `HTTPS_PROXY`, as [A scope's grants](reference.md#a-scopes-grants)
  gives.

## Checking a manifest

Build the image, then check it from `core/agent-proxy/`:

```sh
uv run agent-proxy manifest check IMAGE
```

`IMAGE` is any local image reference; build or pull it first. The reference gives how the command
reaches Docker, what it prints, and how it exits under
[The operator command](reference.md#the-operator-command).

## A working example

`core/agent-proxy/tests/test-plugin/` is a working plugin. Its `Dockerfile` copies a `manifest.json`
into the image, and a `describe` script onto the `PATH` that prints it. The manifest shows both
action kinds. Among its actions are a command action with a flag and a positional parameter, and a
script action whose executable the image also holds. `COPY` keeps each file's mode, so an executable
must already be executable in the build context.

`plugins/git/` is a full plugin: a uv package with its own tests, whose actions run git in the cage.
Its [README](../plugins/git/README.md) gives what it offers and how it is built. `plugins/github/`
is also a full plugin, whose actions run gh in a cage, and its [README](../plugins/github/README.md)
gives what it offers. `plugins/hcp-terraform/` is a full plugin whose script actions call HCP
Terraform's API with Python's standard library, and its [README](../plugins/hcp-terraform/README.md)
gives what it offers.
