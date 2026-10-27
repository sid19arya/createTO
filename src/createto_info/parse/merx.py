"""Parse MERX's public open-solicitations search (linked from /our-work/procurement)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin

from parsel import Selector


@dataclass(frozen=True)
class Solicitation:
    reference: str
    title: str
    buyer: str | None
    location: str | None
    published: date | None
    closing: date | None
    url: str


def _date(text: str | None) -> date | None:
    try:
        return datetime.strptime((text or "").strip(), "%Y/%m/%d").date()
    except ValueError:
        return None


def _clean(text: str | None) -> str | None:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text or None


def parse_open_solicitations(html: bytes | str, base_url: str) -> list[Solicitation]:
    sel = Selector(text=html.decode("utf-8", "replace") if isinstance(html, bytes) else html)
    rows = []
    for a in sel.css("tr.mets-table-row a[id^=searchResultSol_solicitation_]"):
        title = _clean(a.css(".rowTitle::text").get())
        if not title:
            continue
        rows.append(Solicitation(
            reference=a.attrib["id"].rsplit("_", 1)[-1],
            title=title,
            buyer=_clean(a.css(".buyer-name::text").get()),
            location=_clean(a.css(".location::text").get()),
            published=_date(a.css(".publicationDate .dateValue::text").get()),
            closing=_date(a.css(".closingDate .dateValue::text").get()),
            url=urljoin(base_url, a.attrib.get("href", "")),
        ))
    return rows
