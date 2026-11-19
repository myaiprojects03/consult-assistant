"""Clients, tickets, staff accounts and internal-document scoping on real Postgres.
Skipped without TEST_DATABASE_URL."""
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.clients.store import PgClientStore, Ticket
from app.config import CONTENT_DIR
from app.kb.content import sync_content
from app.staff.store import PgStaffStore, hash_password, new_session_token
from scripts.seed_demo import seed
from tests.fakes import HashEmbedder
from tests.test_content_and_storage import pg, with_repo


@pg
def test_clients_contacts_projects_and_ticket_scoping():
    async def check(repo):
        clients = PgClientStore(repo.pool)
        made = await seed(clients, "Jo@Northwind.example")
        made2 = await seed(clients)                               # running the seed again changes nothing
        assert made["northwind"].id == made2["northwind"].id
        assert len(await clients.projects(made["northwind"].id)) == 2
        assert (await clients.client_for_email("jo@northwind.EXAMPLE")).slug == "northwind"
        assert await clients.client_for_email("nobody@example.com") is None
        nw, bw = made["northwind"], made["brightwater"]
        t = await clients.create_ticket(Ticket(id="", reference="", client_id=nw.id, summary="VPN",
                                               contact_email="jo@northwind.example", priority="high",
                                               category="outage"))
        assert t.reference.startswith("TK-") and t.status == "open"
        assert (await clients.ticket_by_reference(nw.id, t.reference.lower())).id == t.id
        assert await clients.ticket_by_reference(bw.id, t.reference) is None     # other client: not found
        await clients.update_ticket(t.id, status="in_progress", external_key="SUP-1", external_url="u")
        got = await clients.get_ticket(t.id)
        assert (got.status, got.external_key) == ("in_progress", "SUP-1")
        assert [x.id for x in await clients.tickets(nw.id, open_only=True)] == [t.id]
        with pytest.raises(asyncpg.CheckViolationError):
            await clients.create_ticket(Ticket(id="", reference="", client_id=nw.id, contact_email="x@y.z",
                                               summary="bad", priority="whenever"))
        conv = await repo.create_conversation("web", "h", "")
        await repo.bind_client(conv.id, nw.id, "jo@northwind.example")
        bound = await repo.get_conversation(conv.id)
        assert bound.client_id == nw.id and bound.verified_email == "jo@northwind.example"
    with_repo(check)


@pg
def test_staff_sessions_expire_and_access_is_per_client():
    async def check(repo):
        clients, staff = PgClientStore(repo.pool), PgStaffStore(repo.pool)
        made = await seed(clients)
        sam = await staff.create_user("Sam@Acme.example", "Sam", "consultant", hash_password("x" * 12), "sam")
        boss = await staff.create_user("boss@acme.example", "Boss", "admin", hash_password("x" * 12))
        assert await staff.allowed_clients(sam) == [] and await staff.allowed_clients(boss) is None
        await staff.grant(sam.id, made["brightwater"].id)
        await staff.grant(sam.id, made["brightwater"].id)                # granting twice is fine
        assert await staff.allowed_clients(sam) == [made["brightwater"].id]
        token, digest = new_session_token()
        await staff.create_session(sam.id, digest)
        assert (await staff.session_user(digest)).email == "sam@acme.example"
        # idle for 13 hours: signed out
        await repo.pool.execute("UPDATE staff_sessions SET last_seen_at = now() - interval '13 hours'")
        assert await staff.session_user(digest) is None
        _, d2 = new_session_token()
        await staff.create_session(sam.id, d2)
        await repo.pool.execute("UPDATE staff_sessions SET expires_at = $1 WHERE token_hash = $2",
                                datetime.now(UTC) - timedelta(seconds=1), d2)
        assert await staff.session_user(d2) is None
        _, d3 = new_session_token()
        await staff.create_session(sam.id, d3)
        await staff.revoke(sam.id, made["brightwater"].id)
        assert await staff.allowed_clients(sam) == []
        await staff.set_password(sam.id, hash_password("y" * 12))     # signs out everywhere
        assert await staff.session_user(d3) is None
        with pytest.raises(asyncpg.UniqueViolationError):
            await staff.create_user("sam@acme.example", "Sam again", "consultant", hash_password("x" * 12))
    with_repo(check)


@pg
def test_internal_documents_are_filtered_by_client_in_the_database():
    async def check(repo):
        made = await seed(PgClientStore(repo.pool))
        ids = {slug: c.id for slug, c in made.items()}
        emb = HashEmbedder()
        stats = await sync_content(repo, emb, CONTENT_DIR, ids)
        assert stats["internal"] == 6

        async def search(q, clients=(), audience="internal"):
            vec = (await emb.embed([q]))[0]
            return await repo.kb_search(query=q, embedding=vec, audience=audience, k=10, clients=clients)
        q = "account notes contact relationship commercial"
        general = await search(q)
        assert general and all(h.client_id is None for h in general)
        bw_only = await search(q, [ids["brightwater"]])
        assert {h.client_id for h in bw_only} <= {None, ids["brightwater"]}
        assert any(h.client_id == ids["brightwater"] for h in bw_only)
        everything = await search(q, None)
        assert {ids["northwind"], ids["brightwater"]} <= {h.client_id for h in everything}
        public = await search("Northwind scanners SMTP pricing day rate", None, audience="public")
        assert all(not h.slug.startswith("internal/") for h in public)
        # a public entry can never be tied to a client
        with pytest.raises(asyncpg.CheckViolationError):
            await repo.pool.execute("UPDATE kb_entries SET client_id = $1 WHERE audience = 'public'",
                                    made["northwind"].id)
    with_repo(check)
