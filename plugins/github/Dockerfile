FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache github-cli python3
ENV XDG_CACHE_HOME=/tmp/cache
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY scripts/github_plugin /plugin/github_plugin
COPY scripts/entry /plugin/entry
RUN mkdir /plugin/bin \
    && for action in pr_create pr_edit pr_view pr_fetch pr_reply pr_checks pr_close job_log_url; do \
        ln -s /plugin/entry "/plugin/bin/$action"; \
    done
