# Extraction Design: Facts, Milestones, Procurement Signals

Two kinds of extraction:

1. **Deterministic** (no LLM): CMS JSON from `__NEXT_DATA__`. This produces projects,
   news, initiatives, relations, and *CMS facts* (one fact per populated field, locator = JSON path).
2. **LLM**: unstructured text (news bodies, PDF pages, 1-hop external pages). This produces
   *document facts*, milestones and procurement signals, each with a verbatim evidence quote.

The core rule: **no claim enters the database without evidence we can check by code.** The model
must return a verbatim quote. The validator confirms the quote appears in the source text
(whitespace/quote normalized, fuzzy ratio ≥ 0.9, or every line of a multi-line quote on the same
page for PDF tables) and rejects the claim otherwise.

Implementation: `extract/pdf.py` (pages, renders, dates), `llm/openrouter.py` (client, ledger,
caps), `extract/llm_extract.py` (units, prompt, cache, verification, claims), `extract/compare.py`
(model comparison + cost projection). Model choice and cost: §8.

---

## 1. Units of work ("chunks")

| Source | Chunk | Notes |
|---|---|---|
| News article | whole body | usually < 3k tokens |
| PDF, text layer | the **whole document**, with `=== PAGE n ===` markers, split into windows of ≤ 40 text pages | one call per window (482 for the current corpus); identical files at two URLs are processed once. The page markers give page-level citations |
| PDF page, low text | rendered PNG of one page | see §5 |
| External 1-hop page | main-content text | |

Each chunk has `input_hash = sha256(prompt version, header with candidate projects, text or
image hash)`. That hash, plus `task`, `prompt_version` and `model`, is the cache key in
`extractions`; only `ok` outputs are reused, so truncated/invalid answers are retried next run.

## 2. Stage A: Gate (Mammoth): dropped for extraction

The first design sent every chunk through `baseten/mammoth-1:free` (a *decisions* model:
`POST /api/alpha/decisions` with a `state` and typed `questions` of type `choice` / `score` /
`noul` / `box`, returning probabilities) to skip boilerplate before extraction. Measured against
the real corpus it doesn't pay off: Mammoth calls count against the same 1,000/day free quota,
the median PDF is 3 pages (so a gate call per document costs about as much as the extraction it
might save), and with a paid extractor at ~$0.001 per document the gate saves cents. It is
**not used for extraction**; news kinds come from CMS topics (no LLM).

Mammoth stays the tool for one job it fits: picking which candidate project an unlinked record
belongs to (§6, `choice` over the top candidates + "none"). Verified request shape:

```json
{"model": "baseten/mammoth-1:free", "state": "<text>",
 "questions": {"project": {"type": "choice", "instructions": "Which project is this about?",
                           "criteria": {"12": "Parkdale Hub, 1313 Queen St W", "none": "none"}}}}
```

## 3. Stage B: Extractor prompt

One call returns all three record types. That is cheaper than three calls, and the model sees the
full context once. Prompts are versioned files in `prompts/` (`extract_v1.md`), and the version is
part of the cache key.

### System prompt (`prompts/extract_v1.md`)

```
You extract structured, verifiable records about real-estate projects managed by CreateTO
(the City of Toronto's real estate agency) from a source document.

Rules:
1. Only extract what the text states. Never infer, estimate or use outside knowledge.
2. Every record MUST include "evidence": a verbatim quote (max 300 characters) copied exactly
   from the text, and "page": the page number from the nearest preceding "=== PAGE n ===" marker
   (null if the text has no markers).
3. Link records to a project only by its id from the CANDIDATE PROJECTS list. If the record is about
   a project not in the list, set project_id to null and fill project_hint with the name/address used.
4. Dates: return ISO format with a precision: "2024-03-15"/day, "2024-03"/month, "2024-Q2"/quarter,
   "2024"/year. Resolve relative dates ("next spring", "last month") using DOCUMENT DATE; if you
   cannot, omit the date. Mark whether the event is "planned" or "actual" as of DOCUMENT DATE.
5. Use the allowed vocabularies below. If nothing fits, use "other" and put a short label in "label".
6. If a section has nothing to extract, return an empty list. Do not pad.
7. Output only JSON matching the schema. No commentary.

FACT fields (vocabulary): residential_units, affordable_units, rental_units, ownership_units,
non_residential_gfa_sqm, total_gfa_sqm, height_storeys, site_area, parking_spaces,
childcare_spaces, park_area, budget, land_value, development_partner, non_profit_partner,
architect, consultant, zoning_status, official_plan_status, proposed_use, community_benefit,
heritage, sustainability_target, tenure, lease_term, other

MILESTONE types: council_approval, board_approval, community_meeting, public_consultation,
rezoning_application, rezoning_approved, site_plan_approval, market_offering_launched,
developer_selected, agreement_signed, land_transfer, demolition_start, construction_start,
construction_complete, occupancy, phase_change, report_published, other

PROCUREMENT kinds: rfp, rfq, rfei, eoi, rfsq, market_offering, prequalification,
award, contract, partner_selection, other
```

### User message

```
DOCUMENT: {title}
URL: {url}
DOCUMENT DATE: {published_or_fetched_date}
DOCUMENT TYPE: {news | pdf | external_page}
GATE HINT: {e.g. "schedule, procurement"}

CANDIDATE PROJECTS (id | name | address | ward | aliases):
{top ≤15 candidates from CMS relations + BM25/trigram match on the chunk}

TEXT:
{chunk text with === PAGE n === markers}
```

### Output schema (pydantic, validated; on failure retry once with the error message)

```jsonc
{
  "facts": [{
    "project_id": 12 | null, "project_hint": "string|null",
    "field": "residential_units", "label": null,
    "value_text": "approximately 800 new homes", "value_num": 800, "unit": "units",
    "as_of": "2024-06", "as_of_precision": "month",
    "evidence": "verbatim quote", "page": 3, "confidence": 0.0-1.0
  }],
  "milestones": [{
    "project_id": 12 | null, "project_hint": null,
    "type": "community_meeting", "label": null,
    "title": "Community Consultation Meeting #2",
    "date": "2021-11-18", "date_precision": "day", "end_date": null,
    "status": "actual | planned | cancelled | delayed",
    "evidence": "...", "page": 1, "confidence": 0.9
  }],
  "procurement_signals": [{
    "project_id": 12 | null, "project_hint": null,
    "kind": "market_offering",
    "title": "Parkdale Hub development partner market offering",
    "reference_number": "CRT-2024-07 | null",
    "issuer": "CreateTO", "counterparty": "selected developer, if any",
    "scope": "short description of goods/services/land sought",
    "value": 1234567.0, "currency": "CAD",
    "opens_at": "2024-05", "closes_at": "2024-07-15",
    "status": "upcoming | open | closed | awarded | cancelled | unknown",
    "external_url": "MERX link if present in text",
    "evidence": "...", "page": null, "confidence": 0.8
  }]
}
```

### Post-validation (code, not LLM)

1. Evidence check against source text (exact after normalization, fuzzy partial ratio ≥ 0.9,
   or all lines of the quote on one page). Fail → dropped, counted as `*_rejected_evidence`.
   Quotes read off an *image* can't be checked against a text layer: they're kept with
   `locator = 'vision'` and confidence capped at 0.5.
2. `page` must exist in the chunk, and the evidence must occur on that page. If it occurs on a
   different page, the page is corrected.
3. Vocabulary/enums enforced. Numbers parsed again from `value_text` as a cross-check.
4. `project_id` must be in the candidate list. Otherwise it is set to null and the record goes to
   linking (§6).
5. Dedupe: same `(project, field, value_num|value_text)` from multiple sources become
   **one fact with multiple `evidence` rows**. A different value for the same `(project, field)`
   is a separate fact; `Claims.resolve_facts()` picks the current one (PLAN.md §8). This is how
   corrections and amendments show up.

## 4. Deterministic sources of the same records (implemented: `parse/cms.py`)

These come for free and are preferred over LLM output:
- CMS project fields → `facts` (`extracted_by='cms'`, locator = JSON path such as `content.siteArea`)
- CMS `timeline` steps (43 projects) → `phase_period` milestones with parsed schedules
  ("2019 - 2021", "July 2026 - Q2 2028", "2024 - present"; placeholders kept undated)
- CMS `phase` → `phase_change` milestone when the phase differs from the previous run (dated by run)
- Project `phase = marketOffering` → procurement signal `market_offering`
- Project file titles naming an RFP/RFQ/REOI/market offering → procurement signal
- MERX public open-solicitations listing → `solicitation` signals, linked by address range
- News `relatedProject(s)` → `links` (51 of 136 articles), news topic → `news_articles.kind`
- Resource lists → `documents.title` + `has_file` links (so PDFs inherit their project
  deterministically; the LLM linker only handles what's left)

CMS gotchas handled in code:
- Relation fields embed whole copies of other stories, so walks never descend into embedded
  stories (`_is_story`).
- Child pages repeat their siblings' embedded blocks, so only pages without a `parentProject`
  own embedded blocks (`Page.own_blocks`).

## 5. PDFs

**Parser:** PyMuPDF (`pymupdf`). It is fast, gives page-accurate text, image counts, vector-drawing
counts and page rendering. `pymupdf4llm` gives markdown with tables if table structure matters.

**Per-page classification (code):**
- `text_chars ≥ 200` → text path (page text goes into the document window)
- `text_chars < 200` and (images or vector drawings present) → **vision path**
- `text_chars < 50` and no images/drawings → blank, skipped

**Vision path:** render the page to PNG at about 110 dpi (long side ≤ 1600 px), then send it to
`inkling:free` (accepts images) with the same extraction schema plus one extra output,
`page_description`: 2–4 sentences describing the diagram, site plan or board. The description is
stored as the page's text (`method='vision'`), so diagram pages become searchable in BM25/chat.
Caps: ≤ 15 vision pages per document and ≤ 300 per run (configurable). Pages with near-identical
image hashes (repeated boards) are deduplicated.

**"Can a model read the PDF natively?"** OpenRouter accepts PDFs as a `file` content part, with
these engines:
- `pdf-text`: free, text layer only (same as what we get from PyMuPDF)
- `mistral-ocr`: paid per page
- `native`: only for models that take files directly (Claude, Gemini, GPT-4o class), which
  internally use page images plus text

None of the free models list file input, so in practice "native" means screenshots either
way. Rendering pages ourselves keeps it free, gives exact page numbers for provenance, lets us
send only the pages that need vision, and lets us cache per page. `mistral-ocr` or a paid
native-PDF model stays available as a config switch for scanned documents (deferred).

## 6. Linking records to projects (implemented: `link/linker.py`, `createto link`)

Every claim carries `link_status` (`linked` / `organization` / `external` / `unresolved`),
`link_method` (`cms`, `extractor`, `alias_match`, `mammoth`) and `link_confidence`.

1. **At extraction:** the extractor gets up to 15 candidate projects (projects whose page lists
   the file, their parents, address and name matches in the text) and may only answer with one of
   their ids (`extractor`); otherwise its `project_hint` is matched deterministically against
   project names/addresses/aliases (`alias_match`).
2. **`createto link`** handles what's left. Records are grouped by (source document, hint);
   hint-less records are decided one by one. Candidates: projects linking the document (also via
   the news page it's attached to) and their parents, plus the top trigram/full-text matches on
   names and aliases (≤ 8). Mammoth answers a `choice` over the candidates plus `organization`
   (CreateTO itself: board, budget, policy) and `external` (a site/district/program that isn't a
   CreateTO project). Candidate labels include curated/learned aliases ("also known as").
3. **Acceptance:** a project at p ≥ 0.7, `organization`/`external` at p ≥ 0.5, else
   `unresolved`. 0.7 is calibrated on the first real pass: links at 0.6–0.7 were mostly wrong
   (the citywide "Housing Now" program → Bloor-Islington at 0.60).
4. **Re-keying:** a linked record is moved to the project, or merged into an identical existing
   claim (same fact value; matching milestone/signal) with its evidence moved over.
5. **Aliases:** hints linked at p ≥ 0.85 become `learned` aliases, except hints naming a
   program/plan/area (`NOT_AN_ALIAS`), which cover more than one project. Former names that no
   CMS text contains are listed in `config.yaml` → `curated_aliases` (e.g. Bloor-Kipling:
   "Westwood Theatre Lands", "Six Points").
6. **Re-runs:** decisions below 0.7 are reopened each run; unchanged questions are answered from
   the `extractions` cache (task `link`), so a stable run makes no calls.
7. **Derived links:** every claim linked by the extractor or Mammoth adds `document → project`
   and `news → project` (`mentions`, method `claims`) for its source, so board meetings and
   articles without CMS relations show up under the projects their minutes discuss.

Results (2026-10-27, 4 passes, ~520 free Mammoth requests, $0): of 3,081 model-extracted
records, 2,402 linked by the extractor, 83 by Mammoth, 140 `organization`, 387 `external`
(mostly legacy Build Toronto sites from 2010s annual reports and board minutes: 4050 Yonge,
Garrison Crossing, 10 York St, Corus Quay; and district-plan sites in the Port Lands framework),
69 `unresolved`. The API shows `external`/`unresolved` records from a project's own files as
`context_records` and organization records at `/organization`.

Free-model rate limit: 20 requests/min per key (OpenRouter `X-RateLimit-Limit`); 429s don't count
against the daily quota. `requests_per_minute` for `:free` models is 18.

## 7. Cost accounting (implemented)

Every request goes into `llm_calls` (model, endpoint, task, tokens in/out, latency, status,
`cost_usd` reported by OpenRouter, and `est_cost_usd` at the non-free list price of the same
model family, labelled as an estimate). Run stats roll these up.

## 8. Model choice and cost (measured 2026-10-27)

Full numbers: [reports/model_comparison.md](../reports/model_comparison.md) (`createto compare`).
Same 21-unit stratified sample (14 PDF text windows across 6 document types, 4 page images,
3 news articles), prompt `extract_v1`, nothing written to claims.

| | `openai/gpt-6-luna` | `thinkingmachines/inkling` (paid) |
|---|---|---|
| Valid answers | 21/21 (JSON schema enforced) | 17/21: every failure hit the 8k output cap after 6–8k reasoning tokens |
| Facts / milestones / signals accepted | 93 / 30 / 10 | 84 / 41 / 3 |
| Quote check pass rate | 100% | 96% |
| Reasoning share of output | 43% | 79% |
| Latency p50 | 9.5 s | 64 s |
| Billed, whole comparison incl. retries | $0.02 | $0.78 |
| Projected full first run (840 requests) | **≈ $0.78** | ≈ $18.8 (understated: truncations) |

Free tiers: `inkling:free` and `inkling-small:free` answer HTTP 403 "only available on agentic
harnesses", so no free extractor is usable from this service.

**Decision: `openai/gpt-6-luna`.** Recall is comparable: most "Inkling-only" facts are the same
facts filed under a different field (50/80 units as `other` vs Luna's `affordable_units`) or raw
financial-statement line items from 2011–2013 annual report images, which the prompt asks to
skip. Inkling found a few genuine extras (a consultant name, a $27M figure). Luna is 24x cheaper,
6x faster and never truncates.

**Full-run cost estimate (Luna, measured, not assumed):** fitted from the sample's billed tokens
(input = 1,586 + 0.241 tokens/char per text unit, least-squares over 14 units; 3,980 input
tokens per page image; output = sample mean per unit, reasoning included):

| scope | requests | cost |
|---|---|---|
| first run as configured (482 text windows, 298 page images, 60 news) | 840 | **$0.78** |
| + render all 437 capped first-party image pages | +437 | +$0.25 |
| + external decks' 384 image pages (deferred) | +384 | +$0.21 |
| every text unit at the sample's *max* output (pessimistic) | 840 | ~$1.40 |
| each re-run after a prompt change | 840 | +$0.78 |
| unchanged repeat run (cache) | 0 | $0.00 |

The run stops on its own at `llm.max_cost_usd_per_run` ($5) or `max_requests_per_run` (1,500).

### Full run results (2026-10-27, run 2)

1,269 units (482 PDF text windows, 727 page images, 60 news) in 3.8 min at concurrency 32;
0 errors, 0 invalid JSON. **Billed: $0.90** (Luna; estimate was $1.03), 5.4M tokens in,
0.91M out of which 0.46M reasoning. Accepted: 2,551 facts, 1,076 milestones, 135 procurement
signals; rejected by the quote check: 19 facts, 5 milestones. 585 records stay unlinked (no
candidate project, no matching hint) and are left for step 7.

Two rules added after looking at the results:
- **Multi-valued fields** (partners, architect, consultant, community benefit, heritage,
  sustainability target, `other`) and facts without a project are not reconciled against each
  other: every supported value stays `current` (`parse/claims.py::MULTI_VALUED`).
- **Signal status ages out**: a document says "open" only as of its own date. Model-extracted
  signals with a past closing date become `closed`; open/upcoming ones from documents older than
  ~6 months without a future closing date become `unknown` (`llm_extract.signal_status`). Only
  live sources (MERX listing, CMS phase) can keep a signal `open`: 5 today.

Known limits (deferred, PLAN.md §7): "latest document wins" can't tell a phase figure (Parkdale
West Block, 175 homes) from a site total (231); unit-mix breakdowns are sometimes filed as
`residential_units`; milestone `planned` is as of the document date.
