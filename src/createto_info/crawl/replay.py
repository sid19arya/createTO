"""Offline replay of a saved crawl, used for the variant demo (unchanged / changed / failed file).

A snapshot is a directory with `manifest.json`:

    {"created_from_run": 2,
     "entries": {"<url>": {"status": 200, "content_type": "...", "etag": "...",
                           "last_modified": "...", "file": "raw:ab/abc.html" | "files/x.pdf"},
                 "<url>": {"status": 403} | {"robots": true} | {"redirect": "<url>"}}}

`raw:` paths point into the content-addressed raw store, so a base snapshot copies nothing;
variants override a few entries with files inside the snapshot directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response
from scrapy.http.headers import Headers
from scrapy.responsetypes import responsetypes
from sqlalchemy import select
from sqlalchemy.orm import Session

from createto_info.crawl.nextdata import extract_next_data
from createto_info.crawl.rawstore import RawStore
from createto_info.crawl.urls import normalize_url
from createto_info.db.models import CrawlRun, Document, DocumentVersion, FetchLog

MANIFEST = "manifest.json"


# ------------------------------------------------------------------ snapshots


def export_snapshot(session: Session, out_dir: Path) -> dict[str, Any]:
    """Snapshot the latest known state of every document plus the latest run's failures."""
    run_id = session.scalar(select(CrawlRun.id).order_by(CrawlRun.id.desc()).limit(1))
    entries: dict[str, dict[str, Any]] = {}
    rows = session.execute(
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .where(Document.removed_at_run.is_(None)))
    for doc, ver in rows:
        entries[doc.url] = {"status": 200, "content_type": ver.content_type,
                            "etag": doc.etag, "last_modified": doc.last_modified,
                            "file": f"raw:{ver.storage_path}"}
    for log in session.scalars(select(FetchLog).where(FetchLog.run_id == run_id)):
        url = normalize_url(log.url) or log.url
        if url in entries:
            continue
        if log.outcome == "redirected" and log.reason and log.reason.startswith("to "):
            entries[url] = {"redirect": log.reason[3:], "status": 301}
        elif log.reason == "robots":
            entries[url] = {"robots": True}
        elif log.outcome in ("blocked", "failed") and log.http_status:
            entries[url] = {"status": log.http_status}
    manifest = {"created_from_run": run_id, "entries": dict(sorted(entries.items()))}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def load_manifest(snapshot: Path) -> dict[str, Any]:
    return json.loads((snapshot / MANIFEST).read_text(encoding="utf-8"))


def read_entry(snapshot: Path, store: RawStore, entry: dict[str, Any]) -> bytes:
    ref = entry["file"]
    if ref.startswith("raw:"):
        return store.get(ref[4:])
    return (snapshot / ref).read_bytes()


def _override(snapshot: Path, manifest: dict[str, Any], url: str, body: bytes,
              name: str) -> None:
    files = snapshot / "files"
    files.mkdir(parents=True, exist_ok=True)
    (files / name).write_bytes(body)
    entry = dict(manifest["entries"][url])
    entry["file"] = f"files/{name}"
    entry["etag"] = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
    entry["last_modified"] = None
    manifest["entries"][url] = entry


def _copy_snapshot(src: Path, dst: Path) -> dict[str, Any]:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return load_manifest(dst)


def _save(snapshot: Path, manifest: dict[str, Any]) -> None:
    (snapshot / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")


# ------------------------------------------------------------------- variants


def build_changed(base: Path, out: Path, store: RawStore, project_slug: str,
                  new_phase: str, new_site_area: str) -> list[str]:
    """Edit one project's CMS record (phase, site area) and empty the MERX open listing."""
    manifest = _copy_snapshot(base, out)
    notes = []
    url = f"https://createto.ca/projects/{project_slug}"
    html = read_entry(out, store, manifest["entries"][url])
    data = extract_next_data(html)
    content = data["props"]["pageProps"]["page"]["content"]
    old_phase, old_area = content.get("phase"), content.get("siteArea")
    content["phase"], content["siteArea"] = new_phase, new_site_area
    data["props"]["pageProps"]["page"]["published_at"] = "2026-10-27T09:00:00.000Z"
    new_json = json.dumps(data, ensure_ascii=False).encode()
    html = re.sub(rb'(<script id="__NEXT_DATA__" type="application/json"[^>]*>)(.*?)(</script>)',
                  lambda m: m.group(1) + new_json + m.group(3), html, count=1, flags=re.S)
    _override(out, manifest, url, html, f"{project_slug}.html")
    notes.append(f"{url}: phase {old_phase} -> {new_phase}, siteArea {old_area!r} -> "
                 f"{new_site_area!r}")

    merx = next((u for u in manifest["entries"]
                 if u.startswith("https://www.merx.com/public/solicitations/open")), None)
    if merx:
        listing = read_entry(out, store, manifest["entries"][merx])
        emptied = re.sub(rb'<tr\s+data-index="\d+".*?</tr>', b"", listing, flags=re.S)
        _override(out, manifest, merx, emptied, "merx_open_empty.html")
        notes.append(f"{merx}: all open solicitations removed (tender closed)")
    _save(out, manifest)
    return notes


def build_failed(base: Path, out: Path, store: RawStore, corrupt_url: str,
                 error_url: str) -> list[str]:
    """Corrupt one PDF and make another answer HTTP 500."""
    manifest = _copy_snapshot(base, out)
    original = read_entry(out, store, manifest["entries"][corrupt_url])
    _override(out, manifest, corrupt_url, original[: max(64, len(original) // 50)] +
              b"\n%% truncated by variant builder", "corrupt.pdf")
    manifest["entries"][error_url] = {"status": 500}
    _save(out, manifest)
    return [f"{corrupt_url}: truncated to {max(64, len(original) // 50)} of {len(original)} "
            f"bytes", f"{error_url}: HTTP 500"]


# ----------------------------------------------------------------- middleware


class ReplayMiddleware:
    """Downloader middleware serving responses from a snapshot instead of the network."""

    def __init__(self, snapshot: Path, store: RawStore):
        self.snapshot = snapshot
        self.store = store
        self.entries = load_manifest(snapshot)["entries"]

    @classmethod
    def from_crawler(cls, crawler):
        s = crawler.settings
        return cls(Path(s["REPLAY_SNAPSHOT"]), RawStore(Path(s["REPLAY_RAW_DIR"])))

    def process_request(self, request, spider=None):
        if request.url.endswith("/robots.txt"):
            return Response(request.url, status=200, body=b"User-agent: *\nAllow: /\n",
                            request=request)
        entry = self.entries.get(normalize_url(request.url) or request.url)
        if entry is None:
            return Response(request.url, status=404, body=b"", request=request)
        if entry.get("robots"):
            raise IgnoreRequest("Forbidden by robots.txt (replay)")
        if "redirect" in entry:
            return Response(request.url, status=entry.get("status", 301),
                            headers={"Location": entry["redirect"]}, request=request)
        status = entry.get("status", 200)
        if status != 200 or "file" not in entry:
            return Response(request.url, status=status, body=b"", request=request)

        etag = entry.get("etag")
        if etag and request.headers.get("If-None-Match", b"").decode() == etag:
            return Response(request.url, status=304, headers={"ETag": etag}, request=request)
        body = read_entry(self.snapshot, self.store, entry)
        headers = Headers({"Content-Type": entry.get("content_type") or "application/octet-stream"})
        if etag:
            headers["ETag"] = etag
        if entry.get("last_modified"):
            headers["Last-Modified"] = entry["last_modified"]
        cls = responsetypes.from_args(headers=headers, url=request.url, body=body)
        return cls(request.url, status=200, headers=headers, body=body, request=request)
