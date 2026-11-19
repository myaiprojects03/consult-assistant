"""Storage: conversations, messages, leads, call requests, knowledge base and the audit trail.
(Bookings and background jobs have their own stores: app/booking/store.py, app/jobs/store.py.)

PostgresRepo is used whenever DATABASE_URL is reachable. MemoryRepo keeps the same behaviour in one
process for unit tests and for trying the chat without Docker.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import count
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from app.llm.base import Message, ToolCall

LEAD_FIELDS = ("name", "email", "phone", "company", "job_title", "need", "company_size",
               "current_systems", "budget", "timeline")


@dataclass
class Conversation:
    id: str
    channel: str
    token_hash: str
    status: str = "open"
    turn_count: int = 0
    page_url: str = ""
    escalation_reason: str | None = None
    visitor_timezone: str = ""      # from the visitor's browser, e.g. "Europe/London"
    escalation_summary: str = ""    # the assistant's note when it handed over
    summary: str = ""               # written for consultants (Slack, CRM)
    client_id: str | None = None    # set once the visitor has proved an existing client's email
    verified_email: str = ""
    staff_user_id: str | None = None  # internal (team) conversations


@dataclass
class StoredMessage:
    id: int
    role: str
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    tool_name: str = ""
    client_message_id: str | None = None
    reply_to: int | None = None

    def to_message(self) -> Message:
        return Message(role=self.role, text=self.text, tool_calls=self.tool_calls,  # type: ignore[arg-type]
                       tool_call_id=self.tool_call_id, tool_name=self.tool_name)


@dataclass
class Lead:
    conversation_id: str
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    job_title: str = ""
    need: str = ""
    company_size: str = ""
    current_systems: str = ""
    budget: str = ""
    timeline: str = ""
    status: str = "new"
    missing: list[str] = field(default_factory=list)
    crm_contact_id: str = ""
    crm_deal_id: str = ""
    crm_note_id: str = ""

    def known(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in LEAD_FIELDS if getattr(self, f)}


@dataclass
class KBHit:
    slug: str
    kind: str
    title: str
    content: str
    score: float
    client_id: str | None = None


@dataclass
class KBChunk:
    content: str
    embedding: list[float] | None


class Repo(ABC):
    # conversations and messages
    @abstractmethod
    async def create_conversation(self, channel: str, token_hash: str, page_url: str,
                                  visitor_timezone: str = "", staff_user_id: str | None = None) -> Conversation: ...
    @abstractmethod
    async def get_conversation(self, conversation_id: str) -> Conversation | None: ...
    @abstractmethod
    async def bind_client(self, conversation_id: str, client_id: str, email: str) -> None:
        """The visitor proved they own a client contact's email: this conversation may see that client."""
    @abstractmethod
    async def client_conversations(self, client_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """Recent verified website conversations of one client (for staff allowed to see that client)."""
    @abstractmethod
    async def staff_threads(self, staff_user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """A staff member's own internal conversations, newest first, with a title (first question)."""
    @abstractmethod
    async def set_summary(self, conversation_id: str, summary: str) -> None: ...
    @abstractmethod
    async def add_user_message(self, conversation_id: str, text: str,
                               client_message_id: str) -> tuple[int, bool]:
        """Store a visitor message once. Returns (message id, created); created is False for a retry."""
    @abstractmethod
    async def find_reply(self, conversation_id: str, user_message_id: int) -> StoredMessage | None:
        """The finished reply to a visitor message: the last assistant message, with the text of every
        assistant message of that turn joined (what the visitor saw). None while unanswered."""
    @abstractmethod
    async def save_turn(self, conversation_id: str, user_message_id: int, turn: list[Message]) -> int:
        """Store the assistant's messages for one visitor message; returns the final reply's id."""
    @abstractmethod
    async def recent_messages(self, conversation_id: str, limit: int) -> list[StoredMessage]: ...

    # leads, call requests, escalations
    @abstractmethod
    async def get_lead(self, conversation_id: str) -> Lead | None: ...
    @abstractmethod
    async def save_lead(self, lead: Lead) -> Lead: ...
    @abstractmethod
    async def save_crm_ids(self, conversation_id: str, contact_id: str, deal_id: str, note_id: str) -> None: ...
    @abstractmethod
    async def add_call_request(self, conversation_id: str, preferred_times: str, timezone: str,
                               notes: str) -> str: ...
    @abstractmethod
    async def escalate(self, conversation_id: str, reason: str, summary: str) -> None: ...
    @abstractmethod
    async def log_event(self, conversation_id: str | None, kind: str, data: dict[str, Any]) -> None: ...

    # knowledge base
    @abstractmethod
    async def kb_hashes(self) -> dict[str, str]: ...
    @abstractmethod
    async def kb_upsert(self, *, slug: str, kind: str, audience: str, title: str, body: str,
                        content_hash: str, chunks: list[KBChunk], client_id: str | None = None) -> None: ...
    @abstractmethod
    async def kb_deactivate_except(self, slugs: set[str]) -> int: ...
    @abstractmethod
    async def kb_search(self, *, query: str, embedding: list[float] | None, audience: str,
                        k: int, clients: Sequence[str] | None = ()) -> list[KBHit]:
        """clients: entries about these clients are included as well as general ones; () = general only;
        None = every client (admins). Public search never sees client entries (the schema forbids them)."""

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------- Postgres
def _vec(v: list[float] | None) -> str | None:
    return None if v is None else "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def or_tsquery(text: str) -> str:
    """Words joined with OR for to_tsquery: questions rarely contain every word of the answer."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1]
    return " | ".join(dict.fromkeys(words))[:2000]


def rrf(*ranked: list[str], k: int = 60) -> dict[str, float]:
    """Reciprocal rank fusion of several ranked lists of ids."""
    scores: dict[str, float] = {}
    for lst in ranked:
        for rank, key in enumerate(lst):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


def join_texts(texts: Any) -> str:
    return "\n\n".join(t.strip() for t in texts if t and t.strip())


def _calls(raw: Any) -> list[ToolCall]:
    data = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return [ToolCall(c["id"], c["name"], c.get("args", {})) for c in data]


def _row_message(r: asyncpg.Record) -> StoredMessage:
    return StoredMessage(id=r["id"], role=r["role"], text=r["text"], tool_calls=_calls(r["tool_calls"]),
                         tool_call_id=r["tool_call_id"], tool_name=r["tool_name"],
                         client_message_id=r["client_message_id"], reply_to=r["reply_to"])


class PostgresRepo(Repo):
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def aclose(self) -> None:
        await self.pool.close()

    async def create_conversation(self, channel: str, token_hash: str, page_url: str,
                                  visitor_timezone: str = "", staff_user_id: str | None = None) -> Conversation:
        row = await self.pool.fetchrow(
            "INSERT INTO conversations (channel, token_hash, page_url, visitor_timezone, staff_user_id) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id", channel, token_hash, page_url[:500], visitor_timezone,
            UUID(staff_user_id) if staff_user_id else None)
        return Conversation(str(row["id"]), channel, token_hash, page_url=page_url, visitor_timezone=visitor_timezone,
                            staff_user_id=staff_user_id)

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        try:
            cid = UUID(conversation_id)
        except ValueError:
            return None
        r = await self.pool.fetchrow("SELECT * FROM conversations WHERE id = $1", cid)
        if r is None:
            return None
        return Conversation(str(r["id"]), r["channel"], r["token_hash"], status=r["status"],
                            turn_count=r["turn_count"], page_url=r["page_url"],
                            escalation_reason=r["escalation_reason"], visitor_timezone=r["visitor_timezone"],
                            summary=r["summary"], escalation_summary=r["escalation_summary"] or "",
                            client_id=str(r["client_id"]) if r["client_id"] else None,
                            verified_email=r["verified_email"],
                            staff_user_id=str(r["staff_user_id"]) if r["staff_user_id"] else None)

    async def bind_client(self, conversation_id: str, client_id: str, email: str) -> None:
        await self.pool.execute("UPDATE conversations SET client_id = $2, verified_email = $3, verified_at = now() "
                                "WHERE id = $1", UUID(conversation_id), UUID(client_id), email)

    async def client_conversations(self, client_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT started_at, verified_email, summary, escalation_reason FROM conversations "
            "WHERE client_id = $1 ORDER BY started_at DESC LIMIT $2", UUID(client_id), limit)
        return [{"started": r["started_at"].strftime("%d %b %Y"), "contact": r["verified_email"],
                 "summary": r["summary"], "handover": r["escalation_reason"] or ""} for r in rows]

    async def staff_threads(self, staff_user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT c.id, c.last_message_at, (SELECT text FROM messages m WHERE m.conversation_id = c.id "
            "AND m.role = 'user' ORDER BY m.id LIMIT 1) AS first FROM conversations c "
            "WHERE c.staff_user_id = $1 ORDER BY c.last_message_at DESC LIMIT $2", UUID(staff_user_id), limit)
        return [{"id": str(r["id"]), "title": (r["first"] or "New conversation")[:80],
                 "updated_at": r["last_message_at"].isoformat()} for r in rows]

    async def set_summary(self, conversation_id: str, summary: str) -> None:
        await self.pool.execute("UPDATE conversations SET summary = $2, summary_at = now() WHERE id = $1",
                                UUID(conversation_id), summary)

    async def add_user_message(self, conversation_id: str, text: str,
                               client_message_id: str) -> tuple[int, bool]:
        cid = UUID(conversation_id)
        row = await self.pool.fetchrow(
            "INSERT INTO messages (conversation_id, role, text, client_message_id) VALUES ($1, 'user', $2, $3) "
            "ON CONFLICT (conversation_id, client_message_id) WHERE client_message_id IS NOT NULL DO NOTHING "
            "RETURNING id", cid, text, client_message_id)
        if row:
            return row["id"], True
        existing = await self.pool.fetchval(
            "SELECT id FROM messages WHERE conversation_id = $1 AND client_message_id = $2", cid, client_message_id)
        return existing, False

    async def find_reply(self, conversation_id: str, user_message_id: int) -> StoredMessage | None:
        rows = await self.pool.fetch(
            "SELECT * FROM messages WHERE conversation_id = $1 AND reply_to = $2 AND role = 'assistant' "
            "ORDER BY id", UUID(conversation_id), user_message_id)
        msgs = [_row_message(r) for r in rows]
        if not msgs or msgs[-1].tool_calls:
            return None
        return replace(msgs[-1], text=join_texts(m.text for m in msgs))

    async def save_turn(self, conversation_id: str, user_message_id: int, turn: list[Message]) -> int:
        cid = UUID(conversation_id)
        last_id = 0
        async with self.pool.acquire() as conn, conn.transaction():
            for m in turn:
                calls = json.dumps([{"id": c.id, "name": c.name, "args": c.args} for c in m.tool_calls])
                last_id = await conn.fetchval(
                    "INSERT INTO messages (conversation_id, role, text, tool_calls, tool_call_id, tool_name, "
                    "reply_to) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7) RETURNING id",
                    cid, m.role, m.text, calls, m.tool_call_id, m.tool_name, user_message_id)
            await conn.execute("UPDATE conversations SET turn_count = turn_count + 1, last_message_at = now() "
                               "WHERE id = $1", cid)
        return last_id

    async def recent_messages(self, conversation_id: str, limit: int) -> list[StoredMessage]:
        rows = await self.pool.fetch(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = $1 ORDER BY id DESC LIMIT $2) t "
            "ORDER BY id", UUID(conversation_id), limit)
        return [_row_message(r) for r in rows]

    async def get_lead(self, conversation_id: str) -> Lead | None:
        r = await self.pool.fetchrow("SELECT * FROM leads WHERE conversation_id = $1", UUID(conversation_id))
        if r is None:
            return None
        return Lead(conversation_id, **{f: r[f] for f in LEAD_FIELDS}, status=r["status"], missing=list(r["missing"]),
                    crm_contact_id=r["crm_contact_id"], crm_deal_id=r["crm_deal_id"], crm_note_id=r["crm_note_id"])

    async def save_crm_ids(self, conversation_id: str, contact_id: str, deal_id: str, note_id: str) -> None:
        await self.pool.execute(
            "UPDATE leads SET crm_contact_id = $2, crm_deal_id = $3, crm_note_id = $4, crm_synced_at = now() "
            "WHERE conversation_id = $1", UUID(conversation_id), contact_id, deal_id, note_id)

    async def save_lead(self, lead: Lead) -> Lead:
        cols = ", ".join(LEAD_FIELDS)
        params = ", ".join(f"${i + 2}" for i in range(len(LEAD_FIELDS)))
        updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in LEAD_FIELDS)
        n = len(LEAD_FIELDS) + 2
        # Column names come from the LEAD_FIELDS constant, never from input; values are parameters.
        await self.pool.execute(
            f"INSERT INTO leads (conversation_id, {cols}, status, missing) VALUES ($1, {params}, ${n}, ${n + 1}) "  # noqa: S608
            f"ON CONFLICT (conversation_id) DO UPDATE SET {updates}, status = EXCLUDED.status, "
            f"missing = EXCLUDED.missing, updated_at = now()",
            UUID(lead.conversation_id), *[getattr(lead, f) for f in LEAD_FIELDS], lead.status, lead.missing)
        return lead

    async def add_call_request(self, conversation_id: str, preferred_times: str, timezone: str,
                               notes: str) -> str:
        rid = await self.pool.fetchval(
            "INSERT INTO call_requests (conversation_id, preferred_times, timezone, notes) "
            "VALUES ($1, $2, $3, $4) RETURNING id", UUID(conversation_id), preferred_times, timezone, notes)
        return str(rid)

    async def escalate(self, conversation_id: str, reason: str, summary: str) -> None:
        await self.pool.execute(
            "UPDATE conversations SET status = 'escalated', escalation_reason = $2, escalation_summary = $3, "
            "escalated_at = COALESCE(escalated_at, now()) WHERE id = $1", UUID(conversation_id), reason, summary)

    async def log_event(self, conversation_id: str | None, kind: str, data: dict[str, Any]) -> None:
        await self.pool.execute("INSERT INTO events (conversation_id, kind, data) VALUES ($1, $2, $3::jsonb)",
                                UUID(conversation_id) if conversation_id else None, kind, json.dumps(data))

    async def kb_hashes(self) -> dict[str, str]:
        rows = await self.pool.fetch("SELECT slug, content_hash FROM kb_entries WHERE active")
        return {r["slug"]: r["content_hash"] for r in rows}

    async def kb_upsert(self, *, slug: str, kind: str, audience: str, title: str, body: str,
                        content_hash: str, chunks: list[KBChunk], client_id: str | None = None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            entry_id = await conn.fetchval(
                "INSERT INTO kb_entries (slug, kind, audience, title, body, content_hash, client_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (slug) DO UPDATE SET kind = EXCLUDED.kind, "
                "audience = EXCLUDED.audience, title = EXCLUDED.title, body = EXCLUDED.body, active = true, "
                "content_hash = EXCLUDED.content_hash, client_id = EXCLUDED.client_id, updated_at = now() "
                "RETURNING id", slug, kind, audience, title, body, content_hash,
                UUID(client_id) if client_id else None)
            await conn.execute("DELETE FROM kb_chunks WHERE entry_id = $1", entry_id)
            for i, ch in enumerate(chunks):
                await conn.execute("INSERT INTO kb_chunks (entry_id, ord, heading, content, embedding) "
                                   "VALUES ($1, $2, $3, $4, $5::vector)", entry_id, i, title, ch.content,
                                   _vec(ch.embedding))

    async def kb_deactivate_except(self, slugs: set[str]) -> int:
        res = await self.pool.execute("UPDATE kb_entries SET active = false WHERE active AND NOT (slug = ANY($1))",
                                      list(slugs))
        return int(res.split()[-1])

    async def kb_search(self, *, query: str, embedding: list[float] | None, audience: str,
                        k: int, clients: Sequence[str] | None = ()) -> list[KBHit]:
        # $3: client ids this caller may see; NULL means all (admins). Enforced here, not by the model.
        allowed = None if clients is None else [UUID(c) for c in clients]
        base = ("FROM kb_chunks c JOIN kb_entries e ON e.id = c.entry_id "
                "WHERE e.active AND e.audience = $1 AND (e.client_id IS NULL OR $3::uuid[] IS NULL "
                "OR e.client_id = ANY($3::uuid[]))")
        rows: dict[int, asyncpg.Record] = {}
        keyword_ids: list[str] = []
        tsq = or_tsquery(query)
        if tsq:
            kw = await self.pool.fetch(
                f"SELECT c.id, e.slug, e.kind, e.title, e.client_id, c.content, "
                f"ts_rank(c.tsv, to_tsquery('english', $2)) AS r {base} "
                f"AND c.tsv @@ to_tsquery('english', $2) ORDER BY r DESC LIMIT 20", audience, tsq, allowed)
            rows.update({r["id"]: r for r in kw})
            keyword_ids = [str(r["id"]) for r in kw]
        vector_ids: list[str] = []
        if embedding is not None:
            vec = await self.pool.fetch(
                f"SELECT c.id, e.slug, e.kind, e.title, e.client_id, c.content {base} AND c.embedding IS NOT NULL "
                f"ORDER BY c.embedding <=> $2::vector LIMIT 20", audience, _vec(embedding), allowed)
            rows.update({r["id"]: r for r in vec})
            vector_ids = [str(r["id"]) for r in vec]
        fused = rrf(keyword_ids, vector_ids)
        best = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
        out = []
        for i, s in best:
            r = rows[int(i)]
            out.append(KBHit(r["slug"], r["kind"], r["title"], r["content"], s,
                             str(r["client_id"]) if r["client_id"] else None))
        return out


# ---------------------------------------------------------------- in-memory
class MemoryRepo(Repo):
    """Same behaviour in one process (tests, or trying the chat without Docker). Keyword search only."""

    def __init__(self) -> None:
        self.conversations: dict[str, Conversation] = {}
        self.messages: dict[str, list[StoredMessage]] = {}
        self.leads: dict[str, Lead] = {}
        self.call_requests: list[dict[str, str]] = []
        self.events: list[tuple[str | None, str, dict[str, Any]]] = []
        self.escalations: dict[str, tuple[str, str]] = {}
        self.kb: dict[str, dict[str, Any]] = {}
        self._ids = count(1)

    async def create_conversation(self, channel: str, token_hash: str, page_url: str,
                                  visitor_timezone: str = "", staff_user_id: str | None = None) -> Conversation:
        c = Conversation(str(uuid4()), channel, token_hash, page_url=page_url, visitor_timezone=visitor_timezone,
                         staff_user_id=staff_user_id)
        self.conversations[c.id] = c
        self.messages[c.id] = []
        return c

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        c = self.conversations.get(conversation_id)
        return replace(c) if c else None

    async def set_summary(self, conversation_id: str, summary: str) -> None:
        self.conversations[conversation_id].summary = summary

    async def bind_client(self, conversation_id: str, client_id: str, email: str) -> None:
        c = self.conversations[conversation_id]
        c.client_id, c.verified_email = client_id, email

    async def client_conversations(self, client_id: str, limit: int = 5) -> list[dict[str, Any]]:
        mine = [c for c in self.conversations.values() if c.client_id == client_id][-limit:]
        return [{"started": "", "contact": c.verified_email, "summary": c.summary,
                 "handover": c.escalation_reason or ""} for c in reversed(mine)]

    async def staff_threads(self, staff_user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        mine = [c for c in self.conversations.values() if c.staff_user_id == staff_user_id]
        out = []
        for c in reversed(mine):
            first = next((m.text for m in self.messages[c.id] if m.role == "user"), "New conversation")
            out.append({"id": c.id, "title": first[:80], "updated_at": ""})
        return out[:limit]

    async def add_user_message(self, conversation_id: str, text: str,
                               client_message_id: str) -> tuple[int, bool]:
        for m in self.messages[conversation_id]:
            if m.client_message_id == client_message_id:
                return m.id, False
        m = StoredMessage(next(self._ids), "user", text, client_message_id=client_message_id)
        self.messages[conversation_id].append(m)
        return m.id, True

    async def find_reply(self, conversation_id: str, user_message_id: int) -> StoredMessage | None:
        msgs = [m for m in self.messages[conversation_id] if m.reply_to == user_message_id and m.role == "assistant"]
        if not msgs or msgs[-1].tool_calls:
            return None
        return replace(msgs[-1], text=join_texts(m.text for m in msgs))

    async def save_turn(self, conversation_id: str, user_message_id: int, turn: list[Message]) -> int:
        last = 0
        for m in turn:
            sm = StoredMessage(next(self._ids), m.role, m.text, list(m.tool_calls), m.tool_call_id, m.tool_name,
                               reply_to=user_message_id)
            self.messages[conversation_id].append(sm)
            last = sm.id
        self.conversations[conversation_id].turn_count += 1
        return last

    async def recent_messages(self, conversation_id: str, limit: int) -> list[StoredMessage]:
        return list(self.messages[conversation_id][-limit:])

    async def get_lead(self, conversation_id: str) -> Lead | None:
        lead = self.leads.get(conversation_id)
        return replace(lead, missing=list(lead.missing)) if lead else None

    async def save_lead(self, lead: Lead) -> Lead:
        old = self.leads.get(lead.conversation_id)
        crm = {k: getattr(old, k) for k in ("crm_contact_id", "crm_deal_id", "crm_note_id")} if old else {}
        self.leads[lead.conversation_id] = replace(lead, missing=list(lead.missing), **crm)
        return lead

    async def save_crm_ids(self, conversation_id: str, contact_id: str, deal_id: str, note_id: str) -> None:
        lead = self.leads[conversation_id]
        lead.crm_contact_id, lead.crm_deal_id, lead.crm_note_id = contact_id, deal_id, note_id

    async def add_call_request(self, conversation_id: str, preferred_times: str, timezone: str,
                               notes: str) -> str:
        rid = str(uuid4())
        self.call_requests.append({"id": rid, "conversation_id": conversation_id,
                                   "preferred_times": preferred_times, "timezone": timezone, "notes": notes})
        return rid

    async def escalate(self, conversation_id: str, reason: str, summary: str) -> None:
        self.escalations[conversation_id] = (reason, summary)
        c = self.conversations[conversation_id]
        c.status, c.escalation_reason, c.escalation_summary = "escalated", reason, summary

    async def log_event(self, conversation_id: str | None, kind: str, data: dict[str, Any]) -> None:
        self.events.append((conversation_id, kind, {**data, "at": datetime.now(UTC).isoformat()}))

    async def kb_hashes(self) -> dict[str, str]:
        return {s: e["content_hash"] for s, e in self.kb.items() if e["active"]}

    async def kb_upsert(self, *, slug: str, kind: str, audience: str, title: str, body: str,
                        content_hash: str, chunks: list[KBChunk], client_id: str | None = None) -> None:
        if audience == "public" and client_id:
            raise ValueError("public entries cannot belong to a client")
        self.kb[slug] = {"kind": kind, "audience": audience, "title": title, "body": body,
                         "content_hash": content_hash, "chunks": chunks, "active": True, "client_id": client_id}

    async def kb_deactivate_except(self, slugs: set[str]) -> int:
        n = 0
        for s, e in self.kb.items():
            if e["active"] and s not in slugs:
                e["active"] = False
                n += 1
        return n

    async def kb_search(self, *, query: str, embedding: list[float] | None, audience: str,
                        k: int, clients: Sequence[str] | None = ()) -> list[KBHit]:
        words = set(or_tsquery(query).split(" | ")) - {""}
        hits = []
        for slug, e in self.kb.items():
            if not e["active"] or e["audience"] != audience:
                continue
            owner = e.get("client_id")
            if owner and clients is not None and owner not in clients:
                continue
            for ch in e["chunks"]:
                tokens = set(re.findall(r"[a-z0-9]+", ch.content.lower()))
                score = len(words & tokens)
                if score:
                    hits.append(KBHit(slug, e["kind"], e["title"], ch.content, float(score), e.get("client_id")))
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]
