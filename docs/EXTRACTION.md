# Extraction Design: Facts, Milestones, Procurement Signals

Two kinds of extraction:

1. **Deterministic** (no LLM): CMS JSON from `__NEXT_DATA__`. This produces projects,
   news, initiatives, relations, and *CMS facts* (one fact per populated field, locator = JSON path).
2. **LLM**: unstructured text (news bodies, PDF pages, 1-hop external pages). This produces
   *document facts*, milestones and procurement signals, each with a verbatim evidence quote.

The core rule: **no claim enters the database without evidence we can check by code.** The model
must return a verbatim quote. The validator confirms the quote appears in the source text
(whitespace/quote normalized, fuzzy ratio ≥ 0.9) and rejects the claim otherwise.

---

## 1. Units of work ("chunks")

| Source | Chunk | Notes |
|---|---|---|
| News article | whole body | usually < 3k tokens |
| PDF, text layer | the **whole document**, with `=== PAGE n ===` markers, split into windows of ≤ 40 pages | one call per window instead of per page; this keeps us inside 1,000 free requests/day. The page markers give page-level citations |
| PDF page, low text | rendered PNG of one page | see §5 |
| External 1-hop page | main-content text | |

Each chunk has `input_hash = sha256(text or png bytes)`. That hash, plus `task`, `prompt_version`
and `model`, is the cache key in `extractions`.

## 2. Stage A: Gate (Mammoth)

`baseten/mammoth-1:free` is a *decisions* model: `POST https://openrouter.ai/api/alpha/decisions`
with a `state` (text) and typed `questions` (`choice` / `score` / `noul` / `box`). It returns
probabilities, not prose. It costs about 250 input tokens and $0, so it is used to avoid running
the expensive extractor on boilerplate.

Verified request shape:

```json
{
  "model": "baseten/mammoth-1:free",
  "state": "<doc title>\n<first ~2,000 chars of chunk>",
  "questions": {
    "content_kind": {
      "type": "choice",
      "instructions": "What is this document section mainly about?",
      "criteria": {
        "project_specifics": "details of a specific site or development: units, uses, size, design, partners, approvals",
        "schedule": "dates, phases, timelines, meetings, upcoming or past project events",
        "procurement": "a tender, RFP, RFQ, RFEI, EOI, market offering, bid, award, contract or developer/partner selection",
        "corporate": "CreateTO corporate matters: board, finances, strategy, staff",
        "boilerplate": "cover pages, tables of contents, legal text, contact info, image credits, blank or decorative"
      }
    },
    "has_dates":  {"type": "noul", "instructions": "Does the text contain specific dates or time periods for events?"},
    "has_money":  {"type": "noul", "instructions": "Does the text mention dollar amounts, budgets or contract values?"}
  }
}
```

Routing:
- `boilerplate` ≥ 0.7 → skip (record `gate=skipped` in `extractions`)
- otherwise → extractor, with the predicted kinds passed in as a hint
- `procurement` ≥ 0.5 **or** `has_money` ≥ 0.6 → procurement section of the prompt marked mandatory

Mammoth is also used in **linking** (§6) and to **classify news** (`news_kind`: announcement /
event / board meeting / press coverage / procurement / report).

## 3. Stage B: Extractor prompt (Inkling)

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

1. Evidence check against source text (fuzzy ≥ 0.9). Fail → dropped, counted in run stats.
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

## 6. Linking records to projects

In order, stopping at the first that applies:
1. CMS relation (news `relatedProjects`, PDF found in a project page's blocks/resources) → that project
2. The extractor returned a valid `project_id`
3. Exact or trigram match of `project_hint` against project names, aliases (slug, address,
   major intersection)
4. Mammoth `choice` question over the top-5 BM25 candidates plus `"none"`; accepted if p ≥ 0.7
5. Otherwise unlinked (kept, with `project_hint`, visible in `/unlinked` for review)

Every link stores `method` and `confidence`.

## 7. Cost accounting

Every request goes into `llm_calls` (model, endpoint, task, tokens in/out, latency, status,
`cost_usd` reported by OpenRouter, and `est_cost_usd` at the non-free list price of the same
model family, labelled as an estimate). Run stats roll these up.
