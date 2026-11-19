"""Existing clients: who they are, who may speak for them (contacts), their projects and their tickets.

Everything a client can see is read through a client id that comes from a verified conversation or a
staff member's access list, never from what the model or the visitor typed.
"""
from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import asyncpg

from app.booking.store import REF_ALPHABET

PRIORITIES = ("low", "normal", "high", "urgent")
TICKET_STATUSES = ("open", "in_progress", "waiting", "resolved", "closed")
OPEN_STATUSES = ("open", "in_progress", "waiting")


def new_ticket_reference() -> str:
    return "TK-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(6))


@dataclass
class Client:
    id: str
    slug: str
    name: str
    tier: str = "sme"
    account_manager: str = ""
    active: bool = True


@dataclass
class Project:
    id: str
    client_id: str
    name: str
    status: str = "in_progress"
    phase: str = ""
    next_milestone: str = ""
    milestone_date: date | None = None
    client_update: str = ""
    owner: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Ticket:
    id: str
    reference: str
    client_id: str
    contact_email: str
    summary: str
    details: str = ""
    priority: str = "normal"
    category: str = "other"
    status: str = "open"
    conversation_id: str | None = None
    external_key: str = ""
    external_url: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ClientStore(ABC):
    @abstractmethod
    async def upsert_client(self, slug: str, name: str, tier: str = "sme", account_manager: str = "") -> Client: ...
    @abstractmethod
    async def add_contact(self, client_id: str, email: str, name: str = "", role: str = "") -> None: ...
    @abstractmethod
    async def client_for_email(self, email: str) -> Client | None: ...
    @abstractmethod
    async def get_client(self, client_id: str) -> Client | None: ...
    @abstractmethod
    async def list_clients(self, ids: Sequence[str] | None = None) -> list[Client]:
        """Active clients; ids=None means all of them."""
    @abstractmethod
    async def upsert_project(self, p: Project) -> Project:
        """Insert, or update the client's project of the same name."""
    @abstractmethod
    async def projects(self, client_id: str) -> list[Project]: ...
    @abstractmethod
    async def create_ticket(self, t: Ticket) -> Ticket: ...
    @abstractmethod
    async def get_ticket(self, ticket_id: str) -> Ticket | None: ...
    @abstractmethod
    async def ticket_by_reference(self, client_id: str, reference: str) -> Ticket | None:
        """Only within that client: another client's reference is simply not found."""
    @abstractmethod
    async def tickets(self, client_id: str, open_only: bool = False, limit: int = 10) -> list[Ticket]: ...
    @abstractmethod
    async def update_ticket(self, ticket_id: str, *, status: str | None = None, external_key: str | None = None,
                            external_url: str | None = None) -> None: ...


def normalize_ticket_reference(text: str) -> str:
    ref = "".join("-" if ch in "‐‑‒–—−" else ch for ch in text).upper()
    ref = ref.replace(" ", "")
    return "TK-" + ref.removeprefix("TK-").removeprefix("TK")


# ---------------------------------------------------------------- Postgres
def _client(r: asyncpg.Record) -> Client:
    return Client(str(r["id"]), r["slug"], r["name"], r["tier"], r["account_manager"], r["active"])


def _project(r: asyncpg.Record) -> Project:
    return Project(str(r["id"]), str(r["client_id"]), r["name"], r["status"], r["phase"], r["next_milestone"],
                   r["milestone_date"], r["client_update"], r["owner"], r["updated_at"])


def _ticket(r: asyncpg.Record) -> Ticket:
    return Ticket(str(r["id"]), r["reference"], str(r["client_id"]), r["contact_email"], r["summary"], r["details"],
                  r["priority"], r["category"], r["status"],
                  str(r["conversation_id"]) if r["conversation_id"] else None, r["external_key"], r["external_url"],
                  r["created_at"], r["updated_at"])


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


class PgClientStore(ClientStore):
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert_client(self, slug: str, name: str, tier: str = "sme", account_manager: str = "") -> Client:
        r = await self.pool.fetchrow(
            "INSERT INTO clients (slug, name, tier, account_manager) VALUES ($1, $2, $3, $4) ON CONFLICT (slug) "
            "DO UPDATE SET name = EXCLUDED.name, tier = EXCLUDED.tier, account_manager = EXCLUDED.account_manager "
            "RETURNING *", slug, name, tier, account_manager)
        return _client(r)

    async def add_contact(self, client_id: str, email: str, name: str = "", role: str = "") -> None:
        await self.pool.execute(
            "INSERT INTO client_contacts (client_id, email, name, role) VALUES ($1, $2, $3, $4) ON CONFLICT (email) "
            "DO UPDATE SET client_id = EXCLUDED.client_id, name = EXCLUDED.name, role = EXCLUDED.role",
            UUID(client_id), email.strip().lower(), name, role)

    async def client_for_email(self, email: str) -> Client | None:
        r = await self.pool.fetchrow("SELECT c.* FROM client_contacts k JOIN clients c ON c.id = k.client_id "
                                     "WHERE k.email = $1 AND c.active", email.strip().lower())
        return _client(r) if r else None

    async def get_client(self, client_id: str) -> Client | None:
        cid = _uuid(client_id)
        r = await self.pool.fetchrow("SELECT * FROM clients WHERE id = $1", cid) if cid else None
        return _client(r) if r else None

    async def list_clients(self, ids: Sequence[str] | None = None) -> list[Client]:
        if ids is None:
            rows = await self.pool.fetch("SELECT * FROM clients WHERE active ORDER BY name")
        else:
            rows = await self.pool.fetch("SELECT * FROM clients WHERE active AND id = ANY($1::uuid[]) ORDER BY name",
                                         [UUID(i) for i in ids])
        return [_client(r) for r in rows]

    async def upsert_project(self, p: Project) -> Project:
        existing = await self.pool.fetchval("SELECT id FROM projects WHERE client_id = $1 AND name = $2",
                                            UUID(p.client_id), p.name)
        if existing:
            r = await self.pool.fetchrow(
                "UPDATE projects SET status = $2, phase = $3, next_milestone = $4, milestone_date = $5, "
                "client_update = $6, owner = $7, updated_at = now() WHERE id = $1 RETURNING *",
                existing, p.status, p.phase, p.next_milestone, p.milestone_date, p.client_update, p.owner)
        else:
            r = await self.pool.fetchrow(
                "INSERT INTO projects (client_id, name, status, phase, next_milestone, milestone_date, client_update, "
                "owner) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *", UUID(p.client_id), p.name, p.status,
                p.phase, p.next_milestone, p.milestone_date, p.client_update, p.owner)
        return _project(r)

    async def projects(self, client_id: str) -> list[Project]:
        rows = await self.pool.fetch("SELECT * FROM projects WHERE client_id = $1 ORDER BY status = 'completed', "
                                     "updated_at DESC", UUID(client_id))
        return [_project(r) for r in rows]

    async def create_ticket(self, t: Ticket) -> Ticket:
        for _ in range(3):
            try:
                r = await self.pool.fetchrow(
                    "INSERT INTO tickets (reference, client_id, conversation_id, contact_email, summary, details, "
                    "priority, category) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
                    t.reference or new_ticket_reference(), UUID(t.client_id),
                    UUID(t.conversation_id) if t.conversation_id else None, t.contact_email, t.summary, t.details,
                    t.priority, t.category)
                return _ticket(r)
            except asyncpg.exceptions.UniqueViolationError:
                t = replace(t, reference=new_ticket_reference())
        raise RuntimeError("could not create a unique ticket reference")

    async def get_ticket(self, ticket_id: str) -> Ticket | None:
        tid = _uuid(ticket_id)
        r = await self.pool.fetchrow("SELECT * FROM tickets WHERE id = $1", tid) if tid else None
        return _ticket(r) if r else None

    async def ticket_by_reference(self, client_id: str, reference: str) -> Ticket | None:
        r = await self.pool.fetchrow("SELECT * FROM tickets WHERE client_id = $1 AND reference = $2",
                                     UUID(client_id), normalize_ticket_reference(reference))
        return _ticket(r) if r else None

    async def tickets(self, client_id: str, open_only: bool = False, limit: int = 10) -> list[Ticket]:
        statuses = list(OPEN_STATUSES if open_only else TICKET_STATUSES)
        rows = await self.pool.fetch("SELECT * FROM tickets WHERE client_id = $1 AND status = ANY($2) "
                                     "ORDER BY created_at DESC LIMIT $3", UUID(client_id), statuses, limit)
        return [_ticket(r) for r in rows]

    async def update_ticket(self, ticket_id: str, *, status: str | None = None, external_key: str | None = None,
                            external_url: str | None = None) -> None:
        await self.pool.execute(
            "UPDATE tickets SET status = COALESCE($2, status), external_key = COALESCE($3, external_key), "
            "external_url = COALESCE($4, external_url), updated_at = now() WHERE id = $1",
            UUID(ticket_id), status, external_key, external_url)


# ---------------------------------------------------------------- in-memory
class MemoryClientStore(ClientStore):
    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}
        self.contacts: dict[str, str] = {}             # email -> client id
        self.projects_: dict[str, Project] = {}
        self.tickets_: dict[str, Ticket] = {}

    async def upsert_client(self, slug: str, name: str, tier: str = "sme", account_manager: str = "") -> Client:
        existing = next((c for c in self.clients.values() if c.slug == slug), None)
        c = Client(existing.id if existing else str(uuid4()), slug, name, tier, account_manager)
        self.clients[c.id] = c
        return replace(c)

    async def add_contact(self, client_id: str, email: str, name: str = "", role: str = "") -> None:
        self.contacts[email.strip().lower()] = client_id

    async def client_for_email(self, email: str) -> Client | None:
        cid = self.contacts.get(email.strip().lower())
        c = self.clients.get(cid) if cid else None
        return replace(c) if c and c.active else None

    async def get_client(self, client_id: str) -> Client | None:
        c = self.clients.get(client_id)
        return replace(c) if c else None

    async def list_clients(self, ids: Sequence[str] | None = None) -> list[Client]:
        return sorted((replace(c) for c in self.clients.values() if c.active and (ids is None or c.id in ids)),
                      key=lambda c: c.name)

    async def upsert_project(self, p: Project) -> Project:
        existing = next((x for x in self.projects_.values() if x.client_id == p.client_id and x.name == p.name), None)
        stored = replace(p, id=existing.id if existing else str(uuid4()))
        self.projects_[stored.id] = stored
        return replace(stored)

    async def projects(self, client_id: str) -> list[Project]:
        return [replace(p) for p in self.projects_.values() if p.client_id == client_id]

    async def create_ticket(self, t: Ticket) -> Ticket:
        stored = replace(t, id=str(uuid4()), reference=t.reference or new_ticket_reference())
        self.tickets_[stored.id] = stored
        return replace(stored)

    async def get_ticket(self, ticket_id: str) -> Ticket | None:
        t = self.tickets_.get(ticket_id)
        return replace(t) if t else None

    async def ticket_by_reference(self, client_id: str, reference: str) -> Ticket | None:
        ref = normalize_ticket_reference(reference)
        return next((replace(t) for t in self.tickets_.values() if t.client_id == client_id and t.reference == ref),
                    None)

    async def tickets(self, client_id: str, open_only: bool = False, limit: int = 10) -> list[Ticket]:
        mine = [t for t in self.tickets_.values() if t.client_id == client_id
                and (not open_only or t.status in OPEN_STATUSES)]
        return [replace(t) for t in sorted(mine, key=lambda t: t.created_at, reverse=True)[:limit]]

    async def update_ticket(self, ticket_id: str, *, status: str | None = None, external_key: str | None = None,
                            external_url: str | None = None) -> None:
        t = self.tickets_[ticket_id]
        t.status = status or t.status
        t.external_key = external_key if external_key is not None else t.external_key
        t.external_url = external_url if external_url is not None else t.external_url
        t.updated_at = datetime.now(UTC)
