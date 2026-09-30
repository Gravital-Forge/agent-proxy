#!/usr/bin/env bash
# Runs the agent-proxy spike's file-access and egress checks as the service user.
# Run with sudo after provision.sh: checks.sh <agent> [--gh-token-file <path>].
# Wipes and refills the scratch tree; all output lands in <tree>/results.txt.
# The sh -c bodies below expand their positional parameters in the child shell.
# shellcheck disable=SC2016
set -euo pipefail

readonly SVC=apxspike
readonly OTHER=apxspike-other
readonly GRP=apxspike-grp
readonly CAGE=apx-spike-cage-fs

die() {
  echo "checks.sh: $*" >&2
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || die "run as root: sudo $0 <agent> [--gh-token-file <path>]"
[[ $# -ge 1 ]] || die "usage: $0 <agent> [--gh-token-file <path>]"
readonly AGENT=$1
shift
token_file=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gh-token-file)
      [[ $# -ge 2 ]] || die "--gh-token-file needs a path"
      token_file=$2
      shift 2
      ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "$AGENT" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "invalid agent name: $AGENT"
case "$AGENT" in
  root | apxspike*) die "agent must be a real agent user, not $AGENT" ;;
esac
getent passwd "$AGENT" >/dev/null || die "no such user: $AGENT"
[[ -d "/home/$AGENT" && ! -L "/home/$AGENT" ]] || die "/home/$AGENT is not a directory"
AGENT_GROUP=$(id -gn "$AGENT")
readonly AGENT_GROUP
SPIKE_SRC=$(cd "$(dirname "$0")/.." && pwd)
readonly SPIKE_SRC
readonly SPIKE_DST=/home/$SVC/spike
readonly TREE=/home/$AGENT/apx-spike
readonly RESULTS=$TREE/results.txt

for f in Dockerfile.tools egress/run.sh; do
  [[ -f "$SPIKE_SRC/$f" ]] || die "missing $SPIKE_SRC/$f"
done
id -u "$SVC" >/dev/null 2>&1 || die "user $SVC is missing; run provision.sh first"
id -u "$OTHER" >/dev/null 2>&1 || die "user $OTHER is missing; run provision.sh first"
[[ -d "$TREE" && ! -L "$TREE" ]] || die "$TREE is missing; run provision.sh first"
SVC_GROUP=$(id -gn "$SVC")
readonly SVC_GROUP

gh_token=""
if [[ -n "$token_file" ]]; then
  [[ -f "$token_file" ]] || die "token file not found: $token_file"
  gh_token=$(<"$token_file")
  gh_token=${gh_token//[$'\t\r\n ']/}
  [[ -n "$gh_token" ]] || die "token file is empty: $token_file"
fi
unset GH_TOKEN

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

as_svc_with_token() {
  local uid
  uid=$(id -u "$SVC")
  sudo --preserve-env=GH_TOKEN -u "$SVC" env --chdir="/home/$SVC" HOME="/home/$SVC" \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    DOCKER_HOST="unix:///run/user/$uid/docker.sock" \
    PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    "$@"
}

as_agent() {
  sudo -u "$AGENT" env --chdir="/home/$AGENT" HOME="/home/$AGENT" \
    PATH=/usr/bin:/bin:/usr/sbin:/sbin "$@"
}

as_other() {
  sudo -u "$OTHER" env --chdir="/home/$OTHER" HOME="/home/$OTHER" \
    PATH=/usr/bin:/bin:/usr/sbin:/sbin "$@"
}

check() {
  local id=$1 expected=$2 observed=$3 verdict=FAIL
  if [[ "$observed" == "$expected" ]]; then
    verdict=PASS
  fi
  echo "CHECK $id expected=$expected observed=$observed $verdict"
}

try() {
  local err
  if err=$("$@" 2>&1 >/dev/null); then
    echo ok
  elif [[ "$err" == *"ermission denied"* ]]; then
    echo denied
  else
    echo error
  fi
}

acl_of() {
  local acl
  acl=$(getfacl -p "$1")
  grep -v -e '^#' -e '^$' <<<"$acl" | tr -d '\t' | paste -sd, -
}

owner_of() {
  if [[ -e "$1" ]]; then
    stat -c '%U:%G' "$1"
  else
    echo missing
  fi
}

root_fixture() {
  local path=$1 owner=$2 group=$3 mode=$4
  rm -f "$path"
  echo "apx-spike root fixture" >"$path"
  setfacl -b "$path"
  chown "$owner:$group" "$path"
  chmod "$mode" "$path"
}

CAGE_SCRIPT=$(cat <<'EOF'
T=$1
cd "$T" || exit 1
check() {
  if [ "$3" = "$2" ]; then v=PASS; else v=FAIL; fi
  echo "CHECK $1 expected=$2 observed=$3 $v"
}
try() {
  if err=$("$@" 2>&1 >/dev/null); then
    echo ok
  else
    case $err in
      *"ermission denied"*) echo denied ;;
      *) echo error ;;
    esac
  fi
}
sg() {
  git -c safe.directory="$T/repo" -c user.name=apx-spike \
    -c user.email=apx-spike@localhost -C "$T/repo" "$@"
}
echo "MEASURE S1 $(id)"
check S2 "002:read=ok,append=ok;022:read=ok,append=ok" \
  "002:read=$(try cat agent-002.txt),append=$(try sh -c 'echo cage >>agent-002.txt');022:read=$(try cat agent-022.txt),append=$(try sh -c 'echo cage >>agent-022.txt')"
check S3 "file=ok,nested=ok" \
  "file=$(try sh -c 'echo cage >cage-made.txt'),nested=$(try sh -c 'mkdir -p cage-dir && echo cage >cage-dir/nested.txt')"
case $(try cat groupfile) in
  ok) s4=groups-survive ;;
  denied) s4=groups-dropped ;;
  *) s4=error ;;
esac
check S4 groups-survive "$s4"
check S5 denied "$(try cat otherfile)"
rc=0
err=$(git -C repo status 2>&1 >/dev/null) || rc=$?
check S6 128 "$rc"
echo "MEASURE S6 stderr=\"$(printf '%s\n' "$err" | head -n 1)\""
rc=0
err=$( { echo cage >repo/cage-commit.txt && sg add cage-commit.txt && sg commit -q -m "cage commit"; } 2>&1 >/dev/null) || rc=$?
check S7 0 "$rc"
if [ "$rc" -ne 0 ]; then
  echo "MEASURE S7 stderr=\"$(printf '%s\n' "$err" | head -n 1)\""
fi
EOF
)
readonly CAGE_SCRIPT

main() {
  trap 'as_svc docker rm -f "$CAGE" >/dev/null 2>&1 || true' EXIT
  local rc err p f
  echo "# apx-spike service-user checks, $(date -u +%Y-%m-%dT%H:%M:%SZ), agent=$AGENT"

  echo "# step 1: copy the egress script and build the tools image on the $SVC daemon"
  install -d -o "$SVC" -g "$SVC_GROUP" -m 0755 "$SPIKE_DST" "$SPIKE_DST/egress"
  install -o "$SVC" -g "$SVC_GROUP" -m 0644 "$SPIKE_SRC/Dockerfile.tools" "$SPIKE_DST/Dockerfile.tools"
  install -o "$SVC" -g "$SVC_GROUP" -m 0755 "$SPIKE_SRC/egress/run.sh" "$SPIKE_DST/egress/run.sh"
  as_svc docker build --quiet --tag apx-spike-tools --file "$SPIKE_DST/Dockerfile.tools" "$SPIKE_DST"

  echo "# step 2: fixtures"
  as_agent sh -c 'umask 002 && echo agent-002 >"$1"' sh "$TREE/agent-002.txt"
  as_agent sh -c 'umask 022 && echo agent-022 >"$1"' sh "$TREE/agent-022.txt"
  as_agent git -c init.defaultBranch=main init -q "$TREE/repo"
  as_agent sh -c 'cd "$1" && echo seed >seed.txt && git add seed.txt && git -c user.name=apx-spike-agent -c user.email=apx-spike-agent@localhost commit -q -m seed' sh "$TREE/repo"
  root_fixture "$TREE/groupfile" "$AGENT" "$GRP" 0640
  root_fixture "$TREE/otherfile" "$AGENT" "$AGENT_GROUP" 0600
  for f in agent-002.txt agent-022.txt groupfile otherfile; do
    echo "MEASURE F1 path=$f $(stat -c 'owner=%U group=%G mode=%a' "$TREE/$f") acl=$(acl_of "$TREE/$f")"
  done

  echo "# step 3: one cage as $SVC"
  as_svc docker rm -f "$CAGE" >/dev/null 2>&1 || true
  rc=0
  as_svc docker run --rm --name "$CAGE" \
    --cap-drop ALL --security-opt no-new-privileges --read-only \
    --tmpfs /tmp:rw,size=64m -e HOME=/tmp \
    --volume "$TREE:$TREE" \
    apx-spike-tools sh -c "$CAGE_SCRIPT" cage "$TREE" || rc=$?
  echo "MEASURE S0 cage_exit=$rc"

  echo "# step 4: what the cage created, seen from the host"
  for p in cage-made.txt cage-dir cage-dir/nested.txt; do
    if [[ -e "$TREE/$p" ]]; then
      echo "MEASURE S8 path=$p $(stat -c 'owner=%U group=%G mode=%a' "$TREE/$p") acl=$(acl_of "$TREE/$p")"
    else
      echo "MEASURE S8 path=$p missing"
    fi
  done
  check S8a "$SVC:$AGENT_GROUP" "$(owner_of "$TREE/cage-made.txt")"
  check S8b "$SVC:$AGENT_GROUP" "$(owner_of "$TREE/cage-dir")"
  check S8c "$SVC:$AGENT_GROUP" "$(owner_of "$TREE/cage-dir/nested.txt")"

  echo "# step 5: the agent works on what the cage created"
  check S9 "read=ok,append=ok,create=ok" \
    "read=$(try as_agent cat "$TREE/cage-made.txt"),append=$(try as_agent sh -c 'echo agent >>"$1"' sh "$TREE/cage-made.txt"),create=$(try as_agent sh -c 'echo agent >"$1"' sh "$TREE/cage-dir/agent-made.txt")"
  rc=0
  err=$(as_agent sh -c 'cd "$1" && echo agent >agent-commit.txt && git add agent-commit.txt && git -c user.name=apx-spike-agent -c user.email=apx-spike-agent@localhost commit -q -m "agent commit"' sh "$TREE/repo" 2>&1 >/dev/null) || rc=$?
  check S10 0 "$rc"
  if [[ "$rc" -ne 0 ]]; then
    echo "MEASURE S10 stderr=\"$(head -n 1 <<<"$err")\""
  fi

  echo "# step 6: a second agent"
  check S11 denied "$(try as_other cat "$TREE/cage-made.txt")"
  check S12 denied "$(try as_other ls "/home/$AGENT")"

  echo "# step 7: egress checks on the $SVC daemon"
  rc=0
  if [[ -n "$gh_token" ]]; then
    export GH_TOKEN=$gh_token
    as_svc_with_token "$SPIKE_DST/egress/run.sh" || rc=$?
  else
    as_svc "$SPIKE_DST/egress/run.sh" || rc=$?
  fi
  echo "MEASURE R1 egress_exit=$rc"
}

as_svc docker info >/dev/null 2>&1 || die "rootless docker for $SVC is not reachable; run provision.sh first"
find "$TREE" -mindepth 1 -delete
install -o "$AGENT" -g "$AGENT_GROUP" -m 0660 /dev/null "$RESULTS"
main 2>&1 | tee "$RESULTS"
echo "results: $RESULTS"
