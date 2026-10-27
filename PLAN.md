# CreateTO Info Service — Plan

Goal: crawl [createto.ca](https://createto.ca) (and the public files it links to), and turn it into a
queryable, source-cited, versioned dataset of Toronto real-estate **projects** and the news,
milestones and procurement signals around them. Serve it through an API, a search bar and a
retrieval-backed chatbot.

Brief: `assets/task_description/CreateTO scraper.pdf`. Related docs:
[docs/DATABASE.md](docs/DATABASE.md) (schema guide), [docs/EXTRACTION.md](docs/EXTRACTION.md)
(LLM prompts, PDF strategy).

---

## 1. What the brief grades (checklist)

- [ ] Start at homepage, crawl exhaustively, find linked files (may be on another host)
- [ ] Persist the scraped pages and files
- [ ] Useful data points (location, phase, dates, ...)
- [ ] Provenance: page/file URL on every fact; for files, page number + short excerpt
- [ ] History: extraction timestamps, corrections/amendments across runs
- [ ] Results in a format another service can use (Postgres + API + JSON export)
- [ ] Re-runnable as the site changes
- [ ] Log blocked/skipped links; be polite (no DDoS)
- [ ] Deliver: repo, run instructions, example output
- [ ] Two runs (first + unchanged repeat): time, requests, files processed, useful records, paid calls, cost
- [ ] Variants: unchanged run, changed source, failed file (saved local copies OK)
- [ ] Label estimated costs (model, hosting, other)

## 2. Site findings (probed 2026-10-26)

- `robots.txt` allows everything; `sitemap.xml` → `sitemap-0.xml` lists **232 URLs**
  (137 news, 75 projects, 5 initiatives, about/legal pages).
- Next.js on Vercel with a **Storyblok** CMS. Every page includes its CMS record as JSON in
  `<script id="__NEXT_DATA__">`. Project records carry `address`, `latitude`, `longitude`, `ward`,
  `councillorName`/`councillorLink`, `phase` (enum), `type`, `siteArea`, `currentUse`,
  `proposedUse`, `surroundingUse`, `majorIntersection`, `summary`, `timeline`, `resources`,
  `initiative`, `parentProject`, plus CMS `uuid`, `created_at`, `published_at`, `updated_at`.
  News records carry `topics`, `initiatives`, `relatedProjects`, `eventStartsAt/EndsAt`,
  `externalUrl`/`externalSource` (third-party press coverage).
  → **Structured fields need no LLM.** They are parsed by plain code.
- Phase enum: `preDevelopment, develomentConcept (sic), marketOffering, preConstruction,
  deliveryAndConstruction, completed, leasing, strategicAcquisition, planAmendment`.
- Linked files live on `a-us.storyblok.com/f/...` (Parkdale Hub alone links ~60 PDFs, some
  are City of Toronto council `backgroundfile-*.pdf` reports).
- **Procurement** page points to **MERX**. Solicitation detail pages need a login, but the
  public *open solicitations* search (`merx.com/public/solicitations/open?keywords=createto`) is
  linked from createto.ca, allowed by robots.txt and lists title, organization, published and
  closing dates (e.g. "Testing & Inspection Services at 1113-1125 Dundas St W", closing
  2026-11-02). Other procurement signals: `marketOffering` phase, market-offering brochures
  (Cushman & Wakefield / `f.datasrvr.com`), news, board-meeting documents, PDFs.
- `secure.toronto.ca` blocks non-browser user agents (Akamai). Logged as `blocked`.
- Site links to a UAT staging copy (`*-uat-*.vercel.app`). It is excluded.
- First full crawl (2026-10-26, crawl only): 781 documents (244 createto.ca pages, 441 Storyblok
  PDFs, 67 external pages, 27 external PDFs), ~940 MB, 916 requests, 287 s.
  - 109 of 232 sitemap URLs (mostly news) are **not linked** from any page (news listing is
    client-side paginated), so the sitemap seed is essential. 12 linked pages are missing from
    the sitemap.
  - Blocked: 28 Toronto Council agenda items on `secure.toronto.ca` (Akamai 403); robots.txt
    disallows the market-offering brochures on `f.datasrvr.com`, the Cushman & Wakefield page and
    a Dropbox file. These are respected and logged.
  - Broken links on createto.ca (404): `/leasing`, a malformed project-file link, two dead
    toronto.ca PDFs.
- Unchanged repeat: 639 of 784 documents answered 304; 0 false changes on createto.ca pages.
- PDF sample (12 Parkdale files): nearly all have a text layer (500–2,600 chars/page). ~10% of
  pages are low-text (display boards, site plans, renderings). One 75-page design-guideline PDF
  had 7k embedded images.
- CMS dates mostly start in 2023 (site migration), so they **cannot** be used as "project dates";
  real dates come from content (news, PDFs, timeline blocks).

## 3. Decisions

| Topic | Decision |
|---|---|
| Storage | Postgres 16 via `docker compose`; raw bytes on disk, content-addressed (`data/raw/<sha256>`) |
| Crawler | Scrapy (AutoThrottle, ≤2 concurrent/domain, HTTP cache, retries, conditional GETs) |
| Seed | sitemap.xml + homepage, then link-following; sitemap-vs-discovered diff is logged |
| Scope | See §4: createto.ca fully, Storyblok assets as first-party, **1 hop** to allow-listed hosts |
| 5-year cutoff | Applies **only to projects whose phase is `completed`**: drop if the latest known activity date is before `now − 5y`. Everything else (all other projects, all news, all files) is kept. If a completed project has no known date, it is kept and flagged `cutoff_unknown`. |
| Structured extraction | Deterministic `__NEXT_DATA__` parser, $0 |
| Gate / classifier | `baseten/mammoth-1:free` via `POST /api/alpha/decisions` (not chat completions) |
| Extractor | `thinkingmachines/inkling:free` (1M context, accepts images). JSON is validated with pydantic and retried on failure |
| Vision fallback | Same model on rendered PNGs of low-text PDF pages |
| Search | Postgres full-text search + `pg_trgm` + lat/lng distance |
| Chat | BM25 (`rank_bm25`) over projects + news + PDF page chunks, citations injected |
| API | FastAPI |
| Model IDs | in `config.yaml`, never hard-coded |

### LLM budget constraints (from `GET /api/v1/key`)
- Free models: **1,000 requests/day** on this key. Paid budget $100 (unused by default).
- Key expires **2026-11-02**. Final demo runs must happen before then, or with a new key.
- Every call is logged with tokens. Reports show `$0 paid` plus an **estimated** cost at the
  model's paid list price.

## 4. Crawl scope policy

| Host class | Rule |
|---|---|
| `createto.ca` | Full crawl, unlimited depth |
| `a-us.storyblok.com` (CMS assets) | Treated as first-party. Every linked file is downloaded |
| Allow-listed external (`www.toronto.ca`, `secure.toronto.ca`, project microsites like `www.2444eglinton.ca`, `www.merx.com`, `comms.cushwakedigital.com`, `www.canada.ca`) | **Max 1 hop**: fetch the page/file linked *directly* from a createto.ca page, store it, never follow its links. Useful for councillor one-pagers, council agenda items, project microsites, MERX open-solicitations listing, market-offering pages |
| Documents (`pdf`, `docx`, ...) on **any** host | Fetched at 1 hop when linked directly from createto.ca (e.g. market-offering brochures on `f.datasrvr.com`, partner press releases) |
| Media (images, video, fonts, `.tif`) | Not fetched; logged `media_not_fetched` / `unsupported_file_type` |
| Press coverage (Globe, Star, UrbanToronto, CP24, ...) | Not fetched (paywalls, copyright). The link is kept as news coverage metadata |
| Social / video / meeting (`twitter.com`, `instagram`, `linkedin`, `youtube`, `webex`, ...) | Not fetched. Recorded as a link (still useful metadata), logged `skipped: out_of_scope` |
| Anything else | Not fetched; logged `skipped: not_allowlisted` (review the log to grow the allow-list) |

External pages get the same rate limits and robots.txt checks. They are indexed for
search/chat and run through deterministic parsers (e.g. councillor profile). LLM fact extraction
runs on them only when they are linked from a project page.

## 5. Pipeline

```
discover ─▶ fetch ─▶ store raw + hash ─▶ changed? ──no──▶ (skip, mark last_seen)
                                           │yes
                                           ▼
                        parse (__NEXT_DATA__ / HTML / PDF text per page)
                                           ▼
                gate (mammoth) ─▶ extract (inkling, cached by hash+prompt+model)
                                           ▼
                     validate evidence ─▶ facts / milestones / procurement_signals
                                           ▼
                   link (CMS relations → name/address match → mammoth choice)
                                           ▼
                    materialize current tables + *_versions diffs + run stats
```

Idempotency: every LLM output is cached in `extractions` keyed by
`(input_hash, task, prompt_version, model)`. An unchanged repeat run therefore makes
**0 LLM calls** and only conditional GETs.

## 6. Build order (MVP first)

1. Repo skeleton, docker-compose Postgres, migrations (Alembic), config, README/AGENTS
2. Crawler + raw store + `fetch_log` + `crawl_runs` stats (no LLM)
3. `__NEXT_DATA__` parsers → projects, news, initiatives, links, CMS facts
4. Change detection + `*_versions` + variant demo (unchanged / changed / failed file)
5. FastAPI + search endpoint
6. PDF text per page + vision fallback + LLM extraction (facts, milestones, procurement)
7. Project linking for news/PDFs
8. BM25 chatbot
9. Run report: metrics from both runs, cost table, example output export

## 7. Deferred (later versions)

- MERX solicitation *detail* pages and closed/awarded listings (detail needs login; closed
  listing isn't linked from createto.ca, so it's 2 hops)
- PostGIS, map UI, ward polygons
- Embeddings / hybrid retrieval, reranking
- MCP server wrapping the API
- Scheduled re-crawls (cron / GitHub Actions) and change alerts
- OCR on scanned PDFs (Tesseract or OpenRouter `mistral-ocr` paid engine)
- Reading Storyblok's Content Delivery API directly instead of `__NEXT_DATA__`
- Joining with City of Toronto open data (council decisions, building permits, ward data)
- Toronto council agenda items/decisions via the City's open data (TMMIS) instead of
  `secure.toronto.ca`, which blocks crawlers
- Human review UI for low-confidence facts
