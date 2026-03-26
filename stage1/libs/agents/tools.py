import os 
from langchain_core.tools import tool 

def _safe_join(repo_dir: str, rel_path: str) -> str:
    """Join and normalize; prevent traversal outside repo_dir."""
    base = os.path.abspath(repo_dir)
    target = os.path.abspath(os.path.normpath(os.path.join(base, rel_path if rel_path else ".")))
    # Ensure target stays under repo_dir
    if os.path.commonpath([base, target]) != base:
        raise PermissionError(f"Refusing to access path outside REPO_DIRECTORY: {rel_path}")
    return target


@tool
def get_contents_of_directory(path: str) -> str:
    """
    Lists files and directories inside `path` relative to REPO_DIRECTORY.
    Does NOT change the working directory.
    """
    repo_dir = os.getenv("REPO_DIRECTORY")
    if not repo_dir:
        return "ERROR: REPO_DIRECTORY environment variable not set."

    try:
        start_path = _safe_join(repo_dir, path)
        if not os.path.exists(start_path):
            return f"ERROR: Path '{path}' does not exist."
        if not os.path.isdir(start_path):
            return f"ERROR: Path '{path}' is not a directory."

        files = []
        directories = []
        with os.scandir(start_path) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False):
                    files.append(entry.name)
                elif entry.is_dir(follow_symlinks=False):
                    directories.append(entry.name)

        files_str = "## Files:\n" + "\n".join(sorted(files))
        dirs_str  = "## Directories:\n" + "\n".join(sorted(directories))
        return files_str + "\n\n" + dirs_str

    except PermissionError as e:
        return f"ERROR: {e}"
    except NotADirectoryError:
        return f"ERROR: Path '{path}' is not a directory."
    except Exception as e:
        return f"ERROR: Unexpected error: {e}"

@tool
def read_file(path: str) -> str:
    """
    Reads a file at `path` relative to REPO_DIRECTORY.
    Does NOT change the working directory.
    """
    repo_dir = os.getenv("REPO_DIRECTORY")
    if not repo_dir:
        return "ERROR: REPO_DIRECTORY environment variable not set."

    try:
        file_path = _safe_join(repo_dir, path)
        if not os.path.exists(file_path):
            return f"ERROR: File '{path}' does not exist."
        if not os.path.isfile(file_path):
            return f"ERROR: Path '{path}' is not a file."

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    except PermissionError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: Unexpected error: {e}"