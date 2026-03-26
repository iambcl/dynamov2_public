import os
import subprocess
from urllib.parse import urlparse
from .clean_compose_file import absolutize_compose_paths

def _to_ssh_url(repo_url: str) -> str:
    """Convert an HTTPS GitHub URL to SSH form, handling www. and trailing .git."""
    if repo_url.startswith("git@"):
        return repo_url
    parsed = urlparse(repo_url)
    host = (parsed.hostname or "").removeprefix("www.")
    path = parsed.path.strip("/")
    if not host or not path:
        raise ValueError(f"Invalid GitHub URL: {repo_url}")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"git@{host}:{path}.git"

def clone_github_repo(repo_url: str, compose_paths: list[str] ) -> None:
    """Clone a GitHub repo into the directory specified by REPO_DIRECTORY."""
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        raise Exception("Error: REPO_DIRECTORY environment variable is not set.")
    if not os.path.isdir(repo_directory):
        print(f"Repository directory '{repo_directory}' does not exist. Not removing anything.")
    else:
        try:
            print(f"Deleting repository directory: {repo_directory}")
            subprocess.run(["sudo", "rm", "-rf", repo_directory])
            print("Deletion successful.")
        except Exception:
            pass
    print(f"Cloning {repo_url} into {repo_directory}...")
    ssh_url = _to_ssh_url(repo_url)
    subprocess.run(
        ["git", "clone", ssh_url, repo_directory],
        check=True,
        text=True,
    )
    current_working_directory = os.getcwd()
    try:
        os.chdir(os.getenv("REPO_DIRECTORY"))
        for path in compose_paths:
            absolutize_compose_paths(path)
    finally:
        os.chdir(current_working_directory)
