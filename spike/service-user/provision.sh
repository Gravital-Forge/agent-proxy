#!/usr/bin/env bash
# Provisions the agent-proxy spike's scratch identities and tree on this host.
# Run with sudo: provision.sh <agent>. Safe to re-run.
# Steps 2 to 4 follow the per-user setup in the git-sidecar repository,
# docs/runbooks/shared-sidecar-isolated-agents.md (Part 1); debug against it.
set -euo pipefail

readonly SVC=apxspike
readonly OTHER=apxspike-other
readonly GRP=apxspike-grp
readonly ACL_BAK=/root/apx-spike-home-acl.bak

die() {
  echo "provision.sh: $*" >&2
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || die "run as root: sudo $0 <agent>"
[[ $# -eq 1 ]] || die "usage: $0 <agent>"
readonly AGENT=$1
[[ "$AGENT" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "invalid agent name: $AGENT"
case "$AGENT" in
  root | apxspike*) die "agent must be a real agent user, not $AGENT" ;;
esac
getent passwd "$AGENT" >/dev/null || die "no such user: $AGENT"
[[ "$(getent passwd "$AGENT" | cut -d: -f6)" == "/home/$AGENT" ]] || die "home of $AGENT is not /home/$AGENT"
[[ -d "/home/$AGENT" && ! -L "/home/$AGENT" ]] || die "/home/$AGENT is not a directory"
AGENT_GROUP=$(id -gn "$AGENT")
readonly AGENT_GROUP
readonly TREE=/home/$AGENT/apx-spike

for cmd in useradd usermod groupadd loginctl systemctl setfacl getfacl newuidmap newgidmap dockerd-rootless-setuptool.sh docker sudo; do
  command -v "$cmd" >/dev/null || die "missing command $cmd; install acl, uidmap, docker-ce-cli, and docker-ce-rootless-extras"
done
[[ -f /etc/subuid && -f /etc/subgid ]] || die "/etc/subuid or /etc/subgid is missing"

cd /

as_svc() {
  local uid
  uid=$(id -u "$SVC")
  sudo -u "$SVC" env --chdir="/home/$SVC" HOME="/home/$SVC" \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    DOCKER_HOST="unix:///run/user/$uid/docker.sock" \
    PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    "$@"
}

add_subids() {
  local file=$1 flag=$2 start
  if grep -q "^${SVC}:" "$file"; then
    echo "$SVC already has a range in $file"
    return
  fi
  start=$(awk -F: '{e=$2+$3; if(e>m)m=e} END{print (m<100000?100000:m)}' "$file")
  usermod "$flag" "${start}-$((start + 65535))" "$SVC"
  echo "gave $SVC the range ${start}-$((start + 65535)) in $file"
}

echo "== step 1: users and group"
if getent group "$GRP" >/dev/null; then
  echo "group $GRP exists"
else
  groupadd "$GRP"
  echo "created group $GRP"
fi
for u in "$SVC" "$OTHER"; do
  if id -u "$u" >/dev/null 2>&1; then
    echo "user $u exists"
  else
    useradd --create-home --shell /usr/sbin/nologin "$u"
    echo "created user $u"
  fi
done
SVC_UID=$(id -u "$SVC")
readonly SVC_UID
svc_groups=" $(id -nG "$SVC") "
if [[ "$svc_groups" == *" $GRP "* ]]; then
  echo "$SVC is in $GRP"
else
  usermod --append --groups "$GRP" "$SVC"
  echo "added $SVC to $GRP"
  if systemctl is-active --quiet "user@${SVC_UID}.service"; then
    systemctl restart "user@${SVC_UID}.service"
    echo "restarted the user manager of $SVC so its processes carry $GRP"
  fi
fi

echo "== step 2: subordinate ids"
add_subids /etc/subuid --add-subuids
add_subids /etc/subgid --add-subgids

echo "== step 3: linger"
if [[ -e "/var/lib/systemd/linger/$SVC" ]]; then
  echo "linger already enabled for $SVC"
else
  loginctl enable-linger "$SVC"
  echo "enabled linger for $SVC"
fi
for _ in $(seq 20); do
  [[ -S "/run/user/$SVC_UID/bus" ]] && break
  sleep 0.5
done
[[ -S "/run/user/$SVC_UID/bus" ]] || die "/run/user/$SVC_UID/bus did not appear within 10 seconds"

echo "== step 4: rootless docker for $SVC"
if as_svc systemctl --user is-active --quiet docker; then
  echo "rootless docker is active for $SVC"
else
  as_svc dockerd-rootless-setuptool.sh install
fi
server=$(as_svc docker version --format '{{.Server.Version}}')
echo "docker server $server at unix:///run/user/$SVC_UID/docker.sock"

echo "== step 5: traverse ACL on /home/$AGENT"
if [[ -e "$ACL_BAK" ]]; then
  echo "home ACL backup already at $ACL_BAK"
else
  getfacl -p "/home/$AGENT" >"$ACL_BAK.tmp"
  mv "$ACL_BAK.tmp" "$ACL_BAK"
  echo "saved the ACL of /home/$AGENT to $ACL_BAK"
fi
home_acl=$(getfacl -p "/home/$AGENT")
if grep -q "^user:${SVC}:--x" <<<"$home_acl"; then
  echo "$SVC already has traverse on /home/$AGENT"
else
  setfacl -m "u:${SVC}:--x" "/home/$AGENT"
  echo "gave $SVC traverse on /home/$AGENT"
fi

echo "== step 6: scratch tree $TREE"
[[ ! -L "$TREE" ]] || die "$TREE is a symlink; remove it and re-run"
if [[ -d "$TREE" ]]; then
  echo "$TREE exists"
elif [[ -e "$TREE" ]]; then
  die "$TREE exists and is not a directory"
else
  mkdir "$TREE"
  echo "created $TREE"
fi
chown "$AGENT:$AGENT_GROUP" "$TREE"
chmod 2770 "$TREE"
setfacl -m "u:${SVC}:rwx,d:u:${SVC}:rwx,d:u:${AGENT}:rwx" "$TREE"
getfacl -p "$TREE"
if as_svc test -w "$TREE"; then
  echo "$SVC can write $TREE"
else
  die "$SVC cannot write $TREE"
fi

echo "provisioned; next: sudo checks.sh $AGENT [--gh-token-file <path>]"
