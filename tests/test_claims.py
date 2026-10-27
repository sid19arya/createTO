"""Reconciliation rules from PLAN.md §8."""

from datetime import date

import pytest
from sqlalchemy import func, select

from createto_info.db.models import (
    CrawlRun,
    Document,
    DocumentVersion,
    Evidence,
    Fact,
    Milestone,
    ProcurementSignal,
    Project,
)
from createto_info.parse.claims import Claims, Source, value_key

pytestmark = pytest.mark.db


@pytest.fixture
def env(db_session):
    s = db_session
    run = CrawlRun(mode="test")
    s.add(run)
    s.flush()
    project = Project(cms_uuid="p", slug="p", url="https://createto.ca/projects/p", name="P",
                      types=[])
    s.add(project)
    s.flush()
    return s, run.id, project.id


def _doc(s, url, run_id, content="v1"):
    """A document whose current version is a fresh version; returns version id."""
    doc = s.scalar(select(Document).where(Document.url == url))
    if doc is None:
        doc = Document(url=url, host="x", kind="pdf", scope="asset")
        s.add(doc)
        s.flush()
    v = DocumentVersion(document_id=doc.id, run_id=run_id, content_hash=f"{url}{content}",
                        storage_path="x")
    s.add(v)
    s.flush()
    doc.current_version_id = v.id
    s.flush()
    return v.id


def _statuses(s, pid, field):
    return dict(s.execute(select(Fact.value_text, Fact.status)
                          .where(Fact.project_id == pid, Fact.field == field)).all())


def test_value_key_normalization():
    assert value_key("  Mixed-use. ", None, None) == "mixed-use"
    assert value_key(None, 800.0, "Units") == "800 units"


def test_same_value_from_two_sources_is_one_fact(env):
    s, run, pid = env
    c = Claims(s, run)
    page = _doc(s, "https://createto.ca/projects/p", run)
    pdf = _doc(s, "https://a/report.pdf", run)
    c.fact(project_id=pid, field="site_area", value_text="4.24 acres",
           source=Source(page, "cms", locator="content.siteArea"))
    c.fact(project_id=pid, field="site_area", value_text="4.24 Acres", as_of=date(2024, 1, 1),
           source=Source(pdf, "model:inkling@extract_v1", page_no=3, excerpt="4.24 acres"))
    c.resolve_facts()
    (fact,) = s.scalars(select(Fact)).all()
    assert fact.status == "current" and fact.extracted_by == "cms"
    assert s.scalar(select(func.count()).select_from(Evidence)) == 2


def test_cms_beats_newer_document(env):
    s, run, pid = env
    c = Claims(s, run)
    page = _doc(s, "https://createto.ca/projects/p", run)
    pdf = _doc(s, "https://a/report.pdf", run)
    c.fact(project_id=pid, field="proposed_use", value_text="Mixed-use",
           as_of=date(2023, 11, 1), source=Source(page, "cms"))
    c.fact(project_id=pid, field="proposed_use", value_text="Residential",
           as_of=date(2026, 3, 1), source=Source(pdf, "model:x"))
    c.resolve_facts()
    assert _statuses(s, pid, "proposed_use") == {"Mixed-use": "current",
                                                 "Residential": "superseded"}


def test_latest_document_wins_and_ties_conflict(env):
    s, run, pid = env
    c = Claims(s, run)
    old = _doc(s, "https://a/2019.pdf", run)
    new = _doc(s, "https://a/2024.pdf", run)
    c.fact(project_id=pid, field="residential_units", value_text="800 homes", value_num=800,
           unit="units", as_of=date(2019, 5, 1), source=Source(old, "model:x"))
    c.fact(project_id=pid, field="residential_units", value_text="~900 homes", value_num=900,
           unit="units", as_of=date(2024, 6, 1), source=Source(new, "model:x"))
    c.resolve_facts()
    assert _statuses(s, pid, "residential_units") == {"800 homes": "superseded",
                                                      "~900 homes": "current"}

    same_day = _doc(s, "https://a/2024b.pdf", run)
    c.fact(project_id=pid, field="residential_units", value_text="950", value_num=950,
           unit="units", as_of=date(2024, 6, 1), source=Source(same_day, "model:x"))
    c.resolve_facts()
    st = _statuses(s, pid, "residential_units")
    assert st["~900 homes"] == st["950"] == "conflicting"


def test_fact_retracted_when_its_only_source_changes(env):
    s, run, pid = env
    c = Claims(s, run)
    page_v1 = _doc(s, "https://createto.ca/projects/p", run)
    c.fact(project_id=pid, field="ward", value_text="Parkdale-High Park",
           source=Source(page_v1, "cms"))
    c.resolve_facts()
    _doc(s, "https://createto.ca/projects/p", run, content="v2")  # field gone in v2
    c.resolve_facts()
    assert _statuses(s, pid, "ward") == {"Parkdale-High Park": "retracted"}
    assert s.scalar(select(Fact.valid_to_run)) == run


def test_signal_matching_and_fill_empty_merge(env):
    s, run, pid = env
    c = Claims(s, run)
    listing = _doc(s, "https://www.merx.com/open", run)
    pdf = _doc(s, "https://a/board.pdf", run)
    rule = c.signal(project_id=pid, kind="solicitation", title="Testing & Inspection Services",
                    status="open", reference_number="4117", closes_at="2026-11-02",
                    source=Source(listing, "rule:merx"))
    # LLM restates it with a different title but the same closing date: same signal,
    # fills the empty `value`, cannot overwrite the rule's status
    merged = c.signal(project_id=pid, kind="solicitation",
                      title="Request for testing services at Dundas site", status="closed",
                      closes_at="2026-11-02", value=250000.0,
                      source=Source(pdf, "model:x", page_no=4))
    assert merged.id == rule.id
    assert (merged.status, merged.value, merged.extracted_by) == ("open", 250000.0, "rule:merx")
    assert s.scalar(select(func.count()).select_from(ProcurementSignal)) == 1

    other = c.signal(project_id=pid, kind="solicitation", title="Snow removal services",
                     source=Source(pdf, "model:x", page_no=9))
    assert other.id != rule.id


def test_milestone_matches_by_overlapping_dates(env):
    s, run, pid = env
    c = Claims(s, run)
    page = _doc(s, "https://createto.ca/projects/p", run)
    pdf = _doc(s, "https://a/r.pdf", run)
    a = c.milestone(project_id=pid, type="construction_start", date=date(2025, 1, 1),
                    date_precision="year", status="planned", source=Source(page, "cms"))
    b = c.milestone(project_id=pid, type="construction_start", date=date(2025, 6, 15),
                    date_precision="day", status="actual", title="Shovels in the ground",
                    source=Source(pdf, "model:x"))
    assert a.id == b.id
    assert (b.status, b.title) == ("planned", "Shovels in the ground"), "fill empty only"
    c2 = c.milestone(project_id=pid, type="construction_start", date=date(2027, 3, 1),
                     date_precision="month", source=Source(pdf, "model:x"))
    assert c2.id != a.id


def test_unsupported_claims_are_retracted(env):
    s, run, pid = env
    c = Claims(s, run)
    page = _doc(s, "https://createto.ca/projects/p", run)
    c.milestone(project_id=pid, type="phase_period", label="marketOffering",
                source=Source(page, "cms"))
    _doc(s, "https://createto.ca/projects/p", run, content="v2")
    (dead,) = c.retract_unsupported("milestone", Milestone)
    assert dead.retracted_run == run


def test_labelled_milestones_never_merge_by_date(env):
    s, run, pid = env
    c = Claims(s, run)
    page = _doc(s, "https://createto.ca/projects/p", run)
    a = c.milestone(project_id=pid, type="phase_period", label="preDevelopment",
                    date=date(2025, 1, 1), date_precision="year", source=Source(page, "cms"))
    b = c.milestone(project_id=pid, type="phase_period", label="developmentConcept",
                    date=date(2025, 1, 1), date_precision="year", source=Source(page, "cms"))
    assert a.id != b.id
