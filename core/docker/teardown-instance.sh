#!/usr/bin/env bash
set -euo pipefail

usage="usage: teardown-instance.sh [--tree PATH] <agent>"

read_overflow_program='import yaml
with open("/etc/agent-proxy/config.yaml") as handle:
    config = yaml.safe_load(handle)
print(config["overflow_dir"])'

fail() {
    echo "teardown-instance.sh: $1" >&2
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
    tree_option=""
    while (( $# > 0 )); do
        case "$1" in
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
    if (( $# < 1 )); then
        refuse_usage
    fi
    agent="$1"
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

check_agent() {
    local agent_entry service_entry tree_path
    if ! agent_entry="$(getent passwd "$agent")"; then
        fail "no user named '$agent'"
    fi
    agent_home="$(cut -d: -f6 <<<"$agent_entry")"
    tree_path="${tree_option:-$agent_home/Projects}"
    if [[ -d "$tree_path" ]]; then
        agent_tree="$(realpath -- "$tree_path")"
        find_tree_parents_in_home
    fi
    if service_entry="$(getent passwd "$service_user")"; then
        service_uid="$(cut -d: -f3 <<<"$service_entry")"
        service_home="$(cut -d: -f6 <<<"$service_entry")"
        docker_host="unix:///run/user/$service_uid/docker.sock"
    fi
    echo "checked the agent: $agent"
}

read_overflow_dir() {
    if [[ -z "$service_uid" || -z "$agent_tree" || ! -S "/run/user/$service_uid/docker.sock" \
        || ! -f "$config_dir/config.yaml" ]] \
        || ! service_docker image inspect agent-proxy:1 >/dev/null 2>&1; then
        echo "skipped reading overflow_dir: the service user, its daemon, the image, the config, or the agent's tree is gone"
        return
    fi
    overflow_dir="$(service_docker run --rm --network none --read-only --cap-drop ALL --user 0:0 \
        --mount "type=bind,source=$config_dir,target=/etc/agent-proxy,readonly" \
        --entrypoint python agent-proxy:1 -c "$read_overflow_program")"
    if [[ "$overflow_dir" != "$agent_tree"/?* || "$(realpath -m -- "$overflow_dir")" != "$overflow_dir" ]]; then
        fail "the config's overflow_dir must lie inside '$agent_tree' and equal its realpath, got '$overflow_dir'"
    fi
    echo "read overflow_dir $overflow_dir from $config_dir/config.yaml"
}

remove_acls() {
    if [[ -z "$service_uid" ]]; then
        echo "skipped removing ACL entries: service user $service_user is already gone"
        return
    fi
    if [[ -n "$agent_tree" ]]; then
        setfacl -R -P -x "u:$service_user" "$agent_tree"
        setfacl -R -P -x "d:u:$service_user" "$agent_tree"
    fi
    local cleared=("${tree_parents_in_home[@]}" "$agent_home") directory
    for directory in "${cleared[@]}"; do
        setfacl -x "u:$service_user" "$directory"
    done
    echo "removed the ACL entries of $service_user from the agent's tree and from ${cleared[*]}"
}

remove_overflow_dir() {
    if [[ -z "$overflow_dir" ]]; then
        echo "skipped removing the overflow directory: no overflow_dir was read"
    elif [[ ! -e "$overflow_dir" && ! -L "$overflow_dir" ]]; then
        echo "overflow directory $overflow_dir is already gone"
    elif as_agent rm -rf -- "$overflow_dir"; then
        echo "removed overflow directory $overflow_dir as $agent"
    else
        echo "warning: $agent could not remove all of overflow directory $overflow_dir; remove what is left by hand" >&2
    fi
}

remove_rootless_daemon() {
    if [[ -z "$service_uid" ]] || ! systemctl is-active --quiet "user@$service_uid.service"; then
        echo "skipped removing the rootless daemon: the service user or its user manager is already gone"
        return
    fi
    as_service_user /usr/bin/dockerd-rootless-setuptool.sh uninstall >&2
    as_service_user /usr/bin/rootlesskit rm -rf "$service_home/.local/share/docker"
    echo "uninstalled the rootless daemon of $service_user and removed its data"
}

disable_user_manager() {
    if [[ -z "$service_uid" ]]; then
        echo "skipped disabling linger: service user $service_user is already gone"
        return
    fi
    loginctl disable-linger "$service_user"
    systemctl stop "user@$service_uid.service"
    echo "disabled linger and stopped user@$service_uid.service"
}

remove_service_user() {
    if [[ -z "$service_uid" ]]; then
        echo "service user $service_user is already gone"
    else
        userdel --remove "$service_user"
        echo "removed service user $service_user and its home"
    fi
    local id_file range_line
    for id_file in /etc/subuid /etc/subgid; do
        range_line="$(awk -F: -v name="$service_user" '$1 == name' "$id_file" 2>/dev/null || true)"
        if [[ -n "$range_line" ]]; then
            echo "warning: $id_file still holds '$range_line'; remove it if no user needs that range" >&2
        fi
    done
}

remove_config_dir() {
    if [[ -d "$config_dir" ]]; then
        rm -rf -- "$config_dir"
        echo "removed $config_dir"
    else
        echo "$config_dir is already gone"
    fi
}

main() {
    parse_arguments "$@"

    if (( EUID != 0 )); then
        fail "must run as root"
    fi

    service_user="agentproxy-$agent"
    config_dir="/etc/agent-proxy/$agent"
    agent_tree=""
    tree_parents_in_home=()
    service_uid=""
    service_home=""
    docker_host=""
    overflow_dir=""

    check_agent
    read_overflow_dir
    remove_acls
    remove_overflow_dir
    remove_rootless_daemon
    disable_user_manager
    remove_service_user
    remove_config_dir
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
