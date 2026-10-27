# AGENTS.md

Guidance for coding agents (Claude Code, etc.) working in this repo. Read this first, then
`PLAN.md`.

## Project
Service that crawls createto.ca and its linked public files, extracts project data with
provenance and history into Postgres, and serves it via API, search and a BM25 chatbot.

- Plan, decisions, build order, deferred list: `PLAN.md`
- Schema guide: `docs/DATABASE.md` (update when migrations change)
- LLM prompts, PDF strategy, linking: `docs/EXTRACTION.md`
- Brief: `assets/task_description/CreateTO scraper.pdf`

## Status
- [x] Planning docs (2026-10-26)
- [x] Step 1: skeleton, docker-compose Postgres, migrations (2026-10-26)
- [x] Step 2: crawler + raw store + fetch_log + run stats (no LLM) (2026-10-26)
- [ ] Step 3: `__NEXT_DATA__` parsers → projects, news, initiatives, links, CMS facts

## Crawl notes
- Use `createto crawl --limit N` while developing. Only complete runs mark missing docs removed.
- Unchanged pages come back as 304. The spider re-reads links from the stored raw copy so
  discovery still reaches everything below them.
- Scope rules live in `config.yaml` → `scope` and `crawl/urls.py::classify` (unit-tested).

## Dev loop
- `uv sync --all-extras`, `docker compose up -d --wait db`, `uv run createto db upgrade`
- Before committing: `uv run ruff check src tests` and `uv run pytest`
- Schema changes: edit `db/models.py` → `alembic revision --autogenerate` → review → `alembic check`
  must report no drift. Alembic drops `use_alter` FKs from `create_table`, so add circular FKs
  with an explicit `op.create_foreign_key` (see the initial migration).
- Windows: Docker Desktop sometimes reports "unable to start"; `docker desktop restart` fixes it.

Update this checklist as steps land (build order in `PLAN.md` §6).

## Ground rules
- Secrets live in `.env` (gitignored). Never commit keys. Copy `.env.example`.
- Model IDs live in config, never in code. Mammoth (`baseten/mammoth-1:free`) uses
  `POST /api/alpha/decisions`, **not** chat completions.
- Free-model quota: 1,000 requests/day on the current key. Test against small fixtures first.
  Full runs go through the pipeline, never ad-hoc agent calls.
- Crawl politely: AutoThrottle, ≤2 concurrent per domain, identify the user agent, respect robots.
- No LLM output enters the DB without verified evidence (see EXTRACTION.md §3).
- Keep runs reproducible: record git SHA + config in `crawl_runs`.

## Repo etiquette
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- Commit small and often. Update README/AGENTS/docs in the same commit as the behaviour change.
- Log discoveries about the site (structure, quirks) in `PLAN.md` §2.
