"""Content loading, knowledge sync, and the Postgres storage (skipped without TEST_DATABASE_URL)."""
import asyncio
import os
import shutil

import pytest

from app.config import CONTENT_DIR
from app.db.repo import KBChunk, Lead, MemoryRepo, PostgresRepo, or_tsquery, rrf
from app.kb.content import Entry, chunk, load_entries, load_rules, sync_content
from app.kb.embeddings import NoEmbedder
from app.llm.base import Message, ToolCall
from tests.fakes import HashEmbedder

TEST_DB = os.environ.get("TEST_DATABASE_URL")


# ---------------- content ----------------
def test_content_files_load_and_rules_are_complete():
    entries = load_entries(CONTENT_DIR)
    assert len({e.slug for e in entries}) == len(entries) >= 15
    assert {e.kind for e in entries} == {"company", "service", "faq", "policy"}
    rules = load_rules(CONTENT_DIR)
    assert rules.allowed_price_figures == []
    assert {"security_incident", "pricing_negotiation", "human_requested"} <= set(rules.escalate_topics)
    assert rules.qualification_fields[:2] == ["need", "company_size"]


def test_long_entries_are_split_and_keep_their_title():
    e = Entry("x", "faq", "Title", "\n\n".join(["word " * 120] * 3))
    parts = chunk(e)
    assert len(parts) == 3 and all(p.startswith("Title\n") for p in parts)


def test_sync_only_reembeds_changed_entries(tmp_path):
    shutil.copytree(CONTENT_DIR, tmp_path / "c")
    repo = MemoryRepo()

    class Counting(HashEmbedder):
        n = 0

        async def embed(self, texts):
            Counting.n += len(texts)
            return await super().embed(texts)

    stats = asyncio.run(sync_content(repo, Counting(), tmp_path / "c"))
    first = Counting.n
    assert stats["updated"] == stats["entries"] + stats["internal"] and first >= stats["updated"]
    stats = asyncio.run(sync_content(repo, Counting(), tmp_path / "c"))
    assert stats["updated"] == 0 and Counting.n == first
    faqs = (tmp_path / "c" / "faqs.yaml").read_text().replace("about 10 employees", "about 15 employees")
    (tmp_path / "c" / "faqs.yaml").write_text(faqs)
    stats = asyncio.run(sync_content(repo, Counting(), tmp_path / "c"))
    assert stats["updated"] == 1


def test_entries_saved_without_vectors_are_embedded_once_the_model_works(tmp_path):
    shutil.copytree(CONTENT_DIR, tmp_path / "c")
    repo = MemoryRepo()
    asyncio.run(sync_content(repo, NoEmbedder(), tmp_path / "c"))
    assert all(e["chunks"][0].embedding is None for e in repo.kb.values())
    stats = asyncio.run(sync_content(repo, HashEmbedder(), tmp_path / "c"))
    assert stats["updated"] == stats["entries"] + stats["internal"]
    assert all(e["chunks"][0].embedding is not None for e in repo.kb.values())


def test_removed_entries_are_deactivated(tmp_path):
    shutil.copytree(CONTENT_DIR, tmp_path / "c")
    repo = MemoryRepo()
    asyncio.run(sync_content(repo, NoEmbedder(), tmp_path / "c"))
    (tmp_path / "c" / "policies.yaml").write_text("[]")
    stats = asyncio.run(sync_content(repo, NoEmbedder(), tmp_path / "c"))
    assert stats["removed"] == 2 and not repo.kb["privacy"]["active"]


def test_search_helpers():
    assert or_tsquery("How much does a cloud migration cost?") == "how | much | does | cloud | migration | cost"
    assert or_tsquery("!!!") == ""
    scores = rrf(["a", "b"], ["b", "c"])
    assert max(scores, key=scores.get) == "b"


# ---------------- Postgres ----------------
pg = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to a Postgres with pgvector")


def with_repo(fn):
    async def go():
        import asyncpg

        from app.db.migrations import connect
        admin = await asyncpg.connect(TEST_DB)
        await admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await admin.close()
        repo = PostgresRepo(await connect(TEST_DB))
        try:
            await fn(repo)
        finally:
            await repo.aclose()
    asyncio.run(go())


@pg
def test_postgres_messages_are_stored_once_and_replies_joined():
    async def check(repo):
        conv = await repo.create_conversation("web", "hash", "https://example.com")
        uid, created = await repo.add_user_message(conv.id, "hi", "client-1")
        uid2, created2 = await repo.add_user_message(conv.id, "hi", "client-1")
        assert created and not created2 and uid == uid2
        assert await repo.find_reply(conv.id, uid) is None
        await repo.save_turn(conv.id, uid, [
            Message("assistant", "Let me check.", tool_calls=[ToolCall("t", "search_knowledge", {"query": "x"})]),
            Message("tool", "result", tool_call_id="t", tool_name="search_knowledge"),
            Message("assistant", "Here it is.")])
        reply = await repo.find_reply(conv.id, uid)
        assert reply.text == "Let me check.\n\nHere it is."
        recent = await repo.recent_messages(conv.id, 10)
        assert [m.role for m in recent] == ["user", "assistant", "tool", "assistant"]
        assert recent[1].tool_calls == [ToolCall("t", "search_knowledge", {"query": "x"})]
        assert (await repo.get_conversation(conv.id)).turn_count == 1
        assert await repo.get_conversation("not-a-uuid") is None
    with_repo(check)


@pg
def test_postgres_leads_escalations_and_events():
    async def check(repo):
        conv = await repo.create_conversation("web", "h", "")
        await repo.save_lead(Lead(conv.id, need="Azure", company_size="40", missing=["budget"]))
        await repo.save_lead(Lead(conv.id, need="Azure", company_size="40", budget="20k", status="qualified"))
        lead = await repo.get_lead(conv.id)
        assert lead.budget == "20k" and lead.status == "qualified" and lead.missing == []
        await repo.escalate(conv.id, "security_incident", "Ransomware")
        c = await repo.get_conversation(conv.id)
        assert c.status == "escalated" and c.escalation_reason == "security_incident"
        rid = await repo.add_call_request(conv.id, "Tuesday", "UTC", "")
        assert len(rid) == 36
        await repo.log_event(conv.id, "guard", {"reason": "price"})
        assert await repo.pool.fetchval("SELECT count(*) FROM events WHERE kind = 'guard'") == 1
    with_repo(check)


@pg
def test_postgres_hybrid_search_ranks_and_keeps_internal_material_private():
    async def check(repo):
        stats = await sync_content(repo, HashEmbedder(), CONTENT_DIR)
        assert stats["updated"] >= 15
        emb = HashEmbedder()
        await repo.kb_upsert(slug="internal-rates", kind="policy", audience="internal", title="Internal day rates",
                             body="Our internal day rate card", content_hash="x",
                             chunks=[KBChunk("Internal day rates cost pricing", (await emb.embed(["x"]))[0])])
        q = "How much does it cost?"
        hits = await repo.kb_search(query=q, embedding=(await emb.embed([q]))[0], audience="public", k=4)
        assert hits[0].slug == "faq-pricing"
        assert all(h.slug != "internal-rates" for h in hits)
        # keyword search alone (no embeddings) still finds it
        hits = await repo.kb_search(query="Microsoft 365 Teams", embedding=None, audience="public", k=4)
        assert hits[0].slug == "microsoft-365"
        # a second sync changes nothing
        assert (await sync_content(repo, HashEmbedder(), CONTENT_DIR))["updated"] == 0
    with_repo(check)


def test_instructions_carry_the_fit_rule_and_flag_small_companies():
    from app.assistant.prompts import system_prompt, turn_context
    rules = load_rules(CONTENT_DIR)
    assert rules.fit["note"] in system_prompt(rules)
    small = Lead("c1", company_size="3")
    small.status = "not_a_fit"
    assert "below the size" in turn_context(small, rules, None, None)
    assert "below the size" not in turn_context(Lead("c2", company_size="40"), rules, None, None)


def test_read_only_model_folder_is_copied_to_temp(tmp_path, monkeypatch):
    """A serverless bundle is read-only, and the model library writes lock files next to the model."""
    import os
    import tempfile

    from app.kb.embeddings import writable_cache
    model = tmp_path / "bundle" / "fastembed"
    (model / "m").mkdir(parents=True)
    (model / "m" / "model.onnx").write_text("weights")
    assert writable_cache(model) == model                      # writable: used in place

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, mode: False if str(p) == str(model) else real_access(p, mode))
    copy = writable_cache(model)
    assert copy == tmp_path / "tmp" / "fastembed"
    assert (copy / "m" / "model.onnx").read_text() == "weights"
    assert writable_cache(model) == copy                       # copied once
    assert writable_cache(tmp_path / "missing") == tmp_path / "missing"
