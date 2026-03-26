"""
OpenHands SWE runner that mirrors run_agents_ollama.py flow.
It uses OpenHands conversation logic in place of dockerized codex container execution.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv
from openhands.sdk import Agent, Conversation, Event, LLM, LLMConvertibleEvent

import mcp_server
from dynamo_src.helper.git_utils import clone_github_repo
from dynamov2.database.db_helper import db_helper
from dynamov2.docker_utils.deploy_compose import deploy_compose_and_record_agent_results
from dynamov2.logger.logger import CustomLogger
from generate_env_files_from_agent_run import HUMAN_PROMPT

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "dynamo_src"
DOCKER_ENV_PATH = SRC_DIR / "helper" / ".docker_env_ollama"
CODEX_MODEL = (
    os.getenv("OPENHANDS_MODEL")
    or os.getenv("CODEX_MODEL")
    or dotenv_values(DOCKER_ENV_PATH).get("CODEX_MODEL")
    or "gpt-5.2-codex"
)
MCP_URL = "http://192.168.15.102:8000/mcp"
OLLAMA_BASE_URL = os.getenv("BASE_URL")
RUN_ID = os.getenv("RUN_ID")
AGENT_RUN_TIMEOUT_SECONDS = 900

if not RUN_ID:
    print("Configure RUN_ID in .env file to proceed.")
    sys.exit()

logger = CustomLogger("openhands_runner", logfile_name="openhands_runner.log")
print(f"[openhands_runner] CODEX_MODEL={CODEX_MODEL}")
logger.info(f"Configured CODEX_MODEL={CODEX_MODEL}")


def _sanitize_repo_dirname(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return "repo"
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned)
    cleaned = cleaned.strip("._-")
    return cleaned or "repo"


def archive_download_directory(repository_name: str, repositories_root: Path | None = None) -> None:
    """Copy the current REPO_DIRECTORY (download_directory sandbox) into repository/<repository_name>."""

    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        logger.error("REPO_DIRECTORY is not set. Skipping repository archive")
        return

    source_path = Path(repo_directory)
    if not source_path.exists() or not source_path.is_dir():
        logger.error(f"REPO_DIRECTORY does not exist or is not a directory: {source_path}")
        return

    root = repositories_root or (ROOT_DIR / "repository")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.error(f"Failed creating repositories root {root}: {exc}")
        return

    dest_dirname = _sanitize_repo_dirname(repository_name)
    destination_path = root / dest_dirname

    try:
        destination_path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, destination_path, dirs_exist_ok=True, symlinks=True)
        logger.info(f"Archived download_directory: {source_path} -> {destination_path}")
    except Exception as exc:
        logger.error(f"Failed archiving download_directory to {destination_path}: {exc}")


def _is_ollama_model(model: str) -> bool:
    model_lower = (model or "").lower()
    if model_lower.startswith(("ollama/", "ollama_chat/")):
        return True
    if "qwen" in model_lower:
        return True
    return (os.getenv("OPENHANDS_LLM_BACKEND", "").lower() == "ollama")


def _to_ollama_model_name(model: str) -> str:
    if model.startswith(("ollama/", "ollama_chat/")):
        return model
    # Default to ollama provider; ollama_chat can return 405 on some servers.
    return f"ollama/{model}"


def _normalize_base_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/")


def _is_openai_compatible_url(url: str) -> bool:
    return url.endswith("/v1") or "/v1/" in url


def _apply_prompt_cache_retention_override(llm: LLM) -> LLM:
    """Ensure we don't send unsupported prompt caching params.

    OpenHands' LLM defaults `prompt_cache_retention="24h"` for GPT-5-family models.
    Some OpenAI-compatible gateways reject unknown/unsupported parameters, causing:
    "Unsupported parameter: prompt_cache_retention".

    Default behavior here is conservative: do not send this parameter unless the
    user explicitly opts in via `OPENHANDS_PROMPT_CACHE_RETENTION`.
    """

    if "OPENHANDS_PROMPT_CACHE_RETENTION" not in os.environ:
        llm.prompt_cache_retention = None
        return llm

    raw_value = os.getenv("OPENHANDS_PROMPT_CACHE_RETENTION")
    cleaned = (raw_value or "").strip()
    llm.prompt_cache_retention = cleaned or None
    return llm


def _build_llm_sync(model: str) -> LLM:
    if _is_ollama_model(model):
        resolved_base_url = _normalize_base_url(
            os.getenv("OPENHANDS_OLLAMA_BASE_URL")
            or os.getenv("OLLAMA_BASE_URL")
            or OLLAMA_BASE_URL
            or "http://127.0.0.1:11434"
        )
        if not resolved_base_url:
            raise ValueError("Ollama base URL is empty. Set OPENHANDS_OLLAMA_BASE_URL or OLLAMA_BASE_URL.")

        if _is_openai_compatible_url(resolved_base_url):
            # OpenAI-compatible endpoint mode (typically .../v1)
            print(f"[openhands_runner] LLM backend=ollama-openai-compatible model={model} base_url={resolved_base_url}")
            logger.info(
                f"Using Ollama OpenAI-compatible configuration for model={model}, base_url={resolved_base_url}"
            )
            llm = LLM(
                model=model,
                base_url=resolved_base_url,
                api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
                reasoning_effort="none",
                native_tool_calling=False,
            )
            return _apply_prompt_cache_retention_override(llm)

        ollama_model = _to_ollama_model_name(model)
        print(f"[openhands_runner] LLM backend=ollama-native model={ollama_model} base_url={resolved_base_url}")
        logger.info(f"Using Ollama native configuration for model={ollama_model}, base_url={resolved_base_url}")
        llm = LLM(
            model=ollama_model,
            base_url=resolved_base_url,
            ollama_base_url=resolved_base_url,
            api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
            # Qwen via Ollama can be strict on non-standard reasoning/tool params.
            reasoning_effort="none",
            native_tool_calling=False,
        )
        return _apply_prompt_cache_retention_override(llm)

    print(f"[openhands_runner] LLM backend=openai-subscription model={model}")
    logger.info(f"Using OpenAI subscription configuration for model={model}")
    llm = LLM.subscription_login(vendor="openai", model=model)
    return _apply_prompt_cache_retention_override(llm)


async def build_llm_from_model(model: str) -> LLM:
    if _is_ollama_model(model):
        return _build_llm_sync(model)
    # subscription_login uses asyncio.run() internally; isolate it in a thread.
    return await asyncio.to_thread(_build_llm_sync, model)


def sequence_compose_paths(compose_paths: list[str]) -> list[str]:
    """Ensure prod compose file overrides dev when both are present."""

    def _priority(path: str) -> int:
        name = Path(path).name.lower()
        if ".prod" in name:
            return 2
        if ".dev" in name:
            return 1
        return 0

    paths = list(compose_paths)
    if not paths:
        return paths

    has_dev = any(_priority(path) == 1 for path in paths)
    has_prod = any(_priority(path) == 2 for path in paths)
    if has_dev and has_prod:
        indexed_paths = list(enumerate(paths))
        indexed_paths.sort(key=lambda item: (_priority(item[1]), item[0]))
        return [path for _, path in indexed_paths]
    return paths


def ensure_env(env_response: object) -> None:
    """
    Ensure a .env file exists even if the env agent misses file creation.
    """
    if not env_response:
        logger.error("env_response is empty")
        return
    if isinstance(env_response, str):
        try:
            env_response = env_response.replace(": False", ": false").replace(": True", ": true")
            env_response = json.loads(env_response)
        except json.JSONDecodeError:
            logger.error("JSON decode error while parsing env response")
            return
    if not isinstance(env_response, dict):
        logger.error("env_response is not a dict despite error handling")
        return

    repo_dir = os.getenv("REPO_DIRECTORY")
    if not repo_dir:
        logger.error("REPO_DIRECTORY is not set. Skipping ensure_env")
        return

    raw_file_locations = env_response.get("env_location") or env_response.get("env_locations")
    if not raw_file_locations:
        raw_file_locations = [".env"]
    elif isinstance(raw_file_locations, str):
        raw_file_locations = [raw_file_locations]

    file_locations: list[str] = []
    for location in raw_file_locations:
        if not isinstance(location, str) or not location.strip():
            continue
        cleaned = location.strip()
        path_obj = Path(cleaned)
        name = path_obj.name.lower()
        if name.endswith((".yml", ".yaml")):
            continue
        if name == "env":
            path_obj = path_obj.with_name(".env")
        elif name != ".env" and not name.endswith(".env"):
            continue
        normalized = str(path_obj)
        if normalized not in file_locations:
            file_locations.append(normalized)

    if ".env" not in file_locations:
        file_locations.append(".env")

    env_vars = env_response.get("environmental_variables_added")
    if not isinstance(env_vars, dict) or not env_vars:
        env_vars = env_response.get("env_vars", {})
    if not isinstance(env_vars, dict) or not env_vars:
        logger.info("No env vars available. Skipping ensure_env")
        return

    lines = [f"{k}={v}" for k, v in env_vars.items() if v is not None]
    if not lines:
        logger.info("env_vars contains only empty values. Skipping ensure_env")
        return
    env_content = "\n".join(lines) + "\n"

    for file_location in file_locations:
        filepath = Path(repo_dir) / file_location
        if filepath.exists():
            continue
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(env_content, encoding="utf-8")
        except Exception as exc:
            logger.error(f"Failed writing env file {filepath}: {exc}")


def _extract_final_text(history: list[dict[str, Any]]) -> str:
    for msg in reversed(history):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return ""


def _strip_fenced_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped.strip()


async def initiate_conversation(compose_filepaths: list[str], human_prompt: str) -> tuple[str, dict[str, Any], float]:
    """Run OpenHands SWE conversation and return stdout-like text, parsed result, and latency."""

    llm_messages: list[Any] = []

    def conversation_callback(event: Event) -> None:
        if isinstance(event, LLMConvertibleEvent):
            llm_messages.append(event.to_llm_message())

    llm = await build_llm_from_model(CODEX_MODEL)
    if not MCP_URL:
        raise ValueError("MCP URL is empty. Set OPENHANDS_MCP_URL or MCP_URL.")

    mcp_config = {
        "mcpServers": {
            "dynamov2_mcp": {
                "url": MCP_URL,
                "type": "sse",
            }
        }
    }
    agent = Agent(
        llm=llm,
        mcp_config=mcp_config,
        system_prompt_filename=str(SRC_DIR / "dynamov2_system_prompt.j2"),
        system_prompt_kwargs={"compose_files": compose_filepaths},
        filter_tools_regex="^(copy_env_file|docker_container_check_v1|read_file|write_docker_compose_file|write_dockerfile|write_env_file)$",
    )

    working_directory = os.getenv("REPO_DIRECTORY")
    if not working_directory:
        raise ValueError("REPO_DIRECTORY environment variable not set")

    conversation = Conversation(agent=agent, callbacks=[conversation_callback], workspace=working_directory)
    conversation.send_message(human_prompt)

    started_at = time.perf_counter()
    if inspect.iscoroutinefunction(conversation.run):
        await conversation.run()
    else:
        await asyncio.to_thread(conversation.run)
    latency = time.perf_counter() - started_at

    history = [evt.model_dump(exclude_none=True) for evt in llm_messages]
    codex_stdout = _extract_final_text(history)
    cleaned_stdout = _strip_fenced_json(codex_stdout)

    codex_graph_logs: dict[str, Any] = {"working": False, "steps_taken": [], "history": history}
    if cleaned_stdout:
        try:
            parsed = json.loads(cleaned_stdout)
            if isinstance(parsed, dict):
                codex_graph_logs.update(parsed)
            else:
                codex_graph_logs["raw"] = cleaned_stdout
        except json.JSONDecodeError:
            codex_graph_logs["raw"] = codex_stdout
    else:
        codex_graph_logs["raw"] = codex_stdout

    return codex_stdout, codex_graph_logs, latency


async def run_agents(repo_url: str, compose_paths: list[str]) -> tuple[object, object]:
    """Run env extraction then OpenHands SWE codex pass, mirroring run_agents_ollama flow."""

    repo = db_helper.get_github_repository(url=repo_url)
    repository_id = repo.id if repo else None

    env_started_at = time.perf_counter()
    env_graph_logs = await mcp_server.read_environment_variables_v2()
    env_graph_logs["latency_seconds"] = time.perf_counter() - env_started_at
    print(env_graph_logs)
    ensure_env(env_graph_logs)

    codex_started_at = time.perf_counter()
    logger.info(f"Starting: openhands_swe for {repository_id}")
    compose_paths = sequence_compose_paths(compose_paths)
    human_prompt = HUMAN_PROMPT.format(docker_compose_filepaths=compose_paths)
    codex_stdout, codex_graph_logs, conversation_latency = await initiate_conversation(compose_paths, human_prompt)
    logger.info(f"Completed: openhands_swe for {repository_id}")
    codex_graph_logs["conversation_latency_seconds"] = conversation_latency
    codex_graph_logs["latency_seconds"] = time.perf_counter() - codex_started_at

    if repository_id is not None:
        db_helper.record_agent_run_result(
            repository_id=repository_id,
            run_id=RUN_ID,
            env_result=env_graph_logs,
            codex_result=codex_graph_logs,
            model=CODEX_MODEL,
            codex_stdout=codex_stdout,
        )
        logger.info("Updated: agent_run_results")
    else:
        print("Warning: Repository not found in database; skipping run result persistence.")
    return env_graph_logs, codex_graph_logs


async def main_async(repo_url: str, compose_paths: list[str], start_server: bool = True) -> tuple[object, object]:
    return await run_agents(repo_url, compose_paths)


def main(repo_url: str, compose_paths: list[str], start_server: bool = True) -> None:
    env_result, codex_result = asyncio.run(run_agents(repo_url, compose_paths))
    print("Environment agent result:", env_result)
    print("Codex agent result:", codex_result)


if __name__ == "__main__":
    async def _runner() -> None:
        count = 0
        while count < 500:
            row = db_helper.get_repository_with_build_or_start_failure(run_id=RUN_ID)
            logger.info(f"Retrieved row: {getattr(row, 'id', None)}")
            if not row:
                print("No repository with image build failure found; stopping.")
                break
            if not row.cleaned_docker_compose_filepath:
                count += 1
                db_helper.record_agent_run_result(
                    repository_id=row.id,
                    run_id=RUN_ID,
                    model=CODEX_MODEL,
                    env_result={"messages": []},
                    codex_result={"messages": []},
                    codex_stdout="",
                )
                logger.info("Retrieved row has no relevant docker compose file.")
                continue

            repo_id = row.id
            try:
                clone_github_repo(row.url, row.cleaned_docker_compose_filepath)
            except Exception as exc:
                logger.error(f"{row.id} clone error: {exc}")
                db_helper.record_agent_run_result(
                    repository_id=row.id,
                    run_id=RUN_ID,
                    model=CODEX_MODEL,
                    env_result={"messages": []},
                    codex_result={"messages": []},
                    codex_stdout="",
                )
                count += 1
                await asyncio.sleep(10)
                continue

            try:
                await asyncio.wait_for(
                    main_async(repo_url=row.url, compose_paths=row.cleaned_docker_compose_filepath),
                    timeout=AGENT_RUN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.info(f"Repository {repo_id} timed out after {AGENT_RUN_TIMEOUT_SECONDS} seconds.")

            result_row = db_helper.get_agent_run_result(row.id, CODEX_MODEL, RUN_ID)
            if result_row and result_row.codex_working:
                repository_name = getattr(row, "name", f"repo_{repo_id}")
                archive_download_directory(repository_name)
                deploy_compose_and_record_agent_results(
                    repository_id=repo_id,
                    repository_name=repository_name,
                    compose_paths=row.cleaned_docker_compose_filepath,
                    run_id=RUN_ID,
                    codex_model=CODEX_MODEL,
                )
            else:
                db_helper.update_agent_traffic_parameters(
                    repository_id=repo_id,
                    run_id=RUN_ID,
                    failure_reason="Failure at agent stage.",
                    one_minute_check=False,
                    processing_host=socket.gethostname(),
                    model=CODEX_MODEL,
                )
            logger.info(f"Repository {repo_id} completed.")
            count += 1

    asyncio.run(_runner())
