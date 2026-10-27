"""Claims and evidence: one write path for every source (CMS, rules, LLM). See PLAN.md §8.

Sources never overwrite each other's records. They *assert* a fact / milestone / procurement
signal with an evidence row; matching decides whether the claim joins an existing record, and
`resolve()` decides which fact values are current.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from createto_info.db.models import (
    Document,
    DocumentVersion,
    Evidence,
    Fact,
    Milestone,
    ProcurementSignal,
)
from createto_info.parse.text import DatePoint, period_end

TITLE_MATCH = 85  # rapidfuzz token_sort_ratio threshold for "same signal/milestone"


def rank(extracted_by: str | None) -> int:
    """Source precedence: cms > rule:* > model:* (PLAN.md §8)."""
    s = extracted_by or ""
    if s == "cms" or s.startswith("cms:"):
        return 3
    if s.startswith("rule:"):
        return 2
    if s.startswith("model:"):
        return 1
    return 0


def value_key(value_text: str | None, value_num: float | None, unit: str | None) -> str:
    """Normalized identity of a fact value. Deliberately simple: no unit conversion or
    approximation handling (deferred, PLAN.md §7)."""
    if value_num is not None:
        return f"{value_num:g} {(unit or '').strip().lower()}".strip()
    text = re.sub(r"\s+", " ", (value_text or "").strip().lower())
    return text.rstrip(".;,")


@dataclass(frozen=True)
class Source:
    version_id: int
    extracted_by: str
    locator: str | None = None
    page_no: int | None = None
    excerpt: str | None = None
    confidence: float = 1.0
    extraction_id: int | None = None


class Claims:
    def __init__(self, session: Session, run_id: int):
        self.s = session
        self.run_id = run_id
        self.asserted: dict[str, set[int]] = defaultdict(set)  # entity type -> ids this pass

    # ------------------------------------------------------------ evidence

    def _evidence(self, entity_type: str, entity_id: int, src: Source) -> None:
        self.s.execute(insert(Evidence).values(
            entity_type=entity_type, entity_id=entity_id, version_id=src.version_id,
            page_no=src.page_no, locator=src.locator,
            excerpt=(src.excerpt or "")[:300] or None, extracted_by=src.extracted_by,
            confidence=src.confidence, extraction_id=src.extraction_id,
            first_run=self.run_id, last_run=self.run_id,
        ).on_conflict_do_update(
            constraint="uq_evidence_location",
            set_={"last_run": self.run_id, "excerpt": (src.excerpt or "")[:300] or None,
                  "extracted_by": src.extracted_by, "confidence": src.confidence},
        ))
        self.asserted[entity_type].add(entity_id)

    def live_sources(self, entity_type: str) -> dict[int, list[str]]:
        """entity id -> extracted_by of evidence on documents' *current* versions."""
        rows = self.s.execute(
            select(Evidence.entity_id, Evidence.extracted_by)
            .join(DocumentVersion, DocumentVersion.id == Evidence.version_id)
            .join(Document, Document.current_version_id == DocumentVersion.id)
            .where(Evidence.entity_type == entity_type, Document.removed_at_run.is_(None)))
        live: dict[int, list[str]] = defaultdict(list)
        for entity_id, by in rows:
            live[entity_id].append(by)
        return live

    # --------------------------------------------------------------- facts

    def fact(self, *, project_id: int | None, field: str, value_text: str | None,
             source: Source, value_num: float | None = None, unit: str | None = None,
             as_of: date | None = None, as_of_precision: str | None = None,
             label: str | None = None, project_hint: str | None = None) -> Fact | None:
        if not (value_text or value_num is not None):
            return None
        key = value_key(value_text, value_num, unit)
        row = self.s.scalar(select(Fact).where(
            Fact.project_id.is_(None) if project_id is None else Fact.project_id == project_id,
            Fact.field == field, Fact.value_key == key))
        if row is None:
            row = Fact(project_id=project_id, project_hint=project_hint, field=field,
                       label=label, value_text=value_text, value_num=value_num, unit=unit,
                       value_key=key, extracted_by=source.extracted_by,
                       confidence=source.confidence, status="current",
                       valid_from_run=self.run_id)
            self.s.add(row)
            self.s.flush()
        if rank(source.extracted_by) > rank(row.extracted_by):
            row.extracted_by, row.confidence = source.extracted_by, source.confidence
        if as_of and (row.as_of is None or as_of.isoformat() > row.as_of):
            row.as_of, row.as_of_precision = as_of.isoformat(), as_of_precision
        self._evidence("fact", row.id, source)
        return row

    def resolve_facts(self) -> dict[str, int]:
        """Apply the v1 reconciliation rules to every (project, field). Returns status counts."""
        live = self.live_sources("fact")
        groups: dict[tuple, list[Fact]] = defaultdict(list)
        for f in self.s.scalars(select(Fact)):
            groups[(f.project_id, f.project_hint if f.project_id is None else None,
                    f.field)].append(f)

        changes: dict[str, int] = defaultdict(int)
        for facts in groups.values():
            alive = [f for f in facts if f.id in live]
            winners: list[Fact] = []
            if alive:
                best = max(max(rank(b) for b in live[f.id]) for f in alive)
                top = [f for f in alive if max(rank(b) for b in live[f.id]) == best]
                if best == 3:  # the live site wins on fields it covers
                    winners = top
                else:  # otherwise the most recent document date wins
                    newest = max((f.as_of or "") for f in top)
                    winners = [f for f in top if (f.as_of or "") == newest]
            winner_status = "current" if len(winners) == 1 else "conflicting"
            for f in facts:
                if f in winners:
                    self._set_status(f, winner_status, None, changes)
                elif winners:
                    self._set_status(f, "superseded", winners[0].id, changes)
                elif f.status != "superseded":
                    self._set_status(f, "retracted", None, changes)
        self.s.flush()
        return dict(changes)

    def _set_status(self, f: Fact, status: str, superseded_by: int | None,
                    changes: dict[str, int]) -> None:
        was_valid = f.status in ("current", "conflicting")
        now_valid = status in ("current", "conflicting")
        if f.status != status:
            changes[f"facts_{status}"] += 1
        if was_valid and not now_valid:
            f.valid_to_run = self.run_id
        elif now_valid and not was_valid:
            f.valid_to_run = None
        f.status = status
        f.superseded_by = superseded_by

    # ---------------------------------------------------------- milestones

    def milestone(self, *, project_id: int | None, type: str, source: Source,
                  label: str | None = None, title: str | None = None,
                  date: date | None = None, date_precision: str | None = None,
                  end_date: date | None = None, status: str = "planned",
                  project_hint: str | None = None) -> Milestone:
        fields = {"label": label, "title": title, "date": date, "date_precision": date_precision,
                  "end_date": end_date, "status": status}
        row = self._match_milestone(project_id, project_hint, type, label, title, date,
                                    date_precision, end_date)
        if row is None:
            row = Milestone(project_id=project_id, project_hint=project_hint, type=type,
                            extracted_by=source.extracted_by, confidence=source.confidence,
                            first_seen_run=self.run_id, **fields)
            self.s.add(row)
            self.s.flush()
        else:
            self._merge(row, fields, source)
        row.last_seen_run = self.run_id
        row.retracted_run = None
        self._evidence("milestone", row.id, source)
        return row

    def _match_milestone(self, project_id, hint, type_, label, title, d, precision, end):
        candidates = self.s.scalars(select(Milestone).where(
            Milestone.project_id.is_(None) if project_id is None
            else Milestone.project_id == project_id,
            Milestone.type == type_))
        candidates = [c for c in candidates if project_id is not None or c.project_hint == hint]
        if label:  # a structured identity (e.g. CMS timeline phase): match on it alone
            return next((c for c in candidates if c.label == label), None)
        if d:
            for c in candidates:
                if c.date and _overlaps((d, precision, end), (c.date, c.date_precision,
                                                              c.end_date)):
                    return c
            return None
        for c in candidates:
            if title and c.title and fuzz.token_sort_ratio(title, c.title) >= TITLE_MATCH:
                return c
        return None

    # -------------------------------------------------- procurement signals

    def signal(self, *, project_id: int | None, kind: str, title: str, source: Source,
               status: str = "unknown", project_hint: str | None = None,
               **fields: Any) -> ProcurementSignal:
        fields = {"title": title, "status": status, **fields}
        row = self._match_signal(project_id, project_hint, kind, title,
                                 fields.get("reference_number"), fields.get("opens_at"),
                                 fields.get("closes_at"))
        if row is None:
            row = ProcurementSignal(project_id=project_id, project_hint=project_hint, kind=kind,
                                    extracted_by=source.extracted_by,
                                    confidence=source.confidence, first_seen_run=self.run_id,
                                    **fields)
            self.s.add(row)
            self.s.flush()
        else:
            self._merge(row, fields, source)
        row.last_seen_run = self.run_id
        row.retracted_run = None
        self._evidence("procurement_signal", row.id, source)
        return row

    def _match_signal(self, project_id, hint, kind, title, ref, opens, closes):
        candidates = [c for c in self.s.scalars(select(ProcurementSignal).where(
            ProcurementSignal.project_id.is_(None) if project_id is None
            else ProcurementSignal.project_id == project_id,
            ProcurementSignal.kind == kind))
            if project_id is not None or c.project_hint == hint]
        if ref:
            for c in candidates:
                if c.reference_number == ref:
                    return c
        for c in candidates:
            if _same_date(opens, c.opens_at) or _same_date(closes, c.closes_at):
                return c
        for c in candidates:
            if c.title and fuzz.token_sort_ratio(title, c.title) >= TITLE_MATCH:
                return c
        return None

    # -------------------------------------------------------------- shared

    def _merge(self, row: Any, fields: dict[str, Any], source: Source) -> None:
        """A source may overwrite fields only if it ranks >= the source that owns the record;
        otherwise it fills empty fields."""
        owns = rank(source.extracted_by) >= rank(row.extracted_by)
        for k, v in fields.items():
            if v is None:
                continue
            if owns or getattr(row, k) in (None, "", "unknown"):
                setattr(row, k, v)
        if rank(source.extracted_by) > rank(row.extracted_by):
            row.extracted_by, row.confidence = source.extracted_by, source.confidence

    def retract_unsupported(self, entity_type: str, model: type) -> list[Any]:
        """Mark milestones/signals with no live evidence as retracted; returns them."""
        live = self.live_sources(entity_type)
        dead = [r for r in self.s.scalars(select(model).where(model.retracted_run.is_(None)))
                if r.id not in live]
        for r in dead:
            r.retracted_run = self.run_id
        if live:
            self.s.execute(update(model).where(model.id.in_(list(live)),
                                               model.retracted_run.is_not(None))
                           .values(retracted_run=None))
        self.s.flush()
        return dead


def _overlaps(a: tuple, b: tuple) -> bool:
    """Do two (date, precision, end_date) periods overlap?"""
    def span(d, precision, end):
        hi = end or d
        return d, period_end(DatePoint(hi, precision or "day")) if precision else hi
    a0, a1 = span(*a)
    b0, b1 = span(*b)
    return a0 <= b1 and b0 <= a1


def _same_date(a: str | None, b: str | None) -> bool:
    """ISO strings of possibly different precision ('2024-07' vs '2024-07-15')."""
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def flatten(values: Iterable[str]) -> str:
    return ", ".join(sorted(v for v in values if v))
