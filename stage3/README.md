# Stage 3: Agentic Compose Repair and Environment Generation

Stage 3 runs MCP-driven and LLM/Codex workflows to infer environment variables, repair compose-related issues, validate container startup, and record structured run outcomes.

Previous stage: [stage2/README.md](../stage2/README.md)  
Next stage: [stage4/README.md](../stage4/README.md)

## What Stage 3 Does

- Exposes an MCP server with tools for:
	- reading repository files and compose files
	- extracting environment variables
	- writing `.env`, Dockerfiles, and compose files
	- running docker compose checks
- Runs environment and codex-style agents (`GPT` and `Ollama` variants).
- Persists agent outputs to DB tables (`agent_run_results`, `agent_traffic_parameters`).
- Optionally deploys compose and captures PCAP outcomes for successful runs.

## Main Files

- [mcp_server.py](mcp_server.py): FastMCP server exposing tool endpoints.
- [run_agents_gpt.py](run_agents_gpt.py): GPT-backed env + codex orchestration.
- [run_agents_ollama.py](run_agents_ollama.py): Ollama-backed env + codex orchestration.
- [generate_env_files_from_agent_run.py](generate_env_files_from_agent_run.py): writes `.env` files from stored agent results.
- [generate_sankey_chart.py](generate_sankey_chart.py): visualizes run outcomes in sankey HTML output.
- [heuristic_generation.py](heuristic_generation.py): helper flow for env extraction and recording.
- [dynamo_src](dynamo_src): implementation source for containerized codex/agent flows.

## Prerequisites

- Python >= 3.13
- Docker Engine + Compose plugin
- PostgreSQL configured
- API/model credentials for configured providers
- `tshark` if compose deployment checks are enabled

## Environment Variables (Common)

- `RUN_ID` (required for run tracking)
- `REPO_DIRECTORY`
- `GITHUB_TOKEN`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `POSTGRES_CONTAINER`
- `POSTGRES_PORT`

Additional model keys depend on your selected backend (for example OpenAI/Ollama/Google keys used in your local config).

## Install

From `stage3/`:

```bash
uv sync
```

## Run

### 1. Start MCP Server

```bash
uv run python mcp_server.py
```

Server defaults to streamable HTTP on `0.0.0.0:8000`.

### 2. Inspect MCP Server (optional)

```bash
npx -y @modelcontextprotocol/inspector
```

### 3. Run Agent Pipelines

GPT path:

```bash
uv run python run_agents_gpt.py
```

Ollama path:

```bash
uv run python run_agents_ollama.py
```

### 4. Generate `.env` Files From Stored Results (optional)

```bash
uv run python generate_env_files_from_agent_run.py
```

### 5. Generate Sankey Report (optional)

```bash
uv run python generate_sankey_chart.py
```

## Outputs

- Agent run records and structured payloads in PostgreSQL.
- Generated `.env` files inside target repositories under `REPO_DIRECTORY`.
- PCAP files under `stage3/pcap/` when deployment checks succeed.
- Visualization output such as `sankey_chart.html`.

## Notes

- Many scripts in this stage are long-running and can process multiple repositories per run.
- `RUN_ID` is used to segment experiments and should be set explicitly before each batch.
