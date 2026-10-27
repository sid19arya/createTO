"""Command line entry point: `uv run createto --help`."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from sqlalchemy import inspect, text

from createto_info.config import REPO_ROOT, get_config, get_env

app = typer.Typer(no_args_is_help=True, help="CreateTO crawler and dataset service.")
db_app = typer.Typer(no_args_is_help=True, help="Database management.")
config_app = typer.Typer(no_args_is_help=True, help="Configuration.")
app.add_typer(db_app, name="db")
app.add_typer(config_app, name="config")


def _alembic_config():
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations."""
    from alembic import command

    command.upgrade(_alembic_config(), revision)
    typer.echo(f"database at {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: str = typer.Argument(...)) -> None:
    """Roll back migrations (e.g. `base` to drop everything)."""
    from alembic import command

    command.downgrade(_alembic_config(), revision)
    typer.echo(f"database at {revision}")


@db_app.command("check")
def db_check() -> None:
    """Verify connectivity and list tables with row counts."""
    from createto_info.db import get_engine

    engine = get_engine()
    with engine.connect() as conn:
        version = conn.execute(text("select version()")).scalar_one()
        typer.echo(version.split(",")[0])
        for table in sorted(inspect(conn).get_table_names()):
            n = conn.execute(text(f'select count(*) from "{table}"')).scalar_one()
            typer.echo(f"  {table:24} {n}")


@app.command()
def crawl(
    limit: int = typer.Option(None, help="Stop scheduling after N documents (dev runs)."),
    skip_files: bool = typer.Option(False, help="Only fetch createto.ca pages."),
    replay: Path = typer.Option(None, help="Serve a saved snapshot dir instead of the network."),
) -> None:
    """Crawl createto.ca and linked public files into the raw store and database."""
    from createto_info.crawl.runner import run_crawl

    run = run_crawl(get_config(), limit=limit, skip_files=skip_files, replay=replay)
    typer.echo(f"run {run.id} {run.status}")
    typer.echo(json.dumps(run.stats, indent=2))


@app.command()
def parse(
    run_id: int = typer.Option(None, help="Crawl run to attribute records to (default: latest)."),
    rebuild: bool = typer.Option(False, help="Wipe derived tables first (drops history; dev)."),
) -> None:
    """Build projects, news, facts, milestones and procurement signals from stored pages."""
    from sqlalchemy import select

    from createto_info.db import session_scope
    from createto_info.db.models import CrawlRun
    from createto_info.parse.cms import Materializer

    with session_scope() as s:
        run = s.get(CrawlRun, run_id) if run_id else s.scalar(
            select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1))
        if run is None:
            raise typer.BadParameter("no crawl runs yet; run `createto crawl` first")
        if rebuild:
            from createto_info.parse.cms import truncate_derived

            truncate_derived(s)
        stats = Materializer(s, get_config(), run.id).run()
        run.stats = {**run.stats, "parse": dict(sorted(stats.items()))}
    typer.echo(f"parsed into run {run.id}")
    typer.echo(json.dumps(dict(sorted(stats.items())), indent=2))


@app.command()
def pdfs() -> None:
    """Extract per-page PDF text, dates and page renders (local, no LLM)."""
    from createto_info.db import session_scope
    from createto_info.extract.pdf import process_pdfs

    with session_scope() as s:
        stats = process_pdfs(s, get_config())
    typer.echo(json.dumps(dict(sorted(stats.items())), indent=2))


def _latest_run(s):
    from sqlalchemy import select

    from createto_info.db.models import CrawlRun

    run = s.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1))
    if run is None:
        raise typer.BadParameter("no crawl runs yet; run `createto crawl` first")
    return run


@app.command()
def compare() -> None:
    """Compare extraction models on a stratified sample (no claims written); writes
    reports/model_comparison.md with a full-run cost projection."""
    from createto_info.db import session_scope
    from createto_info.extract.compare import REPORTS, markdown, run_comparison

    with session_scope() as s:
        report = run_comparison(s, get_config(), _latest_run(s).id)
    path = REPORTS / "model_comparison.md"
    path.write_text(markdown(report), encoding="utf-8")
    typer.echo(f"report written to {path}")


@app.command()
def extract(
    model: str = typer.Option(None, help="Override models.extract from config.yaml."),
    kinds: str = typer.Option("pdf_text,pdf_vision,news", help="Unit kinds to process."),
    limit: int = typer.Option(None, help="Only the first N units (dev)."),
) -> None:
    """LLM extraction of facts, milestones and procurement signals with verified evidence."""
    import asyncio

    from createto_info.db import session_scope
    from createto_info.extract.llm_extract import apply_outputs, build_units, fetch_outputs

    cfg = get_config()
    model = model or cfg.models.extract
    with session_scope() as s:
        run = _latest_run(s)
        units = build_units(s, cfg, set(kinds.split(",")))[: limit or None]
        typer.echo(f"{len(units)} units with {model} (run {run.id})")
        results, fetch_stats = asyncio.run(fetch_outputs(s, cfg, units, model, run.id))
        stats = {**fetch_stats, **apply_outputs(s, results, model, run.id, dry_run=False)}
        calls = s.execute(text(
            "SELECT count(*), coalesce(sum(cost_usd), 0), coalesce(sum(est_cost_usd), 0), "
            "coalesce(sum(tokens_in), 0), coalesce(sum(tokens_out), 0) FROM llm_calls "
            "WHERE run_id = :r"), {"r": run.id}).one()
        stats.update(llm_calls=calls[0], cost_usd=round(float(calls[1]), 5),
                     est_cost_usd=round(float(calls[2]), 5), tokens_in=calls[3],
                     tokens_out=calls[4])
        run.stats = {**run.stats, "extract": dict(sorted(stats.items())),
                     "llm_calls": calls[0], "cost_usd": round(float(calls[1]), 5)}
    typer.echo(json.dumps(dict(sorted(stats.items())), indent=2, default=str))


@app.command()
def link() -> None:
    """Link unresolved extracted records to projects (Mammoth choice over candidates)."""
    from createto_info.db import session_scope
    from createto_info.link.linker import run_linking

    with session_scope() as s:
        run = _latest_run(s)
        stats = run_linking(s, get_config(), run.id)
        run.stats = {**run.stats, "link": dict(sorted(stats.items()))}
    typer.echo(json.dumps(dict(sorted(stats.items())), indent=2))


@app.command()
def index() -> None:
    """Rebuild the retrieval corpus (search_chunks) for search and chat."""
    from createto_info.chat.index import rebuild_chunks
    from createto_info.db import session_scope

    with session_scope() as s:
        counts = rebuild_chunks(s, _latest_run(s).id)
    typer.echo(json.dumps(counts, indent=2))


@app.command()
def ask(question: str) -> None:
    """Ask a question from the terminal (same pipeline as POST /chat)."""
    import asyncio

    from createto_info.chat.answer import answer
    from createto_info.db import session_scope

    with session_scope() as s:
        out = asyncio.run(answer(s, get_config(), question))
    typer.echo(out["answer"])
    for c in out["citations"]:
        page = f" p.{c['page_no']}" if c["page_no"] else ""
        typer.echo(f"  [{c['n']}] {c['title']} {c['url']}{page}")


@app.command("export")
def export_cmd(out: Path = typer.Option(None, help="Output dir (default data/exports).")) -> None:
    """Write every entity as JSON lines (evidence embedded) plus manifest.json."""
    from createto_info.config import REPO_ROOT
    from createto_info.db import session_scope
    from createto_info.export import export_all

    out = out or REPO_ROOT / "data" / "exports"
    with session_scope() as s:
        manifest = export_all(s, out)
    typer.echo(json.dumps(manifest["counts"], indent=2))
    typer.echo(f"written to {out}")


@app.command()
def runs(n: int = typer.Option(5, help="How many recent runs to show.")) -> None:
    """Show recent crawl runs and their headline stats."""
    from sqlalchemy import select

    from createto_info.db import session_scope
    from createto_info.db.models import CrawlRun

    keys = ["duration_s", "requests", "not_modified", "pages_new", "pages_changed",
            "pages_unchanged", "files_new", "files_changed", "files_unchanged", "llm_calls"]
    with session_scope() as s:
        for r in s.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(n)):
            head = " ".join(f"{k}={r.stats.get(k, 0)}" for k in keys)
            typer.echo(f"#{r.id} {r.started_at:%Y-%m-%d %H:%M} {r.mode:11} {r.status:9} {head}")


@app.command()
def snapshot(out: Path = typer.Argument(..., help="Directory to write manifest.json to.")) -> None:
    """Save the latest crawl state as a replayable snapshot (references the raw store)."""
    from createto_info.crawl.replay import export_snapshot
    from createto_info.db import session_scope

    with session_scope() as s:
        manifest = export_snapshot(s, out)
    typer.echo(f"{len(manifest['entries'])} URLs from run {manifest['created_from_run']} -> {out}")


@app.command()
def demo() -> None:
    """Variant demo: first / unchanged / changed source / failed file, replayed offline into a
    separate `createto_demo` database. Writes reports/variants.md."""
    from createto_info.demo import run_variants

    path = run_variants(get_config())
    typer.echo(f"report written to {path}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the API and search page (http://127.0.0.1:8000, docs at /docs)."""
    import uvicorn

    uvicorn.run("createto_info.api.app:app", host=host, port=port, reload=reload)


@config_app.command("show")
def config_show() -> None:
    """Print the resolved configuration (no secrets)."""
    typer.echo(json.dumps(get_config().snapshot(), indent=2))
    typer.echo(f"OPENROUTER_API_KEY set: {bool(get_env().openrouter_api_key)}")


if __name__ == "__main__":
    app()
