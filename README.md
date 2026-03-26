# DynamoV2 Pipeline

This repository is organized as a staged pipeline that discovers Docker Compose repositories, runs them to capture traffic, applies agent-based remediation, and extracts structured traffic/application signals.

## Pipeline Overview

1. [Stage 1](stage1/README.md): discover GitHub repositories containing docker-compose files and store metadata in PostgreSQL.
2. [Stage 2](stage2/README.md): clone and execute repositories, collect one-minute PCAPs, and update processing state.
3. [Stage 3](stage3/README.md): run MCP/agent workflows to generate `.env` values, fix compose/runtime issues, and capture agent-run outcomes.
4. [Stage 4](stage4/README.md): process PCAPs into traffic features and inferred application flows.
5. [Stage 5](stage5/README.md): run controlled namespace + swarm experiments for reproducible network traffic conditions.

## Repository Layout

- [stage1](stage1): GitHub search and metadata ingestion.
- [stage2](stage2): queue + processing workers for compose execution and packet capture.
- [stage3](stage3): MCP tools and LLM/Codex/OpenHands-oriented environment + compose correction runs.
- [stage4](stage4): packet parsing and traffic profile extraction.
- [stage5](stage5): network namespace and swarm testbed.
- [dynamov2_packages](dynamov2_packages): shared DB, logging, git/docker utilities.

## Prerequisites

- Linux host (recommended for full Stage 2/4/5 functionality).
- Python >= 3.13.
- Docker Engine with compose plugin.
- PostgreSQL (see [docker-compose.yaml](docker-compose.yaml)).
- GitHub token for API usage.
- Optional but commonly needed tools: `tshark`, `tc`, `ip`, `iptables`, `nsenter`.

## Common Environment Variables

Set in `.env` files for each stage as needed:

- `GITHUB_TOKEN`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `POSTGRES_CONTAINER`
- `POSTGRES_PORT`
- `REPO_DIRECTORY`

Stage-specific extras are documented in each stage README.

## Quick Start

1. Start PostgreSQL infrastructure:

```bash
docker compose -f docker-compose.yaml up -d
```

2. Run stages in order:

- [stage1](stage1/README.md)
- [stage2](stage2/README.md)
- [stage3](stage3/README.md)
- [stage4](stage4/README.md)
- [stage5](stage5/README.md)

## Notes

- Stages are loosely coupled through PostgreSQL records and generated artifacts (`pcap`, logs, compose/env outputs).
- Some long-running scripts are designed to run continuously in polling loops.
- Prefer running each stage from its own folder so local `.env` and relative paths resolve correctly.