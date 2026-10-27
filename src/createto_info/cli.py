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


@config_app.command("show")
def config_show() -> None:
    """Print the resolved configuration (no secrets)."""
    typer.echo(json.dumps(get_config().snapshot(), indent=2))
    typer.echo(f"OPENROUTER_API_KEY set: {bool(get_env().openrouter_api_key)}")


if __name__ == "__main__":
    app()
