# Writing a plugin

A plugin is an OCI image that declares actions in a manifest. The proxy runs each action in a cage
built from that image. `docs/design.md` gives the plugin model under "Plugins and actions" and the
cage under "The cage". This document tells a plugin author what the image holds, how to write its
manifest, how an action runs, and how to check the manifest.

## The image

The image holds an executable named `describe` on its `PATH`. `describe` prints the manifest as
UTF-8 JSON on stdout and exits 0.

The proxy runs `describe` with the image's entrypoint set to `["describe"]` and no command, in the
container that `docs/architecture.md` gives under `plugins.py`. So `describe` reads only what the
image holds, writes nothing but its output, and keeps within that section's time and output limits.
A non-zero exit shows the start of stderr, so write a diagnostic there.

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

The parser refuses what it cannot check. The manifest and each schema it embeds must nest shallowly
enough for Python's recursion limit, and each integer must stay within Python's digit limit for
converting text to an integer.

The manifest has one member, `actions`. It is an object with at least one member. Each key is an
action name, which matches `ACTION_NAME_PATTERN` in `config.py`. Each value is an action. An
operator grants an action to a scope under the key `<plugin>.<action>`, where `<plugin>` is the name
the config pins the image under. `docs/design.md` gives the config under "Config".

### Every action

Every action states each of these members. None has a default.

- `kind` is `"command"` or `"script"`.
- `input_schema` is a JSON Schema whose `type` is `"object"`, and it passes the draft 2020-12
  meta-schema check.
- `credentials` is an array, possibly empty, of the credentials the action expects. Each item is
  `{"env": NAME}`, with `NAME` matching `ENV_NAME_PATTERN` in `config.py`, or `{"file": PATH}`, with
  `PATH` an absolute path beneath `/run/credentials/` with no `..` segment. `docs/design.md` gives
  how a grant supplies a credential in these forms under "Credentials".
- `timeout` is the action's default timeout, a duration string matching `DURATION_PATTERN` in
  `config.py`, such as `30s` or `5m`, whose count fits a Python `timedelta`. `docs/design.md` gives
  how a grant overrides it under "The cage".

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

"How an action runs" gives how a script receives its input and returns its output.

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

The proxy runs each call in a new cage built from the plugin's image. `docs/architecture.md` gives
the cage under `runner.py`, and `docs/design.md` gives it under "The cage". The cage's entrypoint
replaces the image's entrypoint and command:

- A command action's entrypoint is the argv above.
- A script action's entrypoint is its `executable`, with no arguments. The script reads its input on
  stdin as one compact UTF-8 JSON object, then end of file.

The root filesystem is read-only. Write scratch files under `/tmp`, a small tmpfs.

The cage's network follows the grant's `domains`. `docs/architecture.md` gives the network and its
proxy under `egress.py`, and `docs/design.md` gives the egress model under "The cage".

- A grant that names domains gives the cage one environment variable,
  `HTTPS_PROXY=http://egress:1080`. Through that proxy, the cage reaches each host the grant names,
  exactly as it names it, and no other host.
- The proxy passes TLS through by SNI and never decrypts it, so the tool checks the server's own
  certificate. The cage gets no proxy variable for plain HTTP.
- A tool that ignores `HTTPS_PROXY` has no route out. curl reads the variable; busybox `wget`
  ignores it.
- A grant with `domains: []` gives the cage no proxy and no egress at all.
- A credential cannot take the environment name `HTTPS_PROXY`. The config refuses a grant that gives
  one.

A script succeeds when it exits 0 and prints on stdout one JSON object that its `output_schema`
validates. That stdout must decode as the manifest must, by the rules under "The manifest". The call
then returns that object. Otherwise the script has failed, and the call returns what a command
action returns: the exit code, stdout, and stderr. A stdout over the output cap goes to an overflow
file, so a script whose output passes the cap fails. `docs/design.md` gives the cap and the result
forms under "Results".

## Checking a manifest

Build the image, then check it from `core/agent-proxy/`:

```sh
uv run agent-proxy manifest check IMAGE
```

`IMAGE` is any local image reference; build or pull it first. `docs/architecture.md` gives how the
command reaches Docker under "Where state lives", and what it prints and how it exits under "The
admin command".

## A working example

`core/agent-proxy/tests/test-plugin/` is a working plugin. Its `Dockerfile` copies a `manifest.json`
into the image, and a `describe` script onto the `PATH` that prints it. The manifest declares a
command action with a flag and a positional parameter, and a script action whose executable the
image also holds. `COPY` keeps each file's mode, so an executable must already be executable in the
build context.
