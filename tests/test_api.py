import pytest
from fastapi.testclient import TestClient
from test_materializer import PARENT, _crawl

from createto_info.api.app import app
from createto_info.config import load_config

pytestmark = pytest.mark.db


@pytest.fixture
def client(db_session, tmp_path):
    cfg = load_config()
    cfg.storage.raw_dir = tmp_path / "raw"
    _crawl(db_session, cfg)
    db_session.commit()
    return TestClient(app)


def test_search_by_text_address_and_distance(client):
    names = [p["slug"] for p in client.get("/projects", params={"q": "big site"}).json()]
    assert names[0] == "big-site"
    hit = client.get("/projects", params={"q": "1125 Dundas St W"}).json()
    # market offering + MERX tender; the RFP's status is unknown, so it isn't counted as open
    assert hit[0]["slug"] == "big-site" and hit[0]["open_procurement"] == 2
    near = client.get("/projects", params={"near": "43.65,-79.42", "radius_km": 1}).json()
    assert [p["slug"] for p in near] == ["big-site"] and near[0]["distance_km"] == 0.0
    assert client.get("/projects", params={"near": "nonsense"}).status_code == 422


def test_filters(client):
    offering = client.get("/projects", params={"phase": "Market Offering"}).json()
    assert [p["slug"] for p in offering] == ["big-site"]
    assert client.get("/projects", params={"type": "parksOpenSpaces"}).json()[0]["kind"] == "block"
    assert client.get("/projects", params={"ward": "nowhere"}).json() == []


def test_project_detail_cites_every_fact(client):
    d = client.get("/projects/big-site").json()
    assert d["cms_updated_at"] is None or isinstance(d["cms_updated_at"], str)
    assert {c["slug"] for c in d["children"]} == {"child", "big-site#block-2"}
    assert d["facts"] and all(f["evidence"] and f["evidence"][0]["live"] for f in d["facts"])
    assert {m["label"] for m in d["milestones"]} >= {"marketOffering"}
    assert {s["kind"] for s in d["procurement"]} == {"market_offering", "rfp", "solicitation"}
    assert d["news"][0]["title"].startswith("Corporate Board Meeting")
    assert d["files"][0]["title"] == "Big Site Request for Proposals (RFP)"
    assert client.get("/projects/missing").status_code == 404


def test_history_after_phase_change(client, db_session, tmp_path):
    cfg = load_config()
    cfg.storage.raw_dir = tmp_path / "raw"
    _crawl(db_session, cfg, parent_phase="deliveryAndConstruction")
    db_session.commit()
    h = client.get("/projects/big-site/history").json()
    assert h["versions"][-1]["diff"]["phase"] == ["marketOffering", "deliveryAndConstruction"]
    assert {(f["field"], f["value"], f["superseded_by_value"]) for f in h["past_facts"]} >= {
        ("phase", "marketOffering", "deliveryAndConstruction")}


def test_procurement_news_search_and_stats(client):
    open_ = client.get("/procurement").json()
    assert {s["kind"] for s in open_} == {"market_offering", "solicitation"}
    assert all(s["project_slug"] == "big-site" for s in open_)
    news = client.get("/news", params={"kind": "board_meeting", "project": "big-site"}).json()
    assert len(news) == 1 and news[0]["projects"] == ["big-site"]
    both = client.get("/search", params={"q": "minutes"}).json()
    assert both["news"] and both["news"][0]["title"].startswith("Corporate Board")
    stats = client.get("/stats").json()
    assert stats["projects"] == 2 and stats["project_blocks"] == 1 and stats["latest_run"]
    assert client.get("/").status_code == 200
    assert PARENT  # fixture data used above


def test_organization_and_context_records(client, db_session):
    from sqlalchemy import select

    from createto_info.db.models import Fact, Project

    parent = db_session.scalar(select(Project).where(Project.slug == "big-site"))
    org, ctx = db_session.scalars(
        select(Fact).where(Fact.project_id == parent.id).order_by(Fact.id).limit(2)).all()
    org.project_id, org.link_status = None, "organization"
    ctx.project_id, ctx.link_status, ctx.project_hint = None, "external", "Another site"
    db_session.commit()
    got = client.get("/organization").json()
    assert [r["what"] for r in got] == [org.value_text] and got[0]["evidence"]
    d = client.get("/projects/big-site").json()
    assert "news" in d and all("linked_by" in n for n in d["news"])
    assert d["context_records"] == [] or d["context_records"][0]["project_hint"]
