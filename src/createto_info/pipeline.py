"""`createto run`: the whole pipeline as one recorded run.

crawl -> pdfs -> parse -> extract -> link -> index. Each stage's wall time and stats are stored
under `crawl_runs.stats.stages.<stage>`, and a `summary` stage snapshots record counts and the
run's LLM calls and billed cost, so the run report (`createto report`) needs nothing but the
database. The crawl runs in a subprocess (Twisted's reactor starts once per process).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from createto_info.config import REPO_ROOT, Config
from createto_info.db.models import CrawlRun
from createto_info.db.session import session_scope

STAGES = ["crawl", "pdfs", "parse", "extract", "link", "index"]


def _record(s: Session, run: CrawlRun, name: str, fn: Callable[[], dict[str, Any]],
            echo: Callable[[str], None]) -> None:
    echo(f"[run {run.id}] {name}...")
    t0 = time.monotonic()
    stats = dict(fn())
    seconds = round(time.monotonic() - t0, 1)
    stages = dict((run.stats or {}).get("stages") or {})
    stages[name] = {"seconds": seconds, **{k: v for k, v in sorted(stats.items())}}
    run.stats = {**(run.stats or {}), "stages": stages}
    s.commit()
    echo(f"[run {run.id}] {name} done in {seconds}s")


def summary_counts(s: Session, run_id: int) -> dict[str, Any]:
    counts = dict(s.execute(text("""
        SELECT
          (SELECT count(*) FROM projects WHERE kind = 'project') AS projects,
          (SELECT count(*) FROM projects WHERE kind = 'block') AS project_blocks,
          (SELECT count(*) FROM news_articles) AS news_articles,
          (SELECT count(*) FROM documents WHERE removed_at_run IS NULL) AS documents,
          (SELECT count(*) FROM documents WHERE kind = 'pdf' AND removed_at_run IS NULL) AS pdfs,
          (SELECT count(*) FROM pdf_pages) AS pdf_pages,
          (SELECT count(*) FROM facts WHERE status IN ('current', 'conflicting')) AS facts_current,
          (SELECT count(*) FROM facts) AS facts_total,
          (SELECT count(*) FROM milestones WHERE retracted_run IS NULL) AS milestones,
          (SELECT count(*) FROM procurement_signals WHERE retracted_run IS NULL)
            AS procurement_signals,
          (SELECT count(*) FROM procurement_signals WHERE status IN ('open', 'upcoming')
             AND retracted_run IS NULL) AS open_opportunities,
          (SELECT count(*) FROM evidence) AS evidence_rows,
          (SELECT count(*) FROM search_chunks) AS search_chunks""")).mappings().one())
    llm = [dict(r) for r in s.execute(text("""
        SELECT model, task, count(*) AS calls, count(*) FILTER (WHERE status <> 'ok') AS failed,
               coalesce(sum(tokens_in), 0) AS tokens_in, coalesce(sum(tokens_out), 0) AS tokens_out,
               coalesce(sum(tokens_reasoning), 0) AS tokens_reasoning,
               round(coalesce(sum(cost_usd), 0)::numeric, 5)::float AS billed_usd,
               round(coalesce(sum(est_cost_usd), 0)::numeric, 5)::float AS est_usd
        FROM llm_calls WHERE run_id = :r GROUP BY 1, 2 ORDER BY 1, 2"""), {"r": run_id})
        .mappings()]
    paid = [x for x in llm if not x["model"].endswith(":free")]
    return {**counts, "llm": llm,
            "llm_calls": sum(x["calls"] for x in llm),
            "paid_calls": sum(x["calls"] for x in paid),
            "free_calls": sum(x["calls"] for x in llm) - sum(x["calls"] for x in paid),
            "billed_usd": round(sum(x["billed_usd"] for x in llm), 5),
            "est_usd": round(sum(x["est_usd"] for x in llm), 5)}


def run_pipeline(config: Config, echo: Callable[[str], None] = print,
                 crawl: bool = True, resume: bool = False) -> int:
    """`crawl=False` processes the latest crawl again (tests, or re-processing after a fix).
    `resume=True` finishes an interrupted run: only stages it hasn't recorded yet run, so the
    stats of completed stages (e.g. a paid extraction) are kept as they were."""
    started = time.monotonic()
    if resume:
        crawl = False
    if crawl:
        log = REPO_ROOT / "data" / "logs" / f"run_{datetime.now(UTC):%Y%m%dT%H%M%S}_crawl.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        echo(f"crawl (log: {log})")
        with open(log, "w", encoding="utf-8") as fh:
            proc = subprocess.run([sys.executable, "-m", "createto_info.cli", "crawl"],
                                  stdout=fh, stderr=subprocess.STDOUT, cwd=REPO_ROOT,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if proc.returncode != 0:
            raise RuntimeError(f"crawl failed; see {log}")

    from createto_info.chat.index import rebuild_chunks
    from createto_info.extract.llm_extract import apply_outputs, build_units, fetch_outputs
    from createto_info.extract.pdf import process_pdfs
    from createto_info.link.linker import run_linking
    from createto_info.parse.cms import Materializer

    with session_scope() as s:
        run = s.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1))
        crawl_stats = run.stats or {}
        if resume:
            done = set(crawl_stats.get("stages") or {})
            run.stats = {**crawl_stats, "resumed": sorted(set(STAGES + ["summary"]) - done)}
            echo(f"[run {run.id}] resuming; already recorded: {', '.join(sorted(done))}")
        else:
            done = {"crawl"}
            stages = {"crawl": {"seconds": crawl_stats.get("duration_s"),
                                **{k: v for k, v in crawl_stats.items() if k != "stages"}}}
            run.stats = {**crawl_stats, "pipeline": True, "stages": stages}
        s.commit()

        def record(name: str, fn: Callable[[], dict[str, Any]]) -> None:
            if name not in done:
                _record(s, run, name, fn, echo)

        record("pdfs", lambda: process_pdfs(s, config))
        record("parse", lambda: Materializer(s, config, run.id).run())

        def extract() -> dict[str, Any]:
            units = build_units(s, config)
            results, fstats = asyncio.run(fetch_outputs(s, config, units,
                                                        config.models.extract, run.id))
            return {"units": len(units), **fstats,
                    **apply_outputs(s, results, config.models.extract, run.id, dry_run=False)}

        record("extract", extract)
        record("link", lambda: run_linking(s, config, run.id))
        record("index", lambda: rebuild_chunks(s, run.id))
        record("summary", lambda: summary_counts(s, run.id))
        if resume:  # wall time spans the interruption; report the stages' own time instead
            seconds = sum(v.get("seconds") or 0 for v in run.stats["stages"].values())
        else:
            seconds = time.monotonic() - started
        run.stats = {**run.stats, "pipeline_seconds": round(seconds, 1)}
        s.commit()
        echo(f"run {run.id} finished in {run.stats['pipeline_seconds']}s")
        return run.id
