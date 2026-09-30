#!/usr/bin/env bash
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
dockerfile="$here/../Dockerfile.tools"
iron_image='ironsh/iron-proxy:0.52.0@sha256:004fdc067dc4cc47cebfc0dab3c63ab9a5e21a3230c965e69775a565df99efc6'
tools=apx-spike-tools
mgmt_key=apx-spike-mgmt
cage_flags=(--rm --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs '/tmp:rw,size=64m' -e HOME=/tmp)
containers=(apx-spike-cage apx-spike-mgmt apx-spike-proxy-gh apx-spike-proxy-ex apx-spike-proxy-srcip)
networks=(apx-spike-net-gh apx-spike-net-ex apx-spike-net-none apx-spike-egress)

remove_objects() {
  local c n
  for c in "${containers[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  for n in "${networks[@]}"; do docker network rm "$n" >/dev/null 2>&1 || true; done
}

work=$(mktemp -d)
on_exit() {
  remove_objects
  rm -rf "$work"
}
trap on_exit EXIT
remove_objects

if [ -n "${GH_TOKEN:-}" ]; then
  token_kind=real
else
  token_kind=dummy
  GH_TOKEN=apx-spike-dummy
fi
export GH_TOKEN

report() {
  printf 'CHECK %s expected=%s observed=%s %s\n' "$1" "$2" "$3" "$4"
}

# expected is a |-separated set of values, or "fail" for any curl failure.
check() {
  local id=$1 expected=$2 observed=$3 verdict=FAIL
  if [[ "|$expected|" == *"|$observed|"* ]]; then
    verdict=PASS
  elif [ "$expected" = fail ] && [[ "$observed" == exit* ]] && [ "$observed" != exit0 ]; then
    verdict=PASS
  fi
  report "$id" "$expected" "$observed" "$verdict"
}

cage() {
  local net=$1
  shift
  docker run "${cage_flags[@]}" --name apx-spike-cage --network "$net" \
    -e HTTPS_PROXY=http://egress:1080 "$tools" "$@"
}

cage_gh() {
  local net=$1
  shift
  docker run "${cage_flags[@]}" --name apx-spike-cage --network "$net" \
    -e HTTPS_PROXY=http://egress:1080 -e GH_TOKEN "$tools" "$@"
}

http() {
  local net=$1 out rc=0
  shift
  out=$(cage "$net" curl -sS -o /dev/null -w '%{http_code}' \
    --connect-timeout 10 --max-time 20 "$@" 2>/dev/null) || rc=$?
  if [ "$rc" -eq 0 ]; then echo "$out"; else echo "exit$rc"; fi
}

mgmt() {
  local proxy=$1
  shift
  docker run "${cage_flags[@]}" --name apx-spike-mgmt --network "container:$proxy" \
    "$tools" curl -sS --max-time 10 "$@"
}

reload() {
  local out rc=0 code body
  out=$(mgmt "$1" -X POST -H "Authorization: Bearer $mgmt_key" -w '\n%{http_code}' \
    http://127.0.0.1:9092/v1/reload 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "exit$rc"
    return
  fi
  code=${out##*$'\n'}
  body=${out%$'\n'*}
  printf '%s:%s\n' "$code" "$(printf '%s' "$body" | tr -d ' \n')"
}

write_config() {
  local file=$1 transform=$2 body=$3
  cat >"$file" <<EOF
dns:
  listen: ":53"
  proxy_ip: "127.0.0.1"
proxy:
  tunnel_listen: ":1080"
tls:
  mode: "sni-only"
transforms:
  - name: $transform
    config:
$body
management:
  listen: ":9092"
  api_key_env: "IRON_MANAGEMENT_API_KEY"
EOF
}

domains() {
  printf '      domains: [%s]\n' "$1"
}

start_proxy() {
  local name=$1 net=$2
  shift 2
  docker run -d --name "$name" --network "$net" "$@" \
    -v "$work/$name:/etc/iron-proxy:ro" -e IRON_MANAGEMENT_API_KEY="$mgmt_key" \
    "$iron_image" -config /etc/iron-proxy/proxy.yaml >/dev/null
}

wait_ready() {
  local _
  for _ in $(seq 1 20); do
    if [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ] &&
      mgmt "$1" -o /dev/null http://127.0.0.1:9092/ >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

require_ready() {
  if ! wait_ready "$1"; then
    echo "run.sh: proxy $1 did not become ready; its log follows" >&2
    docker logs "$1" >&2 || true
    exit 1
  fi
}

docker build -t "$tools" -f "$dockerfile" "$(dirname "$dockerfile")" >&2

docker network create --internal --subnet 172.31.1.0/24 apx-spike-net-gh >/dev/null
docker network create --internal --subnet 172.31.2.0/24 apx-spike-net-ex >/dev/null
docker network create --internal --subnet 172.31.3.0/24 apx-spike-net-none >/dev/null
docker network create apx-spike-egress >/dev/null

mkdir -p "$work/apx-spike-proxy-gh" "$work/apx-spike-proxy-ex" "$work/apx-spike-proxy-srcip"
gh_conf="$work/apx-spike-proxy-gh/proxy.yaml"
ex_conf="$work/apx-spike-proxy-ex/proxy.yaml"
srcip_conf="$work/apx-spike-proxy-srcip/proxy.yaml"
write_config "$gh_conf" allowlist "$(domains '"github.com", "api.github.com"')"
write_config "$ex_conf" allowlist "$(domains '"example.com"')"

start_proxy apx-spike-proxy-gh apx-spike-net-gh --network-alias egress
docker network connect apx-spike-egress apx-spike-proxy-gh
start_proxy apx-spike-proxy-ex apx-spike-net-ex --network-alias egress
docker network connect apx-spike-egress apx-spike-proxy-ex
require_ready apx-spike-proxy-gh
require_ready apx-spike-proxy-ex

curl_version=$(docker run "${cage_flags[@]}" --name apx-spike-cage --network none "$tools" curl --version)
printf 'MEASURE M0 curl_version_line="%s"\n' "${curl_version%%$'\n'*}"
printf 'MEASURE M1 gh_token=%s\n' "$token_kind"

check E1 200 "$(http apx-spike-net-gh https://api.github.com/zen)"
check E2 exit56 "$(http apx-spike-net-gh https://example.com)"
check E3 200 "$(http apx-spike-net-ex https://example.com)"
check E4 exit56 "$(http apx-spike-net-ex https://api.github.com/zen)"
check E5 'exit6|exit7' "$(http apx-spike-net-gh --noproxy '*' https://api.github.com/zen)"
check E6a exit5 "$(http apx-spike-net-none https://example.com)"
check E6b 'exit6|exit7' "$(http apx-spike-net-none --noproxy '*' https://example.com)"
check E7a fail "$(http apx-spike-net-gh --proxy http://apx-spike-proxy-ex:1080 https://example.com)"
ex_ip=$(docker inspect -f '{{(index .NetworkSettings.Networks "apx-spike-net-ex").IPAddress}}' apx-spike-proxy-ex)
printf 'MEASURE E7b proxy_ex_ip=%s\n' "$ex_ip"
check E7b fail "$(http apx-spike-net-gh --proxy "http://$ex_ip:1080" https://example.com)"

write_config "$ex_conf" allowlist "$(domains '"example.com", "api.github.com"')"
check E8a '200:{"status":"ok"}' "$(reload apx-spike-proxy-ex)"
check E8b 200 "$(http apx-spike-net-ex https://api.github.com/zen)"
check E8c exit56 "$(http apx-spike-net-gh https://example.com)"
write_config "$ex_conf" apx-spike-no-such-transform "$(domains '"example.com"')"
bad_reload=$(reload apx-spike-proxy-ex)
printf 'MEASURE E8d reply=%s\n' "$bad_reload"
check E8d 422 "${bad_reload%%:*}"
check E8e 200 "$(http apx-spike-net-ex https://api.github.com/zen)"

write_config "$srcip_conf" allowlist "$(printf '      rules:\n        - host: "example.com"\n          source_ip: "172.31.9.0/24"')"
start_proxy apx-spike-proxy-srcip apx-spike-egress
if ! wait_ready apx-spike-proxy-srcip; then
  srcip=refused-at-start
else
  srcip_reload=$(reload apx-spike-proxy-srcip)
  printf 'MEASURE E9 reload=%s\n' "$srcip_reload"
  srcip_request=$(http container:apx-spike-proxy-srcip --proxy http://127.0.0.1:1080 https://example.com)
  printf 'MEASURE E9 request=%s\n' "$srcip_request"
  if [ "${srcip_reload%%:*}" = 422 ]; then
    srcip=reload-422
  elif [ "$srcip_request" = 200 ]; then
    srcip=accepted-and-ignored
  elif [ "$srcip_request" = exit56 ]; then
    srcip=accepted-and-enforced
  else
    srcip="inconclusive-$srcip_request"
  fi
fi
case $srcip in
  refused-at-start | reload-422 | accepted-and-ignored) report E9 unsupported "$srcip" PASS ;;
  *) report E9 unsupported "$srcip" FAIL ;;
esac
if [ "$srcip" = refused-at-start ]; then
  echo 'CHECK E9b SKIP source-IP proxy did not start'
else
  check E9b exit56 "$(http container:apx-spike-proxy-srcip --proxy http://127.0.0.1:1080 https://www.google.com)"
fi

rc=0
cage apx-spike-net-gh git ls-remote https://github.com/paradigmxyz/iron-proxy.git HEAD >/dev/null 2>&1 || rc=$?
check E10 exit0 "exit$rc"

rc=0
gh_err=$(cage_gh apx-spike-net-gh gh api /user 2>&1 >/dev/null) || rc=$?
if [ "$rc" -eq 0 ]; then
  gh_obs=exit0
elif [[ "$gh_err" == *"HTTP 401"* ]]; then
  gh_obs=http401
else
  gh_obs="exit$rc"
fi
if [ "$token_kind" = real ]; then
  check E11 exit0 "$gh_obs"
else
  check E11 http401 "$gh_obs"
fi

if [ "$token_kind" = real ]; then
  rc=0
  # shellcheck disable=SC2016 # $GH_TOKEN must expand inside the cage, never on this host's command line.
  helper='!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
  cage_gh apx-spike-net-gh git -c credential.helper= -c "credential.helper=$helper" \
    ls-remote https://github.com/Gravital-Forge/agent-proxy.git HEAD >/dev/null 2>&1 || rc=$?
  check E12 exit0 "exit$rc"
else
  echo 'CHECK E12 SKIP GH_TOKEN unset, dummy token in use'
fi
