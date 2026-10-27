"""Run one crawl in-process and return the finished crawl_runs row."""

from __future__ import annotations

from scrapy.crawler import CrawlerProcess

from createto_info.config import Config
from createto_info.crawl.recorder import Recorder
from createto_info.crawl.spider import CreateToSpider
from createto_info.db.models import CrawlRun
from createto_info.db.session import session_scope


def scrapy_settings(config: Config) -> dict:
    c = config.crawl
    return {
        "USER_AGENT": c.user_agent,
        "ROBOTSTXT_OBEY": True,
        "CONCURRENT_REQUESTS": 4,
        "CONCURRENT_REQUESTS_PER_DOMAIN": c.concurrent_per_domain,
        "DOWNLOAD_DELAY": c.download_delay_s,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": c.download_delay_s,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": c.autothrottle_target_concurrency,
        "DOWNLOAD_TIMEOUT": c.timeout_s,
        "DOWNLOAD_MAXSIZE": c.max_file_mb * 1024 * 1024,
        "DOWNLOAD_WARNSIZE": 0,
        "RETRY_TIMES": c.retries,
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "LOG_LEVEL": "INFO",
        "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
    }


def run_crawl(config: Config, *, limit: int | None = None, skip_files: bool = False,
              mode: str = "full") -> CrawlRun:
    with session_scope() as session:
        recorder = Recorder(session, config, mode=mode)
        process = CrawlerProcess(scrapy_settings(config), install_root_handler=True)
        crawler = process.create_crawler(CreateToSpider)
        process.crawl(crawler, config=config, recorder=recorder, limit=limit,
                      skip_files=skip_files)
        try:
            process.start()
        except BaseException:
            spider = crawler.spider
            recorder.finish(complete=False, status="failed",
                            sitemap_urls=spider.sitemap_urls if spider else set(),
                            linked_urls=spider.linked_urls if spider else set(),
                            scrapy_stats=crawler.stats.get_stats() if crawler.stats else {})
            raise

        spider = crawler.spider
        stats = crawler.stats.get_stats()
        aborted = stats.get("finish_reason") != "finished"
        return recorder.finish(
            complete=not (limit is not None or skip_files or spider.truncated or aborted),
            sitemap_urls=spider.sitemap_urls,
            linked_urls=spider.linked_urls,
            scrapy_stats=stats,
        )
