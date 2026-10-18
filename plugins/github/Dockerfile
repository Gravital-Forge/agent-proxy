FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
# Alpine 3.24 ships gh 2.97.0, which lacks --attach, so the image takes gh from
# its release tarball. GH_SHA256 is the line the release's checksums file lists
# for that tarball.
ARG GH_VERSION=2.102.0
ARG GH_SHA256=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386
RUN apk add --no-cache git python3 \
    && wget -q -O /tmp/gh.tar.gz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    && echo "${GH_SHA256}  /tmp/gh.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/gh.tar.gz -C /tmp \
    && install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_amd64"
ENV XDG_CACHE_HOME=/tmp/cache
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY scripts/github_plugin /plugin/github_plugin
COPY scripts/entry /plugin/entry
RUN ln -s /plugin/entry /usr/local/bin/gh-cage \
    && mkdir /plugin/bin \
    && for action in pr_create pr_edit pr_view pr_fetch pr_reply pr_checks pr_close job_log_url; do \
        ln -s /plugin/entry "/plugin/bin/$action"; \
    done
