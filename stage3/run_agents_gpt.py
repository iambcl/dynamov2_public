"""
Helper script to run the environment and codex agents via their in-process LangGraph
entry points. Supply repository/compose inputs from an external entrypoint (e.g., main.py)
after loading them from your database. Starting the MCP server here is optional in case
one is already running elsewhere.
"""

from __future__ import annotations

import asyncio
import ast
import json
import subprocess
import time
import os
import re
import socket
import sys
from dotenv import dotenv_values
from pathlib import Path
from dynamo_src.helper.git_utils import clone_github_repo
from dynamov2.database.db_helper import db_helper
from dynamov2.logger.logger import CustomLogger
from langchain_core.load import loads
from dynamo_src.environment_agent import run_env_agent
from dynamo_src.run_container_gpt import (
    build_image,
    run_container,
    IMAGE_NAME,
    DOCKER_CMD,
)

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "dynamo_src"
MCP_PATH = SRC_DIR / "mcp.py"
DOCKER_ENV_PATH = SRC_DIR / "helper" / ".docker_env_gpt"
CODEX_MODEL = dotenv_values(DOCKER_ENV_PATH).get("CODEX_MODEL")
AGENT_RUN_TIMEOUT_SECONDS = 900
PCAP_SIZE_CHECK_LIMIT = 10 ** 6
RUN_ID = os.getenv("RUN_ID")
             
if not RUN_ID:
    print("Configure RUN_ID in .env file to proceed.")
    sys.exit()

def deploy_compose_and_record_agent_results(
    repository_id: int,
    compose_paths: list[str],
    repository_name: str,
    pcap_time: int = 60,
    pcap_location: str | None = None,
) -> dict[str, object]:
    """
    Run the docker compose deployment flow and persist the outcome to agent_traffic_parameters.
    Mirrors the stage_2 process but targets agent-specific tables.
    """
    repo = db_helper.get_github_repository(repository_id=repository_id)
    if not repo:
        return {"success": False, "failure_reason": f"Repository with ID {repository_id} not found"}

    repo_dir_env = os.getenv("REPO_DIRECTORY")
    if not repo_dir_env:
        return {"success": False, "failure_reason": "REPO_DIRECTORY environment variable is not set"}
    repo_dir = Path(repo_dir_env).resolve()
    if not repo_dir.exists():
        return {"success": False, "failure_reason": f"Repository directory {repo_dir} does not exist"}

    def _sanitize_project_name(name: str) -> str:
        cleaned = re.sub(r"[^a-z0-9-]", "-", name.lower())
        cleaned = re.sub(r"-+", "-", cleaned).strip("-")
        return cleaned or f"repo-{repository_id}"

    project_name = _sanitize_project_name(repository_name)

    compose_cmd = [*DOCKER_CMD, "compose", "--project-name", project_name]
    for compose_path in compose_paths:
        compose_cmd.extend(["-f", compose_path])

    def _compose_down() -> None:
        try:
            subprocess.run(
                compose_cmd + ["down", "--remove-orphans", "--volumes", "--rmi", "all"],
                check=False,
                cwd=repo_dir,
            )
        except Exception as cleanup_error:
            print(f"Attempted cleanup after docker-compose failure raised: {cleanup_error}")

    def _record_failure(message: str) -> dict[str, object]:
        db_helper.update_agent_traffic_parameters(
            repository_id=repo.id,
            run_id=RUN_ID,
            failure_reason=message,
            one_minute_check=False,
            processing_host=socket.gethostname(),
            model=CODEX_MODEL,
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
            [*DOCKER_CMD, "network", "ls", "--filter", f"label=com.docker.compose.project={project_name.lower()}"],
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return _record_failure(f"Failed to list docker networks: {exc}")

    pattern = r"(\S+)\s+(\S+)\s+(?:bridge|host|overlay|macvlan|none|custom)\s+local"
    matches = re.findall(pattern, networks_output)
    bridges: list[str] = []
    subnets: list[tuple[str, str]] = []

    if matches:
        for network_id, network_name in matches:
            bridges.append(f"br-{network_id}")
            out = subprocess.check_output([*DOCKER_CMD, "network", "inspect", network_id])
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

    # Derive a predictable pcap filename using the repository_id, name, and host.
    hostname = socket.gethostname()
    safe_repo_name = (repository_name or f"repo_{repository_id}").replace("/", "_")
    model_tag = re.sub(r"[^A-Za-z0-9._:-]+", "_", (CODEX_MODEL or "unknown_model")).strip("_")
    run_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", str(RUN_ID)).strip("_")
    default_pcap_name = f"{repository_id}_{safe_repo_name}_{hostname}_{model_tag}_{run_tag}.pcap"
    pcap_path = Path(pcap_location) if pcap_location else (ROOT_DIR / "pcap" / default_pcap_name)
    pcap_path.parent.mkdir(parents=True, exist_ok=True)

    tshark_cmd = ["tshark"]
    for bridge in bridges:
        tshark_cmd.extend(["-i", bridge])
    tshark_cmd.extend(["-w", str(pcap_path), "-F", "pcap", "-a", f"duration:{pcap_time}"])

    subprocess.run(["touch", str(pcap_path)])
    subprocess.run(["chmod", "666", str(pcap_path)])
    subprocess.Popen(tshark_cmd)

    time.sleep(pcap_time)
    _compose_down()

    try:
        size_bytes = pcap_path.stat().st_size
    except FileNotFoundError:
        return _record_failure("PCAP was not created.")

    if size_bytes < PCAP_SIZE_CHECK_LIMIT:
        return _record_failure("PCAP has not met the size requirements.")

    db_helper.update_agent_traffic_parameters(
        repository_id=repo.id,
        run_id=RUN_ID,
        subnets=subnets,
        one_minute_check=True,
        failure_reason=None,
        processing_host=socket.gethostname(),
        model=CODEX_MODEL,
    )

    return {"success": True, "subnets": subnets, "pcap_location": str(pcap_path)}


async def run_agents(repo_url: str, compose_paths: list[str]) -> tuple[object, object]:
    """Run the environment agent followed by the codex agent using their LangGraph APIs."""

    repo = db_helper.get_github_repository(url=repo_url)
    repository_id = repo.id if repo else None

    env_started_at = time.perf_counter()
    try:
        env_graph_logs = await run_env_agent(repository_id=repository_id)
    except Exception:
        env_latency = time.perf_counter() - env_started_at
        print("Error when running env_agent. Check Langsmith for details.")
        return None, None

    env_latency = time.perf_counter() - env_started_at
    env_graph_logs["latency_seconds"] = env_latency

    codex_started_at = time.perf_counter()
    codex_stdout = run_container(
        docker_compose_filepaths=compose_paths,
        repository_id=repository_id,
        run_id=RUN_ID,
    )
    try:
        codex_graph_logs = loads(codex_stdout)
    except Exception as exc:  # keep container stdout even if parsing fails
        print(f"Failed to parse codex stdout: {exc}")
        codex_graph_logs = {"working": False, "steps_taken": [], "parse_error": str(exc)}
    codex_latency = time.perf_counter() - codex_started_at
    codex_graph_logs["latency_seconds"] = codex_latency

    if repository_id is not None:
        db_helper.record_agent_run_result(
            repository_id=repository_id,
            run_id=RUN_ID,
            env_result=env_graph_logs,
            codex_result=codex_graph_logs,
            model=CODEX_MODEL,
            codex_stdout=codex_stdout,
        )
    else:
        print("Warning: Repository not found in database; skipping run result persistence.")
    return env_graph_logs, codex_graph_logs


async def main_async(repo_url: str, compose_paths: list[str], start_server: bool = True) -> tuple[object, object]:
    """Async entrypoint to run both agents and return their results."""
    return await run_agents(repo_url, compose_paths)

def main(repo_url: str, compose_paths: list[str], start_server: bool = True) -> None:
    env_result, codex_result = asyncio.run(
        run_agents(repo_url, compose_paths)
    )
    print("Environment agent result:", env_result)
    print("Codex agent result:", codex_result)

if __name__ == "__main__":
    async def _runner():
        # Always rebuild to ensure the container reflects current source (including logging the compose paths).
        build_image()
        count = 0
        ids = [#28834,
                # 39382,
                # 40492,
                # 43456,
                # 48932,
                60952,
                1424,
                71796,
                73351,
                77483,
                82335,
                83156,
                39789,
                45835,
                46775,
                55133,
                60751,
                66756,
                79612,
                85559,]
        # ids = db_helper.get_ids_from_table("agent_run_results")[100:110]
        # while count < 1:
        for id in ids:
            # row = db_helper.get_repository_with_build_or_start_failure(run_id=6, include_image_failure=False, include_size_requirement=True)
            row = db_helper.get_github_repository(repository_id=id)
            print("Retrieved row: ", row)
            if not row:
                print("No repository with image build failure found; stopping.")
                break

            repo_id = row.id
            try:
                clone_github_repo(row.url, row.cleaned_docker_compose_filepath)
            except Exception as e:
                print(f"Error: {e}")
                count += 1
                continue
            try:
                env_result, codex_result = await asyncio.wait_for(
                    main_async(
                        repo_url=row.url,
                        compose_paths=row.cleaned_docker_compose_filepath,
                    ),
                    timeout=AGENT_RUN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                env_result = {
                    "status": False,
                    "environmental_variables_added": {},
                    "env_location": None,
                    "latency_seconds": AGENT_RUN_TIMEOUT_SECONDS,
                    "timeout": True,
                }
                codex_result = {
                    "working": False,
                    "steps_taken": [],
                    "latency_seconds": AGENT_RUN_TIMEOUT_SECONDS,
                    "timeout": True,
                }
                print(
                    f"Repository {repo_id} timed out after "
                    f"{AGENT_RUN_TIMEOUT_SECONDS} seconds."
                )
            if codex_result["working"]:
                deploy_compose_and_record_agent_results(
                    repository_id=repo_id,
                    repository_name=getattr(row, "name", f"repo_{repo_id}"),
                    compose_paths=row.cleaned_docker_compose_filepath,
                )
            else:
                db_helper.update_agent_traffic_parameters(
                    repository_id=repo_id,
                    run_id=RUN_ID,
                    failure_reason="Failure at codex stage.",
                    one_minute_check=False,
                    processing_host=socket.gethostname(),
                    model="None",
                )
            logger.info(f"Repository {repo_id} completed.")
            count += 1
    logger = CustomLogger('run_agents_gpt', logfile_name="run_agents_gpt.log")
    # ids = db_helper.get_ids_from_table("agent_run_results")
    # count = 0
    # for id in ids:
    #     if id == 59909:
    #         print(count)
    #     count += 1
    asyncio.run(_runner())
