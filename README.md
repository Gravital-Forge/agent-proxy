# agent-proxy

agent-proxy is a per-agent MCP proxy. An agent calls a tool with a token minted for one scope, and
the proxy runs the scoped, credentialed action in a short-lived container. That container holds only
what the scope grants the action: files, domains, credentials, and plain environment variables. The
credentials stay in the container, so the agent never holds one.

Work lands on `main` through pull requests the operator merges.

- `docs/design.md` is the design: the operator's closed decisions and the reasons behind them.
- `docs/architecture.md` describes what is built, and cites the code by path.
- `docs/runbooks/per-agent-instance.md` tells an admin how to provision, start, and tear down one
  agent's instance, and how to run the deployment acceptance checks.
- `examples/instance.yaml` is a sample instance config.
- `docs/writing-a-plugin.md` tells a plugin author what the image holds, and how to write and check
  its manifest.
- `plugins/git/README.md` describes the git plugin, which runs git and git LFS in a cage.
- `plugins/github/README.md` describes the github plugin, which runs gh in a cage.
- `plugins/hcp-terraform/README.md` describes the hcp-terraform plugin, which reads HCP Terraform
  runs and their logs in a cage.
- `docs/roadmap.md` is the ordered list of work items that build the proof of concept.
- `docs/pipeline/index.md` is how baton sessions build it, one job at a time.
- `spike/FINDINGS.md` is the dated record of the spike that tested the design's load-bearing claims.
