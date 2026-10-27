"""LLM extraction of facts, milestones and procurement signals (docs/EXTRACTION.md).

Phase 1 (async): every unit of work (PDF text window, rendered PDF page, news body) gets an
output, from the `extractions` cache or from the model. Phase 2 (sync): each record's verbatim
evidence is checked against the source text, then accepted records are written through the
claims layer (`parse/claims.py`). `dry_run` stops after measuring (model comparison).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from createto_info.config import REPO_ROOT, Config
from createto_info.db.models import Extraction, NewsArticle, PdfPage
from createto_info.llm.openrouter import (
    BudgetExceeded,
    LLMError,
    OpenRouter,
    image_part,
    parse_json,
)
from createto_info.parse.claims import Claims, Source
from createto_info.parse.cms import ProjectMatcher

PROMPT_VERSION = "extract_v1"
PROMPT = (REPO_ROOT / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
MAX_CANDIDATES = 15

FACT_FIELDS = {
    "residential_units", "affordable_units", "rental_units", "ownership_units",
    "non_residential_gfa_sqm", "total_gfa_sqm", "height_storeys", "site_area", "parking_spaces",
    "childcare_spaces", "park_area", "budget", "land_value", "development_partner",
    "non_profit_partner", "architect", "consultant", "zoning_status", "official_plan_status",
    "proposed_use", "community_benefit", "heritage", "sustainability_target", "tenure",
    "lease_term", "other"}
MILESTONE_TYPES = {
    "council_approval", "board_approval", "community_meeting", "public_consultation",
    "rezoning_application", "rezoning_approved", "site_plan_approval",
    "market_offering_launched", "developer_selected", "agreement_signed", "land_transfer",
    "demolition_start", "construction_start", "construction_complete", "occupancy",
    "phase_change", "report_published", "other"}
SIGNAL_KINDS = {"rfp", "rfq", "rfei", "eoi", "rfsq", "market_offering", "prequalification",
                "award", "contract", "partner_selection", "other"}
SIGNAL_STATUS = {"upcoming", "open", "closed", "awarded", "cancelled", "unknown"}
MILESTONE_STATUS = {"actual", "planned", "cancelled", "delayed"}


# ------------------------------------------------------------------ schema

def _nullable(t: str) -> dict[str, Any]:
    return {"type": [t, "null"]}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": list(props),
            "properties": props}


_COMMON = {"project_id": _nullable("integer"), "project_hint": _nullable("string")}
_TAIL = {"evidence": {"type": "string"}, "page": _nullable("integer"),
         "confidence": {"type": "number"}}
SCHEMA = _obj({
    "facts": {"type": "array", "items": _obj({
        **_COMMON, "field": {"type": "string", "enum": sorted(FACT_FIELDS)},
        "label": _nullable("string"), "value_text": {"type": "string"},
        "value_num": _nullable("number"), "unit": _nullable("string"),
        "as_of": _nullable("string"), **_TAIL})},
    "milestones": {"type": "array", "items": _obj({
        **_COMMON, "type": {"type": "string", "enum": sorted(MILESTONE_TYPES)},
        "label": _nullable("string"), "title": {"type": "string"}, "date": _nullable("string"),
        "end_date": _nullable("string"),
        "status": {"type": "string", "enum": sorted(MILESTONE_STATUS)}, **_TAIL})},
    "procurement_signals": {"type": "array", "items": _obj({
        **_COMMON, "kind": {"type": "string", "enum": sorted(SIGNAL_KINDS)},
        "title": {"type": "string"}, "reference_number": _nullable("string"),
        "issuer": _nullable("string"), "counterparty": _nullable("string"),
        "scope": _nullable("string"), "value": _nullable("number"),
        "currency": _nullable("string"), "opens_at": _nullable("string"),
        "closes_at": _nullable("string"),
        "status": {"type": "string", "enum": sorted(SIGNAL_STATUS)},
        "external_url": _nullable("string"), **_TAIL})},
    "page_description": _nullable("string"),
})


# ------------------------------------------------------------------- units

@dataclass
class Unit:
    kind: str  # pdf_text | pdf_vision | news
    version_id: int
    title: str
    url: str
    doc_date: date | None
    pages: list[tuple[int | None, str]] = field(default_factory=list)  # (page_no, text)
    image: Path | None = None
    image_hash: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def task(self) -> str:
        return "vision_extract" if self.kind == "pdf_vision" else "extract"

    @property
    def page_from(self) -> int | None:
        return self.pages[0][0] if self.pages else None

    @property
    def page_to(self) -> int | None:
        return self.pages[-1][0] if self.pages else None

    def body(self) -> str:
        if self.kind == "news":
            return self.pages[0][1]
        return "\n\n".join(f"=== PAGE {n} ===\n{t}" for n, t in self.pages)

    def header(self) -> str:
        cands = "\n".join(
            f"{c['id']} | {c['name']} | {c.get('address') or ''} | {c.get('ward') or ''}"
            for c in self.candidates) or "(none)"
        kind = {"pdf_text": "pdf", "pdf_vision": "pdf page image", "news": "news"}[self.kind]
        return (f"DOCUMENT: {self.title}\nURL: {self.url}\n"
                f"DOCUMENT DATE: {self.doc_date or 'unknown'}\nDOCUMENT TYPE: {kind}\n\n"
                f"CANDIDATE PROJECTS (id | name | address | ward):\n{cands}\n")

    def messages(self) -> list[dict[str, Any]]:
        if self.kind == "pdf_vision":
            note = (f"{self.header()}\nIMAGE: page {self.page_from} of the document. Quote "
                    f"evidence from text visible in the image; small extracted text layer:\n"
                    f"{self.pages[0][1] or '(none)'}")
            content: Any = [{"type": "text", "text": note}, image_part(self.image)]
        else:
            content = f"{self.header()}\nTEXT:\n{self.body()}"
        return [{"role": "system", "content": PROMPT}, {"role": "user", "content": content}]

    def input_hash(self) -> str:
        h = hashlib.sha256()
        for part in (PROMPT_VERSION, self.kind, self.header(),
                     self.image_hash or self.body()):
            h.update(part.encode())
            h.update(b"\x00")
        return h.hexdigest()


def build_units(s: Session, config: Config, kinds: set[str] | None = None) -> list[Unit]:
    llm = config.llm
    kinds = kinds or {"pdf_text", "pdf_vision", "news"}
    matcher = ProjectMatcher(s)
    projects = {r["id"]: dict(r) for r in s.execute(text(
        "SELECT id, name, address, ward, parent_project_id FROM projects")).mappings()}
    names = sorted(((p["name"].lower(), pid) for pid, p in projects.items()
                    if len(p["name"]) >= 6), key=lambda x: -len(x[0]))

    def candidates(linked: list[int], sample: str) -> list[dict[str, Any]]:
        ids: list[int] = []
        for pid in linked:
            for x in (pid, projects.get(pid, {}).get("parent_project_id")):
                if x and x not in ids:
                    ids.append(x)
        low = sample.lower()
        for pid in sorted(matcher.address_matches(sample)) + [
                pid for name, pid in names if name in low]:
            if pid not in ids:
                ids.append(pid)
        return [projects[i] for i in ids[:MAX_CANDIDATES]]

    file_links: dict[int, list[int]] = {}
    for doc_id, pid in s.execute(text(
            "SELECT to_id, from_id FROM links WHERE rel = 'has_file' AND from_type = 'project'")):
        file_links.setdefault(doc_id, []).append(pid)

    units: list[Unit] = []
    seen_content: set[str] = set()
    docs = s.execute(text("""
        SELECT d.id AS doc_id, d.url, d.title, v.id AS version_id, v.content_hash, v.doc_date
        FROM documents d JOIN document_versions v ON v.id = d.current_version_id
        WHERE d.kind = 'pdf' AND d.removed_at_run IS NULL AND v.parse_status = 'ok'
        ORDER BY d.id""")).mappings().all()
    for d in docs:
        if d["content_hash"] in seen_content:  # same file at two URLs
            continue
        seen_content.add(d["content_hash"])
        pages = list(s.execute(select(PdfPage).where(PdfPage.version_id == d["version_id"])
                               .order_by(PdfPage.page_no)).scalars())
        text_pages = [(p.page_no, p.text) for p in pages if p.method == "text"]
        sample = "\n".join(t for _, t in text_pages[:5]) + " " + (d["title"] or "")
        cands = candidates(file_links.get(d["doc_id"], []), sample)
        title = d["title"] or d["url"].rsplit("/", 1)[-1]
        if "pdf_text" in kinds:
            w = llm.pdf_window_pages
            for i in range(0, len(text_pages), w):
                units.append(Unit("pdf_text", d["version_id"], title, d["url"], d["doc_date"],
                                  pages=text_pages[i:i + w], candidates=cands))
        if "pdf_vision" in kinds:
            for p in pages:
                if p.method == "vision" and p.render_path:
                    units.append(Unit("pdf_vision", d["version_id"], title, d["url"],
                                      d["doc_date"], pages=[(p.page_no, p.text or "")],
                                      image=config.storage.renders_dir / p.render_path,
                                      image_hash=p.render_hash, candidates=cands))
    if "news" in kinds:
        news_links: dict[int, list[int]] = {}
        for nid, pid in s.execute(text(
                "SELECT from_id, to_id FROM links WHERE from_type = 'news' "
                "AND to_type = 'project'")):
            news_links.setdefault(nid, []).append(pid)
        for n in s.scalars(select(NewsArticle).order_by(NewsArticle.id)):
            body = n.body_text or ""
            if len(body) < llm.news_min_chars or n.source_version_id is None:
                continue
            units.append(Unit("news", n.source_version_id, n.title, n.url,
                              n.published_at.date() if n.published_at else None,
                              pages=[(None, body)],
                              candidates=candidates(news_links.get(n.id, []),
                                                    n.title + " " + body[:4000])))
    return units


# ---------------------------------------------------------------- phase 1

async def _run_unit(client: OpenRouter, s: Session, unit: Unit, model: str, run_id: int | None,
                    stats: Counter[str]) -> Extraction | None:
    key = unit.input_hash()
    cached = s.scalar(select(Extraction).where(
        Extraction.input_hash == key, Extraction.task == unit.task,
        Extraction.prompt_version == PROMPT_VERSION, Extraction.model == model,
        Extraction.status == "ok"))
    if cached is not None:
        stats["cached"] += 1
        return cached
    try:
        res = await client.chat(model=model, messages=unit.messages(), task=unit.task,
                                schema=SCHEMA)
    except BudgetExceeded:
        raise
    except LLMError as exc:
        stats["unit_errors"] += 1
        stats[f"error:{str(exc)[:40]}"] += 1
        return None
    status, output = "ok", None
    try:
        output = parse_json(res.content)
        if not isinstance(output, dict):
            raise ValueError("not an object")
    except (ValueError, json.JSONDecodeError):
        status = "invalid_json"
        output = {"raw": res.content[:20000]}
        stats["invalid_json"] += 1
    row = Extraction(input_hash=key, task=unit.task, prompt_version=PROMPT_VERSION, model=model,
                     version_id=unit.version_id, page_from=unit.page_from,
                     page_to=unit.page_to, output=output, status=status,
                     created_run=run_id, llm_call_id=res.call_id)
    existing = s.scalar(select(Extraction).where(
        Extraction.input_hash == key, Extraction.task == unit.task,
        Extraction.prompt_version == PROMPT_VERSION, Extraction.model == model))
    if existing is not None:  # replace a previous invalid output
        for k in ("output", "status", "created_run", "llm_call_id"):
            setattr(existing, k, getattr(row, k))
        row = existing
    else:
        s.add(row)
    s.commit()
    stats["llm_units"] += 1
    return row if status == "ok" else None


async def fetch_outputs(s: Session, config: Config, units: list[Unit], model: str,
                        run_id: int | None, client: OpenRouter | None = None,
                        ) -> tuple[list[tuple[Unit, Extraction | None]], Counter[str]]:
    stats: Counter[str] = Counter()
    own = client is None
    if own:
        client = OpenRouter(s, config, run_id, await OpenRouter.structured_capable(config))
    try:
        results = []
        tasks = [asyncio.create_task(_run_unit(client, s, u, model, run_id, stats))
                 for u in units]
        _report_progress(tasks, client, stats)
        for u, t in zip(units, tasks, strict=True):
            try:
                results.append((u, await t))
            except BudgetExceeded as exc:
                stats["budget_stop"] += 1
                stats[f"budget:{exc}"] = 1
                for other in tasks:
                    other.cancel()
                results.append((u, None))
                break
    finally:
        if own:
            await client.aclose()
    return results, stats


def _report_progress(tasks: list[asyncio.Task], client: OpenRouter, stats: Counter[str],
                     every: int = 50) -> None:
    """Print a progress line every `every` finished units (stdout, flushed for log files)."""
    total, start = len(tasks), time.monotonic()
    done = 0

    def on_done(_: asyncio.Task) -> None:
        nonlocal done
        done += 1
        if done % every and done != total:
            return
        elapsed = time.monotonic() - start
        eta = elapsed / done * (total - done)
        print(f"[extract] {done}/{total} units | {stats['cached']} cached | "
              f"{stats['llm_units']} new | {stats['unit_errors']} errors | "
              f"{stats['invalid_json']} invalid | billed ${client.spent:.4f} | "
              f"{elapsed / 60:.1f} min elapsed, ~{eta / 60:.1f} min left", flush=True)

    for t in tasks:
        t.add_done_callback(on_done)


# ---------------------------------------------------------------- phase 2

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         "­": "", " ": " ", "ﬁ": "fi", "ﬂ": "fl"})


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").translate(_QUOTES).lower()).strip()


def verify(evidence: str, unit: Unit, page: int | None) -> int | None | bool:
    """Page number where the quote occurs (None for unpaged text), or False if not found.

    A quote matches if it occurs on one page (exactly after normalization, or with fuzzy
    partial ratio >= 90), or if it spans lines and *every* line occurs on the same page: PDF
    tables put a label ("TOTAL SITE AREA (ACRES)") and its value ("2.86 acres") apart in the
    text layer, though both are verbatim on the page.
    """
    ev = _norm(evidence)
    if len(ev) < 8:
        return False
    segments = [_norm(x) for x in re.split(r"[\r\n]+", evidence or "") if len(_norm(x)) >= 3]
    ordered = sorted(unit.pages, key=lambda p: p[0] != page)
    for page_no, body in ordered:
        hay = _norm(body)
        if not hay:
            continue
        if ev in hay or fuzz.partial_ratio(ev, hay) >= 90:
            return page_no
        if len(segments) >= 2 and all(seg in hay for seg in segments):
            return page_no
    return False


_ISO = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?|-Q([1-4]))?$")


def parse_when(value: str | None) -> tuple[date | None, str | None]:
    m = _ISO.match((value or "").strip())
    if not m:
        return None, None
    y = int(m.group(1))
    try:
        if m.group(4):
            return date(y, 3 * int(m.group(4)) - 2, 1), "quarter"
        if m.group(3):
            return date(y, int(m.group(2)), int(m.group(3))), "day"
        if m.group(2):
            return date(y, int(m.group(2)), 1), "month"
        return date(y, 1, 1), "year"
    except ValueError:
        return None, None


def apply_outputs(s: Session, results: list[tuple[Unit, Extraction | None]], model: str,
                  run_id: int, dry_run: bool) -> Counter[str]:
    stats: Counter[str] = Counter()
    claims = Claims(s, run_id)
    matcher = ProjectMatcher(s)
    by = f"model:{model}@{PROMPT_VERSION}"
    for unit, ext in results:
        if ext is None:
            continue
        out = ext.output or {}
        allowed = {c["id"] for c in unit.candidates}
        if unit.kind == "pdf_vision" and out.get("page_description") and not dry_run:
            page = s.get(PdfPage, (unit.version_id, unit.page_from))
            if page is not None:
                page.vision_description = str(out["page_description"])[:2000]
        for section, kind in (("facts", "fact"), ("milestones", "milestone"),
                              ("procurement_signals", "signal")):
            for rec in out.get(section) or []:
                if not isinstance(rec, dict):
                    continue
                stats[f"{kind}_proposed"] += 1
                where = verify(str(rec.get("evidence") or ""), unit, rec.get("page"))
                vision = unit.kind == "pdf_vision"
                if where is False and not vision:
                    stats[f"{kind}_rejected_evidence"] += 1
                    continue
                if where is False:  # quote read off an image: unverifiable against text
                    stats[f"{kind}_unverified_vision"] += 1
                    where = unit.page_from
                stats[f"{kind}_accepted"] += 1
                if dry_run:
                    continue
                _write(claims, matcher, unit, ext, rec, kind, where, allowed, by, vision, stats)
    if not dry_run:
        for status, n in claims.resolve_facts().items():
            stats[status] += n
        s.flush()
    return stats


def _write(claims: Claims, matcher: ProjectMatcher, unit: Unit, ext: Extraction,
           rec: dict[str, Any], kind: str, page: int | None, allowed: set[int], by: str,
           vision: bool, stats: Counter[str]) -> None:
    pid = rec.get("project_id") if rec.get("project_id") in allowed else None
    hint = rec.get("project_hint") or None
    if pid is None and hint:
        pid = matcher.match(hint)
        stats["linked_by_hint" if pid else "unlinked"] += 1
    conf = float(rec.get("confidence") or 0.5)
    if vision:
        conf = min(conf, 0.5)
    src = Source(unit.version_id, by, page_no=page, excerpt=str(rec.get("evidence"))[:300],
                 locator="vision" if vision else None, confidence=conf, extraction_id=ext.id)
    if kind == "fact":
        field_name = rec.get("field") if rec.get("field") in FACT_FIELDS else "other"
        as_of, prec = parse_when(rec.get("as_of"))
        if as_of is None and unit.doc_date:
            as_of, prec = unit.doc_date, "day"
        num = rec.get("value_num")
        claims.fact(project_id=pid, project_hint=None if pid else hint, field=field_name,
                    label=rec.get("label"), value_text=str(rec.get("value_text") or "")[:500],
                    value_num=float(num) if isinstance(num, int | float) else None,
                    unit=rec.get("unit"), as_of=as_of, as_of_precision=prec, source=src)
    elif kind == "milestone":
        d, prec = parse_when(rec.get("date"))
        end, _ = parse_when(rec.get("end_date"))
        mtype = rec.get("type") if rec.get("type") in MILESTONE_TYPES else "other"
        title = rec.get("title") or rec.get("label") or mtype
        claims.milestone(project_id=pid, project_hint=None if pid else hint, type=mtype,
                         title=str(title)[:300], date=d, date_precision=prec, end_date=end,
                         status=rec.get("status") if rec.get("status") in MILESTONE_STATUS
                         else "planned", source=src)
    else:
        skind = rec.get("kind") if rec.get("kind") in SIGNAL_KINDS else "other"
        value = rec.get("value")
        claims.signal(project_id=pid, project_hint=None if pid else hint, kind=skind,
                      title=str(rec.get("title") or skind)[:300],
                      status=rec.get("status") if rec.get("status") in SIGNAL_STATUS
                      else "unknown",
                      reference_number=rec.get("reference_number"), issuer=rec.get("issuer"),
                      counterparty=rec.get("counterparty"), scope=rec.get("scope"),
                      value=float(value) if isinstance(value, int | float) else None,
                      currency=rec.get("currency"), opens_at=rec.get("opens_at"),
                      closes_at=rec.get("closes_at"), external_url=rec.get("external_url"),
                      source=src)
