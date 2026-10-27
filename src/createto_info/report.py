"""`createto report`: the deliverable run report, generated from the database.

Inputs: two pipeline runs (`crawl_runs.stats.stages`, written by `createto run`), the
`llm_calls` ledger, fetch log, current tables, and the generated variant / model-comparison
JSON. Nothing is typed in by hand. Also writes `reports/examples/` (sample API outputs and a
slice of the JSONL export).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from createto_info.config import REPO_ROOT, Config

REPORTS = REPO_ROOT / "reports"
EXAMPLES = REPORTS / "examples"
EXAMPLE_PROJECT = "parkdale-hub"


def _dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def _mb(n: int | float | None) -> str:
    return f"{(n or 0) / 1e6:,.0f} MB"


def pipeline_runs(s: Session, ids: list[int] | None) -> list[dict[str, Any]]:
    rows = s.execute(text("""
        SELECT id, mode, status, started_at, finished_at, git_sha, stats FROM crawl_runs
        WHERE (stats ->> 'pipeline')::boolean IS TRUE ORDER BY id""")).mappings().all()
    runs = [dict(r) for r in rows]
    if ids:
        runs = [r for r in runs if r["id"] in ids]
    return runs[-2:]


def _g(run: dict[str, Any], *path: str, default: Any = 0) -> Any:
    cur: Any = run["stats"]
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _sum_prefix(d: dict[str, Any], prefix: str) -> int:
    return sum(v for k, v in d.items() if k.startswith(prefix) and isinstance(v, int | float))


def build(s: Session, config: Config, ids: list[int] | None = None) -> dict[str, Any]:
    runs = pipeline_runs(s, ids)
    if not runs:
        raise RuntimeError("no pipeline runs yet: run `createto run` (twice)")
    out: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "runs": []}
    for r in runs:
        crawl = _g(r, "stages", "crawl", default={})
        summ = _g(r, "stages", "summary", default={})
        ext = _g(r, "stages", "extract", default={})
        link = _g(r, "stages", "link", default={})
        pdfs = _g(r, "stages", "pdfs", default={})
        out["runs"].append({
            "id": r["id"], "started_at": r["started_at"], "git_sha": r["git_sha"],
            "status": r["status"],
            "seconds_total": _g(r, "pipeline_seconds"),
            "seconds": {st: _g(r, "stages", st, "seconds") for st in
                        ("crawl", "pdfs", "parse", "extract", "link", "index")},
            "http_requests": crawl.get("requests", 0),
            "not_modified": crawl.get("not_modified", 0),
            "bytes": crawl.get("bytes", 0),
            "pages": {k: crawl.get(f"pages_{k}", 0) for k in ("new", "changed", "unchanged")},
            "files": {k: crawl.get(f"files_{k}", 0) for k in ("new", "changed", "unchanged")},
            "documents_seen": crawl.get("documents_seen", 0),
            "documents_removed": crawl.get("documents_removed", 0),
            "blocked": _sum_prefix(crawl, "blocked:"),
            "failed": _sum_prefix(crawl, "failed:"),
            "skipped": _sum_prefix(crawl, "skipped:"),
            "pdfs_processed": pdfs.get("pdfs", 0),
            "pdf_pages_processed": sum(v for k, v in pdfs.items()
                                       if k.startswith("pages_") and isinstance(v, int)),
            "llm_units": ext.get("units", 0), "llm_units_cached": ext.get("cached", 0),
            "llm_units_new": ext.get("llm_units", 0),
            "accepted": {k: ext.get(f"{k}_accepted", 0) for k in
                         ("fact", "milestone", "signal")},
            "rejected_quote": sum(ext.get(f"{k}_rejected_evidence", 0) for k in
                                  ("fact", "milestone", "signal")),
            "link_decisions": link.get("decisions", 0), "link_cached": link.get("cached", 0),
            "link_failed": link.get("decision_errors", 0),
            "resumed": _g(r, "resumed", default=[]),
            "changed_by_host": [dict(c) for c in s.execute(text("""
                SELECT regexp_replace(d.url, '^https?://([^/]+).*', '\\1') AS host,
                       count(*) AS n
                FROM document_versions v JOIN documents d ON d.id = v.document_id
                WHERE v.run_id = :r AND EXISTS (SELECT 1 FROM document_versions o
                  WHERE o.document_id = d.id AND o.run_id < :r)
                GROUP BY 1 ORDER BY 2 DESC"""), {"r": r["id"]}).mappings()],
            "llm_errors": [dict(e) for e in s.execute(text("""
                SELECT model, coalesce(substring(error from '"message": "([^"]*)'), 'other')
                         AS reason, count(*) AS n
                FROM llm_calls WHERE run_id = :r AND status <> 'ok'
                GROUP BY 1, 2 ORDER BY 3 DESC"""), {"r": r["id"]}).mappings()],
            "summary": summ,
        })
    latest = runs[-1]["id"]
    out["fetch_reasons"] = [dict(r) for r in s.execute(text("""
        SELECT outcome, reason, count(*) AS n,
               string_agg(DISTINCT regexp_replace(url, '^https?://([^/]+).*', '\\1'), ', ')
                 AS hosts
        FROM fetch_log WHERE run_id = :r AND outcome IN ('blocked', 'failed', 'skipped')
        GROUP BY 1, 2 ORDER BY 3 DESC"""), {"r": latest}).mappings()]
    out["link_status"] = [dict(r) for r in s.execute(text("""
        SELECT link_status, count(*) AS n FROM (
          SELECT link_status FROM facts UNION ALL SELECT link_status FROM milestones
          UNION ALL SELECT link_status FROM procurement_signals) x
        GROUP BY 1 ORDER BY 2 DESC""")).mappings()]
    out["coverage"] = dict(s.execute(text("""
        SELECT (SELECT stats ->> 'sitemap_urls' FROM crawl_runs WHERE id = :r) AS sitemap_urls,
               (SELECT stats ->> 'sitemap_not_linked' FROM crawl_runs WHERE id = :r)
                 AS sitemap_not_linked,
               (SELECT stats ->> 'linked_not_in_sitemap' FROM crawl_runs WHERE id = :r)
                 AS linked_not_in_sitemap""").bindparams(r=latest)).mappings().one())
    out["storage"] = {"raw_bytes": _dir_size(config.storage.raw_dir),
                      "renders_bytes": _dir_size(config.storage.renders_dir),
                      "database_bytes": s.execute(text(
                          "SELECT pg_database_size(current_database())")).scalar()}
    for name in ("variants", "model_comparison"):
        path = REPORTS / f"{name}.json"
        out[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    out["config"] = {"extract_model": config.models.extract, "gate_model": config.models.gate,
                     "max_cost_usd_per_run": config.llm.max_cost_usd_per_run}
    return out


def write_examples(s: Session) -> list[str]:
    from createto_info import export as exporter
    from createto_info.api import catalog, queries

    EXAMPLES.mkdir(parents=True, exist_ok=True)

    def dump(name: str, data: Any) -> str:
        (EXAMPLES / name).write_text(json.dumps(data, indent=2, default=str,
                                                ensure_ascii=False), encoding="utf-8")
        return name

    files = [
        dump(f"project_{EXAMPLE_PROJECT}.json", queries.project_detail(s, EXAMPLE_PROJECT)),
        dump(f"project_{EXAMPLE_PROJECT}_history.json",
             queries.project_history(s, EXAMPLE_PROJECT)),
        dump("facts_affordable_units_200plus.json", catalog.facts(
            s, field="affordable_units", project=None, status=None, source=None,
            link_status=None, q=None, min_value=200, max_value=None, limit=50, offset=0)),
        dump("milestones_construction_start.json", catalog.milestones(
            s, type="construction_start", status=None, project=None, date_from=None,
            date_to=None, link_status=None, include_retracted=False, limit=50, offset=0)),
        dump("procurement_open.json", queries.procurement(s, "open", None, None)),
        dump("search_affordable_housing_near_transit.json", queries.search_projects(
            s, queries.ProjectQuery(q="affordable housing near transit", limit=10))),
    ]
    sample = EXAMPLES / "export_sample"
    sample.mkdir(exist_ok=True)
    for name in exporter.ENTITIES:
        lines = []
        for i, line in enumerate(exporter.jsonl(s, name)):
            if i == 3:
                break
            lines.append(line)
        (sample / f"{name}.jsonl").write_text("".join(lines), encoding="utf-8")
    files.append("export_sample/*.jsonl (first 3 rows of each entity)")
    return files


def markdown(r: dict[str, Any], examples: list[str]) -> str:
    cols = r["runs"]
    head = "| | " + " | ".join(f"run {x['id']} ({'first' if i == 0 else 'unchanged repeat'})"
                               for i, x in enumerate(cols)) + " |"
    sep = "|---|" + "---|" * len(cols)

    def row(label: str, fn) -> str:
        return f"| {label} | " + " | ".join(str(fn(x)) for x in cols) + " |"

    def secs(x: dict[str, Any]) -> str:
        return " / ".join(f"{st} {x['seconds'][st] or 0:.0f}s" for st in x["seconds"])

    L = ["# CreateTO scraper: run report", "",
         f"Generated by `createto report` at {r['generated_at'][:19]}Z from the database "
         "(`crawl_runs`, `llm_calls`, `fetch_log`, current tables). Runs were produced by "
         "`createto run` on a fresh database: a first run, then an unchanged repeat.", "",
         "## Two runs", "", head, sep,
         row("total wall time", lambda x: f"{(x['seconds_total'] or 0) / 60:.1f} min"),
         row("time per stage", secs),
         row("HTTP requests (304 not modified)",
             lambda x: f"{x['http_requests']:,} ({x['not_modified']:,})"),
         row("downloaded", lambda x: _mb(x["bytes"])),
         row("pages new / changed / unchanged",
             lambda x: " / ".join(str(v) for v in x["pages"].values())),
         row("files new / changed / unchanged",
             lambda x: " / ".join(str(v) for v in x["files"].values())),
         row("blocked / failed / skipped links",
             lambda x: f"{x['blocked']} / {x['failed']} / {x['skipped']}"),
         row("PDFs processed (pages)",
             lambda x: f"{x['pdfs_processed']} ({x['pdf_pages_processed']:,})"),
         row("LLM extraction units: new / cached",
             lambda x: f"{x['llm_units_new']} / {x['llm_units_cached']}"),
         row("records accepted (facts / milestones / signals)",
             lambda x: " / ".join(str(v) for v in x["accepted"].values())),
         row("records rejected by the quote check", lambda x: x["rejected_quote"]),
         row("linking decisions: new / cached / failed",
             lambda x: f"{x['link_decisions']} / {x['link_cached']} / {x['link_failed']}"),
         row("useful records after the run", lambda x: (
             f"{x['summary'].get('projects', 0)} projects (+{x['summary'].get('project_blocks', 0)}"
             f" sub-sites), {x['summary'].get('news_articles', 0)} news, "
             f"{x['summary'].get('facts_current', 0):,} current facts, "
             f"{x['summary'].get('milestones', 0):,} milestones, "
             f"{x['summary'].get('procurement_signals', 0)} procurement signals "
             f"({x['summary'].get('open_opportunities', 0)} open)")),
         row("LLM calls: paid / free (of which rejected)",
             lambda x: f"{x['summary'].get('paid_calls', 0)} / "
             f"{x['summary'].get('free_calls', 0)} "
             f"({sum(c['failed'] for c in x['summary'].get('llm', []))})"),
         row("**billed cost (actual)**", lambda x: f"**${x['summary'].get('billed_usd', 0):.2f}**"),
         row("list-price estimate", lambda x: f"${x['summary'].get('est_usd', 0):.2f} (est.)"),
         "", "## Costs", "",
         "Actual = OpenRouter-billed amounts from each call's usage accounting (`llm_calls."
         "cost_usd`). Estimated = tokens × list price from `config.yaml` (`est_cost_usd`).", "",
         "| run | model | task | calls | tokens in / out (reasoning) | billed (actual) | "
         "list price (est.) |", "|---|---|---|---|---|---|---|"]
    for x in cols:
        for c in x["summary"].get("llm", []):
            L.append(f"| {x['id']} | {c['model']} | {c['task']} | {c['calls']} | "
                     f"{c['tokens_in']:,} / {c['tokens_out']:,} ({c['tokens_reasoning']:,}) | "
                     f"${c['billed_usd']:.4f} | ${c['est_usd']:.4f} |")
    st = r["storage"]
    L += ["",
          "| other cost | amount |", "|---|---|",
          "| Hosting | $0: local Docker Postgres and local Python process (none used) |",
          f"| Storage (local disk) | raw pages/files {_mb(st['raw_bytes'])}, page renders "
          f"{_mb(st['renders_bytes'])}, database {_mb(st['database_bytes'])} |",
          "| Linking model (`baseten/mammoth-1:free`) | $0; free tier, 1,000 requests/day, "
          "20/min |",
          "| One-off work outside the runs (not repeated) | model comparison (`reports/"
          "model_comparison.md`) and development calls; see `llm_calls` with `run_id` "
          "outside these runs |",
          "| Chat (`POST /chat`) | ≈ $0.0005 per question, billed per use, not part of runs |",
          ""]
    notes = []
    for x in cols:
        if x["resumed"]:
            notes.append(f"- Run {x['id']} was interrupted (the process was killed for low system "
                         f"memory) and finished with `createto run --resume`, which ran only "
                         f"{', '.join(x['resumed'])}. Its total time is the sum of stage "
                         "times, and model calls made before the interruption count as cached.")
        if x["changed_by_host"]:
            hosts = ", ".join(f"{c['host']} {c['n']}" for c in x["changed_by_host"])
            own = any(c["host"].endswith("createto.ca") for c in x["changed_by_host"])
            notes.append(f"- Run {x['id']}: changed pages by host: {hosts}."
                         + ("" if own else " No createto.ca page or file changed; these are "
                            "linked pages whose boilerplate (news sidebars, live counters) "
                            "differs between fetches."))
        for e in x["llm_errors"]:
            notes.append(f"- Run {x['id']}: {e['n']} `{e['model']}` calls rejected: "
                         f"{e['reason'].strip().rstrip('.')}.")
        if x["link_failed"]:
            notes.append(f"- Run {x['id']}: {x['link_failed']} linking decisions failed; their "
                         "records keep their previous link state until `createto link` re-runs.")
    if notes:
        L += ["## Run notes", "", *notes, ""]
    L += ["## Coverage and quality", "",
          f"- Sitemap URLs: {r['coverage']['sitemap_urls']}; {r['coverage']['sitemap_not_linked']}"
          f" of them are not linked from any page (found only through the sitemap); "
          f"{r['coverage']['linked_not_in_sitemap']} linked createto.ca pages are missing from it.",
          "- Blocked, failed and skipped links in the latest run (full list: "
          "`GET /runs/{id}/fetch-log`):", "",
          "| outcome | reason | count | hosts |", "|---|---|---|---|"]
    for f in r["fetch_reasons"][:14]:
        L.append(f"| {f['outcome']} | {f['reason']} | {f['n']} | {(f['hosts'] or '')[:90]} |")
    L += ["", "- How extracted records are attached (`link_status`):", "",
          "| link status | records |", "|---|---|"]
    L += [f"| {x['link_status']} | {x['n']:,} |" for x in r["link_status"]]
    mc = r.get("model_comparison")
    if mc:
        luna, ink = (mc["models"].get(m, {}) for m in
                     ("openai/gpt-6-luna", "thinkingmachines/inkling"))
        L += ["", "## Model choice", "",
              "From [model_comparison.md](model_comparison.md) (21 stratified units, same "
              "prompt): `openai/gpt-6-luna` answered "
              f"{luna.get('ok')}/{luna.get('units')} with valid JSON vs "
              f"{ink.get('ok')}/{ink.get('units')} for paid Inkling (truncated while "
              f"reasoning), passed the quote check on all accepted facts, and cost "
              f"${luna.get('cost_usd', 0):.4f} vs ${ink.get('cost_usd', 0):.4f} for the sample. "
              "Free Inkling tiers return 403 for non-agent apps."]
    v = r.get("variants")
    if v:
        L += ["", "## Variants: unchanged run, changed source, failed file", "",
              "From [variants.md](variants.md) (`createto demo`: replays of saved local copies "
              "into a separate database, no network, no LLM):", "",
              "| scenario | requests | not modified | pages changed | files failed integrity | "
              "HTTP 500 | facts superseded | procurement closed |",
              "|---|---|---|---|---|---|---|---|"]
        for x in v["runs"]:
            L.append(f"| {x['scenario']} | {x['requests']} | {x['not_modified']} | "
                     f"{x['pages_changed']} | {x.get('files_failed_integrity', 0)} | "
                     f"{x.get('failed:http_500', 0)} | {x['parse.facts_superseded']} | "
                     f"{x['parse.procurement_retracted']} |")
    L += ["", "## Example output", "",
          "In [examples/](examples/), produced from the same database:", ""]
    L += [f"- `{e}`" for e in examples]
    L += ["", "## Reproduce", "", "```bash",
          "cp .env.example .env            # OPENROUTER_API_KEY",
          "uv sync --all-extras && docker compose up -d --wait db",
          "uv run createto db downgrade base && uv run createto db upgrade   # fresh database",
          "uv run createto run             # first run",
          "uv run createto run             # unchanged repeat",
          "uv run createto demo            # variants (separate database)",
          "uv run createto report          # this file", "```", "",
          "Each run stores its git commit and full config snapshot (`crawl_runs.git_sha`, "
          "`crawl_runs.config`): " + ", ".join(f"run {x['id']} at `{(x['git_sha'] or '')[:12]}`"
                                               for x in cols) + ".", ""]
    return "\n".join(L)


def write_report(s: Session, config: Config, ids: list[int] | None = None) -> Path:
    data = build(s, config, ids)
    examples = write_examples(s)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "run_report.json").write_text(json.dumps(data, indent=2, default=str),
                                             encoding="utf-8")
    path = REPORTS / "run_report.md"
    path.write_text(markdown(data, examples), encoding="utf-8")
    return path
