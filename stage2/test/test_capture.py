#!/usr/bin/env python3
"""
Simple test runner that uses the Repository implementation from
stage2/process_repository.py to attempt a pcap capture for a given
repository without performing any database updates.

Usage:
  python stage2/test/test_capture.py --repo-url <url> --repo-path owner/repo \
    --compose-files path/to/docker-compose.yml [more paths] --pcap-time 60

The script prints a JSON summary and exits with code 0 on success
or 1 on failure.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import socket
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
from dynamov2.database.db_helper import db_helper


def load_repository_class() -> type:
    repo_file = Path(__file__).resolve().parents[1] / "process_repository.py"
    if not repo_file.exists():
        raise FileNotFoundError(f"Could not find process_repository.py at {repo_file}")
    spec = importlib.util.spec_from_file_location("process_repository", str(repo_file))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)  # type: ignore
    return getattr(module, "Repository")


def run_test_capture(
    repo_url: str,
    repo_path: str,
    compose_files: list | None = None,
    pcap_time: int = 60,
    pcap_location: str | None = None,
) -> dict:
    """Programmatic API to attempt a pcap capture for a repository.

    Returns a dict with keys: `repo_path`, `pcap_location`, `tmp_pcap_path`,
    `subnets`, `bridges`, and `error_message`.

    The function does not perform any database updates.
    """
    Repository = load_repository_class()

    # Default pcap location is under this test folder: stage2/test/pcap/test.pcap
    if pcap_location is None:
        pcap_dir = Path(__file__).resolve().parent / "pcap"
        pcap_dir.mkdir(parents=True, exist_ok=True)
        pcap_location = str(pcap_dir / "test.pcap")

    compose_files_param = compose_files if compose_files else None

    repo = Repository(
        repo_url=repo_url,
        repo_path=repo_path,
        docker_compose_file_paths=compose_files_param,
        pcap_time=pcap_time,
        pcap_location=pcap_location,
    )

    result = {
        "repo_path": repo_path,
        "pcap_location": getattr(repo, "pcap_location", None),
        "tmp_pcap_path": str(getattr(repo, "tmp_pcap_path", None)) if getattr(repo, "tmp_pcap_path", None) else None,
        "subnets": getattr(repo, "subnets", None),
        "bridges": getattr(repo, "bridges", None),
        "error_message": getattr(repo, "error_message", None),
    }

    return result


__all__ = ["run_test_capture"]


def run_test_capture_by_id(repository_id: int, pcap_time: int = 60, pcap_location: str | None = None) -> dict:
    """Look up repository by `repository_id` using `db_helper.get_github_repository`
    and run a capture using the stored fields. Does not update the DB.

    Returns the same dict as `run_test_capture`.
    """
    row = db_helper.get_github_repository(repository_id=repository_id)
    if not row:
        return {"error_message": f"Repository with id {repository_id} not found"}

    # row.name should be 'owner/name' and cleaned_docker_compose_filepath is a list
    repo_url = getattr(row, "url", None)
    repo_path = getattr(row, "name", None)
    compose_files = getattr(row, "cleaned_docker_compose_filepath", None)

    # If the cleaned paths is empty list, convert to None so Repository reports 'no compose files'
    if isinstance(compose_files, (list, tuple)) and len(compose_files) == 0:
        compose_files = None

    return run_test_capture(
        repo_url=repo_url,
        repo_path=repo_path,
        compose_files=compose_files,
        pcap_time=pcap_time,
        pcap_location=pcap_location,
    )

__all__.append("run_test_capture_by_id")

if __name__ == "__main__":
    #51549, 12724 should generate sizable pcaps. 
    run_test_capture_by_id(repository_id=51549)