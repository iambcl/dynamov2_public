# Stage 2: Compose Execution and PCAP Collection

Stage 2 consumes repositories discovered in Stage 1, runs their docker-compose stacks, and performs a one-minute traffic check with packet capture.

Previous stage: [stage1/README.md](../stage1/README.md)  
Next stage: [stage3/README.md](../stage3/README.md)

## What Stage 2 Does

- Polls repositories that are not yet processed for traffic checks.
- Clones each repository into `REPO_DIRECTORY`.
- Builds/starts compose services (`docker compose up --no-start` then run).
- Detects compose networks and captures traffic with `tshark`.
- Stores success/failure and subnet metadata in DB via `db_helper.update_traffic_parameters(...)`.
- Cleans cloned repositories after each run.

## Main Files

- [process_repository.py](process_repository.py): main repository processor and traffic capture logic.
- [queue_generator.py](queue_generator.py): queue coordinator that publishes work and consumes completion messages.
- [src/message_queue.py](src/message_queue.py): queue producer/consumer utility for `require_processing` and `processing_complete` queues.
- [src/message_worker.py](src/message_worker.py): worker abstraction for consuming tasks and publishing processed results.
- [src/clean_compose_file.py](src/clean_compose_file.py): normalizes compose host paths to absolute paths.

## Prerequisites

- Python >= 3.13
- Docker Engine + Compose plugin
- `tshark` installed and available in `PATH`
- PostgreSQL configured (same DB used by Stage 1)
- RabbitMQ reachable if using queue-based orchestration

## Required Environment Variables

- `GITHUB_TOKEN`
- `REPO_DIRECTORY`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `POSTGRES_CONTAINER`
- `POSTGRES_PORT`

For queue mode ([src/message_queue.py](src/message_queue.py), [src/message_worker.py](src/message_worker.py)):

- `RABBITMQ_HOST`
- `RABBITMQ_PORT`
- `RABBITMQ_USERNAME`
- `RABBITMQ_PASSWORD`
- `RABBITMQ_VHOST`

## Install

From `stage2/`:

```bash
uv sync
```

## Run

### Direct Processor Loop

```bash
uv run python process_repository.py
```

This mode continuously polls DB for unprocessed repositories.

### Queue-Oriented Flow

1. Start queue coordinator:

```bash
uv run python queue_generator.py
```

2. In another terminal, run worker(s) as needed:

```bash
uv run python src/message_worker.py run
```

## Output Artifacts

- PCAP files under `stage2/pcap/`.
- Logs under `stage2/logs/` and `stage2/ssl_logs/`.
- Traffic processing status stored in PostgreSQL.

## Operational Notes

- This stage uses long-running polling loops and is expected to run continuously.
- On repeated failures, records are marked in DB with `failure_reason` and `one_minute_check=False`.
- The processor periodically prunes Docker images/build cache to avoid disk exhaustion.
