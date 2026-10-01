FROM golang:1.27-alpine@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
COPY go.mod *.go ./
RUN test -z "$(gofmt -l .)"
RUN go vet ./...
RUN go test ./...
RUN go build -trimpath -o /out/verify .

FROM scratch
COPY --from=build /out/verify /verify
