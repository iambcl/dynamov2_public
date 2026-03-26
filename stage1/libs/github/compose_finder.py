import time
import requests
import os
from typing import List, Optional
from dynamov2.logger.logger import CustomLogger

GITHUB_API = "https://api.github.com"


def _sleep_for_rate_limit(resp, logger: Optional[CustomLogger]) -> bool:
    """Sleep if rate limited. Returns True if slept and caller should retry."""
    if resp.headers.get("X-RateLimit-Remaining", "1") == "0":
        reset_time = int(resp.headers.get("X-RateLimit-Reset", 0))
        sleep_time = max(0, reset_time - int(time.time()))
        if sleep_time:
            if logger:
                logger.info(f"Rate limit hit. Sleeping for {sleep_time} seconds.")
            time.sleep(sleep_time)
        return True
    return False


def _get_json(url: str, headers: dict, logger: Optional[CustomLogger]) -> Optional[dict]:
    """GET a URL, handling rate limits and transient network errors.

    Uses a small retry/backoff loop to cope with transient connection issues
    such as ``ChunkedEncodingError`` / ``ProtocolError`` and truncated
    responses that cause JSON parsing to fail.
    """
    max_retries = 3
    backoff = 1
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.exceptions.RequestException as e:
            if logger:
                logger.warning(f"Request error for {url}: {e} (attempt {attempt}/{max_retries})")
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None

        # If rate limited, wait and retry
        if _sleep_for_rate_limit(resp, logger):
            continue

        if resp.status_code == 404:
            return None

        if not resp.ok:
            if logger:
                logger.warning(f"Error {resp.status_code} for {url}: {resp.text}")
            return None

        try:
            return resp.json()
        except ValueError as e:
            # Likely a truncated or invalid JSON body. Retry a few times.
            if logger:
                logger.warning(f"Failed to parse JSON from {url}: {e} (attempt {attempt}/{max_retries})")
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None


def get_docker_compose_filepaths(full_name: str, token: Optional[str], logger: Optional[CustomLogger] = None) -> List[str]:
    """
    Retrieve all docker-compose YAML file paths for a repository by walking its tree.

    Args:
        full_name: "owner/repo" identifier.
        token: GitHub token for authenticated requests (optional but recommended).
        logger: Optional logger for diagnostics.

    Returns:
        List of repository-relative docker compose file paths.
    """
    headers = {
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    repo = _get_json(f"{GITHUB_API}/repos/{full_name}", headers, logger)
    if not repo or "default_branch" not in repo:
        return []

    default_branch = repo["default_branch"]
    commit = _get_json(f"{GITHUB_API}/repos/{full_name}/commits/{default_branch}", headers, logger)
    if not commit or "commit" not in commit or "tree" not in commit["commit"]:
        return []

    tree_sha = commit["commit"]["tree"]["sha"]
    tree = _get_json(f"{GITHUB_API}/repos/{full_name}/git/trees/{tree_sha}?recursive=1", headers, logger)
    if not tree or "tree" not in tree:
        return []

    paths = {
        item["path"]
        for item in tree.get("tree", [])
        if item.get("type") == "blob"
    }
    def _clean_compose_paths(candidate_paths: List[str]) -> List[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for path in candidate_paths:
            if not path:
                continue
            normalized = path.replace("\\", "/")
            if normalized.startswith("./"):
                normalized = normalized[2:]
            if normalized.startswith(".github/") or normalized.startswith(".travis/"):
                continue
            filename = os.path.basename(normalized).lower()
            if "docker-compose" not in filename:
                continue
            if not filename.endswith((".yaml", ".yml")):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
        return sorted(cleaned)

    compose_paths = [
        path
        for path in paths
        if "docker-compose" in path.lower() and path.endswith((".yaml", ".yml"))
    ]

    return _clean_compose_paths(compose_paths)
