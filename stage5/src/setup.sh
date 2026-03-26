#!/bin/bash
set -euo pipefail

# ----------------------------
# Config
# ----------------------------
NS1="ns1"
NS2="ns2"

NS1_HOST_IF="veth2"
NS1_NS_IF="veth1"
NS1_HOST_IP="172.16.1.2/30"
NS1_NS_IP="172.16.1.1/30"
NS1_NS_ADDR="172.16.1.1"
NS1_HOST_ADDR="172.16.1.2"

NS2_HOST_IF="veth4"
NS2_NS_IF="veth3"
NS2_HOST_IP="172.16.2.2/30"
NS2_NS_IP="172.16.2.1/30"
NS2_NS_ADDR="172.16.2.1"
NS2_HOST_ADDR="172.16.2.2"

# Change this if needed
UPLINK_IF="wlp2s0"

# Separate Docker state per worker daemon
DOCKER_NS1_DIR="/var/lib/docker-ns1"
DOCKER_NS2_DIR="/var/lib/docker-ns2"
RUN_NS1_DIR="/var/run/docker-ns1"
RUN_NS2_DIR="/var/run/docker-ns2"

ANCHOR_NS1_PIDFILE="/var/run/netns-anchor-${NS1}.pid"
ANCHOR_NS2_PIDFILE="/var/run/netns-anchor-${NS2}.pid"

DOCKERD_NS1_LOG="/tmp/dockerd-${NS1}.log"
DOCKERD_NS2_LOG="/tmp/dockerd-${NS2}.log"

# ----------------------------
# Guards
# ----------------------------
if [ "$EUID" -eq 0 ]; then
  echo "Run this as your regular user, not root."
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required."
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker CLI is required."
  exit 1
fi

as_root() {
  sudo "$@"
}

ipt_has_rule() {
  local table="$1"
  shift
  as_root iptables -t "$table" -C "$@" >/dev/null 2>&1
}

ensure_ipt_rule() {
  local table="$1"
  shift
  if ! ipt_has_rule "$table" "$@"; then
    as_root iptables -t "$table" -A "$@"
  fi
}

cleanup_worker_daemon() {
  local run_dir="$1"
  local pidfile="$run_dir/docker.pid"

  if as_root test -f "$pidfile"; then
    local pid
    pid="$(as_root cat "$pidfile" 2>/dev/null || true)"
    if [ -n "${pid:-}" ]; then
      as_root kill "$pid" 2>/dev/null || true
      sleep 1
      as_root kill -9 "$pid" 2>/dev/null || true
    fi
    as_root rm -f "$pidfile"
  fi

  as_root rm -f "$run_dir/docker.sock" "$run_dir/containerd/containerd.sock" 2>/dev/null || true
}

cleanup_anchor() {
  local pidfile="$1"
  if as_root test -f "$pidfile"; then
    local pid
    pid="$(as_root cat "$pidfile" 2>/dev/null || true)"
    if [ -n "${pid:-}" ]; then
      as_root kill "$pid" 2>/dev/null || true
      sleep 1
      as_root kill -9 "$pid" 2>/dev/null || true
    fi
    as_root rm -f "$pidfile"
  fi
}

delete_ns_if_exists() {
  local ns="$1"
  as_root ip netns list | grep -qw "$ns" && as_root ip netns delete "$ns" || true
}

delete_link_if_exists() {
  local link="$1"
  as_root ip link show "$link" >/dev/null 2>&1 && as_root ip link del "$link" || true
}

start_anchor() {
  local ns="$1"
  local pidfile="$2"

  cleanup_anchor "$pidfile"
  as_root sh -c "ip netns exec $ns sleep infinity >/dev/null 2>&1 & echo \$! > $pidfile"
  local pid
  pid="$(as_root cat "$pidfile")"
  echo "Started anchor for $ns: PID $pid"
}

start_dockerd_in_ns() {
  local ns="$1"
  local anchor_pidfile="$2"
  local data_dir="$3"
  local run_dir="$4"
  local log_file="$5"

  local anchor_pid
  anchor_pid="$(as_root cat "$anchor_pidfile")"

  as_root mkdir -p "$data_dir" "$run_dir"

  cleanup_worker_daemon "$run_dir"

  echo "Starting dockerd in $ns ..."
  as_root nsenter -t "$anchor_pid" -n sh -c "
    nohup dockerd \
      --host=unix://$run_dir/docker.sock \
      --data-root=$data_dir \
      --exec-root=$run_dir \
      --pidfile=$run_dir/docker.pid \
      --exec-opt native.cgroupdriver=systemd \
      > $log_file 2>&1 &
  "

  for _ in $(seq 1 40); do
    if docker -H "unix://$run_dir/docker.sock" version >/dev/null 2>&1; then
      echo "dockerd in $ns is ready"
      return 0
    fi
    sleep 1
  done

  echo "dockerd in $ns failed to come up. Check $log_file"
  exit 1
}

ensure_context() {
  local name="$1"
  local sock="$2"

  docker context inspect "$name" >/dev/null 2>&1 && docker context rm -f "$name" >/dev/null 2>&1 || true
  docker context create "$name" --docker "host=unix://$sock" >/dev/null
}

docker_host_swarm_state() {
  docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || echo "inactive"
}

docker_ctx_swarm_state() {
  local ctx="$1"
  docker --context "$ctx" info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || echo "inactive"
}

# ----------------------------
# Stop old workers / anchors / contexts
# ----------------------------
echo "Cleaning old worker daemons and anchors ..."
cleanup_worker_daemon "$RUN_NS1_DIR"
cleanup_worker_daemon "$RUN_NS2_DIR"
cleanup_anchor "$ANCHOR_NS1_PIDFILE"
cleanup_anchor "$ANCHOR_NS2_PIDFILE"

docker context inspect "$NS1" >/dev/null 2>&1 && docker context rm -f "$NS1" >/dev/null 2>&1 || true
docker context inspect "$NS2" >/dev/null 2>&1 && docker context rm -f "$NS2" >/dev/null 2>&1 || true

# ----------------------------
# Recreate namespaces and links
# ----------------------------
echo "Recreating namespaces ..."
delete_ns_if_exists "$NS1"
delete_ns_if_exists "$NS2"

delete_link_if_exists "$NS1_HOST_IF"
delete_link_if_exists "$NS2_HOST_IF"

as_root ip netns add "$NS1"
as_root ip netns add "$NS2"

# ns1 <-> host
as_root ip link add "$NS1_NS_IF" type veth peer name "$NS1_HOST_IF"
as_root ip link set "$NS1_NS_IF" netns "$NS1"
as_root ip addr add "$NS1_HOST_IP" dev "$NS1_HOST_IF"
as_root ip link set "$NS1_HOST_IF" up
as_root ip netns exec "$NS1" ip addr add "$NS1_NS_IP" dev "$NS1_NS_IF"
as_root ip netns exec "$NS1" ip link set lo up
as_root ip netns exec "$NS1" ip link set "$NS1_NS_IF" up
as_root ip netns exec "$NS1" ip route replace default via "$NS1_HOST_ADDR"

# ns2 <-> host
as_root ip link add "$NS2_NS_IF" type veth peer name "$NS2_HOST_IF"
as_root ip link set "$NS2_NS_IF" netns "$NS2"
as_root ip addr add "$NS2_HOST_IP" dev "$NS2_HOST_IF"
as_root ip link set "$NS2_HOST_IF" up
as_root ip netns exec "$NS2" ip addr add "$NS2_NS_IP" dev "$NS2_NS_IF"
as_root ip netns exec "$NS2" ip link set lo up
as_root ip netns exec "$NS2" ip link set "$NS2_NS_IF" up
as_root ip netns exec "$NS2" ip route replace default via "$NS2_HOST_ADDR"

# worker-to-worker routes via host forwarding
as_root ip netns exec "$NS1" ip route replace 172.16.2.0/30 via "$NS1_HOST_ADDR" dev "$NS1_NS_IF"
as_root ip netns exec "$NS2" ip route replace 172.16.1.0/30 via "$NS2_HOST_ADDR" dev "$NS2_NS_IF"

# per-netns DNS
as_root mkdir -p "/etc/netns/$NS1" "/etc/netns/$NS2"
printf "nameserver 8.8.8.8\n" | as_root tee "/etc/netns/$NS1/resolv.conf" >/dev/null
printf "nameserver 8.8.8.8\n" | as_root tee "/etc/netns/$NS2/resolv.conf" >/dev/null

# ----------------------------
# Host forwarding / firewall
# ----------------------------
echo "Configuring forwarding and firewall ..."
as_root sysctl -w net.ipv4.ip_forward=1 >/dev/null

# inter-worker forwarding for swarm gossip + overlay
ensure_ipt_rule filter FORWARD -i "$NS1_HOST_IF" -o "$NS2_HOST_IF" -j ACCEPT
ensure_ipt_rule filter FORWARD -i "$NS2_HOST_IF" -o "$NS1_HOST_IF" -j ACCEPT
ensure_ipt_rule filter FORWARD -i "$NS1_HOST_IF" -o "$UPLINK_IF" -j ACCEPT
ensure_ipt_rule filter FORWARD -i "$NS2_HOST_IF" -o "$UPLINK_IF" -j ACCEPT
ensure_ipt_rule filter FORWARD -i "$UPLINK_IF" -o "$NS1_HOST_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT
ensure_ipt_rule filter FORWARD -i "$UPLINK_IF" -o "$NS2_HOST_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT

# NAT for outbound internet access from workers
ensure_ipt_rule nat POSTROUTING -s 172.16.1.0/30 -o "$UPLINK_IF" -j MASQUERADE
ensure_ipt_rule nat POSTROUTING -s 172.16.2.0/30 -o "$UPLINK_IF" -j MASQUERADE

# allow swarm ports from workers to host manager
ensure_ipt_rule filter INPUT -i "$NS1_HOST_IF" -p tcp --dport 2377 -j ACCEPT
ensure_ipt_rule filter INPUT -i "$NS2_HOST_IF" -p tcp --dport 2377 -j ACCEPT
ensure_ipt_rule filter INPUT -i "$NS1_HOST_IF" -p tcp --dport 7946 -j ACCEPT
ensure_ipt_rule filter INPUT -i "$NS2_HOST_IF" -p tcp --dport 7946 -j ACCEPT
ensure_ipt_rule filter INPUT -i "$NS1_HOST_IF" -p udp --dport 7946 -j ACCEPT
ensure_ipt_rule filter INPUT -i "$NS2_HOST_IF" -p udp --dport 7946 -j ACCEPT
ensure_ipt_rule filter INPUT -i "$NS1_HOST_IF" -p udp --dport 4789 -j ACCEPT
ensure_ipt_rule filter INPUT -i "$NS2_HOST_IF" -p udp --dport 4789 -j ACCEPT

# ----------------------------
# Start anchors and worker daemons
# ----------------------------
echo "Starting anchors ..."
start_anchor "$NS1" "$ANCHOR_NS1_PIDFILE"
start_anchor "$NS2" "$ANCHOR_NS2_PIDFILE"

echo "Starting worker dockerd instances ..."
start_dockerd_in_ns "$NS1" "$ANCHOR_NS1_PIDFILE" "$DOCKER_NS1_DIR" "$RUN_NS1_DIR" "$DOCKERD_NS1_LOG"
start_dockerd_in_ns "$NS2" "$ANCHOR_NS2_PIDFILE" "$DOCKER_NS2_DIR" "$RUN_NS2_DIR" "$DOCKERD_NS2_LOG"

echo "Creating Docker contexts ..."
ensure_context "$NS1" "$RUN_NS1_DIR/docker.sock"
ensure_context "$NS2" "$RUN_NS2_DIR/docker.sock"

# ----------------------------
# Create or reuse swarm manager on host
# ----------------------------
MANAGER_ADDR="$NS1_HOST_ADDR"

echo "Preparing swarm on host manager ..."
HOST_SWARM_STATE="$(docker_host_swarm_state)"
if [ "$HOST_SWARM_STATE" != "active" ]; then
  docker swarm init --advertise-addr "$MANAGER_ADDR"
else
  echo "Host is already in a swarm: $HOST_SWARM_STATE"
fi

WORKER_TOKEN="$(docker swarm join-token -q worker)"

# ----------------------------
# Join workers
# ----------------------------
echo "Joining workers to swarm ..."

if [ "$(docker_ctx_swarm_state "$NS1")" = "active" ]; then
  docker --context "$NS1" swarm leave --force || true
fi

if [ "$(docker_ctx_swarm_state "$NS2")" = "active" ]; then
  docker --context "$NS2" swarm leave --force || true
fi

docker --context "$NS1" swarm join \
  --token "$WORKER_TOKEN" \
  --advertise-addr "$NS1_NS_ADDR" \
  "${MANAGER_ADDR}:2377"

docker --context "$NS2" swarm join \
  --token "$WORKER_TOKEN" \
  --advertise-addr "$NS2_NS_ADDR" \
  "${MANAGER_ADDR}:2377"

# ----------------------------
# Verification
# ----------------------------
echo
echo "=== Swarm nodes ==="
docker node ls

echo
echo "=== Worker networks ==="
docker --context "$NS1" network ls
docker --context "$NS2" network ls

echo
echo "Done."
echo "Manager:"
echo "  docker node ls"
echo
echo "Workers:"
echo "  docker --context $NS1 info"
echo "  docker --context $NS2 info"
echo
echo "Logs:"
echo "  $DOCKERD_NS1_LOG"
echo "  $DOCKERD_NS2_LOG"