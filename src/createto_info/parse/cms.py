"""Materialize domain tables from stored Storyblok records — deterministic, no LLM.

Reads the current version of every createto.ca page (`document_versions.cms_json`) and the
MERX listing, then upserts initiatives, councillors, projects (+ embedded sub-site blocks),
aliases, news, links, CMS facts, timeline milestones, procurement signals and project
version diffs. Safe to re-run: every write is an upsert keyed on stable CMS ids.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from createto_info.config import Config
from createto_info.crawl.rawstore import RawStore
from createto_info.crawl.urls import normalize_url
from createto_info.db.models import (
    Councillor,
    Document,
    DocumentVersion,
    Initiative,
    Link,
    Milestone,
    NewsArticle,
    ProcurementSignal,
    Project,
    ProjectAlias,
    ProjectVersion,
)
from createto_info.parse.claims import Claims, Source, flatten
from createto_info.parse.merx import parse_open_solicitations
from createto_info.parse.text import (
    Address,
    addresses_overlap,
    date_from_title,
    find_addresses,
    parse_iso,
    parse_schedule,
    parse_site_area_acres,
    period_end,
    richtext_links,
    richtext_to_text,
)

SITE = "https://createto.ca/"
CMS_ALIAS_KINDS = ("name", "slug", "address", "intersection", "curated")
CMS = "cms"

# news topic slug -> news_articles.kind
NEWS_KINDS = {
    "createto-board-meetings": "board_meeting",
    "project-updates": "project_update",
    "news-releases": "news_release",
    "createto-insider": "insider",
    "community-engagement": "community_engagement",
    "media-coverage": "media_coverage",
}

# CMS project field -> fact field (docs/EXTRACTION.md vocabulary)
FACT_FIELDS = {
    "address": "address", "ward": "ward", "councillorName": "councillor",
    "majorIntersection": "major_intersection", "siteArea": "site_area",
    "currentUse": "current_use", "proposedUse": "proposed_use",
    "surroundingUse": "surrounding_use", "phase": "phase", "type": "types",
    "delivery": "delivery",
}

_PROCUREMENT = re.compile(
    r"\b(?P<kind>RFP|RFQ|RFSQ|REOI|RFEI|EOI|RFI|RFT"
    r"|Request for (?:Proposals?|Quotations?|Qualifications?|Expressions? of Interest"
    r"|Information|Tenders?)|Market Offering|Call for (?:Proposals|Submissions))\b",
    re.I,
)
_PROCUREMENT_KIND = {
    "rfp": "rfp", "request for proposal": "rfp", "request for proposals": "rfp",
    "call for proposals": "rfp", "call for submissions": "rfp",
    "rfq": "rfq", "request for quotation": "rfq", "request for quotations": "rfq",
    "rfsq": "rfsq", "request for qualification": "rfsq", "request for qualifications": "rfsq",
    "reoi": "eoi", "eoi": "eoi", "request for expression of interest": "eoi",
    "request for expressions of interest": "eoi",
    "rfei": "rfei", "rfi": "rfei", "request for information": "rfei",
    "rft": "tender", "request for tender": "tender", "request for tenders": "tender",
    "market offering": "market_offering",
}


@dataclass
class Page:
    url: str
    version: DocumentVersion
    props: dict[str, Any]

    @property
    def record(self) -> dict[str, Any]:
        return self.props.get("page") or {}

    @property
    def content(self) -> dict[str, Any]:
        return self.record.get("content") or {}

    @property
    def component(self) -> str | None:
        return self.content.get("component")

    @property
    def is_child(self) -> bool:
        return bool(_refs(self.content.get("parentProject")))

    def own_blocks(self, component: str = "projectBlock") -> list[dict[str, Any]]:
        """Sub-site blocks owned by this page. Child pages repeat their siblings' blocks for
        navigation, so only a page without a parent owns the blocks it shows."""
        return [] if self.is_child else list(_walk(self.content.get("blocks"), component))

    @property
    def canonical_url(self) -> str:
        slug = (self.record.get("full_slug") or "").strip("/")
        return normalize_url(SITE + slug) if slug else self.url


def _uuid_of(ref: Any) -> str | None:
    """A story reference may be a bare uuid, a story object, or a multilink."""
    if isinstance(ref, str):
        return ref or None
    if isinstance(ref, dict):
        if ref.get("uuid"):
            return ref["uuid"]
        if isinstance(ref.get("id"), str) and ref["id"]:  # multilink: id is the story uuid
            return ref["id"]
    return None


def _refs(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [u for u in (_uuid_of(i) for i in items) if u]


def _is_story(obj: dict[str, Any]) -> bool:
    """An embedded copy of another story (relation field resolved by the CMS)."""
    return "uuid" in obj and "content" in obj and ("full_slug" in obj or "slug" in obj)


def _walk(obj: Any, component: str):
    """Yield nested components of a type, never descending into embedded stories."""
    if isinstance(obj, dict):
        if _is_story(obj):
            return
        if obj.get("component") == component:
            yield obj
        for v in obj.values():
            yield from _walk(v, component)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, component)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = richtext_to_text(value) if not isinstance(value, str) else value.strip()
    return text or None


class Materializer:
    def __init__(self, session: Session, config: Config, run_id: int):
        self.s = session
        self.config = config
        self.run_id = run_id
        self.store = RawStore(config.storage.raw_dir)
        self.stats: Counter[str] = Counter()
        self.today = datetime.now(UTC).date()
        self.project_ids: dict[str, int] = {}  # cms uuid -> projects.id
        self.initiative_ids: dict[str, int] = {}
        self.pages: dict[str, Page] = {}  # cms uuid -> page (deduped)
        self.claims = Claims(session, run_id)

    # ------------------------------------------------------------- entry

    def run(self) -> Counter[str]:
        self._load_pages()
        by_comp: dict[str, list[Page]] = {}
        for page in self.pages.values():
            by_comp.setdefault(page.component or "", []).append(page)

        for page in by_comp.get("initiative", []):
            self._initiative(page)
        projects = by_comp.get("project", [])
        for page in projects:
            self._project(page)
        for page in projects:
            self._project_relations(page)
        for page in projects:
            self._project_aliases(page)
        for page in projects:
            self._attach_files(page, "project", self.project_ids[page.record["uuid"]])
        for page in by_comp.get("article", []):
            self._news(page)
        for page in projects:
            self._facts(page)
            self._timeline(page)
            self._resource_procurement(page)
        self._market_offerings(projects)
        self._merx()
        for page in projects:
            self._version(page)
        self._reconcile()
        self._cutoff()
        self.s.flush()
        return self.stats

    def _load_pages(self) -> None:
        rows = self.s.execute(
            select(Document.url, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(Document.scope == "first_party", Document.kind == "page",
                   Document.removed_at_run.is_(None), DocumentVersion.cms_json.is_not(None))
            .order_by(Document.url)
        )
        for url, version in rows:
            page = Page(url, version, version.cms_json)
            uuid = page.record.get("uuid")
            if not uuid:
                continue
            # the same story can be served at two URLs; keep the one matching its full_slug
            if uuid not in self.pages or url == page.canonical_url:
                self.pages[uuid] = page
        self.stats["cms_pages"] = len(self.pages)

    # ------------------------------------------------------- initiatives

    def _initiative(self, page: Page) -> None:
        r, c = page.record, page.content
        row = self.s.scalar(select(Initiative).where(Initiative.cms_uuid == r["uuid"]))
        if row is None:
            row = Initiative(cms_uuid=r["uuid"])
            self.s.add(row)
        row.name = r.get("name") or r.get("slug")
        row.slug = r.get("slug")
        row.description = _text(c.get("summary")) or _text(c.get("body"))
        row.url = page.canonical_url
        row.source_version_id = page.version.id
        self.s.flush()
        self.initiative_ids[r["uuid"]] = row.id
        self.stats["initiatives"] += 1

    # ---------------------------------------------------------- projects

    def _councillor(self, name: str | None, link: Any, ward: str | None) -> int | None:
        if not name or not name.strip():
            return None
        name = name.strip()
        row = self.s.scalar(select(Councillor).where(Councillor.name == name))
        if row is None:
            row = Councillor(name=name)
            self.s.add(row)
            self.stats["councillors"] += 1
        url = (link or {}).get("url") or (link or {}).get("cached_url") if isinstance(
            link, dict) else None
        row.profile_url = normalize_url(url) if url else row.profile_url
        row.ward = ward or row.ward
        self.s.flush()
        return row.id

    def _phase_label(self, page: Page, phase: str | None) -> str | None:
        return (page.props.get("projectPhases") or {}).get(phase or "", phase)

    def _upsert_project(self, uuid: str, page: Page, c: dict[str, Any], *, name: str,
                        slug: str, kind: str, summary: str | None,
                        description: str | None) -> Project:
        row = self.s.scalar(select(Project).where(Project.cms_uuid == uuid))
        if row is None:
            row = Project(cms_uuid=uuid, first_seen_run=self.run_id)
            self.s.add(row)
            self.stats[f"{kind}s_new"] += 1
        r = page.record
        initiative = (_refs(c.get("initiative")) or _refs(c.get("initiatives")) or [None])[0]
        row.kind = kind
        row.slug = slug
        row.url = page.canonical_url
        row.name = name
        row.summary = summary
        row.description = description
        row.address = (c.get("address") or "").strip() or None
        row.major_intersection = (c.get("majorIntersection") or "").strip() or None
        row.latitude = _float(c.get("latitude"))
        row.longitude = _float(c.get("longitude"))
        row.ward = (c.get("ward") or "").strip() or None
        row.councillor_id = self._councillor(c.get("councillorName"), c.get("councillorLink"),
                                             row.ward)
        row.phase = c.get("phase") or None
        row.phase_label = self._phase_label(page, row.phase)
        row.types = sorted(t for t in (c.get("type") or []) if t)
        labels = page.props.get("projectTypes") or {}
        row.type_labels = ", ".join(labels.get(t, t) for t in row.types) or None
        row.initiative_id = self.initiative_ids.get(initiative) if initiative else None
        row.site_area_text = (c.get("siteArea") or "").strip() or None
        row.site_area_acres = parse_site_area_acres(row.site_area_text)
        row.current_use = (c.get("currentUse") or "").strip() or None
        row.proposed_use = (c.get("proposedUse") or "").strip() or None
        row.surrounding_use = (c.get("surroundingUse") or "").strip() or None
        row.delivery = (c.get("delivery") or "").strip() or None
        row.cms_created_at = parse_iso(r.get("created_at"))
        row.cms_published_at = parse_iso(r.get("first_published_at") or r.get("published_at"))
        row.cms_updated_at = parse_iso(r.get("published_at") or r.get("updated_at"))
        row.source_version_id = page.version.id
        row.last_seen_run = self.run_id
        self.s.flush()
        self.project_ids[uuid] = row.id
        self.stats[f"{kind}s"] += 1
        return row

    def _project(self, page: Page) -> None:
        r, c = page.record, page.content
        description = _text(c.get("body"))
        summary = _text(c.get("summary")) or (description[:500] if description else None)
        self._upsert_project(r["uuid"], page, c, name=r.get("name") or r["slug"],
                             slug=r["slug"], kind="project", summary=summary,
                             description=description)
        # sub-sites described inline on the parent page become child projects
        for block in page.own_blocks():
            label = (block.get("label") or "").strip() or "Block"
            body = _text(block.get("body"))
            merged = {**block, "initiative": c.get("initiative"),
                      "initiatives": c.get("initiatives")}
            self._upsert_project(block["_uid"], page, merged,
                                 name=f"{r.get('name')} – {label}",
                                 slug=f"{r['slug']}#{_slugify(label)}", kind="block",
                                 summary=body[:500] if body else None, description=body)

    def _project_relations(self, page: Page) -> None:
        r, c = page.record, page.content
        me = self.project_ids[r["uuid"]]
        children = [b["_uid"] for b in page.own_blocks()]
        for link in page.own_blocks("projectBlockLink"):
            children += _refs(link.get("project"))
        for child_uuid in children:
            child = self.project_ids.get(child_uuid)
            if child and child != me:
                self.s.get(Project, child).parent_project_id = me
                self._link("project", me, "project", child, "parent_of")
        for parent_uuid in _refs(c.get("parentProject")):
            parent = self.project_ids.get(parent_uuid)
            if parent and parent != me:
                self.s.get(Project, me).parent_project_id = parent
                self._link("project", parent, "project", me, "parent_of")

    def _project_aliases(self, page: Page) -> None:
        ids = [self.project_ids[page.record["uuid"]]]
        ids += [self.project_ids[b["_uid"]] for b in page.own_blocks()]
        for pid in ids:
            p = self.s.get(Project, pid)
            # rebuild only the aliases derived from the CMS; `learned` ones (step 7) persist
            self.s.execute(delete(ProjectAlias).where(ProjectAlias.project_id == pid,
                                                      ProjectAlias.kind.in_(CMS_ALIAS_KINDS)))
            aliases = {(p.name, "name"), (p.slug.split("#")[0].replace("-", " "), "slug")}
            aliases |= {(a, "curated") for a in self.config.curated_aliases.get(p.slug, [])}
            if p.address:
                aliases.add((p.address, "address"))
            if p.major_intersection:
                aliases.add((p.major_intersection, "intersection"))
            for alias, kind in aliases:
                if alias and alias.strip():
                    self.s.execute(insert(ProjectAlias).values(
                        project_id=pid, alias=alias.strip(), kind=kind
                    ).on_conflict_do_nothing())
                    self.stats["aliases"] += 1

    # ------------------------------------------------------------ files

    def _attach_files(self, page: Page, entity: str, entity_id: int) -> None:
        """Link documents listed in resource blocks and rich-text bodies; record their titles."""
        for doc_ref in _walk(page.content, "resourcesItem"):
            for d in doc_ref.get("documents") or []:
                self._file_link(entity, entity_id, d.get("filename"), d.get("title") or
                                d.get("name"), page.url)
        for href in richtext_links(page.content.get("body")):
            self._file_link(entity, entity_id, href, None, page.url)

    def _file_link(self, entity: str, entity_id: int, href: str | None, title: str | None,
                   base: str) -> None:
        url = normalize_url(href or "", base)
        if not url:
            return
        doc = self.s.scalar(select(Document).where(Document.url == url))
        if doc is None or doc.kind == "page" and doc.scope == "first_party":
            return
        if title and title.strip():
            doc.title = title.strip()
        self._link(entity, entity_id, "document", doc.id, "has_file")
        self.stats[f"{entity}_files"] += 1

    # -------------------------------------------------------------- news

    def _news(self, page: Page) -> None:
        r, c = page.record, page.content
        row = self.s.scalar(select(NewsArticle).where(NewsArticle.cms_uuid == r["uuid"]))
        if row is None:
            row = NewsArticle(cms_uuid=r["uuid"], first_seen_run=self.run_id)
            self.s.add(row)
            self.stats["news_new"] += 1
        title = (c.get("title") or "").strip() or r.get("name") or r["slug"]
        topics = [t for t in c.get("topics") or [] if isinstance(t, dict)]
        published = parse_iso(r.get("first_published_at") or r.get("published_at"))
        titled = date_from_title(title)
        if titled:
            published = datetime(titled.year, titled.month, titled.day, tzinfo=UTC)
        row.slug = r["slug"]
        row.url = page.canonical_url
        row.title = title
        row.published_at = published
        row.author = _str(c.get("author"))
        row.topics = [t.get("name") for t in topics if t.get("name")]
        row.kind = next((NEWS_KINDS[t["slug"]] for t in topics if t.get("slug") in NEWS_KINDS),
                        None)
        row.initiative_ids = [self.initiative_ids[u] for u in
                              _refs(c.get("initiative")) + _refs(c.get("initiatives"))
                              if u in self.initiative_ids]
        row.importance = c.get("importance")
        row.event_starts_at = parse_iso(c.get("eventStartsAt"))
        row.event_ends_at = parse_iso(c.get("eventEndsAt"))
        row.external_url = _str(c.get("externalUrl"))
        row.external_source = _str(c.get("externalSource"))
        row.body_text = _text(c.get("body"))
        row.source_version_id = page.version.id
        row.last_seen_run = self.run_id
        self.s.flush()
        self.stats["news"] += 1
        for uuid in set(_refs(c.get("relatedProject")) + _refs(c.get("relatedProjects"))):
            if uuid in self.project_ids:
                self._link("news", row.id, "project", self.project_ids[uuid], "mentions")
                self.stats["news_project_links"] += 1
        self._attach_files(page, "news", row.id)

    # ------------------------------------------------------------- facts

    def _facts(self, page: Page) -> None:
        as_of = (page.version.cms_updated_at or page.version.fetched_at).date()
        ids = [(page.record["uuid"], page.content, "content")]
        for i, b in enumerate(page.own_blocks()):
            ids.append((b["_uid"], b, f"content.blocks[{i}]"))
        for uuid, c, prefix in ids:
            pid = self.project_ids[uuid]
            for key, field in FACT_FIELDS.items():
                value = c.get(key)
                if isinstance(value, list):
                    value = flatten(value)
                value = (str(value).strip() if value is not None else "") or None
                if not value:
                    continue
                self.claims.fact(
                    project_id=pid, field=field, value_text=value, as_of=as_of,
                    as_of_precision="day",
                    source=Source(page.version.id, CMS, locator=f"{prefix}.{key}",
                                  excerpt=value))
                self.stats["fact_claims"] += 1

    # -------------------------------------------------------- milestones

    def _timeline(self, page: Page) -> None:
        pid = self.project_ids[page.record["uuid"]]
        steps = list(_walk(page.content.get("timeline"), "projectTimelineStep"))
        active = next((i for i, s in enumerate(steps) if s.get("active")), None)
        seen: Counter[str] = Counter()
        for i, step in enumerate(steps):
            phase = step.get("phase") or "unknown"
            seen[phase] += 1
            # a phase can repeat (931 Yonge: market offering in 2021 and again in 2025)
            key = phase if seen[phase] == 1 else f"{phase}#{seen[phase]}"
            raw = (step.get("schedule") or "").strip()
            sched = parse_schedule(raw)
            label = self._phase_label(page, phase)
            self.claims.milestone(
                project_id=pid, type="phase_period", label=key, title=label,
                date=sched.start.date if sched and sched.start else None,
                date_precision=sched.start.precision if sched and sched.start else None,
                end_date=sched.end.date if sched and sched.end else None,
                status=_step_status(i, active, sched, raw, self.today),
                source=Source(page.version.id, CMS, locator=f"content.timeline[{i}]",
                              excerpt=f"{label}: {raw or 'n/a'}"))
            self.stats["milestone_claims"] += 1

    # ------------------------------------------------------- procurement

    def _market_offerings(self, projects: list[Page]) -> None:
        for page in projects:
            if page.content.get("phase") != "marketOffering":
                continue
            self.claims.signal(
                project_id=self.project_ids[page.record["uuid"]], kind="market_offering",
                title=f"{page.record.get('name')} market offering", status="open",
                issuer="CreateTO",
                source=Source(page.version.id, CMS, locator="content.phase",
                              excerpt="Current Phase: Market Offering"))
            self.stats["procurement_claims"] += 1

    def _resource_procurement(self, page: Page) -> None:
        pid = self.project_ids[page.record["uuid"]]
        for item in _walk(page.content, "resourcesItem"):
            for d in item.get("documents") or []:
                name = (d.get("title") or d.get("name") or "").strip()
                m = _PROCUREMENT.search(name)
                if not m:
                    continue
                word = m.group("kind").lower()
                kind = _PROCUREMENT_KIND.get(word, _PROCUREMENT_KIND.get(word.rstrip("s"),
                                                                         "other"))
                self.claims.signal(
                    project_id=pid, kind=kind, title=name, issuer="CreateTO",
                    external_url=normalize_url(d.get("filename") or "", page.url),
                    source=Source(page.version.id, "rule:resource_title",
                                  locator=f"resources:{d.get('filename')}", excerpt=name))
                self.stats["procurement_claims"] += 1

    def _merx(self) -> None:
        rows = self.s.execute(
            select(Document.url, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(Document.url.like("https://www.merx.com/public/solicitations/open%"),
                   Document.removed_at_run.is_(None)))
        matcher = ProjectMatcher(self.s)
        for url, version in rows:
            html = self.store.get(version.storage_path)
            for sol in parse_open_solicitations(html, url):
                pid = matcher.match(sol.title)
                status = "closed" if sol.closing and sol.closing < self.today else "open"
                self.claims.signal(
                    project_id=pid, project_hint=None if pid else sol.title,
                    kind="solicitation", title=sol.title, status=status,
                    reference_number=sol.reference, issuer=sol.buyer,
                    opens_at=sol.published.isoformat() if sol.published else None,
                    closes_at=sol.closing.isoformat() if sol.closing else None,
                    external_url=sol.url, scope=sol.location,
                    source=Source(version.id, "rule:merx", locator=f"merx:{sol.reference}",
                                  excerpt=f"{sol.title} | {sol.buyer} | Published "
                                          f"{sol.published} | Closing {sol.closing}"))
                self.stats["merx_solicitations"] += 1
                self.stats["merx_linked" if pid else "merx_unlinked"] += 1

    def _reconcile(self) -> None:
        """Resolve fact values and retract milestones/signals nobody asserts any more."""
        for status, n in self.claims.resolve_facts().items():
            self.stats[status] += n
        for m in self.claims.retract_unsupported("milestone", Milestone):
            if m.extracted_by == "cms:diff":  # a detected phase change is history, keep it
                m.retracted_run = None
            else:
                self.stats["milestones_retracted"] += 1
        for sig in self.claims.retract_unsupported("procurement_signal", ProcurementSignal):
            # a market offering whose project left the phase, or a tender gone from the
            # MERX open listing, is closed
            if sig.status in ("open", "upcoming"):
                sig.status = "closed"
            self.stats["procurement_retracted"] += 1
        self.s.flush()

    # ---------------------------------------------------------- history

    def _version(self, page: Page) -> None:
        uuids = [page.record["uuid"]] + [b["_uid"] for b in page.own_blocks()]
        for uuid in uuids:
            p = self.s.get(Project, self.project_ids[uuid])
            snap = _snapshot(p, page if uuid == page.record["uuid"] else None)
            prev = self.s.scalar(select(ProjectVersion).where(ProjectVersion.project_id == p.id)
                                 .order_by(ProjectVersion.id.desc()).limit(1))
            if prev is not None and prev.snapshot == snap:
                continue
            diff = None
            if prev is not None:
                keys = set(prev.snapshot) | set(snap)
                diff = {k: [prev.snapshot.get(k), snap.get(k)] for k in sorted(keys)
                        if prev.snapshot.get(k) != snap.get(k)}
                self.stats["projects_changed"] += 1
                if "phase" in diff:
                    old, new = diff["phase"]
                    self.claims.milestone(
                        project_id=p.id, type="phase_change", label=f"{old}->{new}",
                        title=f"{old} → {new}", date=self.today, date_precision="day",
                        status="actual",
                        source=Source(page.version.id, "cms:diff", locator="content.phase",
                                      excerpt=f"Phase changed from {old} to {new} "
                                              f"(detected by crawl run {self.run_id})"))
            self.s.add(ProjectVersion(project_id=p.id, run_id=self.run_id,
                                      version_id=page.version.id, snapshot=snap, diff=diff))
            self.stats["project_versions"] += 1

    def _cutoff(self) -> None:
        """5-year rule (PLAN.md §3): only `completed` projects with no recent activity."""
        limit = self.today - timedelta(days=round(365.25 * self.config.cutoff.years))
        for p in self.s.scalars(select(Project)):
            dates = [d for m in self.s.scalars(select(Milestone).where(
                Milestone.project_id == p.id)) for d in (m.end_date or m.date,) if d]
            news = self.s.scalars(
                select(NewsArticle.published_at).join(
                    Link, (Link.from_type == "news") & (Link.from_id == NewsArticle.id))
                .where(Link.to_type == "project", Link.to_id == p.id))
            dates += [n.date() for n in news if n]
            dates = [d for d in dates if d <= self.today]
            p.last_activity_date = max(dates) if dates else None
            if p.phase != "completed":
                p.cutoff_status = None
            elif p.last_activity_date is None:
                p.cutoff_status = "cutoff_unknown"
            else:
                p.cutoff_status = ("excluded_completed_old" if p.last_activity_date < limit
                                   else "in_window")
            if p.cutoff_status:
                self.stats[f"cutoff_{p.cutoff_status}"] += 1

    # ------------------------------------------------------------ links

    def _link(self, from_type: str, from_id: int, to_type: str, to_id: int, rel: str,
              method: str = CMS, confidence: float = 1.0) -> None:
        self.s.execute(insert(Link).values(
            from_type=from_type, from_id=from_id, to_type=to_type, to_id=to_id, rel=rel,
            method=method, confidence=confidence, run_id=self.run_id,
        ).on_conflict_do_update(
            index_elements=["from_type", "from_id", "to_type", "to_id", "rel"],
            set_={"run_id": self.run_id},
        ))


class ProjectMatcher:
    """Deterministic text → project matching by street address overlap or exact name."""

    def __init__(self, session: Session):
        self.addresses: list[tuple[Address, int]] = []
        self.names: list[tuple[str, int]] = []
        for pid, alias, kind in session.execute(
                select(ProjectAlias.project_id, ProjectAlias.alias, ProjectAlias.kind)):
            for addr in find_addresses(alias):
                self.addresses.append((addr, pid))
            if kind == "name" and len(alias) >= 6:
                self.names.append((alias.lower(), pid))

    def address_matches(self, text: str) -> set[int]:
        """Every project whose address (range) overlaps an address mentioned in the text."""
        return {pid for a in find_addresses(text) for b, pid in self.addresses
                if addresses_overlap(a, b)}

    def match(self, text: str) -> int | None:
        hits = self.address_matches(text)
        if not hits:
            low = text.lower()
            hits = {pid for name, pid in self.names if name in low}
        # prefer a single unambiguous project; with blocks, parent and child may both match
        return min(hits) if hits else None


def _step_status(i: int, active: int | None, sched, raw: str, today: date) -> str:
    if raw.lower().startswith("complete"):
        return "actual"
    if active is not None:
        return "actual" if i < active else "current" if i == active else "planned"
    if sched and sched.start:
        end = period_end(sched.end or sched.start)
        if sched.ongoing or sched.start.date <= today <= end:
            return "current"
        return "actual" if end < today else "planned"
    return "planned"


def _snapshot(p: Project, page: Page | None) -> dict[str, Any]:
    snap = {
        "name": p.name, "phase": p.phase, "types": p.types, "address": p.address,
        "ward": p.ward, "councillor_id": p.councillor_id, "latitude": p.latitude,
        "longitude": p.longitude, "site_area": p.site_area_text, "current_use": p.current_use,
        "proposed_use": p.proposed_use, "surrounding_use": p.surrounding_use,
        "major_intersection": p.major_intersection, "summary": p.summary,
        "parent_project_id": p.parent_project_id, "initiative_id": p.initiative_id,
    }
    if page is not None:
        snap["timeline"] = [
            [s.get("phase"), (s.get("schedule") or "").strip(), bool(s.get("active"))]
            for s in _walk(page.content.get("timeline"), "projectTimelineStep")]
    return snap


def _str(value: Any) -> str | None:
    """Plain string from a CMS value that may be a string, story reference or multilink."""
    if isinstance(value, dict):
        value = (value.get("name") or value.get("url") or value.get("cached_url")
                 or (value.get("content") or {}).get("name"))
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


DERIVED_TABLES = ["links", "evidence", "facts", "milestones", "procurement_signals",
                  "project_versions", "project_aliases", "news_articles", "projects",
                  "councillors", "initiatives"]


def truncate_derived(session: Session) -> None:
    """Empty every table the materializer writes. Raw crawl data is untouched."""
    from sqlalchemy import text

    session.execute(text(f"truncate {', '.join(DERIVED_TABLES)} restart identity cascade"))
    session.execute(text("update documents set title = null"))
