import pytest
from test_materializer import _crawl

from createto_info.config import load_config

pytestmark = pytest.mark.db


def test_pipeline_stages_and_report(db_session, tmp_path, monkeypatch):
    from createto_info import report as report_mod
    from createto_info.db.models import CrawlRun
    from createto_info.pipeline import run_pipeline

    cfg = load_config()
    cfg.storage.raw_dir = tmp_path / "raw"
    cfg.storage.renders_dir = tmp_path / "renders"
    _crawl(db_session, cfg)
    db_session.commit()
    monkeypatch.setattr("createto_info.pipeline.session_scope", _scope(db_session))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    from createto_info import config as config_mod
    config_mod.get_env.cache_clear()

    run_id = run_pipeline(cfg, echo=lambda _: None, crawl=False)
    run = db_session.get(CrawlRun, run_id)
    stages = run.stats["stages"]
    assert set(stages) == {"crawl", "pdfs", "parse", "extract", "link", "index", "summary"}
    assert stages["summary"]["projects"] == 2 and stages["summary"]["billed_usd"] == 0
    assert stages["extract"]["units"] == 0 and stages["link"]["items"] == 0
    assert stages["index"]["project"] == 3

    # an interrupted run resumes from its missing stages; completed stages keep their stats
    run.stats = {**run.stats, "stages": {k: v for k, v in stages.items()
                                         if k not in ("link", "index", "summary")}}
    run.stats["stages"]["extract"]["marker"] = 1
    db_session.commit()
    assert run_pipeline(cfg, echo=lambda _: None, resume=True) == run_id
    db_session.refresh(run)
    assert run.stats["stages"]["extract"]["marker"] == 1
    assert run.stats["resumed"] == ["index", "link", "summary"]
    assert set(run.stats["stages"]) == set(stages)

    monkeypatch.setattr(report_mod, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(report_mod, "EXAMPLES", tmp_path / "reports" / "examples")
    monkeypatch.setattr(report_mod, "EXAMPLE_PROJECT", "big-site")
    path = report_mod.write_report(db_session, cfg)
    md = path.read_text(encoding="utf-8")
    assert "## Two runs" in md and f"run {run_id} (first)" in md
    assert "billed cost (actual)" in md and "## Reproduce" in md
    assert (tmp_path / "reports" / "examples" / "project_big-site.json").exists()


def _scope(session):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.commit()
    return scope
