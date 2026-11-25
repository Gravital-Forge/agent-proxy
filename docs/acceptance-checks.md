# Acceptance checks

An acceptance check is a check that needs root, a second user, or a real credential. The project's
tests need no root, no second user, and no real credential, so they cannot run these checks. The
operator runs them on a host, against one agent's instance provisioned as
[the deployment guide](deployment.md) gives. The checks use the agent `agent-01`, its service user
`agentproxy-agent-01`, and the placeholders that the guide's opening sets. Every `docker` command
below runs as root with `DOCKER_HOST=unix:///run/user/<uid>/docker.sock`.

Each check opens with what it shows. A check that tests one plugin's own setting cites that plugin's
README.

1. **Provisioning twice.** This check shows that a second run of the provisioning script changes
   nothing. On a clean host, provision once. Then capture four records:

   ```sh
   getfacl -R -P -p <tree> > tree-acl.before
   getfacl -p <home> > home-acl.before
   docker inspect --format '{{.Id}}' agent-proxy > container.before
   stat -c '%i %Y' /etc/agent-proxy/agent-01/config.yaml > config.before
   ```

   Run the provisioning script again with the same arguments and the config installed now, capture
   the same four records into `.after` files, and `diff` each pair. The check passes when every
   `diff` prints nothing.
2. **A cage writes the tree.** This check shows that a cage on the service user's daemon writes the
   agent's tree with the owner, group, and mode that
   [The agent's tree](architecture.md#the-agents-tree) gives. As the agent, create a new file in a
   repository below the tree. Through the agent's MCP client, call `git_add` with that file; the
   call writes the repository's `.git/index` anew. Then run `stat -c '%U %G %a' <repo>/.git/index`
   and `getfacl -p <repo>/.git/index`. The check passes when the owner is `agentproxy-agent-01`, the
   group is the agent's group, and `getfacl` gives `agent-01` read and write after the mask.
3. **A second agent cannot read created files.** This check shows that a second agent cannot read a
   cage-made file, even with traverse on the agent's home: the file's own barrier holds apart from
   the home's. Take a second agent user, `<second agent>` below, and the file from check 2.
   1. `sudo -u <second agent> cat <repo>/.git/index` fails with permission denied.
   2. Grant the second agent traverse on the home with `setfacl -m u:<second agent>:x <home>`. The
      same `cat` still fails, because traverse on the home does not expose a cage-made file.
   3. Remove that entry with `setfacl -x u:<second agent> <home>`.
4. **The agent cannot reach the service user's socket.** This check shows that the agent cannot
   reach the service user's Docker socket, so it cannot start a container that skips its grants.
   `sudo -u agent-01 docker -H unix:///run/user/<uid>/docker.sock version` fails with permission
   denied.
5. **The cage hardening and the cage cost.** This check shows that the cage hardening holds on the
   service user's daemon, and it measures the cage cost there. Run the core package's
   `tests/integration/test_runner.py` as the service user against its own daemon. The service user
   needs uv on the `PATH` that `sudo` gives it, and its own copy of the checkout, because uv writes
   the package's environment inside it:

   ```sh
   cp -r <checkout> <service home>/agent-proxy
   chown -R agentproxy-agent-01:agentproxy-agent-01 <service home>/agent-proxy
   cd <service home>/agent-proxy/core/agent-proxy
   sudo -u agentproxy-agent-01 env HOME=<service home> DOCKER_HOST=unix:///run/user/<uid>/docker.sock uv run pytest --showlocals tests/integration/test_runner.py
   ```

   The check passes when every test passes and, afterwards, the label checks in the contributing
   guide under [What a test leaves behind](../CONTRIBUTING.md#what-a-test-leaves-behind) print
   nothing on the service user's daemon. Read the cage-cost median from the `MEASURE cage-cost` line
   the run prints. `test_the_cage_passes_the_hardening_probes` checks that the cage's root
   filesystem refuses a write, `/tmp` takes one, the cage holds no effective capability, and
   `no_new_privs` is set. Remove the copy with `rm -rf <service home>/agent-proxy`.
6. **Git over HTTPS with a real credential.** This check shows that git over HTTPS, through the
   credential helper, reaches a private repository. Store a real `github-token`, as
   [The first start](deployment.md#the-first-start) does, and give a `git.fetch` grant shaped like
   the `git.push` grant in `examples/instance.yaml`. Through the agent's MCP client, call
   `git_fetch` on a repository below the tree whose remote is a private repository that the grant's
   `GITHUB_REPOS` names. The check passes when the result's `exit_code` is 0.
7. **A run's log with a real credential.** This check shows that, with a real credential, the github
   plugin's `run_view` with `log_failed` returns a failed run's log under a grant that names the
   storage host GitHub redirects the fetch to. With a real `github-token` stored, give a
   `github.run_view` grant that names `api.github.com` and the storage host GitHub redirects the log
   fetch to, and that sets `GITHUB_REPOS` in its `env`. The github plugin's
   [Egress](../plugins/github/README.md#egress) gives `api.github.com` and one example storage host,
   and says a grant must name each storage host one by one. Through the agent's MCP client, call
   `github_run_view` with `log_failed` on a failed run of a repository that the grant's
   `GITHUB_REPOS` names. The check passes when the result holds the run's log.
8. **The repository list with a real credential.** This check shows that, with a real credential and
   grants whose `GITHUB_REPOS` names one repository, the git plugin's `push` and the github plugin's
   `pr_view` succeed on that repository, and each is refused on a repository the token reaches that
   the list does not name. With a real `github-token` stored, take two GitHub repositories the token
   can push to, `<listed>` and `<unlisted>`, each as `owner/name`. Give the agent a checkout of each
   below the tree, whose `origin` is that repository, and open a pull request on `<listed>`. Give
   `git.push` and `github.pr_view` grants shaped like those in `examples/instance.yaml`, with
   `GITHUB_REPOS: "<listed>"`. The `git.push` grant's `roots` name both checkouts with mode `rw`,
   and its `domains` also name each host GitHub's LFS server sends objects to, as the git plugin's
   [A grant](../plugins/git/README.md#a-grant) gives. Put the config in force as
   [Changing the config](deployment.md#changing-the-config) gives.
   1. As the agent, create a branch under the grant's prefix in each checkout and commit one change
      on it, for example with `git -C <checkout> checkout -b task/list-check`.
   2. Through the agent's MCP client, call `git_push` with `repo` set to the `<listed>` checkout.
      The result's `exit_code` is 0.
   3. As the agent, track one new file with LFS in the `<listed>` checkout and commit it, for
      example with `git -C <checkout> lfs track '*.bin'`, then a commit of `.gitattributes` and a
      new `.bin` file. Call `git_push` with `repo` set to the `<listed>` checkout. The result's
      `exit_code` is 0. This shows that a real GitHub LFS upload and verify never ask the credential
      helper about a path outside the list. It also shows that they ask the helper only for `https`
      and `github.com`, the one protocol and host it answers, as the git plugin's
      [The credential helper](../plugins/git/README.md#the-credential-helper) gives.
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
9. **A signed commit with a real key.** This check shows that a commit the git plugin's `commit`
   signs with a real key, and `push` sends, is Verified on GitHub. Make a key pair with no
   passphrase, and store its private half as the credential `git-signing-key`, as
   [The first start](deployment.md#the-first-start) stores each credential:

   ```sh
   ssh-keygen -q -t ed25519 -N "" -C agent-01-signing -f <key dir>/signing-key
   docker exec -i agent-proxy agent-proxy credential set git-signing-key < <key dir>/signing-key
   ```

   Upload `<key dir>/signing-key.pub` to a GitHub account as a signing key. Then delete
   `<key dir>/signing-key`, because the instance's store holds the key now. With a real
   `github-token` stored, take a checkout below the tree whose `origin` is a GitHub repository the
   token can push to. Give a `git.commit` grant and a `git.push` grant whose `roots` name that
   checkout with mode `rw`:

   - The `git.commit` grant gives `git-signing-key` as `{file: /run/credentials/git-signing-key}`.
     Its `env` sets `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, and
     `GIT_SIGNING_KEY: /run/credentials/git-signing-key`. The git plugin's
     [Commit signing](../plugins/git/README.md#commit-signing) gives the setting.
   - The committer email that `commit` takes is a verified email of the account that holds the
     signing key. The git plugin's [Commit identity](../plugins/git/README.md#commit-identity) gives
     where `commit` takes it from.
   - The `git.push` grant is shaped like the one in `examples/instance.yaml`, and its `GITHUB_REPOS`
     names the repository.

   Put the config in force as [Changing the config](deployment.md#changing-the-config) gives.

   1. As the agent, create a branch under the `git.push` grant's prefix in the checkout, and stage
      one change on it, for example with `git -C <checkout> checkout -b task/signing-check` and
      `git -C <checkout> add <file>`.
   2. Through the agent's MCP client, call `git_commit` with `repo` set to the checkout and
      `message` set to `Check commit signing`. The result's `exit_code` is 0.
   3. Call `git_push` with `repo` set to the checkout. The result's `exit_code` is 0.

   The check passes when the commit's page on GitHub shows Verified. Afterwards, delete the branch
   `task/signing-check` on GitHub.

10. **Attachments with a real credential.** This check shows that, with a real credential and
    `GITHUB_PR_ATTACHMENTS` on, the github plugin's `pr_edit` attaches a PNG and an H.264 MP4 to a
    scratch pull request in a private organization repository, and both render for the operator on
    the pull request's page. The agent writes both files itself, in a worktree below the grant's
    root, so the check also shows that the cage reads the agent's own files there. With the setting
    `off`, the same call is refused.

    With a real `github-token` stored, take a private repository of a GitHub organization that the
    token can push to, as `owner/name`, and a checkout of it below the tree. Open a scratch pull
    request on it, and note its number. Give a `github.pr_edit` grant shaped like the one in
    `examples/instance.yaml`: its `roots` name a directory below the tree, `<root>` below, with mode
    `ro`; its `domains` name `api.github.com` and `uploads.github.com`; its `GITHUB_REPOS` names the
    repository; and its `env` sets `GITHUB_PR_ATTACHMENTS: "on"`. The github plugin's
    [Attachments](../plugins/github/README.md#attachments) gives each member. Put the config in
    force as [Changing the config](deployment.md#changing-the-config) gives.

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

11. **The workspace list with a real token.** This check shows that, with a real token and a grant
    whose `HCP_TERRAFORM_WORKSPACES` names one workspace, the hcp-terraform plugin's `run_list` and
    `plan_log` succeed on that workspace, and each is refused on another workspace the token
    reaches.

    Store a real HCP Terraform API token as the credential `hcp-terraform-token`, as
    [The first start](deployment.md#the-first-start) stores each credential. Take two workspaces the
    token can read, `<listed>` and `<unlisted>`, each as `organization/name`, and one run of each
    whose plan has finished, `<listed run>` and `<unlisted run>`. Pick `<listed run>` from the
    workspace's 20 newest runs. Give `hcp-terraform.run_list` and `hcp-terraform.plan_log` grants
    shaped like those in `examples/instance.yaml`, with `HCP_TERRAFORM_WORKSPACES: "<listed>"`. Put
    the config in force as [Changing the config](deployment.md#changing-the-config) gives.

    1. Through the agent's MCP client, call `hcp-terraform_run_list` with `organization` and
       `workspace` set to those of `<listed>`, and `limit` set to 20. The result's `exit_code` is 0,
       and its `runs` holds `<listed run>`.
    2. Call `hcp-terraform_run_list` with `organization` and `workspace` set to those of
       `<unlisted>`. The result's `exit_code` is 2, and its `stderr` holds
       `hcp-terraform plugin refused: workspace '<unlisted>' is not in HCP_TERRAFORM_WORKSPACES`.
    3. Call `hcp-terraform_plan_log` with `run_id` set to `<listed run>`. The result's `exit_code`
       is 0, and its `log` reads as the plan's output on the run's page in HCP Terraform, with no
       stray control characters. This confirms the log format that the hcp-terraform plugin's
       [The actions](../plugins/hcp-terraform/README.md#the-actions) gives.
    4. Call `hcp-terraform_plan_log` with `run_id` set to `<unlisted run>`. The result's `exit_code`
       is 2, and its `stderr` holds the refusal of step 2.

    The check passes when each result is as given.

12. **Release mode.** This check shows that an instance provisioned in release mode runs on the
    published images: the instance image names its verifier by digest, the service user's daemon
    holds the verifier at that digest, and a call succeeds through it. Take a clean host and a
    release that the project's maintainers have published, whose packages are all public on GHCR.
    Check out the release's tag, and provision with `--release <version>` and the full config. Do
    [The first start](deployment.md#the-first-start). Then read the verifier's reference from the
    instance image:

    ```sh
    docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' agent-proxy:1
    ```

    Its `AGENT_PROXY_VERIFIER_IMAGE` line names `<reference>`, which has the form
    `ghcr.io/gravital-forge/agent-proxy-verifier@sha256:<digest>`. Run
    `docker image inspect --format '{{json .RepoDigests}}' <reference>`, then call any tool the
    token grants through the agent's MCP client. The check passes when that `inspect` prints a list
    that holds `<reference>`, and the call succeeds.
    [The root verifier](architecture.md#the-root-verifier) gives the verifier's pin.

13. **The open-home warning on a real home.** This check shows that the provisioning step
    `warn_open_home` reads the real output of `getfacl -pc` and the host's real group lookups. The
    step's tests give it fixed output, so only a real home shows this.
    [Provisioning](deployment.md#provisioning) gives the step and its two outputs.

    Take a host where the step found the agent's home closed at provisioning, and where the home's
    owning group has no member but the agent. `stat -c %G <home>` prints that group, `<group>`
    below. Take a second user, `<second user>` below, who is neither the agent nor the service user.
    Each step below runs the provisioning step alone, as root, with this command:

    ```sh
    bash -c 'source "$1"; agent=agent-01 service_user=agentproxy-agent-01 agent_home=<home>; warn_open_home' _ <checkout>/core/docker/provision-instance.sh
    ```

    The script runs none of its own steps when it is sourced, so the command runs `warn_open_home`
    and nothing else. In each step below, the command exits 0.

    1. Run the command on the home as provisioning left it. It prints nothing on stderr, and this
       line on stdout:

       ```text
       checked that only agent-01 and agentproxy-agent-01 can traverse <home>
       ```

    2. Give the second user traverse with `setfacl -m u:<second user>:x <home>`. Run the command. It
       prints nothing on stdout, and this line on stderr:

       ```text
       provision-instance.sh: warning: user <second user> can traverse <home>; only agent-01 and agentproxy-agent-01 may; close it with 'setfacl -x u:<second user> <home>'
       ```

       Close the entry with the command that the warning names.
    3. Note the owning group's entry, the `group::` line that `getfacl -p <home>` prints, as
       `<group permissions>`. Give the owning group traverse with `setfacl -m g::--x <home>`, and
       run the command. It prints the line of step 1, because the group has no member but the agent.
       Add the second user to the group with `gpasswd -a <second user> <group>`, and run the command
       again. It prints nothing on stdout, and this line on stderr:

       ```text
       provision-instance.sh: warning: owning group <group> can traverse <home>; only agent-01 and agentproxy-agent-01 may; close it with 'setfacl -m g::--- <home>'
       ```

       Remove the second user from the group with `gpasswd -d <second user> <group>`. Put back the
       owning group's entry with `setfacl -m g::<group permissions> <home>`.

    The check passes when each output is as given.

14. **A provided token from a real GitHub App.** This check shows that the github-app provider mints
    an installation token from a real App, that a call's cage receives it, and that a second call
    takes it from the cache. Take a GitHub App installed on an account, its private key file,
    `<key file>` below, its App ID, and the installation's ID. Take a private repository that the
    installation reaches with read access to its contents, `<repo>` below, as `owner/name`, and a
    checkout of it below the tree whose `origin` is that repository.

    Store the key as the credential `github-app-key`, as
    [The first start](deployment.md#the-first-start) gives. In the config, pin the github-app
    plugin's image, and give a `provided_credentials` entry shaped like the one in
    `examples/instance.yaml`, with the App's ID and the installation's ID.
    [A provided credential](../plugins/github-app/README.md#a-provided-credential) gives the entry.
    Give a `git.fetch` grant whose `roots` name the checkout, whose `domains` name `github.com`,
    whose `credentials` give that provided credential as `GH_TOKEN`, and whose `GITHUB_REPOS` names
    `<repo>`. Put the config in force as [Changing the config](deployment.md#changing-the-config)
    gives.

    1. Run `docker exec agent-proxy agent-proxy check`. The `credential` row of the provided
       credential passes, and its detail begins `provided by 'github-app.installation_token'`.
    2. Through the agent's MCP client, call `git_fetch` on the checkout. The result's `exit_code` is
       0.
    3. Run `docker exec agent-proxy agent-proxy audit tail -n 2`. The first line's `action` is
       `github-app_installation_token`, its `credential` names the provided credential, its
       `exit_code` is 0, and its `refusal` is `null`. The second line's `action` is `git_fetch`.
    4. Call `git_fetch` on the checkout again. The result's `exit_code` is 0. Run
       `docker exec agent-proxy agent-proxy audit tail -n 2` again. Its last line is the new
       `git_fetch` line, and the line before it is the first `git_fetch` line, so no provider ran.

    The check passes when each result is as given.

15. **A clone of a private repository with a provided token.** This check shows that the git
    plugin's `clone` makes a working checkout of a private repository with the token of check 14,
    and that later git actions accept that checkout. Take the config of check 14, and as the agent,
    make an empty directory below the tree, `<clone dir>` below. Give a `git.clone` grant shaped
    like the one in `examples/instance.yaml`, whose `roots` name `<clone dir>` with mode `rw` and
    whose `GITHUB_REPOS` names `<repo>`, as [`clone`](../plugins/git/README.md#clone) gives. Give a
    `git.status` grant whose `roots` name `<clone dir>` with mode `rw`. Put the config in force as
    [Changing the config](deployment.md#changing-the-config) gives.

    1. Through the agent's MCP client, call `git_clone` with `repo` set to `<repo>` and `path` set
       to `<clone dir>`. The result's `exit_code` is 0.
    2. Call `git_status` with `repo` set to `<clone dir>`. The result's `exit_code` is 0, and its
       `stdout` says the branch is the repository's default branch and is up to date with origin's
       branch of that name.

    The check passes when each result is as given. Afterwards, remove `<clone dir>`, as the agent,
    with `rm -rf <clone dir>`.
