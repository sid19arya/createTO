# createTO Info

Crawls [createto.ca](https://createto.ca) and the public files it links to, then turns them into a
source-cited, versioned Postgres dataset of Toronto real-estate projects, with their milestones,
news and procurement signals. The data is served through an API, a search bar and a small
retrieval-backed chatbot.

> Status: all 9 planned steps done (schema, crawler, CMS parsing, evidence/history + variant demo,
> API + search page, LLM extraction from PDFs/news, record linking, cited chatbot).
> See [PLAN.md](PLAN.md) §6 and the example output in [reports/variants.md](reports/variants.md).

## How it works
```
 createto.ca + linked PDFs / MERX
              │
   1. crawl   │  Scrapy, polite; raw copies → data/raw/, fetch metadata → Postgres
              ▼
   2. parse   │  CMS (__NEXT_DATA__) → projects, news, facts, milestones, procurement (no LLM)
              ▼
   3. extract │  PDF pages → LLM facts with verified evidence → link records to projects
              ▼
   4. serve   │  FastAPI: JSON API + search page (+ BM25 chatbot, step 8, not built yet)
```
Each step reads what the previous one stored, so any step can be re-run on its own. Re-runs are
incremental: unchanged pages return 304, parse is an idempotent upsert, and extraction/linking
only spend LLM calls on changed inputs.

| Step | Command(s) | Output |
|---|---|---|
| 1. Crawl | `uv run createto run` | The whole pipeline as one recorded run (per-stage time and stats on the run) |
| `uv run createto report [RUN_IDS]` | Run report for the last two pipeline runs → `reports/run_report.md`, `.json`, `examples/` |
| `uv run createto crawl` | `data/raw/`, `documents`, `fetch_log`, `crawl_runs` |
| 2. CMS parse | `uv run createto parse` | projects, news, initiatives, facts with evidence and version history |
| 3. LLM extraction | `uv run createto pdfs` → `uv run createto extract` → `uv run createto link` | PDF page text/renders, LLM facts, records linked to projects (needs `OPENROUTER_API_KEY`) |
| 4. Serve | `uv run createto serve` | http://127.0.0.1:8000 |

Once `serve` is running, you can reach the data at:
- **Search page:** http://127.0.0.1:8000/ (free text, phase filter, near a point)
- **JSON API:** for example `curl "localhost:8000/projects?q=affordable%20housing"`. All endpoints
  are listed under [API](#api).
- **Interactive API docs:** http://127.0.0.1:8000/docs
- **Postgres directly:** `localhost:5432` (see [docs/DATABASE.md](docs/DATABASE.md))

Do the [Setup](#setup) first. [Commands](#commands) lists every option (dev crawls, rebuilds,
model comparison, replay/demo).

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

## Run everything
```bash
uv run createto run               # crawl -> pdfs -> parse -> extract -> link -> index (one run)
uv run createto run               # unchanged repeat: 304s, cached extraction/linking, $0
uv run createto report            # reports/run_report.md from the database + reports/examples/
uv run createto serve             # API, search page and chat on http://127.0.0.1:8000
```
Results of the two deliverable runs: [reports/run_report.md](reports/run_report.md).
Variants (unchanged / changed source / failed file): [reports/variants.md](reports/variants.md).
Model choice and cost projection: [reports/model_comparison.md](reports/model_comparison.md).

## Commands
| Command | What it does |
|---|---|
| `uv run createto run` | The whole pipeline as one recorded run (per-stage time and stats on the run) |
| `uv run createto report [RUN_IDS]` | Run report for the last two pipeline runs → `reports/run_report.md`, `.json`, `examples/` |
| `uv run createto crawl` | Full polite crawl: sitemap + links, raw files to `data/raw/`, metadata to Postgres |
| `uv run createto crawl --limit 25` | Dev crawl; partial runs never mark documents as removed |
| `uv run createto parse` | Build projects, news, facts, milestones, procurement signals from stored pages (no LLM) |
| `uv run createto parse --rebuild` | Same, after wiping derived tables (drops version history; dev only) |
| `uv run createto serve` | API + search page at http://127.0.0.1:8000 (OpenAPI docs at `/docs`) |
| `uv run createto pdfs` | Per-page PDF text, document dates and page renders (local, no LLM) |
| `uv run createto compare` | Model comparison on a stratified sample + full-run cost projection → `reports/model_comparison.md` (≈ $0.80, mostly the paid Inkling side) |
| `uv run createto extract [--limit N]` | LLM extraction with verified evidence (Luna; ≈ $0.78 for a full first run, $0 when unchanged) |
| `uv run createto link` | Link extracted records without a project (free Mammoth decisions; organization / external / unresolved otherwise) |
| `uv run createto demo` | Variant demo (first / unchanged / changed source / failed file) replayed offline into a separate `createto_demo` DB; writes `reports/variants.md` |
| `uv run createto snapshot DIR` | Save the latest crawl state as a replayable snapshot (`manifest.json` referencing the raw store) |
| `uv run createto crawl --replay DIR` | Crawl a saved snapshot instead of the network |
| `uv run createto index` | Rebuild the retrieval corpus (`search_chunks`: projects, news, PDF pages) |
| `uv run createto ask "question"` | Cited answer from the terminal (same pipeline as `POST /chat`) |
| `uv run createto export` | Every entity as JSON lines with evidence embedded + `manifest.json` → `data/exports/` |
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
| `GET /organization?q=` | CreateTO-level records: board decisions, budgets, policy, with evidence |
| `GET /facts?field=&project=&min_value=&max_value=&source=&status=&link_status=&q=` | Facts across projects with evidence (e.g. `field=affordable_units&min_value=200`) |
| `GET /facts/fields`, `GET /facts/{id}` | Field counts; one fact with all evidence, what it replaced and alternatives |
| `GET /milestones?type=&status=&project=&from=&to=` | Dated events across projects (e.g. `type=construction_start&from=2026-01-01`) |
| `GET /documents?project=&kind=&scope=&q=`, `/documents/{id}`, `/documents/{id}/pages/{n}` | Crawled pages and files: versions, integrity, linked projects, extracted records, page text |
| `GET /initiatives`, `GET /councillors` | Each with its projects |
| `GET /runs/{id}`, `GET /runs/{id}/fetch-log?outcome=blocked` | Run stats, fetch outcomes, LLM calls and billed cost; the audit of fetched/skipped/blocked URLs |
| `POST /chat` `{question, history}` | Answer using only retrieved sources, with `[n]` citations → URL + page; `GET /chat/search?q=` shows the retrieval |
| `GET /export/{entity}.jsonl` | Bulk JSON lines: projects, facts, milestones, procurement_signals, news, documents, initiatives, councillors, runs |
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

## Chat

`POST /chat` (and the Ask box on `/`) retrieves the top 8 chunks with BM25 over
`search_chunks` (77 project profiles with their current facts, 136 news articles, ~5,000 PDF
pages; image pages via their model-written descriptions), keeping up to 3 slots for live project
profiles and at most 2 chunks per document. A live **dataset overview** source (projects by
current phase, open procurement) is always included, so "which / how many" questions don't
depend on retrieval. `openai/gpt-6-luna` answers only from those numbered sources and cites them;
when they don't contain the answer it says so. ≈ $0.0005 per question, logged in `llm_calls`
(task `chat`).

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
