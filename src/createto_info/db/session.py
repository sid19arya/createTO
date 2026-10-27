from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from createto_info.config import get_env


def sqlalchemy_url(url: str) -> str:
    """Use the psycopg3 driver regardless of how DATABASE_URL is spelled."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache
def get_engine() -> Engine:
    return create_engine(sqlalchemy_url(get_env().database_url), pool_pre_ping=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = sessionmaker(get_engine(), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
