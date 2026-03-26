from __future__ import annotations

from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from libs.agents.agents import ReActAgent
from langchain_core.messages import BaseMessage, HumanMessage
from dotenv import load_dotenv
from dynamov2_db.db_helper import db_helper
from typing import Any, Sequence
from pathlib import Path
import subprocess
import shutil
import json
from tqdm import tqdm
import sys
import os
from dynamov2_db.logger import CustomLogger


# ------------------------
# JSON-safe serialization
# ------------------------

def _json_safe(obj: Any) -> Any:
    """Recursively convert objects to JSON-safe types without losing info."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {str(_json_safe(k)): _json_safe(v) for k, v in obj.items()}

    # Pydantic BaseModel
    try:
        from pydantic import BaseModel  # type: ignore
        if isinstance(obj, BaseModel):
            return _json_safe(obj.model_dump())
    except Exception:
        pass

    # Enums
    try:
        import enum
        if isinstance(obj, enum.Enum):
            return obj.value
    except Exception:
        pass

    # Datetime
    try:
        from datetime import datetime
        if isinstance(obj, datetime):
            return obj.isoformat()
    except Exception:
        pass

    # Bytes → hex
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes__": True, "hex": bytes(obj).hex()}

    # Fallback
    return repr(obj)


def serialize_messages(messages: Sequence[BaseMessage]) -> list[dict]:
    """
    Turn LangChain messages into JSON-safe dicts.
    Preserves: type, content, name, id, tool_calls, tool_call_id,
    additional_kwargs, response_metadata.
    """
    out: list[dict] = []
    for m in messages:
        rec: dict[str, Any] = {
            "type": getattr(m, "type", m.__class__.__name__),
            "content": _json_safe(getattr(m, "content", None)),
        }
        for attr in ("name", "id", "tool_call_id"):
            val = getattr(m, attr, None)
            if val is not None:
                rec[attr] = _json_safe(val)

        tc = getattr(m, "tool_calls", None)
        if tc is not None:
            rec["tool_calls"] = _json_safe(tc)

        ak = getattr(m, "additional_kwargs", None)
        if ak:
            rec["additional_kwargs"] = _json_safe(ak)

        meta = getattr(m, "response_metadata", None)
        if meta:
            rec["response_metadata"] = _json_safe(meta)

        out.append(rec)
    return out


# --------------------------------
# Filesystem & Git helper funcs
# --------------------------------

def clone_github_repo(repo_url: str) -> None:
    """Clone a GitHub repo into the directory specified by REPO_DIRECTORY."""
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        raise Exception("Error: REPO_DIRECTORY environment variable is not set.")
    print(f"Cloning {repo_url} into {repo_directory}...")
    # This clones directly *into* the path in REPO_DIRECTORY (e.g., "./test")
    subprocess.run(
        ["git", "clone", repo_url, repo_directory],
        check=True,
        capture_output=True,
        text=True,
    )
    print("Clone successful.")


def delete_cloned_repo() -> None:
    """Delete the directory pointed by REPO_DIRECTORY (if it exists)."""
    repo_path = os.getenv("REPO_DIRECTORY")
    if not repo_path:
        raise Exception("Error: REPO_DIRECTORY environment variable is not set.")
    if not os.path.isdir(repo_path):
        print(f"Repository directory '{repo_path}' does not exist. Not removing anything.")
        return
    print(f"Deleting repository directory: {repo_path}")
    shutil.rmtree(repo_path)
    print("Deletion successful.")


def read_jsonl_file(file_path: str) -> list[dict]:
    data: list[dict] = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    data.append(obj)
                except json.JSONDecodeError as e:
                    print(f"JSON decoding error on line {line_number}: {e}")
    except FileNotFoundError:
        print(f"Error: File not found: {file_path}")
    except Exception as e:
        print(f"Unexpected error reading file {file_path}: {e}")
    return data


def append_to_jsonl_file(file_path: str, obj: Any) -> None:
    with open(file_path, "a", encoding="utf-8") as f:
        json_line = json.dumps(obj, ensure_ascii=False)
        f.write(json_line + "\n")


def reset_jsonl_file(file_path: str) -> None:
    try:
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        with open(file_path, "w", encoding="utf-8"):
            pass
    except Exception as e:
        print(f"Error resetting file {file_path}: {e}")


# -------------
# Main routine
# -------------
def main() -> None:
    load_dotenv()
    logger = CustomLogger('Agentic Labels', logfile_name='stage_5_agentic_labels.log')
    reasoning_llm = ChatOpenAI(model="gpt-4o-mini")
    tool_llm = ChatOpenAI(model="gpt-4o-mini")
    agent = ReActAgent(
        tool_calling_llm=tool_llm,
        reasoning_llm=reasoning_llm,
    )

    row = db_helper.get_repository_without_application_labels()
    logger.info(f"Retrieved {row}")
    count = 0
    while row:
        # Clean previous clone (if any)
        try:
            delete_cloned_repo()
        except Exception as e:
            print("could not delete repository:", e)
            # proceed anyway

        # Clone new repo
        try:
            clone_github_repo(row.url)
        except Exception as e:
            print("could not clone repo:", row.url, "due to:", e)
            sys.exit()

        # Analyze with agent
        message = HumanMessage(
            content="Start by analyzing the repo in the current working directory. "
        )
        response = agent.invoke(message)  # Expecting a dict-like result

        output = {}
        # Collect fields from response safely
        output["label"] = response.get("label")
        output["confidence"] = response.get("confidence")

        # Serialize messages to JSON-safe structure
        messages = response.get("messages", [])
        try:
            output["messages"] = serialize_messages(messages)
        except Exception:
            # As a last resort, store a repr so nothing is lost
            output["messages"] = _json_safe(messages)

        application_label_list = output["label"]
        application_label_id_list = []
        for labels in application_label_list:
            l = [label.strip() for label in labels.split(",")]
            for label in l:
                label = label.lower()
                application_label = db_helper.get_application_label(name=label)
                if application_label == None:
                    _, _, application_label = db_helper.add_application_label(label)
                application_label_id_list.append(application_label)

        for app_label_row in application_label_id_list:
            label_id = app_label_row.id 
            db_helper.assign_application_label_to_repository(repository_id=row.id, label_id=label_id, confidence= output["confidence"])
        row = db_helper.get_repository_without_application_labels()
        logger.info(f"Retrieved {row}")
        count += 1
            
if __name__ == "__main__":
    main()