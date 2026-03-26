"""Helpers for docker compose deployments used by agents."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Sequence

from ..database.db_helper import db_helper


def deploy_compose_and_record_agent_results(
    repository_id: int,
    compose_paths: list[str],
    repository_name: str,
    pcap_time: int = 60,
    pcap_location: str | None = None,
    *,
    run_id: str | None = None,
    codex_model: str | None = None,
    repo_directory: str | Path | None = None,
    docker_cmd: Sequence[str] | None = None,
    pcap_root: str | Path | None = None,
    pcap_size_check_limit: int = 10**6,
) -> dict[str, object]:
    """
    Run the docker compose deployment flow and persist the outcome to
    agent_traffic_parameters. Mirrors the stage 2 process but targets
    agent-specific tables.
    """
    repo = db_helper.get_github_repository(repository_id=repository_id)
    if not repo:
        return {
            "success": False,
            "failure_reason": f"Repository with ID {repository_id} not found",
        }

    resolved_run_id = run_id or os.getenv("RUN_ID")
    if not resolved_run_id:
        return {
            "success": False,
            "failure_reason": "RUN_ID environment variable is not set",
        }

    repo_dir_value = repo_directory or os.getenv("REPO_DIRECTORY")
    if not repo_dir_value:
        return {
            "success": False,
            "failure_reason": "REPO_DIRECTORY environment variable is not set",
        }
    repo_dir = Path(repo_dir_value).resolve()
    if not repo_dir.exists():
        return {
            "success": False,
            "failure_reason": f"Repository directory {repo_dir} does not exist",
        }

    resolved_docker_cmd = list(docker_cmd) if docker_cmd else ["docker"]
    resolved_model = codex_model or os.getenv("CODEX_MODEL")

    def _sanitize_project_name(name: str) -> str:
        cleaned = re.sub(r"[^a-z0-9-]", "-", name.lower())
        cleaned = re.sub(r"-+", "-", cleaned).strip("-")
        return cleaned or f"repo-{repository_id}"

    project_name = _sanitize_project_name(repository_name)

    compose_cmd = [*resolved_docker_cmd, "compose", "--project-name", project_name]
    for compose_path in compose_paths:
        compose_cmd.extend(["-f", compose_path])

    def _compose_down() -> None:
        try:
            subprocess.run(
                compose_cmd
                + ["down", "--remove-orphans", "--volumes", "--rmi", "all"],
                check=False,
                cwd=repo_dir,
            )
        except Exception as cleanup_error:
            print(
                "Attempted cleanup after docker-compose failure raised: "
                f"{cleanup_error}"
            )

    def _record_failure(message: str) -> dict[str, object]:
        db_helper.update_agent_traffic_parameters(
            repository_id=repo.id,
            run_id=resolved_run_id,
            failure_reason=message,
            one_minute_check=False,
            processing_host=socket.gethostname(),
            model=resolved_model,
        )
        return {"success": False, "failure_reason": message}

    try:
        build_result = subprocess.run(
            compose_cmd + ["up", "--no-start"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=repo_dir,
            timeout=300,
        )
    except Exception as exc:
        _compose_down()
        return _record_failure(f"Exception while building containers: {exc}")

    if build_result.returncode != 0:
        error_output = (build_result.stdout or "").strip()
        message = (
            "Image building failed."
            f" Exit code {build_result.returncode}. {error_output}"
        ).strip()
        try:
            _compose_down()
        finally:
            return _record_failure(message)

    try:
        networks_output = subprocess.check_output(
            [
                *resolved_docker_cmd,
                "network",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={project_name.lower()}",
            ],
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        _compose_down()
        return _record_failure(f"Failed to list docker networks: {exc}")

    pattern = r"(\S+)\s+(\S+)\s+(?:bridge|host|overlay|macvlan|none|custom)\s+local"
    matches = re.findall(pattern, networks_output)
    bridges: list[str] = []
    subnets: list[tuple[str, str]] = []

    if matches:
        for network_id, network_name in matches:
            bridges.append(f"br-{network_id}")
            out = subprocess.check_output(
                [*resolved_docker_cmd, "network", "inspect", network_id]
            )
            info = json.loads(out)[0]
            cfgs = (info.get("IPAM") or {}).get("Config") or []
            for cfg in cfgs:
                subnet = cfg.get("Subnet")
                if subnet:
                    subnets.append((network_name, subnet))
    else:
        print("Network ID and names not found in the output.")

    if not bridges:
        bridges = ["docker0"]

    up_result = subprocess.run(
        compose_cmd + ["up", "-d"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=repo_dir,
    )
    if up_result.returncode != 0:
        error_output = (up_result.stdout or "").strip()
        message = (
            "Repo docker-compose failed to start containers. "
            f"Exit code {up_result.returncode}. {error_output}"
        ).strip()
        _compose_down()
        return _record_failure(message)

    hostname = socket.gethostname()
    safe_repo_name = (repository_name or f"repo_{repository_id}").replace("/", "_")
    model_tag = re.sub(r"[^A-Za-z0-9._:-]+", "_", (resolved_model or "unknown_model")).strip("_")
    run_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", str(resolved_run_id)).strip("_")
    default_pcap_name = (
        f"{repository_id}_{safe_repo_name}_{hostname}_{model_tag}_{run_tag}.pcap"
    )
    try:
        if pcap_location:
            pcap_path = Path(pcap_location)
        else:
            root = Path(pcap_root) if pcap_root else (Path.cwd() / "pcap")
            pcap_path = root / default_pcap_name
        pcap_path.parent.mkdir(parents=True, exist_ok=True)

        tshark_cmd = ["tshark"]
        for bridge in bridges:
            tshark_cmd.extend(["-i", bridge])
        tshark_cmd.extend(
            ["-w", str(pcap_path), "-F", "pcap", "-a", f"duration:{pcap_time}"]
        )

        subprocess.run(["touch", str(pcap_path)])
        subprocess.run(["chmod", "666", str(pcap_path)])
        subprocess.Popen(tshark_cmd)

        time.sleep(pcap_time)
    finally:
        _compose_down()

    try:
        size_bytes = pcap_path.stat().st_size
    except FileNotFoundError:
        return _record_failure("PCAP was not created.")

    if size_bytes < pcap_size_check_limit:
        return _record_failure("PCAP has not met the size requirements.")

    db_helper.update_agent_traffic_parameters(
        repository_id=repo.id,
        run_id=resolved_run_id,
        subnets=subnets,
        one_minute_check=True,
        failure_reason=None,
        processing_host=socket.gethostname(),
        model=resolved_model,
    )

    return {"success": True, "subnets": subnets, "pcap_location": str(pcap_path)}
