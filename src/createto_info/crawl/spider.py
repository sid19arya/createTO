"""Scrapy spider: sitemap + homepage seeds, link discovery, scope policy, conditional GETs."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import scrapy
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response
from scrapy.selector import Selector
from twisted.internet.error import DNSLookupError, TCPTimedOutError, TimeoutError

from createto_info.config import Config
from createto_info.crawl.nextdata import extract_next_data, iter_urls
from createto_info.crawl.recorder import Fetched, Recorder
from createto_info.crawl.urls import FIRST_PARTY, classify, kind_for, normalize_url

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class CreateToSpider(scrapy.Spider):
    name = "createto"
    custom_settings = {"HTTPERROR_ALLOW_ALL": True}

    def __init__(self, config: Config, recorder: Recorder, limit: int | None = None,
                 skip_files: bool = False, **kwargs: Any):
        super().__init__(**kwargs)
        self.cfg = config
        self.rec = recorder
        self.limit = limit
        self.skip_files = skip_files
        self.validators = recorder.validators()
        self.scheduled: set[str] = set()
        self.logged_skips: set[str] = set()
        self.sitemap_urls: set[str] = set()
        self.linked_urls: set[str] = set()
        self.truncated = False

    # ------------------------------------------------------------ scheduling

    async def start(self) -> Any:
        sitemap = normalize_url(self.cfg.crawl.sitemap_url)
        yield scrapy.Request(sitemap, callback=self.parse_sitemap, errback=self.on_error,
                             meta={"scope": FIRST_PARTY, "referrer": None}, dont_filter=True)
        for url in self.cfg.crawl.start_urls:
            for req in self.follow_url(url, referrer=None, from_scope=None):
                yield req

    def follow_url(self, href: str, referrer: str | None, from_scope: str | None,
                   via: str = "link") -> Iterator[scrapy.Request]:
        url = normalize_url(href, referrer)
        if url is None:
            return
        (self.sitemap_urls if via == "sitemap" else self.linked_urls).add(url)
        if url in self.scheduled:
            return
        decision = classify(url, self.cfg.scope, from_scope)
        if decision.fetch and self.skip_files and decision.scope != FIRST_PARTY:
            decision = type(decision)(False, None, "skip_files_flag")
        if not decision.fetch:
            if url not in self.logged_skips:
                self.logged_skips.add(url)
                self.rec.log(url, "skipped", decision.reason, referrer)
            return
        if self.limit is not None and len(self.scheduled) >= self.limit:
            self.truncated = True
            return
        self.scheduled.add(url)

        headers = {}
        v = self.validators.get(url)
        if v and v.etag:
            headers["If-None-Match"] = v.etag
        if v and v.last_modified:
            headers["If-Modified-Since"] = v.last_modified
        yield scrapy.Request(url, callback=self.parse_any, errback=self.on_error, headers=headers,
                             meta={"scope": decision.scope, "referrer": referrer})

    # ------------------------------------------------------------- callbacks

    def parse_sitemap(self, response: Response) -> Iterator[scrapy.Request]:
        self._store(response)
        sel = Selector(response, type="xml")
        for loc in sel.xpath("//sm:sitemap/sm:loc/text()", namespaces=_SITEMAP_NS).getall():
            child = normalize_url(loc.strip())
            if child:
                yield response.follow(child, callback=self.parse_sitemap, errback=self.on_error,
                                      meta={"scope": FIRST_PARTY, "referrer": response.url})
        for loc in sel.xpath("//sm:url/sm:loc/text()", namespaces=_SITEMAP_NS).getall():
            yield from self.follow_url(loc.strip(), response.url, None, via="sitemap")

    def parse_any(self, response: Response) -> Iterator[scrapy.Request]:
        self._log_redirects(response)
        meta = response.meta
        elapsed = _ms(meta)
        if response.status == 304:
            body = self.rec.record_not_modified(response.url, meta["referrer"], elapsed)
            if body is not None and meta["scope"] == FIRST_PARTY:
                yield from self._links(body, response.url)
            return
        if response.status >= 400:
            outcome = "blocked" if response.status in (401, 403, 429) else "failed"
            self.rec.log(response.url, outcome, f"http_{response.status}", meta["referrer"],
                         response.status, elapsed)
            return

        self._store(response)
        # only first-party HTML is mined for links; external pages are 1 hop and never followed
        if meta["scope"] == FIRST_PARTY and kind_for(_ctype(response), response.url) == "page":
            yield from self._links(response.body, response.url)

    def _links(self, body: bytes, url: str) -> Iterator[scrapy.Request]:
        hrefs = set(Selector(text=body.decode("utf-8", "replace")).css("a::attr(href)").getall())
        hrefs.update(iter_urls(extract_next_data(body) or {}))
        for href in sorted(hrefs):
            yield from self.follow_url(href, url, FIRST_PARTY)

    def _store(self, response: Response) -> None:
        self.rec.record_fetch(Fetched(
            url=normalize_url(response.url) or response.url,
            status=response.status,
            body=response.body,
            content_type=_ctype(response),
            etag=_header(response, "ETag"),
            last_modified=_header(response, "Last-Modified"),
            scope=response.meta.get("scope", FIRST_PARTY),
            referrer=response.meta.get("referrer"),
            elapsed_ms=_ms(response.meta),
        ))

    def _log_redirects(self, response: Response) -> None:
        for src in response.meta.get("redirect_urls", []):
            self.rec.log(src, "redirected", f"to {response.url}", response.meta.get("referrer"))

    def on_error(self, failure) -> None:
        request = failure.request
        referrer = request.meta.get("referrer")
        if failure.check(IgnoreRequest):
            msg = str(failure.value)
            outcome, reason = (("blocked", "robots") if "robots" in msg.lower()
                               else ("failed", "too_large") if "size" in msg.lower()
                               else ("skipped", "ignored"))
        elif failure.check(TimeoutError, TCPTimedOutError):
            outcome, reason = "failed", "timeout"
        elif failure.check(DNSLookupError):
            outcome, reason = "failed", "dns"
        elif "max size" in str(failure.value).lower() or "maxsize" in str(failure.value).lower():
            outcome, reason = "failed", "too_large"
        else:
            outcome, reason = "failed", type(failure.value).__name__
        self.rec.log(request.url, outcome, reason, referrer)


def _header(response: Response, name: str) -> str | None:
    value = response.headers.get(name)
    return value.decode("latin-1") if value else None


def _ctype(response: Response) -> str | None:
    return _header(response, "Content-Type")


def _ms(meta: dict[str, Any]) -> int | None:
    latency = meta.get("download_latency")
    return int(latency * 1000) if latency is not None else None
