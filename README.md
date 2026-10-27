# createTO Info

Crawls [createto.ca](https://createto.ca) and the public files it links to, then turns them into a
source-cited, versioned Postgres dataset of Toronto real-estate projects, with their milestones,
news and procurement signals. The data is served through an API, a search bar and a small
retrieval-backed chatbot.

> Status: steps 1–4 of 9 done (schema, crawler, CMS parsing, evidence/history + variant demo).
> See [PLAN.md](PLAN.md) §6 and the example output in [reports/variants.md](reports/variants.md).

## Docs
- [PLAN.md](PLAN.md): goals, site findings, crawl scope, pipeline, roadmap, deferred work
- [docs/DATABASE.md](docs/DATABASE.md): what each table is for
- [docs/EXTRACTION.md](docs/EXTRACTION.md): LLM prompts, PDF handling, project linking
- [AGENTS.md](AGENTS.md): notes for coding agents and contributors

## Requirements
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Docker (for Postgres 16)
- An [OpenRouter](https://openrouter.ai) API key (LLM steps only)

## Setup
```bash
cp .env.example .env              # add OPENROUTER_API_KEY
uv sync --all-extras              # install deps into .venv
docker compose up -d --wait db    # Postgres on localhost:5432
uv run createto db upgrade        # apply migrations
uv run createto db check          # connectivity + table row counts
```

## Commands
| Command | What it does |
|---|---|
| `uv run createto crawl` | Full polite crawl: sitemap + links, raw files to `data/raw/`, metadata to Postgres |
| `uv run createto crawl --limit 25` | Dev crawl; partial runs never mark documents as removed |
| `uv run createto parse` | Build projects, news, facts, milestones, procurement signals from stored pages (no LLM) |
| `uv run createto parse --rebuild` | Same, after wiping derived tables (drops version history; dev only) |
| `uv run createto demo` | Variant demo (first / unchanged / changed source / failed file) replayed offline into a separate `createto_demo` DB; writes `reports/variants.md` |
| `uv run createto snapshot DIR` | Save the latest crawl state as a replayable snapshot (`manifest.json` referencing the raw store) |
| `uv run createto crawl --replay DIR` | Crawl a saved snapshot instead of the network |
| `uv run createto runs` | Recent runs with headline stats (`crawl_runs.stats` has the full set) |
| `uv run createto config show` | Print resolved `config.yaml` (no secrets) |
| `uv run createto db upgrade` / `downgrade base` | Apply / roll back migrations |
| `uv run createto db check` | Postgres version and row count per table |
| `uv run pytest` | Tests (DB tests are skipped if Postgres is down) |

## Layout
```
config.yaml               crawl scope, models, limits (stored on every run for reproducibility)
src/createto_info/        package: config, db models/session, CLI
  crawl/                  scope policy (urls), Next.js data reader, raw store, recorder, Scrapy spider
  parse/                  CMS materializer, claims/evidence reconciliation, MERX parser, helpers
  demo.py                 variant demo orchestration
reports/                  generated example outputs (committed)
migrations/               Alembic migrations (schema guide: docs/DATABASE.md)
tests/
data/                     raw pages/files and renders (gitignored)
```

## Changing the schema
Edit `src/createto_info/db/models.py`, then:
```bash
uv run alembic revision --autogenerate -m "describe change"   # review the generated file
uv run createto db upgrade
uv run alembic check                                          # must report no drift
```
Update `docs/DATABASE.md` in the same commit.
