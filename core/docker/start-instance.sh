#!/usr/bin/env bash
set -euo pipefail

usage="usage: start-instance.sh <image> <name> <port> <config-dir> <state-dir> <agent-tree> [docker-run-option...]"

fail() {
    echo "start-instance.sh: $1" >&2
    exit 1
}

require_real_directory() {
    local role="$1" path="$2"
    if [[ "$path" != /* || ! -d "$path" || "$(realpath -- "$path")" != "$path" ]]; then
        fail "the $role must be an absolute, existing directory equal to its realpath, got '$path'"
    fi
}

if (( $# < 6 )); then
    echo "$usage" >&2
    exit 2
fi

image="$1"
name="$2"
port="$3"
config_dir="$4"
state_dir="$5"
agent_tree="$6"
shift 6

docker_host="${DOCKER_HOST:-}"
if [[ "$docker_host" != unix:///* ]]; then
    fail "DOCKER_HOST must be unix://<path> with an absolute path, got '$docker_host'"
fi
socket_path="${docker_host#unix://}"

if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
    fail "the port must be an integer from 1 to 65535, got '$port'"
fi

if [[ ! -f "$config_dir/config.yaml" ]]; then
    fail "the config directory holds no config.yaml, got '$config_dir'"
fi

require_real_directory "state directory" "$state_dir"
require_real_directory "agent tree" "$agent_tree"

if running="$(docker container inspect --format '{{.State.Running}}' "$name" 2>/dev/null)"; then
    if [[ "$running" == true ]]; then
        echo "instance $name is already running"
    else
        docker start "$name"
    fi
    exit 0
fi

docker run -d \
    --name "$name" \
    --restart unless-stopped \
    --user 0:0 \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp \
    --publish "127.0.0.1:$port:8000" \
    --mount "type=bind,source=$socket_path,target=/var/run/docker.sock" \
    --mount "type=bind,source=$config_dir,target=/etc/agent-proxy,readonly" \
    --mount "type=bind,source=$state_dir,target=$state_dir" \
    --mount "type=bind,source=$agent_tree,target=$agent_tree" \
    --env "AGENT_PROXY_STATE_DIR=$state_dir" \
    "$@" \
    "$image"
