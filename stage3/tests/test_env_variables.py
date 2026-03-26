import os
import asyncio
from types import SimpleNamespace

# Force REPO_DIRECTORY to the stage3 download_directory sandbox
REPO = os.path.abspath("/home/bingcheng/dynamov2/stage3/download_directory")
os.environ["REPO_DIRECTORY"] = REPO

import sys
import os as _os
# Ensure stage3 directory is on sys.path so `mcp_server` imports reliably
_root = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))
if _root not in sys.path:
    sys.path.insert(0, _root)

import mcp_server
from dynamov2.git_utils.utils import clone_github_repo
from dynamov2.database.db_helper import db_helper
'''
Tested: 
45719: {'env_vars': {'ACCEPT_EULA': 'Y', 'SA_PASSWORD': 'Password!23', 'MSSQL_PID': 'Developer', 'KEYCLOAK_USER': 'admin', 'KEYCLOAK_PASSWORD': 'admin', 'DB_VENDOR': 'mssql', 'DB_USER': 'sa', 'DB_PASSWORD': 'Password!23', 'DB_ADDR': 'mssql', 'DB_DATABASE': 'Keycloak'}, 'env_locations': ['.env'], 'traffic_types': ['mssql', 'http']}
68064: {'env_vars': {'DBUSER': 'dbwebapp', 'DBPASS': 'dbwebapp', 'DBNAME': 'dbwebappdb', 'DBHOST': 'mysql-router', 'DBPORT': '6446', 'MYSQL_USER': 'root', 'MYSQL_HOST': 'mysql-server-1', 'MYSQL_PORT': '3306', 'MYSQL_PASSWORD': 'mysql', 'MYSQL_INNODB_NUM_MEMBERS': '3', 'MYSQL_ROOT_PASSWORDMYSQL': 'mysql', 'MYSQL_ROOT_HOST': '%', 'MYSQLSH_SCRIPT': '/scripts/setupCluster.js', 'MYSQL_SCRIPT': '/scripts/db.sql'}, 'env_locations': ['dbwebapp.env', 'mysql-router.env', 'mysql-server.env', 'mysql-shell.env', 'mysql-server.'], 'traffic_types': ['mysql', 'http']}

'''
ids = [28631]
for id in ids:
    row = db_helper.get_github_repository(id)
    clone_github_repo(row.url, row.cleaned_docker_compose_filepath)
    results = asyncio.run(mcp_server.read_environment_variables_v2())
    print(results)
    db_helper.record_agent_run_result(env_result=results, repository_id=id, run_id=2, model=os.getenv("LANGEXTRACT_MODEL"), codex_result={})
