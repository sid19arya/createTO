from datetime import date

import pytest
from sqlalchemy import func, select

from createto_info.config import load_config
from createto_info.db.models import (
    CrawlRun,
    Document,
    DocumentVersion,
    Evidence,
    Fact,
    Link,
    Milestone,
    NewsArticle,
    Project,
)
from createto_info.link.linker import interpret, run_linking
from createto_info.parse.claims import Claims, Source

pytestmark = pytest.mark.db


def test_interpret_thresholds():
    assert interpret({"choice": "p12", "probabilities": {"p12": 0.9}}) == ("linked", 12, 0.9)
    assert interpret({"choice": "p12", "probabilities": {"p12": 0.65}})[0] == "unresolved"
    assert interpret({"choice": "organization", "probabilities": {"organization": 0.7}})[:2] \
        == ("organization", None)
    assert interpret({"choice": "external", "probabilities": {"external": 0.3}})[0] == \
        "unresolved"


@pytest.fixture
def world(db_session):
    s = db_session
    run = CrawlRun(mode="test")
    bi = Project(cms_uuid="bi", slug="bloor-islington", url="https://createto.ca/projects/bi",
                 name="Bloor-Islington", address="3326 Bloor Street West", types=[])
    s.add_all([run, bi])
    s.flush()
    report = Document(url="https://a/westwood.pdf", host="a", kind="pdf", scope="asset",
                      title="Westwood Theatre Lands Final Report")
    minutes = Document(url="https://a/minutes.pdf", host="a", kind="pdf", scope="asset")
    s.add_all([report, minutes])
    s.flush()
    versions = {}
    for d in (report, minutes):
        v = DocumentVersion(document_id=d.id, content_hash=d.url, storage_path="x")
        s.add(v)
        s.flush()
        d.current_version_id = v.id
        versions[d.url] = v.id
    news = NewsArticle(cms_uuid="n", slug="board", url="https://createto.ca/news/board",
                       title="Corporate Board Meeting", topics=[], initiative_ids=[])
    s.add(news)
    s.flush()
    s.add_all([Link(from_type="project", from_id=bi.id, to_type="document", to_id=report.id,
                    rel="has_file", method="cms"),
               Link(from_type="news", from_id=news.id, to_type="document", to_id=minutes.id,
                    rel="has_file", method="cms")])
    c = Claims(s, run.id)
    model = "model:m@extract_v1"
    # the CMS already knows Bloor-Islington's site area; the report repeats it unlinked
    c.fact(project_id=bi.id, field="site_area", value_text="19 acres",
           source=Source(versions[report.url], "cms"))
    c.fact(project_id=None, project_hint="Westwood Theatre Lands", field="site_area",
           value_text="19 acres", as_of=date(2017, 1, 1),
           source=Source(versions[report.url], model, page_no=3, excerpt="19 acres"))
    c.fact(project_id=None, project_hint="Westwood Theatre Lands", field="height_storeys",
           value_text="40 storeys", as_of=date(2017, 1, 1),
           source=Source(versions[report.url], model, page_no=9, excerpt="40 storeys"))
    c.milestone(project_id=None, project_hint="Westwood Theatre Lands", type="council_approval",
                title="Council adopts plan", date=date(2017, 6, 1), date_precision="month",
                status="actual", source=Source(versions[report.url], model, page_no=1))
    c.fact(project_id=None, field="budget", value_text="$12M operating budget",
           source=Source(versions[minutes.url], model, page_no=2, excerpt="$12M"))
    s.flush()
    return s, run.id, bi.id, news.id


def test_linking_moves_merges_and_classifies(world):
    s, run_id, bi_id, news_id = world
    asked = []

    async def decide(state, questions):
        asked.append(state)
        criteria = questions["subject"]["criteria"]
        assert "organization" in criteria and "external" in criteria
        if "Westwood" in state:
            assert f"p{bi_id}" in criteria, "project linking the document is a candidate"
            return {"answers": {"subject": {"choice": f"p{bi_id}",
                                            "probabilities": {f"p{bi_id}": 0.97}}}}
        return {"answers": {"subject": {"choice": "organization",
                                        "probabilities": {"organization": 0.8}}}}

    stats = run_linking(s, load_config(), run_id, decide=decide)
    assert stats["items"] == 2 and len(asked) == 2, "hint group decided once"
    area = s.scalars(select(Fact).where(Fact.field == "site_area")).all()
    assert len(area) == 1 and area[0].project_id == bi_id, "merged into the CMS fact"
    assert s.scalar(select(func.count()).select_from(Evidence).where(
        Evidence.entity_type == "fact", Evidence.entity_id == area[0].id)) == 2
    height = s.scalar(select(Fact).where(Fact.field == "height_storeys"))
    assert (height.project_id, height.link_status, height.link_method) == \
        (bi_id, "linked", "mammoth")
    assert s.scalar(select(Milestone.project_id)) == bi_id
    budget = s.scalar(select(Fact).where(Fact.field == "budget"))
    assert (budget.project_id, budget.link_status) == (None, "organization")
    assert s.scalar(select(func.count()).select_from(Link).where(
        Link.rel == "mentions", Link.from_type == "document", Link.to_id == bi_id)) == 1

    again = run_linking(s, load_config(), run_id, decide=decide)
    assert again["items"] == 0, "decided records are not asked again"


def test_low_confidence_stays_unresolved_and_news_mentions_derive(world):
    s, run_id, bi_id, news_id = world

    async def decide(state, questions):
        if "budget" in state:  # minutes linked from the board news page
            return {"answers": {"subject": {"choice": f"p{bi_id}",
                                            "probabilities": {f"p{bi_id}": 0.9}}}}
        return {"answers": {"subject": {"choice": f"p{bi_id}",
                                        "probabilities": {f"p{bi_id}": 0.4}}}}

    run_linking(s, load_config(), run_id, decide=decide)
    height = s.scalar(select(Fact).where(Fact.field == "height_storeys"))
    assert (height.project_id, height.link_status) == (None, "unresolved")
    assert height.link_confidence == pytest.approx(0.4)
    assert s.scalar(select(func.count()).select_from(Link).where(
        Link.from_type == "news", Link.from_id == news_id, Link.to_id == bi_id,
        Link.rel == "mentions")) == 1, "board page now mentions the project"


def test_confident_hints_become_aliases_and_uncertain_decisions_reopen(world):
    s, run_id, bi_id, news_id = world
    calls = []

    async def decide(state, questions):
        calls.append(state)
        return {"answers": {"subject": {"choice": f"p{bi_id}",
                                        "probabilities": {f"p{bi_id}": 0.95}}}}

    run_linking(s, load_config(), run_id, decide=decide)
    from createto_info.db.models import ProjectAlias
    learned = s.scalars(select(ProjectAlias.alias).where(ProjectAlias.kind == "learned")).all()
    assert "Westwood Theatre Lands" in learned

    budget = s.scalar(select(Fact).where(Fact.field == "budget"))
    budget.link_status, budget.link_method, budget.link_confidence = "external", "mammoth", 0.55
    s.flush()
    stats = run_linking(s, load_config(), run_id, decide=decide)
    assert stats["reopened"] == 1, "uncertain decision goes back for another look"
    # learned aliases change candidate labels, so that re-ask is a new question; once nothing
    # changes, reopening is answered from the cache
    n_before = len(calls)
    budget.link_status, budget.link_method, budget.link_confidence = "external", "mammoth", 0.55
    s.flush()
    run_linking(s, load_config(), run_id, decide=decide)
    assert len(calls) == n_before, "unchanged question -> cached answer, no new call"


@pytest.mark.parametrize("hint,blocked", [
    ("Modular Housing Initiative Phase 2", True), ("Downsview Area Secondary Plan area", True),
    ("Port Lands Planning Framework", True), ("Former Westwood Theatre site (Six Points)", False),
    ("Pinewood Studios, Port Lands", False), ("Planet Fitness lot", False),
])
def test_program_and_plan_hints_are_not_learned_as_aliases(hint, blocked):
    from createto_info.link.linker import NOT_AN_ALIAS

    assert bool(NOT_AN_ALIAS.search(hint)) is blocked
