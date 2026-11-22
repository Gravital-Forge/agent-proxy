#!/usr/bin/env bash
set -euo pipefail
shopt -s inherit_errexit nullglob

usage="usage: build-images.sh <version> <registry> <source-url>"

fail() {
    echo "build-images.sh: $1" >&2
    exit 1
}

digest_reference() {
    local name="$1" image="$2" repo_digests repo_digest
    repo_digests="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image")"
    while IFS= read -r repo_digest; do
        if [[ "$repo_digest" == "$registry/$name@sha256:"* ]]; then
            echo "$repo_digest"
            return
        fi
    done <<< "$repo_digests"
    fail "the image $image has no digest reference in $registry"
}

build_and_push() {
    local name="$1" image="$registry/$1:$version"
    shift
    docker build \
        --platform linux/amd64 \
        --provenance=false \
        --label "org.opencontainers.image.source=$source_url" \
        --label "org.opencontainers.image.version=$version" \
        --tag "$image" \
        "$@" >&2
    docker push "$image" >&2
    digest_reference "$name" "$image"
}

if (( $# != 3 )); then
    echo "$usage" >&2
    exit 2
fi

version="$1"
registry="$2"
source_url="$3"

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    fail "the version must be <major>.<minor>.<patch>, got '$version'"
fi

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"

verifier_reference="$(build_and_push agent-proxy-verifier "$root/core/verifier")"
echo "$verifier_reference"

build_and_push agent-proxy \
    --file "$root/core/docker/Dockerfile" \
    --build-arg "VERIFIER_IMAGE=$verifier_reference" \
    "$root/core/agent-proxy"

for dockerfile in "$root"/plugins/*/Dockerfile; do
    plugin_dir="$(dirname -- "$dockerfile")"
    build_and_push "agent-proxy-plugin-$(basename -- "$plugin_dir")" "$plugin_dir"
done
