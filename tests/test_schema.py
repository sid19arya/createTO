import pytest
from sqlalchemy import inspect, text

from createto_info.db.models import Base

EXPECTED_TABLES = {
    "crawl_runs", "documents", "document_versions", "pdf_pages", "fetch_log",
    "llm_calls", "extractions",
    "initiatives", "councillors", "projects", "project_aliases", "project_versions",
    "news_articles", "facts", "fact_sources", "milestones", "procurement_signals", "links",
    "search_chunks",
}


def test_metadata_matches_database_guide():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


@pytest.fixture
def engine():
    from createto_info.db import get_engine

    eng = get_engine()
    try:
        with eng.connect():
            pass
    except Exception:
        pytest.skip("Postgres not reachable; run `docker compose up -d db`")
    return eng


@pytest.mark.db
def test_migrated_database_has_all_tables(engine):
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names()) - {"alembic_version"}
    assert tables == EXPECTED_TABLES


@pytest.mark.db
def test_project_search_tsv_is_generated(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "insert into projects (cms_uuid, slug, url, name, address, types) "
            "values ('t-uuid', 't', 'https://x', 'Parkdale Hub', '1313 Queen Street West', '{}')"
        ))
        hit = conn.execute(text(
            "select name from projects where search_tsv @@ plainto_tsquery('english', 'queen')"
        )).scalar_one()
        assert hit == "Parkdale Hub"
        conn.execute(text("delete from projects where cms_uuid = 't-uuid'"))
