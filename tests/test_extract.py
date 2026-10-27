from datetime import date

import pytest
from sqlalchemy import func, select

from createto_info.db.models import (
    CrawlRun,
    Document,
    DocumentVersion,
    Evidence,
    Extraction,
    Fact,
    ProcurementSignal,
    Project,
)
from createto_info.extract.compare import _fact_gist
from createto_info.extract.llm_extract import (
    PROMPT_VERSION,
    SCHEMA,
    Unit,
    apply_outputs,
    parse_when,
    signal_status,
    verify,
)
from createto_info.extract.pdf import pdf_date
from createto_info.llm.openrouter import parse_json

PAGES = [(1, "CreateTO initiates market offering. The site is 1.26 acres."),
         (2, "TOTAL SITE AREA (ACRES)\nZONING\nLEASEHOLD\n2.86 acres\nRM (u120)")]


def _unit(**kw) -> Unit:
    return Unit(kind="pdf_text", version_id=1, title="Brochure", url="https://x/b.pdf",
                doc_date=date(2024, 3, 1), pages=PAGES, **kw)


def test_verify_exact_fuzzy_and_table_segments():
    u = _unit()
    assert verify("The site is 1.26 acres", u, 2) == 1, "found on page 1 even if page 2 claimed"
    assert verify("the  site is 1.26 Acres.", u, None) == 1
    assert verify("TOTAL SITE AREA (ACRES)\n2.86 acres", u, 2) == 2, "label/value split by table"
    assert verify("The site is 9.99 acres", u, 1) is False
    assert verify("acres", u, 1) is False, "too short to be evidence"


@pytest.mark.parametrize("value,expected", [
    ("2024-03-15", (date(2024, 3, 15), "day")), ("2024-03", (date(2024, 3, 1), "month")),
    ("2024-Q2", (date(2024, 4, 1), "quarter")), ("2024", (date(2024, 1, 1), "year")),
    ("next spring", (None, None)), (None, (None, None)), ("2024-13", (None, None)),
])
def test_parse_when(value, expected):
    assert parse_when(value) == expected


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": [1, 2]} hope that helps') == {"a": [1, 2]}


def test_schema_is_strict():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(SCHEMA)


def test_pdf_date_and_fact_gist():
    assert pdf_date({"creationDate": "D:20240317120000-04'00'"}) == date(2024, 3, 17)
    assert pdf_date({}) is None
    assert _fact_gist({"value_text": "88 permanently affordable homes"}) == "88"
    assert _fact_gist({"value_text": "23,300 sq. ft."}) == "23300"
    assert _fact_gist({"value_text": "St. Clare's Housing Society"}) == "st. clare's housing"


@pytest.mark.db
def test_apply_outputs_writes_verified_claims_only(db_session):
    s = db_session
    run = CrawlRun(mode="test")
    project = Project(cms_uuid="p", slug="p", url="https://createto.ca/projects/p",
                      name="610 Bay Street", types=[])
    doc = Document(url="https://x/b.pdf", host="x", kind="pdf", scope="asset")
    s.add_all([run, project, doc])
    s.flush()
    ver = DocumentVersion(document_id=doc.id, content_hash="h", storage_path="x", run_id=run.id)
    s.add(ver)
    s.flush()
    doc.current_version_id = ver.id
    unit = _unit(candidates=[{"id": project.id, "name": project.name}])
    unit.version_id = ver.id
    output = {
        "facts": [
            {"project_id": project.id, "project_hint": None, "field": "site_area",
             "label": None, "value_text": "1.26 acres", "value_num": 1.26, "unit": "acres",
             "as_of": None, "evidence": "The site is 1.26 acres.", "page": 1,
             "confidence": 0.9},
            {"project_id": project.id, "project_hint": None, "field": "residential_units",
             "label": None, "value_text": "900 homes", "value_num": 900, "unit": "units",
             "as_of": None, "evidence": "The project will deliver 900 homes.", "page": 1,
             "confidence": 0.9},
            {"project_id": 999999, "project_hint": None, "field": "site_area", "label": None,
             "value_text": "2.86 acres", "value_num": 2.86, "unit": "acres", "as_of": "2023",
             "evidence": "TOTAL SITE AREA (ACRES)\n2.86 acres", "page": 2, "confidence": 0.8}],
        "milestones": [],
        "procurement_signals": [
            {"project_id": project.id, "project_hint": None, "kind": "market_offering",
             "title": "Market offering", "reference_number": None, "issuer": "CreateTO",
             "counterparty": None, "scope": None, "value": None, "currency": None,
             "opens_at": None, "closes_at": None, "status": "open", "external_url": None,
             "evidence": "CreateTO initiates market offering.", "page": 1, "confidence": 0.9}],
        "page_description": None,
    }
    ext = Extraction(input_hash="k", task="extract", prompt_version=PROMPT_VERSION,
                     model="m", version_id=ver.id, output=output, status="ok")
    s.add(ext)
    s.flush()

    stats = apply_outputs(s, [(unit, ext)], "m", run.id, dry_run=False)
    assert stats["fact_rejected_evidence"] == 1, "fabricated 900 homes is rejected"
    assert stats["fact_accepted"] == 2 and stats["signal_accepted"] == 1
    facts = {f.value_text: f for f in s.scalars(select(Fact))}
    assert set(facts) == {"1.26 acres", "2.86 acres"}
    assert facts["1.26 acres"].project_id == project.id
    assert facts["1.26 acres"].as_of == "2024-03-01", "document date when the claim has none"
    assert facts["2.86 acres"].project_id is None, "project id not among candidates"
    assert facts["2.86 acres"].extracted_by == f"model:m@{PROMPT_VERSION}"
    ev = s.scalar(select(Evidence).where(Evidence.entity_type == "fact",
                                         Evidence.entity_id == facts["2.86 acres"].id))
    assert (ev.page_no, ev.extraction_id) == (2, ext.id)
    assert s.scalar(select(func.count()).select_from(ProcurementSignal)) == 1


@pytest.mark.parametrize("status,closes,doc,expected", [
    ("open", "2026-11-02", date(2026, 10, 1), "open"),        # live, closes in future
    ("open", "2022-06-01", date(2022, 5, 16), "closed"),      # closing date passed
    ("open", None, date(2019, 4, 1), "unknown"),              # old doc: can't confirm
    ("upcoming", None, date(2026, 9, 1), "upcoming"),         # recent doc
    ("awarded", None, date(2019, 4, 1), "awarded"),           # historical facts stay
    ("bogus", None, date(2026, 9, 1), "unknown"),
])
def test_signal_status_ages_out(status, closes, doc, expected):
    assert signal_status(status, closes, doc, today=date(2026, 10, 27)) == expected
