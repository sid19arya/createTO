"""ORM models. Layout and intent are documented in docs/DATABASE.md; keep the two in sync.

Enumerated values are stored as text and validated in code (vocabularies in docs/EXTRACTION.md),
so adding a vocabulary term never needs a migration.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Json = dict[str, Any]


class Base(DeclarativeBase):
    type_annotation_map = {Json: JSONB, list[str]: ARRAY(Text), list[int]: ARRAY(Integer)}


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _run_fk(nullable: bool = True) -> Mapped[int | None]:
    return mapped_column(ForeignKey("crawl_runs.id"), nullable=nullable, index=True)


def _tsv(expr: str) -> Mapped[Any]:
    return mapped_column(TSVECTOR, Computed(expr, persisted=True))


# ---------------------------------------------------------------- crawl layer


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = _now()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(Text, default="full")  # full | incremental | replay
    status: Mapped[str] = mapped_column(Text, default="running")
    git_sha: Mapped[str | None] = mapped_column(Text)
    config: Mapped[Json] = mapped_column(default=dict)
    stats: Mapped[Json] = mapped_column(default=dict)
    notes: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    """One row per normalized URL ever seen, independent of content."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    host: Mapped[str] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(Text)  # page | pdf | doc | sitemap | image | other
    title: Mapped[str | None] = mapped_column(Text)  # human title from the CMS resource list
    scope: Mapped[str] = mapped_column(Text)  # first_party | asset | external_1hop
    discovered_via: Mapped[str | None] = mapped_column(Text)  # sitemap | link | both
    first_seen_run: Mapped[int | None] = _run_fk()
    last_seen_run: Mapped[int | None] = _run_fk()
    removed_at_run: Mapped[int | None] = _run_fk()
    # latest HTTP validators, sent as If-None-Match / If-Modified-Since on the next run
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_versions.id", use_alter=True, name="fk_documents_current_version")
    )


class DocumentVersion(Base):
    """One row per distinct content of a document. Immutable."""

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "content_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    run_id: Mapped[int | None] = _run_fk()
    fetched_at: Mapped[dt.datetime] = _now()
    http_status: Mapped[int | None]
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(Text, index=True)
    storage_path: Mapped[str] = mapped_column(Text)
    cms_uuid: Mapped[str | None] = mapped_column(Text, index=True)
    cms_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cms_json: Mapped[Json | None]
    text: Mapped[str | None] = mapped_column(Text)
    # publication date: PDF creation date, else CMS publish date; drives "latest document wins"
    doc_date: Mapped[dt.date | None] = mapped_column(Date)
    parse_status: Mapped[str] = mapped_column(Text, default="pending")  # pending | ok | failed
    parse_error: Mapped[str | None] = mapped_column(Text)


class PdfPage(Base):
    __tablename__ = "pdf_pages"

    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), primary_key=True)
    page_no: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str | None] = mapped_column(Text)
    text_chars: Mapped[int] = mapped_column(default=0)
    image_count: Mapped[int] = mapped_column(default=0)
    drawing_count: Mapped[int] = mapped_column(default=0)
    method: Mapped[str] = mapped_column(Text)  # text | vision | blank | vision_skipped_cap
    render_path: Mapped[str | None] = mapped_column(Text)
    render_hash: Mapped[str | None] = mapped_column(Text)
    vision_description: Mapped[str | None] = mapped_column(Text)


class FetchLog(Base):
    """Every URL decision per run, including skipped and blocked links."""

    __tablename__ = "fetch_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = _run_fk(nullable=False)
    ts: Mapped[dt.datetime] = _now()
    url: Mapped[str] = mapped_column(Text, index=True)
    referrer_url: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None]
    elapsed_ms: Mapped[int | None]


# ----------------------------------------------------------- extraction layer


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = _run_fk()
    ts: Mapped[dt.datetime] = _now()
    model: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text)  # chat | decisions
    task: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int | None]
    tokens_out: Mapped[int | None]
    tokens_reasoning: Mapped[int | None]  # hidden reasoning tokens, billed as output
    latency_ms: Mapped[int | None]
    status: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int | None]
    cost_usd: Mapped[float | None] = mapped_column(Double)
    est_cost_usd: Mapped[float | None] = mapped_column(Double)
    request_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class Extraction(Base):
    """Cached parser/LLM output; makes unchanged re-runs free."""

    __tablename__ = "extractions"
    __table_args__ = (UniqueConstraint("input_hash", "task", "prompt_version", "model"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    input_hash: Mapped[str] = mapped_column(Text)
    task: Mapped[str] = mapped_column(Text)  # gate | extract | vision_extract | link | news_kind
    prompt_version: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"), index=True)
    page_from: Mapped[int | None]
    page_to: Mapped[int | None]
    output: Mapped[Json | None]
    status: Mapped[str] = mapped_column(Text)  # ok | invalid_json | skipped_by_gate | error
    created_run: Mapped[int | None] = _run_fk()
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id"))
    created_at: Mapped[dt.datetime] = _now()


# --------------------------------------------------------------- domain layer


class Initiative(Base):
    __tablename__ = "initiatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    cms_uuid: Mapped[str] = mapped_column(Text, unique=True)
    slug: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    source_version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"))


class Councillor(Base):
    __tablename__ = "councillors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    ward: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    source_version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"))


class Project(Base):
    """Current view of a project. Values from documents live in `facts`."""

    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_search_tsv", "search_tsv", postgresql_using="gin"),
        Index("ix_projects_types", "types", postgresql_using="gin"),
        Index(
            "ix_projects_name_trgm", "name",
            postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"},
        ),
        Index(
            "ix_projects_address_trgm", "address",
            postgresql_using="gin", postgresql_ops={"address": "gin_trgm_ops"},
        ),
        Index("ix_projects_lat_lng", "latitude", "longitude"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cms_uuid: Mapped[str] = mapped_column(Text, unique=True)
    slug: Mapped[str] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(Text, server_default="project")  # project | block (sub-site)
    url: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)  # full body text
    address: Mapped[str | None] = mapped_column(Text)
    major_intersection: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    ward: Mapped[str | None] = mapped_column(Text, index=True)
    councillor_id: Mapped[int | None] = mapped_column(ForeignKey("councillors.id"))
    phase: Mapped[str | None] = mapped_column(Text, index=True)
    phase_label: Mapped[str | None] = mapped_column(Text)
    types: Mapped[list[str]] = mapped_column(default=list)
    type_labels: Mapped[str | None] = mapped_column(Text)  # "Affordable Housing, Waterfront"
    initiative_id: Mapped[int | None] = mapped_column(ForeignKey("initiatives.id"))
    parent_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    site_area_text: Mapped[str | None] = mapped_column(Text)
    site_area_acres: Mapped[float | None] = mapped_column(Double)
    current_use: Mapped[str | None] = mapped_column(Text)
    proposed_use: Mapped[str | None] = mapped_column(Text)
    surrounding_use: Mapped[str | None] = mapped_column(Text)
    delivery: Mapped[str | None] = mapped_column(Text)
    cms_created_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cms_published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cms_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_date: Mapped[dt.date | None] = mapped_column(Date)
    cutoff_status: Mapped[str | None] = mapped_column(Text)
    source_version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"))
    first_seen_run: Mapped[int | None] = _run_fk()
    last_seen_run: Mapped[int | None] = _run_fk()
    search_tsv: Mapped[Any] = _tsv(
        "setweight(to_tsvector('english'::regconfig, coalesce(name, '')), 'A') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(address, '') || ' ' || "
        "coalesce(major_intersection, '') || ' ' || coalesce(ward, '')), 'B') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(summary, '') || ' ' || "
        "coalesce(current_use, '') || ' ' || coalesce(proposed_use, '')), 'C') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(type_labels, '')), 'B') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(description, '')), 'D')"
    )


class ProjectAlias(Base):
    __tablename__ = "project_aliases"
    __table_args__ = (
        UniqueConstraint("project_id", "alias"),
        Index(
            "ix_project_aliases_alias_trgm", "alias",
            postgresql_using="gin", postgresql_ops={"alias": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    alias: Mapped[str] = mapped_column(Text)
    # slug | address | intersection | former_name | microsite
    kind: Mapped[str] = mapped_column(Text)


class ProjectVersion(Base):
    __tablename__ = "project_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[int | None] = _run_fk()
    version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"))
    snapshot: Mapped[Json]
    diff: Mapped[Json | None]  # {field: [old, new]}; null for the first snapshot
    created_at: Mapped[dt.datetime] = _now()


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (Index("ix_news_search_tsv", "search_tsv", postgresql_using="gin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cms_uuid: Mapped[str] = mapped_column(Text, unique=True)
    slug: Mapped[str] = mapped_column(Text, index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    author: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list[str]] = mapped_column(default=list)
    initiative_ids: Mapped[list[int]] = mapped_column(default=list)
    kind: Mapped[str | None] = mapped_column(Text)
    importance: Mapped[str | None] = mapped_column(Text)
    event_starts_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    event_ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    external_url: Mapped[str | None] = mapped_column(Text)
    external_source: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    source_version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"))
    first_seen_run: Mapped[int | None] = _run_fk()
    last_seen_run: Mapped[int | None] = _run_fk()
    search_tsv: Mapped[Any] = _tsv(
        "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(body_text, '')), 'C')"
    )


class Fact(Base):
    """A value claimed for a project field. Identity: (project, field, normalized value).

    Several sources asserting the same value share one row (see `evidence`); `status` is decided
    by the reconciliation rules in PLAN.md §8.
    """

    __tablename__ = "facts"
    __table_args__ = (
        Index("ix_facts_project_field", "project_id", "field"),
        UniqueConstraint("project_id", "field", "value_key", name="uq_facts_identity",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    project_hint: Mapped[str | None] = mapped_column(Text)
    field: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_num: Mapped[float | None] = mapped_column(Double)
    value_key: Mapped[str] = mapped_column(Text)  # normalized value used for identity
    unit: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[str | None] = mapped_column(Text)  # latest document date asserting it
    as_of_precision: Mapped[str | None] = mapped_column(Text)
    # highest-ranked source among its evidence: cms > rule:* > model:<id>@<prompt_version>
    extracted_by: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Double, default=1.0)
    # current | superseded | conflicting | retracted
    status: Mapped[str] = mapped_column(Text, default="current")
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("facts.id"))
    valid_from_run: Mapped[int | None] = _run_fk()
    valid_to_run: Mapped[int | None] = _run_fk()
    # how the record was tied to a project (PLAN.md step 7):
    # link_status: linked | organization (CreateTO itself) | external (not a CreateTO site)
    #              | unresolved; link_method: cms | extractor | alias_match | mammoth
    link_status: Mapped[str | None] = mapped_column(Text, index=True)
    link_method: Mapped[str | None] = mapped_column(Text)
    link_confidence: Mapped[float | None] = mapped_column(Double)


class Evidence(Base):
    """One source location backing a fact, milestone or procurement signal."""

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "version_id", "page_no", "locator",
                         name="uq_evidence_location", postgresql_nulls_not_distinct=True),
        Index("ix_evidence_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text)  # fact | milestone | procurement_signal
    entity_id: Mapped[int]
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), index=True)
    page_no: Mapped[int | None]
    locator: Mapped[str | None] = mapped_column(Text)  # JSON path, CSS selector, row id
    excerpt: Mapped[str | None] = mapped_column(Text)
    extracted_by: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Double, default=1.0)
    extraction_id: Mapped[int | None] = mapped_column(ForeignKey("extractions.id"))
    first_run: Mapped[int | None] = _run_fk()
    last_run: Mapped[int | None] = _run_fk()


class _Sourced:
    """Columns shared by records backed by `evidence` rows."""

    extracted_by: Mapped[str] = mapped_column(Text)  # highest-ranked source
    confidence: Mapped[float] = mapped_column(Double, default=1.0)
    first_seen_run: Mapped[int | None] = _run_fk()
    last_seen_run: Mapped[int | None] = _run_fk()
    # set when no evidence is live any more (source page changed or removed); cleared if re-asserted
    retracted_run: Mapped[int | None] = _run_fk()
    # how the record was tied to a project (PLAN.md step 7):
    # link_status: linked | organization (CreateTO itself) | external (not a CreateTO site)
    #              | unresolved; link_method: cms | extractor | alias_match | mammoth
    link_status: Mapped[str | None] = mapped_column(Text, index=True)
    link_method: Mapped[str | None] = mapped_column(Text)
    link_confidence: Mapped[float | None] = mapped_column(Double)


class Milestone(_Sourced, Base):
    __tablename__ = "milestones"
    __table_args__ = (Index("ix_milestones_project_date", "project_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    project_hint: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    date: Mapped[dt.date | None] = mapped_column(Date)  # first day of the precision period
    date_precision: Mapped[str | None] = mapped_column(Text)  # day | month | quarter | year
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)  # actual | planned | cancelled | delayed


class ProcurementSignal(_Sourced, Base):
    __tablename__ = "procurement_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    project_hint: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    reference_number: Mapped[str | None] = mapped_column(Text)
    issuer: Mapped[str | None] = mapped_column(Text)
    counterparty: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Double)
    currency: Mapped[str | None] = mapped_column(Text)
    opens_at: Mapped[str | None] = mapped_column(Text)  # ISO with precision, e.g. 2024-05
    closes_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="unknown")
    external_url: Mapped[str | None] = mapped_column(Text)


class Link(Base):
    """Generic graph edge between any two entities (see docs/DATABASE.md for rel values)."""

    __tablename__ = "links"
    __table_args__ = (
        UniqueConstraint("from_type", "from_id", "to_type", "to_id", "rel"),
        Index("ix_links_to", "to_type", "to_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    from_type: Mapped[str] = mapped_column(Text)
    from_id: Mapped[int]
    to_type: Mapped[str] = mapped_column(Text)
    to_id: Mapped[int]
    rel: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Double, default=1.0)
    run_id: Mapped[int | None] = _run_fk()


# -------------------------------------------------------------- serving layer


class SearchChunk(Base):
    __tablename__ = "search_chunks"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "page_no"),
        Index("ix_search_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text)  # project | news | pdf_page | external
    entity_id: Mapped[int]
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    page_no: Mapped[int | None]
    updated_run: Mapped[int | None] = _run_fk()
    tsv: Mapped[Any] = _tsv(
        "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
        "setweight(to_tsvector('english'::regconfig, coalesce(text, '')), 'C')"
    )
