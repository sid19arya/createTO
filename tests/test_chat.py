import asyncio

import pytest
from fastapi.testclient import TestClient
from test_materializer import _crawl

from createto_info.api.app import app
from createto_info.chat.answer import answer, build_messages, cited
from createto_info.chat.index import Hit, get_index, rebuild_chunks, tokenize
from createto_info.config import load_config


def test_tokenize_drops_stopwords_keeps_numbers():
    assert tokenize("How many homes at 1313 Queen St. W?") == ["many", "homes", "1313", "queen",
                                                               "st", "w"]


def test_cited_numbers_only_in_range():
    assert cited("Yes [1][3], also [9] and [x].", 4) == [1, 3]


def test_build_messages_numbers_sources_and_keeps_history():
    hits = [Hit(1, 1.0, "news", 1, None, "Title", "Body text", "https://x", None, "2024-01-01")]
    msgs = build_messages("Q?", hits, [{"role": "user", "content": "earlier"},
                                       {"role": "system", "content": "ignored"}])
    assert [m["role"] for m in msgs] == ["system", "user", "user"]
    assert "[1] Title (news, 2024-01-01) https://x" in msgs[-1]["content"]


@pytest.fixture
def corpus(db_session, tmp_path):
    cfg = load_config()
    cfg.storage.raw_dir = tmp_path / "raw"
    _crawl(db_session, cfg)
    counts = rebuild_chunks(db_session, 1)
    db_session.commit()
    return db_session, cfg, counts


@pytest.mark.db
def test_index_and_search(corpus):
    s, _, counts = corpus
    assert counts == {"project": 3, "news": 1}
    # tiny corpus: query a term only the parent has (IDF of "big"/"site" is ~0 here)
    hits = get_index(s).search("Dundas")
    assert hits and hits[0].title == "Big Site" and hits[0].url.endswith("/projects/big-site")
    assert get_index(s).search("zzzz") == []


@pytest.mark.db
def test_answer_with_stubbed_model(corpus):
    s, cfg, _ = corpus
    seen = {}

    async def complete(messages):
        seen["prompt"] = messages[-1]["content"]
        return "Big Site is in Market Offering [1] and has 2 acres [2]."

    out = asyncio.run(answer(s, cfg, "What phase is Big Site in?", complete=complete))
    assert "[1] CreateTO dataset overview (live site)" in seen["prompt"]
    assert "Market Offering: Big Site" in seen["prompt"]
    assert [c["n"] for c in out["citations"]] == [1, 2]
    assert out["sources"][0]["entity_type"] == "overview"


@pytest.mark.db
def test_chat_endpoints(corpus, monkeypatch):
    from createto_info.llm import openrouter

    async def fake_chat(self, **kw):
        return openrouter.ChatResult("Answer [2].", 0, 10, 5, 0, 0.0, 1)

    monkeypatch.setattr(openrouter.OpenRouter, "chat", fake_chat)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    from createto_info import config as config_mod
    config_mod.get_env.cache_clear()
    client = TestClient(app)
    hits = client.get("/chat/search", params={"q": "Dundas"}).json()
    assert hits[0]["title"] == "Big Site"
    res = client.post("/chat", json={"question": "Tell me about Big Site"}).json()
    assert res["answer"] == "Answer [2]." and [c["n"] for c in res["citations"]] == [2]
    assert client.post("/chat", json={"question": ""}).status_code == 422
