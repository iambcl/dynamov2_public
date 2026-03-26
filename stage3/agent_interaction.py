import asyncio
from datetime import datetime
from pathlib import Path
import os
import inspect
import json
from typing import Any
from dotenv import load_dotenv
import mcp_server
load_dotenv()
from openhands.sdk import Agent, Conversation, Event, LLM, LLMConvertibleEvent
from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.sdk.tool import Tool
from openhands.tools.browser_use import BrowserToolSet
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool
from dynamo_src.repository_preparation import prepare_repository_for_interaction

MODEL = os.getenv("CODEX_MODEL")
COMPOSE_PROJECT_NAME = "agent_interaction"
LOG_DIR = Path(os.getenv("AGENT_INTERACTION_LOG_DIR", str(Path(__file__).resolve().parent / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LLM_EVENT_LOG_FILE = LOG_DIR / f"llm_events_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

TASK_PROMPT = \
"""
Explore sites hosted locally to generate network traffic.
Here are some common environmental variables that can be used:
This key can be used for Gemini models on a free tier. 
Stick to models that allow for a high number of interactions, since the agents will be exploring websites and generating traffic, which can lead to a large number of API calls.

- MODEL=gemini-2.0-flash-lite
- LLM_PROVIDER=google
- LLM_ENDPOINT=https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent
- GOOGLE_API_KEY=AIzaSyCGLUFnVMc5cGgTBxe1IbQq5RJYNilIv20

You will be provided context below about the localhost sites available through the docker compose file and the README of the repository. 
Use this information to guide the explorer in generating website traffic.

README File context:

{readme_context}

Docker Compose File context:

{docker_compose_context}

This is the list of compose files you need to run to set up the local environment: {list_of_compose_files}
Make sure to run the compose files in the correct order if there are dependencies between them.

At the end of the run, make sure to teardown the compose environment created to avoid resource leakage.
"""

MCP_URL = "http://192.168.15.102:8000/mcp"


MCP_CONFIG = {
    "mcpServers": {
        "dynamov2_mcp": {
            "url": MCP_URL,
            "type": "sse",
        }
    }
}

def conversation_callback(event: Event):
    if isinstance(event, LLMConvertibleEvent):
        message = event.to_llm_message()
        llm_messages.append(message)

        # Print a readable, compact log line for each LLM message event.
        ts = datetime.now().strftime("%H:%M:%S")

        role = "unknown"
        content: Any = message
        if isinstance(message, dict):
            role = str(message.get("role", role))
            content = message.get("content", "")
        else:
            role = str(getattr(message, "role", role))
            content = getattr(message, "content", str(message))

        if isinstance(content, list):
            content = "\n".join(str(part) for part in content)
        else:
            content = str(content)

        divider = "-" * 80
        rendered = f"\n{divider}\n[{ts}] {role.upper()}\n{divider}\n{content}\n"
        print(rendered)

        # Persist each event so runs can be reviewed after execution.
        with open(LLM_EVENT_LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(rendered)
            log_file.write("RAW MESSAGE:\n")
            try:
                if isinstance(message, dict):
                    log_file.write(json.dumps(message, ensure_ascii=False, indent=2))
                else:
                    log_file.write(str(message))
            except Exception as exc:
                log_file.write(f"<failed to serialize message: {exc}>")
            log_file.write("\n\n")

# Tools
cwd = os.getenv("REPO_DIRECTORY")

tools = [
    Tool(name=BrowserToolSet.name),
    Tool(name=FileEditorTool.name)
]

llm_messages = []  # collect raw LLM messages

llm = LLM.subscription_login(vendor="openai", model=MODEL, prompt_cache_retention=None)

tool_regex = "^(browser.*|docker_container_check_v1|file_editor.*)$"

def _create_conversation() -> Conversation:
    agent = Agent(
        llm=llm, 
        tools=tools, 
        mcp_config=MCP_CONFIG,
        filter_tools_regex=tool_regex,
        system_prompt_filename=str(Path(os.getcwd()) / "agent_interaction_system_prompt.j2"),
        )
    return Conversation(agent=agent, callbacks=[conversation_callback], workspace=cwd)

async def run_interaction(id: int):
    print(f"LLM event log file: {LLM_EVENT_LOG_FILE}")
    github_row = prepare_repository_for_interaction(id)
    list_of_compose_files = github_row.cleaned_docker_compose_filepath
    docker_compose_context = ""
    
    path = Path(os.getenv("REPO_DIRECTORY"))
    for compose_file in list_of_compose_files:
        with open(path / compose_file, 'r') as f:
            content = f.read()
            docker_compose_context += f"Content of {compose_file}:\n{content}\n\n"
    
    readme_context = path / "README.md"
    if readme_context.exists():
        with open(readme_context, 'r') as f:
            readme_context = f.read()
    else:
        readme_context = "No README.md found."
    task_prompt = TASK_PROMPT.format(
        readme_context=readme_context, 
        docker_compose_context=docker_compose_context,
        list_of_compose_files=list_of_compose_files)

    conversation = _create_conversation()
    conversation.send_message(task_prompt)
    if inspect.iscoroutinefunction(conversation.run):
        await conversation.run()
    else:
        await asyncio.to_thread(conversation.run)

if __name__ == "__main__": 
    ids = [20693]
    for id in ids:
        # deploy_thread = threading.Thread(
        #     target=deploy_compose_and_record_agent_results,
        #     args=(row.id, row.cleaned_docker_compose_filepaths, row.name),
        #     kwargs={"run_id": "99"},
        #     name="deploy-compose-thread",
        #     daemon=False,
        # )
        # deploy_thread.start()

        asyncio.run(run_interaction(id))
        # deploy_thread.join()
