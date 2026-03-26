from dotenv import load_dotenv
load_dotenv()
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.session import ServerSession
from dynamov2.database.db_helper import db_helper
# from langchain_ollama import OllamaEmbeddings
import langsmith as ls
import os, subprocess, re, json, asyncio, time, yaml, shutil
from typing import List, Dict, Tuple, Optional, Any
# from langchain_experimental.text_splitter import SemanticChunker
from dynamo_src.helper.clean_compose_file import absolutize_compose_paths
from langchain_openai import ChatOpenAI
import langextract as lx
import textwrap
import uvicorn

mcp = FastMCP("demo", json_response=True, host="0.0.0.0")

def _sanitize_path(raw_path: str) -> str:
    """
    Normalize user-supplied paths so they work with REPO_DIRECTORY.
    Mirrors docker_container_check_v1 handling (download_directory stripping and
    acceptance of absolute paths).
    """
    normalized = raw_path.replace("\\", "/")
    if os.path.isabs(normalized):
        return normalized
    if normalized.startswith("download_directory/"):
        normalized = normalized[len("download_directory/"):]
    elif "/download_directory/" in normalized:
        _, _, suffix = normalized.partition("/download_directory/")
        normalized = suffix or normalized
    return normalized.lstrip("/")

'''
Exposes tools for agent to get application information from database
'''
@mcp.tool()
def applications_present() -> Dict[str, Any]:
    """
    Returns a list of all applications present in the database with their details.
    """
    print("Tool called: applications_present")
    return db_helper.get_repository_applications()
'''
Exposes tools for env agent:
'''

@mcp.tool()
def return_secrets() -> Dict:
    """
    Returns the keys and values currently available to be added to the .env file
    """
    print("Tool called: return_secrets")
    app_db_vars = {key: os.getenv(key) for key in os.environ if key.startswith("APP")}
    return {
        "openai_api_key": os.getenv("OPENAI_API_KEY"),
        "base_url": os.getenv("BASE_URL"),
        "gemini_api_key": os.getenv("GOOGLE_API_KEY"),
        "github_token": os.getenv("GITHUB_TOKEN"),
        **app_db_vars,
    }

@mcp.tool()
async def read_environment_variables_v2() -> Dict:
    """
    Scan README, env-named, and compose-named files for mentioned environment variables using LangExtract.
    Returns a deduplicated mapping of env vars to their values (or null if unspecified) plus any discovered
    env file locations (defaults to ".env" when none are found).
    """
    print("Tool called: read_environment_variables_v2")

    def _collect_target_files(
        repo_directory: str, readme_candidates: List[str]
    ) -> Tuple[List[str], List[str]]:
        targets: List[str] = []
        env_files: List[str] = []
        readme_lower = {name.lower() for name in readme_candidates}
        for root, _, files in os.walk(repo_directory):
            for fname in files:
                lower = fname.lower()
                if lower in readme_lower or "env" in lower or ("compose" in lower and "bak" not in lower):
                    full_path = os.path.join(root, fname)
                    targets.append(full_path)
                    if lower not in readme_lower and "env" in lower:
                        env_files.append(full_path)
        return sorted(set(targets)), sorted(set(env_files))

    def _normalize_var(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", name).strip()
        return cleaned.upper()

    def _normalize_env_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if text.lower() in {"", "null", "none"}:
            return None
        return text

    def _normalize_location(location: str, repo_directory: str) -> str:
        cleaned = str(location).strip().strip('"').strip("'").strip("`")
        if not cleaned:
            return ""
        cleaned = cleaned.replace("\\", "/")
        cleaned = _sanitize_path(cleaned)
        if os.path.isabs(cleaned):
            try:
                cleaned = os.path.relpath(cleaned, repo_directory)
            except Exception:
                pass
        cleaned = cleaned.lstrip("./")
        return cleaned

    def _choose_env_file_location(candidates: List[str]) -> str:
        normalized: List[str] = []
        seen = set()
        for candidate in candidates:
            clean = candidate.strip()
            if not clean:
                continue
            clean = clean.replace("\\", "/").lstrip("./")
            if clean not in seen:
                seen.add(clean)
                normalized.append(clean)

        if not normalized:
            return ".env"

        def _score(path: str) -> Tuple[int, int]:
            score = 0
            lower = path.lower()
            if "example" in lower or "sample" in lower:
                score -= 1
            if path.startswith(".env") or "/.env" in path:
                score += 3
            if "/" not in path:
                score += 1
            return score, -len(path)

        normalized.sort(key=_score, reverse=True)
        return normalized[0]

    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return {
            "state": "error",
            "message": "REPO_DIRECTORY environment variable is not set.",
            "env_vars": {},
            "env_locations": [".env"],
        }

    readme_candidates = ["README.en.md", "README.md"]
    targets, env_files = _collect_target_files(repo_directory, readme_candidates)
    if not targets:
        return {
            "env_vars": {},
            "env_locations": [".env"],
        }

    prompt = textwrap.dedent(
        """
        Extract environment variable names and values mentioned in the text.
        If a variable has a configurable or example value, capture it as an attribute named "value".
        If a value is not provided, use an empty string for "value".
        Also extract any environment file locations (e.g., .env, .env.local, config/.env).
        Use extraction_class "env_var" for variables and "env_file" for file locations.
        Use the exact text spans for extraction_text.
        """
    ).strip()

    examples = [
        lx.data.ExampleData(
            text=(
                "Ensure `project_root/out/.env` is synced w/ project.\n"
                "Note: `project_root/.env` is ignored when building the project but can be used for encoding. "
                "A new `{project_root}/out/.env` will not be created, so keep this in mind."
            ),
            extractions=[
                lx.data.Extraction(
                    extraction_class="env_file",
                    extraction_text="`project_root/.env`",
                    attributes={"path": ".env"},
                ),
                lx.data.Extraction(
                    extraction_class="env_file",
                    extraction_text="`project_root/out/.env`",
                    attributes={"path": "out/.env"},
                ),
            ],
        ),
        lx.data.ExampleData(
            text=(
                "# 2. 创建并配置 .env 文件\n"
                "# 进入后端目录，复制示例文件\n"
                "backend/.env\n"
                "cd backend\n"
                "cp .env.example .env"
            ),
            extractions=[
                lx.data.Extraction(
                    extraction_class="env_file",
                    extraction_text=r"backend/.env",
                    attributes={"path": r"backend/.env"},
                ),
            ],
        ),
        lx.data.ExampleData(
            text=(
                "  mariadb:\n"
                "    container_name: mariadb\n"
                "    build:\n"
                "      context: /home/bingcheng/mcp-server/download_directory/srcs/requirements/mariadb\n"
                "      args:\n"
                "      - DB_ROOT_PASSWORD\n"
                "      - DB_NAME\n"
                "      - DB_USER\n"
                "      - DB_PASSWORD\n"
                "    env_file: /home/bingcheng/mcp-server/download_directory/srcs/.env\n"
                "    image: mariadb\n"
                "    volumes:\n"
                "    - mariadb:/var/lib/mysql\n"
                "    networks:\n"
                "    - inception\n"
                "    restart: always"
            ),
            extractions=[
                lx.data.Extraction(
                    extraction_class="env_file",
                    extraction_text="/home/bingcheng/mcp-server/download_directory/srcs/.env",
                    attributes={"path": "srcs/.env"},
                ),
            ],
        ),
        lx.data.ExampleData(
            text=(
                "Set these in your .env:\n"
                "API_BASE_URL=https://api.example.com\n"
                "DEBUG=\n"
                "OPENAI_API_KEY=sk-abc123\n"
            ),
            extractions=[
                lx.data.Extraction(
                    extraction_class="env_var",
                    extraction_text="API_BASE_URL",
                    attributes={"value": "https://api.example.com"},
                ),
                lx.data.Extraction(
                    extraction_class="env_var",
                    extraction_text="DEBUG",
                    attributes={"value": ""},
                ),
                lx.data.Extraction(
                    extraction_class="env_var",
                    extraction_text="OPENAI_API_KEY",
                    attributes={"value": "sk-abc123"},
                ),
            ],
        ),
        lx.data.ExampleData(
            text=(
                "Required environment variables:\n"
                "- DB_HOST (example: localhost)\n"
                "- DB_PORT=5432\n"
            ),
            extractions=[
                lx.data.Extraction(
                    extraction_class="env_var",
                    extraction_text="DB_HOST",
                    attributes={"value": "localhost"},
                ),
                lx.data.Extraction(
                    extraction_class="env_var",
                    extraction_text="DB_PORT",
                    attributes={"value": "5432"},
                ),
            ],
        ),
    ]

    collected: Dict[str, Optional[str]] = {}
    env_locations: List[str] = [
        _normalize_location(os.path.relpath(path, repo_directory), repo_directory) for path in env_files
    ]
    env_locations = [loc for loc in env_locations if loc]
    env_seen: set[str] = set(env_locations)

    try:
        max_chunks = int(os.getenv("ENV_CHUNK_LIMIT", "20"))
    except Exception:
        max_chunks = 20

    try:
        max_tokens = int(os.getenv("ENV_TOKEN_LIMIT", "40000"))
    except Exception:
        max_tokens = 200000

    def _estimate_tokens(text: str) -> int:
        # Rough heuristic: ~4 characters per token for English-ish text.
        return max(1, len(text) // 4)

    documents: List[lx.data.Document] = []
    estimated_tokens = 0
    for path in targets:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as f:
                text = f.read()
            print(f"read_environment_variables_v2 decoded {path} as latin-1")
        except Exception as exc:
            print(f"Skipping {path}: {exc}")
            continue

        if not text.strip():
            continue

        label = os.path.relpath(path, repo_directory)
        doc_text = f"File: {label}\n{text}"
        estimated_tokens += _estimate_tokens(doc_text)
        if estimated_tokens > max_tokens:
            print(
                "read_environment_variables_v2: token limit exceeded "
                f"({estimated_tokens} > {max_tokens})"
            )
            fallback_locations = env_locations or [_choose_env_file_location(env_locations)]
            return {
                "state": "error",
                "message": "Env information not available: token limit exceeded",
                "env_vars": {},
                "env_locations": fallback_locations,
            }
        documents.append(lx.data.Document(text=doc_text))

    print("Current number of documents: ", len(documents))
    if len(documents) > max_chunks:
        print(f"read_environment_variables_v2: document limit exceeded ({len(documents)} > {max_chunks})")
        fallback_locations = env_locations or [_choose_env_file_location(env_locations)]
        return {
            "state": "error",
            "message": "Env information not available: too many documents to process",
            "env_vars": {},
            "env_locations": fallback_locations,
        }

    model_id = os.getenv("LANGEXTRACT_MODEL")
    model_url = os.getenv("BASE_URL")

    try:
        results = lx.extract(
            text_or_documents=documents,
            prompt_description=prompt,
            examples=examples,
            model_id=model_id,
            model_url=model_url,
            api_key=os.getenv("LANGEXTRACT_API_KEY"),
            max_char_buffer=800,
            show_progress=False,
        )
    except Exception as exc:
        return {
            "state": "error",
            "message": f"LangExtract failed: {exc}",
            "env_vars": {},
            "env_locations": env_locations or [".env"],
        }

    for doc in results or []:
        for extraction in getattr(doc, "extractions", []) or []:
            extraction_class = (extraction.extraction_class or "").lower()
            if "env_var" in extraction_class:
                normalized = _normalize_var(extraction.extraction_text or "")
                if not normalized or normalized in collected:
                    continue
                attrs = extraction.attributes or {}
                raw_value = attrs.get("value") or attrs.get("default") or attrs.get("example")
                collected[normalized] = _normalize_env_value(raw_value)
            elif "env_file" in extraction_class:
                attrs = extraction.attributes or {}
                raw_loc = attrs.get("path") or attrs.get("location") or ""
                if not raw_loc:
                    raw_loc = extraction.extraction_text or ""
                normalized_loc = _normalize_location(raw_loc, repo_directory)
                if normalized_loc and normalized_loc not in env_seen:
                    env_seen.add(normalized_loc)
                    env_locations.append(normalized_loc)

    final_env_locations = env_locations or [_choose_env_file_location(env_locations)]

    if not collected:
        return {
            "env_vars": {},
            "env_locations": final_env_locations,
        }

    return {
        "env_vars": collected,
        "env_locations": final_env_locations,
    }

'''
Exposes tools for swe agent:
'''
@mcp.tool()
def read_file(path: str) -> Dict[str, str]:
    """
    Read a file relative to REPO_DIRECTORY (or via absolute path). Handles
    download_directory prefixes the same way docker_container_check_v1 does.
    """
    print("Tool called: read_file")
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return {"state": "error", "message": "REPO_DIRECTORY is not set"}

    sanitized = _sanitize_path(path)
    if sanitized == "":
        return {"state": "error", "message": "Path is empty after sanitization"}

    target_path = sanitized if os.path.isabs(sanitized) else os.path.join(repo_directory, sanitized)
    target_path = os.path.abspath(target_path)

    # Ensure target is inside REPO_DIRECTORY
    repo_root = os.path.abspath(repo_directory)
    repo_prefix = repo_root + os.sep
    if not (target_path == repo_root or target_path.startswith(repo_prefix)):
        return {"state": "error", "message": "Target path must be within REPO_DIRECTORY"}

    # Restrict readable files to Dockerfiles, docker compose YAMLs, .env files, requirements.txt, and pyproject.toml
    basename = os.path.basename(target_path).lower()
    allowed = False
    # Dockerfile or Dockerfile.* (e.g., Dockerfile.dev)
    if basename == "dockerfile" or basename.startswith("dockerfile."):
        allowed = True
    # .env files (e.g., .env, .env.local)
    if basename.startswith(".env"):
        allowed = True
    # docker compose YAML files (contain 'compose' and end with .yml/.yaml)
    if ("compose" in basename) and basename.endswith((".yml", ".yaml")):
        allowed = True
    # Python dependency manifest
    if basename == "requirements.txt":
        allowed = True
    # Python project/dependency manifest
    if basename == "pyproject.toml":
        allowed = True
    if not allowed:
        return {"state": "error", "message": "read_file is restricted to Dockerfiles, docker compose YAMLs, .env files, requirements.txt, and pyproject.toml only"}

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            contents = f.read()
        return {"state": "ok", "path": target_path, "content": contents}
    except UnicodeDecodeError:
        with open(target_path, "r", encoding="latin-1") as f:
            contents = f.read()
        print(f"read_file decoded {target_path} as latin-1")
        return {"state": "ok", "path": target_path, "content": contents}
    except FileNotFoundError:
        return {"state": "error", "message": f"File not found: {target_path}"}
    except IsADirectoryError:
        return {"state": "error", "message": f"Target is a directory: {target_path}"}
    except Exception as exc:
        return {"state": "error", "message": f"Failed to read {target_path}: {exc}"}

@mcp.tool()
def copy_env_file(destination_path: str) -> Dict[str, str]:
    """
    Copy the root .env to the provided destination path.
    Use this tool to copy the .env file to a target location.
    Destination is treated as relative to download_repository unless an absolute path is supplied.
    """
    print("Tool called: copy_env_file")
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return {"state": "error", "message": "REPO_DIRECTORY is not set"}

    source_path = os.path.join(repo_directory, ".env")
    if not os.path.isfile(source_path):
        return {"state": "error", "message": f".env does not exist at {source_path}"}

    sanitized = _sanitize_path(destination_path)
    if sanitized == "":
        return {"state": "error", "message": "Destination path is empty after sanitization"}

    target_path = sanitized if os.path.isabs(sanitized) else os.path.join(repo_directory, sanitized)
    target_path = os.path.abspath(target_path)

    if destination_path.endswith("/") or os.path.isdir(target_path):
        target_path = os.path.join(target_path, ".env")
        target_path = os.path.abspath(target_path)

    # Ensure target is inside REPO_DIRECTORY
    repo_root = os.path.abspath(repo_directory)
    repo_prefix = repo_root + os.sep
    if not (target_path == repo_root or target_path.startswith(repo_prefix)):
        return {"state": "error", "message": "Destination path must be within REPO_DIRECTORY"}

    if os.path.abspath(source_path) == target_path:
        return {"state": "error", "message": "Destination path points to the source .env file"}

    target_dir = os.path.dirname(target_path)
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    try:
        shutil.copy2(source_path, target_path)
        return {"state": "ok", "message": f"Copied .env to {target_path}"}
    except Exception as exc:
        return {"state": "error", "message": f"Failed to copy .env: {exc}"}

@mcp.tool()
async def write_env_file(text: str, path_to_write: str | None = None) -> str:
    '''
    Writes the provided text as a .env file in the root directory of the downloaded Github repository by default.
    Provide the argument for path_to_write to change what filename to write and where to write it relative to the root directory.
    For example, writing to download_directory/.env will require path_to_write to be .env
    '''
    print("Tool called: write_env_file")
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return "REPO_DIRECTORY is not set"

    repo_root = os.path.abspath(repo_directory)
    repo_prefix = repo_root + os.sep

    if path_to_write is None:
        candidate_path = os.path.join(repo_root, ".env")
    else:
        sanitized = _sanitize_path(path_to_write)
        if sanitized == "":
            return "path_to_write is empty after sanitization"
        candidate_path = sanitized if os.path.isabs(sanitized) else os.path.join(repo_root, sanitized)

    file_path = os.path.abspath(candidate_path)

    # Ensure the target path remains within REPO_DIRECTORY
    if not (file_path == repo_root or file_path.startswith(repo_prefix)):
        return "Target path must be within REPO_DIRECTORY"

    file_existed = os.path.exists(file_path)
    dir_name = os.path.dirname(file_path)
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name, exist_ok=True)

    def _parse_assignment(line: str) -> tuple[str | None, str | None]:
        if not line.strip():
            return None, None
        if line.lstrip().startswith("#"):
            return None, None
        if "=" not in line:
            return None, None
        key_part, value_part = line.split("=", 1)
        key = key_part.strip()
        if not key:
            return None, None
        return key, value_part

    existing_content = ""
    existing_lines: List[str] = []
    if file_existed:
        with open(file_path, "r", encoding="utf-8") as f:
            existing_content = f.read()
        existing_lines = existing_content.splitlines()

    key_to_index: Dict[str, int] = {}
    for idx, line in enumerate(existing_lines):
        key, _ = _parse_assignment(line)
        if key:
            key_to_index[key] = idx

    for raw_line in text.splitlines():
        key, value = _parse_assignment(raw_line)
        if key is None:
            # Preserve comments or empty lines from the provided text by appending them.
            existing_lines.append(raw_line)
            continue

        line_to_write = f"{key}={value}"
        if key in key_to_index:
            target_index = key_to_index[key]
            existing_lines[target_index] = line_to_write
        else:
            existing_lines.append(line_to_write)
            target_index = len(existing_lines) - 1
        key_to_index[key] = target_index

    final_content = "\n".join(existing_lines)
    if existing_lines:
        final_content += "\n"

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        if file_existed:
            return f"env file has been updated at {file_path}"
        return f"env file has been created at {file_path}"
    except Exception as e:
        return f"Error has occurred. {e}"

@mcp.tool()
async def write_docker_compose_file(text: str, path_to_write: str | None = None) -> str:
    '''
    Writes sanitized docker compose YAML within REPO_DIRECTORY.
    Defaults to docker-compose.yml in the repository root. path_to_write is treated as
    relative to REPO_DIRECTORY (download_directory prefixes are stripped) and must
    remain inside that directory.
    '''
    print("Tool called: write_docker_compose_file")
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return "REPO_DIRECTORY is not set"

    repo_root = os.path.abspath(repo_directory)
    repo_prefix = repo_root + os.sep

    def _sanitize_compose_text(raw_text: str) -> str:
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        sanitized_lines = []
        for line in normalized.split("\n"):
            cleaned = line.replace("\ufeff", "")
            cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch == "\t")
            sanitized_lines.append(cleaned)
        final = "\n".join(sanitized_lines)
        if sanitized_lines:
            final += "\n"
        return final

    if path_to_write is None:
        file_path = os.path.join(repo_root, "docker-compose.yml")
    else:
        sanitized = _sanitize_path(path_to_write)
        if sanitized == "":
            return "path_to_write is empty after sanitization"
        candidate_path = sanitized if os.path.isabs(sanitized) else os.path.join(repo_root, sanitized)
        file_path = os.path.abspath(candidate_path)

    if not (file_path == repo_root or file_path.startswith(repo_prefix)):
        return "Target path must be within REPO_DIRECTORY"

    filename = os.path.basename(file_path).lower()
    if not (filename.endswith((".yml", ".yaml")) and "compose" in filename):
        return "Target file must be a docker compose YAML (include 'compose' and use .yml/.yaml)"

    target_dir = os.path.dirname(file_path)
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    file_existed = os.path.exists(file_path)
    sanitized_text = _sanitize_compose_text(text)

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(sanitized_text)
        if file_existed:
            return f"Docker compose file has been updated at {file_path}"
        return f"Docker compose file has been created at {file_path}"
    except Exception as e:
        return f"Error has occurred. {e}"

@mcp.tool()
async def write_dockerfile(text: str, path: str | None = None) -> str:
    """
    Writes a Dockerfile within REPO_DIRECTORY.
    Defaults to Dockerfile in the repository root. path is treated as
    relative to REPO_DIRECTORY (download_directory prefixes are stripped) and must
    remain inside that directory.
    """
    print("Tool called: write_dockerfile")
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return "REPO_DIRECTORY is not set"

    repo_root = os.path.abspath(repo_directory)
    repo_prefix = repo_root + os.sep

    def _sanitize_dockerfile_text(raw_text: str) -> str:
        normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        sanitized_lines = []
        for line in normalized.split("\n"):
            cleaned = line.replace("\ufeff", "")
            cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch == "\t")
            sanitized_lines.append(cleaned)
        final = "\n".join(sanitized_lines)
        if sanitized_lines:
            final += "\n"
        return final

    if path is None:
        file_path = os.path.join(repo_root, "Dockerfile")
    else:
        sanitized = _sanitize_path(path)
        if sanitized == "":
            return "path is empty after sanitization"
        candidate_path = sanitized if os.path.isabs(sanitized) else os.path.join(repo_root, sanitized)
        file_path = os.path.abspath(candidate_path)

    if not (file_path == repo_root or file_path.startswith(repo_prefix)):
        return "Target path must be within REPO_DIRECTORY"

    filename = os.path.basename(file_path)
    if not (filename == "Dockerfile" or filename.startswith("Dockerfile.")):
        return "Target file must be a Dockerfile (Dockerfile or Dockerfile.*)"

    if os.path.isdir(file_path):
        return "Target path is a directory"

    target_dir = os.path.dirname(file_path)
    if target_dir and not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    file_existed = os.path.exists(file_path)
    sanitized_text = _sanitize_dockerfile_text(text)

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(sanitized_text)
        if file_existed:
            return f"Dockerfile has been updated at {file_path}"
        return f"Dockerfile has been created at {file_path}"
    except Exception as e:
        return f"Error has occurred. {e}"

@mcp.tool()
async def docker_container_check_v1(docker_compose_filepaths: List[str]) -> dict:
    """
    Starts docker containers via docker compose and returns status/logs per service.
    For each service found, returns its container state and exit code (when available) alongside recent logs.
    """
    print("Tool called: docker_container_check_v1")
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return {"state": "error", "message": "REPO_DIRECTORY is not set"}

    cleaned = []
    repo_root = os.path.abspath(repo_directory)
    repo_prefix = repo_root + os.sep
    for path in docker_compose_filepaths:
        normalized = path.replace("\\", "/")
        if normalized.startswith("download_directory/"):
            normalized = normalized[len("download_directory/"):]
        elif "/download_directory/" in normalized:
            _, _, suffix = normalized.partition("/download_directory/")
            normalized = suffix or normalized

        candidate = normalized if os.path.isabs(normalized) else os.path.join(repo_directory, normalized)
        full_path = os.path.abspath(candidate)

        # Ensure compose file is inside REPO_DIRECTORY
        if not (full_path == repo_root or full_path.startswith(repo_prefix)):
            return {"state": "error", "message": f"Compose file {path} must be within REPO_DIRECTORY"}

        cleaned.append(full_path)
    docker_compose_filepaths = cleaned
    print("docker_container_check_v1 received compose files: ", docker_compose_filepaths)
    def _parse_ps_output(raw: str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

        entries = []
        for line in [l for l in raw.splitlines() if l.strip()]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                return None
        return entries if entries else None

    def _start_docker_container_with_service_details(docker_compose_filepaths: List[str]) -> dict:
        project_root = os.getenv("REPO_DIRECTORY")
        if not project_root:
            return {"state": "error", "message": "REPO_DIRECTORY is not set"}

        def _compose_base_cmd():
            base = ["docker", "compose", "--project-name", "dynamov2_check"]
            for path in docker_compose_filepaths:
                base.extend(["-f", path])
            return base
        
        def _compose_down():
            subprocess.run(
                [
                    *_compose_base_cmd(),
                    "down",
                    "--rmi",
                    "all",
                    "--volumes",
                    "--remove-orphans",
                ],
                cwd=project_root,
            )

        def _get_service_logs(project_root: str, service_name: str) -> str:
            logs_proc = subprocess.run(
                _compose_base_cmd() + ["logs", "--no-color", "--tail", "10", service_name],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return logs_proc.stdout

        compose_up_output = ""
        try:
            compose_up_proc = subprocess.run(
                _compose_base_cmd() + ["up", "-d"],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=True,
            )
            compose_up_output = compose_up_proc.stdout or ""
            time.sleep(30)
        except subprocess.CalledProcessError as e:
            output_lines = e.stdout.splitlines() if e.stdout else []
            tail_output = "\n".join(output_lines[-10:]) if output_lines else ""
            '''
            TODO:
            Add summarization here to ensure that the context of the initial model is not dropped if changing model settings doesn't work
            '''
            _compose_down()

            return {
                "state": "compose_failed",
                "exit_code": e.returncode,
                "output": tail_output,
            }

        service_details = {}
        try:
            ps_out = subprocess.run(
                _compose_base_cmd() + ["ps", "--format", "json"],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=True,
            ).stdout

            services = _parse_ps_output(ps_out)
            if services is None:
                error_response = {
                    "state": "error",
                    "message": "Unable to parse docker compose ps output.",
                    "compose_up_output": compose_up_output,
                }
                if ps_out:
                    error_response["raw_ps_output"] = ps_out
                return error_response

            for entry in services:
                '''
                TODO:
                Add summarization here to ensure that the context of the initial model is not dropped if changing model settings doesn't work
                '''
                service_name = entry.get("Service") or entry.get("Name") or "unknown"
                service_details[service_name] = {
                    "state": entry.get("State"),
                    "exit_code": entry.get("ExitCode"),
                    "logs": _get_service_logs(project_root, service_name),
                }

            return {"state": "ok", "services": service_details}
        finally: _compose_down()
    try:
        timeout_seconds = 290
        return await asyncio.wait_for(
            asyncio.to_thread(
                _start_docker_container_with_service_details, docker_compose_filepaths
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return {
            "state": "timeout",
            "message": "docker_container_check_v1 exceeded 5 minute timeout",
            "input_values": docker_compose_filepaths,
        }
    except Exception as e:
        return {
            "state": "error",
            "message": f"docker_container_check_v1 failed: {type(e).__name__}: {e}",
            "input_values": docker_compose_filepaths,
        }

# Run with streamable HTTP transport
if __name__ == "__main__":
    uvicorn.run(
        mcp.streamable_http_app,
        host="0.0.0.0",
        port=8000,
        timeout_keep_alive=600,
        factory=True
        )
