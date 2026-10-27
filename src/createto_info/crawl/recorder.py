"""Persists crawl results: crawl_runs, documents, document_versions, fetch_log, raw files."""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from createto_info.config import REPO_ROOT, Config
from createto_info.crawl.nextdata import (
    cms_identity,
    extract_next_data,
    page_props,
    semantic_hash,
    visible_text_hash,
)
from createto_info.crawl.rawstore import RawStore, sha256
from createto_info.crawl.urls import kind_for
from createto_info.db.models import CrawlRun, Document, DocumentVersion, FetchLog


def git_sha() -> str | None:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.CalledProcessError):
        return None


@dataclass
class Validators:
    etag: str | None
    last_modified: str | None


@dataclass
class Fetched:
    url: str
    status: int
    body: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None
    scope: str
    referrer: str | None
    elapsed_ms: int | None


class Recorder:
    """One instance per run. Commits after every write so a crash keeps partial progress."""

    def __init__(self, session: Session, config: Config, mode: str = "full"):
        self.s = session
        self.config = config
        self.store = RawStore(config.storage.raw_dir)
        self.counts: Counter[str] = Counter()
        self.run = CrawlRun(mode=mode, git_sha=git_sha(), config=config.snapshot())
        self.s.add(self.run)
        self.s.commit()

    @property
    def run_id(self) -> int:
        return self.run.id

    def validators(self) -> dict[str, Validators]:
        """Conditional-request headers for every known, not-removed document."""
        rows = self.s.execute(
            select(Document.url, Document.etag, Document.last_modified).where(
                Document.removed_at_run.is_(None)
            )
        )
        return {u: Validators(e, lm) for u, e, lm in rows if e or lm}

    # ---------------------------------------------------------------- writes

    def _document(self, url: str, kind: str, scope: str) -> Document:
        doc = self.s.scalar(select(Document).where(Document.url == url))
        if doc is None:
            from urllib.parse import urlsplit

            doc = Document(
                url=url, host=urlsplit(url).hostname or "", kind=kind, scope=scope,
                first_seen_run=self.run_id,
            )
            self.s.add(doc)
            self.s.flush()
        doc.last_seen_run = self.run_id
        doc.removed_at_run = None
        return doc

    def record_fetch(self, f: Fetched) -> str:
        """Store a 200 response. Returns 'new', 'changed' or 'unchanged'."""
        kind = kind_for(f.content_type, f.url)
        doc = self._document(f.url, kind, f.scope)
        doc.kind = kind
        doc.etag, doc.last_modified = f.etag, f.last_modified

        raw_hash = sha256(f.body)
        props = page_props(extract_next_data(f.body)) if kind == "page" else None
        if props:
            content_hash = semantic_hash(props)
        elif kind == "page":
            content_hash = visible_text_hash(f.body)
        else:
            content_hash = raw_hash
        cms_uuid, cms_updated = cms_identity(props)

        existing = self.s.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == doc.id, DocumentVersion.content_hash == content_hash
            )
        )
        if existing:
            change = "unchanged" if doc.current_version_id == existing.id else "changed"
            doc.current_version_id = existing.id  # content reverted to an earlier version
        else:
            change = "new" if doc.current_version_id is None else "changed"
            _, path = self.store.put(f.body, kind)
            version = DocumentVersion(
                document_id=doc.id, run_id=self.run_id, http_status=f.status,
                etag=f.etag, last_modified=f.last_modified, content_type=f.content_type,
                bytes=len(f.body), content_hash=content_hash,
                storage_path=self.store.relpath(path),
                cms_uuid=cms_uuid, cms_updated_at=cms_updated, cms_json=props,
            )
            version.parse_status, version.parse_error = _validate(kind, f.body, props)
            self.s.add(version)
            self.s.flush()
            if version.parse_status == "failed" and doc.current_version_id is not None:
                # keep serving the last good version; the broken one is kept for audit
                change = "failed_integrity"
                self.counts["files_failed_parse"] += 1
            else:
                if version.parse_status == "failed":
                    self.counts["files_failed_parse"] += 1
                doc.current_version_id = version.id

        self._log(f.url, "fetched", change, f.referrer, f.status, f.elapsed_ms)
        self.counts[f"{_bucket(kind)}_{change}"] += 1
        self.s.commit()
        return change

    def record_not_modified(
        self, url: str, referrer: str | None, elapsed_ms: int | None
    ) -> bytes | None:
        """Mark a 304 as seen. Returns the stored body so the caller can still extract links."""
        doc = self.s.scalar(select(Document).where(Document.url == url))
        body = None
        if doc is not None:
            if doc.current_version_id and doc.kind == "page":
                version = self.s.get(DocumentVersion, doc.current_version_id)
                body = self.store.get(version.storage_path)
            doc.last_seen_run = self.run_id
            doc.removed_at_run = None
            self.counts[f"{_bucket(doc.kind)}_unchanged"] += 1
        self._log(url, "not_modified", None, referrer, 304, elapsed_ms)
        self.counts["not_modified"] += 1
        self.s.commit()
        return body

    def log(self, url: str, outcome: str, reason: str | None, referrer: str | None = None,
            http_status: int | None = None, elapsed_ms: int | None = None) -> None:
        self._log(url, outcome, reason, referrer, http_status, elapsed_ms)
        # redirect reasons carry the target URL; count them under one key
        key = outcome if outcome == "redirected" or not reason else f"{outcome}:{reason}"
        self.counts[key] += 1
        if outcome in ("failed", "blocked") and reason not in _GONE:
            # a known document that errored this run still exists; don't let a complete run
            # mark it removed (only 404/410 mean gone)
            doc = self.s.scalar(select(Document).where(Document.url == url))
            if doc is not None:
                doc.last_seen_run = self.run_id
        self.s.commit()

    def _log(self, url, outcome, reason, referrer, http_status, elapsed_ms) -> None:
        self.s.add(FetchLog(
            run_id=self.run_id, url=url, outcome=outcome, reason=reason,
            referrer_url=referrer, http_status=http_status, elapsed_ms=elapsed_ms,
        ))

    # ---------------------------------------------------------------- finish

    def finish(self, *, complete: bool, sitemap_urls: set[str], linked_urls: set[str],
               scrapy_stats: dict[str, Any], status: str | None = None) -> CrawlRun:
        """Close the run. `complete` = the whole site was attempted (no --limit, no abort);
        only then are documents missing from this run marked removed."""
        removed = 0
        if complete:
            res = self.s.execute(
                update(Document)
                .where(Document.last_seen_run < self.run_id, Document.removed_at_run.is_(None))
                .values(removed_at_run=self.run_id)
            )
            removed = res.rowcount or 0

        seen = {u for (u,) in self.s.execute(
            select(Document.url).where(Document.last_seen_run == self.run_id))}
        for url in seen:
            via = ("both" if url in sitemap_urls and url in linked_urls
                   else "sitemap" if url in sitemap_urls else "link" if url in linked_urls
                   else None)
            if via:
                self.s.execute(update(Document).where(Document.url == url)
                               .values(discovered_via=via))

        started = self.run.started_at
        finished = datetime.now(UTC)
        self.run.finished_at = finished
        self.run.status = status or ("succeeded" if complete else "partial")
        self.run.stats = {
            "duration_s": round((finished - started).total_seconds(), 1),
            # replays answer in a downloader middleware, before the downloader's own counters
            "requests": scrapy_stats.get("downloader/request_count")
            or scrapy_stats.get("response_received_count", 0),
            "responses": scrapy_stats.get("downloader/response_count")
            or scrapy_stats.get("response_received_count", 0),
            "bytes": scrapy_stats.get("downloader/response_bytes", 0),
            "status_counts": {
                k.rsplit("/", 1)[1]: v for k, v in scrapy_stats.items()
                if k.startswith("downloader/response_status_count/")
            },
            "documents_seen": len(seen),
            "documents_removed": removed,
            "sitemap_urls": len(sitemap_urls),
            "sitemap_not_linked": len(sitemap_urls - linked_urls),
            "linked_not_in_sitemap": len(
                {u for u in linked_urls - sitemap_urls if u in seen and "createto.ca" in u}),
            **dict(sorted(self.counts.items())),
            # no LLM in the crawl step; kept explicit for the run report
            "llm_calls": 0,
            "cost_usd": 0.0,
        }
        self.s.commit()
        return self.run


_GONE = {"http_404", "http_410"}


def _validate(kind: str, body: bytes, props: dict | None) -> tuple[str, str | None]:
    """Cheap integrity check at fetch time; full text extraction happens later (step 6)."""
    if kind == "pdf":
        import pymupdf

        if b"%PDF-" not in body[:1024]:  # e.g. an HTML error page served as a PDF
            return "failed", "not a pdf: missing %PDF- header"
        try:
            with pymupdf.open(stream=body, filetype="pdf") as doc:
                if doc.page_count == 0:
                    return "failed", "pdf has no pages"
                # all 471 PDFs from the live crawl open without repair; a repaired file is
                # damaged (e.g. truncated) and only partially recoverable
                if doc.is_repaired:
                    return "failed", f"damaged pdf: repaired on open, {doc.page_count} page(s)"
                doc.load_page(doc.page_count - 1).get_text()
        except Exception as exc:  # pymupdf raises several error types for broken files
            return "failed", f"{type(exc).__name__}: {exc}"[:500]
        return "ok", None
    if kind == "page":
        return "ok", None
    return "pending", None


def _bucket(kind: str) -> str:
    return "pages" if kind in ("page", "sitemap") else "files"
