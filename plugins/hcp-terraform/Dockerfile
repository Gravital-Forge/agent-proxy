FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache python3
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY scripts/hcp_terraform_plugin /plugin/hcp_terraform_plugin
COPY scripts/entry /plugin/entry
RUN mkdir /plugin/bin \
    && for action in workspace_list run_list run_view plan_log apply_log; do \
        ln -s /plugin/entry "/plugin/bin/$action"; \
    done
