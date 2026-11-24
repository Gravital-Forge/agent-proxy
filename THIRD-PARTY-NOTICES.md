# Third-party notices

`LICENSE` covers the source in this repository, except the vendored seccomp profile that
[The instance image](#the-instance-image) names. Each published image also holds third-party
software, and that software keeps its own licence. This notice names where each image's third-party
software comes from.

## The verifier image

`ghcr.io/gravital-forge/agent-proxy-verifier` holds one executable, built from `core/verifier/` with
the Go toolchain. The executable carries the Go standard library, under the BSD-3-Clause licence.

## The instance image

`ghcr.io/gravital-forge/agent-proxy` is built on the Python image that `core/docker/Dockerfile`
names. That image holds Python and the operating system packages it is built from, each under its
own licence. The instance image also installs the dependencies that `core/agent-proxy/uv.lock`
locks, without its dev group, each under its own licence.

The instance image also ships Docker's default seccomp profile, verbatim, as the core package's
`agent_proxy/seccomp/default.json`, from `core/agent-proxy/agent_proxy/seccomp/default.json`. The
file comes from `github.com/moby/profiles/seccomp` v0.2.4, under the Apache License 2.0.

## The plugin images

Each image `ghcr.io/gravital-forge/agent-proxy-plugin-<plugin>` is built on the Alpine image that
`plugins/<plugin>/Dockerfile` names, and installs Alpine packages. Each package keeps its own
licence, and Alpine publishes the source of each one.

Two of these images ship software that needs a notice of its own:

- The git and github plugin images ship git from Alpine's package. git is under the GPL-2.0 licence.
- The github plugin image ships gh from gh's release tarball. gh is under its own MIT licence.
