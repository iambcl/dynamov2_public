from dotenv import load_dotenv
load_dotenv()

from typing import TypedDict, Annotated, Sequence, List
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import START, END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages
from pathlib import Path
from httpcore import ConnectError
import os, asyncio
from langchain_core.load import dumps
from dynamo_src.models import coding_model as model,  coding_eval_model as eval_model, coding_tools as tools

ROOT_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = (ROOT_DIR / os.getenv("REPO_DIRECTORY", "download_directory")).resolve()
RECURSION_LIMIT = int(os.getenv("RECURSION_LIMIT"))
BASE_URL = os.getenv("BASE_URL")

SYSTEM_PROMPT = \
"""You are an agent responsible for checking if a docker compose file is working correctly with docker. 
You will need to start docker containers with the tools available to you. 
If there is any errors, you will need to make changes to the code where the error occurred with the tools available to you.
A .env file will be created at the root directory. 
If the error has to do with the .env file, the .env file should be copied to the correct location referenced by the error message.
If there is a port conflict, make changes to the port used in the docker compose file.
You will only have access to the download_directory folder that has been mounted in a docker container.
Only update files within the download_directory folder.
You will be given a list of paths to docker compose files relative to the download_directory.
Keep track of what actions you have taken and provide it at the end in a list such as: ["step 1", "step 2", "step 3"].
"""

HUMAN_PROMPT = \
"""Run the docker compose files to check if there is any errors. 
If there is any errors, implement changes to the code based on the error message. 
If the error involves not having access to the docker image required, return False. (eg. requested access to the resource is denied)
A .env file will be created at the root directory. 
You will only have access to the download_directory folder that has been mounted in a docker container.
Only update files within the download_directory folder.
If the error has to do with the .env file, the .env file should be copied to the correct location referenced by the error message.
To check the results of the code change, run the docker compose files again. 
The docker compose file paths are: {docker_compose_filepaths}. 
Return the final response in JSON format with the keys and format:
{{ working: True/False, steps_taken: ["step 1", "step 2", "step 3"]}}
"""

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

def check_tool_call(state: AgentState):
    last_message = state["messages"][-1]
    if not last_message.tool_calls:
        return "end"
    return "continue"

async def run(state: AgentState):
    if type(state["messages"][0]) != SystemMessage:
        state["messages"] = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
    response = await model.ainvoke(state["messages"])
    return {"messages": [response]}

def eval(state: AgentState):
    message = state["messages"][-1]
    response = eval_model.invoke(
        "Summarise the message and return the final response in JSON format with the keys and format: \n"\
        "1) working: return True/False depending on whether the message suggests whether the docker compose file is working. \n" \
        "2) steps_taken: return a list of steps taken \n" \
        "Ignore errors concerning `version` attribute in the docker.compose files\n\n" \
        f"Message: \n\n{message.content}"
    )
    return {"messages": [response]}

graph = StateGraph(AgentState)
graph.add_node('run', run)
graph.add_node('tools',ToolNode(tools))
graph.add_node('evaluation', eval)
graph.add_conditional_edges('run',check_tool_call,{
                            "continue": 'tools',
                            "end": 'evaluation'
                            })
graph.add_edge('tools','run')
graph.add_edge(START, 'run')
graph.add_edge('evaluation', END)

codex = graph.compile()

async def main(docker_compose_filepaths: List[str], repository_id: int):
    try:
        response = await codex.ainvoke(
            {"messages": [HumanMessage(content=HUMAN_PROMPT.format(docker_compose_filepaths=docker_compose_filepaths))]},
            config={"metadata":
                        {
                         "repository_id": repository_id,
                         "run_id": os.getenv("RUN_ID"),
                         "model": os.getenv("CODEX_MODEL")
                         },
                    "recursion_limit": RECURSION_LIMIT,
                    }
        )
        return response
    except ConnectError as exc:
        return {
            "working": False,
            "steps_taken": f"Could not reach model endpoint at {BASE_URL}: {exc}",
        }
    except (GraphRecursionError,asyncio.TimeoutError):
        response = {
            "working": False,
            "steps_taken": "Recursion/Timeout limit met. Assume that the repository is faulty."
        }
        return response
    except Exception as exc:
        err_msg = str(exc)
        if "context_length_exceeded" in err_msg or "Input tokens exceed" in err_msg:
            return {
                "working": False,
                "error_type": "context_length_exceeded",
                "steps_taken": ["Model context limit exceeded while running coding agent."],
                "error": err_msg,
            }
        return {
            "working": False,
            "error_type": "unexpected_exception",
            "steps_taken": ["Coding agent failed with an unexpected exception."],
            "error": err_msg,
        }

if __name__ == '__main__':
    compose_env = os.getenv("DOCKER_COMPOSE_FILES")
    repository_id = os.getenv("REPOSITORY_ID")
    if compose_env is None:
        raise RuntimeError("DOCKER_COMPOSE_FILES environment variable must be set")
    compose_paths = [p.strip() for p in compose_env.split(",") if p.strip()]
    compose_paths = compose_paths or ["docker-compose.yml"]
    # print(f"DOCKER_COMPOSE_FILES (raw): {compose_env}")
    # print(f"Using compose paths: {compose_paths}")
    result = asyncio.run(main(compose_paths, repository_id))
    print(dumps(result))
