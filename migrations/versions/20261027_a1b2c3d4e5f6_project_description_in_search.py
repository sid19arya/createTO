"""project description and type labels in search vector

Generated columns can't be altered in place, and Alembic doesn't diff their expressions,
so the column is dropped and re-added with the new expression.

Revision ID: a1b2c3d4e5f6
Revises: f5811340daa5
Create Date: 2026-10-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f5811340daa5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BASE = (
    "setweight(to_tsvector('english'::regconfig, coalesce(name, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(address, '') || ' ' || "
    "coalesce(major_intersection, '') || ' ' || coalesce(ward, '')), 'B') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(summary, '') || ' ' || "
    "coalesce(current_use, '') || ' ' || coalesce(proposed_use, '')), 'C')"
)
_WITH_DESCRIPTION = (
    _BASE
    + " || setweight(to_tsvector('english'::regconfig, coalesce(type_labels, '')), 'B')"
    + " || setweight(to_tsvector('english'::regconfig, coalesce(description, '')), 'D')"
)


def _replace(expr: str) -> None:
    op.drop_index("ix_projects_search_tsv", table_name="projects")
    op.drop_column("projects", "search_tsv")
    op.add_column("projects", sa.Column(
        "search_tsv", postgresql.TSVECTOR(), sa.Computed(expr, persisted=True), nullable=False))
    op.create_index("ix_projects_search_tsv", "projects", ["search_tsv"],
                    postgresql_using="gin")


def upgrade() -> None:
    # human labels of `types` ("Affordable Housing Waterfront"); arrays can't feed a generated
    # tsvector (array_to_string isn't immutable), so the materializer writes this text column
    op.add_column("projects", sa.Column("type_labels", sa.Text(), nullable=True))
    _replace(_WITH_DESCRIPTION)


def downgrade() -> None:
    _replace(_BASE)
    op.drop_column("projects", "type_labels")
