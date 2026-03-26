# Stage 5: Namespace + Docker Swarm Testbed

This folder sets up a local multi-node Docker Swarm testbed using Linux network namespaces (`ns1`, `ns2`) and host-side veth pairs. It is designed for traffic experiments (latency, bandwidth, qdisc), optional cross-traffic replay, and packet capture.

The main end-to-end example is in `src/helper.py`.

Previous stage: [stage4/README.md](../stage4/README.md)

## What This Stage Does

- Creates two Linux namespaces: `ns1` and `ns2`
- Creates veth pairs:
  - `ns1` side: `veth1` (inside ns1) <-> `veth2` (on host)
  - `ns2` side: `veth3` (inside ns2) <-> `veth4` (on host)
- Starts two namespace-scoped Docker daemons (workers)
- Creates Docker contexts `ns1` and `ns2`
- Initializes host Docker as Swarm manager and joins `ns1`/`ns2` as workers
- Configures iptables/NAT so workers can communicate and reach uplink
- Optionally applies traffic shaping (`tc`) and captures pcap with `tshark`

This stage is typically used for controlled experiments after Stage 4 feature extraction is available.

## Key Files

- `src/setup.sh`: Creates namespaces, networking, workers, and Swarm membership
- `src/teardown.sh`: Cleans up namespaces, workers, contexts, iptables, and swarm state
- `src/helper.py`: End-to-end orchestration example (setup, shaping, convert compose, deploy stack, capture, teardown)
- `src/compose_to_swarm_copilot.py`: Converts compose files to Swarm stack YAML and adds placement constraints
- `src/prepare_directory.py`: Clones/copies repository content into `$REPO_DIRECTORY`

## Prerequisites

1. Linux host with `sudo` access (run scripts as regular user, not root).
2. Docker Engine installed and working on host.
3. Tools used by scripts:
   - `ip`, `ip netns`, `iptables`, `tc`, `nsenter`
   - `dockerd` (for namespace worker daemons)
   - `tshark` (for pcap capture)
   - `tcpreplay-edit` (if using CTP replay)
4. Python >= 3.13 and project dependencies from `pyproject.toml`.
5. A valid uplink interface name on your machine (defaults to `wlp2s0` in scripts).

## Important Configuration

### 1. Uplink Interface

`src/setup.sh` and `src/teardown.sh` use:

```bash
UPLINK_IF="wlp2s0"
```

If your machine uses a different interface (for example `eth0`, `enp3s0`, `wlan0`), update both scripts before running.

### 2. Environment Variables

The code loads `.env` via `python-dotenv`. Common variables used in this stage:

- `REPO_DIRECTORY`: target path where repository content is prepared (`prepare_directory.py`)
- `REPOSITORY`: archive source root used when copying pre-archived repos
- `CTP_PROFILES`: base directory for cross-traffic pcaps (`incoming/` and `outgoing/`)

Example:

```bash
REPO_DIRECTORY=/home/you/dynamov2/stage5/repository
REPOSITORY=/home/you/dynamov2/stage5/repository
CTP_PROFILES=/home/you/dynamov2/stage5/ctps
```

## Setup and Run

### 1. Install Python dependencies

From `stage5/`:

```bash
pip install -e .
```

(or your preferred environment manager)

### 2. Build namespace + Swarm testbed

```bash
bash src/setup.sh
```

This will:

- recreate `ns1` and `ns2`
- start worker `dockerd` instances in those namespaces
- create Docker contexts `ns1` and `ns2`
- initialize/join Swarm

### 3. Verify Swarm and workers

```bash
docker node ls
docker --context ns1 info
docker --context ns2 info
```

Optional network checks:

```bash
docker --context ns1 network ls
docker --context ns2 network ls
sudo ip netns list
```

### 4. Run the end-to-end example (`helper.py`)

From `stage5/`:

```bash
python src/helper.py
```

`helper.py` flow (high level):

1. Runs `src/setup.sh`
2. Prepares repository content with `prepare_directory(...)`
3. Applies traffic shaping to host veths (`veth2`, `veth4`)
4. Converts compose to Swarm file using `convert_compose_files_to_swarm(...)`
5. Starts `tshark` capture on `veth2`
6. Deploys stack with `docker stack deploy`
7. Optionally replays cross traffic (`play_ctp`)
8. Waits for duration, tears stack down, stops capture
9. Removes qdisc and runs `src/teardown.sh`

### 5. Teardown manually (if needed)

```bash
bash src/teardown.sh
```

Use this when runs are interrupted and you need a clean state.

## Working with Your Own Compose Files

`convert_compose_files_to_swarm(...)` expects compose file paths relative to `$REPO_DIRECTORY`.

Example pattern from `helper.py`:

```python
output_path = asyncio.run(
    convert_compose_files_to_swarm(
        compose_relative_paths=["docker/docker-compose.yml"],
        client_services=["mc"],
        server_services=["db", "object-storage", "pgadmin"],
    )
)
```

Notes:

- The converter can auto-label swarm nodes with `node.labels.netns` (`ns1`/`ns2`) for placement constraints.
- Output stack file is written into the download repository root (default filename: `docker-stack.yml`).

## Troubleshooting

- `Run this as your regular user, not root.`
  - Execute scripts as your normal user; scripts call `sudo` internally.
- Worker dockerd fails to start:
  - Check `/tmp/dockerd-ns1.log` and `/tmp/dockerd-ns2.log`.
- Swarm join or overlay issues:
  - Re-check `UPLINK_IF`, iptables rules, and `docker node ls`.
- `tshark is not installed or not in PATH`:
  - Install `tshark` and ensure it is in `PATH`.
- `tcpreplay-edit` not found:
  - Install `tcpreplay` package if using CTP replay.

## Safety/Cleanup Notes

- The setup modifies host iptables and enables `net.ipv4.ip_forward`.
- `teardown.sh` removes rules it added and deletes namespace-scoped Docker state under:
  - `/var/lib/docker-ns1`, `/var/lib/docker-ns2`
  - `/var/run/docker-ns1`, `/var/run/docker-ns2`

Run teardown after experiments to avoid stale network and swarm state.

## Stage Integration

- Uses repository and traffic context produced by earlier stages.
- Produces controlled-environment PCAPs in `stage5/pcap/` for downstream analysis or validation experiments.
