"""
Utility helpers for copying files and directories, with optional sudo elevation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import json
import ast
from pathlib import Path
from typing import Iterable, Union

PathLike = Union[str, Path]

def _normalize_result(raw_result: object, expected_keys: set[str]) -> dict:
    """Coerce agent output to a dict and validate expected keys."""
    def _parse_jsonish(text: str) -> object | None:
        """Parse JSON or Python-literal-ish text, trimming leading logs if needed."""
        for candidate in (text,):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            # Try to salvage JSON substring between the first/last brace.
            start, end = candidate.find("{"), candidate.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except json.JSONDecodeError:
                    pass
            # Fall back to ast for common non-JSON outputs (e.g., Python literals).
            try:
                return ast.literal_eval(candidate)
            except Exception:
                return None

    if isinstance(raw_result, str):
        cleaned = raw_result.strip()
        # Strip code fences like ```json or ``` to allow JSON parsing.
        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json") :].lstrip()
        elif cleaned.startswith("```"):
            cleaned = cleaned[len("```") :].lstrip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")].rstrip()
        parsed = _parse_jsonish(cleaned)
        if parsed is None:
            return {"status": False}
        # Some agent responses end up as JSON-encoded strings (e.g., "\"{...}\"").
        if isinstance(parsed, str):
            parsed = _parse_jsonish(parsed)
            if parsed is None:
                return {"status": False}
    elif isinstance(raw_result, dict):
        parsed = raw_result
    else:
        return {"status": False}

    if not isinstance(parsed, dict) or not expected_keys.issubset(parsed.keys()):
        return {"status": False}
    return parsed

def copy_path(
    source: PathLike,
    destination: PathLike,
    *,
    use_sudo: bool = False,
    sudo_command: str = "sudo",
    sudo_flags: Iterable[str] = ("-n",),  # non-interactive by default
    follow_symlinks: bool = True,
) -> Path:
    """
    Copy a file or directory from `source` to `destination`.

    - Automatically creates parent directories (with or without sudo).
    - Works even if destination or its parents are permission-protected.
    - Returns the final destination path.

    Set `use_sudo=True` if destination requires elevated privileges.
    """
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()

    if not src.exists():
        raise FileNotFoundError(f"Source '{src}' does not exist.")

    if use_sudo:
        return _copy_with_sudo(src, dst, sudo_command, sudo_flags, follow_symlinks)
    else:
        _ensure_parent_dir(dst, use_sudo=False, sudo_command=sudo_command, sudo_flags=sudo_flags)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            return dst
        if dst.is_dir():
            target = dst / src.name
        else:
            target = dst
        shutil.copy2(src, target, follow_symlinks=follow_symlinks)
        return target


# -----------------------------------------------------------------------------
# Internal sudo helpers
# -----------------------------------------------------------------------------

def _copy_with_sudo(
    src: Path,
    dst: Path,
    sudo_command: str,
    sudo_flags: Iterable[str],
    follow_symlinks: bool,
) -> Path:
    """Copy paths with sudo, avoiding permission errors on stat()."""

    # Ensure parent directories exist via sudo (no .exists() call)
    _ensure_parent_dir(dst, use_sudo=True, sudo_command=sudo_command, sudo_flags=sudo_flags)

    is_dir = _sudo_is_dir(src, sudo_command, sudo_flags)
    cp_cmd = [sudo_command, *sudo_flags, "cp"]

    if is_dir:
        # directory copy
        if _sudo_is_dir(dst, sudo_command, sudo_flags):
            # merge contents into existing dir
            cp_cmd += ["-a", f"{src}/.", str(dst)]
        else:
            cp_cmd += ["-a", str(src), str(dst)]
    else:
        # file copy
        if not follow_symlinks:
            cp_cmd.append("-P")
        cp_cmd += ["-p", str(src)]
        if _sudo_is_dir(dst, sudo_command, sudo_flags):
            cp_cmd.append(str(dst / src.name))
        else:
            cp_cmd.append(str(dst))

    _run_sudo(cp_cmd)
    return dst


def _ensure_parent_dir(
    path: Path,
    *,
    use_sudo: bool,
    sudo_command: str,
    sudo_flags: Iterable[str],
) -> None:
    """Create parent directories, using sudo if necessary."""
    parent = path.parent
    if use_sudo:
        _run_sudo([sudo_command, *sudo_flags, "mkdir", "-p", str(parent)])
    else:
        parent.mkdir(parents=True, exist_ok=True)


def _sudo_is_dir(path: Path, sudo_command: str, sudo_flags: Iterable[str]) -> bool:
    """Check if path is a directory using sudo (avoids PermissionError)."""
    res = subprocess.run(
        [sudo_command, *sudo_flags, "test", "-d", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return res.returncode == 0


def _run_sudo(command: list[str]) -> None:
    """Run a sudo command and raise clear error if it fails."""
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Sudo command failed: {' '.join(command)}") from exc
