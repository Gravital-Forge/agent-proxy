FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache git git-lfs openssh-keygen python3 && git lfs install --system --skip-repo
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY scripts/git_plugin /plugin/git_plugin
COPY scripts/entry /plugin/entry
RUN ln -s /plugin/entry /usr/local/bin/git-cage \
    && mkdir /plugin/bin \
    && ln -s /plugin/entry /plugin/bin/git-credential-agent-proxy \
    && for action in add rm restore commit diff lfs_track lfs_untrack push checkout worktree config_get clone; do \
        ln -s /plugin/entry "/plugin/bin/$action"; \
    done
