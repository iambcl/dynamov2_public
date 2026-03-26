import os
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from dynamov2.database.db_helper import db_helper
from dynamov2.git_utils.utils import clone_github_repo

def _sanitize_repo_dirname(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return "repo"
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned)
    cleaned = cleaned.strip("._-")
    return cleaned or "repo"


def copy_archived_repository_to_download_repository(
    repository_name: str,
    *,
    archived_repositories_root: str | Path | None = None,
    download_repository_dir: str | Path | None = None,
) -> Path:
    """Copy an archived repository into the download repository sandbox.

    - Source (archive) defaults to: <this file>/repository/<sanitized repository_name>
    - Destination defaults to: the directory in $REPO_DIRECTORY
    """

    repositories_root = Path(archived_repositories_root) if archived_repositories_root else (Path(__file__).resolve().parent / "repository")

    destination = (
        Path(download_repository_dir)
        if download_repository_dir is not None
        else Path(os.getenv("REPO_DIRECTORY", "")).expanduser()
    )
    if not str(destination):
        raise ValueError("REPO_DIRECTORY is not set and download_repository_dir was not provided")

    source = repositories_root / _sanitize_repo_dirname(repository_name)
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"Archived repository directory not found: {source}")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)

    shutil.copytree(source, destination, symlinks=True)
    return destination

def prepare_directory(id: int):
    row = db_helper.get_traffic_parameters_by_id(id)
    github_row = db_helper.get_github_repository(row.id)
    if row and row.application_traffic_present == 'true':
        clone_github_repo(github_row.url, github_row.cleaned_docker_compose_filepath)
    else:
        copy_archived_repository_to_download_repository(github_row.name, archived_repositories_root=os.getenv("REPOSITORY"))

if __name__ == "__main__":
    prepare_directory(76568) #download: 563. Copy: 9155