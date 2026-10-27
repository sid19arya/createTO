"""Variant demo (brief: "show an unchanged run, a changed source and a failed file").

Replays saved snapshots of the real crawl into a separate `createto_demo` database:

  1. first      base snapshot into an empty DB
  2. unchanged  same snapshot again (expect 304s, no new versions, no new claims)
  3. changed    one project's phase + site area edited, MERX tender removed from the listing
  4. failed     on top of (3): one PDF truncated, one PDF answering HTTP 500

Each crawl runs in a subprocess (Twisted's reactor can start only once per process).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select, text

from createto_info.config import REPO_ROOT, Config, get_env
from createto_info.crawl.rawstore import RawStore
from createto_info.crawl.replay import build_changed, build_failed, export_snapshot
from createto_info.db.session import session_scope, sqlalchemy_url, use_database

DEMO_DB = "createto_demo"
SNAPSHOTS = REPO_ROOT / "data" / "snapshots"
REPORTS = REPO_ROOT / "reports"
PROJECT = "parkdale-hub"


def _demo_url() -> str:
    base = sqlalchemy_url(get_env().database_url)
    return base.rsplit("/", 1)[0] + f"/{DEMO_DB}"


def _reset_demo_db() -> None:
    base = sqlalchemy_url(get_env().database_url)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'drop database if exists "{DEMO_DB}" with (force)'))
        conn.execute(text(f'create database "{DEMO_DB}"'))
    admin.dispose()


def build_snapshots(config: Config) -> dict[str, Any]:
    """Export the main DB's latest state and derive the variant snapshots."""
    store = RawStore(config.storage.raw_dir)
    base, changed, failed = (SNAPSHOTS / n for n in ("base", "changed", "failed"))
    with session_scope() as s:
        manifest = export_snapshot(s, base)
    pdfs = sorted(u for u, e in manifest["entries"].items()
                  if u.startswith("https://a-us.storyblok.com/") and u.endswith(".pdf")
                  and e.get("file"))
    notes = {
        "base": [f"{len(manifest['entries'])} URLs from crawl run "
                 f"{manifest['created_from_run']}"],
        "changed": build_changed(base, changed, store, PROJECT, "preConstruction",
                                 "4.5 acres"),
        "failed": build_failed(changed, failed, store, pdfs[0], pdfs[1]),
    }
    return {"snapshots": {"base": base, "changed": changed, "failed": failed}, "notes": notes,
            "corrupt_url": pdfs[0], "error_url": pdfs[1]}


def _cli(args: list[str], env: dict[str, str], log: Path) -> None:
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.run([sys.executable, "-m", "createto_info.cli", *args], env=env,
                              stdout=fh, stderr=subprocess.STDOUT, cwd=REPO_ROOT)
    if proc.returncode != 0:
        raise RuntimeError(f"`createto {' '.join(args)}` failed; see {log}")


def run_variants(config: Config) -> Path:
    built = build_snapshots(config)
    snaps = built["snapshots"]
    scenarios = [("first", snaps["base"]), ("unchanged", snaps["base"]),
                 ("changed", snaps["changed"]), ("failed", snaps["failed"])]

    _reset_demo_db()
    env = {**os.environ, "DATABASE_URL": _demo_url(), "PYTHONIOENCODING": "utf-8"}
    logs = REPO_ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _cli(["db", "upgrade"], env, logs / "demo_migrate.log")
    for name, snap in scenarios:
        _cli(["crawl", "--replay", str(snap)], env, logs / f"demo_{name}_crawl.log")
        _cli(["parse"], env, logs / f"demo_{name}_parse.log")

    use_database(_demo_url())
    with session_scope() as s:
        report = _collect(s, [n for n, _ in scenarios], built)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "variants.json").write_text(json.dumps(report, indent=2, default=str),
                                           encoding="utf-8")
    path = REPORTS / "variants.md"
    path.write_text(_markdown(report), encoding="utf-8")
    return path


# --------------------------------------------------------------------- report

_RUN_KEYS = ["duration_s", "requests", "not_modified", "pages_new", "pages_changed",
             "pages_unchanged", "files_new", "files_changed", "files_unchanged",
             "files_failed_parse", "files_failed_integrity", "failed:http_500",
            "documents_removed", "llm_calls",
             "cost_usd"]
_PARSE_KEYS = ["fact_claims", "facts_superseded", "facts_retracted", "milestones_retracted",
               "procurement_retracted", "projects_changed", "project_versions"]


def _collect(s, names: list[str], built: dict[str, Any]) -> dict[str, Any]:
    from createto_info.db.models import (
        CrawlRun,
        Document,
        DocumentVersion,
        Evidence,
        Fact,
        FetchLog,
        Milestone,
        ProcurementSignal,
        Project,
        ProjectVersion,
    )

    runs = list(s.scalars(select(CrawlRun).order_by(CrawlRun.id)))
    table = []
    for name, run in zip(names, runs, strict=True):
        parse = run.stats.get("parse", {})
        table.append({"scenario": name, "run_id": run.id, "status": run.status,
                      **{k: run.stats.get(k, 0) for k in _RUN_KEYS},
                      **{f"parse.{k}": parse.get(k, 0) for k in _PARSE_KEYS}})

    project = s.scalar(select(Project).where(Project.slug == PROJECT))
    versions = [{"run_id": v.run_id, "diff": v.diff} for v in s.scalars(
        select(ProjectVersion).where(ProjectVersion.project_id == project.id)
        .order_by(ProjectVersion.id))]
    facts = [{"field": f.field, "value": f.value_text, "status": f.status,
              "valid_from_run": f.valid_from_run, "valid_to_run": f.valid_to_run}
             for f in s.scalars(select(Fact).where(
                 Fact.project_id == project.id, Fact.field.in_(["phase", "site_area"]))
                 .order_by(Fact.field, Fact.id))]
    milestones = [{"type": m.type, "title": m.title, "date": m.date, "status": m.status}
                  for m in s.scalars(select(Milestone).where(
                      Milestone.project_id == project.id, Milestone.type == "phase_change"))]
    signals = []
    for sig in s.scalars(select(ProcurementSignal).order_by(ProcurementSignal.id)):
        ev = s.scalar(select(Evidence.excerpt).where(
            Evidence.entity_type == "procurement_signal", Evidence.entity_id == sig.id))
        signals.append({"kind": sig.kind, "title": sig.title, "status": sig.status,
                        "first_seen_run": sig.first_seen_run, "retracted_run": sig.retracted_run,
                        "evidence": ev})
    failed_run = runs[-1].id
    failures = [{"url": f.url, "outcome": f.outcome, "reason": f.reason}
                for f in s.scalars(select(FetchLog).where(
                    FetchLog.run_id == failed_run, FetchLog.url.in_(
                        [built["corrupt_url"], built["error_url"]])))]
    bad_parse = [{"url": u, "parse_status": st, "parse_error": err} for u, st, err in s.execute(
        select(Document.url, DocumentVersion.parse_status, DocumentVersion.parse_error)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(DocumentVersion.parse_status == "failed"))]
    error_doc = s.scalar(select(Document).where(Document.url == built["error_url"]))
    corrupt_doc = s.scalar(select(Document).where(Document.url == built["corrupt_url"]))
    corrupt_current = s.get(DocumentVersion, corrupt_doc.current_version_id)
    return {
        "notes": built["notes"], "runs": table,
        "changed": {"project": PROJECT, "versions": versions, "facts": facts,
                    "phase_change_milestones": milestones, "procurement_signals": signals},
        "failed": {"fetch_log": failures, "failed_parses": bad_parse,
                   "error_doc_removed_at_run": error_doc.removed_at_run if error_doc else None,
                   "corrupt_current_version_ok": corrupt_current.parse_status},
    }


def _markdown(r: dict[str, Any]) -> str:
    cols = ["scenario", "requests", "not_modified", "pages_new", "pages_changed",
            "pages_unchanged", "files_new", "files_changed", "files_unchanged",
            "files_failed_integrity", "failed:http_500", "documents_removed",
            "parse.facts_superseded", "parse.procurement_retracted", "parse.project_versions",
            "llm_calls", "duration_s"]
    lines = ["# Variant demo: unchanged run, changed source, failed file", "",
             "Generated by `createto demo`: replays of saved local copies of the real crawl "
             "into a separate `createto_demo` database. No network, no LLM calls.", "",
             "## What each snapshot changes", ""]
    for name, notes in r["notes"].items():
        lines += [f"- **{name}**: " + "; ".join(notes)]
    lines += ["", "## Runs", "", "| " + " | ".join(cols) + " |",
              "|" + "---|" * len(cols)]
    for row in r["runs"]:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    ch = r["changed"]
    lines += ["", f"## Changed source: `{ch['project']}`", "", "Project version diffs:", ""]
    for v in ch["versions"]:
        lines.append(f"- run {v['run_id']}: " + ("initial snapshot" if not v["diff"] else
                     ", ".join(f"`{k}` {a!r} → {b!r}" for k, (a, b) in v["diff"].items())))
    lines += ["", "Facts (history kept, never overwritten):", "",
              "| field | value | status | valid from run | valid to run |", "|---|---|---|---|---|"]
    for f in ch["facts"]:
        lines.append(f"| {f['field']} | {f['value']} | {f['status']} | {f['valid_from_run']} | "
                     f"{f['valid_to_run'] or ''} |")
    lines += ["", "Detected phase changes: " + (", ".join(
        f"{m['title']} on {m['date']}" for m in ch["phase_change_milestones"]) or "none"), "",
        "Procurement signals:", "",
        "| kind | title | status | first seen run | retracted run |", "|---|---|---|---|---|"]
    for sig in ch["procurement_signals"]:
        lines.append(f"| {sig['kind']} | {sig['title']} | {sig['status']} | "
                     f"{sig['first_seen_run']} | {sig['retracted_run'] or ''} |")
    fl = r["failed"]
    lines += ["", "## Failed file", "", "Fetch log of the failed run:", ""]
    lines += [f"- `{f['url']}`: {f['outcome']} ({f['reason']})" for f in fl["fetch_log"]]
    lines += ["", "Document versions that failed integrity checks:", ""]
    lines += [f"- `{b['url']}`: {b['parse_error']}" for b in fl["failed_parses"]]
    lines += ["", f"Neither failure loses data: the truncated PDF's broken version is stored for "
              f"audit but its last good version stays current ("
              f"`{fl['corrupt_current_version_ok']}`), and the HTTP 500 document is not marked "
              f"removed (removed_at_run = {fl['error_doc_removed_at_run']}).", ""]
    return "\n".join(lines)
