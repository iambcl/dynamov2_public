"""Utility wrapper around the Copilot SDK to send prompts and return results.

Provides a single async entrypoint `run_copilot_prompt` that encapsulates
the `CopilotClient` session creation, sending the prompt, and parsing the
returned stdout into structured logs when possible.
"""
from __future__ import annotations
import asyncio
import os
import time
from typing import Any, Optional, Tuple
from dotenv import load_dotenv
load_dotenv()
from copilot import CopilotClient
from dynamov2.logger.logger import CustomLogger
from dynamov2.database.db_helper import db_helper
import json
from generate_env_files_from_agent_run import generate_env_from_database

SYSTEM_PROMPT = \
"""You are an agent responsible for checking if a docker compose file is working correctly with docker. 
You will need to start docker containers with the tools available to you. 
If there is any errors, you will need to make changes to the code where the error occurred with the tools available to you.
A .env file will be created at the root directory. 
If the error has to do with the .env file, the .env file should be copied to the correct location referenced by the error message.
You have access to the PythonREPLTool. Use it whenever computation is needed. 
If there is a port conflict, make changes to the port used in the docker compose file.
You will only have access to the download_directory folder that has been mounted in a docker container.
Only update files within the download_directory folder.
You will be given a list of paths to docker compose files relative to the download_directory.
Keep track of what actions you have taken and provide it at the end in a list such as: ["step 1", "step 2", "step 3"].
DO NOT use any tools other than the tools provided by the dynamov2_mcp server.
"""

async def run_copilot_prompt(
    prompt: str,
    model: Optional[str] = None,
    logger: Optional[Any] = None,
) -> Tuple[str, dict, float]:
    """
    Send `prompt` to Copilot and return (stdout, parsed_logs, latency_seconds).
    """
    # prefer explicit model, then environment CODEX_MODEL, then fallback
    model = model or os.getenv("CODEX_MODEL") or "gpt-5-mini"
    current_dir = os.getcwd()
    codex_stdout = ""
    session = None
    start = time.perf_counter()
    print("Trying to start copilot client")
    # os.chdir(os.getenv("REPO_DIRECTORY"))
    client = CopilotClient({
        "cli_url": "localhost:43849",
        "cwd": os.getenv("REPO_DIRECTORY"),

    })
    await client.start()
    print("Copilot client started")
    try:
        session = await client.create_session({
                                                "model": model, 
                                                "system_message": {
                                                    "mode": "append",
                                                    "content": SYSTEM_PROMPT},
                                                # "available_tools": ["dynamov2_mcp/copy_env_file", "dynamov2_mcp/docker_container_check_v1", "dynamov2_mcp/read_file", "dynamov2_mcp/write_docker_compose_file", "dynamov2_mcp/write_env_file", "dynamov2_mcp/write_dockerfile"],
                                                "mcp_servers": {
                                                    "dynamov2_mcp": {
                                                        "url": "http://192.168.15.102:8000/mcp",
                                                        "type": "http",
                                                        "tools": [
                                                            'copy_env_file', 
                                                            'docker_container_check_v1', 
                                                            'read_file', 
                                                            'write_docker_compose_file', 
                                                            'write_dockerfile', 
                                                            'write_env_file'
                                                        ]
                                                    }
                                                }
                                            })
        print("Session created. Sending prompt...")
        response = await session.send_and_wait({"prompt": f"{prompt}"},timeout=3000)
        codex_stdout = response.data.content
        print(codex_stdout)
    except Exception as exc:
        if logger:
            logger.info(f"Exception during Copilot session: {exc}")
        codex_stdout = ""
    finally:
        if session is not None:
            await session.destroy()

    latency = time.perf_counter() - start

    try:
        parsed = json.loads(codex_stdout)
    except Exception as exc:
        if logger:
            logger.info(f"Failed to parse codex stdout: {exc}")
        parsed = {"working": False, "steps_taken": [], "parse_error": str(exc)}
    # os.chdir(current_dir)
    return codex_stdout, parsed, latency

if __name__ == '__main__':
    async def main():
        file_path = "list_of_repo.txt"  # or the absolute path if you prefer
        logger = CustomLogger('copilot_runner', logfile_name="copilot_runner.log")

        # Read raw text and lines
        with open(file_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        for line in lines:
            await asyncio.sleep(5)  # brief pause between runs
            repo_id, run_id, model = line.split()
            repo_id = int(repo_id)
            run_id = int(run_id)
            model = model[1:-1]  # remove quotes
            print("Currently processing repo_id:", repo_id, "run_id:", run_id, "model:", model)
            human_prompt = generate_env_from_database(repository_id=repo_id, run_id=run_id, model=model)

            try:
                codex_stdout, parsed, latency = await run_copilot_prompt(prompt=human_prompt, logger=logger, model="gpt-5-mini")
            except Exception as e:
                print(f"Error running copilot prompt for repo_id {repo_id}, run_id {run_id}: {e}")
                continue

            success, msg, row = db_helper.record_agent_run_result(
                repository_id=repo_id,
                run_id=15,
                model="gpt-5-mini",
                env_result={},
                codex_result={**parsed, "latency_seconds": latency},
                codex_stdout=codex_stdout
            )

    # Run the async main once to manage a single event loop for all prompts
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")