# Deploying agent-proxy

This guide takes the operator through one agent's instance: the requirements, why each part of the
setup is needed, the config, provisioning, the first start, the self-check, changing the config,
teardown, and upgrading to a new release. The architecture gives the isolation model under
[The isolation model](architecture.md#the-isolation-model), and the instance under
[The instance](architecture.md#the-instance). The examples use the agent `agent-01`, whose service
user is `agentproxy-agent-01`.

Every step runs as root. Where a step names `<uid>`, it means the service user's uid, which
`id -u agentproxy-agent-01` prints. `<home>` is the agent's home, `<tree>` is the resolved path of
the agent's tree, and `<service home>` is the service user's home. The agent's tree is the path that
`--tree` names, or `<home>/Projects` without that option. Every `docker` command runs as root with
`DOCKER_HOST=unix:///run/user/<uid>/docker.sock`, so it reaches the service user's daemon.

## Requirements

The host needs these packages:

- Docker Engine with `docker-ce-rootless-extras`, which gives the service user its rootless daemon.
- `uidmap`, which gives the rootless daemon its subordinate uid and gid ranges.
- `dbus-user-session`, which the service user's systemd user manager needs.
- `acl`, which gives `setfacl` and `getfacl`.
- systemd, which keeps the service user's daemon running under linger.

A cage's memory limit and process limit hold only where the service user's daemon can enforce them.
A rootless daemon can enforce them on a host with cgroup v2 that delegates the `memory` and `pids`
controllers to the service user. Where the daemon cannot enforce a limit, the instance warns at
start and runs each cage without that limit, as
[Failures and warnings](reference.md#failures-and-warnings) gives.

Root must be able to read a checkout of this repository. The scripts run from it, and without
`--release` the provisioning script builds the verifier and instance images from it. With
`--release`, the checkout must be at the release's tag, so that the scripts match the release's
images.

Each plugin line in the config names its image by digest, in a registry that the service user's
daemon can pull from. A release's notes list the published images under `## Images`, each by its
digest reference, ready for a plugin line. To use a plugin image built from the checkout, push it to
such a registry.

## Why each part is needed

Provisioning sets up these parts around the instance. Each one closes a way that the agent, the
instance, or another agent could reach too far.

- **The service user.** The instance, its egress proxies, and its cages run as `agentproxy-<agent>`,
  never as the agent and never as root. So the agent cannot read a cage's credentials, and a
  compromised instance or cage reaches one agent's tree and one instance's credentials, not the
  host. Under the agent's own uid, the agent could read each cage's environment and files through
  `/proc`; under root, a compromise would hold the host.
- **Its rootless daemon.** The service user runs its own Docker daemon, and the instance starts
  every container on it. The agent cannot reach that daemon's socket, so it cannot start a container
  that skips its grants, and the daemon maps container root to the service user. With the host's
  root daemon in its place, a compromised instance or cage holds root on the host.
- **The ACLs on the agent's tree.** Only the agent and the service user may traverse the agent's
  home, and provisioning warns when anyone else can. The service user gains traverse on the home and
  an ACL entry on the tree, the agent gains a default entry, and setgid groups each new file to the
  agent. So the instance reads and writes the tree, the agent can read and write each file a cage
  creates, and another agent reaches neither. Without them, a cage cannot open its roots, or the
  agent cannot write a file that a cage rewrote, such as a repository's `.git/index`; and a mode or
  group wide enough to fix that opens the tree to other users.

[The isolation model](architecture.md#the-isolation-model) gives how each part works.

## The config

Copy `examples/instance.yaml`, and edit the copy:

1. Set `agent_tree` to `<tree>`, the path that `realpath` prints for the agent's tree, such as
   `realpath /home/agent-01/Projects`. Provisioning refuses any other value.
2. Set `overflow_dir` to a directory inside the tree, by a path with no symbolic link in it. The
   scripts create that directory at provisioning and remove it at teardown, as the agent, so both
   refuse any other path.
3. Under `plugins`, give each plugin's image by its digest reference: from the release notes'
   `## Images`, or from the registry that the operator pushed the image to.
4. Under `scopes`, give each grant the agent needs. Each plugin's README gives the `env` settings
   its grants need: [git](../plugins/git/README.md#a-grant),
   [github](../plugins/github/README.md#a-grant), and
   [hcp-terraform](../plugins/hcp-terraform/README.md#a-grant).
5. Under `provided_credentials`, give each credential that a provider fetches in place of a stored
   value, and name it in the grants that use it. The github-app plugin's
   [A provided credential](../plugins/github-app/README.md#a-provided-credential) gives an entry
   that mints GitHub App installation tokens. Leave the section out when no grant needs one.

The reference gives every field and every rule a config must satisfy under
[The config file](reference.md#the-config-file).

## Provisioning

Run the script from the checkout:

```sh
sudo core/docker/provision-instance.sh [--release <version>] [--tree <path>] agent-01 <port> <config-file>
```

`<port>` is the loopback port the instance serves on, and `<config-file>` is the config to install.
The config may name stored credentials that are not in the store yet;
[The first start](#the-first-start) stores them.

The options come before the positional arguments, in any order:

- `--release <version>` selects release mode. The script pulls the instance image of the release
  `v<version>` and builds no image. `<version>` is `<major>.<minor>.<patch>`, with no `v`. The
  service user's daemon must be able to pull from `ghcr.io`.
- `--tree <path>` names the agent's tree.

An unknown option, an option with no value, or a missing positional argument makes the script print
its usage line and exit 2, before it runs any step.

The script runs the steps below in order, and reports each step as it runs it. Each step leaves its
object alone when it is already in place, so a second run with the same config changes nothing.
Every run installs `<config-file>` over the installed config. An instance that is already running
keeps its config in force until a reload, as [Changing the config](#changing-the-config) gives. A
step that fails stops the script; after the fix, run the script again from the top. To debug a step,
run its commands by hand.

These phrases stand for the way the script reaches the service user and the agent:

- "As the service user" means through
  `systemd-run --machine=agentproxy-agent-01@.host --user --wait --pipe --quiet <command>`.
- "On the service user's daemon" means through
  `DOCKER_HOST=unix:///run/user/<uid>/docker.sock docker <arguments>`.
- "As the agent" means through `runuser -u agent-01 -- <command>`, run from `/`. The agent can swap
  any path inside the tree for a symbolic link, so a step that creates or grants in the tree as root
  could reach a file outside it. Such steps run as the agent instead.

1. `check_arguments` checks that the agent exists, that the service user's name is at most 32
   characters, that the port is an integer from 1 to 65535, that a given `<version>` is
   `<major>.<minor>.<patch>`, that the config file is a readable regular file, and that the agent's
   tree is a directory. It reads the agent's home, the tree's realpath, and the agent's group as
   `id -gn agent-01`. It stops when the tree's realpath does not lie strictly inside the realpath of
   the home; [The tree inside the home](architecture.md#the-tree-inside-the-home) gives why.
2. `create_service_user` creates the service user with
   `useradd --create-home --shell /usr/sbin/nologin agentproxy-agent-01`. The service user then
   needs a line in both `/etc/subuid` and `/etc/subgid`. When either line is missing, the step stops
   and names the fix: run
   `usermod --add-subuids FIRST-LAST --add-subgids FIRST-LAST agentproxy-agent-01` with a range no
   other user holds.
3. `create_config_root` creates `/etc/agent-proxy` with
   `install -d -o root -g root -m 0755 /etc/agent-proxy`. The directory must exist before the
   service user's daemon starts, because that daemon sees only the `/etc` entries present at its
   start. A daemon started before it needs a restart, which step 5 gives it.
4. `enable_user_manager` runs `loginctl enable-linger agentproxy-agent-01` and
   `systemctl start user@<uid>.service`.
5. `setup_rootless_daemon` installs and starts the rootless daemon, as the service user, with
   `/usr/bin/dockerd-rootless-setuptool.sh install`. When the daemon is already active and step 3
   created `/etc/agent-proxy`, the step restarts the daemon, as the service user, with
   `/usr/bin/systemctl --user restart docker`.
6. `prepare_images` makes the instance image `agent-proxy:1` present on the service user's daemon.
   Without `--release`, it builds two images from the checkout:
   `build --tag agent-proxy-verifier:1 core/verifier`, then
   `build --tag agent-proxy:1 --file core/docker/Dockerfile core/agent-proxy`. The verifier image
   must be on the daemon before the first call, as
   [The root verifier](architecture.md#the-root-verifier) gives. With `--release <version>`, the
   step runs `pull ghcr.io/gravital-forge/agent-proxy:<version>`, then
   `tag ghcr.io/gravital-forge/agent-proxy:<version> agent-proxy:1`, so every later step and
   teardown use the one name. That image names its verifier by digest, and the instance pulls the
   verifier when it starts.
7. `install_config` creates `/etc/agent-proxy/agent-01` with `install -d -o root -g root -m 0755`
   when it is missing, and installs the config with
   `install -o root -g root -m 0644 <config-file> /etc/agent-proxy/agent-01/config.yaml`. It skips
   the install when `cmp -s` finds the installed file the same.
8. `read_config_paths` reads `agent_tree` and `overflow_dir` from the installed config, in a
   throwaway `agent-proxy:1` container on the service user's daemon with no network, a read-only
   root filesystem, and the config directory mounted read-only. It stops when `agent_tree` is not
   the tree, or when `overflow_dir` is not inside the tree or is reached through a symbolic link.
9. `create_state_dir` creates the instance's state directory with
   `install -d -o agentproxy-agent-01 -g agentproxy-agent-01 -m 0700 <service home>/state`.
10. `create_overflow_dir` creates the overflow directory, as the agent, with
    `install -d -m 2770 <overflow_dir>`, so the agent and its group own it. It stops when that path
    is a symbolic link.
11. `apply_tree_permissions` gives the tree the group, setgid, and ACL entries that
    [The agent's tree](architecture.md#the-agents-tree) sets. It runs each command as the agent, so
    it changes only the entries the agent owns:

    ```sh
    find <tree> <closed> -o -user agent-01 ! -group <agent group> -exec chgrp -h <agent group> {} +
    find <tree> <closed> -o -type d -user agent-01 ! -perm -2000 -exec chmod g+s {} +
    find <tree> <closed> -o -type d -user agent-01 -exec setfacl -P -m u:agentproxy-agent-01:rwX,d:u:agentproxy-agent-01:rwx,d:u:agent-01:rwx {} +
    find <tree> <closed> -o ! -type d ! -type l -user agent-01 -exec setfacl -P -m u:agentproxy-agent-01:rwX {} +
    ```

    Here `<closed>` is `-type d \( ! -readable -o ! -executable \) -prune`, so the walk does not
    enter a directory the agent cannot open. The step skips each entry the agent does not own, and
    each directory it cannot open with all that lies below it. Its line gives the number of entries
    it skipped, and counts a directory it cannot open as one. An entry the service user makes
    already has the group, setgid, and default ACL entries from the setgid directory it is made in.
    Fix any other skipped entry by hand, as its owner.
12. `grant_home_traverse` gives the service user traverse on the agent's home with
    `setfacl -m u:agentproxy-agent-01:x <home>`. On a home of mode `700`, `ls -l` then shows the
    home as `drwx--x---+`, because the group bits show the ACL mask. The home's group still has no
    access: `getfacl -p <home>` shows `group::---`.
13. `warn_open_home` reads the home's ACL with `getfacl -pc <home>`, and warns about each entry that
    lets anyone but the agent and the service user traverse the home, as
    [The agent's tree](architecture.md#the-agents-tree) gives. It changes nothing on the host. Each
    warning is one line on stderr, which names the entry and the command that closes it:

    ```text
    provision-instance.sh: warning: <who> can traverse <home>; only agent-01 and agentproxy-agent-01 may; close it with '<command>'
    ```

    The entries, each with its `<who>` and its `<command>`:
    - `other`: `any other user`, closed with `chmod o-x <home>`.
    - A named user: `user <name>`, closed with `setfacl -x u:<name> <home>`.
    - A named group: `group <name>`, closed with `setfacl -x g:<name> <home>`.
    - The owning group, when it has a member other than the agent: `owning group <group>`, closed
      with `setfacl -m g::<permissions> <home>`, where `<permissions>` is the group's entry with `x`
      cleared, such as `r--`. On a home with an ACL, `chmod g-x` would clear `x` from the mask
      instead, and take the service user's traverse with it.

    With no warning, the step prints
    `checked that only agent-01 and agentproxy-agent-01 can traverse <home>`. Either way the script
    goes on to the next step. Close each entry by hand, then run the script again to see the check
    pass.
14. `grant_tree_traverse` gives the service user traverse, as the agent, on each directory strictly
    between the home and the tree, with `setfacl -m u:agentproxy-agent-01:x <directory>`. Its line
    gives the number of directories. With the tree `<home>/Projects`, there are none.
15. `start_instance` starts the instance container, named `agent-proxy`, on the service user's
    daemon, with `DOCKER_HOST` set as above:

    ```sh
    core/docker/start-instance.sh agent-proxy:1 agent-proxy <port> /etc/agent-proxy/agent-01 <service home>/state <tree>
    ```

    To put a changed port or a new image in force, remove the container first with
    `docker rm --force agent-proxy`; [The instance](architecture.md#the-instance) gives why.
    Removing the container ends any call that runs at that moment, so do it when no call runs. Such
    a call's cage stays behind until the next start removes it, as
    [Known limits](architecture.md#known-limits) gives. The port this step prints is the port that
    the operator gave, not always the one bound; `docker port agent-proxy` shows the bound one.
16. `print_next_steps` prints the commands that mint a token and store a credential, and the path of
    this guide.

## The first start

Provision with the full config. The instance starts even though its credential store is still empty.
It logs one warning for each stored credential that a grant or a provided credential names and the
store lacks, and each warning gives the command that the operator stores the credential with. It
also warns when the service user's daemon cannot enforce a cage's memory or process limit, as
[Failures and warnings](reference.md#failures-and-warnings) gives. `docker logs agent-proxy` shows
the warnings. Then:

1. Store each stored credential the config names. The command reads the value from stdin:

   ```sh
   docker exec -i agent-proxy agent-proxy credential set <name>
   ```

   [Credentials](architecture.md#credentials) gives when a stored credential takes effect. Until the
   operator stores it, a call under a grant that names it is refused, and the refusal gives the
   warning's text.

   A provided credential is never stored. Store each stored credential its entry names instead, such
   as a GitHub App's private key, which the command takes from a file the same way:

   ```sh
   docker exec -i agent-proxy agent-proxy credential set github-app-key < <key file>
   ```

   The provider runs on the first call that needs its value, not at start.
2. Mint a token for the agent with
   `docker exec agent-proxy agent-proxy token mint --name <name> --scope <scope>`. The token prints
   once; give it to the agent's MCP client. The `token mint` verb under [Verbs](reference.md#verbs)
   gives the warnings it prints.
3. Run the self-check, as [The self-check](#the-self-check) gives.

The reference gives every verb under [The operator command](reference.md#the-operator-command).

## The self-check

Run the self-check on every scope with `docker exec agent-proxy agent-proxy check`. Add
`--scope <scope>` to check one scope. Each row is one check, and a `fail` row's detail says what is
wrong, such as a stored credential that is not in the store, a root that the instance cannot open,
or an egress proxy that does not run. Fix each one, then run the check again, until every row
passes. The agent can run the same check on its own scope through the tool `agent-proxy_self_check`.

The reference gives the rows and the exit status under the `check` verb of
[The operator command](reference.md#the-operator-command), and the report under
[The self-check report](reference.md#the-self-check-report). The architecture gives what the
self-check covers under [The self-check](architecture.md#the-self-check).

## Changing the config

To put a changed config in force, install it over the installed config:

```sh
install -o root -g root -m 0644 <config-file> /etc/agent-proxy/agent-01/config.yaml
```

Then run `docker exec agent-proxy agent-proxy reload`. The `reload` verb under
[Verbs](reference.md#verbs) gives what it prints when it accepts or refuses the config.

## Teardown

Run the script from the checkout, with the same `--tree` that provisioning took:

```sh
sudo core/docker/teardown-instance.sh [--tree <path>] agent-01
```

`--tree <path>` names the agent's tree, as in provisioning. The option comes before the agent, and
the script refuses a bad command line as provisioning does.

Teardown does not read the tree from the installed config, so without `--tree` it takes the default
tree. In the usual case, provisioning took a tree outside `<home>/Projects`, that directory exists,
and the instance is still installed. Step 2 then finds the config's `overflow_dir` outside
`<home>/Projects`, so teardown stops with exit status 1 and removes nothing. Run it again with the
`--tree` that provisioning took.

The script runs the steps below in order. A step whose object is already gone says so and skips its
work, so teardown also cleans up after a provisioning that stopped part way.

1. `check_agent` reads the agent's home and the tree, and the service user's uid and home when the
   service user exists. When the tree exists, the step stops if the tree's realpath does not lie
   strictly inside the realpath of the home, as provisioning's step 1 does. It stops before any step
   removes an ACL entry.
2. `read_overflow_dir` reads `overflow_dir` from the installed config, as provisioning's
   `read_config_paths` does. It skips the read when the service user, its daemon, the
   `agent-proxy:1` image, the config, or the tree is gone. It stops on an `overflow_dir` that is not
   inside the tree or is reached through a symbolic link.
3. `remove_acls` removes the service user's entries from the tree with
   `setfacl -R -P -x u:agentproxy-agent-01 <tree>` and
   `setfacl -R -P -x d:u:agentproxy-agent-01 <tree>`. It then removes the entry from each directory
   between the home and the tree with `setfacl -x u:agentproxy-agent-01 <directory>`, and from the
   home with `setfacl -x u:agentproxy-agent-01 <home>`. When the tree is gone, the step cannot find
   those directories, and removes the entry from the home alone. Its line names each directory it
   cleared.
4. `remove_overflow_dir` removes the overflow directory that step 2 read, as the agent, with
   `rm -rf -- <overflow_dir>`. When the agent cannot remove all of it, the step warns, and teardown
   goes on; remove what is left by hand.
5. `remove_rootless_daemon` uninstalls the rootless daemon, as the service user, with
   `/usr/bin/dockerd-rootless-setuptool.sh uninstall`. It then removes the daemon's data, the
   instance container and its images included, as the service user, with
   `/usr/bin/rootlesskit rm -rf <service home>/.local/share/docker`.
6. `disable_user_manager` runs `loginctl disable-linger agentproxy-agent-01` and
   `systemctl stop user@<uid>.service`.
7. `remove_service_user` removes the service user and its home, the state directory included, with
   `userdel --remove agentproxy-agent-01`.
8. `remove_config_dir` removes `/etc/agent-proxy/agent-01` with `rm -rf`.

Teardown leaves these in place:

- the agent's own default ACL entry on the tree, `default:user:agent-01:rwx`;
- the tree's group and setgid;
- the files cages created in the tree, whose owner stays the removed service user's uid;
- the overflow directory, when step 2 skipped its read;
- the service user's entry on each directory between the home and the tree, when the tree is gone;
- the service user's entries on the tree that provisioning took, and on each directory between it
  and the home, when teardown runs without that `--tree`, the tree lies outside `<home>/Projects`,
  and step 2 skips its read. Teardown then clears `<home>/Projects` and the home, or the home alone
  when there is no `<home>/Projects`. A tree inside `<home>/Projects` is cleared with it;
- `/etc/agent-proxy` itself;
- any line for the service user in `/etc/subuid` or `/etc/subgid`. The script warns about each one;
  remove it when no user needs that range.

## Upgrading to a new release

An upgrade replaces the instance container and keeps the state directory, so the tokens, the
credentials, and the audit log stay.

1. Check out the new release's tag, so that the scripts match its images.
2. In the config, give each plugin the new digest reference from the release notes' `## Images`.
3. Remove the instance container with `docker rm --force agent-proxy`, because a new image takes
   effect only in a new container, as [The instance](architecture.md#the-instance) gives. Removing
   the container ends any call that runs at that moment, so upgrade when no call runs. Such a call's
   cage stays behind until the new container's start removes it, as
   [Known limits](architecture.md#known-limits) gives. The state directory is a host path, so
   removing the container leaves it in place.
4. Provision again with `--release <new version>`, the same `--tree`, the same port, and the updated
   config. Provisioning pulls the new instance image, tags it `agent-proxy:1`, installs the config,
   and starts a new container, which pulls its verifier and the new plugin images.
5. Run the self-check, as [The self-check](#the-self-check) gives.

An instance built from the checkout upgrades the same way, with no `--release`: provisioning builds
the verifier and instance images again from the checkout at the new tag.
