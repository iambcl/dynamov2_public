#!/bin/bash
set -euo pipefail

NS1="ns1"
NS2="ns2"

RUN_NS1_DIR="/var/run/docker-ns1"
RUN_NS2_DIR="/var/run/docker-ns2"

ANCHOR_NS1_PIDFILE="/var/run/netns-anchor-${NS1}.pid"
ANCHOR_NS2_PIDFILE="/var/run/netns-anchor-${NS2}.pid"

DOCKER_NS1_DIR="/var/lib/docker-ns1"
DOCKER_NS2_DIR="/var/lib/docker-ns2"

NS1_HOST_IF="veth2"
NS2_HOST_IF="veth4"

UPLINK_IF="wlp2s0"

if [ "$EUID" -eq 0 ]; then
  echo "Run this as your regular user, not root."
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required."
  exit 1
fi

as_root() {
  sudo "$@"
}

remove_context_if_exists() {
  local ctx="$1"
  docker context inspect "$ctx" >/dev/null 2>&1 && docker context rm -f "$ctx" >/dev/null 2>&1 || true
}

leave_swarm_if_active() {
  local ctx="$1"
  if docker --context "$ctx" info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null | grep -q '^active$'; then
    docker --context "$ctx" swarm leave --force || true
  fi
}

stop_worker_daemon() {
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

  as_root rm -f "$run_dir/docker.sock" 2>/dev/null || true
}

stop_anchor() {
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

remove_iptables_rule_if_exists() {
  local table="$1"
  shift
  while as_root iptables -t "$table" -C "$@" >/dev/null 2>&1; do
    as_root iptables -t "$table" -D "$@"
  done
}

echo "=== Workers leaving swarm ==="
leave_swarm_if_active "$NS1"
leave_swarm_if_active "$NS2"

echo "=== Removing worker contexts ==="
remove_context_if_exists "$NS1"
remove_context_if_exists "$NS2"

echo "=== Stopping worker dockerd daemons ==="
stop_worker_daemon "$RUN_NS1_DIR"
stop_worker_daemon "$RUN_NS2_DIR"

echo "=== Stopping namespace anchors ==="
stop_anchor "$ANCHOR_NS1_PIDFILE"
stop_anchor "$ANCHOR_NS2_PIDFILE"

echo "=== Removing namespaces and links ==="
delete_ns_if_exists "$NS1"
delete_ns_if_exists "$NS2"
delete_link_if_exists "$NS1_HOST_IF"
delete_link_if_exists "$NS2_HOST_IF"

echo "=== Removing per-namespace DNS files ==="
as_root rm -rf "/etc/netns/$NS1" "/etc/netns/$NS2"

echo "=== Removing firewall/NAT rules added by setup ==="
remove_iptables_rule_if_exists filter FORWARD -i "$NS1_HOST_IF" -o "$NS2_HOST_IF" -j ACCEPT
remove_iptables_rule_if_exists filter FORWARD -i "$NS2_HOST_IF" -o "$NS1_HOST_IF" -j ACCEPT
remove_iptables_rule_if_exists filter FORWARD -i "$NS1_HOST_IF" -o "$UPLINK_IF" -j ACCEPT
remove_iptables_rule_if_exists filter FORWARD -i "$NS2_HOST_IF" -o "$UPLINK_IF" -j ACCEPT
remove_iptables_rule_if_exists filter FORWARD -i "$UPLINK_IF" -o "$NS1_HOST_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT
remove_iptables_rule_if_exists filter FORWARD -i "$UPLINK_IF" -o "$NS2_HOST_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT

remove_iptables_rule_if_exists nat POSTROUTING -s 172.16.1.0/30 -o "$UPLINK_IF" -j MASQUERADE
remove_iptables_rule_if_exists nat POSTROUTING -s 172.16.2.0/30 -o "$UPLINK_IF" -j MASQUERADE

remove_iptables_rule_if_exists filter INPUT -i "$NS1_HOST_IF" -p tcp --dport 2377 -j ACCEPT
remove_iptables_rule_if_exists filter INPUT -i "$NS2_HOST_IF" -p tcp --dport 2377 -j ACCEPT
remove_iptables_rule_if_exists filter INPUT -i "$NS1_HOST_IF" -p tcp --dport 7946 -j ACCEPT
remove_iptables_rule_if_exists filter INPUT -i "$NS2_HOST_IF" -p tcp --dport 7946 -j ACCEPT
remove_iptables_rule_if_exists filter INPUT -i "$NS1_HOST_IF" -p udp --dport 7946 -j ACCEPT
remove_iptables_rule_if_exists filter INPUT -i "$NS2_HOST_IF" -p udp --dport 7946 -j ACCEPT
remove_iptables_rule_if_exists filter INPUT -i "$NS1_HOST_IF" -p udp --dport 4789 -j ACCEPT
remove_iptables_rule_if_exists filter INPUT -i "$NS2_HOST_IF" -p udp --dport 4789 -j ACCEPT

echo "=== Removing worker daemon state ==="

as_root rm -rf "$DOCKER_NS1_DIR" "$DOCKER_NS2_DIR" "$RUN_NS1_DIR" "$RUN_NS2_DIR"
echo "Worker Docker data removed."
docker swarm leave --force >/dev/null 2>&1 || true
echo
echo "Cleanup complete."