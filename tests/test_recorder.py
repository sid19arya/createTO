import json

import pytest
from sqlalchemy import func, select

from createto_info.config import load_config
from createto_info.crawl.recorder import Fetched, Recorder
from createto_info.db.models import Document, DocumentVersion, FetchLog

pytestmark = pytest.mark.db

URL = "https://createto.ca/projects/test"


def _html(phase: str, build_id: str) -> bytes:
    data = {"buildId": build_id, "props": {"pageProps": {
        "page": {"uuid": "u-1", "content": {"phase": phase}}}}}
    return (b'<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data).encode() + b"</script></html>")


def _fetched(body: bytes, url: str = URL, ctype: str = "text/html") -> Fetched:
    return Fetched(url=url, status=200, body=body, content_type=ctype, etag='"e1"',
                   last_modified=None, scope="first_party", referrer=None, elapsed_ms=5)


@pytest.fixture
def config(tmp_path):
    cfg = load_config()
    cfg.storage.raw_dir = tmp_path / "raw"
    return cfg


def _versions(s):
    return s.scalar(select(func.count()).select_from(DocumentVersion))


def test_new_unchanged_changed_lifecycle(db_session, config):
    r1 = Recorder(db_session, config)
    assert r1.record_fetch(_fetched(_html("marketOffering", "b1"))) == "new"
    r1.finish(complete=True, sitemap_urls={URL}, linked_urls=set(), scrapy_stats={})

    # same CMS record, different Next.js build: not a change, no new raw file
    r2 = Recorder(db_session, config)
    assert r2.record_fetch(_fetched(_html("marketOffering", "b2"))) == "unchanged"
    assert _versions(db_session) == 1
    assert len(list(config.storage.raw_dir.rglob("*.html"))) == 1

    r3 = Recorder(db_session, config)
    assert r3.record_fetch(_fetched(_html("preConstruction", "b2"))) == "changed"
    assert _versions(db_session) == 2
    doc = db_session.scalar(select(Document).where(Document.url == URL))
    assert doc.discovered_via is None or doc.discovered_via == "sitemap"
    assert doc.etag == '"e1"'


def test_not_modified_returns_stored_body_and_counts_unchanged(db_session, config):
    body = _html("leasing", "b1")
    r1 = Recorder(db_session, config)
    r1.record_fetch(_fetched(body))
    r2 = Recorder(db_session, config)
    assert URL in r2.validators()
    assert r2.record_not_modified(URL, None, 3) == body
    run = r2.finish(complete=True, sitemap_urls=set(), linked_urls={URL}, scrapy_stats={})
    assert run.stats["not_modified"] == 1 and run.stats["pages_unchanged"] == 1
    assert run.stats["documents_removed"] == 0


def test_complete_run_marks_missing_documents_removed(db_session, config):
    r1 = Recorder(db_session, config)
    r1.record_fetch(_fetched(b"%PDF-1.4 a", "https://a-us.storyblok.com/f/1/x/a/r.pdf",
                             "application/pdf"))
    r1.finish(complete=True, sitemap_urls=set(), linked_urls=set(), scrapy_stats={})

    partial = Recorder(db_session, config)
    partial.finish(complete=False, sitemap_urls=set(), linked_urls=set(), scrapy_stats={})
    assert db_session.scalar(select(Document.removed_at_run)) is None

    full = Recorder(db_session, config)
    run = full.finish(complete=True, sitemap_urls=set(), linked_urls=set(), scrapy_stats={})
    assert run.stats["documents_removed"] == 1
    assert db_session.scalar(select(Document.removed_at_run)) == full.run_id


def test_skips_are_logged_with_reason(db_session, config):
    r = Recorder(db_session, config)
    r.log("https://twitter.com/x", "skipped", "out_of_scope", URL)
    row = db_session.scalar(select(FetchLog))
    assert (row.outcome, row.reason, row.referrer_url) == ("skipped", "out_of_scope", URL)
