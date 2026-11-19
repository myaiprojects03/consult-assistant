"""Calls on real Postgres: one row per Twilio call or outbound request, a single dialler, final statuses
kept. Skipped without TEST_DATABASE_URL."""
from app.voice.store import Call, PgCallStore
from tests.test_content_and_storage import pg, with_repo


@pg
def test_calls_are_idempotent_claimed_once_and_keep_their_final_status():
    async def check(repo):
        calls = PgCallStore(repo.pool)
        conv = await repo.create_conversation("voice", "h", "")
        a, created = await calls.create(Call(id="", direction="inbound", call_sid="CA1", from_number="+4477"))
        b, again = await calls.create(Call(id="", direction="inbound", call_sid="CA1"))
        assert created and not again and a.id == b.id
        o, _ = await calls.create(Call(id="", direction="outbound", request_key="u:1", to_number="+4478"))
        o2, again = await calls.create(Call(id="", direction="outbound", request_key="u:1"))
        assert not again and o2.id == o.id and o.caller_number == "+4478"
        assert await calls.claim(o.id, ("queued",), "dialling")
        assert not await calls.claim(o.id, ("queued",), "dialling")           # only one dialler
        await calls.update(a.id, conversation_id=conv.id, status="completed", duration_s=42)
        await calls.update(a.id, status="in_progress", outcome="transferred")  # late callback: status kept
        got = await calls.get(a.id)
        assert (got.status, got.outcome, got.duration_s, got.conversation_id) == ("completed", "transferred", 42,
                                                                                  conv.id)
        assert (await calls.by_sid("CA1")).id == a.id and len(await calls.recent()) == 2
    with_repo(check)
