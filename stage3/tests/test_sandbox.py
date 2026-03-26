import os
import asyncio
import pytest

# Force REPO_DIRECTORY to the stage3 download_directory sandbox
REPO = os.path.abspath("/home/bingcheng/dynamov2/stage3/download_directory")
os.environ["REPO_DIRECTORY"] = REPO

import sys
import os as _os
# Ensure stage3 directory is on sys.path so `mcp_server` imports reliably
_root = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))
if _root not in sys.path:
    sys.path.insert(0, _root)

import mcp_server


@pytest.fixture(autouse=True)
def ensure_repo_files():
    """Ensure baseline files exist inside the enforced REPO_DIRECTORY before each test."""
    repo = REPO
    os.makedirs(repo, exist_ok=True)
    # baseline .env for copy tests
    env_path = os.path.join(repo, ".env")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("TEST=1\n")
    yield


def test_copy_env_file_outside_rejected():
    result = mcp_server.copy_env_file("/tmp/test_outside.env")
    assert isinstance(result, dict)
    assert result.get("state") == "error"


def test_copy_env_file_inside_succeeds():
    result = mcp_server.copy_env_file("subdir_out/")
    assert result.get("state") == "ok"


def test_write_env_file_outside_rejected():
    res = asyncio.run(mcp_server.write_env_file("NEW=2\n", "/tmp/evil.env"))
    assert isinstance(res, str)
    assert "within REPO_DIRECTORY" in res or "must be within REPO_DIRECTORY" in res


def test_write_env_file_inside_updates():
    res = asyncio.run(mcp_server.write_env_file("NEW=2\n", "subdir_out/.env"))
    assert isinstance(res, str)
    assert "env file has been" in res


def test_write_docker_compose_outside_rejected():
    res = asyncio.run(mcp_server.write_docker_compose_file("version: '3'\n", "/tmp/compose.yml"))
    assert "within REPO_DIRECTORY" in res or "must be within REPO_DIRECTORY" in res


def test_write_docker_compose_inside_succeeds():
    res = asyncio.run(mcp_server.write_docker_compose_file("version: '3'\n", "docker-compose.test.yml"))
    assert "Docker compose file has been" in res


def test_write_dockerfile_outside_rejected():
    res = asyncio.run(mcp_server.write_dockerfile("FROM scratch\n", "/tmp/Dockerfile"))
    assert "within REPO_DIRECTORY" in res or "must be within REPO_DIRECTORY" in res


def test_write_dockerfile_inside_succeeds():
    res = asyncio.run(mcp_server.write_dockerfile("FROM scratch\n", "Dockerfile.test"))
    assert "Dockerfile has been" in res


def test_read_file_outside_rejected():
    res = mcp_server.read_file("/etc/passwd")
    assert res.get("state") == "error"


def test_read_file_inside_succeeds():
    # create test file inside repo
    test_path = os.path.join(REPO, "test_read.txt")
    with open(test_path, "w", encoding="utf-8") as f:
        f.write("hello")
    res = mcp_server.read_file("test_read.txt")
    assert res.get("state") == "ok"
    assert res.get("content") == "hello"


def test_docker_check_outside_rejected():
    res = asyncio.run(mcp_server.docker_container_check_v1(["/tmp/docker-compose.yml"]))
    assert isinstance(res, dict)
    assert res.get("state") == "error"
