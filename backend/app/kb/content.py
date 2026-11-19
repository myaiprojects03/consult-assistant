"""The firm's editable content: company profile, services, FAQs, policies and the assistant's rules,
plus internal documents for the team assistant (content/internal/*.md, never searched by the public chat).

Phase 1 reads it from YAML files in content/ and syncs it into the database at startup (only entries
whose text changed are re-embedded). The admin dashboard (Phase 5) edits the same records, so staff
can change answers and rules without touching code.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.db.repo import KBChunk, Repo
from app.kb.embeddings import Embedder

log = logging.getLogger(__name__)
MAX_CHUNK_CHARS = 900


@dataclass
class Rules:
    """Business rules that steer the assistant. Loaded from content/rules.yaml."""
    firm_name: str
    assistant_name: str
    tone: str
    greeting: str
    qualification_fields: list[str]
    escalate_topics: dict[str, list[str]]           # reason -> trigger phrases (checked in code)
    escalate_guidance: list[str]                    # situations the model must hand over itself
    allowed_price_figures: list[str] = field(default_factory=list)
    office_hours: str = ""
    booking_note: str = ""
    fit: dict[str, Any] = field(default_factory=dict)
    booking: dict[str, Any] = field(default_factory=dict)          # see app/booking/slots.py BookingConfig
    voice: dict[str, Any] = field(default_factory=dict)            # see app/voice/rules.py VoiceRules
    privacy: dict[str, Any] = field(default_factory=dict)          # see app/privacy/store.py Retention


@dataclass
class Entry:
    slug: str
    kind: str
    title: str
    body: str
    audience: str = "public"
    client: str = ""            # internal documents about one client: that client's slug
    source: str = ""            # file it came from, shown as the source reference

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.kind}|{self.audience}|{self.client}|{self.title}|{self.body}".encode()
                              ).hexdigest()


INTERNAL_KINDS = {"proposal", "case_study", "sop", "template", "technical", "notes", "policy"}


def load_internal_entries(content_dir: Path) -> list[Entry]:
    """Markdown files with a small header:

        ---
        title: Northwind Azure migration proposal
        kind: proposal            (proposal, case_study, sop, template, technical, notes, policy)
        client: northwind         (only for documents about one client)
        ---
    """
    entries: list[Entry] = []
    folder = content_dir / "internal"
    for path in sorted(folder.glob("**/*.md")) if folder.exists() else []:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(f"{path.name}: missing the --- header (title, kind, client)")
        _, header, body = text.split("---", 2)
        meta = yaml.safe_load(header) or {}
        kind = str(meta.get("kind", "notes"))
        if kind not in INTERNAL_KINDS:
            raise ValueError(f"{path.name}: kind must be one of {', '.join(sorted(INTERNAL_KINDS))}")
        rel = path.relative_to(content_dir).as_posix()
        entries.append(Entry(slug="internal/" + rel.removeprefix("internal/").removesuffix(".md"), kind=kind,
                             title=str(meta.get("title") or path.stem).strip(), body=body.strip(),
                             audience="internal", client=str(meta.get("client") or "").strip(), source=rel))
    return entries


def load_rules(content_dir: Path) -> Rules:
    data = yaml.safe_load((content_dir / "rules.yaml").read_text(encoding="utf-8"))
    return Rules(**data)


def load_entries(content_dir: Path) -> list[Entry]:
    entries: list[Entry] = []
    for name, kind in (("company.yaml", "company"), ("services.yaml", "service"), ("faqs.yaml", "faq"),
                       ("policies.yaml", "policy")):
        path = content_dir / name
        if not path.exists():
            continue
        for item in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            entries.append(Entry(slug=item["slug"], kind=kind, title=item["title"].strip(),
                                 body=item["body"].strip(), audience=item.get("audience", "public")))
    slugs = [e.slug for e in entries]
    dupes = {s for s in slugs if slugs.count(s) > 1}
    if dupes:
        raise ValueError(f"Duplicate content slugs: {', '.join(sorted(dupes))}")
    return entries


def chunk(entry: Entry) -> list[str]:
    """Short entries stay whole; long ones split on paragraphs (and never across a "## " section of an
    internal document). Each chunk repeats the title and its section so a chunk found on its own still
    says what it is about."""
    out: list[str] = []
    sections: list[tuple[str, str]] = [("", entry.body)]
    if entry.audience == "internal":
        sections = []
        heading = ""
        lines: list[str] = []
        for line in entry.body.splitlines():
            if line.startswith("## "):
                if any(x.strip() for x in lines):
                    sections.append((heading, "\n".join(lines)))
                heading, lines = line[3:].strip(), []
            else:
                lines.append(line)
        if any(x.strip() for x in lines):
            sections.append((heading, "\n".join(lines)))
    for heading, text in sections:
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        current = ""
        label = f"{entry.title} - {heading}" if heading else entry.title
        for p in paras:
            if current and len(current) + len(p) > MAX_CHUNK_CHARS:
                out.append(f"{label}\n{current}")
                current = p
            else:
                current = f"{current}\n\n{p}" if current else p
        if current:
            out.append(f"{label}\n{current}")
    return out or [entry.title]


async def sync_content(repo: Repo, embedder: Embedder, content_dir: Path,
                       client_ids: dict[str, str] | None = None) -> dict[str, int]:
    """client_ids: client slug -> id. An internal document for a client that does not exist is skipped,
    never stored unscoped (it would otherwise be visible to every consultant)."""
    entries = load_entries(content_dir)
    for e in load_internal_entries(content_dir):
        if e.client and e.client not in (client_ids or {}):
            log.warning("skipped %s: client '%s' does not exist (add it, then restart)", e.source, e.client)
            continue
        entries.append(e)
    current = await repo.kb_hashes()
    changed = [e for e in entries if current.get(e.slug) != e.content_hash]
    for e in changed:
        texts = chunk(e)
        vectors = await embedder.embed(texts)
        # Stored without vectors, the hash gets a marker so the entry is embedded once the model loads.
        await repo.kb_upsert(slug=e.slug, kind=e.kind, audience=e.audience, title=e.title, body=e.body,
                             content_hash=e.content_hash if vectors else e.content_hash + ":keywords-only",
                             chunks=[KBChunk(t, vectors[i] if vectors else None) for i, t in enumerate(texts)],
                             client_id=(client_ids or {}).get(e.client) if e.client else None)
    removed = await repo.kb_deactivate_except({e.slug for e in entries})
    internal = sum(1 for e in entries if e.audience == "internal")
    stats = {"entries": len(entries) - internal, "internal": internal, "updated": len(changed), "removed": removed}
    log.info("knowledge base synced: %s", stats)
    return stats
