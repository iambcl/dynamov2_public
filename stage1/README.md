# Stage 1: GitHub Docker Compose Repository Discovery

This stage scans public GitHub repositories for `docker-compose` files, enriches each match with repository metadata, and stores results in PostgreSQL.

The main entry point is [github_search.py](github_search.py).

Next stage: [stage2/README.md](../stage2/README.md)

## What This Script Does

`github_search.py` performs a continuous GitHub Code Search over file path pattern:

- Query base: `docker-compose in:path`
- Query slicing: `size:<start>..<end>`
- Pagination: `per_page=100`, pages `1..10`

For each candidate repository, it:

1. Filters obvious non-target paths (`.github`, `.travis`) and some blocked repositories.
2. Pulls repository metadata from GitHub:
	 - last commit date
	 - stars
	 - open issues
	 - created date
	 - README presence
3. Resolves valid Docker Compose file paths via [libs/github/compose_finder.py](libs/github/compose_finder.py).
4. Inserts or updates repository records using `db_helper`.

## Key Behaviors

- Progress persistence:
	- Progress is saved to `.github_search_progress.json`.
	- On restart, the script resumes from the saved size window and page.
- Rate-limit handling:
	- If GitHub returns `X-RateLimit-Remaining: 0`, the script sleeps until reset.
- Retry logic:
	- Compose path discovery has exponential backoff retries via `safe_get_docker_compose_filepaths`.
- Data quality filter:
	- Repositories are only stored when at least one valid compose file path is found.

## Prerequisites

- Python `>=3.13`
- PostgreSQL reachable from your environment
- GitHub Personal Access Token

Stage 1 relies on `dynamov2_packages` for DB/logging utilities.

## Environment Variables

Set these in `.env` (or export in shell):

- `GITHUB_TOKEN`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `POSTGRES_CONTAINER`
- `POSTGRES_PORT`

If any PostgreSQL variable is missing, `db_helper` raises at import time.

## Install

From the `stage1` directory:

```bash
uv sync
```

If you do not use `uv`, install dependencies from [pyproject.toml](pyproject.toml) with your preferred tool.

## Usage

1. Initialize/create DB tables:

```bash
uv run python initialise_db.py
```

2. Run repository search:

```bash
uv run python github_search.py
```

## Resume and Reset

- Resume is automatic if `.github_search_progress.json` exists.
- To restart from default (`size:40..40`, page `1`), delete the progress file:

```bash
rm -f .github_search_progress.json
```

## Notes and Limitations

- GitHub Code Search has a practical cap of 1000 results per query slice; this script iterates size windows to continue coverage.
- Very large runs can take a long time due to rate limiting.
- Some API failures currently terminate execution (`sys.exit`) in specific branches.

## Related Files

- [github_search.py](github_search.py): main collector logic
- [initialise_db.py](initialise_db.py): create DB schema
- [libs/github/compose_finder.py](libs/github/compose_finder.py): compose path discovery and cleanup
