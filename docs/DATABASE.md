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
                     ├─ facts ──────────────┐
                     ├─ milestones ─────────┼── evidence ── document_versions
                     ├─ procurement_signals ┘
   initiatives ── projects ── project_versions                       
   councillors ──────┤                                                
   news_articles ────┤                                                
                     └─ links (generic edges between any entities)    
           ┌──────── SERVING LAYER ────────────────────────────────┐
                    search_chunks (BM25 / FTS corpus for search + chat)
```

Three rules hold everywhere:
1. **Raw is immutable.** `document_versions` rows and files in `data/raw/` are never updated. A
   change on the site creates a new version.
2. **Every domain record cites a source.** Entities parsed from one page (projects, news,
   initiatives) carry `source_version_id`. Claims (facts, milestones, procurement signals) can be
   backed by many sources, each an `evidence` row (document version, page, locator, excerpt).
   Sources never overwrite each other; reconciliation rules are in PLAN.md §8.
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
| `title` | human title from a CMS resource list (e.g. "2023 - Planning and Housing Committee Staff Report") |
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
| `parse_status`, `parse_error` | integrity check at fetch time: PDFs must carry a `%PDF-` header and open without repair (all 471 real PDFs do); pages are `ok`; other files `pending`. A `failed` version is kept for audit but **never replaces** the document's last good current version |

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
| `reason` | for `fetched`: `new`, `changed`, `unchanged`, `failed_integrity` (stored for audit, not made current). For `skipped`: `out_of_scope`, `not_allowlisted`, `hop_limit`, `media_not_fetched`, `unsupported_file_type`, `skip_path`, `skip_files_flag`. For `blocked`/`failed`: `robots`, `http_<code>`, `timeout`, `dns`, `too_large`. For `redirected`: `to <url>` |
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
| `id`, `cms_uuid` (unique), `slug`, `url`, `name`, `summary`, `description` (full body text) | |
| `kind` | `project` (has its own page) or `block` (sub-site embedded on a parent page, e.g. "Bloor-Kipling – Block 2"; `cms_uuid` is the block's `_uid`, slug `parent#block-2`) |
| `address`, `major_intersection`, `latitude`, `longitude` | indexed (btree on lat/lng; PostGIS deferred) |
| `ward`, `councillor_id` | |
| `phase` (enum), `phase_label`, `types` (text[]), `initiative_id`, `parent_project_id` | GIN index on `types` |
| `site_area_text`, `site_area_acres` (parsed) | |
| `current_use`, `proposed_use`, `surrounding_use`, `delivery` | |
| `cms_created_at`, `cms_published_at`, `cms_updated_at` | CMS metadata, *not* project dates |
| `last_activity_date` | max(milestone dates, linked news dates) not in the future; used by the 5-year rule |
| `cutoff_status` | `in_window`, `excluded_completed_old`, `cutoff_unknown` (only meaningful when `phase='completed'`) |
| `source_version_id`, `first_seen_run`, `last_seen_run` | |
| `search_tsv` | generated tsvector over name, summary, address, ward, uses, types |

Indexes: `slug`, `phase`, `ward`, GIN(`search_tsv`), GIN trigram(`name`, `address`).

### `project_aliases`
`project_id, alias, kind`. `kind` is `name`, `slug`, `address`, `intersection` (rebuilt from the
CMS each parse), `curated` (from `config.yaml` → `curated_aliases`, also rebuilt) or `learned`
(hints Mammoth linked at p ≥ 0.85; persist across parses). Used for linking and search.

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
kind (board_meeting|project_update|news_release|insider|community_engagement|media_coverage;
from the CMS topic, no LLM),
importance, event_starts_at, event_ends_at, external_url, external_source, body_text,
source_version_id, search_tsv`.
Links to projects go through `links` (`rel='mentions'`).
`published_at` is the full date in the title when there is one (board meetings: "Corporate Board
Meeting - November 28, 2024"), else Storyblok `first_published_at`, which kept the original dates
(2017+) through the 2023 migration. `sort_by_date` is unreliable and ignored.

### `facts`
A value claimed for a project field. **Identity is `(project_id, field, value_key)`**: the same
value asserted by the CMS and by a PDF is one row with two `evidence` rows.

| column | notes |
|---|---|
| `id`, `project_id` (nullable), `project_hint` | |
| `field` (vocabulary in EXTRACTION.md), `label` | |
| `value_text`, `value_num`, `unit` | as first asserted |
| `value_key` | normalized identity: lower-cased, whitespace-collapsed text, or `"<num> <unit>"` |
| `as_of`, `as_of_precision` | latest document date asserting the value (CMS: page publish date) |
| `extracted_by`, `confidence` | highest-ranked source among its evidence (`cms` > `rule:*` > `model:*`) |
| `status` | `current`, `superseded`, `conflicting`, `retracted`. Recomputed every parse (PLAN.md §8) |
| `superseded_by`, `valid_from_run`, `valid_to_run` | corrections and amendments over time |

Link bookkeeping (facts, milestones, procurement signals): `link_status` (`linked`,
`organization`, `external`, `unresolved`), `link_method` (`cms`, `extractor`, `alias_match`,
`mammoth`), `link_confidence`. See EXTRACTION.md §6.

Status rules per `(project, field)`, among facts with **live** evidence: a CMS-backed value is
`current`; otherwise the latest `as_of` wins and equal dates with different values are all
`conflicting`; the rest are `superseded` (pointing at the winner). A fact with no live evidence
and no replacement is `retracted`.

### `evidence`
One source location backing a fact, milestone or procurement signal.
`entity_type (fact|milestone|procurement_signal), entity_id, version_id, page_no, locator (JSON
path / row id), excerpt (≤300 chars), extracted_by, confidence, extraction_id, first_run,
last_run`. Unique per `(entity, version, page, locator)`; re-asserting bumps `last_run`.
Evidence is **live** while its `version_id` is its document's current version and the document
isn't removed; that's how a re-crawl that changes a page retracts what the old page claimed.

### `milestones`
Dated project events.
`id, project_id, project_hint, type, label, title, date, date_precision, end_date, status,
extracted_by, confidence, first_seen_run, last_seen_run, retracted_run` + `evidence` rows.
Deterministic types: `phase_period` (one per CMS timeline step; `label` = phase key, with `#2`
when a phase repeats; dates from the schedule text) and `phase_change` (detected when a re-crawl
sees a new phase; dated by the run; never retracted, it's history). `status` is `actual`,
`current`, `planned`, `cancelled` or `delayed`.
Matching (same project and type): by `label` if the claim has one (structured identity), else
by dates overlapping at their precision, else title similarity. A lower-ranked source only fills
empty fields. `retracted_run` is set when no evidence is live any more.

### `procurement_signals`
Public evidence of buying, selling or partnering opportunities.
`id, project_id, project_hint, kind, title, reference_number, issuer, counterparty, scope, value,
currency, opens_at, closes_at, status, external_url, extracted_by, confidence, first_seen_run,
last_seen_run, retracted_run` + `evidence` rows.
Matching (same project and kind): reference number, else same open/close date, else title
similarity ≥ 0.85; lower-ranked sources fill empty fields only. A retracted `open` signal becomes
`closed` (project left the market-offering phase; tender gone from the MERX open listing).

Deterministic sources (`extracted_by`):
- `cms`: project in phase `marketOffering` → `market_offering`, `open`
- `rule:resource_title`: project file titled RFP / RFQ / REOI / market offering / ...
- `rule:merx`: rows of MERX's public open-solicitations listing → `solicitation` with
  reference number, buyer, published/closing dates; linked to a project by street-address range
  overlap ("1113-1125 Dundas St W" ↔ "1113-1117 Dundas Street West")

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

### Read queries
The API's read-side SQL lives in `src/createto_info/api/queries.py` (project cards, detail with
evidence, history, procurement, news, stats). No database views yet; add them if analysts need
the same shapes from SQL directly.

`projects.search_tsv` weights: name (A); address, intersection, ward, `type_labels` (B); summary,
current/proposed use (C); full `description` (D). `type_labels` is the human text of `types`
("Affordable Housing, Waterfront"), written by the materializer because an array can't feed a
generated tsvector.

## Typical queries

```sql
-- Projects in market offering near a point, by distance
SELECT name, phase, address,
       111.0 * sqrt(power(latitude - 43.64, 2) + power((longitude + 79.43) * cos(radians(43.64)), 2)) AS km
FROM projects WHERE phase = 'marketOffering' ORDER BY km LIMIT 10;

-- Every current fact about a project, with all its citations
SELECT f.field, f.value_text, e.extracted_by, d.url, e.page_no, e.excerpt
FROM facts f JOIN evidence e ON e.entity_type = 'fact' AND e.entity_id = f.id
JOIN document_versions v ON v.id = e.version_id JOIN documents d ON d.id = v.document_id
WHERE f.project_id = 12 AND f.status IN ('current', 'conflicting');

-- What changed in the last run
SELECT p.name, pv.diff FROM project_versions pv JOIN projects p ON p.id = pv.project_id
WHERE pv.run_id = (SELECT max(id) FROM crawl_runs);
```
