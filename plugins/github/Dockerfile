FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache github-cli python3
ENV XDG_CACHE_HOME=/tmp/cache
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY scripts/github_plugin /plugin/github_plugin
COPY scripts/entry /plugin/entry
RUN mkdir /plugin/bin \
    && for action in pr_create pr_edit pr_view pr_fetch pr_reply pr_checks pr_close; do \
        ln -s /plugin/entry "/plugin/bin/$action"; \
    done
