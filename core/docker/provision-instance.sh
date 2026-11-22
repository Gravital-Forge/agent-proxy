#!/usr/bin/env bash
set -euo pipefail

usage="usage: provision-instance.sh [--release VERSION] [--tree PATH] <agent> <port> <config-file>"
release_repository="ghcr.io/gravital-forge/agent-proxy"

read_config_program='import yaml
with open("/etc/agent-proxy/config.yaml") as handle:
    config = yaml.safe_load(handle)
print(config["agent_tree"])
print(config["overflow_dir"])'

fail() {
    echo "provision-instance.sh: $1" >&2
    exit 1
}

refuse_usage() {
    echo "$usage" >&2
    exit 2
}

require_option_value() {
    if (( $# < 2 )) || [[ -z "$2" ]]; then
        refuse_usage
    fi
}

parse_arguments() {
    release_version=""
    tree_option=""
    while (( $# > 0 )); do
        case "$1" in
            --release)
                require_option_value "$@"
                release_version="$2"
                shift 2
                ;;
            --tree)
                require_option_value "$@"
                tree_option="$2"
                shift 2
                ;;
            --)
                shift
                break
                ;;
            -*) refuse_usage ;;
            *) break ;;
        esac
    done
    if (( $# < 3 )); then
        refuse_usage
    fi
    agent="$1"
    port="$2"
    config_file="$3"
}

as_service_user() {
    systemd-run "--machine=$service_user@.host" --user --wait --pipe --quiet "$@"
}

as_agent() {
    (cd / && runuser -u "$agent" -- "$@")
}

service_docker() {
    DOCKER_HOST="$docker_host" docker "$@"
}

has_id_range() {
    awk -F: -v name="$service_user" '$1 == name { found = 1 } END { exit !found }' "$1" 2>/dev/null
}

find_tree_parents_in_home() {
    local home_realpath directory
    home_realpath="$(realpath -m -- "$agent_home")"
    if [[ "$agent_tree" != "$home_realpath"/?* ]]; then
        fail "the agent's tree '$agent_tree' must lie inside the agent's home '$home_realpath'"
    fi
    tree_parents_in_home=()
    directory="$(dirname -- "$agent_tree")"
    while [[ "$directory" != "$home_realpath" ]]; do
        tree_parents_in_home+=("$directory")
        directory="$(dirname -- "$directory")"
    done
}

check_arguments() {
    local agent_entry tree_path
    if ! agent_entry="$(getent passwd "$agent")"; then
        fail "no user named '$agent'"
    fi
    if (( ${#service_user} > 32 )); then
        fail "the service user name '$service_user' is longer than 32 characters"
    fi
    if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
        fail "the port must be an integer from 1 to 65535, got '$port'"
    fi
    if [[ -n "$release_version" && ! "$release_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        fail "the release must be <major>.<minor>.<patch>, got '$release_version'"
    fi
    if [[ ! -f "$config_file" || ! -r "$config_file" ]]; then
        fail "the config file must be a readable regular file, got '$config_file'"
    fi
    agent_home="$(cut -d: -f6 <<<"$agent_entry")"
    tree_path="${tree_option:-$agent_home/Projects}"
    if [[ ! -d "$tree_path" ]]; then
        fail "the agent's tree '$tree_path' is not a directory"
    fi
    agent_tree="$(realpath -- "$tree_path")"
    find_tree_parents_in_home
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

prepare_images() {
    if [[ -n "$release_version" ]]; then
        local release_image="$release_repository:$release_version"
        service_docker pull "$release_image" >/dev/null
        service_docker tag "$release_image" agent-proxy:1
        echo "pulled $release_image as agent-proxy:1 on the daemon of $service_user"
        return
    fi
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
        as_agent install -d -m 2770 "$overflow_dir"
        echo "created overflow directory $overflow_dir as $agent"
    fi
}

apply_tree_permissions() {
    local closed=(-type d \( ! -readable -o ! -executable \) -prune)
    local skipped
    as_agent find "$agent_tree" "${closed[@]}" -o -user "$agent" ! -group "$agent_group" \
        -exec chgrp -h "$agent_group" {} +
    as_agent find "$agent_tree" "${closed[@]}" -o -type d -user "$agent" ! -perm -2000 \
        -exec chmod g+s {} +
    as_agent find "$agent_tree" "${closed[@]}" -o -type d -user "$agent" \
        -exec setfacl -P -m "u:$service_user:rwX,d:u:$service_user:rwx,d:u:$agent:rwx" {} +
    as_agent find "$agent_tree" "${closed[@]}" -o ! -type d ! -type l -user "$agent" \
        -exec setfacl -P -m "u:$service_user:rwX" {} +
    skipped="$(as_agent find "$agent_tree" "${closed[@]}" -printf x -o ! -user "$agent" -printf x | wc -c)"
    echo "applied group $agent_group, setgid, and the ACLs of $service_user as $agent on $agent_tree; skipped $skipped entries $agent does not own or cannot open"
}

grant_home_traverse() {
    setfacl -m "u:$service_user:x" "$agent_home"
    echo "granted $service_user traverse on $agent_home"
}

grant_tree_traverse() {
    local directory
    for directory in "${tree_parents_in_home[@]}"; do
        as_agent setfacl -m "u:$service_user:x" "$directory"
    done
    echo "granted $service_user traverse as $agent on each directory between $agent_home and $agent_tree, ${#tree_parents_in_home[@]} in all"
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
    echo "next, follow the deployment guide: $(realpath -m -- "$script_dir/../../docs/deployment.md")"
}

main() {
    parse_arguments "$@"

    if (( EUID != 0 )); then
        fail "must run as root"
    fi

    service_user="agentproxy-$agent"
    config_dir="/etc/agent-proxy/$agent"
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    config_root_created=false

    check_arguments
    create_service_user
    create_config_root
    enable_user_manager
    setup_rootless_daemon
    prepare_images
    install_config
    read_config_paths
    create_state_dir
    create_overflow_dir
    apply_tree_permissions
    grant_home_traverse
    grant_tree_traverse
    start_instance
    print_next_steps
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
