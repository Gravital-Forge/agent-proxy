# Contributing to agent-proxy

Every change lands on `main` through a pull request, and CI runs on each pull request. This guide
gives what you need to build and check a change before you open one.

## Development setup

You need these tools on the host:

- uv, which gives each package its Python and its environment.
- poethepoet, the task runner that runs each package's checks. Install it with
  `uv tool install poethepoet`.
- shellcheck, which the core package's lint runs on the scripts in `core/docker/`.
- Docker with a rootless daemon, for the integration tests.

The verifier's Go code is format-checked, vetted, tested, and built inside its Dockerfile, and the
test plugin's setid probe is format-checked, vetted, and built inside the test plugin's Dockerfile.
So you need no Go toolchain to check either. Each plugin's README gives, under "Build and test",
what that plugin's unit tests need on the host.

## The packages

A directory under `core/` or `plugins/` that holds a `pyproject.toml` is its own uv package, with
its tests inside it. Run a package's checks from the package's own directory. The repository root
holds no `pyproject.toml`.

## The checks

Each package declares its checks as poe tasks in its `pyproject.toml`. Run them from the package's
directory:

- `poe lint` runs ruff's lint and its format check. In the core package, it also runs shellcheck.
- `poe test` runs the unit tests under coverage, then prints the coverage report.
- `poe test-integration` runs the integration tests on the Docker daemon that `DOCKER_HOST` names.
  That daemon must be rootless. For the rootless daemon of your own user:

  ```sh
  DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock poe test-integration
  ```

The Markdown lint runs from the repository root, with the rules in `.rumdl.toml`:

```sh
uvx rumdl@<version> check .
```

`<version>` is the rumdl version that the `lint-md` job in `.github/workflows/ci.yml` pins.

CI runs on each pull request and each push to `main`. Its jobs run `poe lint` and `poe test` in
every package, run the Markdown lint, check the manifest of each plugin image as [Plugins](#plugins)
gives, and build the release images as [Published images](docs/architecture.md#published-images)
gives. The integration tests run outside CI, because they need a rootless daemon.

### What a test leaves behind

Every container, network, volume, and image a test creates carries the label `agent-proxy.test=1`.
After an integration run, with no other run on the same daemon, each of these commands prints
nothing:

```sh
docker ps -aq --filter label=agent-proxy.test=1
docker network ls -q --filter label=agent-proxy.test=1
docker volume ls -q --filter label=agent-proxy.test=1
```

To remove the labelled images, run:

```sh
docker image prune --all --force --filter label=agent-proxy.test=1
```

The prune needs `--all`, because without it a tagged image survives.

## The test suites

- The core's unit tests, in `core/agent-proxy/tests/unit/`, are plain pytest with Docker mocked.
- The core's integration tests, in `core/agent-proxy/tests/integration/`, run on a real rootless
  daemon. Their `conftest.py` builds the images the tests run, among them the test plugin, which it
  pushes to a throwaway registry container so that a test pulls it by digest; the egress probe; the
  verifier; and the instance image, with a test variant built from
  `core/agent-proxy/tests/instance-image/` that sets the test label.
- The transport test, `core/agent-proxy/tests/integration/test_transport.py`, starts the server and
  drives its MCP surface with a FastMCP client.
- `core/agent-proxy/tests/test-plugin/` holds the source of the test plugin, and
  `core/agent-proxy/tests/egress-probe/` holds the source of the egress probe: the pinned Alpine
  base with curl added.
- `core/agent-proxy/tests/test-plugin/setid-probe/` holds the setid probe, a Go program on the
  standard library alone, which a build stage of the test plugin's Dockerfile builds. The test
  plugin runs it as its `setid_probe` action. In the directory it is given, it tries each route to a
  file that holds the setuid or the setgid bit, then the controls that must still work there, and
  prints one line for each attempt: the route, the mode, and `ok` or the errno's name.
  `test_a_cage_cannot_make_a_setid_file_in_a_rw_root` in
  `core/agent-proxy/tests/integration/test_runner.py` runs it, as
  [The seccomp profile](docs/architecture.md#the-seccomp-profile) gives.
- Each plugin package holds its own unit and integration tests, which its README describes.

## Writing tests

- An egress test asserts the proxy's refusal, never curl's exit code, which changes between curl
  versions. It uses curl, never busybox `wget`, which ignores `HTTPS_PROXY`.
- A claim about Docker, iron-proxy, git, or gh behaviour that a design choice rests on gets a test
  that runs it on the pinned version.
- An upgrade of iron-proxy runs `test_iron_proxy_ignores_a_rules_source_ip` in
  `core/agent-proxy/tests/integration/test_egress.py` first.

An [acceptance check](docs/acceptance-checks.md) goes into that document, never into the test
suites.

## Plugins

A plugin's source is a uv package under `plugins/<plugin>/`. It holds the plugin's Dockerfile, its
manifest and the `describe` script that prints it, its scripts, its tests, and its README. CI's
`manifest-check` job builds each plugin image and runs `agent-proxy manifest check` on it.
[Writing a plugin](docs/writing-a-plugin.md) gives what the image holds, and how to write and check
its manifest.

## Pins

Dependabot, configured in `.github/dependabot.yml`, proposes updates for three kinds of pin: the
`uses:` lines of the workflows, each `FROM` line of a Dockerfile in a directory that file lists, and
each package's `uv.lock`.

Every other pin is updated by hand. That is a version or digest written in Python source or a test;
in a workflow's `run` script, step input, or service container; in a Dockerfile `RUN` line or `ARG`;
or in a language version file, such as `.python-version` or the `go` line of `go.mod`.

The seccomp profile in `core/agent-proxy/agent_proxy/seccomp/default.json` is a pin updated by hand
too. It is Docker's default profile, byte for byte: `github.com/moby/profiles/seccomp` v0.2.4, the
version that Docker 29.9.2 builds in. moby's source vendors it as
`vendor/github.com/moby/profiles/seccomp/default.json` at the tag `docker-v<version>`. Never edit
the file. To update it, copy the file at a new daemon version's tag, and change the SHA-256 that
`test_the_vendored_profile_is_docker_s_default_byte_for_byte` in
`core/agent-proxy/tests/unit/test_cage_seccomp.py` checks, and the version in
[`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

## Repository layout

- `core/agent-proxy/` holds the core package: the Python source of the instance and the operator
  command, the manifest schema, and the tests with the images they build.
- `core/docker/` holds the instance image's Dockerfile, and the shell scripts that provision, start,
  and tear down an instance and build the release images.
- `core/verifier/` holds the in-cage root verifier: its Go source, its tests, and the Dockerfile
  that builds it into a static executable.
- `plugins/` holds one uv package for each plugin.
- `docs/` holds the product's documents: how it works, how to deploy and check it, its reference,
  and how to write a plugin.
- `examples/` holds a sample instance config.
- `.github/` holds the CI and release workflows, the Dependabot config, and the branch rulesets.
