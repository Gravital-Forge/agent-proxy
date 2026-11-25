FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache python3 openssl
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY scripts/github_app_plugin /plugin/github_app_plugin
COPY scripts/entry /plugin/entry
RUN mkdir /plugin/bin \
    && ln -s /plugin/entry /plugin/bin/installation_token
