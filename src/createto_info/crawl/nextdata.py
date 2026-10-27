"""Read the Storyblok record that Next.js embeds in every createto.ca page."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

_NEXT_DATA = re.compile(
    rb'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)
_URLISH = re.compile(r"^https?://", re.I)

# pageProps keys that are site chrome / UI strings, identical on every page
_CHROME_KEYS = {"labels", "common", "header", "footer", "theme"}


def extract_next_data(html: bytes) -> dict[str, Any] | None:
    m = _NEXT_DATA.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def page_props(next_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not next_data:
        return None
    props = (next_data.get("props") or {}).get("pageProps")
    if not isinstance(props, dict):
        return None
    return {k: v for k, v in props.items() if k not in _CHROME_KEYS}


def cms_identity(props: dict[str, Any] | None) -> tuple[str | None, datetime | None]:
    page = (props or {}).get("page") or {}
    uuid = page.get("uuid")
    updated = page.get("published_at") or page.get("updated_at")
    try:
        ts = datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else None
    except (AttributeError, ValueError):
        ts = None
    return uuid, ts


def semantic_hash(props: dict[str, Any]) -> str:
    """Hash of the page's own CMS record.

    Stable across Next.js rebuilds (which change the HTML wrapper) and across changes to
    derived lists such as `articles` or `similar`, which change whenever *other* pages do.
    """
    record = props.get("page", props)
    blob = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def iter_urls(obj: Any) -> Iterator[str]:
    """Every absolute URL string anywhere in the JSON (asset filenames, external links)."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from iter_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_urls(v)
    elif isinstance(obj, str) and _URLISH.match(obj) and len(obj) < 2048:
        yield obj
