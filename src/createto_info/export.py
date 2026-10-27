"""Bulk export as JSON lines: one file per entity, evidence embedded, plus a manifest.

Used by `createto export` (files under data/exports/) and `GET /export/{name}.jsonl`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from createto_info.crawl.recorder import git_sha


def _evidence_sql(entity: str, alias: str) -> str:
    return f"""(SELECT json_agg(json_build_object(
        'url', d.url, 'title', d.title, 'page', e.page_no, 'locator', e.locator,
        'excerpt', e.excerpt, 'extracted_by', e.extracted_by, 'confidence', e.confidence,
        'live', d.current_version_id = e.version_id AND d.removed_at_run IS NULL)
        ORDER BY e.id)
      FROM evidence e JOIN document_versions v ON v.id = e.version_id
      JOIN documents d ON d.id = v.document_id
      WHERE e.entity_type = '{entity}' AND e.entity_id = {alias}.id) AS evidence"""


ENTITIES: dict[str, str] = {
    "projects": """
        SELECT p.slug, p.kind, p.name, p.url, p.summary, p.description, p.address,
               p.major_intersection, p.latitude, p.longitude, p.ward, c.name AS councillor,
               p.phase, p.phase_label, p.types, p.type_labels, i.slug AS initiative,
               pp.slug AS parent, p.site_area_text, p.site_area_acres, p.current_use,
               p.proposed_use, p.surrounding_use, p.cms_published_at, p.cms_updated_at,
               p.last_activity_date, p.cutoff_status, p.first_seen_run, p.last_seen_run,
               (SELECT array_agg(a.alias ORDER BY a.alias) FROM project_aliases a
                 WHERE a.project_id = p.id) AS aliases
        FROM projects p LEFT JOIN initiatives i ON i.id = p.initiative_id
        LEFT JOIN projects pp ON pp.id = p.parent_project_id
        LEFT JOIN councillors c ON c.id = p.councillor_id ORDER BY p.slug""",
    "facts": f"""
        SELECT f.id, p.slug AS project, f.project_hint, f.field, f.label, f.value_text,
               f.value_num, f.unit, f.as_of, f.as_of_precision, f.status, f.superseded_by,
               f.valid_from_run, f.valid_to_run, f.extracted_by, f.confidence, f.link_status,
               f.link_method, f.link_confidence, {_evidence_sql('fact', 'f')}
        FROM facts f LEFT JOIN projects p ON p.id = f.project_id ORDER BY f.id""",
    "milestones": f"""
        SELECT m.id, p.slug AS project, m.project_hint, m.type, m.label, m.title, m.date,
               m.date_precision, m.end_date, m.status, m.extracted_by, m.confidence,
               m.link_status, m.link_method, m.first_seen_run, m.last_seen_run,
               m.retracted_run, {_evidence_sql('milestone', 'm')}
        FROM milestones m LEFT JOIN projects p ON p.id = m.project_id ORDER BY m.id""",
    "procurement_signals": f"""
        SELECT s.id, p.slug AS project, s.project_hint, s.kind, s.title, s.status,
               s.reference_number, s.issuer, s.counterparty, s.scope, s.value, s.currency,
               s.opens_at, s.closes_at, s.external_url, s.extracted_by, s.confidence,
               s.link_status, s.link_method, s.first_seen_run, s.last_seen_run,
               s.retracted_run, {_evidence_sql('procurement_signal', 's')}
        FROM procurement_signals s LEFT JOIN projects p ON p.id = s.project_id
        ORDER BY s.id""",
    "news": """
        SELECT n.slug, n.url, n.title, n.published_at, n.kind, n.topics, n.importance,
               n.external_url, n.body_text,
               (SELECT array_agg(DISTINCT p.slug) FROM links l JOIN projects p ON p.id = l.to_id
                 WHERE l.from_type = 'news' AND l.from_id = n.id AND l.to_type = 'project')
                 AS projects
        FROM news_articles n ORDER BY n.published_at DESC NULLS LAST""",
    "documents": """
        SELECT d.id, d.url, d.title, d.kind, d.scope, d.host, v.doc_date, v.fetched_at,
               v.bytes, v.content_hash, v.parse_status, d.first_seen_run, d.last_seen_run,
               d.removed_at_run,
               (SELECT array_agg(DISTINCT p.slug) FROM links l JOIN projects p
                 ON p.id = l.from_id WHERE l.rel = 'has_file' AND l.from_type = 'project'
                 AND l.to_id = d.id) AS listed_on,
               (SELECT array_agg(DISTINCT p.slug) FROM links l JOIN projects p
                 ON p.id = l.to_id WHERE l.rel = 'mentions' AND l.from_type = 'document'
                 AND l.from_id = d.id) AS mentions
        FROM documents d LEFT JOIN document_versions v ON v.id = d.current_version_id
        ORDER BY d.id""",
    "initiatives": "SELECT slug, name, url, description FROM initiatives ORDER BY slug",
    "councillors": "SELECT name, ward, profile_url FROM councillors ORDER BY name",
    "runs": """SELECT id, mode, status, started_at, finished_at, git_sha, stats
               FROM crawl_runs ORDER BY id""",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


def rows(s: Session, name: str) -> Iterator[dict[str, Any]]:
    result = s.execute(text(ENTITIES[name]).execution_options(yield_per=500))
    for r in result.mappings():
        yield dict(r)


def jsonl(s: Session, name: str) -> Iterator[str]:
    for row in rows(s, name):
        yield json.dumps(row, default=_json_default, ensure_ascii=False) + "\n"


def export_all(s: Session, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in ENTITIES:
        path = out_dir / f"{name}.jsonl"
        n = 0
        with open(path, "w", encoding="utf-8") as fh:
            for line in jsonl(s, name):
                fh.write(line)
                n += 1
        counts[name] = n
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "latest_run": s.execute(text("SELECT max(id) FROM crawl_runs")).scalar(),
        "git_sha": git_sha(),
        "counts": counts,
        "files": {name: f"{name}.jsonl" for name in ENTITIES},
        "notes": "Facts, milestones and procurement signals embed their evidence (source URL, "
                 "page, locator, verbatim excerpt, live flag). See docs/DATABASE.md.",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
