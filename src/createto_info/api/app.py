"""HTTP API over the dataset. Run `uv run createto serve`; docs at /docs."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from createto_info import __version__
from createto_info.api import queries
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


@app.get("/runs", summary="Crawl runs with metrics (requests, changes, LLM calls, cost)")
def runs(s: DB, limit: Annotated[int, Query(ge=1, le=100)] = 10) -> list[dict[str, Any]]:
    return queries.runs(s, limit)
