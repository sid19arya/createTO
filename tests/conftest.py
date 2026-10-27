"""Shared fixtures. DB tests run against a separate `createto_test` database."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

TEST_DB = "createto_test"


@pytest.fixture(scope="session")
def test_db_url():
    from createto_info import config as config_mod
    from createto_info.db import session as session_mod

    base_url = session_mod.sqlalchemy_url(config_mod.get_env().database_url)
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(text("select 1 from pg_database where datname = :n"),
                                 {"n": TEST_DB})
            if not exists:
                conn.execute(text(f'create database "{TEST_DB}"'))
    except Exception:
        pytest.skip("Postgres not reachable; run `docker compose up -d db`")
    finally:
        admin.dispose()

    url = base_url.rsplit("/", 1)[0] + f"/{TEST_DB}"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    config_mod.get_env.cache_clear()
    session_mod.get_engine.cache_clear()

    from alembic import command

    from createto_info.cli import _alembic_config

    command.downgrade(_alembic_config(), "base")
    command.upgrade(_alembic_config(), "head")
    yield url

    if old is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old
    config_mod.get_env.cache_clear()
    session_mod.get_engine.cache_clear()


@pytest.fixture
def db_session(test_db_url):
    from createto_info.db.models import Base
    from createto_info.db.session import get_engine, session_scope

    engine = get_engine()
    with engine.begin() as conn:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        conn.execute(text(f"truncate {names} restart identity cascade"))
    with session_scope() as s:
        yield s
