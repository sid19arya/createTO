"""Per-page PDF text and page renders (local, no LLM). See docs/EXTRACTION.md §5.

For every current, intact PDF version without `pdf_pages` rows:
- text per page (PyMuPDF), stored in `pdf_pages` and joined into `document_versions.text`;
- a page is `text` if it has enough characters, `vision` if it is mostly images/vector drawings
  (rendered to PNG for the vision model, within caps), else `blank`;
- `document_versions.doc_date` from the PDF creation date.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import date
from pathlib import Path

import pymupdf
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from createto_info.config import Config
from createto_info.crawl.rawstore import RawStore
from createto_info.crawl.recorder import _validate
from createto_info.db.models import Document, DocumentVersion, PdfPage

RENDER_LONG_SIDE = 1600


def pdf_date(metadata: dict | None) -> date | None:
    m = re.match(r"D:(\d{4})(\d{2})?(\d{2})?", (metadata or {}).get("creationDate") or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        return None


def render_page(page: pymupdf.Page, out_dir: Path) -> tuple[str, Path]:
    zoom = RENDER_LONG_SIDE / max(page.rect.width, page.rect.height)
    png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    digest = hashlib.sha256(png).hexdigest()
    path = out_dir / digest[:2] / f"{digest}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    return digest, path


def process_pdfs(session: Session, config: Config) -> Counter[str]:
    llm = config.llm
    store = RawStore(config.storage.raw_dir)
    stats: Counter[str] = Counter()
    # versions fetched before the integrity check existed
    for version in session.scalars(
            select(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.kind == "pdf", DocumentVersion.parse_status == "pending")):
        version.parse_status, version.parse_error = _validate(
            "pdf", store.get(version.storage_path), None)
        stats[f"validated_{version.parse_status}"] += 1
    session.flush()

    pending = session.execute(
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .where(Document.kind == "pdf", Document.removed_at_run.is_(None),
               DocumentVersion.parse_status == "ok",
               ~exists().where(PdfPage.version_id == DocumentVersion.id))
        .order_by(Document.id)).all()
    vision_budget = llm.vision_max_pages_per_run
    for doc, version in pending:
        vision_ok = doc.scope in ("first_party", "asset") or not llm.vision_first_party_only
        texts = []
        with pymupdf.open(stream=store.get(version.storage_path), filetype="pdf") as pdf:
            version.doc_date = pdf_date(pdf.metadata)
            doc_vision = 0
            for i, page in enumerate(pdf, start=1):
                text = page.get_text().strip()
                images = len(page.get_images())
                drawings = len(page.get_drawings()) if len(text) < llm.vision_min_text_chars else 0
                row = PdfPage(version_id=version.id, page_no=i, text=text,
                              text_chars=len(text), image_count=images, drawing_count=drawings)
                if len(text) >= llm.vision_min_text_chars:
                    row.method = "text"
                elif images or drawings:
                    if not vision_ok:
                        row.method = "vision_skipped_scope"
                    elif doc_vision >= llm.vision_max_pages_per_doc or vision_budget <= 0:
                        row.method = "vision_skipped_cap"
                    else:
                        row.method = "vision"
                        row.render_hash, path = render_page(page, config.storage.renders_dir)
                        row.render_path = path.relative_to(config.storage.renders_dir).as_posix()
                        doc_vision += 1
                        vision_budget -= 1
                else:
                    row.method = "blank"
                stats[f"pages_{row.method}"] += 1
                session.add(row)
                texts.append(f"=== PAGE {i} ===\n{text}")
        version.text = "\n\n".join(texts)
        stats["pdfs"] += 1
        session.flush()
    _render_skipped(session, config, store, vision_budget, stats)
    session.commit()
    return stats


def _render_skipped(session: Session, config: Config, store: RawStore, budget: int,
                    stats: Counter[str]) -> None:
    """Render pages an earlier, lower cap skipped, now that the caps allow them."""
    rows = session.execute(
        select(PdfPage.version_id, PdfPage.page_no)
        .join(DocumentVersion, DocumentVersion.id == PdfPage.version_id)
        .join(Document, Document.current_version_id == DocumentVersion.id)
        .where(PdfPage.method == "vision_skipped_cap", Document.removed_at_run.is_(None))
        .order_by(PdfPage.version_id, PdfPage.page_no)).all()
    by_version: dict[int, list[int]] = {}
    for version_id, page_no in rows:
        by_version.setdefault(version_id, []).append(page_no)
    for version_id, page_nos in by_version.items():
        version = session.get(DocumentVersion, version_id)
        done = session.scalar(select(func.count()).select_from(PdfPage).where(
            PdfPage.version_id == version_id, PdfPage.method == "vision")) or 0
        with pymupdf.open(stream=store.get(version.storage_path), filetype="pdf") as pdf:
            for page_no in page_nos:
                if budget <= 0 or done >= config.llm.vision_max_pages_per_doc:
                    break
                page = session.get(PdfPage, (version_id, page_no))
                page.render_hash, path = render_page(pdf[page_no - 1],
                                                     config.storage.renders_dir)
                page.render_path = path.relative_to(config.storage.renders_dir).as_posix()
                page.method = "vision"
                done += 1
                budget -= 1
                stats["pages_vision_rendered_late"] += 1
        session.flush()
