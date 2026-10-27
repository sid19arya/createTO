"""OpenRouter chat client: JSON-schema output, images, rate limits, retries, spend cap, ledger.

Every request (including failed ones) becomes an `llm_calls` row with tokens, latency,
OpenRouter's reported cost (`cost_usd`) and our list-price estimate (`est_cost_usd`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from createto_info.config import Config, get_env
from createto_info.db.models import LlmCall

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str
    call_id: int
    tokens_in: int
    tokens_out: int
    tokens_reasoning: int
    cost_usd: float | None
    latency_ms: int


@dataclass
class _Limiter:
    interval: float
    next_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


def image_part(path: Path) -> dict[str, Any]:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def parse_json(text: str) -> Any:
    """JSON from a model reply, tolerating code fences or prose around one object."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class OpenRouter:
    def __init__(self, session: Session, config: Config, run_id: int | None,
                 structured_models: set[str] | None = None):
        self.s = session
        self.config = config
        self.run_id = run_id
        self.key = get_env().openrouter_api_key
        if not self.key:
            raise LLMError("OPENROUTER_API_KEY is not set (see .env.example)")
        self.limiters: dict[str, _Limiter] = {}
        self.sem = asyncio.Semaphore(config.llm.concurrency)
        self.requests = 0
        self.spent = 0.0
        # models that enforce a JSON schema via response_format (checked against /models)
        self.structured = structured_models or set()
        self.client = httpx.AsyncClient(
            base_url=config.llm.base_url, timeout=config.llm.timeout_s,
            headers={"Authorization": f"Bearer {self.key}",
                     "HTTP-Referer": "https://github.com/createto-info",
                     "X-Title": "createto-info"})

    async def aclose(self) -> None:
        await self.client.aclose()

    @staticmethod
    async def structured_capable(config: Config) -> set[str]:
        async with httpx.AsyncClient(base_url=config.llm.base_url, timeout=30) as c:
            data = (await c.get("/v1/models")).json()["data"]
        return {m["id"] for m in data
                if "structured_outputs" in (m.get("supported_parameters") or [])}

    def _limiter(self, model: str) -> _Limiter:
        if model not in self.limiters:
            self.limiters[model] = _Limiter(60.0 / self.config.llm.rpm(model))
        return self.limiters[model]

    def _check_budget(self) -> None:
        llm = self.config.llm
        if self.requests >= llm.max_requests_per_run:
            raise BudgetExceeded(f"request cap {llm.max_requests_per_run} reached")
        if self.spent >= llm.max_cost_usd_per_run:
            raise BudgetExceeded(f"spend cap ${llm.max_cost_usd_per_run} reached "
                                 f"(${self.spent:.4f})")

    async def chat(self, *, model: str, messages: list[dict[str, Any]], task: str,
                   schema: dict[str, Any] | None = None, attempts: int = 3) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages,
                                "max_tokens": self.config.llm.max_output_tokens,
                                "usage": {"include": True}}
        if schema and model in self.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "extraction", "strict": True, "schema": schema}}
        last: Exception | None = None
        for attempt in range(attempts):
            self._check_budget()
            await self._limiter(model).wait()
            async with self.sem:
                self.requests += 1
                t0 = time.monotonic()
                try:
                    resp = await self.client.post("/v1/chat/completions", json=body)
                    latency = int((time.monotonic() - t0) * 1000)
                    data = resp.json() if resp.content else {}
                except (httpx.HTTPError, json.JSONDecodeError) as exc:
                    latency = int((time.monotonic() - t0) * 1000)
                    self._log(model, task, None, latency, "error", None, {}, repr(exc))
                    last = exc
                    await asyncio.sleep(2 * 4 ** attempt)
                    continue
            usage = data.get("usage") or {}
            if resp.status_code != 200 or "error" in data or not data.get("choices"):
                err = json.dumps(data.get("error") or data)[:500]
                self._log(model, task, data.get("id"), latency, "error", resp.status_code,
                          usage, err)
                last = LLMError(f"{resp.status_code}: {err}")
                if resp.status_code in RETRY_STATUS or (resp.status_code == 200
                                                         and "error" not in data):
                    wait = float(resp.headers.get("retry-after") or 2 * 4 ** attempt)
                    await asyncio.sleep(min(wait, 60))
                    continue
                raise last
            content = data["choices"][0]["message"].get("content") or ""
            call = self._log(model, task, data.get("id"), latency, "ok", 200, usage, None)
            return ChatResult(content=content, call_id=call.id,
                              tokens_in=call.tokens_in or 0, tokens_out=call.tokens_out or 0,
                              tokens_reasoning=call.tokens_reasoning or 0,
                              cost_usd=call.cost_usd, latency_ms=latency)
        raise LLMError(f"{model} failed after {attempts} attempts: {last}")

    def _log(self, model: str, task: str, request_id: str | None, latency: int, status: str,
             http_status: int | None, usage: dict[str, Any], error: str | None) -> LlmCall:
        tin = usage.get("prompt_tokens")
        tout = usage.get("completion_tokens")
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        cost = usage.get("cost")
        if cost is not None:
            self.spent += float(cost)
        call = LlmCall(run_id=self.run_id, model=model, endpoint="chat", task=task,
                       tokens_in=tin, tokens_out=tout, tokens_reasoning=reasoning,
                       latency_ms=latency, status=status, http_status=http_status,
                       cost_usd=cost, est_cost_usd=self.config.models.estimate(
                           model, tin or 0, tout or 0),
                       request_id=request_id, error=error)
        self.s.add(call)
        self.s.commit()
        return call
