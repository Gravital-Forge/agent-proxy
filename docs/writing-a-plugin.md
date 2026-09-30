# Writing a plugin

A plugin is an OCI image that declares actions in a manifest. The proxy runs each action in a cage
built from that image. `docs/design.md` gives the plugin model under "Plugins and actions" and the
cage under "The cage". This document tells a plugin author what the image holds, how to write its
manifest, and how to check it.

## The image

The image holds an executable named `describe` on its `PATH`. `describe` prints the manifest as
UTF-8 JSON on stdout and exits 0.

The proxy runs `describe` with the image's entrypoint set to `["describe"]` and no command. The
container has no network, a read-only root filesystem, no capabilities, and `no-new-privileges`. So
`describe` reads only what the image holds and writes nothing but its output. `describe` in
`core/agent-proxy/agent_proxy/plugins.py` sets these limits:

- `DESCRIBE_TIMEOUT_SECONDS` bounds how long `describe` runs.
- `DESCRIBE_STDOUT_LIMIT` bounds how many bytes it prints on stdout.
- A non-zero exit is a refusal that shows the start of stderr, so write a diagnostic there.

The simplest `describe` prints a manifest file that the image holds. `docs/design.md` gives where a
plugin's source lives under "Plugins and actions" and "Repository layout".

## The manifest

The manifest schema is `core/agent-proxy/agent_proxy/schemas/manifest.schema.json`, a JSON Schema of
draft 2020-12. `parse_manifest` in `core/agent-proxy/agent_proxy/plugins.py` checks the schema, then
the rules the schema cannot state. This section gives both. Each name pattern it cites is a constant
in `core/agent-proxy/agent_proxy/config.py` or in `plugins.py`.

The manifest is a JSON object. No key repeats in one object, and every number is finite. Every
object that the manifest format defines refuses a member it does not name. `input_schema` and
`output_schema` are JSON Schemas, so they take any member that JSON Schema allows.

The manifest has one member, `actions`. It is an object with at least one member. Each key is an
action name, which matches `ACTION_NAME_PATTERN` in `config.py`. Each value is an action. An
operator grants an action to a scope under the key `<plugin>.<action>`, where `<plugin>` is the name
the config pins the image under. `docs/design.md` gives the config under "Config".

### Every action

Every action states each of these members. None has a default.

- `kind` is `"command"` or `"script"`.
- `input_schema` is a JSON Schema whose `type` is `"object"`, and it passes the draft 2020-12
  meta-schema check. It becomes the input schema of the action's MCP tool.
- `credentials` is an array, possibly empty, of the credentials the action expects. Each item is
  `{"env": NAME}`, with `NAME` matching `ENV_NAME_PATTERN` in `config.py`, or `{"file": PATH}`, with
  `PATH` an absolute path beneath `/run/credentials/` with no `..` segment. `docs/design.md` gives
  how a grant supplies a credential in these forms under "Credentials".
- `timeout` is the action's default timeout, a duration string matching `DURATION_PATTERN` in
  `config.py`, such as `30s` or `5m`. `docs/design.md` gives how a grant overrides it under "The
  cage".

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

`docs/design.md` gives how a script receives its input and returns its output under "Plugins and
actions".

## How argv is formed

A command action's argv is the `argv` prefix, then each parameter in the order `parameters` lists
it, then the items of `args`:

- A boolean flag adds the flag when it is true, and adds nothing when it is false.
- A string or integer flag adds the flag, then the value.
- A positional parameter adds the value.
- An optional property that the input leaves out adds nothing.

## Checking a manifest

Build the image, then check it from `core/agent-proxy/`:

```sh
uv run agent-proxy manifest check IMAGE
```

`IMAGE` is any local image reference. The command never pulls, so build or pull the image first. It
reaches Docker through `DOCKER_HOST`. It exits 0 when the manifest passes and prints each warning on
stderr. It exits 1 when the manifest fails and prints each failure on stderr, one per line.
`docs/architecture.md` gives what the command prints under "The admin command".

## A working example

`core/agent-proxy/tests/test-plugin/` is a working plugin. Its `Dockerfile` copies a `manifest.json`
into the image, and a `describe` script onto the `PATH` that prints it. The manifest declares a
command action with a flag and a positional parameter, and a script action whose executable the
image also holds. `COPY` keeps each file's mode, so each executable is executable in the build
context.
