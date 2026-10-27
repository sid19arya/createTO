import json

import pytest
from fastapi.testclient import TestClient
from test_materializer import _crawl

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


def test_facts_query_and_detail(client):
    facts = client.get("/facts", params={"project": "big-site", "field": "site_area"}).json()
    assert [f["value"] for f in facts] == ["2 acres"] and facts[0]["evidence_count"] == 1
    assert client.get("/facts", params={"source": "documents"}).json() == []
    detail = client.get(f"/facts/{facts[0]['id']}").json()
    assert detail["evidence"][0]["locator"] == "content.siteArea"
    assert "value_key" not in detail and detail["alternatives"] == []
    fields = {f["field"]: f for f in client.get("/facts/fields").json()}
    assert fields["phase"]["current"] == 3  # parent, child page, embedded block
    assert client.get("/facts/999999").status_code == 404


def test_milestones_filters(client):
    ms = client.get("/milestones", params={"project": "big-site"}).json()
    assert {m["label"] for m in ms} == {"preDevelopment", "marketOffering",
                                        "deliveryAndConstruction"}
    later = client.get("/milestones", params={"from": "2025-01-01"}).json()
    assert [m["label"] for m in later] == ["marketOffering"]


def test_documents_runs_and_fetch_log(client):
    docs = client.get("/documents", params={"kind": "pdf"}).json()
    assert len(docs) == 1 and docs[0]["listed_on"] == ["big-site"]
    detail = client.get(f"/documents/{docs[0]['id']}").json()
    assert len(detail["versions"]) == 1 and detail["versions"][0]["is_current"]
    run = client.get("/runs/1").json()
    assert any(o["outcome"] == "fetched" for o in run["fetch_outcomes"])
    log = client.get("/runs/1/fetch-log", params={"outcome": "fetched"}).json()
    assert len(log) == 5
    assert client.get("/runs/99").status_code == 404


def test_initiatives_councillors_and_export(client):
    assert client.get("/initiatives").json() == []
    (c,) = client.get("/councillors").json()
    assert c["name"] == "Ausma Malik" and c["project_slugs"] == ["big-site"]
    lines = client.get("/export/facts.jsonl").text.strip().splitlines()
    row = json.loads(lines[0])
    assert row["evidence"][0]["url"].startswith("https://createto.ca/")
    assert client.get("/export/secrets.jsonl").status_code == 404


def test_export_files_and_manifest(db_session, tmp_path):
    from createto_info.export import ENTITIES, export_all

    cfg = load_config()
    cfg.storage.raw_dir = tmp_path / "raw"
    _crawl(db_session, cfg)
    manifest = export_all(db_session, tmp_path / "out")
    assert set(manifest["counts"]) == set(ENTITIES)
    assert manifest["counts"]["projects"] == 3
    assert (tmp_path / "out" / "manifest.json").exists()
