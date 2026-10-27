"""HTTP API over the dataset. Run `uv run createto serve`; docs at /docs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from createto_info import __version__
from createto_info import export as exporter
from createto_info.api import catalog, queries
from createto_info.db.session import session_scope

STATIC = Path(__file__).parent / "static"

app = FastAPI(
    title="CreateTO Info API",
    version=__version__,
    description=(
        "Projects, news, milestones and procurement signals collected from createto.ca and the "
        "public files it links to. Every fact carries its evidence: source URL, page/locator "
        "and a verbatim excerpt."
    ),
)


def db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


DB = Annotated[Session, Depends(db)]


def _near(near: str | None) -> tuple[float | None, float | None]:
    if not near:
        return None, None
    try:
        lat, lng = (float(x) for x in near.split(","))
    except ValueError as exc:
        raise HTTPException(422, "near must be 'lat,lng', e.g. 43.6411,-79.4332") from exc
    return lat, lng


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health(s: DB) -> dict[str, Any]:
    return {"ok": True, "latest_run": queries.stats(s)["latest_run"]}


@app.get("/stats", summary="Dataset counts")
def stats(s: DB) -> dict[str, Any]:
    return queries.stats(s)


@app.get("/facets", summary="Filter values (phases, wards, types, initiatives)")
def facets(s: DB) -> dict[str, Any]:
    return queries.facets(s)


@app.get("/projects", summary="Search and filter projects")
def projects(
    s: DB,
    q: Annotated[str, Query(description="Free text: name, address, ward, description")] = "",
    phase: Annotated[str | None, Query(description="Phase key or label")] = None,
    ward: str | None = None,
    type: Annotated[str | None, Query(description="Type key, e.g. affordableHousing")] = None,
    initiative: Annotated[str | None, Query(description="Initiative slug")] = None,
    near: Annotated[str | None, Query(description="'lat,lng' to rank/filter by distance")] = None,
    radius_km: float | None = None,
    include_excluded: Annotated[bool, Query(
        description="Include completed projects outside the 5-year window")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    lat, lng = _near(near)
    return queries.search_projects(s, queries.ProjectQuery(
        q=q, phase=phase, ward=ward, type=type, initiative=initiative, lat=lat, lng=lng,
        radius_km=radius_km, include_excluded=include_excluded, limit=limit, offset=offset))


@app.get("/projects/{slug:path}/history", summary="Version diffs and past fact values")
def project_history(slug: str, s: DB) -> dict[str, Any]:
    out = queries.project_history(s, slug)
    if out is None:
        raise HTTPException(404, f"no project {slug!r}")
    return out


@app.get("/projects/{slug:path}", summary="Project with facts, timeline, procurement, news, files")
def project(slug: str, s: DB) -> dict[str, Any]:
    out = queries.project_detail(s, slug)
    if out is None:
        raise HTTPException(404, f"no project {slug!r}")
    return out


@app.get("/search", summary="Search bar: projects and news in one call")
def search(
    s: DB,
    q: str = "",
    near: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    lat, lng = _near(near)
    return {
        "projects": queries.search_projects(
            s, queries.ProjectQuery(q=q, lat=lat, lng=lng, limit=limit)),
        "news": queries.news(s, q or None, None, None, None, limit=5, offset=0) if q else [],
    }


@app.get("/news", summary="News and board-meeting articles")
def news(
    s: DB,
    q: str | None = None,
    kind: Annotated[str | None, Query(
        description="board_meeting, project_update, news_release, insider, ...")] = None,
    project: Annotated[str | None, Query(description="Project slug")] = None,
    since: Annotated[str | None, Query(description="ISO date")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return queries.news(s, q, kind, project, since, limit, offset)


@app.get("/procurement", summary="Procurement signals (market offerings, RFPs, MERX tenders)")
def procurement(
    s: DB,
    status: Annotated[str | None, Query(
        description="'open' (open or upcoming, still supported), or an exact status")] = "open",
    kind: str | None = None,
    project: Annotated[str | None, Query(description="Project slug")] = None,
) -> list[dict[str, Any]]:
    return queries.procurement(s, status or None, kind, project)


@app.get("/organization", summary="CreateTO-level records: board decisions, budgets, policy")
def organization(s: DB, q: str | None = None,
                 limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:
    return queries.organization(s, q, limit)


@app.get("/facts", summary="Facts across projects, filterable, with evidence")
def facts(
    s: DB,
    field: Annotated[str | None, Query(description="e.g. affordable_units, architect")] = None,
    project: Annotated[str | None, Query(description="Project slug")] = None,
    status: Annotated[str | None, Query(
        description="default current+conflicting; or superseded, retracted, all")] = None,
    source: Annotated[str | None, Query(description="cms or documents")] = None,
    link_status: Annotated[str | None, Query(
        description="linked, organization, external, unresolved")] = None,
    q: Annotated[str | None, Query(description="Substring of the value")] = None,
    min_value: float | None = None,
    max_value: float | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return catalog.facts(s, field=field, project=project, status=status, source=source,
                         link_status=link_status, q=q, min_value=min_value,
                         max_value=max_value, limit=limit, offset=offset)


@app.get("/facts/fields", summary="Fact fields with counts")
def fact_fields(s: DB) -> list[dict[str, Any]]:
    return catalog.fact_fields(s)


@app.get("/facts/{fact_id}", summary="One fact: all evidence, what it replaced, alternatives")
def fact(fact_id: int, s: DB) -> dict[str, Any]:
    out = catalog.fact(s, fact_id)
    if out is None:
        raise HTTPException(404, f"no fact {fact_id}")
    return out


@app.get("/milestones", summary="Dated events across projects")
def milestones(
    s: DB,
    type: Annotated[str | None, Query(
        description="e.g. construction_start, council_approval, phase_period")] = None,
    status: Annotated[str | None, Query(description="actual, current, planned, ...")] = None,
    project: str | None = None,
    date_from: Annotated[str | None, Query(alias="from", description="ISO date")] = None,
    date_to: Annotated[str | None, Query(alias="to", description="ISO date")] = None,
    link_status: str | None = None,
    include_retracted: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return catalog.milestones(s, type=type, status=status, project=project,
                              date_from=date_from, date_to=date_to, link_status=link_status,
                              include_retracted=include_retracted, limit=limit, offset=offset)


@app.get("/documents", summary="Crawled pages and files")
def documents(
    s: DB,
    project: Annotated[str | None, Query(description="Listed on or mentioning")] = None,
    kind: Annotated[str | None, Query(description="page, pdf, doc, sitemap")] = None,
    scope: Annotated[str | None, Query(
        description="first_party, asset, external_1hop")] = None,
    q: Annotated[str | None, Query(description="Substring of title or URL")] = None,
    parse_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return catalog.documents(s, project=project, kind=kind, scope=scope, q=q,
                             parse_status=parse_status, limit=limit, offset=offset)


@app.get("/documents/{doc_id}", summary="Document: versions, pages, records extracted from it")
def document(doc_id: int, s: DB) -> dict[str, Any]:
    out = catalog.document(s, doc_id)
    if out is None:
        raise HTTPException(404, f"no document {doc_id}")
    return out


@app.get("/documents/{doc_id}/pages/{page_no}", summary="Text of one PDF page (citations)")
def document_page(doc_id: int, page_no: int, s: DB) -> dict[str, Any]:
    out = catalog.document_page(s, doc_id, page_no)
    if out is None:
        raise HTTPException(404, f"no page {page_no} for document {doc_id}")
    return out


@app.get("/initiatives", summary="Initiatives with their projects")
def initiatives(s: DB) -> list[dict[str, Any]]:
    return catalog.initiatives(s)


@app.get("/councillors", summary="Ward councillors with their projects")
def councillors(s: DB) -> list[dict[str, Any]]:
    return catalog.councillors(s)


@app.get("/export/{name}.jsonl", summary="Bulk export (JSON lines, evidence embedded)")
def export(name: str) -> StreamingResponse:
    if name not in exporter.ENTITIES:
        raise HTTPException(404, f"unknown export {name!r}; one of {sorted(exporter.ENTITIES)}")

    def stream():
        with session_scope() as s:
            yield from exporter.jsonl(s, name)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/runs/{run_id}", summary="One run: stats, fetch outcomes, LLM calls and cost")
def run(run_id: int, s: DB) -> dict[str, Any]:
    out = catalog.run(s, run_id)
    if out is None:
        raise HTTPException(404, f"no run {run_id}")
    return out


@app.get("/runs/{run_id}/fetch-log", summary="Fetched, skipped, blocked and failed URLs")
def fetch_log(
    run_id: int, s: DB,
    outcome: Annotated[str | None, Query(
        description="fetched, not_modified, skipped, blocked, failed, redirected")] = None,
    reason: str | None = None,
    host: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return catalog.fetch_log(s, run_id, outcome=outcome, reason=reason, host=host,
                             limit=limit, offset=offset)


@app.get("/runs", summary="Crawl runs with metrics (requests, changes, LLM calls, cost)")
def runs(s: DB, limit: Annotated[int, Query(ge=1, le=100)] = 10) -> list[dict[str, Any]]:
    return queries.runs(s, limit)
