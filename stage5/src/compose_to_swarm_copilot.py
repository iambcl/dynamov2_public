"""Convert Docker Compose files into a Docker Swarm stack file using Copilot SDK.

This module intentionally has no CLI entrypoint. Import and call the async
functions from your application code.
"""

from __future__ import annotations

import os
import re
import subprocess
import json
import ast
from pathlib import Path

from copilot import CopilotClient, PermissionHandler

def _normalize_compose_relative_paths(
    compose_relative_paths: list[str] | str,
) -> list[str]:
    """Normalize compose paths from list[str], CSV string, or stringified list."""
    if isinstance(compose_relative_paths, list):
        normalized: list[str] = []
        for path in compose_relative_paths:
            item = str(path).strip()
            if not item:
                continue
            if item.startswith("[") and item.endswith("]"):
                try:
                    parsed = ast.literal_eval(item)
                    if isinstance(parsed, list):
                        normalized.extend(
                            [
                                str(parsed_item).strip().strip("\"'")
                                for parsed_item in parsed
                                if str(parsed_item).strip()
                            ]
                        )
                        continue
                except Exception:
                    pass
            normalized.append(item.strip("\"'"))
        return normalized

    raw = str(compose_relative_paths).strip()
    if not raw:
        return []

    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                return [
                    str(path).strip()
                    for path in parsed
                    if str(path).strip()
                ]
        except Exception:
            pass

    return [part.strip().strip("\"'") for part in raw.split(",") if part.strip()]


def _normalize_name_list(values: list[str] | str | None) -> list[str]:
    """Normalize list-like user input for service names."""
    if values is None:
        return []
    if isinstance(values, list):
        return [str(v).strip() for v in values if str(v).strip()]

    raw = str(values).strip()
    if not raw:
        return []

    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass

    return [part.strip().strip("\"'") for part in raw.split(",") if part.strip()]


SYSTEM_PROMPT = (
    "You are an expert Docker engineer. Convert Docker Compose files into a "
    "single Docker Swarm stack-compatible compose file. Return only YAML content."
)


def _extract_yaml(text: str) -> str:
    """Extract YAML from plain output or fenced markdown output."""
    fenced = re.search(r"```(?:yaml|yml)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip() + "\n"
    return text.strip() + "\n"


def _build_prompt(compose_files: list[tuple[str, str]]) -> str:
    joined_inputs = "\n\n".join(
        [
            f"Compose file path: {relative_path}\n```yaml\n{yaml_text}\n```"
            for relative_path, yaml_text in compose_files
        ]
    )
    return (
        "Convert these Docker Compose YAML files to one Docker Swarm stack YAML.\n"
        "Rules:\n"
        "1. Merge all compose inputs exactly as Docker Compose override semantics intend.\n"
        "2. Keep service names, environment variables, networks, volumes, and ports.\n"
        "3. Remove or adjust Compose-only features unsupported in Swarm.\n"
        "4. Add deploy blocks only when it is safe and obvious.\n"
        "5. Preserve semantics as closely as possible.\n"
        "6. Return only YAML, no explanation.\n\n"
        "Input YAML files:\n"
        f"{joined_inputs}"
    )


def _resolve_download_directory(download_directory: Path | None) -> Path:
    if download_directory is not None:
        return download_directory.resolve()

    repo_dir = os.getenv("REPO_DIRECTORY")
    if repo_dir:
        # In this project REPO_DIRECTORY points directly to download_directory.
        resolved_repo_dir = Path(repo_dir).resolve()
        if resolved_repo_dir.exists():
            return resolved_repo_dir

        # Backward-compatible fallback for environments where REPO_DIRECTORY
        # points to a repository root instead of download_directory.
        return (resolved_repo_dir / "download_directory").resolve()
    return Path("download_directory").resolve()


def _read_compose_files_from_download_directory(
    compose_relative_paths: list[str],
    download_directory: Path,
) -> list[tuple[str, str]]:
    compose_files: list[tuple[str, str]] = []
    for relative_path in compose_relative_paths:
        rel_path = Path(relative_path)
        if rel_path.is_absolute():
            raise ValueError(f"Compose path must be relative: {relative_path}")

        full_path = (download_directory / rel_path).resolve()
        if not full_path.exists():
            raise FileNotFoundError(f"Compose file not found: {full_path}")
        if not full_path.is_file():
            raise ValueError(f"Compose path is not a file: {full_path}")

        compose_files.append((relative_path, full_path.read_text(encoding="utf-8")))
    return compose_files


def _resolve_output_path_in_download_root(
    output_filename: str,
    download_directory: Path,
) -> Path:
    filename_only = Path(output_filename).name
    if not filename_only or filename_only in {".", ".."}:
        raise ValueError("output_filename must be a valid file name")
    return (download_directory / filename_only).resolve()


def _compose_cmd_for_files(
    compose_relative_paths: list[str],
    download_directory: Path,
    project_name: str,
    extra_compose_files: list[Path] | None = None,
) -> list[str]:
    cmd = ["docker", "compose", "--project-name", project_name]
    for relative_path in compose_relative_paths:
        compose_file = (download_directory / relative_path).resolve()
        cmd.extend(["-f", str(compose_file)])
    for extra_file in extra_compose_files or []:
        cmd.extend(["-f", str(extra_file.resolve())])
    return cmd


def _inspect_merged_services(
    compose_relative_paths: list[str],
    download_directory: Path,
    project_name: str,
) -> dict[str, dict]:
    """Return merged `services` from `docker compose config` as a dict."""
    compose_cmd = _compose_cmd_for_files(
        compose_relative_paths=compose_relative_paths,
        download_directory=download_directory,
        project_name=project_name,
    )
    config_output = subprocess.check_output(
        compose_cmd + ["config", "--format", "json"],
        text=True,
        cwd=download_directory,
    )
    merged = json.loads(config_output)
    services = (merged or {}).get("services") or {}
    return services if isinstance(services, dict) else {}


def _sanitize_image_component(value: str) -> str:
    lowered = value.lower()
    sanitized = re.sub(r"[^a-z0-9_.-]+", "-", lowered).strip("-._")
    return sanitized or "service"


def _generate_image_overrides_yaml(
    merged_services: dict[str, dict],
    project_name: str,
) -> tuple[str, dict[str, str]]:
    """Create override YAML for services with build but no image."""
    image_overrides: dict[str, str] = {}
    for service_name, service_def in merged_services.items():
        if not isinstance(service_def, dict):
            continue
        has_build = "build" in service_def
        has_image = bool(service_def.get("image"))
        if has_build and not has_image:
            image_overrides[service_name] = (
                f"local/{_sanitize_image_component(project_name)}-"
                f"{_sanitize_image_component(service_name)}:local"
            )

    if not image_overrides:
        return "", {}

    lines = ["services:"]
    for service_name in sorted(image_overrides):
        lines.append(f"  {service_name}:")
        lines.append(f"    image: {image_overrides[service_name]}")
    return "\n".join(lines) + "\n", image_overrides


def _generate_placement_overrides_yaml(
    merged_services: dict[str, dict],
    client_services: list[str],
    server_services: list[str],
    placement_label_key: str,
    client_label_value: str,
    server_label_value: str,
) -> str:
    """Create override YAML with deploy placement constraints for service groups."""
    if not client_services and not server_services:
        return ""

    known_services = set(merged_services.keys())
    unknown = sorted((set(client_services) | set(server_services)) - known_services)
    if unknown:
        raise ValueError(f"Unknown services in placement config: {', '.join(unknown)}")

    overlap = sorted(set(client_services).intersection(server_services))
    if overlap:
        raise ValueError(
            f"Services cannot be both client and server: {', '.join(overlap)}"
        )

    lines = ["services:"]

    for service_name in sorted(set(client_services)):
        lines.extend(
            [
                f"  {service_name}:",
                "    deploy:",
                "      placement:",
                "        constraints:",
                f'          - "node.labels.{placement_label_key} == {client_label_value}"',
            ]
        )

    for service_name in sorted(set(server_services)):
        lines.extend(
            [
                f"  {service_name}:",
                "    deploy:",
                "      placement:",
                "        constraints:",
                f'          - "node.labels.{placement_label_key} == {server_label_value}"',
            ]
        )

    return "\n".join(lines) + "\n"


def _build_local_images(
    compose_relative_paths: list[str],
    download_directory: Path,
    project_name: str,
    extra_compose_files: list[Path] | None = None,
) -> None:
    """Build compose services locally so images exist before conversion."""
    compose_cmd = _compose_cmd_for_files(
        compose_relative_paths=compose_relative_paths,
        download_directory=download_directory,
        project_name=project_name,
        extra_compose_files=extra_compose_files,
    )
    subprocess.run(
        compose_cmd + ["build"],
        check=True,
        cwd=download_directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _label_swarm_nodes_by_addr(
    *,
    placement_label_key: str,
    client_label_value: str,
    server_label_value: str,
    client_node_addr: str,
    server_node_addr: str,
) -> None:
    """Ensure swarm nodes are labeled for placement constraints.

    Node lookup is based on `docker node inspect ... .Status.Addr`.
    """
    node_ids_text = subprocess.check_output(["docker", "node", "ls", "-q"], text=True)
    node_ids = [line.strip() for line in node_ids_text.splitlines() if line.strip()]
    if not node_ids:
        raise RuntimeError("No swarm nodes found; cannot apply placement labels")

    client_labeled = False
    server_labeled = False
    for node_id in node_ids:
        node_addr = subprocess.check_output(
            ["docker", "node", "inspect", node_id, "--format", "{{.Status.Addr}}"],
            text=True,
        ).strip()
        if node_addr == client_node_addr:
            subprocess.run(
                [
                    "docker",
                    "node",
                    "update",
                    "--label-add",
                    f"{placement_label_key}={client_label_value}",
                    node_id,
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            client_labeled = True
        elif node_addr == server_node_addr:
            subprocess.run(
                [
                    "docker",
                    "node",
                    "update",
                    "--label-add",
                    f"{placement_label_key}={server_label_value}",
                    node_id,
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            server_labeled = True

    if not client_labeled or not server_labeled:
        missing = []
        if not client_labeled:
            missing.append(f"client node addr {client_node_addr}")
        if not server_labeled:
            missing.append(f"server node addr {server_node_addr}")
        raise RuntimeError(
            "Could not label expected swarm nodes: " + ", ".join(missing)
        )


async def convert_compose_files_to_swarm(
    compose_relative_paths: list[str] | str,
    output_filename: str = "docker-stack.yml",
    download_directory: Path | None = None,
    build_local_images: bool = True,
    compose_project_name: str = "swarm_convert",
    client_services: list[str] | str | None = None,
    server_services: list[str] | str | None = None,
    placement_label_key: str = "netns",
    client_label_value: str = "ns1",
    server_label_value: str = "ns2",
    client_node_addr: str = "172.16.1.1",
    server_node_addr: str = "172.16.2.1",
    auto_label_namespace_nodes: bool = True,
    model: str | None = None,
) -> Path:
    """Convert multiple compose files to a single swarm-compatible YAML file.

    Args:
        compose_relative_paths: Compose file paths relative to `download_directory`.
        output_filename: Output file name written at `download_directory` root.
        download_directory: Base directory containing compose files.
            Defaults to REPO_DIRECTORY, else ./download_directory.
        build_local_images: If True, run `docker compose build` before conversion.
        compose_project_name: Compose project name used during the build step.
        client_services: Services to constrain to client node label (default ns1).
        server_services: Services to constrain to server node label (default ns2).
        placement_label_key: Swarm node label key used for placement constraints.
        client_label_value: Swarm node label value for client services.
        server_label_value: Swarm node label value for server services.
        client_node_addr: Swarm node address for namespace ns1 worker.
        server_node_addr: Swarm node address for namespace ns2 worker.
        auto_label_namespace_nodes: If True, label swarm nodes before conversion.
        model: Optional model override. Falls back to CODEX_MODEL or gpt-5-mini.
    """
    normalized_compose_relative_paths = _normalize_compose_relative_paths(
        compose_relative_paths
    )
    if not normalized_compose_relative_paths:
        raise ValueError("compose_relative_paths must contain at least one file")

    resolved_download_directory = _resolve_download_directory(download_directory)
    output_path = _resolve_output_path_in_download_root(
        output_filename=output_filename,
        download_directory=resolved_download_directory,
    )
    compose_files = _read_compose_files_from_download_directory(
        compose_relative_paths=normalized_compose_relative_paths,
        download_directory=resolved_download_directory,
    )
    normalized_client_services = _normalize_name_list(client_services)
    normalized_server_services = _normalize_name_list(server_services)

    if auto_label_namespace_nodes and (
        normalized_client_services or normalized_server_services
    ):
        _label_swarm_nodes_by_addr(
            placement_label_key=placement_label_key,
            client_label_value=client_label_value,
            server_label_value=server_label_value,
            client_node_addr=client_node_addr,
            server_node_addr=server_node_addr,
        )

    generated_override_path = (
        resolved_download_directory / ".generated.swarm-image-overrides.yml"
    )
    generated_override_prompt_entry: tuple[str, str] | None = None
    generated_override_for_build: list[Path] = []
    generated_placement_override_path = (
        resolved_download_directory / ".generated.swarm-placement-overrides.yml"
    )

    try:
        merged_services = _inspect_merged_services(
            compose_relative_paths=normalized_compose_relative_paths,
            download_directory=resolved_download_directory,
            project_name=compose_project_name,
        )

        override_yaml, _image_overrides = _generate_image_overrides_yaml(
            merged_services=merged_services,
            project_name=compose_project_name,
        )
        if override_yaml:
            generated_override_path.write_text(override_yaml, encoding="utf-8")
            generated_override_for_build = [generated_override_path]
            generated_override_prompt_entry = (
                ".generated.swarm-image-overrides.yml",
                override_yaml,
            )
            compose_files.append(generated_override_prompt_entry)

        placement_yaml = _generate_placement_overrides_yaml(
            merged_services=merged_services,
            client_services=normalized_client_services,
            server_services=normalized_server_services,
            placement_label_key=placement_label_key,
            client_label_value=client_label_value,
            server_label_value=server_label_value,
        )
        if placement_yaml:
            generated_placement_override_path.write_text(
                placement_yaml,
                encoding="utf-8",
            )
            compose_files.append(
                (".generated.swarm-placement-overrides.yml", placement_yaml)
            )
    except subprocess.CalledProcessError as exc:
        config_output = exc.stdout or ""
        raise RuntimeError(
            "Failed to inspect merged compose config before conversion. "
            f"Project name: {compose_project_name}. Output: {config_output}"
        ) from exc

    if build_local_images:
        try:
            _build_local_images(
                compose_relative_paths=normalized_compose_relative_paths,
                download_directory=resolved_download_directory,
                project_name=compose_project_name,
                extra_compose_files=generated_override_for_build,
            )
        except subprocess.CalledProcessError as exc:
            build_output = exc.stdout or ""
            raise RuntimeError(
                "Failed to build local images via docker compose. "
                f"Project name: {compose_project_name}. Output: {build_output}"
            ) from exc

    model = model or os.getenv("CODEX_MODEL") or "gpt-5-mini"
    client = CopilotClient(
        {
            "cwd": str(resolved_download_directory),
        }
    )
    await client.start()

    session = None
    try:
        session = await client.create_session(
            {
                "model": model,
                "on_permission_request": PermissionHandler.approve_all,
                "system_message": {
                    "mode": "append",
                    "content": SYSTEM_PROMPT,
                },
            }
        )

        response = await session.send_and_wait(
            {"prompt": _build_prompt(compose_files)},
            timeout=300,
        )
        swarm_yaml = _extract_yaml(response.data.content)
        output_path.write_text(swarm_yaml, encoding="utf-8")
        return output_path
    finally:
        if session is not None:
            await session.destroy()
        if generated_override_path.exists():
            try:
                generated_override_path.unlink()
            except Exception:
                pass
        if generated_placement_override_path.exists():
            try:
                generated_placement_override_path.unlink()
            except Exception:
                pass
