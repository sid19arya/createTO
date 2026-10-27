import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from createto_info.config import load_config
from createto_info.crawl.recorder import Fetched, Recorder
from createto_info.db.models import (
    Fact,
    Link,
    Milestone,
    NewsArticle,
    ProcurementSignal,
    Project,
    ProjectVersion,
)
from createto_info.parse.cms import Materializer

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parent / "fixtures"
PHASES = {"preDevelopment": "Pre-development", "marketOffering": "Market Offering",
          "deliveryAndConstruction": "Delivery and Construction"}
PARENT, CHILD, ARTICLE = "uuid-parent", "uuid-child", "uuid-article"
PDF = "https://a-us.storyblok.com/f/1/x/abc/rfp.pdf"


def _story(uuid, slug, name):
    return {"uuid": uuid, "slug": slug, "full_slug": f"projects/{slug}", "name": name,
            "content": {"component": "project"}}


def _sibling_blocks():
    return [{"component": "projectBlock", "_uid": "blk-2", "label": "Block 2",
             "address": "10 Example St", "phase": "preDevelopment", "type": ["parksOpenSpaces"],
             "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                 {"type": "text", "text": "Future park."}]}]}}]


def _parent(phase):
    return {"projectPhases": PHASES, "page": {
        "uuid": PARENT, "slug": "big-site", "full_slug": "projects/big-site", "name": "Big Site",
        "first_published_at": "2023-10-01T00:00:00Z",
        "content": {
            "component": "project", "phase": phase, "type": ["mixedUse", "housing"],
            "address": "1113-1117 Dundas Street West", "ward": "Spadina-Fort York",
            "councillorName": "Ausma Malik", "siteArea": "2 acres", "parentProject": "",
            "latitude": "43.65", "longitude": "-79.42",
            "summary": "A big site.",
            "timeline": [
                {"component": "projectTimelineStep", "phase": "preDevelopment",
                 "schedule": "2019 - 2021", "active": False},
                {"component": "projectTimelineStep", "phase": "marketOffering",
                 "schedule": "2026", "active": True},
                {"component": "projectTimelineStep", "phase": "deliveryAndConstruction",
                 "schedule": "To be determined", "active": False}],
            "blocks": _sibling_blocks() + [{"component": "projectBlockLink", "project": {
                "id": CHILD, "linktype": "story", "cached_url": "projects/child"}}],
            "resources": [{"component": "resources", "resources": [{
                "component": "resourcesItem", "documents": [
                    {"name": "Big Site Request for Proposals (RFP)", "filename": PDF}]}]}],
        }}}


def _child():
    # child pages repeat the parent's embedded blocks and embed the parent story itself
    return {"projectPhases": PHASES, "page": {
        "uuid": CHILD, "slug": "child", "full_slug": "projects/child", "name": "Child Site",
        "content": {"component": "project", "phase": "preDevelopment",
                    "parentProject": _story(PARENT, "big-site", "Big Site"),
                    "blocks": _sibling_blocks()}}}


def _article():
    return {"page": {
        "uuid": ARTICLE, "slug": "board", "full_slug": "news/board",
        "name": "Corporate Board Meeting - November 28, 2024",
        "first_published_at": "2024-01-07T00:00:00Z",
        "content": {"component": "article", "relatedProject": PARENT,
                    "author": {"name": "Comms"},
                    "topics": [{"name": "Board Meetings", "slug": "createto-board-meetings"}],
                    "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                        {"type": "text", "text": "Minutes."}]}]}}}}


def _page_html(props):
    data = {"props": {"pageProps": props}}
    return (b'<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data).encode() + b"</script></html>")


def _crawl(session, config, parent_phase="marketOffering"):
    rec = Recorder(session, config)
    for url, props in [("https://createto.ca/projects/big-site", _parent(parent_phase)),
                       ("https://createto.ca/projects/child", _child()),
                       ("https://createto.ca/news/board", _article())]:
        rec.record_fetch(Fetched(url, 200, _page_html(props), "text/html", None, None,
                                 "first_party", None, 1))
    rec.record_fetch(Fetched(PDF, 200, b"%PDF-1.4", "application/pdf", None, None, "asset",
                             None, 1))
    rec.record_fetch(Fetched("https://www.merx.com/public/solicitations/open?keywords=createto",
                             200, (FIXTURES / "merx_open_createto.html").read_bytes(),
                             "text/html", None, None, "external_1hop", None, 1))
    rec.finish(complete=True, sitemap_urls=set(), linked_urls=set(), scrapy_stats={})
    return Materializer(session, config, rec.run_id).run()


@pytest.fixture
def config(tmp_path):
    cfg = load_config()
    cfg.storage.raw_dir = tmp_path / "raw"
    return cfg


def _count(s, model, *where):
    return s.scalar(select(func.count()).select_from(model).where(*where))


def test_first_parse_builds_project_graph(db_session, config):
    stats = _crawl(db_session, config)
    s = db_session
    projects = {p.cms_uuid: p for p in s.scalars(select(Project))}
    assert set(projects) == {PARENT, CHILD, "blk-2"}, "sibling block must not be duplicated"
    parent, child, block = projects[PARENT], projects[CHILD], projects["blk-2"]
    assert child.parent_project_id == parent.id and block.parent_project_id == parent.id
    assert block.kind == "block" and block.name == "Big Site – Block 2"
    assert parent.phase_label == "Market Offering" and parent.site_area_acres == 2.0
    assert parent.types == ["housing", "mixedUse"]

    statuses = dict(s.execute(select(Milestone.label, Milestone.status)
                              .where(Milestone.project_id == parent.id)).all())
    assert statuses == {"preDevelopment": "actual", "marketOffering": "current",
                        "deliveryAndConstruction": "planned"}

    news = s.scalar(select(NewsArticle))
    assert news.kind == "board_meeting" and news.author == "Comms"
    assert news.published_at.date().isoformat() == "2024-11-28", "title date wins"
    assert _count(s, Link, Link.from_type == "news", Link.to_id == parent.id) == 1
    assert _count(s, Link, Link.rel == "has_file", Link.from_id == parent.id) == 1

    kinds = dict(s.execute(select(ProcurementSignal.kind, ProcurementSignal.project_id)).all())
    assert kinds == {"market_offering": parent.id, "rfp": parent.id,
                     "solicitation": parent.id}, "MERX row matched by address range"
    assert stats["fact_claims"] > 0 and stats["facts_current"] == 0, stats


def test_reparse_is_idempotent_and_phase_change_is_versioned(db_session, config):
    _crawl(db_session, config)
    again = Materializer(db_session, config, 1).run()
    assert not any(k.endswith("_new") for k in again), again

    _crawl(db_session, config, parent_phase="deliveryAndConstruction")
    s = db_session
    parent = s.scalar(select(Project).where(Project.cms_uuid == PARENT))
    latest = s.scalar(select(ProjectVersion).where(ProjectVersion.project_id == parent.id)
                      .order_by(ProjectVersion.id.desc()).limit(1))
    assert latest.diff["phase"] == ["marketOffering", "deliveryAndConstruction"]
    phase_facts = s.execute(select(Fact.value_text, Fact.status).where(
        Fact.project_id == parent.id, Fact.field == "phase").order_by(Fact.id)).all()
    assert phase_facts == [("marketOffering", "superseded"), ("deliveryAndConstruction", "current")]
    assert _count(s, Milestone, Milestone.type == "phase_change") == 1
    offering = s.scalar(select(ProcurementSignal).where(
        ProcurementSignal.kind == "market_offering"))
    assert offering.status == "closed"
