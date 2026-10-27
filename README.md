# createTO Info

Crawls [createto.ca](https://createto.ca) and the public files it links to, then turns them into a
source-cited, versioned Postgres dataset of Toronto real-estate projects, with their milestones,
news and procurement signals. The data is served through an API, a search bar and a small
retrieval-backed chatbot.

> Status: planning. See [PLAN.md](PLAN.md) for design, decisions and build order.

## Docs
- [PLAN.md](PLAN.md): goals, site findings, crawl scope, pipeline, roadmap, deferred work
- [docs/DATABASE.md](docs/DATABASE.md): what each table is for
- [docs/EXTRACTION.md](docs/EXTRACTION.md): LLM prompts, PDF handling, project linking
- [AGENTS.md](AGENTS.md): notes for coding agents and contributors

## Setup (planned)
```bash
cp .env.example .env          # add OPENROUTER_API_KEY
docker compose up -d db
# install + run commands will be added as the pipeline lands
```
