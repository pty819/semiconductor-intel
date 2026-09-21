# semiconductor-intel

Semiconductor industry intelligence system built on the [NOOA agent framework](https://github.com/NVIDIA-NeMo/labs-OO-Agents): it ingests news, filings, PDFs and other sources about the semiconductor industry, runs NOOA-driven extraction and knowledge pipelines whose proposals are committed only after Python-side validation, and serves the results over a FastAPI API backed by PostgreSQL (full-text + vector search). The `intel` package lives in `src/intel/`; NOOA is a git dependency pinned to commit `d4d46f7` (see `[tool.uv.sources]` in pyproject.toml).

Full design specification (16 docs + contracts): `/Users/liyifan/Documents/Codex/2026-09-19-agent/semiconductor-intel-design/`

Frontend is Vue 3 (`web/`). Compose serves the production build behind nginx; local UI work uses `pnpm dev`.

## Development

```bash
uv sync          # create .venv and install dependencies (includes NOOA path dep)
uv run pytest    # run unit tests (integration/live markers excluded by default)
uv run ruff check src tests
```

Copy `.env.example` to `.env` and fill in secrets.

## Runtime binding (this machine)

| Role | Where |
|---|---|
| Postgres | **192.168.1.82:5432** — container `semiconductor-intel-pg`, user `postgres`, database `postgres`. URL: `postgresql+asyncpg://postgres:${PG_PASSWORD}@192.168.1.82:5432/postgres` |
| LLM | OpenAI-v1. Debug: 82 llamacpp `http://192.168.1.82:8080/v1` (model id from `GET /v1/models`, e.g. Bonsai-27B GGUF). A grok2api gateway is also OpenAI-v1 if you point `INTEL_LLM_BASE_URL` at it — **do not** put `grok-*` names in committed yaml. |
| NAS 21 | Optional only. `HOST=liyifan@192.168.1.21 HOST_PORT=5433 ./deploy/postgres/setup.sh` — not required for runtime. |

Placeholder model ids in `src/intel/nooa_adapter/registry/routes.yaml` (`openai/model-l1` / `l2` / `l3`) must be replaced with the ids `GET /v1/models` actually returns before a live smoke run.

## Local API + Vue

```bash
uv run alembic upgrade head
uv run intel create-user <login>     # prompts for password
uv run intel api --host 127.0.0.1 --port 8000
cd web && pnpm i && pnpm dev         # Vite on :5173; proxy /api yourself or call :8000
```

Production same-origin: compose `reverse-proxy` serves `web/dist` and proxies `/api` to the API.

## Workers

Job kinds are registered in `intel.workers.composition.build_runtime`:

| Compose service | `--role` | Kinds |
|---|---|---|
| pipeline-worker | `pipeline` | discover, source_poll, parse, index, route, extract, event_build, apply_review, watch_check |
| fetch-worker | `fetch` | fetch |
| research-runner | `research` | archive_answer, investigate, report_build |
| (local) | `all` | every handler above |

```bash
uv run intel worker --role all
uv run intel scheduler
```

Research jobs need `INTEL_GATEWAY_SECRET` (fail-closed when empty).

## Compose

```bash
export PG_PASSWORD=...
docker compose -f deploy/compose.yaml config     # must pass
# or: podman compose -f deploy/compose.yaml config
docker compose -f deploy/compose.yaml up --build
```

Services: `reverse-proxy`, `api`, `scheduler`, `pipeline-worker`, `fetch-worker`, `research-runner`. Postgres is **not** started by default (82 is the DB). Optional local image:

```bash
INTEL_DATABASE_URL=postgresql+asyncpg://postgres:${PG_PASSWORD}@postgres:5432/postgres \
  docker compose -f deploy/compose.yaml --profile local-db up --build
```

After the API is up: `uv run intel migrate` (or `alembic upgrade head`) against `INTEL_DATABASE_URL`. Migration `0004_intel_app_grants` gives the `intel_app` role DML; API/workers `SET LOCAL ROLE intel_app`.

## Seed + Etching trial

Golden fixtures live in `fixtures/golden/{etching-rf,etching-materials,etching-oes}/` (≥5 synthetic samples each: RF, 材料选择, OES). Offline eval:

```bash
uv run python tools/run_golden.py
```

Live trial (联调): create a user, create an industry whose topics cover Etching RF / 材料选择 / OES, enable feeds, wait for discover→fetch→parse→index→route→extract, then archive-answer a question in the Vue Conversations page.

## Still 联调 (not this task)

- Real `GET /v1/models` ids in `routes.yaml`
- End-to-end against 82 postgres + llamacpp
- NAS container rebuild if you switch HOST=
- Vue talking to the live API (Task 16 is mock-driven)
