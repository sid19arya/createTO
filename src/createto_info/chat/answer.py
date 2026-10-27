"""Retrieval-augmented answers with numbered citations (PLAN.md step 8)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session

from createto_info.chat.index import Hit, get_index
from createto_info.config import Config
from createto_info.llm.openrouter import OpenRouter

SOURCE_CHARS = 2500
SYSTEM = """You answer questions about CreateTO (the City of Toronto's real estate agency) and its
projects, using ONLY the numbered sources provided.

- Cite every claim with the source number in brackets, e.g. [1] or [2][4].
- If the sources don't contain the answer, say so plainly and suggest what to search for.
  Never use outside knowledge, never guess numbers or dates.
- "project profile (live site)" and "live site summary" sources show createto.ca as crawled
  now: use them for the current phase, status, open opportunities or anything asked
  "currently". Documents and news are dated snapshots.
- When sources disagree, prefer the most recent one and say which date each refers to.
- Be concise: 2-6 sentences, or a short list for "which projects" questions."""

Complete = Callable[[list[dict[str, Any]]], Awaitable[str]]


def _source_block(i: int, h: Hit) -> str:
    where = h.url or ""
    if h.page_no:
        where += f" p.{h.page_no}"
    kind = {"project": "project profile (live site)", "news": "news",
            "pdf_page": "document page", "overview": "live site summary"}.get(
        h.entity_type, h.entity_type)
    date = f", {h.date}" if h.date else ""
    return f"[{i}] {h.title} ({kind}{date}) {where}\n{h.text[:SOURCE_CHARS]}"


def overview(s: Session) -> Hit:
    """One always-included source with the live dataset's aggregate view, for "which / how
    many" questions retrieval can't enumerate: projects by current phase, open procurement."""
    from sqlalchemy import text

    lines = ["Projects by current phase on createto.ca:"]
    for phase, names in s.execute(text("""
            SELECT coalesce(phase_label, phase, 'unknown'), string_agg(name, '; ' ORDER BY name)
            FROM projects WHERE kind = 'project' GROUP BY 1 ORDER BY 1""")):
        lines.append(f"- {phase}: {names}")
    lines.append("Open procurement opportunities (live sources only):")
    for kind, title, project, closes in s.execute(text("""
            SELECT s.kind, s.title, p.name, s.closes_at FROM procurement_signals s
            LEFT JOIN projects p ON p.id = s.project_id
            WHERE s.status IN ('open', 'upcoming') AND s.retracted_run IS NULL
            ORDER BY s.closes_at NULLS LAST""")):
        lines.append(f"- {kind}: {title}" + (f" ({project})" if project else "")
                     + (f", closes {closes}" if closes else ""))
    return Hit(0, 0.0, "overview", 0, None, "CreateTO dataset overview (live site)",
               "\n".join(lines), "https://createto.ca/projects", None)


def build_messages(question: str, hits: list[Hit],
                   history: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}]
    for turn in (history or [])[-4:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])[:2000]})
    sources = "\n\n".join(_source_block(i, h) for i, h in enumerate(hits, start=1))
    messages.append({"role": "user", "content": f"Question: {question}\n\nSources:\n{sources}"})
    return messages


def cited(answer: str, n_sources: int) -> list[int]:
    nums = {int(x) for x in re.findall(r"\[(\d{1,2})\]", answer)}
    return sorted(i for i in nums if 1 <= i <= n_sources)


async def answer(s: Session, config: Config, question: str,
                 history: list[dict[str, str]] | None = None, k: int = 8,
                 complete: Complete | None = None) -> dict[str, Any]:
    # follow-ups ("what about its timeline?") retrieve with the previous question too
    prev = next((t["content"] for t in reversed(history or []) if t.get("role") == "user"), "")
    hits = get_index(s).search(f"{question} {prev}".strip(), k=k)
    if not hits:
        return {"answer": "I couldn't find anything about that in the collected CreateTO "
                          "pages and documents.", "citations": [], "sources": []}
    hits = [overview(s), *hits]
    messages = build_messages(question, hits, history)
    if complete is None:
        client = OpenRouter(s, config, None)
        try:
            res = await client.chat(model=config.models.extract, messages=messages,
                                    task="chat")
            text_out = res.content.strip()
        finally:
            await client.aclose()
    else:
        text_out = (await complete(messages)).strip()
    sources = [{"n": i, **h.as_dict(), "text": h.text[:400]} for i, h in
               enumerate(hits, start=1)]
    used = cited(text_out, len(hits))
    return {"answer": text_out, "citations": [src for src in sources if src["n"] in used],
            "sources": sources}
