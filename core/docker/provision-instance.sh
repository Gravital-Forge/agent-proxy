#!/usr/bin/env bash
set -euo pipefail

usage="usage: provision-instance.sh <agent> <port> <config-file>"

read_config_program='import yaml
with open("/etc/agent-proxy/config.yaml") as handle:
    config = yaml.safe_load(handle)
print(config["agent_tree"])
print(config["overflow_dir"])'

fail() {
    echo "provision-instance.sh: $1" >&2
    exit 1
}

as_service_user() {
    systemd-run "--machine=$service_user@.host" --user --wait --pipe --quiet "$@"
}

service_docker() {
    DOCKER_HOST="$docker_host" docker "$@"
}

has_id_range() {
    awk -F: -v name="$service_user" '$1 == name { found = 1 } END { exit !found }' "$1" 2>/dev/null
}

check_arguments() {
    local agent_entry
    if ! agent_entry="$(getent passwd "$agent")"; then
        fail "no user named '$agent'"
    fi
    if (( ${#service_user} > 32 )); then
        fail "the service user name '$service_user' is longer than 32 characters"
    fi
    if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
        fail "the port must be an integer from 1 to 65535, got '$port'"
    fi
    if [[ ! -f "$config_file" || ! -r "$config_file" ]]; then
        fail "the config file must be a readable regular file, got '$config_file'"
    fi
    agent_home="$(cut -d: -f6 <<<"$agent_entry")"
    if [[ ! -d "$agent_home/Projects" ]]; then
        fail "the agent's tree '$agent_home/Projects' is not a directory"
    fi
    agent_tree="$(realpath -- "$agent_home/Projects")"
    agent_group="$(id -gn "$agent")"
    echo "checked the arguments: agent $agent, tree $agent_tree, port $port"
}

create_service_user() {
    if getent passwd "$service_user" >/dev/null; then
        echo "service user $service_user already exists"
    else
        useradd --create-home --shell /usr/sbin/nologin "$service_user"
        echo "created service user $service_user"
    fi
    local id_file
    for id_file in /etc/subuid /etc/subgid; do
        if ! has_id_range "$id_file"; then
            fail "$id_file has no range for $service_user; run 'usermod --add-subuids FIRST-LAST --add-subgids FIRST-LAST $service_user' with a free range, then run this script again"
        fi
    done
    local service_entry
    service_entry="$(getent passwd "$service_user")"
    service_uid="$(cut -d: -f3 <<<"$service_entry")"
    service_home="$(cut -d: -f6 <<<"$service_entry")"
    state_dir="$service_home/state"
    docker_host="unix:///run/user/$service_uid/docker.sock"
}

create_config_root() {
    if [[ -d /etc/agent-proxy ]]; then
        echo "/etc/agent-proxy already exists"
    else
        install -d -o root -g root -m 0755 /etc/agent-proxy
        config_root_created=true
        echo "created /etc/agent-proxy"
    fi
}

enable_user_manager() {
    if [[ "$(loginctl show-user --property=Linger --value "$service_user" 2>/dev/null)" == yes ]] \
        && systemctl is-active --quiet "user@$service_uid.service"; then
        echo "linger and the user manager for $service_user are already in place"
        return
    fi
    loginctl enable-linger "$service_user"
    systemctl start "user@$service_uid.service"
    echo "enabled linger and started user@$service_uid.service for $service_user"
}

setup_rootless_daemon() {
    if ! as_service_user /usr/bin/systemctl --user is-active --quiet docker; then
        as_service_user /usr/bin/dockerd-rootless-setuptool.sh install >&2
        echo "installed and started the rootless daemon of $service_user"
    elif [[ "$config_root_created" == true ]]; then
        as_service_user /usr/bin/systemctl --user restart docker
        echo "restarted the rootless daemon of $service_user so it sees /etc/agent-proxy"
    else
        echo "the rootless daemon of $service_user is already active"
    fi
}

build_images() {
    service_docker build --quiet --tag agent-proxy-verifier:1 "$script_dir/../verifier" >/dev/null
    service_docker build --quiet --tag agent-proxy:1 --file "$script_dir/Dockerfile" \
        "$script_dir/../agent-proxy" >/dev/null
    echo "built agent-proxy-verifier:1 and agent-proxy:1 on the daemon of $service_user"
}

install_config() {
    local created_dir=""
    if [[ ! -d "$config_dir" ]]; then
        install -d -o root -g root -m 0755 "$config_dir"
        created_dir="created $config_dir, "
    fi
    if cmp -s "$config_file" "$config_dir/config.yaml"; then
        echo "$config_dir/config.yaml is already up to date"
    else
        install -o root -g root -m 0644 "$config_file" "$config_dir/config.yaml"
        echo "${created_dir}installed $config_file as $config_dir/config.yaml"
    fi
}

read_config_paths() {
    local paths configured_tree
    paths="$(service_docker run --rm --network none --read-only --cap-drop ALL --user 0:0 \
        --mount "type=bind,source=$config_dir,target=/etc/agent-proxy,readonly" \
        --entrypoint python agent-proxy:1 -c "$read_config_program")"
    configured_tree="$(sed -n 1p <<<"$paths")"
    overflow_dir="$(sed -n 2p <<<"$paths")"
    if [[ "$configured_tree" != "$agent_tree" ]]; then
        fail "the config's agent_tree is '$configured_tree', but the agent's tree is '$agent_tree'"
    fi
    if [[ "$overflow_dir" != "$agent_tree"/?* || "$(realpath -m -- "$overflow_dir")" != "$overflow_dir" ]]; then
        fail "the config's overflow_dir must lie inside '$agent_tree' and equal its realpath, got '$overflow_dir'"
    fi
    echo "read the config: agent_tree $configured_tree, overflow_dir $overflow_dir"
}

create_state_dir() {
    if [[ -d "$state_dir" ]]; then
        echo "state directory $state_dir already exists"
    else
        install -d -o "$service_user" -g "$service_user" -m 0700 "$state_dir"
        echo "created state directory $state_dir"
    fi
}

create_overflow_dir() {
    if [[ -L "$overflow_dir" ]]; then
        fail "the overflow directory '$overflow_dir' is a symbolic link"
    fi
    if [[ -d "$overflow_dir" ]]; then
        echo "overflow directory $overflow_dir already exists"
    else
        install -d -o "$agent" -g "$agent_group" -m 2770 "$overflow_dir"
        echo "created overflow directory $overflow_dir"
    fi
}

apply_tree_permissions() {
    find "$agent_tree" ! -group "$agent_group" -exec chgrp -h "$agent_group" {} +
    find "$agent_tree" -type d ! -perm -2000 -exec chmod g+s {} +
    setfacl -R -P -m "u:$service_user:rwX,d:u:$service_user:rwx,d:u:$agent:rwx" "$agent_tree"
    echo "applied group $agent_group, setgid, and the ACLs of $service_user on $agent_tree"
}

grant_home_traverse() {
    setfacl -m "u:$service_user:x" "$agent_home"
    echo "granted $service_user traverse on $agent_home"
}

start_instance() {
    DOCKER_HOST="$docker_host" "$script_dir/start-instance.sh" agent-proxy:1 agent-proxy "$port" \
        "$config_dir" "$state_dir" "$agent_tree" >/dev/null
    echo "instance agent-proxy runs on 127.0.0.1:$port"
}

print_next_steps() {
    local exec_prefix="DOCKER_HOST=$docker_host docker exec"
    echo "next, mint a token: $exec_prefix agent-proxy agent-proxy token mint --name <name> --scope <scope>"
    echo "next, store a credential: $exec_prefix --interactive agent-proxy agent-proxy credential set <name>"
    echo "next, follow the runbook: $(realpath -m -- "$script_dir/../../docs/runbooks/per-agent-instance.md")"
}

if (( $# < 3 )); then
    echo "$usage" >&2
    exit 2
fi

if (( EUID != 0 )); then
    fail "must run as root"
fi

agent="$1"
port="$2"
config_file="$3"
service_user="agentproxy-$agent"
config_dir="/etc/agent-proxy/$agent"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
config_root_created=false

check_arguments
create_service_user
create_config_root
enable_user_manager
setup_rootless_daemon
build_images
install_config
read_config_paths
create_state_dir
create_overflow_dir
apply_tree_permissions
grant_home_traverse
start_instance
print_next_steps
