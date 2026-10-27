"""Pure helpers for CMS values: rich text, schedules, dates, areas, addresses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

# ------------------------------------------------------------------ rich text

_BLOCK_NODES = {"paragraph", "heading", "blockquote", "listItem", "list_item", "hardBreak",
                "hard_break", "bulletList", "bullet_list", "orderedList", "ordered_list"}


def richtext_to_text(doc: Any) -> str:
    """Flatten a Storyblok/TipTap rich-text document to plain text with paragraph breaks."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        walk(node.get("content"))
        if node.get("type") in _BLOCK_NODES:
            out.append("\n")

    if isinstance(doc, str):
        return doc.strip()
    walk(doc)
    text = "".join(out)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def richtext_links(doc: Any) -> list[str]:
    """Hrefs of link marks inside a rich-text document."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            for mark in node.get("marks") or []:
                href = (mark.get("attrs") or {}).get("href")
                if mark.get("type") == "link" and href:
                    found.append(href)
            walk(node.get("content"))

    walk(doc)
    return found


# -------------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_SEASONS = {"winter": 1, "spring": 4, "summer": 7, "fall": 10, "autumn": 10}

_YEAR = r"(19|20)\d{2}"
_POINT = re.compile(
    rf"^(?:(?P<day_month>[a-z]+)\s+(?P<day>\d{{1,2}}),?\s+(?P<day_year>{_YEAR})"
    rf"|q(?P<q>[1-4])\s+(?P<q_year>{_YEAR})"
    rf"|(?P<month>[a-z]+)\s+(?P<m_year>{_YEAR})"
    rf"|(?P<year>{_YEAR}))$"
)


@dataclass(frozen=True)
class DatePoint:
    date: date
    precision: str  # day | month | quarter | year


def period_end(point: DatePoint) -> date:
    """Last day of the period a point stands for (2026/year -> 2026-12-31)."""
    d = point.date
    if point.precision == "day":
        return d
    months = {"year": 12, "quarter": 3, "month": 1}[point.precision]
    if point.precision == "year":
        d = d.replace(month=1)
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1) - timedelta(days=1)


@dataclass(frozen=True)
class Schedule:
    start: DatePoint | None
    end: DatePoint | None
    ongoing: bool = False  # "2024 - present"


def parse_date_point(text: str, default_year: int | None = None) -> DatePoint | None:
    t = text.strip().lower().replace(".", "")
    if default_year and re.fullmatch(r"[a-z]+|q[1-4]", t):
        t = f"{t} {default_year}"
    m = _POINT.match(t)
    if not m:
        return None
    try:
        if m["day"]:
            return DatePoint(date(int(m["day_year"]), _MONTHS[m["day_month"]], int(m["day"])),
                             "day")
        if m["q"]:
            return DatePoint(date(int(m["q_year"]), 3 * int(m["q"]) - 2, 1), "quarter")
        if m["month"]:
            word = m["month"]
            if word in _SEASONS:
                return DatePoint(date(int(m["m_year"]), _SEASONS[word], 1), "quarter")
            return DatePoint(date(int(m["m_year"]), _MONTHS[word], 1), "month")
        return DatePoint(date(int(m["year"]), 1, 1), "year")
    except (KeyError, ValueError):
        return None


def parse_schedule(text: str | None) -> Schedule | None:
    """Parse timeline strings like '2021', '2019 - 2021', 'July 2026 - Q2 2028', '2024 - present'.

    Returns None for placeholders ('To be determined', '_____', 'Complete').
    """
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip())
    parts = [p.strip() for p in re.split(r"\s*[-–—]\s*|\s+to\s+", t) if p.strip()]
    if not parts or len(parts) > 2:
        return None
    end_year = re.search(_YEAR, parts[-1])
    if len(parts) == 1:
        point = parse_date_point(parts[0])
        return Schedule(point, None) if point else None
    end_text = parts[1].lower()
    ongoing = end_text in ("present", "ongoing", "today")
    end = None if ongoing else parse_date_point(parts[1])
    start = parse_date_point(parts[0], int(end_year.group(0)) if end_year else None)
    if start is None:
        return None
    return Schedule(start, end, ongoing)


_TITLE_DATE = re.compile(
    rf"\b({'|'.join(m for m in _MONTHS if len(m) > 3)})\s+(\d{{1,2}}),?\s+({_YEAR})", re.I
)


def date_from_title(title: str) -> date | None:
    """A full 'Month D, YYYY' date inside a title (board meetings carry their real date there)."""
    m = _TITLE_DATE.search(title or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------- areas

_AREA = re.compile(r"([\d,]*\.?\d+)\s*(acres?|ac|hectares?|ha|sq\.?\s*ft|square feet|sf|m2|m²"
                   r"|sq\.?\s*m|square met(?:re|er)s)\b", re.I)
_TO_ACRES = {"acre": 1.0, "ac": 1.0, "hectare": 2.47105, "ha": 2.47105,
             "sqft": 1 / 43560, "squarefeet": 1 / 43560, "sf": 1 / 43560,
             "m2": 1 / 4046.86, "m²": 1 / 4046.86, "sqm": 1 / 4046.86,
             "squaremetres": 1 / 4046.86, "squaremeters": 1 / 4046.86}


def parse_site_area_acres(text: str | None) -> float | None:
    m = _AREA.search(text or "")
    if not m:
        return None
    unit = re.sub(r"[\s.]", "", m.group(2).lower()).rstrip("s")
    unit = {"acre": "acre", "hectare": "hectare", "squarefeet": "squarefeet",
            "squaremetre": "squaremetres", "squaremeter": "squaremeters"}.get(unit, unit)
    factor = _TO_ACRES.get(unit) or _TO_ACRES.get(unit + "s")
    if factor is None:
        return None
    return round(float(m.group(1).replace(",", "")) * factor, 4)


# ----------------------------------------------------------------- addresses

_ABBREV = {
    "st": "street", "ave": "avenue", "av": "avenue", "rd": "road", "dr": "drive",
    "blvd": "boulevard", "cres": "crescent", "cr": "crescent", "ct": "court", "pl": "place",
    "pkwy": "parkway", "ln": "lane", "sq": "square", "terr": "terrace", "hwy": "highway",
    "w": "west", "e": "east", "n": "north", "s": "south",
}
_ADDRESS = re.compile(
    r"\b(\d{1,5})(?:\s*[-–]\s*(\d{1,5}))?\s+((?:[A-Z][\w'.]*\s*){1,4}?"
    r"(?:street|st|avenue|ave|av|road|rd|drive|dr|boulevard|blvd|crescent|cres|court|ct|place"
    r"|pl|parkway|pkwy|lane|ln|square|sq|way|terrace|terr|highway|hwy|gardens|circle|trail)\b\.?"
    r"(?:\s+(?:west|east|north|south|w|e|n|s)\b\.?)?)",
    re.I,
)


@dataclass(frozen=True)
class Address:
    start: int
    end: int
    street: str  # normalized, e.g. "dundas street west"


def normalize_street(street: str) -> str:
    words = re.sub(r"[^\w\s]", " ", street.lower()).split()
    return " ".join(_ABBREV.get(w, w) for w in words)


def find_addresses(text: str) -> list[Address]:
    """Street addresses (with number ranges) mentioned in free text."""
    found = []
    for m in _ADDRESS.finditer(text or ""):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if end < start:
            end = start
        found.append(Address(start, end, normalize_street(m.group(3))))
    return found


def addresses_overlap(a: Address, b: Address) -> bool:
    return a.street == b.street and a.start <= b.end and b.start <= a.end
