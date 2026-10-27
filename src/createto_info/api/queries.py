"""Read-side queries behind the API. Plain SQL where ranking needs Postgres features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from createto_info.parse.cms import ProjectMatcher

OPEN = ("open", "upcoming")

# search-bar semantics: match ANY term, rank by how many/how well they match
TSQ = "replace(plainto_tsquery('english', :q)::text, ' & ', ' | ')::tsquery"
TSQ_ALL = "plainto_tsquery('english', :q)"  # bonus for documents matching every term
TRIGRAM_MIN = 0.6  # below this, "1113 Dundas St W" also matched "277 Victoria Street"

# score = any-term rank + all-terms rank (each 0..1) + 0.5 * best trigram word-similarity
#         + 1 for a street-address hit
_CARD_COLUMNS = """
    p.id, p.slug, p.kind, p.name, p.phase, p.phase_label, p.types, p.address, p.ward,
    p.latitude, p.longitude, p.site_area_text, p.summary, p.url, p.cutoff_status,
    p.last_activity_date, i.slug AS initiative_slug, i.name AS initiative,
    pp.slug AS parent_slug,
    (SELECT count(*) FROM procurement_signals s WHERE s.project_id = p.id
       AND s.status IN ('open', 'upcoming') AND s.retracted_run IS NULL) AS open_procurement
"""
_HAVERSINE = """6371 * 2 * asin(sqrt(
    power(sin(radians(p.latitude - :lat) / 2), 2) +
    cos(radians(:lat)) * cos(radians(p.latitude)) *
    power(sin(radians(p.longitude - :lng) / 2), 2)))"""


@dataclass
class ProjectQuery:
    q: str = ""
    phase: str | None = None
    ward: str | None = None
    type: str | None = None
    initiative: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    include_excluded: bool = False
    limit: int = 20
    offset: int = 0


def search_projects(s: Session, pq: ProjectQuery) -> list[dict[str, Any]]:
    q = (pq.q or "").strip()
    params: dict[str, Any] = {"limit": pq.limit, "offset": pq.offset}
    where = ["TRUE"]
    score = "0.0"
    if q:
        params["q"] = q
        params["addr"] = sorted(ProjectMatcher(s).address_matches(q)) or [-1]
        fts = (f"(ts_rank_cd(p.search_tsv, {TSQ}, 32) + "
               f"ts_rank_cd(p.search_tsv, {TSQ_ALL}, 32))")
        trgm = ("greatest(word_similarity(:q, p.name), coalesce((SELECT max(word_similarity("
                ":q, a.alias)) FROM project_aliases a WHERE a.project_id = p.id), 0))")
        addr = "(p.id = ANY(:addr))::int"
        score = f"{fts} + 0.5 * {trgm} + {addr}"
        where.append(f"(p.search_tsv @@ {TSQ} OR {trgm} >= {TRIGRAM_MIN} "
                     f"OR p.id = ANY(:addr))")
    if pq.phase:
        params["phase"] = pq.phase.lower()
        where.append("(lower(p.phase) = :phase OR lower(p.phase_label) = :phase)")
    if pq.ward:
        params["ward"] = f"%{pq.ward}%"
        where.append("p.ward ILIKE :ward")
    if pq.type:
        params["type"] = pq.type
        where.append(":type = ANY(p.types)")
    if pq.initiative:
        params["initiative"] = pq.initiative
        where.append("i.slug = :initiative")
    if not pq.include_excluded:
        where.append("p.cutoff_status IS DISTINCT FROM 'excluded_completed_old'")
    distance = "NULL::float"
    if pq.lat is not None and pq.lng is not None:
        params.update(lat=pq.lat, lng=pq.lng)
        distance = _HAVERSINE
        where.append("p.latitude IS NOT NULL")
        if pq.radius_km:
            params["radius"] = pq.radius_km
            where.append(f"{_HAVERSINE} <= :radius")

    sql = f"""
        SELECT {_CARD_COLUMNS}, ({score}) AS score, ({distance}) AS distance_km
        FROM projects p
        LEFT JOIN initiatives i ON i.id = p.initiative_id
        LEFT JOIN projects pp ON pp.id = p.parent_project_id
        WHERE {" AND ".join(where)}
        ORDER BY score DESC, distance_km ASC NULLS LAST, p.name
        LIMIT :limit OFFSET :offset"""
    rows = s.execute(text(sql), params).mappings().all()
    return [_card(r) for r in rows]


def _card(r: Any) -> dict[str, Any]:
    d = dict(r)
    d.pop("id", None)
    if d.get("summary") and len(d["summary"]) > 300:
        d["summary"] = d["summary"][:297].rstrip() + "..."
    if d.get("score") is not None:
        d["score"] = round(float(d["score"]), 4)
    if d.get("distance_km") is not None:
        d["distance_km"] = round(float(d["distance_km"]), 2)
    return d


# ---------------------------------------------------------------- evidence


def _evidence(s: Session, entity_type: str, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    rows = s.execute(text("""
        SELECT e.entity_id, d.url, d.title, e.page_no, e.locator, e.excerpt, e.extracted_by,
               (d.current_version_id = e.version_id AND d.removed_at_run IS NULL) AS live
        FROM evidence e
        JOIN document_versions v ON v.id = e.version_id
        JOIN documents d ON d.id = v.document_id
        WHERE e.entity_type = :t AND e.entity_id = ANY(:ids)
        ORDER BY e.entity_id, live DESC, e.id"""), {"t": entity_type, "ids": ids}).mappings()
    out: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d.pop("entity_id"), []).append(d)
    return out


# ----------------------------------------------------------------- detail


def project_detail(s: Session, slug: str) -> dict[str, Any] | None:
    row = s.execute(text(f"""
        SELECT {_CARD_COLUMNS}, p.description, p.major_intersection, p.site_area_acres,
               p.current_use, p.proposed_use, p.surrounding_use, p.cms_updated_at,
               c.name AS councillor, c.profile_url AS councillor_url, pp.name AS parent_name
        FROM projects p
        LEFT JOIN initiatives i ON i.id = p.initiative_id
        LEFT JOIN projects pp ON pp.id = p.parent_project_id
        LEFT JOIN councillors c ON c.id = p.councillor_id
        WHERE p.slug = :slug"""), {"slug": slug}).mappings().first()
    if row is None:
        return None
    pid = row["id"]
    detail = dict(row)
    detail.pop("id")

    detail["children"] = [dict(r) for r in s.execute(text(
        "SELECT slug, name, kind, phase_label FROM projects WHERE parent_project_id = :p "
        "ORDER BY name"), {"p": pid}).mappings()]

    facts = [dict(r) for r in s.execute(text("""
        SELECT id, field, value_text AS value, value_num, unit, status, extracted_by, as_of
        FROM facts WHERE project_id = :p AND status IN ('current', 'conflicting')
        ORDER BY field, status, id"""), {"p": pid}).mappings()]
    ev = _evidence(s, "fact", [f["id"] for f in facts])
    for f in facts:
        f["evidence"] = ev.get(f.pop("id"), [])
    detail["facts"] = facts

    milestones = [dict(r) for r in s.execute(text("""
        SELECT id, type, label, title, date, date_precision, end_date, status, extracted_by
        FROM milestones WHERE project_id = :p AND retracted_run IS NULL
        ORDER BY date NULLS LAST, id"""), {"p": pid}).mappings()]
    ev = _evidence(s, "milestone", [m["id"] for m in milestones])
    for m in milestones:
        m["evidence"] = ev.get(m.pop("id"), [])
    detail["milestones"] = milestones

    detail["procurement"] = _signals(s, "s.project_id = :p", {"p": pid})
    # CMS relations, plus news whose text or attached minutes yielded claims about the project
    detail["news"] = [dict(r) for r in s.execute(text("""
        SELECT n.title, n.published_at, n.kind, n.url,
               string_agg(DISTINCT l.method, ',') AS linked_by
        FROM links l JOIN news_articles n ON n.id = l.from_id
        WHERE l.from_type = 'news' AND l.to_type = 'project' AND l.to_id = :p
        GROUP BY n.id ORDER BY n.published_at DESC NULLS LAST"""), {"p": pid}).mappings()]
    detail["context_records"] = _claims_view(s, """
        c.project_id IS DISTINCT FROM :p AND c.link_status IN ('external', 'unresolved')
        AND EXISTS (SELECT 1 FROM evidence e2 JOIN document_versions v2 ON v2.id = e2.version_id
                    JOIN links l ON l.rel = 'has_file' AND l.from_type = 'project'
                                AND l.from_id = :p AND l.to_id = v2.document_id
                    WHERE e2.entity_type = c.t AND e2.entity_id = c.id)""", {"p": pid}, 30)
    detail["files"] = [dict(r) for r in s.execute(text("""
        SELECT coalesce(d.title, regexp_replace(d.url, '^.*/', '')) AS title, d.url, d.kind,
               v.parse_status
        FROM links l JOIN documents d ON d.id = l.to_id
        LEFT JOIN document_versions v ON v.id = d.current_version_id
        WHERE l.from_type = 'project' AND l.from_id = :p AND l.rel = 'has_file'
        ORDER BY d.title NULLS LAST, d.url"""), {"p": pid}).mappings()]
    return detail


def project_history(s: Session, slug: str) -> dict[str, Any] | None:
    pid = s.execute(text("SELECT id FROM projects WHERE slug = :slug"), {"slug": slug}).scalar()
    if pid is None:
        return None
    versions = [dict(r) for r in s.execute(text("""
        SELECT pv.run_id, r.started_at AS run_started_at, pv.diff
        FROM project_versions pv JOIN crawl_runs r ON r.id = pv.run_id
        WHERE pv.project_id = :p ORDER BY pv.id"""), {"p": pid}).mappings()]
    facts = [dict(r) for r in s.execute(text("""
        SELECT f.field, f.value_text AS value, f.status, f.valid_from_run, f.valid_to_run,
               w.value_text AS superseded_by_value
        FROM facts f LEFT JOIN facts w ON w.id = f.superseded_by
        WHERE f.project_id = :p AND f.status IN ('superseded', 'retracted')
        ORDER BY f.field, f.valid_to_run"""), {"p": pid}).mappings()]
    return {"slug": slug, "versions": versions, "past_facts": facts}


# --------------------------------------------------------- other listings


def _signals(s: Session, where: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(r) for r in s.execute(text(f"""
        SELECT s.id, s.kind, s.title, s.status, s.reference_number, s.issuer, s.counterparty,
               s.scope, s.value, s.currency, s.opens_at, s.closes_at, s.external_url,
               s.extracted_by, s.first_seen_run, s.retracted_run,
               p.slug AS project_slug, p.name AS project_name, p.address AS project_address,
               p.ward AS project_ward, p.phase_label AS project_phase,
               p.latitude AS project_latitude, p.longitude AS project_longitude
        FROM procurement_signals s LEFT JOIN projects p ON p.id = s.project_id
        WHERE {where}
        ORDER BY (s.status IN ('open', 'upcoming')) DESC, s.closes_at NULLS LAST, s.id"""),
        params).mappings()]
    ev = _evidence(s, "procurement_signal", [r["id"] for r in rows])
    for r in rows:
        r["evidence"] = ev.get(r.pop("id"), [])
    return rows


def procurement(s: Session, status: str | None, kind: str | None,
                project: str | None) -> list[dict[str, Any]]:
    where, params = ["TRUE"], {}
    if status == "open":
        where.append("s.status IN ('open', 'upcoming') AND s.retracted_run IS NULL")
    elif status:
        where.append("s.status = :status")
        params["status"] = status
    if kind:
        where.append("s.kind = :kind")
        params["kind"] = kind
    if project:
        where.append("p.slug = :project")
        params["project"] = project
    return _signals(s, " AND ".join(where), params)


def news(s: Session, q: str | None, kind: str | None, project: str | None,
         since: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = ["TRUE"], {"limit": limit, "offset": offset}
    rank = "0.0"
    if q:
        params["q"] = q
        where.append(f"n.search_tsv @@ {TSQ}")
        rank = (f"ts_rank_cd(n.search_tsv, {TSQ}, 32) + "
                f"ts_rank_cd(n.search_tsv, {TSQ_ALL}, 32)")
    if kind:
        where.append("n.kind = :kind")
        params["kind"] = kind
    if since:
        where.append("n.published_at >= CAST(:since AS date)")
        params["since"] = since
    if project:
        where.append("""EXISTS (SELECT 1 FROM links l JOIN projects p ON p.id = l.to_id
            WHERE l.from_type = 'news' AND l.from_id = n.id AND l.to_type = 'project'
              AND p.slug = :project)""")
        params["project"] = project
    rows = s.execute(text(f"""
        SELECT n.title, n.published_at, n.kind, n.topics, n.url,
               left(n.body_text, 300) AS excerpt, ({rank}) AS score,
               (SELECT array_agg(p.slug ORDER BY p.slug) FROM links l
                  JOIN projects p ON p.id = l.to_id
                WHERE l.from_type = 'news' AND l.from_id = n.id AND l.to_type = 'project')
                 AS projects
        FROM news_articles n
        WHERE {" AND ".join(where)}
        ORDER BY score DESC, n.published_at DESC NULLS LAST
        LIMIT :limit OFFSET :offset"""), params).mappings()
    return [{**dict(r), "score": round(float(r["score"]), 4)} for r in rows]


_CLAIMS = """
    SELECT 'fact' t, id, project_id, project_hint, link_status, link_confidence,
           field AS kind, coalesce(value_text, '') AS what, as_of AS date
      FROM facts WHERE status IN ('current', 'conflicting')
    UNION ALL SELECT 'milestone', id, project_id, project_hint, link_status, link_confidence,
           type, coalesce(title, ''), date::text FROM milestones WHERE retracted_run IS NULL
    UNION ALL SELECT 'procurement_signal', id, project_id, project_hint, link_status,
           link_confidence, kind, title, coalesce(closes_at, opens_at)
      FROM procurement_signals WHERE retracted_run IS NULL"""


def _claims_view(s: Session, where: str, params: dict[str, Any],
                 limit: int) -> list[dict[str, Any]]:
    rows = [dict(r) for r in s.execute(text(f"""
        SELECT c.t AS record_type, c.id, c.kind, c.what, c.date, c.project_hint,
               c.link_status, c.link_confidence
        FROM ({_CLAIMS}) c WHERE {where}
        ORDER BY c.date DESC NULLS LAST, c.id LIMIT :limit"""),
        {**params, "limit": limit}).mappings()]
    by_type: dict[str, list[int]] = {}
    for r in rows:
        by_type.setdefault(r["record_type"], []).append(r["id"])
    ev = {t: _evidence(s, t, ids) for t, ids in by_type.items()}
    for r in rows:
        r["evidence"] = ev[r["record_type"]].get(r.pop("id"), [])
    return rows


def organization(s: Session, q: str | None, limit: int) -> list[dict[str, Any]]:
    """CreateTO / Build Toronto as an organization: board decisions, budgets, policy."""
    where, params = "c.link_status = 'organization'", {}
    if q:
        where += " AND (c.what ILIKE :q OR c.kind ILIKE :q)"
        params["q"] = f"%{q}%"
    return _claims_view(s, where, params, limit)


def runs(s: Session, limit: int) -> list[dict[str, Any]]:
    return [dict(r) for r in s.execute(text("""
        SELECT id, mode, status, started_at, finished_at, git_sha, stats
        FROM crawl_runs ORDER BY id DESC LIMIT :n"""), {"n": limit}).mappings()]


def stats(s: Session) -> dict[str, Any]:
    counts = s.execute(text("""
        SELECT
          (SELECT count(*) FROM projects WHERE kind = 'project') AS projects,
          (SELECT count(*) FROM projects WHERE kind = 'block') AS project_blocks,
          (SELECT count(*) FROM news_articles) AS news,
          (SELECT count(*) FROM documents WHERE removed_at_run IS NULL) AS documents,
          (SELECT count(*) FROM facts WHERE status IN ('current', 'conflicting')) AS facts,
          (SELECT count(*) FROM milestones WHERE retracted_run IS NULL) AS milestones,
          (SELECT count(*) FROM procurement_signals) AS procurement_signals,
          (SELECT count(*) FROM procurement_signals WHERE status IN ('open', 'upcoming')
             AND retracted_run IS NULL) AS open_procurement,
          (SELECT max(id) FROM crawl_runs) AS latest_run""")).mappings().one()
    phases = s.execute(text("""
        SELECT coalesce(phase_label, phase) AS phase, count(*) AS n FROM projects
        WHERE kind = 'project' GROUP BY 1 ORDER BY 2 DESC""")).all()
    return {**dict(counts), "projects_by_phase": {p: n for p, n in phases}}


def facets(s: Session) -> dict[str, Any]:
    """Values for search filters."""
    return {
        "phases": [dict(r) for r in s.execute(text(
            "SELECT DISTINCT phase AS key, phase_label AS label FROM projects "
            "WHERE phase IS NOT NULL ORDER BY label")).mappings()],
        "wards": [w for (w,) in s.execute(text(
            "SELECT DISTINCT ward FROM projects WHERE ward IS NOT NULL ORDER BY ward"))],
        "types": [t for (t,) in s.execute(text(
            "SELECT DISTINCT unnest(types) AS t FROM projects ORDER BY t"))],
        "initiatives": [dict(r) for r in s.execute(text(
            "SELECT slug, name FROM initiatives ORDER BY name")).mappings()],
    }
