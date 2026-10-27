"""Link unresolved claims to projects (PLAN.md step 7; docs/EXTRACTION.md §6).

Records whose extractor couldn't name a candidate project are grouped by (source document,
project hint); hint-less records are decided one by one. For each group Mammoth picks one of:
a candidate CreateTO project, `organization` (CreateTO itself: board, budget, policy) or
`external` (a site/district that isn't a CreateTO project). Low-confidence answers stay
`unresolved`. Linked records are re-keyed through the claims layer (merging with an existing
identical claim), and document/news → project `mentions` links are derived from them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from createto_info.config import Config
from createto_info.db.models import (
    Evidence,
    Extraction,
    Fact,
    Link,
    Milestone,
    ProcurementSignal,
    ProjectAlias,
)
from createto_info.llm.openrouter import BudgetExceeded, LLMError, OpenRouter
from createto_info.parse.claims import Claims, Source

PROMPT_VERSION = "link_v1"
MAX_CANDIDATES = 8
# calibrated on the first real pass: links in 0.6-0.7 were mostly wrong ("Housing Now" program
# -> Bloor-Islington at 0.60), links >= 0.7 held up on review
ACCEPT_PROJECT = 0.7
ACCEPT_OTHER = 0.5
LEARN_ALIAS_AT = 0.85  # a hint linked this confidently becomes a project alias
# hints naming a program, plan or area cover more than one project: never learn them as aliases
NOT_AN_ALIAS = re.compile(r"\b(initiative|program(me)?|plan|framework|study|strategy|area|"
                          r"precinct|secondary|policy|portfolio)\b", re.I)
MODELS = {"fact": Fact, "milestone": Milestone, "procurement_signal": ProcurementSignal}

Decide = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Item:
    document_id: int
    title: str
    hint: str | None
    records: list[tuple[str, int]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # "<what>: <value> — "<quote>""
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def state(self) -> str:
        shown = "\n".join(f"- {line}" for line in self.lines[:3])
        return (f"Source document: {self.title}\n"
                f"Project or site named in the text: {self.hint or 'not stated'}\n"
                f"Records extracted from it:\n{shown}")

    def questions(self) -> dict[str, Any]:
        criteria = {f"p{c['id']}": c["label"] for c in self.candidates}
        criteria["organization"] = ("CreateTO or Build Toronto as an organization: board, "
                                    "budget, finances, policy, staff, corporate plans")
        criteria["external"] = ("a site, district, program or project that is not one of the "
                                "listed CreateTO projects (e.g. another agency's site)")
        return {"subject": {"type": "choice",
                            "instructions": "Which CreateTO project are these records about?",
                            "criteria": criteria}}

    def key(self) -> str:
        blob = json.dumps([PROMPT_VERSION, self.state(), self.questions()], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


# ------------------------------------------------------------------ items

def build_items(s: Session) -> list[Item]:
    rows = s.execute(text("""
        WITH u AS (
          SELECT 'fact' t, id, project_hint h, field || ': ' || coalesce(value_text, '') what
            FROM facts WHERE project_id IS NULL AND link_status = 'unresolved'
          UNION ALL SELECT 'milestone', id, project_hint, type || ': ' || coalesce(title, '')
            FROM milestones WHERE project_id IS NULL AND link_status = 'unresolved'
          UNION ALL SELECT 'procurement_signal', id, project_hint, kind || ': ' || title
            FROM procurement_signals WHERE project_id IS NULL AND link_status = 'unresolved')
        SELECT DISTINCT ON (u.t, u.id) u.t, u.id, u.h, u.what, e.excerpt,
               v.document_id, coalesce(d.title, regexp_replace(d.url, '^.*/', '')) AS title
        FROM u JOIN evidence e ON e.entity_type = u.t AND e.entity_id = u.id
        JOIN document_versions v ON v.id = e.version_id JOIN documents d ON d.id = v.document_id
        ORDER BY u.t, u.id, e.id""")).mappings().all()
    items: dict[tuple, Item] = {}
    for r in rows:
        hint = (r["h"] or "").strip() or None
        key = (r["document_id"], hint.lower()) if hint else (r["t"], r["id"])
        item = items.setdefault(key, Item(r["document_id"], r["title"], hint))
        item.records.append((r["t"], r["id"]))
        item.lines.append(f'{r["what"][:120]} — "{(r["excerpt"] or "")[:200]}"')
    out = list(items.values())
    for item in out:
        item.candidates = candidates(s, item)
    return out


def candidates(s: Session, item: Item) -> list[dict[str, Any]]:
    """Projects that link to the source document (and their parents) + best text matches."""
    linked = s.execute(text("""
        WITH direct AS (
          SELECT l.from_id AS pid FROM links l
          WHERE l.rel = 'has_file' AND l.from_type = 'project' AND l.to_id = :doc
          UNION SELECT l2.to_id FROM links l JOIN links l2
            ON l2.from_type = 'news' AND l2.from_id = l.from_id AND l2.to_type = 'project'
          WHERE l.rel = 'has_file' AND l.from_type = 'news' AND l.to_id = :doc)
        SELECT pid FROM direct
        UNION SELECT p.parent_project_id FROM projects p JOIN direct ON direct.pid = p.id
        WHERE p.parent_project_id IS NOT NULL"""), {"doc": item.document_id}).scalars().all()
    query = item.hint or " ".join(line.split(" — ")[0] for line in item.lines[:2])
    matched = s.execute(text("""
        SELECT p.id FROM projects p
        ORDER BY greatest(word_similarity(:q, p.name), coalesce((SELECT max(word_similarity(
                   :q, a.alias)) FROM project_aliases a WHERE a.project_id = p.id), 0))
               + ts_rank_cd(p.search_tsv,
                            replace(plainto_tsquery('english', :q)::text, ' & ', ' | ')::tsquery)
                 DESC
        LIMIT 5"""), {"q": query}).scalars().all()
    ids: list[int] = []
    for pid in [*linked, *matched]:
        if pid not in ids:
            ids.append(pid)
    rows = s.execute(text("""
        SELECT p.id, p.name, p.address, p.ward, left(coalesce(p.summary, ''), 160) AS summary,
               (SELECT string_agg(a.alias, '; ') FROM project_aliases a
                WHERE a.project_id = p.id AND a.kind IN ('curated', 'learned')) AS aka
        FROM projects p WHERE p.id = ANY(:ids)"""), {"ids": ids[:MAX_CANDIDATES]}).mappings()
    by_id = {r["id"]: r for r in rows}

    def label(r: Any) -> str:
        head = ", ".join(x for x in (r["name"], r["address"], r["ward"]) if x)
        aka = f" (also known as: {r['aka'][:160]})" if r["aka"] else ""
        return head + aka + (f". {r['summary']}" if r["summary"] else "")

    return [{"id": i, "label": label(by_id[i])} for i in ids[:MAX_CANDIDATES] if i in by_id]


# --------------------------------------------------------------- decisions

def interpret(answer: dict[str, Any]) -> tuple[str, int | None, float]:
    """(link_status, project_id, probability) from a Mammoth choice answer."""
    probs = answer.get("probabilities") or {}
    choice = answer.get("choice") or (max(probs, key=probs.get) if probs else None)
    p = float(probs.get(choice, answer.get("confidence") or 0.0))
    if choice and choice.startswith("p") and choice[1:].isdigit() and p >= ACCEPT_PROJECT:
        return "linked", int(choice[1:]), p
    if choice in ("organization", "external") and p >= ACCEPT_OTHER:
        return choice, None, p
    return "unresolved", None, p


async def decide_all(s: Session, items: list[Item], model: str, decide: Decide,
                     stats: Counter[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    async def one(item: Item) -> None:
        key = item.key()
        cached = s.scalar(select(Extraction).where(
            Extraction.input_hash == key, Extraction.task == "link",
            Extraction.prompt_version == PROMPT_VERSION, Extraction.model == model,
            Extraction.status == "ok"))
        if cached is not None:
            stats["cached"] += 1
            out[key] = cached.output
            return
        try:
            res = await decide(item.state(), item.questions())
        except LLMError:
            stats["decision_errors"] += 1
            return
        answer = res["answers"]["subject"]
        s.add(Extraction(input_hash=key, task="link", prompt_version=PROMPT_VERSION,
                         model=model, output=answer, status="ok",
                         llm_call_id=res.get("call_id")))
        s.commit()
        stats["decisions"] += 1
        out[key] = answer

    tasks = [asyncio.create_task(one(i)) for i in items]
    try:
        for t in tasks:
            await t
    except BudgetExceeded:
        stats["budget_stop"] += 1
        for t in tasks:
            t.cancel()
    return out


# ------------------------------------------------------------------- apply

def relink(s: Session, claims: Claims, entity_type: str, record_id: int, project_id: int,
           method: str, confidence: float) -> str:
    """Attach a record to a project, merging into an identical existing claim if there is one.
    Returns 'moved' or 'merged'."""
    model = MODELS[entity_type]
    row = s.get(model, record_id)
    if entity_type == "fact":
        target = s.scalar(select(Fact).where(
            Fact.project_id == project_id, Fact.field == row.field,
            Fact.value_key == row.value_key))
    elif entity_type == "milestone":
        target = claims._match_milestone(project_id, None, row.type, None, row.title, row.date,
                                         row.date_precision, row.end_date)
    else:
        target = claims._match_signal(project_id, None, row.kind, row.title,
                                      row.reference_number, row.opens_at, row.closes_at)
    if target is None or target.id == row.id:
        row.project_id = project_id
        row.link_status, row.link_method, row.link_confidence = "linked", method, confidence
        return "moved"
    if entity_type != "fact":
        fields = {k: getattr(row, k) for k in (
            ("title", "date", "date_precision", "end_date", "status") if entity_type ==
            "milestone" else ("title", "status", "reference_number", "issuer", "counterparty",
                              "scope", "value", "currency", "opens_at", "closes_at",
                              "external_url"))}
        claims._merge(target, fields, Source(0, row.extracted_by, confidence=row.confidence))
    for ev in s.scalars(select(Evidence).where(Evidence.entity_type == entity_type,
                                               Evidence.entity_id == row.id)):
        dup = s.scalar(select(Evidence.id).where(
            Evidence.entity_type == entity_type, Evidence.entity_id == target.id,
            Evidence.version_id == ev.version_id,
            Evidence.page_no.is_not_distinct_from(ev.page_no),
            Evidence.locator.is_not_distinct_from(ev.locator)))
        if dup:
            s.delete(ev)
        else:
            ev.entity_id = target.id
    s.flush()
    if entity_type == "fact":
        s.execute(update(Fact).where(Fact.superseded_by == row.id).values(superseded_by=None))
    s.delete(row)
    s.flush()
    return "merged"


def derive_mentions(s: Session, run_id: int) -> int:
    """document → project and news → project `mentions` from linked claims' evidence."""
    rows = s.execute(text("""
        WITH c AS (
          SELECT 'fact' t, id, project_id, link_confidence FROM facts
            WHERE project_id IS NOT NULL AND link_method IN ('extractor', 'alias_match', 'mammoth')
          UNION ALL SELECT 'milestone', id, project_id, link_confidence FROM milestones
            WHERE project_id IS NOT NULL AND link_method IN ('extractor', 'alias_match', 'mammoth')
          UNION ALL SELECT 'procurement_signal', id, project_id, link_confidence
            FROM procurement_signals
            WHERE project_id IS NOT NULL AND link_method IN ('extractor', 'alias_match', 'mammoth'))
        SELECT v.document_id, c.project_id, max(coalesce(c.link_confidence, 1)) conf
        FROM c JOIN evidence e ON e.entity_type = c.t AND e.entity_id = c.id
        JOIN document_versions v ON v.id = e.version_id
        GROUP BY 1, 2""")).all()
    news_by_doc: dict[int, set[int]] = defaultdict(set)
    for doc_id, news_id in s.execute(text("""
            SELECT d.id, n.id FROM news_articles n JOIN documents d ON d.url = n.url
            UNION SELECT l.to_id, l.from_id FROM links l
            WHERE l.rel = 'has_file' AND l.from_type = 'news'""")):
        news_by_doc[doc_id].add(news_id)
    n = 0
    for doc_id, project_id, conf in rows:
        edges = [("document", doc_id)] + [("news", nid) for nid in news_by_doc.get(doc_id, ())]
        for from_type, from_id in edges:
            s.execute(insert(Link).values(
                from_type=from_type, from_id=from_id, to_type="project", to_id=project_id,
                rel="mentions", method="claims", confidence=float(conf), run_id=run_id,
            ).on_conflict_do_nothing())
            n += 1
    return n


def learn_aliases(s: Session) -> int:
    """Hints Mammoth linked confidently ("Former Westwood Theatre site (Six Points)" ->
    Bloor-Kipling) become aliases, so later candidate searches find the project."""
    before = s.scalar(select(func.count()).select_from(ProjectAlias)) or 0
    for table in ("facts", "milestones", "procurement_signals"):
        rows = s.execute(text(f"""
            SELECT DISTINCT project_id, project_hint FROM {table}
            WHERE link_method = 'mammoth' AND link_status = 'linked'
              AND link_confidence >= :c AND project_hint IS NOT NULL"""),
            {"c": LEARN_ALIAS_AT}).all()
        for project_id, hint in rows:
            if NOT_AN_ALIAS.search(hint):
                continue
            s.execute(insert(ProjectAlias).values(
                project_id=project_id, alias=hint.strip()[:200], kind="learned",
            ).on_conflict_do_nothing())
    return (s.scalar(select(func.count()).select_from(ProjectAlias)) or 0) - before


def reopen_uncertain(s: Session) -> int:
    """Mammoth decisions below the acceptance threshold go back to `unresolved` so the next
    pass can decide them with better candidates (cached when nothing changed)."""
    n = 0
    for model in MODELS.values():
        res = s.execute(update(model).where(
            model.link_method == "mammoth", model.link_confidence < ACCEPT_PROJECT,
            model.link_status != "unresolved").values(
            link_status="unresolved", project_id=None))
        n += res.rowcount or 0
    return n


def run_linking(s: Session, config: Config, run_id: int,
                decide: Decide | None = None) -> Counter[str]:
    stats: Counter[str] = Counter()
    claims = Claims(s, run_id)
    model = config.models.gate
    stats["aliases_learned_before"] = learn_aliases(s)
    stats["reopened"] = reopen_uncertain(s)
    s.flush()
    items = build_items(s)
    stats["items"] = len(items)
    stats["records"] = sum(len(i.records) for i in items)

    async def go() -> dict[str, dict[str, Any]]:
        if decide is not None:
            return await decide_all(s, items, model, decide, stats)
        client = OpenRouter(s, config, run_id)
        try:
            async def call(state: str, questions: dict[str, Any]) -> dict[str, Any]:
                return await client.decide(model=model, state=state, questions=questions,
                                           task="link")
            return await decide_all(s, items, model, call, stats)
        finally:
            await client.aclose()

    answers = asyncio.run(go())
    for item in items:
        answer = answers.get(item.key())
        if answer is None:
            continue
        status, project_id, p = interpret(answer)
        for entity_type, record_id in item.records:
            if status == "linked":
                stats[relink(s, claims, entity_type, record_id, project_id, "mammoth", p)] += 1
            else:
                s.execute(update(MODELS[entity_type]).where(
                    MODELS[entity_type].id == record_id).values(
                    link_status=status, link_method="mammoth", link_confidence=p))
            stats[f"records_{status}"] += 1
    s.flush()
    for k, v in claims.resolve_facts().items():
        stats[k] += v
    s.execute(delete(Link).where(Link.rel == "mentions", Link.method == "claims"))
    stats["mention_links"] = derive_mentions(s, run_id)
    stats["aliases_learned_after"] = learn_aliases(s)
    s.flush()
    return stats
