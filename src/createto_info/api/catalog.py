"""Cross-project read queries: facts, milestones, documents, crawl audit, initiatives,
councillors. Project search/detail live in `queries.py`."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from createto_info.api.queries import _evidence

LIVE_FACT = ("current", "conflicting")


def _page(sql: str, params: dict[str, Any], limit: int, offset: int) -> str:
    params.update(limit=limit, offset=offset)
    return sql + " LIMIT :limit OFFSET :offset"


def _attach_evidence(s: Session, entity_type: str, rows: list[dict[str, Any]],
                     per_row: int | None = None) -> list[dict[str, Any]]:
    ev = _evidence(s, entity_type, [r["id"] for r in rows])
    for r in rows:
        items = ev.get(r["id"], [])
        r["evidence_count"] = len(items)
        r["evidence"] = items[:per_row] if per_row else items
    return rows


# ------------------------------------------------------------------- facts

def facts(s: Session, *, field: str | None, project: str | None, status: str | None,
          source: str | None, link_status: str | None, q: str | None,
          min_value: float | None, max_value: float | None, limit: int,
          offset: int) -> list[dict[str, Any]]:
    where, params = ["TRUE"], {}
    if status == "all":
        pass
    elif status:
        where.append("f.status = :status")
        params["status"] = status
    else:
        where.append("f.status IN ('current', 'conflicting')")
    if field:
        where.append("f.field = :field")
        params["field"] = field
    if project:
        where.append("p.slug = :project")
        params["project"] = project
    if source == "cms":
        where.append("f.extracted_by = 'cms'")
    elif source == "documents":
        where.append("f.extracted_by LIKE 'model:%'")
    if link_status:
        where.append("f.link_status = :link_status")
        params["link_status"] = link_status
    if q:
        where.append("(f.value_text ILIKE :q OR f.label ILIKE :q)")
        params["q"] = f"%{q}%"
    if min_value is not None:
        where.append("f.value_num >= :min")
        params["min"] = min_value
    if max_value is not None:
        where.append("f.value_num <= :max")
        params["max"] = max_value
    sql = _page(f"""
        SELECT f.id, f.field, f.label, f.value_text AS value, f.value_num, f.unit, f.status,
               f.as_of, f.extracted_by, f.confidence, f.link_status, f.link_method,
               f.project_hint, p.slug AS project_slug, p.name AS project_name
        FROM facts f LEFT JOIN projects p ON p.id = f.project_id
        WHERE {" AND ".join(where)}
        ORDER BY p.name NULLS LAST, f.field, f.value_num DESC NULLS LAST, f.id""",
                params, limit, offset)
    rows = [dict(r) for r in s.execute(text(sql), params).mappings()]
    return _attach_evidence(s, "fact", rows, per_row=3)


def fact(s: Session, fact_id: int) -> dict[str, Any] | None:
    row = s.execute(text("""
        SELECT f.*, p.slug AS project_slug, p.name AS project_name
        FROM facts f LEFT JOIN projects p ON p.id = f.project_id WHERE f.id = :id"""),
        {"id": fact_id}).mappings().first()
    if row is None:
        return None
    out = {k: v for k, v in dict(row).items() if k != "value_key"}
    out["evidence"] = _evidence(s, "fact", [fact_id]).get(fact_id, [])
    out["superseded_by_fact"] = dict(s.execute(text(
        "SELECT id, value_text AS value, status, as_of FROM facts WHERE id = :i"),
        {"i": row["superseded_by"]}).mappings().first() or {}) or None
    out["supersedes"] = [dict(r) for r in s.execute(text(
        "SELECT id, value_text AS value, as_of, valid_from_run, valid_to_run FROM facts "
        "WHERE superseded_by = :i ORDER BY as_of NULLS FIRST"), {"i": fact_id}).mappings()]
    out["alternatives"] = [dict(r) for r in s.execute(text("""
        SELECT id, value_text AS value, status, as_of, extracted_by FROM facts
        WHERE project_id IS NOT DISTINCT FROM :p AND field = :f AND id <> :i
        ORDER BY as_of DESC NULLS LAST"""),
        {"p": row["project_id"], "f": row["field"], "i": fact_id}).mappings()]
    return out


def fact_fields(s: Session) -> list[dict[str, Any]]:
    return [dict(r) for r in s.execute(text("""
        SELECT field, count(*) FILTER (WHERE status IN ('current', 'conflicting')) AS current,
               count(*) AS total, count(DISTINCT project_id) AS projects
        FROM facts GROUP BY field ORDER BY current DESC""")).mappings()]


# -------------------------------------------------------------- milestones

def milestones(s: Session, *, type: str | None, status: str | None, project: str | None,
               date_from: str | None, date_to: str | None, link_status: str | None,
               include_retracted: bool, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = ["TRUE"], {}
    if not include_retracted:
        where.append("m.retracted_run IS NULL")
    for key, col in (("type", "m.type"), ("status", "m.status"),
                     ("link_status", "m.link_status")):
        value = {"type": type, "status": status, "link_status": link_status}[key]
        if value:
            where.append(f"{col} = :{key}")
            params[key] = value
    if project:
        where.append("p.slug = :project")
        params["project"] = project
    if date_from:
        where.append("coalesce(m.end_date, m.date) >= CAST(:dfrom AS date)")
        params["dfrom"] = date_from
    if date_to:
        where.append("m.date <= CAST(:dto AS date)")
        params["dto"] = date_to
    sql = _page(f"""
        SELECT m.id, m.type, m.label, m.title, m.date, m.date_precision, m.end_date, m.status,
               m.extracted_by, m.confidence, m.link_status, m.project_hint,
               p.slug AS project_slug, p.name AS project_name, p.ward AS project_ward
        FROM milestones m LEFT JOIN projects p ON p.id = m.project_id
        WHERE {" AND ".join(where)}
        ORDER BY m.date NULLS LAST, m.id""", params, limit, offset)
    rows = [dict(r) for r in s.execute(text(sql), params).mappings()]
    return _attach_evidence(s, "milestone", rows, per_row=2)


# --------------------------------------------------------------- documents

_DOC_COLUMNS = """
    d.id, d.url, coalesce(nullif(d.title, ''), regexp_replace(d.url, '^.*/', '')) AS title,
    d.kind, d.scope, d.host, d.discovered_via, d.first_seen_run,
    d.last_seen_run, d.removed_at_run, v.fetched_at, v.bytes, v.doc_date, v.content_type,
    v.parse_status, v.parse_error,
    (SELECT count(*) FROM pdf_pages pp WHERE pp.version_id = v.id) AS pages,
    (SELECT count(*) FROM evidence e WHERE e.version_id = v.id) AS evidence_rows,
    (SELECT array_agg(DISTINCT p.slug) FROM links l JOIN projects p ON p.id = l.from_id
      WHERE l.rel = 'has_file' AND l.from_type = 'project' AND l.to_id = d.id) AS listed_on,
    (SELECT array_agg(DISTINCT p.slug) FROM links l JOIN projects p ON p.id = l.to_id
      WHERE l.rel = 'mentions' AND l.from_type = 'document' AND l.from_id = d.id) AS mentions
"""


def documents(s: Session, *, project: str | None, kind: str | None, scope: str | None,
              q: str | None, parse_status: str | None, limit: int,
              offset: int) -> list[dict[str, Any]]:
    where, params = ["d.removed_at_run IS NULL"], {}
    for key in ("kind", "scope"):
        value = {"kind": kind, "scope": scope}[key]
        if value:
            where.append(f"d.{key} = :{key}")
            params[key] = value
    if parse_status:
        where.append("v.parse_status = :ps")
        params["ps"] = parse_status
    if q:
        where.append("(d.title ILIKE :q OR d.url ILIKE :q)")
        params["q"] = f"%{q}%"
    if project:
        where.append("""EXISTS (SELECT 1 FROM links l JOIN projects p
            ON p.id = CASE WHEN l.rel = 'has_file' THEN l.from_id ELSE l.to_id END
            WHERE ((l.rel = 'has_file' AND l.from_type = 'project' AND l.to_id = d.id)
                OR (l.rel = 'mentions' AND l.from_type = 'document' AND l.from_id = d.id))
              AND p.slug = :project)""")
        params["project"] = project
    sql = _page(f"""
        SELECT {_DOC_COLUMNS}
        FROM documents d LEFT JOIN document_versions v ON v.id = d.current_version_id
        WHERE {" AND ".join(where)} ORDER BY v.doc_date DESC NULLS LAST, d.id""",
                params, limit, offset)
    return [dict(r) for r in s.execute(text(sql), params).mappings()]


def document(s: Session, doc_id: int) -> dict[str, Any] | None:
    row = s.execute(text(f"""
        SELECT {_DOC_COLUMNS}, v.id AS version_id
        FROM documents d LEFT JOIN document_versions v ON v.id = d.current_version_id
        WHERE d.id = :id"""), {"id": doc_id}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["versions"] = [dict(r) for r in s.execute(text("""
        SELECT id, run_id, fetched_at, http_status, bytes, content_hash, doc_date,
               parse_status, parse_error, (id = :cur) AS is_current
        FROM document_versions WHERE document_id = :d ORDER BY id"""),
        {"d": doc_id, "cur": row["version_id"]}).mappings()]
    out["page_methods"] = {m: n for m, n in s.execute(text(
        "SELECT method, count(*) FROM pdf_pages WHERE version_id = :v GROUP BY 1"),
        {"v": row["version_id"]})}
    out["linked_from_news"] = [dict(r) for r in s.execute(text("""
        SELECT n.title, n.url, n.published_at FROM links l JOIN news_articles n
          ON n.id = l.from_id
        WHERE l.rel = 'has_file' AND l.from_type = 'news' AND l.to_id = :d"""),
        {"d": doc_id}).mappings()]
    out["records"] = [dict(r) for r in s.execute(text("""
        SELECT e.entity_type, e.entity_id, e.page_no, e.excerpt, e.extracted_by
        FROM evidence e JOIN document_versions v ON v.id = e.version_id
        WHERE v.document_id = :d ORDER BY e.page_no NULLS FIRST, e.id LIMIT 200"""),
        {"d": doc_id}).mappings()]
    return out


def document_page(s: Session, doc_id: int, page_no: int) -> dict[str, Any] | None:
    row = s.execute(text("""
        SELECT d.url, d.title, pp.page_no, pp.method, pp.text, pp.vision_description,
               pp.image_count, pp.drawing_count
        FROM documents d JOIN pdf_pages pp ON pp.version_id = d.current_version_id
        WHERE d.id = :d AND pp.page_no = :n"""), {"d": doc_id, "n": page_no}).mappings().first()
    return dict(row) if row else None


# ------------------------------------------------------------- crawl audit

def run(s: Session, run_id: int) -> dict[str, Any] | None:
    row = s.execute(text("SELECT * FROM crawl_runs WHERE id = :r"),
                    {"r": run_id}).mappings().first()
    if row is None:
        return None
    out = dict(row)
    out["fetch_outcomes"] = [dict(r) for r in s.execute(text("""
        SELECT outcome, reason, count(*) AS n FROM fetch_log WHERE run_id = :r
        GROUP BY 1, 2 ORDER BY 3 DESC"""), {"r": run_id}).mappings()]
    out["llm"] = [dict(r) for r in s.execute(text("""
        SELECT model, task, count(*) AS calls, count(*) FILTER (WHERE status <> 'ok') AS failed,
               sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
               round(sum(coalesce(cost_usd, 0))::numeric, 5) AS billed_usd,
               round(sum(coalesce(est_cost_usd, 0))::numeric, 5) AS est_usd
        FROM llm_calls WHERE run_id = :r GROUP BY 1, 2 ORDER BY 1, 2"""),
        {"r": run_id}).mappings()]
    return out


def fetch_log(s: Session, run_id: int, *, outcome: str | None, reason: str | None,
              host: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = ["run_id = :r"], {"r": run_id}
    if outcome:
        where.append("outcome = :outcome")
        params["outcome"] = outcome
    if reason:
        where.append("reason = :reason")
        params["reason"] = reason
    if host:
        where.append("url ILIKE :host")
        params["host"] = f"%://{host}%"
    sql = _page(f"""
        SELECT ts, url, referrer_url, outcome, reason, http_status, elapsed_ms FROM fetch_log
        WHERE {" AND ".join(where)} ORDER BY id""", params, limit, offset)
    return [dict(r) for r in s.execute(text(sql), params).mappings()]


# ------------------------------------------------- initiatives, councillors

def initiatives(s: Session) -> list[dict[str, Any]]:
    return [dict(r) for r in s.execute(text("""
        SELECT i.slug, i.name, i.url, left(i.description, 400) AS description,
               count(p.id) AS projects,
               array_agg(p.slug ORDER BY p.name) FILTER (WHERE p.id IS NOT NULL) AS project_slugs
        FROM initiatives i LEFT JOIN projects p ON p.initiative_id = i.id
        GROUP BY i.id ORDER BY i.name""")).mappings()]


def councillors(s: Session) -> list[dict[str, Any]]:
    return [dict(r) for r in s.execute(text("""
        SELECT c.name, c.ward, c.profile_url, count(p.id) AS projects,
               array_agg(p.slug ORDER BY p.name) FILTER (WHERE p.id IS NOT NULL) AS project_slugs
        FROM councillors c LEFT JOIN projects p ON p.councillor_id = c.id
        GROUP BY c.id ORDER BY c.ward NULLS LAST, c.name""")).mappings()]
