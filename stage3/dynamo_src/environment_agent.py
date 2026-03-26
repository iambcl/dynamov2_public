from dotenv import load_dotenv
load_dotenv()

from typing import TypedDict, Annotated, Sequence, List
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph.message import add_messages
from langgraph.graph import START,END, StateGraph
import os
from langgraph.prebuilt import ToolNode
from dotenv import load_dotenv
from dynamo_src.models import env_model as model, env_eval_model as eval_model, env_tools as tools

FEW_SHOT_EXAMPLES = '''

Example 1:

Ensure `project_root/out/.env` is synced w/ project.
Note: `project_root/.env` is ignored when building the project but can be used for encoding. A new `{project_root}/out/.env` will not be created, so keep this in mind.

Example 1 expected output:

{
  "env_vars": {},
  "env_locations": [".env", "out/.env"]
}

Example 2:

# 2. 创建并配置 .env 文件
# 进入后端目录，复制示例文件
cd backend
cp .env.example .env

Example 2 expected output:

{
  "env_vars": {
    "GOOGLE_API_KEY": "your-google-api-key-here"
  },
  "env_locations": [
    "backend/.env"
  ]
}

Example 3:

  mariadb:
    container_name: mariadb
    build:
      context: /home/bingcheng/mcp-server/download_directory/srcs/requirements/mariadb
      args:
      - DB_ROOT_PASSWORD
      - DB_NAME
      - DB_USER
      - DB_PASSWORD
    env_file: /home/bingcheng/mcp-server/download_directory/srcs/.env
    image: mariadb
    volumes:
    - mariadb:/var/lib/mysql
    networks:
    - inception
    restart: always

Example 3 expected output:

{
  "env_vars": {},
  "env_locations": ["srcs/.env"]
}

'''

SYSTEM_MESSAGE = '''
You are a environment file generating agent that uses tools to retrieve the required information.
Do not use placeholders for the API keys.
When passed the link of the repository, you should:

1) Use the read_environment_variables tool to get the information on what environmental variables is required and what is the default value. Some information may not be available from this tool alone.
2) Get the available secrets using tools available.
3) Create a .env file with the collected secrets to fill in the missing values for the environmental variables.

Use the tools and resources in the MCP server to do the above tasks and reason about the information given provided.
DO NOT tell the user what is needed in the .env file, create the file with the information obtained from available tools.
If there is no information to generate the .env file, create an empty .env file.
'''

HUMAN_MESSAGE = """
Generate a .env file with information retrieved from tools for a GitHub repository downloaded in download_directory. 
The .env file is meant to be used with docker compose up.
If the number of chunks being processed exceeds the limit, respond with status False.
Use the tool read_environment_variables that will provide information gathered from READMEs, environmental and compose files available in the repository.
If the environmental variable does not have a suggested value and other tools are not able to provide this information, add it into the .env file without a value.
If the environmental variable has a suggested value, keep the suggested value unless it can be meaningfully replaced with a value retrieved from tools.
Only add environmental variables that is required based on the information from tools.
For example, if openAI keys are not required, do not add it into the .env file.
If no .env file is required or you have generated a .env file via the tool write_env_file, respond with status True. 
Keep track of the environmental variables added. 

Few shot examples:

{FEW_SHOT_EXAMPLES}

Return your response in JSON with the following format: 

{{
"status": True/False, 
"environmental_variables_added": {{example var1: suggested value, example var2: suggested value, var3: None, example var4: suggested value}},
"env_location": the location where the env file should be stored
}}

""".format(FEW_SHOT_EXAMPLES=FEW_SHOT_EXAMPLES)

EVAL_MESSAGE = """Summarise the following message and return a response in JSON with the following format based on the message: 

{{
"status": True/False, 
"environmental_variables_added": {{example var1: suggested value, example var2: suggested value, var3: None, example var4: suggested value}},
"env_location": the location where the env file should be stored
}}

If no .env file is required or a .env file has been created, respond with status True. 

Message:

{message}

"""

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    repository_id: int

async def run_env_agent(repository_id: int) -> dict:
    response = await agent.ainvoke({"messages": [HumanMessage(content=HUMAN_MESSAGE)]},
                                   config={"metadata": {"repository_id": repository_id,
                                                        "run_id": os.getenv("RUN_ID"),
                                                        "model": os.getenv("GRAPH_MODEL")}})
    return response

async def stream_main():
    """
    Stream incremental updates from the agent for the same sample prompt used in main().
    Useful for observing tool calls and intermediate steps.
    """
    async for event in agent.astream(
        {"messages": [HumanMessage(content="Return the openai API key and Ollama host url")]},
        stream_mode="updates",
    ):
        print(event)


async def run(state: AgentState):
    if len(state["messages"]) == 1:
        system_message = SystemMessage(content=SYSTEM_MESSAGE)
        state["messages"] = [system_message] + state["messages"]
    response = await model.ainvoke(state["messages"])
    return {"messages": [response]}


def check_tool_call(state: AgentState):
    last_message = state["messages"][-1]
    if not last_message.tool_calls:
        return "next"
    return "continue"

def eval(state: AgentState):
    message = state["messages"][-1].content
    response = eval_model.invoke(EVAL_MESSAGE.format(message=message))
    return {"messages": [response]}

graph = StateGraph(AgentState)
graph.add_node('run', run)
graph.add_node('tools',ToolNode(tools))
graph.add_node('eval', eval)
graph.add_conditional_edges('run',check_tool_call,{
                            "continue": 'tools',
                            "next": 'eval'
                            })
graph.add_edge('tools','run')
graph.add_edge(START, 'run')
graph.add_edge('eval', END)

agent = graph.compile()
 
if __name__ == '__main__':
    # asyncio.run(run_env_agent("https://github.com/attunehq/attune"))
    pass
