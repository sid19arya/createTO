import json

import pytest

from createto_info.config import load_config
from createto_info.crawl.nextdata import (
    cms_identity,
    extract_next_data,
    iter_urls,
    page_props,
    semantic_hash,
    visible_text_hash,
)
from createto_info.crawl.rawstore import RawStore
from createto_info.crawl.urls import (
    ASSET,
    EXTERNAL,
    FIRST_PARTY,
    classify,
    extension,
    normalize_url,
)

SCOPE = load_config().scope


@pytest.mark.parametrize("href,base,expected", [
    ("/projects/parkdale-hub/", "https://createto.ca/", "https://createto.ca/projects/parkdale-hub"),
    ("https://CreateTO.ca/news#top", None, "https://createto.ca/news"),
    ("https://createto.ca/?utm_source=x&page=2", None, "https://createto.ca/?page=2"),
    ("mailto:procurement@createto.ca", None, None),
    ("javascript:void(0)", None, None),
    ("#main", "https://createto.ca/", None),
])
def test_normalize_url(href, base, expected):
    assert normalize_url(href, base) == expected


def test_extension_handles_storyblok_image_transforms():
    assert extension("https://a-us.storyblok.com/f/1/x/abc/report.PDF") == "pdf"
    assert extension("https://a-us.storyblok.com/f/1/1920x1080/abc/photo.jpg/m/800x0") == "jpg"


@pytest.mark.parametrize("url,from_scope,fetch,scope,reason", [
    ("https://createto.ca/projects/x", FIRST_PARTY, True, FIRST_PARTY, None),
    ("https://createto.ca/_next/static/chunk.js", FIRST_PARTY, False, None, "media_not_fetched"),
    ("https://createto.ca/images/favicon.png", FIRST_PARTY, False, None, "media_not_fetched"),
    ("https://a-us.storyblok.com/f/1/x/abc/r.pdf", FIRST_PARTY, True, ASSET, None),
    ("https://a-us.storyblok.com/f/1/x/abc/r.jpg", FIRST_PARTY, False, None, "media_not_fetched"),
    ("https://www.toronto.ca/councillor", FIRST_PARTY, True, EXTERNAL, None),
    ("https://www.toronto.ca/councillor", EXTERNAL, False, None, "hop_limit"),
    ("https://twitter.com/_createto", FIRST_PARTY, False, None, "out_of_scope"),
    ("https://example.com/", FIRST_PARTY, False, None, "not_allowlisted"),
    ("https://f.datasrvr.com/fr1/025/1/Brochure.pdf", FIRST_PARTY, True, EXTERNAL, None),
    ("https://f.datasrvr.com/fr1/025/1/Brochure.pdf", EXTERNAL, False, None, "hop_limit"),
])
def test_classify(url, from_scope, fetch, scope, reason):
    d = classify(url, SCOPE, from_scope)
    assert (d.fetch, d.scope, d.reason) == (fetch, scope, reason)


def _page(content: dict, related: list) -> bytes:
    data = {"props": {"pageProps": {
        "page": {"uuid": "u-1", "published_at": "2025-11-18T17:16:26.657Z", "content": content},
        "related": related, "labels": {"x": "y"},
    }}}
    return (b'<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data).encode() + b"</script></html>")


def test_next_data_identity_and_semantic_hash():
    a = page_props(extract_next_data(_page({"phase": "x"}, [])))
    b = page_props(extract_next_data(_page({"phase": "x"}, [{"other": "page"}])))
    c = page_props(extract_next_data(_page({"phase": "y"}, [])))
    assert "labels" not in a
    uuid, ts = cms_identity(a)
    assert uuid == "u-1" and ts.year == 2025
    assert semantic_hash(a) == semantic_hash(b), "derived lists must not count as a change"
    assert semantic_hash(a) != semantic_hash(c)


def test_iter_urls_finds_nested_assets():
    obj = {"blocks": [{"file": {"filename": "https://a-us.storyblok.com/f/1/x/a/b.pdf"}},
                      {"text": "not a url"}]}
    assert list(iter_urls(obj)) == ["https://a-us.storyblok.com/f/1/x/a/b.pdf"]


def test_rawstore_is_content_addressed(tmp_path):
    store = RawStore(tmp_path)
    h1, p1 = store.put(b"hello", "pdf")
    h2, p2 = store.put(b"hello", "pdf")
    assert h1 == h2 and p1 == p2 and p1.suffix == ".pdf"
    assert store.get(store.relpath(p1)) == b"hello"


def test_visible_text_hash_ignores_scripts_and_markup():
    a = b'<html><script>var t=1</script><p class="a">Open bids</p></html>'
    b = b'<html><script>var t=2</script><p class="b">Open   bids</p><!-- x --></html>'
    c = b"<html><p>Closed bids</p></html>"
    assert visible_text_hash(a) == visible_text_hash(b) != visible_text_hash(c)
