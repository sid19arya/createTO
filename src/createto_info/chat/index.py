"""Retrieval corpus (`search_chunks`) and an in-memory BM25 index over it.

Chunks: one per project (profile + current facts), one per news article, one per PDF page
(text pages; image pages use their model-written description). `createto index` rebuilds the
table; the API builds the BM25 index lazily and rebuilds it when the table changes.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from rank_bm25 import BM25Okapi
from sqlalchemy import text
from sqlalchemy.orm import Session

MAX_CHARS = 4000  # per chunk; PDF pages are rarely longer
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")
_STOP = set("""a an and are as at be by for from has have in is it its of on or that the this
to was were will with which who what when where how does do did their there these those
about into than then they them our we you your i me my can could would should""".split())


def tokenize(value: str) -> list[str]:
    return [t for t in _TOKEN.findall((value or "").lower()) if t not in _STOP]


def rebuild_chunks(s: Session, run_id: int | None) -> dict[str, int]:
    """Replace `search_chunks` with the current corpus. Returns counts per entity type."""
    s.execute(text("TRUNCATE search_chunks RESTART IDENTITY"))
    s.execute(text("""
        INSERT INTO search_chunks (entity_type, entity_id, project_id, title, text, url, page_no,
                                   updated_run)
        SELECT 'project', p.id, p.id, p.name,
               concat_ws(E'\\n',
                 p.name || coalesce(' (' || p.phase_label || ')', ''),
                 nullif(concat_ws(', ', p.address, p.major_intersection, p.ward), ''),
                 'Types: ' || p.type_labels, p.summary, p.description,
                 (SELECT string_agg(f.field || ': ' || f.value_text, E'\\n' ORDER BY f.field)
                    FROM facts f WHERE f.project_id = p.id
                      AND f.status IN ('current', 'conflicting'))),
               p.url, NULL, :run
        FROM projects p"""), {"run": run_id})
    s.execute(text("""
        INSERT INTO search_chunks (entity_type, entity_id, project_id, title, text, url, page_no,
                                   updated_run)
        SELECT 'news', n.id,
               (SELECT min(l.to_id) FROM links l WHERE l.from_type = 'news'
                  AND l.from_id = n.id AND l.to_type = 'project'),
               n.title, left(n.title || E'\\n' || coalesce(n.body_text, ''), :max), n.url, NULL,
               :run
        FROM news_articles n"""), {"run": run_id, "max": MAX_CHARS})
    s.execute(text("""
        INSERT INTO search_chunks (entity_type, entity_id, project_id, title, text, url, page_no,
                                   updated_run)
        SELECT 'pdf_page', d.id,
               (SELECT min(l.from_id) FROM links l WHERE l.rel = 'has_file'
                  AND l.from_type = 'project' AND l.to_id = d.id),
               coalesce(nullif(d.title, ''), regexp_replace(d.url, '^.*/', '')),
               left(CASE WHEN pp.method = 'text' THEN pp.text
                         ELSE concat_ws(E'\\n', pp.vision_description, pp.text) END, :max),
               d.url, pp.page_no, :run
        FROM documents d
        JOIN document_versions v ON v.id = d.current_version_id
        JOIN pdf_pages pp ON pp.version_id = v.id
        WHERE d.removed_at_run IS NULL
          AND (pp.method = 'text' OR (pp.method = 'vision' AND pp.vision_description IS NOT NULL))
        """), {"run": run_id, "max": MAX_CHARS})
    s.flush()
    return {t: n for t, n in s.execute(text(
        "SELECT entity_type, count(*) FROM search_chunks GROUP BY 1"))}


@dataclass
class Hit:
    chunk_id: int
    score: float
    entity_type: str
    entity_id: int
    project_id: int | None
    title: str
    text: str
    url: str | None
    page_no: int | None
    date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "chunk_id"}


class Index:
    """BM25 over all chunks; a few thousand rows, so it lives in memory."""

    def __init__(self, s: Session):
        rows = s.execute(text("""
            SELECT c.id, c.entity_type, c.entity_id, c.project_id, c.title, c.text, c.url,
                   c.page_no,
                   CASE c.entity_type
                     WHEN 'news' THEN (SELECT n.published_at::date::text FROM news_articles n
                                       WHERE n.id = c.entity_id)
                     WHEN 'pdf_page' THEN (SELECT v.doc_date::text FROM documents d
                                           JOIN document_versions v
                                             ON v.id = d.current_version_id
                                           WHERE d.id = c.entity_id)
                   END AS date
            FROM search_chunks c ORDER BY c.id""")).mappings().all()
        self.hits = [Hit(r["id"], 0.0, r["entity_type"], r["entity_id"], r["project_id"],
                         r["title"] or "", r["text"] or "", r["url"], r["page_no"], r["date"])
                     for r in rows]
        corpus = [tokenize(f"{h.title} {h.title} {h.text}") for h in self.hits]
        self.bm25 = BM25Okapi(corpus) if corpus else None
        self.signature = signature(s)

    def search(self, query: str, k: int = 8, per_source: int = 2,
               project_slots: int = 3) -> list[Hit]:
        """Top-k chunks. Up to `project_slots` go to project profiles (the live site's current
        state), which otherwise lose to long, term-dense document pages; at most `per_source`
        chunks per document so one long PDF can't crowd out other sources."""
        tokens = tokenize(query)
        if not tokens or self.bm25 is None:
            return []
        scores = self.bm25.get_scores(tokens)
        order = [i for i in sorted(range(len(scores)), key=lambda i: -scores[i])
                 if scores[i] > 0]
        chosen: list[int] = [i for i in order
                             if self.hits[i].entity_type == "project"][:project_slots]
        per: dict[tuple[str, int], int] = defaultdict(int)
        for i in chosen:
            per[(self.hits[i].entity_type, self.hits[i].entity_id)] += 1
        for i in order:
            if len(chosen) >= k:
                break
            key = (self.hits[i].entity_type, self.hits[i].entity_id)
            if i in chosen or per[key] >= per_source:
                continue
            per[key] += 1
            chosen.append(i)
        chosen.sort(key=lambda i: -scores[i])
        return [Hit(**{**self.hits[i].__dict__, "score": round(float(scores[i]), 3)})
                for i in chosen]


def signature(s: Session) -> tuple:
    return tuple(s.execute(text(
        "SELECT count(*), coalesce(max(id), 0), coalesce(max(updated_run), 0) "
        "FROM search_chunks")).one())


_cache: dict[str, Index] = {}


def get_index(s: Session) -> Index:
    idx = _cache.get("idx")
    if idx is None or idx.signature != signature(s):
        idx = _cache["idx"] = Index(s)
    return idx
