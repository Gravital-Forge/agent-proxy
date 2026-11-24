FROM golang:1.27-alpine@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS build
# WORKDIR comes before ENV so that no step is shared with the verifier's build
# stage: under the containerd image store, removing one stage's images also
# removes a step the other still builds on, and orphans the other's images.
WORKDIR /setid-probe
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
COPY setid-probe/go.mod setid-probe/*.go ./
RUN test -z "$(gofmt -l .)"
RUN go vet ./...
RUN go build -trimpath -o /out/setid-probe .

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
COPY manifest.json /plugin/manifest.json
COPY describe /usr/local/bin/describe
COPY count-bytes /plugin/count-bytes
COPY fail /plugin/fail
COPY --from=build /out/setid-probe /plugin/setid-probe
