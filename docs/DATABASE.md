# Database Guide

Postgres 16 (`docker compose up -d db`). Extensions: `pg_trgm` (fuzzy name/address matching),
`unaccent`. Migrations live in `migrations/` (Alembic). This file is the human-readable guide.
Keep it in sync with the migrations.

## Mental model

```
           ┌──────────── CRAWL LAYER (what we fetched) ────────────┐
crawl_runs ─┬─ fetch_log                                            
            └─ document_versions ── documents                       
                     │   └─ pdf_pages                               
                     │                                              
           ┌──────── EXTRACTION LAYER (how we read it) ────────────┐
                     ├─ extractions (LLM/parser outputs, cached) ── llm_calls
                     │                                              
           ┌──────── DOMAIN LAYER (what it means) ─────────────────┐
                     ├─ facts ── fact_sources                      
   initiatives ── projects ── project_versions                       
                     ├─ milestones                                   
                     ├─ procurement_signals                          
   councillors ──────┤                                                
   news_articles ────┤                                                
                     └─ links (generic edges between any entities)    
           ┌──────── SERVING LAYER ────────────────────────────────┐
                    search_chunks (BM25 / FTS corpus for search + chat)
```

Three rules hold everywhere:
1. **Raw is immutable.** `document_versions` rows and files in `data/raw/` are never updated. A
   change on the site creates a new version.
2. **Every domain record cites a source.** It points to a `document_version` (and a page/locator
   plus excerpt) through `source_version_id` or `fact_sources`.
3. **Current tables + history.** `projects`, `facts`, etc. hold the current view. History lives
   in `project_versions`, superseded facts, and `first_seen_run` / `last_seen_run` columns.

---

## Crawl layer

### `crawl_runs`
One row per pipeline execution. It holds the numbers the brief asks for.

| column | notes |
|---|---|
| `id`, `started_at`, `finished_at` | |
| `mode` | `full`, `incremental`, `replay` (variant demos from saved copies) |
| `git_sha`, `config` (jsonb) | reproducibility: exact code + settings used |
| `status` | `running`, `succeeded`, `failed`, `partial` |
| `stats` (jsonb) | `requests`, `bytes`, `not_modified`, `pages_{new,changed,unchanged,removed}`, `files_{new,changed,unchanged,failed}`, `records_{facts,milestones,procurement,...}`, `llm_calls`, `llm_cached`, `tokens_in/out`, `cost_usd`, `est_cost_usd`, `evidence_rejected` |

Used by: run reports, `/runs` endpoint, the two-run comparison.

### `documents`
One row per **URL** ever seen (page or file), independent of content.

| column | notes |
|---|---|
| `id`, `url` (unique, normalized), `host` | |
| `kind` | `page`, `pdf`, `doc`, `sitemap`, `image`, `other` |
| `scope` | `first_party` (createto.ca), `asset` (storyblok), `external_1hop` |
| `removed_at_run` | set when a *complete* run (no `--limit`/`--skip-files`) no longer finds the URL |
| `discovered_via` | `sitemap`, `link`, `both` |
| `first_seen_run`, `last_seen_run`, `removed_at_run` | lifecycle; a URL missing from a run gets `removed_at_run` set |
| `etag`, `last_modified` | latest HTTP validators, sent as `If-None-Match` / `If-Modified-Since` next run (both createto.ca and Storyblok answer 304) |
| `current_version_id` | latest `document_versions.id` |

### `document_versions`
One row per **distinct content** of a document, keyed by `(document_id, content_hash)`. If a
repeat run fetches identical content, it adds no row and only bumps `documents.last_seen_run`.

`content_hash` is *semantic*: for createto.ca pages it hashes the page's own Storyblok record
(`pageProps.page`), so Next.js rebuilds and changes to derived lists (`articles`, `similar`)
don't count as changes. Other HTML pages hash their visible text (scripts, comments and
markup removed), so session tokens don't count as changes. Files hash their raw bytes. The raw
file is written to `storage_path` only when a new version is created.

| column | notes |
|---|---|
| `id`, `document_id`, `run_id` (run that first saw this content) | |
| `fetched_at`, `http_status`, `etag`, `last_modified`, `content_type`, `bytes` | |
| `content_hash` (sha256), `storage_path` | raw bytes at `data/raw/ab/abcdef….html/.pdf` |
| `cms_uuid`, `cms_updated_at`, `cms_json` (jsonb) | for createto.ca pages: `__NEXT_DATA__` `pageProps` minus site chrome (labels, header, footer, theme) |
| `text` | main-content plain text (HTML) or joined page text (PDF) |
| `parse_status`, `parse_error` | `ok`, `failed` (e.g. corrupt PDF; this is the "failed file" variant) |

Used by: everything downstream; "show me the page as it was on date X".

### `pdf_pages`
One row per page of a PDF version.

| column | notes |
|---|---|
| `version_id`, `page_no` | PK |
| `text`, `text_chars`, `image_count`, `drawing_count` | from PyMuPDF |
| `method` | `text`, `vision`, `blank`, `vision_skipped_cap` |
| `render_path`, `render_hash` | PNG for vision pages (dedupes repeated boards) |
| `vision_description` | model-written description, indexed for search |

### `fetch_log`
Every URL decision per run, including the ones we did not fetch. This is where blocked and
skipped links are logged, as the brief requires.

| column | notes |
|---|---|
| `run_id`, `url`, `referrer_url`, `ts` | |
| `outcome` | `fetched`, `not_modified`, `skipped`, `blocked`, `failed`, `redirected` |
| `reason` | for `fetched`: `new`, `changed`, `unchanged`. For `skipped`: `out_of_scope`, `not_allowlisted`, `hop_limit`, `media_not_fetched`, `unsupported_file_type`, `skip_path`, `skip_files_flag`. For `blocked`/`failed`: `robots`, `http_<code>`, `timeout`, `dns`, `too_large`. For `redirected`: `to <url>` |
| `http_status`, `elapsed_ms` | |

---

## Extraction layer

### `extractions`
One row per (input, task, prompt version, model). This is the **cache** that makes repeat runs free.

| column | notes |
|---|---|
| `id`, `input_hash`, `task` (`gate`, `extract`, `vision_extract`, `link`, `news_kind`), `prompt_version`, `model` | unique together |
| `version_id`, `page_from`, `page_to` | which source chunk |
| `output` (jsonb), `status` (`ok`, `invalid_json`, `skipped_by_gate`, `error`) | |
| `created_run`, `llm_call_id` | |

Changing a prompt bumps `prompt_version`, which re-extracts everything once. Unchanged content
with an unchanged prompt costs nothing.

### `llm_calls`
Raw ledger of every model request.

`id, run_id, ts, model, endpoint (chat|decisions), task, tokens_in, tokens_out, latency_ms,
status, http_status, cost_usd (reported), est_cost_usd (estimated, labelled), request_id`

---

## Domain layer

### `initiatives`
CreateTO's top-level categories (Housing, Economic Development, City Building & Cultural
Vitality, Environmental Sustainability, ...).
`id, cms_uuid, slug, name, description, url`.

### `projects`  (current view)
The main table. Columns come straight from the CMS record. Values derived from documents live
in `facts`.

| column | notes |
|---|---|
| `id`, `cms_uuid` (unique), `slug`, `url`, `name`, `summary` | |
| `address`, `major_intersection`, `latitude`, `longitude` | indexed (btree on lat/lng; PostGIS deferred) |
| `ward`, `councillor_id` | |
| `phase` (enum), `phase_label`, `types` (text[]), `initiative_id`, `parent_project_id` | GIN index on `types` |
| `site_area_text`, `site_area_acres` (parsed) | |
| `current_use`, `proposed_use`, `surrounding_use`, `delivery` | |
| `cms_created_at`, `cms_published_at`, `cms_updated_at` | CMS metadata, *not* project dates |
| `last_activity_date` | max(milestone dates, linked news dates); used by the 5-year rule |
| `cutoff_status` | `in_window`, `excluded_completed_old`, `cutoff_unknown` (only meaningful when `phase='completed'`) |
| `source_version_id`, `first_seen_run`, `last_seen_run` | |
| `search_tsv` | generated tsvector over name, summary, address, ward, uses, types |

Indexes: `slug`, `phase`, `ward`, GIN(`search_tsv`), GIN trigram(`name`, `address`).

### `project_aliases`
`project_id, alias, kind (slug|address|intersection|former_name|microsite)`. Used for linking
news and PDFs to projects.

### `project_versions`
Snapshot of a project's normalized fields every time they change.
`project_id, run_id, version_id, snapshot (jsonb), diff (jsonb: {field: [old, new]})`.
This answers "when did Parkdale Hub move to Market Offering?" and supports the "changed
source" variant demo.

### `councillors`
From the CMS `councillorName`/`councillorLink` plus the 1-hop toronto.ca profile page.
`id, name, ward, profile_url, email, phone, source_version_id`.

### `news_articles`
`id, cms_uuid, slug, url, title, published_at, author, topics (text[]), initiative_ids,
kind (announcement|event|board_meeting|press_coverage|procurement|report; from Mammoth),
importance, event_starts_at, event_ends_at, external_url, external_source, body_text,
source_version_id, search_tsv`.
Links to projects go through `links` (`rel='mentions'` or `'related'`).

### `facts`
Claim-level data with provenance. CMS fields and LLM-extracted document facts live side by side.

| column | notes |
|---|---|
| `id`, `project_id` (nullable), `project_hint` | |
| `field` (vocabulary in EXTRACTION.md), `label` | |
| `value_text`, `value_num`, `unit`, `as_of`, `as_of_precision` | |
| `extracted_by` | `cms`, `rule`, `model:<id>@<prompt_version>` |
| `confidence` | 1.0 for CMS |
| `status` | `current`, `superseded`, `retracted` (source removed) |
| `superseded_by`, `valid_from_run`, `valid_to_run` | corrections and amendments over time |

### `fact_sources`
Many-to-many: one fact can be backed by several documents.
`fact_id, version_id, page_no, locator (JSON path or CSS selector), excerpt (≤300 chars),
extraction_id`.

### `milestones`
Dated project events.
`id, project_id, type, label, title, date, date_precision, end_date, status
(actual|planned|cancelled|delayed), source_version_id, page_no, excerpt, extracted_by,
confidence, first_seen_run, last_seen_run`.
Deduped on `(project_id, type, date)`. A planned milestone that later appears as actual is
updated in place, with the change recorded in `project_versions.diff`.

### `procurement_signals`
Public evidence of buying, selling or partnering opportunities. **Not** MERX listings.
`id, project_id, kind, title, reference_number, issuer, counterparty, scope, value, currency,
opens_at, closes_at, status, external_url, source_version_id, page_no, excerpt, extracted_by,
confidence, first_seen_run, last_seen_run`.

### `links`
Generic graph edges, so the data forms a connected web.
`from_type, from_id, to_type, to_id, rel, method (cms|extractor|alias_match|mammoth|html_link),
confidence, run_id`.
Examples: `news →mentions→ project`, `project →related→ project`, `project →has_file→ document`,
`project →parent→ project`, `procurement_signal →about→ project`, `document →links_to→ document`
(the raw link graph, which doubles as the generated sitemap).

---

## Serving layer

### `search_chunks`
The corpus for BM25 chat retrieval and the search bar.
`id, entity_type (project|news|pdf_page|external), entity_id, project_id, title, text,
url, page_no, tsv, updated_run`.
Rebuilt incrementally for changed versions. The Python BM25 index is built from this table at
API startup.

### Views (convenience for API and analysts)
- `v_project_card`: project + initiative + councillor + latest milestone + open procurement count
- `v_project_timeline`: milestones ∪ dated news ∪ phase changes, ordered by date
- `v_open_opportunities`: procurement signals with status in (`upcoming`, `open`), joined to project location/phase
- `v_run_report`: `crawl_runs.stats` flattened for the deliverable table

## Typical queries

```sql
-- Projects in market offering near a point, by distance
SELECT name, phase, address,
       111.0 * sqrt(power(latitude - 43.64, 2) + power((longitude + 79.43) * cos(radians(43.64)), 2)) AS km
FROM projects WHERE phase = 'marketOffering' ORDER BY km LIMIT 10;

-- Every fact about a project, with citations
SELECT f.field, f.value_text, d.url, s.page_no, s.excerpt
FROM facts f JOIN fact_sources s ON s.fact_id = f.id
JOIN document_versions v ON v.id = s.version_id JOIN documents d ON d.id = v.document_id
WHERE f.project_id = 12 AND f.status = 'current';

-- What changed in the last run
SELECT p.name, pv.diff FROM project_versions pv JOIN projects p ON p.id = pv.project_id
WHERE pv.run_id = (SELECT max(id) FROM crawl_runs);
```
