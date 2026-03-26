import subprocess
import sys
from typing import List
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_ROOT.parent
IMAGE_NAME = "codex-runner"
CONTAINER_NAME = "codex-runner-container"
DOCKERFILE = SRC_ROOT / "Dockerfile.codex"
DOWNLOAD_DIR = PROJECT_ROOT / "download_directory"
DOCKER_CMD = ["docker"]


def remove_image():
    subprocess.run([*DOCKER_CMD, "stop", CONTAINER_NAME])
    subprocess.run([*DOCKER_CMD, "rmi", "-f", IMAGE_NAME], check=False)


def build_image():
    cmd = [
        *DOCKER_CMD,
        "build",
        "-f",
        str(DOCKERFILE),
        "-t",
        IMAGE_NAME,
        str(PROJECT_ROOT),
    ]
    subprocess.run(cmd, check=True)


def run_container(docker_compose_filepaths: List[str], repository_id: int, run_id: int):
    """
    Run the Codex container built from Dockerfile.codex. This container always uses
    Dockerfile.codex and does not depend on docker-compose.
    """
    if not docker_compose_filepaths:
        raise ValueError("docker_compose_filepaths must contain at least one path")

    entrypoint = "codex.py"
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    compose_env = ",".join(docker_compose_filepaths)

    cmd = [
        *DOCKER_CMD,
        "run", 
        "--rm",
        "--name",
        CONTAINER_NAME,
        "-v",
        f"{DOWNLOAD_DIR}:/app/download_directory",
        "--env-file", "/home/bingcheng/dynamov2_pipelined/stages/stage_3/dynamo_src/helper/.docker_env_gpt",
        "-e",
        f"DOCKER_COMPOSE_FILES={compose_env}",
        f"REPOSITORY_ID={repository_id}",
        "-e",
        f"RUN_ID={run_id}"
    ]

    cmd += [IMAGE_NAME, "python", f"/app/{entrypoint}"]
    try:
        output = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        # Bubble up with logged output so the caller can see why the container failed.
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return {
            "working": False,
            "exc.stdout": exc.stdout,
            "exc.stderr": exc.stderr
        }
    except Exception as e:
        return {
            "working": False
        }
    # else:
    #     if output.stdout:
    #         print(output.stdout, end="")
    #     if output.stderr:
    #         print(output.stderr, file=sys.stderr, end="")

    return output.stdout

def main():
    remove_image()
    try:
        build_image()
        run_container(["docker-compose.yml"])
        # exec_codex()
    finally:
        remove_image()


if __name__ == "__main__":
    main()
