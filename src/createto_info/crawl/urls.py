"""URL normalization and the crawl scope policy (PLAN.md §4)."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from createto_info.config import ScopeConfig

_TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid)$", re.I)
_NON_HTTP = ("mailto:", "tel:", "javascript:", "data:", "#", "sms:")

_DOC_EXTENSIONS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "rtf", "txt"}

FIRST_PARTY = "first_party"
ASSET = "asset"
EXTERNAL = "external_1hop"


def normalize_url(href: str, base: str | None = None) -> str | None:
    """Absolute, canonical form used as the identity of a document. None if not http(s)."""
    href = (href or "").strip()
    if not href or href.lower().startswith(_NON_HTTP):
        return None
    url = urljoin(base, href) if base else href
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not _TRACKING_PARAMS.match(k)]
    )
    return urlunsplit(("https" if parts.scheme == "https" else "http", host, path, query, ""))


def extension(url: str) -> str:
    """Lower-case file extension of the URL path ('' if none).

    Storyblok image transforms (`.../photo.jpg/m/800x0`) report the original extension.
    """
    path = urlsplit(url).path
    if "/m/" in path:
        path = path.split("/m/", 1)[0]
    return posixpath.splitext(path)[1].lstrip(".").lower()


@dataclass(frozen=True)
class Decision:
    fetch: bool
    scope: str | None  # first_party | asset | external_1hop; None if never fetched
    reason: str | None = None  # why it is skipped


def classify(url: str, scope_cfg: ScopeConfig, from_scope: str | None) -> Decision:
    """Decide whether a URL discovered on a page of `from_scope` should be fetched.

    `from_scope` is None for seeds (homepage, sitemap entries).
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    ext = extension(url)

    if host in scope_cfg.never_fetch_hosts:
        return Decision(False, None, "out_of_scope")
    if ext in scope_cfg.media_extensions:
        return Decision(False, None, "media_not_fetched")

    if host in scope_cfg.first_party_hosts:
        if any(parts.path.startswith(p) for p in scope_cfg.skip_path_prefixes):
            return Decision(False, None, "skip_path")
        return Decision(True, FIRST_PARTY)

    # everything below is only reachable from createto.ca itself (max 1 hop)
    if from_scope not in (None, FIRST_PARTY):
        return Decision(False, None, "hop_limit")

    if host in scope_cfg.asset_hosts:
        if ext and ext not in scope_cfg.file_extensions:
            return Decision(False, None, "unsupported_file_type")
        return Decision(True, ASSET)
    if host in scope_cfg.external_1hop_hosts:
        return Decision(True, EXTERNAL)
    if scope_cfg.external_files_any_host and ext in scope_cfg.file_extensions:
        return Decision(True, EXTERNAL)
    return Decision(False, None, "not_allowlisted")


def kind_for(content_type: str | None, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    ext = extension(url)
    if ct == "application/pdf" or ext == "pdf":
        return "pdf"
    if ct in ("text/html", "application/xhtml+xml"):
        return "page"
    if ct in ("application/xml", "text/xml") or ext == "xml":
        return "sitemap" if "sitemap" in url else "other"
    if ext in _DOC_EXTENSIONS or "officedocument" in ct:
        return "doc"
    if ct.startswith("image/"):
        return "image"
    return "other"
