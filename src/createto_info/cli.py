"""Command line entry point: `uv run createto --help`."""

from __future__ import annotations

import json

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
) -> None:
    """Crawl createto.ca and linked public files into the raw store and database."""
    from createto_info.crawl.runner import run_crawl

    run = run_crawl(get_config(), limit=limit, skip_files=skip_files)
    typer.echo(f"run {run.id} {run.status}")
    typer.echo(json.dumps(run.stats, indent=2))


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


@config_app.command("show")
def config_show() -> None:
    """Print the resolved configuration (no secrets)."""
    typer.echo(json.dumps(get_config().snapshot(), indent=2))
    typer.echo(f"OPENROUTER_API_KEY set: {bool(get_env().openrouter_api_key)}")


if __name__ == "__main__":
    app()
