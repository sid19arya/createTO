from datetime import date

import pytest

from createto_info.parse.text import (
    DatePoint,
    addresses_overlap,
    date_from_title,
    find_addresses,
    parse_schedule,
    parse_site_area_acres,
    period_end,
    richtext_links,
    richtext_to_text,
)


def test_richtext_to_text_and_links():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Read the "},
            {"type": "text", "text": "report", "marks": [
                {"type": "link", "attrs": {"href": "https://a-us.storyblok.com/f/1/x/a/r.pdf"}}]},
            {"type": "text", "text": "."}]},
        {"type": "paragraph"},
        {"type": "bullet_list", "content": [{"type": "list_item", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "One"}]}]}]},
    ]}
    assert richtext_to_text(doc) == "Read the report.\n\nOne"
    assert richtext_links(doc) == ["https://a-us.storyblok.com/f/1/x/a/r.pdf"]


@pytest.mark.parametrize("text,start,end,ongoing", [
    ("2021", (date(2021, 1, 1), "year"), None, False),
    ("2019 - 2021", (date(2019, 1, 1), "year"), (date(2021, 1, 1), "year"), False),
    ("2019-2020", (date(2019, 1, 1), "year"), (date(2020, 1, 1), "year"), False),
    ("Q1 2026", (date(2026, 1, 1), "quarter"), None, False),
    ("July 2026 - Q2 2028", (date(2026, 7, 1), "month"), (date(2028, 4, 1), "quarter"), False),
    ("September 14, 2024", (date(2024, 9, 14), "day"), None, False),
    ("October - Q4 2024 ", (date(2024, 10, 1), "month"), (date(2024, 10, 1), "quarter"), False),
    ("January - February 2026", (date(2026, 1, 1), "month"), (date(2026, 2, 1), "month"), False),
    ("2024 - present", (date(2024, 1, 1), "year"), None, True),
    ("2023-Present", (date(2023, 1, 1), "year"), None, True),
])
def test_parse_schedule(text, start, end, ongoing):
    s = parse_schedule(text)
    assert (s.start.date, s.start.precision) == start
    assert ((s.end.date, s.end.precision) if s.end else None) == end
    assert s.ongoing is ongoing


@pytest.mark.parametrize("text", ["To be determined", "_____", "Complete", "", None])
def test_parse_schedule_placeholders(text):
    assert parse_schedule(text) is None


def test_date_from_title():
    assert date_from_title("Corporate Board Meeting - November 28, 2024") == date(2024, 11, 28)
    assert date_from_title("Etobicoke Civic Centre Approved") is None


@pytest.mark.parametrize("text,acres", [
    ("4.24 acres", 4.24), ("Approx. 73 acres (Allen East District)", 73.0), ("1 acre", 1.0),
    ("2 ha", 4.9421), ("43,560 sq ft", 1.0), ("", None), ("TBD", None),
])
def test_parse_site_area(text, acres):
    assert parse_site_area_acres(text) == acres


def test_find_addresses_and_overlap():
    (a,) = find_addresses("Testing & Inspection Services at 1113-1125 Dundas St W")
    assert (a.start, a.end, a.street) == (1113, 1125, "dundas street west")
    (b,) = find_addresses("1113-1117 Dundas Street West")
    assert addresses_overlap(a, b)
    (c,) = find_addresses("1313 Queen Street West")
    assert not addresses_overlap(a, c)


@pytest.mark.parametrize("point,end", [
    (DatePoint(date(2026, 1, 1), "year"), date(2026, 12, 31)),
    (DatePoint(date(2028, 4, 1), "quarter"), date(2028, 6, 30)),
    (DatePoint(date(2026, 12, 1), "month"), date(2026, 12, 31)),
    (DatePoint(date(2024, 9, 14), "day"), date(2024, 9, 14)),
])
def test_period_end(point, end):
    assert period_end(point) == end
