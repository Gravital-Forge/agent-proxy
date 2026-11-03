# Per-agent instance runbook

This runbook takes an admin through one agent's instance: provisioning, the first start, teardown,
and the deployment acceptance checks. `docs/design.md` gives the isolation model under "Deployment
and isolation", and `docs/architecture.md` describes the instance under "The instance". The examples
use the agent `agent-01`, whose service user is `agentproxy-agent-01`.

Every step runs as root. Where a step names `<uid>`, it means the service user's uid, which
`id -u agentproxy-agent-01` prints. `<home>` is the agent's home, `<tree>` is the resolved path of
the agent's `Projects` tree, and `<service home>` is the service user's home.

## Before you start

The host needs these packages:

- Docker Engine with `docker-ce-rootless-extras`, which gives the service user its rootless daemon.
- `uidmap`, which gives the rootless daemon its subordinate uid and gid ranges.
- `dbus-user-session`, which the service user's systemd user manager needs.
- `acl`, which gives `setfacl` and `getfacl`.
- systemd, which keeps the service user's daemon running under linger.

Root must be able to read a checkout of this repository. The scripts run from it, and the
provisioning script builds the verifier and instance images from it.

Push each plugin image to a registry that the service user's daemon can pull from. Each plugin line
in the config names its image by digest.

## The config

Start from `examples/instance.yaml`. `docs/design.md` gives the rules a config must satisfy under
"Config".

- `agent_tree` names the resolved path of the agent's `Projects` tree, the path that
  `realpath /home/agent-01/Projects` prints. The runner refuses a root or an overflow directory
  under a symbolic link, and provisioning refuses any other value.
- `overflow_dir` names a directory inside the tree, by a path with no symbolic link in it. Root
  creates and removes that directory, so both scripts refuse any other path.
- Every root names a directory below the tree, never the tree itself.
- Every grant that holds `github-token` sets `GITHUB_REPOS` in its `env`, or the plugin refuses each
  call under that grant. `plugins/git/README.md` gives the list's form under "The repository list".
- Every grant that holds `hcp-terraform-token` sets `HCP_TERRAFORM_WORKSPACES` in its `env`, or the
  plugin refuses each call under that grant. `plugins/hcp-terraform/README.md` gives the list's form
  under "The workspace list".
- A grant that signs commits gives the signing key as a file credential, and sets `GIT_SIGNING_KEY`
  in its `env` to that file's path. Store the key as "The first start" stores each credential.
  `plugins/git/README.md` gives the setting under "Commit signing".

## Provisioning

Run the script from the checkout:

```sh
sudo core/docker/provision-instance.sh agent-01 <port> <config-file>
```

`<port>` is the loopback port the instance serves on, and `<config-file>` is the config to install.
Read "The first start" below before you choose the config.

The script runs the steps below in order, and reports each step as it runs it. Each step leaves its
object alone when it is already in place, so a second run with the same config changes nothing.
Every run installs `<config-file>` over the installed config, so once "The first start" has put the
full config in place, run the script with the full config. A step that fails stops the script; after
the fix, run the script again from the top. To debug a step, run its commands by hand.

Three phrases below stand for the way the script reaches the service user and the agent:

- "As the service user" means through
  `systemd-run --machine=agentproxy-agent-01@.host --user --wait --pipe --quiet <command>`.
- "On the service user's daemon" means through
  `DOCKER_HOST=unix:///run/user/<uid>/docker.sock docker <arguments>`.
- "As the agent" means through `runuser -u agent-01 -- <command>`, run from `/`. The agent can swap
  any path inside the tree for a symbolic link, so a step that creates or grants in the tree as root
  could reach a file outside it. Such steps run as the agent instead.

1. `check_arguments` checks that the agent exists, that the service user's name is at most 32
   characters, that the port is an integer from 1 to 65535, that the config file is a readable
   regular file, and that `<home>/Projects` is a directory. It reads the agent's home, the tree as
   `realpath -- <home>/Projects`, and the agent's group as `id -gn agent-01`.
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
6. `build_images` builds two images on the service user's daemon, from the checkout:
   `build --tag agent-proxy-verifier:1 core/verifier`, then
   `build --tag agent-proxy:1 --file core/docker/Dockerfile core/agent-proxy`. The verifier image is
   built before the instance starts because an image mount never pulls: without it, no cage starts.
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
11. `apply_tree_permissions` gives the tree the group, setgid, and ACL entries that `docs/design.md`
    sets under "Deployment and isolation". It runs each command as the agent, so it changes only the
    entries the agent owns:

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
    `setfacl -m u:agentproxy-agent-01:x <home>`. After it, `ls -l` shows the home as `drwx--x---+`,
    because the group bits show the ACL mask. The home's group still has no access:
    `getfacl -p <home>` shows `group::---`.
13. `start_instance` starts the instance container, named `agent-proxy`, on the service user's
    daemon, with `DOCKER_HOST` set as above:

    ```sh
    core/docker/start-instance.sh agent-proxy:1 agent-proxy <port> /etc/agent-proxy/agent-01 <service home>/state <tree>
    ```

    When the container already exists, the start script starts it as it was created, so a changed
    port takes effect only after you remove the container. The port this step prints is the one you
    asked for, not always the one bound; `docker port agent-proxy` on the service user's daemon
    shows the bound one.
14. `print_next_steps` prints the commands that mint a token and store a credential, and the path of
    this runbook.

## The first start

The instance does not start from a config that names a credential its store lacks, and the store is
empty until the instance runs. So provision with a first config that names no credential: the full
config with every grant that names a credential removed. Then:

1. Store each credential the full config names. The command reads the value from stdin:

   ```sh
   docker exec -i agent-proxy agent-proxy credential set <name>
   ```

2. Install the full config over the first one:

   ```sh
   install -o root -g root -m 0644 <full-config> /etc/agent-proxy/agent-01/config.yaml
   ```

3. Put the full config in force with `docker exec agent-proxy agent-proxy reload`. If it refuses, it
   prints each error, and the running config stays.
4. Mint a token for the agent with
   `docker exec agent-proxy agent-proxy token mint --name <name> --scope <scope>`. The token prints
   once; give it to the agent's MCP client.

Every `docker` command here runs as root with `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`.
`docs/architecture.md` gives every verb of the admin command under "The admin command".

## Teardown

Run the script from the checkout:

```sh
sudo core/docker/teardown-instance.sh agent-01
```

The script runs the steps below in order. A step whose object is already gone says so and skips its
work, so teardown also cleans up after a provisioning that stopped part way.

1. `check_agent` reads the agent's home and the tree, and the service user's uid and home when the
   service user exists.
2. `read_overflow_dir` reads `overflow_dir` from the installed config, as provisioning's
   `read_config_paths` does. It skips the read when the service user, its daemon, the
   `agent-proxy:1` image, the config, or the tree is gone. It stops on an `overflow_dir` that is not
   inside the tree or is reached through a symbolic link.
3. `remove_acls` removes the service user's entries from the tree with
   `setfacl -R -P -x u:agentproxy-agent-01 <tree>` and
   `setfacl -R -P -x d:u:agentproxy-agent-01 <tree>`, and from the home with
   `setfacl -x u:agentproxy-agent-01 <home>`.
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
- `/etc/agent-proxy` itself;
- any line for the service user in `/etc/subuid` or `/etc/subgid`. The script warns about each one;
  remove it when no user needs that range.

## Deployment acceptance checks

`docs/design.md` lists the design's checks under "Deployment acceptance checks". A check that tests
one plugin's own setting cites that plugin's README instead. `.dev/verification.md` says why every
check runs here and never in the project. Every `docker` command below runs as root with
`DOCKER_HOST=unix:///run/user/<uid>/docker.sock`.

1. **Provisioning twice.** On a clean host, provision once. Then capture four records:

   ```sh
   getfacl -R -P -p <tree> > tree-acl.before
   getfacl -p <home> > home-acl.before
   docker inspect --format '{{.Id}}' agent-proxy > container.before
   stat -c '%i %Y' /etc/agent-proxy/agent-01/config.yaml > config.before
   ```

   Run the provisioning script again with the same arguments and the config installed now, capture
   the same four records into `.after` files, and `diff` each pair. The check passes when every
   `diff` prints nothing.
2. **A cage writes the tree.** As the agent, create a new file in a repository below the tree.
   Through the agent's MCP client, call `git_add` with that file; the call writes the repository's
   `.git/index` anew. Then run `stat -c '%U %G %a' <repo>/.git/index` and
   `getfacl -p <repo>/.git/index`. The check passes when the owner is `agentproxy-agent-01`, the
   group is the agent's group, and `getfacl` gives `agent-01` read and write after the mask.
3. **A second agent cannot read created files.** Take a second agent user, `<second agent>` below,
   and the file from check 2.
   1. `sudo -u <second agent> cat <repo>/.git/index` fails with permission denied.
   2. Grant the second agent traverse on the home with `setfacl -m u:<second agent>:x <home>`. The
      same `cat` still fails; this is check `S11`.
   3. Remove that entry with `setfacl -x u:<second agent> <home>`.
4. **The agent cannot reach the service user's socket.**
   `sudo -u agent-01 docker -H unix:///run/user/<uid>/docker.sock version` fails with permission
   denied.
5. **`H1` to `H4` and the cage cost.** Run the core package's `tests/integration/test_runner.py` as
   the service user against its own daemon. The service user needs uv on the `PATH` that `sudo`
   gives it, and its own copy of the checkout, because uv writes the package's environment inside
   it:

   ```sh
   cp -r <checkout> <service home>/agent-proxy
   chown -R agentproxy-agent-01:agentproxy-agent-01 <service home>/agent-proxy
   cd <service home>/agent-proxy/core/agent-proxy
   sudo -u agentproxy-agent-01 env HOME=<service home> DOCKER_HOST=unix:///run/user/<uid>/docker.sock uv run pytest --showlocals tests/integration/test_runner.py
   ```

   The check passes when every test passes and, afterwards, the label checks in
   `.dev/verification.md` print nothing on the service user's daemon. Read the cage-cost median from
   the `MEASURE cage-cost` line the run prints. `spike/FINDINGS.md` gives what `H1` to `H4`, `C1`,
   and `C2` each check, and the cost the spike measured. Remove the copy with
   `rm -rf <service home>/agent-proxy`.
6. **`E12` with a real credential.** Store a real `github-token`, as "The first start" does, and
   give a `git.fetch` grant shaped like the `git.push` grant in `examples/instance.yaml`. Through
   the agent's MCP client, call `git_fetch` on a repository below the tree whose remote is a private
   repository that the grant's `GITHUB_REPOS` names. The check passes when the result's `exit_code`
   is 0.
7. **A run's log with a real credential.** With a real `github-token` stored, give a
   `github.run_view` grant that names `api.github.com` and the storage host GitHub redirects the log
   fetch to, and that sets `GITHUB_REPOS` in its `env`; `plugins/github/README.md` gives
   `api.github.com` and one example storage host under "Egress", and says a grant must name each
   storage host one by one. Through the agent's MCP client, call `github_run_view` with `log_failed`
   on a failed run of a repository that the grant's `GITHUB_REPOS` names. The check passes when the
   result holds the run's log.
8. **The repository list with a real credential.** With a real `github-token` stored, take two
   GitHub repositories the token can push to, `<listed>` and `<unlisted>`, each as `owner/name`.
   Give the agent a checkout of each below the tree, whose `origin` is that repository, and open a
   pull request on `<listed>`. Give `git.push` and `github.pr_view` grants shaped like those in
   `examples/instance.yaml`, with `GITHUB_REPOS: "<listed>"`. The `git.push` grant's `roots` name
   both checkouts with mode `rw`, and its `domains` also name each host GitHub's LFS server sends
   objects to, as `plugins/git/README.md` gives under "A grant". Put the config in force as "The
   first start" does.
   1. As the agent, create a branch under the grant's prefix in each checkout and commit one change
      on it, for example with `git -C <checkout> checkout -b task/list-check`.
   2. Through the agent's MCP client, call `git_push` with `repo` set to the `<listed>` checkout.
      The result's `exit_code` is 0.
   3. As the agent, track one new file with LFS in the `<listed>` checkout and commit it, for
      example with `git -C <checkout> lfs track '*.bin'`, then a commit of `.gitattributes` and a
      new `.bin` file. Call `git_push` with `repo` set to the `<listed>` checkout. The result's
      `exit_code` is 0. This shows that a real GitHub LFS upload and verify never ask the credential
      helper about a path outside the list.
   4. Call `git_push` with `repo` set to the `<unlisted>` checkout. The result's `exit_code` is not
      0, and its `stderr` holds
      `git plugin refused: repository '<unlisted>' is not in GITHUB_REPOS`.
   5. Call `github_pr_view` with `repo` set to `<listed>` and `pr` set to the number of that pull
      request. The result's `exit_code` is 0.
   6. Call `github_pr_view` with `repo` set to `<unlisted>` and `pr` set to any number. The result's
      `exit_code` is 2, and its `stderr` holds
      `github plugin refused: repository '<unlisted>' is not in GITHUB_REPOS`.

   The check passes when each result is as given. Afterwards, delete the branch `task/list-check`
   from `<listed>` on GitHub.
9. **A signed commit with a real key.** Make a key pair with no passphrase, and store its private
   half as the credential `git-signing-key`, as "The first start" stores `github-token`:

   ```sh
   ssh-keygen -q -t ed25519 -N "" -C agent-01-signing -f <key dir>/signing-key
   docker exec -i agent-proxy agent-proxy credential set git-signing-key < <key dir>/signing-key
   ```

   Upload `<key dir>/signing-key.pub` to a GitHub account as a signing key. Then delete
   `<key dir>/signing-key`, because the instance's store holds the key now. With a real
   `github-token` stored, take a checkout below the tree whose `origin` is a GitHub repository the
   token can push to. Give a `git.commit` grant and a `git.push` grant whose `roots` name that
   checkout with mode `rw`:

   - The `git.commit` grant gives `git-signing-key` as `{file: /run/credentials/git-signing-key}`,
     and sets `GIT_SIGNING_KEY: /run/credentials/git-signing-key` in its `env`.
     `plugins/git/README.md` gives the setting under "Commit signing".
   - The committer email that `commit` takes is a verified email of the account that holds the
     signing key. `plugins/git/README.md` gives where `commit` takes it from under "Commit
     identity".
   - The `git.push` grant is shaped like the one in `examples/instance.yaml`, and its `GITHUB_REPOS`
     names the repository.

   Put the config in force as "The first start" does.

   1. As the agent, create a branch under the `git.push` grant's prefix in the checkout, and stage
      one change on it, for example with `git -C <checkout> checkout -b task/signing-check` and
      `git -C <checkout> add <file>`.
   2. Through the agent's MCP client, call `git_commit` with `repo` set to the checkout and
      `message` set to `Check commit signing`. The result's `exit_code` is 0.
   3. Call `git_push` with `repo` set to the checkout. The result's `exit_code` is 0.

   The check passes when the commit's page on GitHub shows Verified. Afterwards, delete the branch
   `task/signing-check` on GitHub.

10. **Attachments with a real credential.** With a real `github-token` stored, take a private
    repository of a GitHub organization that the token can push to, as `owner/name`, and a checkout
    of it below the tree. Open a scratch pull request on it, and note its number. Give a
    `github.pr_edit` grant shaped like the one in `examples/instance.yaml`: its `roots` name a
    directory below the tree, `<root>` below, with mode `ro`; its `domains` name `api.github.com`
    and `uploads.github.com`; its `GITHUB_REPOS` names the repository; and its `env` sets
    `GITHUB_PR_ATTACHMENTS: "on"`. `plugins/github/README.md` gives each member under "Attachments".
    Put the config in force as "The first start" does.

    1. As the agent, create a worktree of the checkout below `<root>`, for example with
       `git -C <checkout> worktree add <root>/attach-check`. As the agent, write two files in it: a
       PNG, `proof.png`, and an H.264 MP4, `proof.mp4`.
    2. Through the agent's MCP client, call `github_pr_edit` with `repo` set to the repository,
       `pr_number` set to the scratch pull request's number, `attach` naming
       `<root>/attach-check/proof.png` and `<root>/attach-check/proof.mp4`, and `body` set to:

       ```markdown
       ![screenshot](<root>/attach-check/proof.png)

       ![](<root>/attach-check/proof.mp4)
       ```

       The result's `exit_code` is 0, and its `pr_number` is the scratch pull request's number.
    3. As the operator, open the pull request's page on GitHub. The image shows, and the video plays
       in the page.
    4. Set the grant's `GITHUB_PR_ATTACHMENTS` to `"off"`, and put the config in force again. Make
       the call of step 2 again. The result's `exit_code` is 2, and its `stderr` holds
       `github plugin refused: attach is off: GITHUB_PR_ATTACHMENTS is 'off'`.

    The check passes when each result is as given and both files render in step 3. Afterwards, close
    the scratch pull request, and remove the worktree with
    `git -C <checkout> worktree remove <root>/attach-check`.

11. **The workspace list with a real token.** Store a real HCP Terraform API token as the credential
    `hcp-terraform-token`, as "The first start" stores each credential. Take two workspaces the
    token can read, `<listed>` and `<unlisted>`, each as `organization/name`, and one run of each
    whose plan has finished, `<listed run>` and `<unlisted run>`. Pick `<listed run>` from the
    workspace's 20 newest runs. Give `hcp-terraform.run_list` and `hcp-terraform.plan_log` grants
    shaped like those in `examples/instance.yaml`, with `HCP_TERRAFORM_WORKSPACES: "<listed>"`. Put
    the config in force as "The first start" does.

    1. Through the agent's MCP client, call `hcp-terraform_run_list` with `organization` and
       `workspace` set to those of `<listed>`, and `limit` set to 20. The result's `exit_code` is 0,
       and its `runs` holds `<listed run>`.
    2. Call `hcp-terraform_run_list` with `organization` and `workspace` set to those of
       `<unlisted>`. The result's `exit_code` is 2, and its `stderr` holds
       `hcp-terraform plugin refused: workspace '<unlisted>' is not in HCP_TERRAFORM_WORKSPACES`.
    3. Call `hcp-terraform_plan_log` with `run_id` set to `<listed run>`. The result's `exit_code`
       is 0, and its `log` reads as the plan's output on the run's page in HCP Terraform, with no
       stray control characters. This confirms the log format that `plugins/hcp-terraform/README.md`
       gives under "The actions".
    4. Call `hcp-terraform_plan_log` with `run_id` set to `<unlisted run>`. The result's `exit_code`
       is 2, and its `stderr` holds the refusal of step 2.

    The check passes when each result is as given.
