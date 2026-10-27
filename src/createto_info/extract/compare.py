"""Model comparison on a fixed stratified sample + full-run cost projection.

Runs the same units through each model in `models.compare` (dry run: nothing written to the
claims tables), then reports validity, evidence pass rates, agreement, latency and the actual
OpenRouter-billed cost, and projects a full run from measured tokens per unit type.
Outputs are cached in `extractions`, so the chosen model's sample results are reused later.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import statistics
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from createto_info.config import REPO_ROOT, Config
from createto_info.db.models import Extraction, LlmCall
from createto_info.extract.llm_extract import (
    Unit,
    _norm,
    apply_outputs,
    build_units,
    fetch_outputs,
    verify,
)
from createto_info.llm.openrouter import OpenRouter

REPORTS = REPO_ROOT / "reports"


def pick_sample(units: list[Unit], seed: int = 7) -> list[Unit]:
    """Deterministic stratified sample covering the document shapes we have."""
    rng = random.Random(seed)
    text_units = [u for u in units if u.kind == "pdf_text"]
    strata: dict[str, list[Unit]] = {
        "short pdf (<=3 text pages)": [u for u in text_units if len(u.pages) <= 3
                                       and "storyblok" in u.url],
        "medium pdf (4-15 pages)": [u for u in text_units if 4 <= len(u.pages) <= 15
                                    and "storyblok" in u.url],
        "long pdf window (40 pages)": [u for u in text_units if len(u.pages) >= 35],
        "board / staff report": [u for u in text_units if any(
            k in (u.title + u.url).lower() for k in ("board", "staff report", "backgroundfile"))
            and len(u.pages) <= 25],
        "procurement / offering": [u for u in text_units if any(
            k in (u.title + u.url).lower() for k in ("offering", "rfp", "brochure",
                                                     "expression", "rfq", "procure"))],
        "external pdf": [u for u in text_units if "storyblok" not in u.url
                         and len(u.pages) <= 25],
        "vision page": [u for u in units if u.kind == "pdf_vision"],
        "news article": [u for u in units if u.kind == "news"],
    }
    quota = {"short pdf (<=3 text pages)": 4, "medium pdf (4-15 pages)": 3,
             "long pdf window (40 pages)": 1, "board / staff report": 2,
             "procurement / offering": 2, "external pdf": 2, "vision page": 4,
             "news article": 3}
    chosen: list[Unit] = []
    used: set[tuple[int, int | None, str]] = set()
    for name, pool in strata.items():
        pool = sorted(pool, key=lambda u: (u.version_id, u.page_from or 0))
        rng.shuffle(pool)
        n = 0
        for u in pool:
            key = (u.version_id, u.page_from, u.kind)
            if key in used or (u.kind != "news" and any(
                    c.version_id == u.version_id for c in chosen)):
                continue
            used.add(key)
            u.stratum = name  # type: ignore[attr-defined]
            chosen.append(u)
            n += 1
            if n == quota[name]:
                break
    return chosen


def _records(unit: Unit, ext: Extraction | None) -> dict[str, set]:
    """Accepted (evidence-verified) records keyed for cross-model agreement."""
    out = (ext.output if ext else None) or {}
    keys: dict[str, set] = {"fact": set(), "milestone": set(), "signal": set()}
    for section, kind in (("facts", "fact"), ("milestones", "milestone"),
                          ("procurement_signals", "signal")):
        for r in out.get(section) or []:
            if not isinstance(r, dict):
                continue
            if unit.kind != "pdf_vision" and verify(str(r.get("evidence") or ""), unit,
                                                    r.get("page")) is False:
                continue
            if kind == "fact":
                keys[kind].add((r.get("field"), _fact_gist(r)))
            elif kind == "milestone":
                keys[kind].add((r.get("type"), (r.get("date") or "")[:7]))
            else:
                keys[kind].add((r.get("kind"), " ".join(sorted(_norm(r.get("title")).split()))))
    return keys


def _fact_gist(r: dict[str, Any]) -> str:
    """Loose identity for cross-model agreement: the first number in the value if any
    ("88 permanently affordable homes" ~ "88"), else the first three words."""
    v = str(r.get("value_text") or "")
    if isinstance(r.get("value_num"), int | float):
        return f"{r['value_num']:g}"
    m = re.search(r"\d[\d,]*(?:\.\d+)?", v)
    if m:
        return m.group(0).replace(",", "")
    return " ".join(_norm(v).split()[:3])


def run_comparison(s: Session, config: Config, run_id: int) -> dict[str, Any]:
    units = build_units(s, config)
    sample = pick_sample(units)
    report: dict[str, Any] = {"sample": [
        {"stratum": u.stratum, "kind": u.kind, "title": u.title, "url": u.url,  # type: ignore
         "pages": f"{u.page_from}-{u.page_to}" if u.page_from else None,
         "chars": len(u.body()), "candidates": len(u.candidates)} for u in sample],
        "models": {}}

    async def go() -> dict[str, list]:
        client = OpenRouter(s, config, run_id, await OpenRouter.structured_capable(config))
        try:
            results = {}
            for model in config.models.compare:
                results[model] = await fetch_outputs(s, config, sample, model, run_id, client)
            return results
        finally:
            await client.aclose()

    results = asyncio.run(go())
    per_model_records: dict[str, list[dict[str, set]]] = {}
    for model, (pairs, fstats) in results.items():
        calls = {c.id: c for c in s.scalars(select(LlmCall).where(
            LlmCall.id.in_([e.llm_call_id for _, e in pairs if e and e.llm_call_id])))}
        vstats = apply_outputs(s, pairs, model, run_id, dry_run=True)
        rows = [(u, e, calls.get(e.llm_call_id) if e else None) for u, e in pairs]
        lat = [c.latency_ms for _, _, c in rows if c]
        report["models"][model] = {
            "units": len(pairs), "ok": sum(1 for _, e in pairs if e), **fstats, **vstats,
            "tokens_in": sum(c.tokens_in or 0 for _, _, c in rows if c),
            "tokens_out": sum(c.tokens_out or 0 for _, _, c in rows if c),
            "tokens_reasoning": sum(c.tokens_reasoning or 0 for _, _, c in rows if c),
            "cost_usd": round(sum(c.cost_usd or 0 for _, _, c in rows if c), 5),
            "latency_p50_ms": int(statistics.median(lat)) if lat else None,
            "latency_max_ms": max(lat) if lat else None,
            "per_kind": _per_kind(rows),
        }
        per_model_records[model] = [_records(u, e) if e else None for u, e in pairs]
    report["agreement"] = _agreement(per_model_records, sample)
    report["projection"] = _project(units, report["models"], config)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "model_comparison.json").write_text(json.dumps(report, indent=2, default=str),
                                                   encoding="utf-8")
    return report


def _per_kind(rows: list) -> dict[str, dict[str, Any]]:
    """Measured (chars, tokens in, tokens out, cost) per unit, grouped by unit kind."""
    acc: dict[str, dict[str, Any]] = defaultdict(lambda: {"units": []})
    for u, _, c in rows:
        if c is None:
            continue
        acc[u.kind]["units"].append([len(u.body()), c.tokens_in or 0, c.tokens_out or 0,
                                     c.cost_usd or 0.0])
    return dict(acc)


def _fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares y = a + b*x (falls back to the mean when x doesn't vary)."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if len(points) < 3 or sxx == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in points) / sxx
    return my - b * mx, b


def _agreement(records: dict[str, list[dict[str, set] | None]],
               sample: list[Unit]) -> dict[str, Any]:
    models = list(records)
    if len(models) < 2:
        return {}
    a, b = models[:2]
    both = [i for i in range(len(sample))
            if records[a][i] is not None and records[b][i] is not None]
    out: dict[str, Any] = {"models": [a, b], "units_compared": len(both)}
    for kind in ("fact", "milestone", "signal"):
        ka = {(i, k) for i in both for k in records[a][i][kind]}
        kb = {(i, k) for i in both for k in records[b][i][kind]}
        out[kind] = {"both": len(ka & kb), f"only_{a}": len(ka - kb), f"only_{b}": len(kb - ka),
                     "examples_only_" + a: [(sample[i].title[:50], k) for i, k in
                                            sorted(ka - kb, key=str)[:6]],
                     "examples_only_" + b: [(sample[i].title[:50], k) for i, k in
                                            sorted(kb - ka, key=str)[:6]]}
    return out


def _project(units: list[Unit], models: dict[str, Any], config: Config) -> dict[str, Any]:
    """Full-run projection: measured tokens per unit kind x number of units of that kind."""
    counts = Counter(u.kind for u in units)
    chars = Counter()
    for u in units:
        chars[u.kind] += len(u.body())
    proj: dict[str, Any] = {"units": dict(counts), "input_chars": dict(chars)}
    for model, m in models.items():
        price = config.models.est_price_per_mtok.get(model, {"input": 0, "output": 0})
        total_in = total_out = 0.0
        detail = {}
        for kind, n in counts.items():
            pts = (m["per_kind"].get(kind) or {}).get("units") or []
            if not pts:
                continue
            if kind == "pdf_vision":  # image tokens don't scale with the tiny text layer
                tin = statistics.fmean(p[1] for p in pts) * n
            else:  # fixed prompt per unit + tokens per character of document text
                a, b = _fit([(p[0], p[1]) for p in pts])
                tin = a * n + b * chars[kind]
            outs = [p[2] for p in pts]
            tout = statistics.fmean(outs) * n
            detail[kind] = {"tokens_in": int(tin), "tokens_out": int(tout),
                            "sample_units": len(pts),
                            "out_tokens_p50": int(statistics.median(outs)),
                            "out_tokens_max": max(outs)}
            total_in += tin
            total_out += tout
        cost = (total_in * price["input"] + total_out * price["output"]) / 1e6
        sample_cost = m["cost_usd"]
        sample_est = (m["tokens_in"] * price["input"] + m["tokens_out"] * price["output"]) / 1e6
        # scale list-price estimate by the billed/listed ratio seen on the sample
        ratio = sample_cost / sample_est if sample_est else 1.0
        proj[model] = {"tokens_in": int(total_in), "tokens_out": int(total_out),
                       "list_price_usd": round(cost, 3),
                       "billed_to_list_ratio_on_sample": round(ratio, 3),
                       "projected_billed_usd": round(cost * ratio, 3),
                       "requests": sum(counts.values()), "by_kind": detail}
    return proj


def markdown(r: dict[str, Any]) -> str:
    models = list(r["models"])
    L = ["# Extraction model comparison", "",
         "Generated by `createto compare`: the same stratified sample of real units sent to each "
         "model with prompt `extract_v1` (nothing written to the claims tables). Costs are "
         "OpenRouter-billed amounts from the usage accounting of each call.", "",
         "## Sample", "", "| stratum | kind | document | pages | chars | candidates |",
         "|---|---|---|---|---|---|"]
    for u in r["sample"]:
        L.append(f"| {u['stratum']} | {u['kind']} | {u['title'][:60]} | {u['pages'] or ''} | "
                 f"{u['chars']} | {u['candidates']} |")
    rows = [("units answered (valid JSON)", lambda m: f"{m['ok']}/{m['units']}"),
            ("invalid JSON", lambda m: m.get("invalid_json", 0)),
            ("errors", lambda m: m.get("unit_errors", 0))]
    for k in ("fact", "milestone", "signal"):
        rows.append((f"{k}s proposed / accepted", lambda m, k=k:
                     f"{m.get(k + '_proposed', 0)} / {m.get(k + '_accepted', 0)}"))
        rows.append((f"{k}s rejected (quote not in source)", lambda m, k=k:
                     m.get(k + "_rejected_evidence", 0)))
    rows += [("evidence pass rate (text units)", _pass_rate),
             ("tokens in / out (of which reasoning)", lambda m:
              f"{m['tokens_in']:,} / {m['tokens_out']:,} ({m['tokens_reasoning']:,})"),
             ("billed cost (sample)", lambda m: f"${m['cost_usd']:.4f}"),
             ("latency p50 / max", lambda m: f"{(m['latency_p50_ms'] or 0) / 1000:.1f}s / "
                                             f"{(m['latency_max_ms'] or 0) / 1000:.1f}s")]
    L += ["", "## Results", "", "| | " + " | ".join(models) + " |",
          "|---|" + "---|" * len(models)]
    for label, fn in rows:
        L.append(f"| {label} | " + " | ".join(str(fn(r["models"][m])) for m in models) + " |")
    ag = r.get("agreement") or {}
    if ag:
        a, b = ag["models"]
        L += ["", "## Agreement on accepted records", "",
              f"Only the {ag['units_compared']} units both models answered; facts match on "
              "field + first number (or first three words) of the value.", "",
              f"| | both | only {a} | only {b} |", "|---|---|---|---|"]
        for k in ("fact", "milestone", "signal"):
            L.append(f"| {k}s | {ag[k]['both']} | {ag[k]['only_' + a]} | {ag[k]['only_' + b]} |")
        for k in ("fact", "signal"):
            for m in (a, b):
                ex = ag[k].get("examples_only_" + m) or []
                if ex:
                    L += ["", f"Examples of {k}s only {m} found:", ""]
                    L += [f"- {doc}: `{key}`" for doc, key in ex]
    p = r["projection"]
    L += ["", "## Full-run projection", "",
          f"Units in a full first run: {p['units']} (input characters: {p['input_chars']}).",
          "Input tokens are fitted per unit kind as fixed prompt + tokens per character; output "
          "tokens use the sample mean per unit (reasoning tokens included). Unchanged repeat "
          "runs are served from the `extractions` cache: 0 requests, $0.", "",
          "| model | requests | tokens in | tokens out | list price | billed/list on sample | "
          "projected billed |", "|---|---|---|---|---|---|---|"]
    for m in models:
        q = p.get(m)
        if q:
            L.append(f"| {m} | {q['requests']} | {q['tokens_in']:,} | {q['tokens_out']:,} | "
                     f"${q['list_price_usd']:.2f} | {q['billed_to_list_ratio_on_sample']} | "
                     f"**${q['projected_billed_usd']:.2f}** |")
    return "\n".join(L) + "\n"


def _pass_rate(m: dict[str, Any]) -> str:
    acc = sum(m.get(f"{k}_accepted", 0) - m.get(f"{k}_unverified_vision", 0)
              for k in ("fact", "milestone", "signal"))
    rej = sum(m.get(f"{k}_rejected_evidence", 0) for k in ("fact", "milestone", "signal"))
    return f"{acc / (acc + rej):.0%}" if acc + rej else "n/a"
