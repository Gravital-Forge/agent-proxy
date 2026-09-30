# agent-proxy spike findings, 2026-09-29

This spike tests the load-bearing claims of the agent-proxy design before the
core is built on them. For each claim it gives a verdict, the check lines that
decide it, and the design change it forces on the core. This is a dated record
of three runs. It is not updated after the spike.

- **Agent egress run**: `spike/egress/run.sh` on the agent's own rootless
  daemon, 2026-09-29, with a dummy GitHub token.
- **Agent cage run**: `spike/cage.py` on the agent's own rootless daemon,
  2026-09-29.
- **Operator run**: `spike/service-user/checks.sh` run by the operator with
  sudo, stamped `2026-09-30T03:38:45Z` (UTC; still 2026-09-29 on the host's
  clock). It runs the file-access checks and then
  the egress script on the rootless daemon of the scratch service user
  `apxspike`, with a real GitHub token.

`apxspike` stands in for `agentproxy-<agent>`, `apxspike-other` for a second
agent, and `apxspike-grp` for a supplementary group of the service user. Check
lines are quoted as printed, except that the agent's account name is replaced
by `<agent>`.

## Verdicts

- **The cage works the agent's tree through the user ACL**: confirmed.
- **Host supplementary groups reach a rootless container**: refuted. The
  groups are dropped, so cage access must come from user ACL entries only.
- **One iron-proxy selects allowlists by source address**: changed.
  iron-proxy 0.52.0 has no source-IP rules. One proxy per distinct domain list
  works, and hot reload works.
- **git over HTTPS and `gh` work through the proxy with an environment token**:
  confirmed.
- **Per-call cage cost**: confirmed. The median is 458.5 ms with a network,
  inside the half-second estimate made during design. The maximum is not.

## Claim 1: the cage works the agent's tree through the user ACL

**Verdict: confirmed.** A cage on the service user's daemon reads and writes
the agent's tree through the `user:apxspike` ACL entry. What it creates comes
out owned by the service user, grouped to the agent, and read-write for the
agent. A second agent cannot read it.

Operator run:

<!-- rumdl-disable MD013 -->

```text
MEASURE F1 path=agent-002.txt owner=<agent> group=<agent> mode=660 acl=user::rw-,user:<agent>:rwx#effective:rw-,user:apxspike:rwx#effective:rw-,group::rwx#effective:rw-,mask::rw-,other::---
MEASURE F1 path=agent-022.txt owner=<agent> group=<agent> mode=660 acl=user::rw-,user:<agent>:rwx#effective:rw-,user:apxspike:rwx#effective:rw-,group::rwx#effective:rw-,mask::rw-,other::---
MEASURE S1 uid=0(root) gid=0(root) groups=0(root),0(root),1(bin),2(daemon),3(sys),4(adm),6(disk),10(wheel),11(floppy),20(dialout),26(tape),27(video)
CHECK S2 expected=002:read=ok,append=ok;022:read=ok,append=ok observed=002:read=ok,append=ok;022:read=ok,append=ok PASS
CHECK S3 expected=file=ok,nested=ok observed=file=ok,nested=ok PASS
CHECK S5 expected=denied observed=denied PASS
MEASURE S8 path=cage-made.txt owner=apxspike group=<agent> mode=660 acl=user::rw-,user:<agent>:rwx#effective:rw-,user:apxspike:rwx#effective:rw-,group::rwx#effective:rw-,mask::rw-,other::---
MEASURE S8 path=cage-dir owner=apxspike group=<agent> mode=2770 acl=user::rwx,user:<agent>:rwx,user:apxspike:rwx,group::rwx,mask::rwx,other::---,default:user::rwx,default:user:<agent>:rwx,default:user:apxspike:rwx,default:group::rwx,default:mask::rwx,default:other::---
MEASURE S8 path=cage-dir/nested.txt owner=apxspike group=<agent> mode=660 acl=user::rw-,user:<agent>:rwx#effective:rw-,user:apxspike:rwx#effective:rw-,group::rwx#effective:rw-,mask::rw-,other::---
CHECK S8a expected=apxspike:<agent> observed=apxspike:<agent> PASS
CHECK S8b expected=apxspike:<agent> observed=apxspike:<agent> PASS
CHECK S8c expected=apxspike:<agent> observed=apxspike:<agent> PASS
CHECK S9 expected=read=ok,append=ok,create=ok observed=read=ok,append=ok,create=ok PASS
CHECK S10 expected=0 observed=0 PASS
CHECK S11 expected=denied observed=denied PASS
CHECK S12 expected=denied observed=denied PASS
```

<!-- rumdl-enable MD013 -->

How to read it:

- `S1` and `S8` belong together. The cage process is uid 0 inside its user
  namespace, and what it creates is owned by `apxspike` on the host. Rootless
  Docker maps container root to the service user, as the spec's hardening
  section says.
- The agent created one fixture under umask `022`, yet it came out mode `660`
  with `user:apxspike:rwx#effective:rw-` (the `F1` lines). The default ACL
  overrides the agent's umask, so `S2` passes for both fixtures.
- `S8` shows the spec's ownership rule. The owner is the service user. The
  group is the agent's, from the setgid bit. The mode is `660` and `other` has
  nothing. The new directory `cage-dir` inherited setgid (`2770`) and every
  default entry.
- `S5` shows that container root has no reach past the ACL. A `0600` file of
  the agent's with no ACL entry is denied.
- `S11` must be read with `S8`. `S8a` shows that `cage-made.txt` existed, so
  the denial is real. But `S11` cannot say which barrier stopped the second
  agent. `S12` shows it cannot list the agent's home, and the spec gives it no
  traverse there either, so it may have been stopped at the home directory.
  The file's own barrier, `other::---`, is shown by the `S8` ACL line. No
  access attempt tested that barrier on its own.

**Forced design change:** none to the ACL layout. The uid-0 cage does force a
change on the git plugin; see "git refuses a repository it does not own" below.

## Claim 2: host supplementary groups reach a rootless container

**Verdict: refuted.** The service user's supplementary groups do not reach the
cage.

Operator run:

<!-- rumdl-disable MD013 -->

```text
MEASURE F1 path=groupfile owner=<agent> group=apxspike-grp mode=640 acl=user::rw-,group::r--,other::---
MEASURE S1 uid=0(root) gid=0(root) groups=0(root),0(root),1(bin),2(daemon),3(sys),4(adm),6(disk),10(wheel),11(floppy),20(dialout),26(tape),27(video)
MEASURE S4 supplementary_groups=dropped
```

<!-- rumdl-enable MD013 -->

`apxspike` is a member of `apxspike-grp`, and `groupfile` grants that group
read. The cage was denied. The group list in `S1` is the image's own group
list for root, with no host group in it.

After the operator run, a read of `/proc` on the host showed the `rootlesskit`
process of the `apxspike` daemon holding `apxspike-grp`. This is an
observation, not a check line. The daemon has the group, and the container
start drops it, so neither a daemon restart nor a different provisioning order
would bring it back.

**Forced design change:**

- The cage reaches the agent's tree only through the
  `user:agentproxy-<agent>` ACL entry and its default entry. The spec's layout
  already works this way. Group access is now closed as an alternative:
  provisioning must never grant the instance access by adding the service user
  to a group, the agent's group included.
- The cage cannot choose the agent's group for what it creates, because it is
  not a member. The setgid bit on each root is the only thing that groups cage
  files to the agent (`S8`). Provisioning must set setgid on every root, and
  the core must not rely on the cage to fix a file's group.
- The spec's known gap stands with no mitigation by group. A file with an
  explicit restrictive mode lowers the ACL mask, and so it limits the service
  user's entry.

## Claim 3: one iron-proxy selects allowlists by source address

**Verdict: changed.** iron-proxy 0.52.0 has no source-IP rules. The spike
tested one proxy per distinct domain list in their place, and that layout
holds. Hot reload works.

### Source-IP rules are absent

Planning found that iron-proxy 0.52.0 has no source-IP rules. The iron-proxy
pull request that added `source_ip` (#191) was closed unmerged, and the one
for `source_cidrs` (#184) was still open. The pinned binary carries only
`host`, `cidr`, `methods`, and `paths` in a rule. `E9` tests this on the pinned
image. It starts a proxy whose only allowlist entry is a rule for
`example.com` limited to `172.31.9.0/24`, and requests `example.com` from an
address outside that range. `E9b` requests a host the rule does not name. Both
runs printed the same lines:

```text
MEASURE E9 reload=200:{"status":"ok"}
MEASURE E9 request=200
CHECK E9 expected=unsupported observed=accepted-and-ignored PASS
CHECK E9b expected=exit56 observed=exit56 PASS
```

iron-proxy accepts the rule and enforces its `host`, but it ignores
`source_ip`. A `source_ip` rule therefore passes validation and widens access.
`E9` prints `PASS` only for `refused-at-start`, `reload-422`, and
`accepted-and-ignored`, so its `PASS` means "not enforced".

### One proxy per domain list

The tested layout has one iron-proxy per distinct domain list. Each proxy sits
on its own `--internal` network and on one shared egress bridge. Each proxy
answers to the alias `egress` on its internal network, so every cage uses
`HTTPS_PROXY=http://egress:1080` and cages differ only in the network they
join. The `gh` network allows `github.com` and `api.github.com`. The `ex`
network allows `example.com`. The `none` network has no proxy. Both runs
printed the same lines:

```text
CHECK E1 expected=200 observed=200 PASS
CHECK E2 expected=exit56 observed=exit56 PASS
CHECK E3 expected=200 observed=200 PASS
CHECK E4 expected=exit56 observed=exit56 PASS
CHECK E5 expected=exit6|exit7 observed=exit6 PASS
CHECK E6a expected=exit5 observed=exit5 PASS
CHECK E6b expected=exit6|exit7 observed=exit6 PASS
CHECK E7a expected=fail observed=exit5 PASS
MEASURE E7b proxy_ex_ip=172.31.2.2
CHECK E7b expected=fail observed=exit7 PASS
```

- `E1` to `E4`: each network reaches its own allowlist and nothing else.
- `E5` and `E6b`: a cage that bypasses the proxy cannot resolve an outside
  name (exit 6), so it has no direct way out by name. No check tried an
  outside address directly.
- `E6a`: on a network with no proxy, there is no proxy to reach (exit 5).
- `E7a` and `E7b`: a cage cannot reach another network's proxy, by name or by
  address.

### Hot reload

`E8` rewrites the `ex` proxy's config in place and posts a reload. Both runs
printed the same lines:

```text
CHECK E8a expected=200:{"status":"ok"} observed=200:{"status":"ok"} PASS
CHECK E8b expected=200 observed=200 PASS
CHECK E8c expected=exit56 observed=exit56 PASS
MEASURE E8d reply=422:{"error":"unknown_transform_\"apx-spike-no-such-transform\":_unknown_transform:_\"apx-spike-no-such-transform\""}
CHECK E8d expected=422 observed=422 PASS
CHECK E8e expected=200 observed=200 PASS
```

A valid change takes effect on reload (`E8a`, `E8b`) and leaves the other
proxy alone (`E8c`). An invalid config is refused with 422, and the previous
config keeps serving (`E8d`, `E8e`).

**Forced design change:**

- The core runs one iron-proxy per distinct domain list, not one per instance.
  Each proxy has its own internal network, joins the shared egress bridge, and
  takes the alias `egress`.
- A grant with `domains: []` gets an internal network with no proxy.
- The core never writes a `source_ip` rule into iron-proxy config. The core
  pins iron-proxy. A version upgrade must repeat `E9` before the core relies on
  source rules.
- A reload refused with 422 leaves the running config in place, but the
  refused file stays on disk, and a proxy restart would read it. The core must
  validate the config before it writes the file, or restore the previous file
  after a 422. This follows from `E8d`; the spike did not restart a proxy.

## Claim 4: git and gh through the proxy with an environment token

**Verdict: confirmed.** With a real token in `GH_TOKEN`, `gh` and an
authenticated `git ls-remote` succeed through the `gh` network's proxy.

Operator run, real token:

```text
MEASURE M1 gh_token=real
CHECK E10 expected=exit0 observed=exit0 PASS
CHECK E11 expected=exit0 observed=exit0 PASS
CHECK E12 expected=exit0 observed=exit0 PASS
```

Agent egress run, dummy token:

```text
MEASURE M1 gh_token=dummy
CHECK E10 expected=exit0 observed=exit0 PASS
CHECK E11 expected=http401 observed=http401 PASS
CHECK E12 SKIP GH_TOKEN unset, dummy token in use
```

- `E10` is an anonymous `git ls-remote` of a public repository.
- `E11` is `gh api /user`. With the dummy token, GitHub itself answered 401,
  so the request passed the proxy and the token reached GitHub.
- `E12` is `git ls-remote` of a private repository, run only with the real
  token.

The proxy passes TLS through by SNI, so the cage needs no CA of the proxy's.

**Forced design change:** `gh` reads `GH_TOKEN` as it is. git does not read
the variable, so the git plugin supplies it through a credential helper. `E12`
used this form, which clears any inherited helper before it adds its own:

<!-- rumdl-disable MD013 -->

```text
-c credential.helper= -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
```

<!-- rumdl-enable MD013 -->

## Claim 5: the per-call cost of starting a cage

**Verdict: confirmed.** A cage per call fits the half-second estimate made
during design at the median. A cage call on an internal network has a median
of 458.5 ms. The p95 is at the estimate and the maximum is over it.

Agent cage run, 30 timed calls after 3 warm-up calls, each one a create,
start, wait, and remove of a cage running `true`, through the Docker SDK for
Python:

<!-- rumdl-disable MD013 -->

```text
MEASURE C1 network=none calls=30 min_ms=256.1 median_ms=331.8 p95_ms=394.1 max_ms=427.9
MEASURE C2 network=apx-spike-net-cost calls=30 min_ms=344.2 median_ms=458.5 p95_ms=498.5 max_ms=552.6
```

<!-- rumdl-enable MD013 -->

`C2` is the figure to compare. `C1` is the baseline with no network: the
network adds about 125 ms at the median. Across four runs the medians stayed
within 298 to 357 ms for `C1` and 453 to 471 ms for `C2`; only the quoted run's
full output was kept. The figures cover the cage alone. They leave out the
action's own work and any image pull. They come from the agent's daemon; the
cost on the service user's daemon was not measured.

**Forced design change:** none. A cage per call stands. Every action call adds
about half a second around its tool's own run time, so a sequence of calls
pays it once per call.

## Facts the core must honor that the spec does not state

### iron-proxy config

Planning verified these on the pinned image; `E8a`, `E8d`, and every egress
check exercised them. `spike/egress/run.sh` holds a working config.

- `dns.proxy_ip` is required, even when DNS interception is not used.
- The proxy binds `:53`, `:80`, `:443`, and `:9090` by default.
- `tls.mode: "sni-only"` needs no CA.
- CONNECT requests arrive on `proxy.tunnel_listen`.
- The allowlist is
  `transforms: [{name: allowlist, config: {domains: [...]}}]`.
- A denied CONNECT gets 403.
- `POST /v1/reload` with `Authorization: Bearer <key>` returns
  `{"status":"ok"}`. A config that fails to parse or build returns 422. The
  endpoint re-reads the file on disk and ignores the request body.
- The image's entrypoint has no default arguments, so every run passes
  `-config /etc/iron-proxy/proxy.yaml`.
- The config is a bind-mounted directory, never a single file. A single-file
  bind mount keeps the old inode when the file is replaced, and a reload would
  read stale config.

### curl, not busybox wget

busybox `wget` ignores `HTTPS_PROXY` and connects directly. On an internal
network that connection fails, so a deny check made with `wget` passes for the
wrong reason. The core's egress tests use curl.

### A refused CONNECT is exit 56 in this curl

The tools image, built from the pinned alpine base with unpinned packages, had
this curl in both runs:

<!-- rumdl-disable MD013 -->

```text
MEASURE M0 curl_version_line="curl 8.14.1 (x86_64-alpine-linux-musl) libcurl/8.14.1 OpenSSL/3.5.8 zlib/1.3.2 brotli/1.1.0 zstd/1.5.7 c-ares/1.34.8 libidn2/2.3.7 libpsl/0.21.5 nghttp2/1.69.0"
```

<!-- rumdl-enable MD013 -->

It exits 56 on a refused CONNECT (`E2`, `E4`, `E8c`, `E9b`). Newer curl exits
7, the code it also uses for a failed connection (`E7b`). The core matches on
the proxy's 403, not on the exit code, because a rebuild can bring a different
curl.

### git refuses a repository it does not own

Operator run:

```text
MEASURE S6 exit=128 stderr="fatal: detected dubious ownership in repository at '/home/<agent>/apx-spike/repo'"
CHECK S7 expected=0 observed=0 PASS
```

The cage process is uid 0 (`S1`), and the repository belongs to the agent's
uid, which the cage's user namespace does not map. git refuses to work in it
(`S6`). With `-c safe.directory=<repository path>`, a commit succeeds (`S7`).
The git plugin passes `safe.directory` on the command line for each root; that
is the form `S7` tested. The check that `safe.directory` turns off guards
against configuration a repository plants. Inside the cage that risk is the
spec's known gap about planted code, and the cage bounds it.

### Cage cost

See claim 5.

## Sanity checks on the cage flags

These check the cage flags; they are not spike claims. Agent cage run:

```text
CHECK H1 expected=refused observed=refused PASS
CHECK H2 expected=written observed=written PASS
CHECK H3 expected=0000000000000000 observed=0000000000000000 PASS
CHECK H4 expected=1 observed=1 PASS
```

The root filesystem is read-only, `/tmp` is writable, no capability is in
effect, and `no-new-privileges` is set. The operator run's cage used the same
flags but did not repeat these checks.

## Where the scripts differ from the plan

- Extra `MEASURE` lines give context, and no check depends on them. They are
  `M1` (token kind), `F1` (fixture owner, mode, and ACL), `S0` (cage exit),
  `R1` (egress script exit), `E7b` (proxy address), and the `E8d` and `E9`
  replies.
- `S4` and `S6` print as `MEASURE` lines, not `CHECK` lines, because the plan
  gave them no expected value.
- `S8` prints a `MEASURE` line per path, plus the checks `S8a` to `S8c` on
  owner and group. Read the mode and ACL from the `MEASURE` lines.
- The plan had root create `groupfile` and `otherfile`. The script has the
  agent write each file, strip its ACL, and set its mode. Root then changes
  only the group, with `chown -h`, so root never writes through a path the
  agent controls. The `F1` lines show the result the plan asked for.
- `E9` prints `FAIL` for an `inconclusive-<value>` result as well as for
  `accepted-and-enforced`. The plan passed every value except the latter.

## Not run

- `H1` to `H4`, `C1`, and `C2` on the service user's daemon.
- The `S` checks on the agent's daemon; they need root.
- `E12` in the agent egress run, which had no real token.
- A second-agent access check that separates the file's own barrier from the
  home directory's.

## Reproduce

From the repository root. `run.sh` builds the `apx-spike-tools` image that
`cage.py` uses.

```bash
spike/egress/run.sh
uv run --script spike/cage.py
sudo spike/service-user/provision.sh <agent>
sudo spike/service-user/checks.sh <agent> --gh-token-file <path>
sudo spike/service-user/teardown.sh <agent>
```

`--gh-token-file` is optional. Without it, `E11` uses the dummy token and
`E12` is skipped. `checks.sh` writes its output to
`/home/<agent>/apx-spike/results.txt`.
