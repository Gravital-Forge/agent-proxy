#!/usr/bin/env bash
# Removes everything provision.sh created and restores the agent's home ACL.
# Run with sudo: teardown.sh <agent>. Safe to re-run.
set -euo pipefail

readonly SVC=apxspike
readonly OTHER=apxspike-other
readonly GRP=apxspike-grp
readonly ACL_BAK=/root/apx-spike-home-acl.bak

die() {
  echo "teardown.sh: $*" >&2
  exit 1
}

failures=0
warn() {
  echo "teardown.sh: $*" >&2
  failures=$((failures + 1))
}

[[ "$(id -u)" -eq 0 ]] || die "run as root: sudo $0 <agent>"
[[ $# -eq 1 ]] || die "usage: $0 <agent>"
readonly AGENT=$1
[[ "$AGENT" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "invalid agent name: $AGENT"
case "$AGENT" in
  root | apxspike*) die "agent must be a real agent user, not $AGENT" ;;
esac
getent passwd "$AGENT" >/dev/null || die "no such user: $AGENT"
[[ -d "/home/$AGENT" && ! -L "/home/$AGENT" ]] || die "/home/$AGENT is not a directory"
readonly TREE=/home/$AGENT/apx-spike

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

remove_subids() {
  local file=$1 flag=$2 line name start count
  local -a lines=()
  mapfile -t lines < <(grep "^${SVC}:" "$file" || true)
  for line in "${lines[@]}"; do
    IFS=: read -r name start count <<<"$line"
    [[ "$name" == "$SVC" ]] || continue
    if usermod "$flag" "${start}-$((start + count - 1))" "$SVC"; then
      echo "removed the range ${start}-$((start + count - 1)) of $SVC from $file"
    else
      warn "could not remove the range ${start}-$((start + count - 1)) of $SVC from $file"
    fi
  done
}

svc_uid=""
if id -u "$SVC" >/dev/null 2>&1; then
  svc_uid=$(id -u "$SVC")
fi

echo "== stop docker for $SVC"
if [[ -n "$svc_uid" && -S "/run/user/$svc_uid/bus" ]] && as_svc systemctl --user is-active --quiet docker; then
  as_svc systemctl --user stop docker || warn "could not stop docker for $SVC"
else
  echo "docker for $SVC is not running"
fi

echo "== linger"
if [[ -e "/var/lib/systemd/linger/$SVC" ]]; then
  if [[ -n "$svc_uid" ]]; then
    if loginctl disable-linger "$SVC"; then
      echo "disabled linger for $SVC"
    else
      warn "could not disable linger for $SVC"
    fi
  else
    rm -f "/var/lib/systemd/linger/$SVC"
    echo "removed the linger file of $SVC"
  fi
else
  echo "linger is not enabled for $SVC"
fi

echo "== session"
if [[ -n "$svc_uid" ]]; then
  if loginctl show-user "$SVC" >/dev/null 2>&1; then
    loginctl terminate-user "$SVC" || warn "could not terminate the session of $SVC"
  fi
  for _ in $(seq 20); do
    pgrep -u "$svc_uid" >/dev/null || break
    sleep 0.5
  done
  if pgrep -u "$svc_uid" >/dev/null; then
    pkill -KILL -u "$svc_uid" || true
    sleep 1
  fi
  if pgrep -u "$svc_uid" >/dev/null; then
    warn "processes of $SVC are still running"
  else
    echo "no processes of $SVC are running"
  fi
else
  echo "user $SVC does not exist"
fi

echo "== home ACL of /home/$AGENT"
if [[ -f "$ACL_BAK" ]]; then
  if setfacl --restore="$ACL_BAK"; then
    rm -f "$ACL_BAK"
    echo "restored the ACL of /home/$AGENT from $ACL_BAK and deleted the backup"
  else
    warn "could not restore the ACL of /home/$AGENT; the backup stays at $ACL_BAK"
  fi
else
  home_acl=$(getfacl -p "/home/$AGENT")
  if [[ -n "$svc_uid" ]] && grep -q "^user:${SVC}:" <<<"$home_acl"; then
    if setfacl -x "u:${SVC}" "/home/$AGENT"; then
      echo "no backup at $ACL_BAK; removed the ACL entry of $SVC from /home/$AGENT"
    else
      warn "could not remove the ACL entry of $SVC from /home/$AGENT"
    fi
  else
    echo "no backup at $ACL_BAK and no ACL entry of $SVC on /home/$AGENT"
  fi
fi
getfacl -p "/home/$AGENT"

echo "== scratch tree $TREE"
if [[ -L "$TREE" ]]; then
  rm -f "$TREE"
  echo "removed the symlink $TREE"
elif [[ -e "$TREE" ]]; then
  if rm -rf --one-file-system "$TREE"; then
    echo "removed $TREE"
  else
    warn "could not remove $TREE"
  fi
else
  echo "$TREE does not exist"
fi

echo "== subordinate ids"
if [[ -n "$svc_uid" ]]; then
  remove_subids /etc/subuid --del-subuids
  remove_subids /etc/subgid --del-subgids
fi

echo "== users and groups"
for u in "$SVC" "$OTHER"; do
  if id -u "$u" >/dev/null 2>&1; then
    if userdel --remove "$u"; then
      echo "deleted user $u"
    else
      warn "userdel $u failed"
    fi
  else
    echo "user $u does not exist"
  fi
  if [[ -d "/home/$u" ]] && ! id -u "$u" >/dev/null 2>&1; then
    if rm -rf --one-file-system "/home/${u:?}"; then
      echo "removed /home/$u"
    else
      warn "could not remove /home/$u"
    fi
  fi
  if getent group "$u" >/dev/null && ! id -u "$u" >/dev/null 2>&1; then
    groupdel "$u" || warn "could not delete group $u"
  fi
done
if getent group "$GRP" >/dev/null; then
  if groupdel "$GRP"; then
    echo "deleted group $GRP"
  else
    warn "could not delete group $GRP"
  fi
else
  echo "group $GRP does not exist"
fi
if grep -q "^${SVC}:" /etc/subuid /etc/subgid; then
  warn "entries for $SVC remain in /etc/subuid or /etc/subgid; remove them by hand"
fi

if [[ "$failures" -gt 0 ]]; then
  die "finished with $failures problem(s); see the messages above"
fi
echo "teardown complete"
