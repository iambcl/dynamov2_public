from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import os
from pathlib import Path
from typing import Any
from dynamov2.git_utils.utils import clone_github_repo
from dynamov2.database.db_helper import db_helper


MODEL_NAME = "gpt-5-mini"
HUMAN_PROMPT = \
"""Run the docker compose files to check if there is any errors returned by the docker_container_check_v1 tool. 
If there is any errors, implement changes only in docker compose files, Dockerfile(s), or .env files based on the error message. 
If the error involves not having access to the docker image required, return False. (eg. requested access to the resource is denied)
If the error requires code changes, return False and describe the required change.
A .env file will be created at the root directory. 
You are running inside a docker container where the folder download_directory is mounted in the working directory.
If the error has to do with the .env file, the .env file should be copied to the correct location referenced by the error message.
To check the results of the code change, run the docker compose files again. 
The docker compose file paths are: {docker_compose_filepaths}. 
Return the final response in JSON format with the keys and format:
{{ working: True/False, steps_taken: ["step 1", "step 2", "step 3"]}}
"""



def _normalize_env_locations(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _write_env_files(
    repo_dir: Path, locations: list[str], env_vars: dict[str, Any]
) -> list[Path]:
    written: list[Path] = []
    if not env_vars:
        return written
    lines = [f"{key}={value}" for key, value in env_vars.items()]
    if not lines:
        return written
    text = "\n".join(lines) + "\n"
    for location in locations:
        target = Path(location)
        if not target.is_absolute():
            target = repo_dir / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def generate_env_from_database(repository_id: int, model: str, run_id: int = 14) -> str:
    load_dotenv()
    repo = db_helper.get_github_repository(repository_id=repository_id)
    if not repo:
        print(f"Repository with ID {repository_id} not found.")
        return HUMAN_PROMPT.format(docker_compose_filepaths=[])
    
    compose_paths = list(getattr(repo, "cleaned_docker_compose_filepath", []) or [])
    try:
        clone_github_repo(repo.url, compose_paths)
    except Exception as exc:
        print(f"Failed to clone repository: {exc}")

    run_result = db_helper.get_agent_run_result(repository_id, model, run_id)
    if not run_result:
        print("No agent run result data available for the requested run.")
        return HUMAN_PROMPT.format(docker_compose_filepaths=repo.cleaned_docker_compose_filepath)

    locations = _normalize_env_locations(run_result.env_location)
    env_vars = run_result.env_environmental_variables
    if not locations:
        print("No env_location data available for the requested run.")
        return HUMAN_PROMPT.format(docker_compose_filepaths=repo.cleaned_docker_compose_filepath)
    if not isinstance(env_vars, dict) or not env_vars:
        print("No env_environmental_variables data available for the requested run.")
        return HUMAN_PROMPT.format(docker_compose_filepaths=repo.cleaned_docker_compose_filepath)

    repo_dir_env = os.getenv("REPO_DIRECTORY")
    if not repo_dir_env:
        print("REPO_DIRECTORY environment variable is not set.")
        return HUMAN_PROMPT.format(docker_compose_filepaths=repo.cleaned_docker_compose_filepath)
    repo_dir = Path(repo_dir_env).resolve()

    written = _write_env_files(repo_dir, locations, env_vars)
    if not written:
        print("No .env files were written.")
    else:
        print("Created .env files:")
        for path in written:
            print(f"- {path}")
    
    return HUMAN_PROMPT.format(docker_compose_filepaths=repo.cleaned_docker_compose_filepath)


if __name__ == "__main__":
    # Mirror run_agents_ollama.py: edit IDs directly here.
    ids = [87909]
    exit_code = 0
    for repo_id in ids:
        try:
            result = generate_env_from_database(repo_id, MODEL_NAME)
            print(f"Success for repo {repo_id}")
            print(result)
        except Exception as e:
            print(f"Error for repo {repo_id}: {e}")
            exit_code = 1
            break
    raise SystemExit(exit_code)
