from dotenv import load_dotenv
load_dotenv()
import mcp_server
import asyncio
from dynamov2.git_utils.utils import clone_github_repo
from dynamov2.database.db_helper import db_helper
import os

LANGEXTRACT_MODEL = os.getenv("LANGEXTRACT_MODEL")

def generate_env(id: int):
    row = db_helper.get_github_repository(id)
    clone_github_repo(row.url, row.cleaned_docker_compose_filepath)
    env_graph_logs = asyncio.run(mcp_server.read_environment_variables_v2())
    db_helper.record_agent_run_result(env_result=env_graph_logs, repository_id=id, run_id=0, model=LANGEXTRACT_MODEL)