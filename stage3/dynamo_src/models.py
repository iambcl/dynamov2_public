import os
from dynamo_src.helper.connect_to_mcp import tools
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_experimental.tools import PythonREPLTool

BASE_URL = os.getenv("BASE_URL")
ENV_MODEL = os.getenv("GRAPH_MODEL")
ENV_EVAL_MODEL = os.getenv("EVAL_MODEL")

CODING_MODEL = os.getenv("CODEX_MODEL")
CODING_EVAL_MODEL = os.getenv("CODEX_EVAL_MODEL")

def model_generator(model, tools = None):
    if 'gpt-5' in model:
        llm = ChatOpenAI(
            model=model,
            temperature=0
        )
    else:
        llm = ChatOllama(
            model=model,
            temperature=0,
            num_ctx=65536,
            base_url=BASE_URL or "http://localhost:11434"
        )
    if tools:
        llm = llm.bind_tools(tools)
    return llm

env_tools = [tool for tool in tools if "container_check" not in tool.name and "get_repository_applications" not in tool.name]
coding_tools = [tool for tool in tools if "clone" not in tool.name and "secrets" not in tool.name and "read_env" not in tool.name and "get_repository_applications" not in tool.name]
if ENV_MODEL:
    env_model = model_generator(ENV_MODEL, env_tools)
    env_eval_model = model_generator(ENV_EVAL_MODEL)

if CODING_MODEL:
    coding_model = model_generator(CODING_MODEL, coding_tools)
    coding_eval_model = model_generator(CODING_EVAL_MODEL)