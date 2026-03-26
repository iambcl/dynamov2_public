import os, subprocess, re, json, time, shutil, threading
from typing import List, Dict, Tuple, Optional, Any
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
import langsmith as ls


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

@tool
def return_secrets() -> Dict:
    """
    Returns the keys and values currently available to be added to the .env file
    """
    app_db_vars = {key: os.getenv(key) for key in os.environ if key.startswith("APP")}
    return {
        "openai_api_key": os.getenv("OPENAI_API_KEY"),
        "ollama_base_url": os.getenv("OLLAMA_BASE_URL"),
        "gemini_api_key": os.getenv("GOOGLE_API_KEY"),
        "github_token": os.getenv("GITHUB_TOKEN"),
        **app_db_vars,
    }

@tool
def read_file(path: str) -> Dict[str, str]:
    """
    Read a file relative to REPO_DIRECTORY (or via absolute path). Handles
    download_directory prefixes the same way docker_container_check_v1 does.
    """
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return {"state": "error", "message": "REPO_DIRECTORY is not set"}

    sanitized = _sanitize_path(path)
    if sanitized == "":
        return {"state": "error", "message": "Path is empty after sanitization"}

    target_path = sanitized if os.path.isabs(sanitized) else os.path.join(repo_directory, sanitized)
    target_path = os.path.abspath(target_path)

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

@tool
def read_environment_variables() -> Dict:
    """
    Scan README, env-named, and compose-named files for mentioned environment variables by asking an LLM
    about each chunk. Returns a deduplicated mapping of env vars to their values (or null if unspecified)
    plus any discovered env file locations (defaults to ".env" when none are found).
    """
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

    def _chunk_text(label: str, text: str, chunk_size: int = 800, overlap: int = 200) -> List[str]:
        step = max(chunk_size - overlap, 1)
        chunks = []
        for i in range(0, len(text), step):
            snippet = text[i : i + chunk_size]
            chunks.append(f"File: {label}\n{snippet}")
        return chunks

    def _normalize_var(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", name).strip()
        return cleaned.upper()

    def _parse_env_list(raw: str) -> List[str]:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
            if isinstance(parsed, dict):
                values = []
                for value in parsed.values():
                    if isinstance(value, list):
                        values.extend([str(item) for item in value])
                    else:
                        values.append(str(value))
                return values
        except Exception:
            pass
        matches = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", raw)
        return matches

    def _normalize_env_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if text.lower() in {"", "null", "none"}:
            return None
        return text

    def _normalize_location(location: str, repo_directory: str) -> str:
        cleaned = str(location).strip().strip('"').strip("'")
        if not cleaned:
            return ""
        cleaned = cleaned.replace("\\", "/")
        if os.path.isabs(cleaned):
            try:
                cleaned = os.path.relpath(cleaned, repo_directory)
            except Exception:
                pass
        cleaned = cleaned.lstrip("./")
        return cleaned

    def _parse_env_response(raw: str) -> Dict[str, Any]:
        def _extract_vars(vars_part: Any) -> Dict[str, Optional[str]]:
            vars_dict: Dict[str, Optional[str]] = {}
            if isinstance(vars_part, dict):
                for key, value in vars_part.items():
                    key_str = str(key)
                    if key_str:
                        vars_dict[key_str] = _normalize_env_value(value)
            elif isinstance(vars_part, list):
                for item in vars_part:
                    key_str = str(item)
                    if key_str:
                        vars_dict[key_str] = None
            elif vars_part:
                key_str = str(vars_part)
                if key_str:
                    vars_dict[key_str] = None
            return vars_dict

        def _extract_locations(loc_part: Any) -> List[str]:
            if isinstance(loc_part, list):
                return [str(item) for item in loc_part if str(item).strip()]
            if loc_part:
                return [str(loc_part)]
            return []

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                vars_part = parsed.get("env_vars") or parsed.get("environment_variables") or parsed.get("vars") or {}
                loc_part = parsed.get("env_locations") or parsed.get("env_files") or parsed.get("locations") or []
                return {
                    "env_vars": _extract_vars(vars_part),
                    "env_locations": _extract_locations(loc_part),
                }
            if isinstance(parsed, list):
                return {"env_vars": _extract_vars(parsed), "env_locations": []}
        except Exception:
            pass
        return {
            "env_vars": _extract_vars(_parse_env_list(raw)),
            "env_locations": re.findall(r"\.env[^\s,;]*", raw, flags=re.IGNORECASE),
        }

    def _ask_for_envs(model: ChatOllama, chunk: str, existing: List[str], known_locations: List[str]) -> Dict[str, Any]:
        prompt = f"""
        Extract environment variable **names** and **values** from the repository documentation chunk below.
        If a variable has a configurable or example value, capture that value.  
        Also extract any mentioned environment file locations (e.g., `.env`, `.env.local`, `config/.env`). 
        The file location should be relative to download_directory. (e.g., a/b/download_directory/.env becomes .env)

        Do NOT repeat variables or locations already collected.

        Respond **ONLY** with a JSON object shaped exactly like:

        {{
        "env_vars": {{
            "VAR_NAME": "value from chunk or null if unspecified"
        }},
        "env_locations": ["list", "of", "new", "locations"]
        }}

        Use empty objects/arrays if the chunk provides nothing new.

        Already collected env vars: {existing}
        Already collected env file locations: {known_locations}

        Chunk:
        {chunk}

        Return ONLY the JSON object with newly found env vars and env file locations.
        """
        with ls.tracing_context(enabled=False):
            response = model.invoke(prompt)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        return _parse_env_response(str(content))

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
        raise Exception("Error: REPO_DIRECTORY environment variable is not set.")

    readme_candidates = ["README.en.md", "README.md"]
    targets, env_files = _collect_target_files(repo_directory, readme_candidates)
    if not targets:
        return {
            "env_vars": {},
            "env_locations": [".env"],
        }
    #Use beast
    # model = ChatOllama(
    #     model="gpt-oss:120b",
    #     temperature=0,
    #     base_url=os.getenv("OLLAMA_BASE_URL"),
    # )
    #Use self hosted
    model = ChatOllama(
        model="gpt-oss:20b",
        temperature=0,
        base_url=os.getenv("OLLAMA_BEAST_URL")
    )

    collected: Dict[str, Optional[str]] = {}
    env_locations: List[str] = [
        _normalize_location(os.path.relpath(path, repo_directory), repo_directory) for path in env_files
    ]
    env_locations = [loc for loc in env_locations if loc]
    env_seen: set[str] = set(env_locations)
    try:
        max_chunks = int(os.getenv("ENV_CHUNK_LIMIT", "400"))
    except Exception:
        max_chunks = 400

    all_chunks: List[str] = []
    for path in targets:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as f:
                text = f.read()
            print(f"read_environment_variables decoded {path} as latin-1")
        except Exception as exc:
            print(f"Skipping {path}: {exc}")
            continue

        if not text.strip():
            continue

        label = os.path.relpath(path, repo_directory)
        all_chunks.extend(_chunk_text(label, text))
    print("Current number of chunks: ", len(all_chunks))
    if len(all_chunks) > max_chunks:
        print(f"read_environment_variables: chunk limit exceeded ({len(all_chunks)} > {max_chunks})")
        fallback_locations = env_locations or [_choose_env_file_location(env_locations)]
        return {
            "state": "error",
            "message": "Env information not available: too many chunks to process",
            "env_vars": {},
            "env_locations": fallback_locations,
        }

    for chunk in all_chunks:
        ask_result = _ask_for_envs(model, chunk, sorted(collected), sorted(env_locations))
        new_vars = ask_result.get("env_vars", {})
        new_locations = ask_result.get("env_locations", [])
        for var, value in new_vars.items():
            normalized = _normalize_var(var)
            if normalized and normalized not in collected:
                collected[normalized] = _normalize_env_value(value)
        for loc in new_locations:
            normalized_loc = _normalize_location(loc, repo_directory)
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

@tool
def copy_env_file(destination_path: str) -> Dict[str, str]:
    """
    Copy the root .env to the provided destination path.
    Use this tool to copy the .env file to a target location.
    Destination is treated as relative to download_repository unless an absolute path is supplied.
    """
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
    
@tool
def write_env_file(text: str, path_to_write: str | None = None) -> str:
    '''
    Writes the provided text as a .env file in the root directory of the downloaded Github repository by default.
    Provide the argument for path_to_write to change what filename to write and where to write it relative to the root directory.
    For example, writing to download_directory/.env will require path_to_write to be .env
    '''
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return "REPO_DIRECTORY is not set"

    if path_to_write is None:
        file_path = os.path.join(repo_directory, ".env")
    else:
        file_path = os.path.join(repo_directory, path_to_write)

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

@tool
def write_docker_compose_file(text: str, path_to_write: str | None = None) -> str:
    '''
    Writes sanitized docker compose YAML within REPO_DIRECTORY.
    Defaults to docker-compose.yml in the repository root. path_to_write is treated as
    relative to REPO_DIRECTORY (download_directory prefixes are stripped) and must
    remain inside that directory.
    '''
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
    
@tool
def docker_container_check_v1(docker_compose_filepaths: List[str]) -> dict:
    """
    Starts docker containers via docker compose and returns status/logs per service.
    For each service found, returns its container state and exit code (when available) alongside recent logs.
    """
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        return {"state": "error", "message": "REPO_DIRECTORY is not set"}

    cleaned = []
    for path in docker_compose_filepaths:
        normalized = path.replace("\\", "/")
        if os.path.isabs(normalized):
            cleaned.append(normalized)
            continue
        if normalized.startswith("download_directory/"):
            normalized = normalized[len("download_directory/"):]
        elif "/download_directory/" in normalized:
            _, _, suffix = normalized.partition("/download_directory/")
            normalized = suffix or normalized
        cleaned.append(normalized.lstrip("/"))
    docker_compose_filepaths = cleaned

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
            base = ["sudo", "docker", "compose", "--project-name", "dynamov2"]
            for path in docker_compose_filepaths:
                base.extend(["-f", path])
            return base

        def _get_service_logs(project_root: str, service_name: str) -> str:
            logs_proc = subprocess.run(
                _compose_base_cmd() + ["logs", "--no-color", "--tail", "20", service_name],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return logs_proc.stdout

        compose_up_output = ""
        try:
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
                service_name = entry.get("Service") or entry.get("Name") or "unknown"
                service_details[service_name] = {
                    "state": entry.get("State"),
                    "exit_code": entry.get("ExitCode"),
                    "logs": _get_service_logs(project_root, service_name),
                }

            return {"state": "ok", "services": service_details}
        finally:
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

    timeout_seconds = 290
    result_container: dict = {}
    error_container: dict = {}

    def _worker():
        try:
            result_container["value"] = _start_docker_container_with_service_details(
                docker_compose_filepaths
            )
        except Exception as exc:  # capture and re-raise outside the thread
            error_container["error"] = exc

    worker_thread = threading.Thread(target=_worker, daemon=True)
    worker_thread.start()
    worker_thread.join(timeout_seconds)

    if worker_thread.is_alive():
        return {
            "state": "timeout",
            "message": "docker_container_check_v1 exceeded 5 minute timeout",
            "input_values": docker_compose_filepaths,
        }

    if "error" in error_container:
        return {"input_values": docker_compose_filepaths, "error": error_container["error"]}

    return result_container.get("value", {"state": "error", "message": "No result produced"})

env_tools = [write_env_file, read_environment_variables, read_file, return_secrets]
codex_tools = [docker_container_check_v1, write_docker_compose_file, copy_env_file, read_file]
