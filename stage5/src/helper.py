import subprocess
import asyncio
from datetime import datetime
import shutil
from dotenv import load_dotenv
load_dotenv()
import os
import socket
import re
import json
import time
import threading
from pathlib import Path
from dynamov2.database.db_helper import db_helper

VETHS = ["veth2", "veth4"]


def capture_ns1_traffic_pcap(
    *,
    repository_id: int,
    latency_ms: int,
    mbps_limit: int,
    qdisc: str,
    duration_seconds: int = 60,
    interface: str = "veth2",
    output_directory: Path = Path("/home/bingcheng/dynamov2/stage5/pcap"),
) -> tuple[Path, subprocess.Popen]:
    """Capture traffic entering/leaving ns1 and save it to a pcap file.

    Captures on the host-side ns1 veth (`veth2` by default), which includes
    both ingress and egress traffic for namespace `ns1`.
    """
    if shutil.which("tshark") is None:
        raise RuntimeError("tshark is not installed or not in PATH")

    output_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = (
        f"repo{repository_id}_ns1_{latency_ms}ms_{mbps_limit}mbps_{qdisc}_{timestamp}.pcap"
    )
    output_path = output_directory / file_name

    cmd = [
        "tshark",
        "-i",
        interface,
        "-w",
        str(output_path),
        "-F",
        "pcap",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return output_path, proc


def run_stack_for_duration(
    *,
    stack_file: Path,
    stack_name: str = "appstack",
    duration_seconds: int = 60,
    ctp_name: str | None = None,
) -> None:
    """Deploy a Docker stack, optionally play CTP, then tear it down."""

    def _safe_check_output(cmd: list[str]) -> str:
        try:
            return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
        except subprocess.CalledProcessError as exc:
            return (exc.output or "").strip()

    def _print_stack_placement(stack_name: str) -> None:
        print("=== Stack services ===")
        services_output = _safe_check_output(
            [
                "docker",
                "stack",
                "services",
                stack_name,
                "--format",
                "{{.Name}}\t{{.Replicas}}\t{{.Ports}}",
            ]
        )
        print(services_output or "(no services found)")

        service_names_raw = _safe_check_output(
            ["docker", "stack", "services", stack_name, "--format", "{{.Name}}"]
        )
        service_names = [line.strip() for line in service_names_raw.splitlines() if line.strip()]

        node_ids_raw = _safe_check_output(["docker", "node", "ls", "-q"])
        node_ids = [line.strip() for line in node_ids_raw.splitlines() if line.strip()]
        node_details: dict[str, str] = {}
        for node_id in node_ids:
            node_details[node_id] = _safe_check_output(
                [
                    "docker",
                    "node",
                    "inspect",
                    node_id,
                    "--format",
                    "{{.ID}}\t{{.Description.Hostname}}\t{{.Status.Addr}}\t{{index .Spec.Labels \"netns\"}}",
                ]
            )

        print("=== Stack task placement ===")
        if not service_names:
            print("(no tasks found)")
        for service_name in service_names:
            print(f"-- {service_name} --")
            task_ids_raw = _safe_check_output(
                [
                    "docker",
                    "service",
                    "ps",
                    service_name,
                    "-q",
                    "--no-trunc",
                ]
            )
            task_ids = [line.strip() for line in task_ids_raw.splitlines() if line.strip()]
            if not task_ids:
                print("(no tasks yet)")
                continue

            for task_id in task_ids:
                task_info = _safe_check_output(
                    [
                        "docker",
                        "inspect",
                        task_id,
                        "--format",
                        "{{.ID}}\t{{.NodeID}}\t{{.DesiredState}}\t{{.Status.State}}\t{{.Status.Err}}",
                    ]
                )
                parts = task_info.split("\t")
                node_id = parts[1] if len(parts) > 1 else ""
                node_line = node_details.get(node_id, "")
                print(f"{task_info}\t{node_line}")

        print("=== Swarm node labels (namespace mapping) ===")
        if not node_ids:
            print("(no swarm nodes found)")
            return

        for node_id in node_ids:
            print(node_details.get(node_id, ""))

    if not stack_file.exists():
        raise FileNotFoundError(f"Stack file not found: {stack_file}")

    subprocess.run(
        ["docker", "stack", "deploy", "-c", str(stack_file), stack_name],
        check=True,
        text=True,
        capture_output=True,
    )

    # Give scheduler a short moment to place tasks before reporting placement.
    time.sleep(5)
    _print_stack_placement(stack_name)

    ctp_threads: list[threading.Thread] = []
    ctp_results: dict[str, dict[str, str | int]] = {}
    if ctp_name:
        print(f"Starting cross traffic profile: {ctp_name}")
        ctp_threads, ctp_results = play_ctp(ctp_name)

    try:
        time.sleep(duration_seconds)
    finally:
        for thread in ctp_threads:
            thread.join(timeout=2)
        if ctp_results:
            print("=== CTP replay results ===")
            for direction in ("incoming", "outgoing"):
                result = ctp_results.get(direction)
                if not result:
                    print(f"{direction}: no result")
                    continue
                print(f"{direction}: returncode={result.get('returncode')}")
                stderr = str(result.get("stderr", "")).strip()
                if stderr:
                    print(f"{direction} stderr: {stderr}")
        subprocess.run(
            ["docker", "stack", "rm", stack_name],
            check=False,
            text=True,
            capture_output=True,
        )


def stop_capture_process(proc: subprocess.Popen) -> None:
    """Gracefully stop a tshark background process."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def remove_root_qdisc(veth):
    """Remove the root qdisc from a veth device if it exists."""
    remove_existing_command = ["sudo", "-S", "tc", "qdisc", "del", "dev", veth, "root"]
    subprocess.run(
        remove_existing_command,
        text=True,
        capture_output=True,
    )

def apply_tc_to_veths(veths, latency_ms, mbps_limit, qdisc, r2q=100):
    def _build_tc_tree(veth, latency_ms, mbps_limit, qdisc, r2q):
        """Build a single tc tree without conflicting root qdisc assignments."""
        if mbps_limit > 0:
            subprocess.run(
                [
                    "sudo", "-S", "tc", "qdisc", "add", "dev", veth,
                    "root", "handle", "1:", "htb", "default", "10", "r2q", str(r2q),
                ],
                text=True,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "sudo", "-S", "tc", "class", "add", "dev", veth,
                    "parent", "1:", "classid", "1:10", "htb",
                    "rate", f"{mbps_limit}Mbit", "ceil", f"{mbps_limit}Mbit",
                ],
                text=True,
                check=True,
                capture_output=True,
            )

            parent_for_qdisc = "1:10"
            if latency_ms > 0:
                subprocess.run(
                    [
                        "sudo", "-S", "tc", "qdisc", "add", "dev", veth,
                        "parent", "1:10", "handle", "10:", "netem",
                        "delay", f"{latency_ms}ms",
                    ],
                    text=True,
                    check=True,
                    capture_output=True,
                )
                parent_for_qdisc = "10:1"

            if qdisc:
                subprocess.run(
                    [
                        "sudo", "-S", "tc", "qdisc", "add", "dev", veth,
                        "parent", parent_for_qdisc, "handle", "20:", qdisc,
                    ],
                    text=True,
                    check=True,
                    capture_output=True,
                )
            return

        if latency_ms > 0:
            subprocess.run(
                [
                    "sudo", "-S", "tc", "qdisc", "add", "dev", veth,
                    "root", "handle", "10:", "netem", "delay", f"{latency_ms}ms",
                ],
                text=True,
                check=True,
                capture_output=True,
            )
            if qdisc:
                subprocess.run(
                    [
                        "sudo", "-S", "tc", "qdisc", "add", "dev", veth,
                        "parent", "10:1", "handle", "20:", qdisc,
                    ],
                    text=True,
                    check=True,
                    capture_output=True,
                )
            return

        if qdisc:
            subprocess.run(
                [
                    "sudo", "-S", "tc", "qdisc", "add", "dev", veth,
                    "root", "handle", "20:", qdisc,
                ],
                text=True,
                check=True,
                capture_output=True,
            )

    for veth in veths:
        remove_root_qdisc(veth)
        _build_tc_tree(veth, latency_ms, mbps_limit, qdisc, r2q)

def play_ctp(ctpName):
    bg_location = os.getenv("CTP_PROFILES")
    outgoing_cmd = [
        "sudo",
        "-S",
        "ip",
        "netns",
        "exec",
        "ns1",
        "tcpreplay-edit",
        "-i",
        "veth1",
        "--pnat=169.231.0.0/16:172.16.1.1,128.111.0.0/16:172.16.1.1",
        f"{bg_location}/outgoing/{ctpName}",
    ]

    incoming_cmd = [
        "sudo",
        "-S",
        "ip",
        "netns",
        "exec",
        "ns2",
        "tcpreplay-edit",
        "-i",
        "veth3",
        "--pnat=169.231.0.0/16:172.16.1.1,128.111.0.0/16:172.16.1.1",
        f"{bg_location}/incoming/{ctpName}",
    ]

    results: dict[str, dict[str, str | int]] = {}

    def _run_and_capture(direction: str, command: list[str]) -> None:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
        )
        results[direction] = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    t1 = threading.Thread(target=_run_and_capture, args=("incoming", incoming_cmd))
    t2 = threading.Thread(target=_run_and_capture, args=("outgoing", outgoing_cmd))

    t1.start()
    t2.start()
    return [t1, t2], results



if __name__ == "__main__":
    from prepare_directory import prepare_directory
    from compose_to_swarm_copilot import convert_compose_files_to_swarm

    try:
        row = db_helper.get_github_repository(563)
        subprocess.run(["bash", "src/setup.sh"], text=True, check=True)
        prepare_directory(563)
        apply_tc_to_veths(VETHS, latency_ms=200, mbps_limit=10, qdisc="fq_codel")
        compose_relative_paths = [
            path.strip()
            for path in str(row.cleaned_docker_compose_filepath).split(",")
            if path.strip()
        ]
        output_path = asyncio.run(
            convert_compose_files_to_swarm(
                compose_relative_paths=["docker/docker-compose.yml"],
                client_services=["mc"],
                server_services=["db", "object-storage","pgadmin"],
            )
        )
        print(f"Generated swarm file: {output_path}")
        pcap_path, capture_proc = capture_ns1_traffic_pcap(
            repository_id=row.id,
            latency_ms=200,
            mbps_limit=10,
            qdisc="fq_codel",
            duration_seconds=60,
        )
        print(f"Started capture: {pcap_path}")
        try:
            run_stack_for_duration(
                stack_file=output_path,
                stack_name="appstack",
                duration_seconds=60,
                ctp_name="on_70_profile8.pcap",
            )
        finally:
            stop_capture_process(capture_proc)
            print(f"Capture saved: {pcap_path}")
        #play ctp
        # play_ctp("on_70_profile8.pcap")
    finally:
        for veth in VETHS:
            remove_root_qdisc(veth)
        subprocess.run(["bash", "src/teardown.sh"], text=True)