# createTO Info

Crawls [createto.ca](https://createto.ca) and the public files it links to, then turns them into a
source-cited, versioned Postgres dataset of Toronto real-estate projects, with their milestones,
news and procurement signals. The data is served through an API, a search bar and a small
retrieval-backed chatbot.

> Status: steps 1–5 of 9 done (schema, crawler, CMS parsing, evidence/history + variant demo,
> API + search page).
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
| `uv run createto serve` | API + search page at http://127.0.0.1:8000 (OpenAPI docs at `/docs`) |
| `uv run createto demo` | Variant demo (first / unchanged / changed source / failed file) replayed offline into a separate `createto_demo` DB; writes `reports/variants.md` |
| `uv run createto snapshot DIR` | Save the latest crawl state as a replayable snapshot (`manifest.json` referencing the raw store) |
| `uv run createto crawl --replay DIR` | Crawl a saved snapshot instead of the network |
| `uv run createto runs` | Recent runs with headline stats (`crawl_runs.stats` has the full set) |
| `uv run createto config show` | Print resolved `config.yaml` (no secrets) |
| `uv run createto db upgrade` / `downgrade base` | Apply / roll back migrations |
| `uv run createto db check` | Postgres version and row count per table |
| `uv run pytest` | Tests (DB tests are skipped if Postgres is down) |

## API

| Endpoint | What it returns |
|---|---|
| `GET /` | Search page: free text, phase filter, `near` point; `/#<slug>` deep-links a project |
| `GET /projects?q=&phase=&ward=&type=&initiative=&near=lat,lng&radius_km=` | Ranked project cards (score, distance, open procurement count) |
| `GET /projects/{slug}` | Project with cited facts, timeline, procurement, sub-sites, news, files |
| `GET /projects/{slug}/history` | Version diffs and superseded/retracted fact values |
| `GET /search?q=&near=` | Search bar: projects + news in one call |
| `GET /news?q=&kind=&project=&since=` | News and board-meeting items |
| `GET /procurement?status=open&kind=&project=` | Market offerings, procurement-titled files, MERX tenders, with evidence |
| `GET /runs`, `/stats`, `/facets`, `/health` | Crawl metrics, dataset counts, filter values |

Search ranking (`api/queries.py`): any-term full-text rank + all-terms bonus over name (A),
address/ward/type labels (B), summary/uses (C) and full description (D); plus 0.5 × best trigram
word-similarity on name and aliases (cut-off 0.6); plus 1.0 when a street address in the query
overlaps the project's address range ("1125 Dundas St W" finds "1113-1117 Dundas Street West").
With `near`, results also carry a haversine distance and `radius_km` filters.

Examples:
```bash
curl "localhost:8000/projects?q=affordable%20housing%20near%20transit&limit=5"
curl "localhost:8000/projects?near=43.6532,-79.3832&radius_km=2"
curl "localhost:8000/procurement?status=open"
curl "localhost:8000/projects/parkdale-hub"
```

## Layout
```
config.yaml               crawl scope, models, limits (stored on every run for reproducibility)
src/createto_info/        package: config, db models/session, CLI
  crawl/                  scope policy (urls), Next.js data reader, raw store, recorder, Scrapy spider
  parse/                  CMS materializer, claims/evidence reconciliation, MERX parser, helpers
  api/                    FastAPI app, SQL queries, static search page
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
