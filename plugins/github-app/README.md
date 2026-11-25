# The github-app plugin

The github-app plugin gives a GitHub App installation token as the value of a provided credential,
and does nothing else. Its one action, `installation_token`, is a provider action, so no grant can
name it and no token can call it: only a `provided_credentials` entry runs it. The core's
architecture defines the cage, the grant, and the other terms this README uses, under
[Concepts](../../docs/architecture.md#concepts). [Writing a plugin](../../docs/writing-a-plugin.md)
gives the plugin model this follows, the provider action kind's contract included.

## What it does

`installation_token` builds a JWT that authenticates as the App, and signs it with the App's private
key through the `openssl` command. It then asks GitHub for a token for one installation of the App,
with `POST https://api.github.com/app/installations/<installation ID>/access_tokens`. On GitHub's
`201` answer, it prints the token as `value` and GitHub's `expires_at`, as one JSON line, and exits
0. GitHub's installation tokens last one hour.

The JWT's header is `{"alg": "RS256", "typ": "JWT"}`. Its claims are `iat`, 60 seconds before the
time the run starts, `exp`, 540 seconds after it, and `iss`, the App ID as a string. GitHub refuses
an `exp` more than ten minutes in its future, and an `iat` set back a minute allows for a clock that
runs ahead of GitHub's.

## The inputs

- **The App's private key**, a stored credential passed as a file at
  `/run/credentials/github-app-key`. `manifest.json` declares that path. GitHub issues the key as a
  PKCS#1 PEM file, and the plugin also takes a PKCS#8 PEM key.
- **`GITHUB_APP_ID`**, the App's ID, in the entry's `env`. It is a string of ASCII digits.
- **`GITHUB_INSTALLATION_ID`**, the ID of the App's installation on the account whose repositories
  the token reaches, in the entry's `env`. It is a string of ASCII digits.

The entry's `domains` is `[api.github.com]`, the one host the plugin reaches.

## A provided credential

A config pins the plugin's image as `github-app`, and names `github-app.installation_token` as the
provider of a provided credential. A grant then takes that credential as an environment variable, as
it takes a stored one. In the form the core's reference gives under
[The config file](../../docs/reference.md#the-config-file):

```yaml
provided_credentials:
  github-app-token:
    provider: github-app.installation_token
    credentials: {github-app-key: {file: /run/credentials/github-app-key}}
    env: {GITHUB_APP_ID: "123456", GITHUB_INSTALLATION_ID: "7654321"}
    domains: [api.github.com]
    refresh_before: 10m

scopes:
  dev-agent:
    grants:
      git.fetch:
        roots: [{path: /home/agent-01/Projects/example-repo, mode: rw}]
        domains: [github.com]
        credentials: {github-app-token: {env: GH_TOKEN}}
        env: {GITHUB_REPOS: "example-org/*"}
```

`refresh_before: 10m` asks for a new token when the cached one has ten minutes or less left. The
entry needs no `max_age`, because GitHub's answer always carries `expires_at`. Quote each ID, so the
YAML holds it as a string. The git plugin refuses every call of a grant that gives a `GH_TOKEN` and
sets no `GITHUB_REPOS`, as its [credential helper](../git/README.md#the-credential-helper) gives.

## The App's permissions

The provider itself needs no App permission: the App's JWT is all GitHub asks for to issue an
installation token. The token holds every permission the App's installation grants, on every
repository the installation reaches. The plugin asks for no narrower set of repositories or
permissions, so set the App's permissions and the installation's repositories to what the grants
that use the token need.

## Failures

Every failure exits 1 with one line on `stderr`, which begins `github-app plugin failed: `, then the
reason. The core then refuses each call that needs the value, unless an unexpired value is cached.
The reasons:

- An ID is unset or empty: `<name> is not set; the provided credential's env sets it`, where
  `<name>` is `GITHUB_APP_ID` or `GITHUB_INSTALLATION_ID`.
- An ID holds anything but ASCII digits: `<name> is not a string of digits: '<value>'`.
- The key file cannot be opened:
  `the App key at /run/credentials/github-app-key cannot be read: <error>`.
- The `openssl` command does not start: `openssl at /usr/bin/openssl could not run: <error>`.
- `openssl` cannot sign with the key, for example because the file holds no PEM private key:
  `openssl could not sign the JWT with the App key at <path>: exit <code>: <line>`, where `<path>`
  is the key's path and `<line>` is the first line of openssl's `stderr`, which names the file and
  never quotes its content.
- GitHub answers anything but `201`:
  `POST /app/installations/<id>/access_tokens answered <status>: <message>`, where `<message>` is
  the `message` member of GitHub's JSON answer, on one line and cut to 200 characters. An answer
  whose body holds no `message` string gives
  `POST /app/installations/<id>/access_tokens answered <status> with no message`. A redirect is such
  an answer, because the plugin follows none.
- GitHub's `201` lacks a non-empty `token` string or an `expires_at` string:
  `POST /app/installations/<id>/access_tokens answered 201 without a token and an expires_at string`.
- No answer comes: `POST /app/installations/<id>/access_tokens failed: <error>`. Each request times
  out after 20 seconds.

The plugin checks both IDs before it reads the key, and reads the key before it sends any request.

## Where the key and the token go

- The key stays in the cage. `openssl` reads it from its file, and nothing prints it.
- The JWT travels only to `api.github.com`, in the `Authorization` header of the one POST.
  `https://api.github.com` is a constant in `api.py`, never read from input or the environment. The
  request follows no redirect, so the JWT never follows one to another host.
- The token reaches only the plugin's `stdout`, which the core reads as the provided value.
- No failure line holds the key, the JWT, or a token. A reason quotes only GitHub's `message`, never
  the rest of an answer's body. The 200-character cut is shorter than an RS256 JWT's signature, so a
  `message` that echoed the request's `Authorization` header still cannot quote the whole JWT.

## Egress

The plugin uses urllib's default proxy handler, so it reaches the web only through the cage's egress
proxy, which `HTTPS_PROXY` names. Its default TLS context reads `SSL_CERT_FILE`. The egress proxy
refuses a host the entry's `domains` does not name.

## Layout

- `scripts/github_app_plugin/` is the module. `errors.py` holds the one failure, `app_jwt.py` the
  JWT and its signing through `openssl`, `api.py` the request to GitHub and the checks of its
  answer, and `entry.py` the checks of the IDs and the run that prints the value or the failure
  line.
- `scripts/entry` is the executable. The image links the action's `executable`,
  `/plugin/bin/installation_token`, to `/plugin/entry`.
- `Dockerfile` builds the image on the Alpine base the git plugin pins, with Python 3 and `openssl`
  from apk. The module uses Python's standard library alone: no GitHub SDK, JWT library, or
  third-party HTTP library.
- `describe` prints `manifest.json`.
- `tests/unit/` runs the module with its HTTP layer faked, signs with keys it generates with the
  host's `openssl`, and checks the manifest. It needs no Docker, and needs `openssl` at
  `/usr/bin/openssl`.
- `tests/fake-api/` builds a fake `api.github.com`, which runs on an internal network under that
  name, with a certificate from its own test CA. It answers the access-token request with `201` when
  the JWT's signature verifies against the public key the test hands it, and with `401` and GitHub's
  message when it does not. It logs each request's method, host, path, headers, and decoded JWT.
- `tests/integration/` builds the plugin image, the core's verifier image, and the fake, and runs
  the action in a real cage through the core's runner, with keys the test generates. It shows that a
  PKCS#1 key and a PKCS#8 key each give the token and its expiry, that a key the fake does not trust
  gives GitHub's message, and that a missing installation ID fails with no request.

## Build and test

Build the image from this directory, then check its manifest from `core/agent-proxy/`:

```sh
docker build -t agent-proxy-github-app .
cd ../../core/agent-proxy
uv run agent-proxy manifest check agent-proxy-github-app
```

Run the checks from this directory:

```sh
poe lint
poe test
poe test-integration
```

`poe test` runs the unit tests and needs only the host's `openssl` at `/usr/bin/openssl`.
`poe test-integration` runs on the Docker daemon that `DOCKER_HOST` names. The contributing guide
gives under [The checks](../../CONTRIBUTING.md#the-checks) what each check needs, and what the
integration tests must leave behind.
