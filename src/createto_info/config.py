"""Settings: secrets from the environment / .env, everything else from config.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    openrouter_api_key: str = ""
    database_url: str = "postgresql://createto:createto@localhost:5432/createto"
    config_path: Path = REPO_ROOT / "config.yaml"


class CrawlConfig(BaseModel):
    start_urls: list[str]
    sitemap_url: str
    user_agent: str
    concurrent_per_domain: int = 2
    download_delay_s: float = 0.5
    autothrottle_target_concurrency: float = 1.0
    max_file_mb: int = 50
    timeout_s: int = 30
    retries: int = 2


class ScopeConfig(BaseModel):
    first_party_hosts: list[str]
    asset_hosts: list[str] = Field(default_factory=list)
    external_1hop_hosts: list[str] = Field(default_factory=list)
    external_files_any_host: bool = False
    never_fetch_hosts: list[str] = Field(default_factory=list)
    skip_path_prefixes: list[str] = Field(default_factory=list)
    file_extensions: list[str] = Field(default_factory=list)
    media_extensions: list[str] = Field(default_factory=list)


class CutoffConfig(BaseModel):
    years: int = 5


class ModelsConfig(BaseModel):
    gate: str
    extract: str
    vision: str
    fallback: str | None = None
    compare: list[str] = Field(default_factory=list)
    est_price_per_mtok: dict[str, dict[str, float]] = Field(default_factory=dict)

    def estimate(self, model: str, tokens_in: int, tokens_out: int) -> float | None:
        price = self.est_price_per_mtok.get(model)
        if price is None:
            return None
        return (tokens_in * price["input"] + tokens_out * price["output"]) / 1e6


class LLMConfig(BaseModel):
    base_url: str
    max_requests_per_run: int
    max_cost_usd_per_run: float = 5.0
    concurrency: int = 4
    requests_per_minute: dict[str, int] = Field(default_factory=lambda: {"default": 60})
    max_output_tokens: int = 8000
    timeout_s: int = 180
    pdf_window_pages: int
    vision_min_text_chars: int
    vision_max_pages_per_doc: int
    vision_max_pages_per_run: int
    vision_first_party_only: bool = True
    news_min_chars: int = 200

    def rpm(self, model: str) -> int:
        for suffix, value in self.requests_per_minute.items():
            if suffix != "default" and model.endswith(suffix):
                return value
        return self.requests_per_minute.get("default", 60)


class StorageConfig(BaseModel):
    raw_dir: Path
    renders_dir: Path

    def resolve(self) -> StorageConfig:
        return StorageConfig(
            raw_dir=(REPO_ROOT / self.raw_dir).resolve(),
            renders_dir=(REPO_ROOT / self.renders_dir).resolve(),
        )


class Config(BaseModel):
    crawl: CrawlConfig
    scope: ScopeConfig
    cutoff: CutoffConfig
    models: ModelsConfig
    llm: LLMConfig
    storage: StorageConfig
    # project slug -> extra names (former names, district names) used for linking
    curated_aliases: dict[str, list[str]] = Field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe dump stored on crawl_runs.config (contains no secrets)."""
        return self.model_dump(mode="json")


def load_config(path: Path | None = None) -> Config:
    path = path or Env().config_path
    with open(path, encoding="utf-8") as f:
        cfg = Config.model_validate(yaml.safe_load(f))
    cfg.storage = cfg.storage.resolve()
    return cfg


@lru_cache
def get_env() -> Env:
    return Env()


@lru_cache
def get_config() -> Config:
    return load_config()
