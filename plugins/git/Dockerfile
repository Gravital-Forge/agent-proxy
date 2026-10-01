FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache git git-lfs python3 && git lfs install --system --skip-repo
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY scripts/git_plugin /plugin/git_plugin
COPY scripts/entry /plugin/entry
RUN ln -s /plugin/entry /usr/local/bin/git-cage \
    && mkdir /plugin/bin \
    && for action in add rm restore commit diff lfs_track lfs_untrack; do \
        ln -s /plugin/entry "/plugin/bin/$action"; \
    done
