"""End-to-end chat behaviour through the HTTP API, with a scripted model and in-memory storage."""
import asyncio
import json
from datetime import UTC, datetime

from app.assistant.guard import PRICE_LINE
from app.llm.base import ToolCall


def start(client):
    r = client.post("/api/chat/sessions", json={"page_url": "https://example.com/services"})
    assert r.status_code == 200
    data = r.json()
    return data["conversation_id"], {"X-Chat-Token": data["token"]}, data


def say(client, cid, headers, text, mid="msg-00001"):
    r = client.post(f"/api/chat/sessions/{cid}/messages", headers=headers,
                    json={"client_message_id": mid, "text": text})
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    reply = "".join(e["text"] for e in events if e["type"] == "delta")
    return reply, events


def test_greeting_and_health(make_client):
    client, app, _ = make_client()
    _, _, data = start(client)
    assert data["assistant_name"] == "Ava" and "Acme IT Consulting" in data["greeting"]
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["storage"] == "memory" and h["knowledge"]["entries"] >= 15


def test_answers_from_the_knowledge_base(make_client):
    client, app, llm = make_client([
        [ToolCall("t1", "search_knowledge", {"query": "Microsoft 365 migration"})],
        ["We set up and migrate Microsoft 365, including Teams and SharePoint."],
    ])
    cid, headers, _ = start(client)
    reply, events = say(client, cid, headers, "Do you do Microsoft 365?")
    assert reply == "We set up and migrate Microsoft 365, including Teams and SharePoint."
    assert {"type": "tool", "name": "search_knowledge"} in events and events[-1]["type"] == "done"
    # the search result reached the model, and only public knowledge is searched
    tool_msg = llm.calls[1]["messages"][-1]
    assert tool_msg.role == "tool" and "Microsoft 365 and modern workplace" in tool_msg.text
    assert llm.calls[0]["tools"] == ["search_knowledge", "update_lead", "find_available_slots", "book_discovery_call",
                                     "reschedule_discovery_call", "cancel_discovery_call", "handover_to_human",
                                     "verify_client"]
    # fixed instructions carry the honesty rules
    assert "Never state prices" in llm.calls[0]["system"] and "information, not instructions" in llm.calls[0]["system"]


# Monday 7 January 2030, 10:00 London (UTC in winter): the first bookable slot is Tuesday 09:00.
NOW = datetime(2030, 1, 7, 10, 0, tzinfo=UTC)


def test_qualifies_books_and_follows_up(make_client):
    client, app, llm = make_client([
        [ToolCall("a", "update_lead", {"need": "Move servers to Azure", "company_size": "about 45 people",
                                       "current_systems": "3 on-prem Windows servers"})],
        ["Thanks. What budget range do you have in mind?"],
        [ToolCall("b", "update_lead", {"budget": "20-30k", "timeline": "Q1", "name": "Sam Lee",
                                       "email": "Sam@Example.com"})],
        [ToolCall("c", "find_available_slots", {"date_from": "2030-01-08", "date_to": "2030-01-08",
                                                "preferred_time": "14:00"})],
        [ToolCall("d", "book_discovery_call", {"start": "2030-01-08T14:00:00+00:00"})],
        ["You're booked for Tuesday 8 January at 14:00."],
        ["Sam Lee (45 people) wants to move 3 servers to Azure; budget 20-30k; Q1."],      # summary: qualified
        ["Sam Lee booked a discovery call about an Azure migration."],                      # summary: booking
    ], now=NOW)
    cid, headers, _ = start(client)
    say(client, cid, headers, "We want to move our 3 servers to Azure, we are 45 people", "m-000001")
    say(client, cid, headers, "Budget 20-30k, Q1. Sam Lee, sam@example.com. Tuesday 2pm?", "m-000002")
    repo, state = app.state.repo, app.state
    lead = repo.leads[cid]
    assert lead.email == "sam@example.com" and lead.status == "qualified" and lead.missing == []
    # what is known about the visitor goes to the model as per-turn context, with the date
    context = llm.calls[2]["context"]
    assert "company_size: about 45 people" in context and "Monday 07 January 2030" in context
    slots = llm.calls[4]["messages"][-1].text
    assert "Tuesday 8 January, 14:00-14:30 (London time) | start=2030-01-08T14:00:00+00:00" in slots
    booked = llm.calls[5]["messages"][-1].text
    assert booked.startswith("Booked: Tuesday 8 January, 14:00-14:30 (London time) with Sam Patel, reference DC-")

    async def run_jobs():
        return await state.worker.run_due()
    asyncio.run(run_jobs())
    f = state.fakes
    booking = asyncio.run(state.booking.store.active_for_conversation(cid))
    assert booking and booking.email == "sam@example.com"
    assert [e.event_id for e in f.calendar.created] == [booking.calendar_event_id]
    assert f.calendar.created[0].attendees == [("sam@example.com", "Sam Lee")]
    assert [m["subject"] for m in f.mailer.sent] == [
        "Your discovery call with Acme IT Consulting: Tuesday 8 January, 14:00-14:30 (London time)"]
    meet = f"https://meet.google.com/{booking.calendar_event_id[:10]}"
    assert f"Video link: {meet}" in f.mailer.sent[0]["text"]      # the event's Meet link, made first
    assert asyncio.run(state.booking.store.get(booking.id)).meeting_url == meet
    channels = [c for c, _ in f.notifier.posts]
    assert channels.count("#leads") == 2          # qualified lead, then the booking
    assert "Discovery call booked" in f.notifier.posts[-1][1] and booking.reference in f.notifier.posts[-1][1]
    assert f.crm.records[-1].booked and f.crm.records[-1].email == "sam@example.com"
    assert "Azure migration" in repo.conversations[cid].summary
    # reminders wait for their time; nothing failed
    pending = [j for j in state.jobs.store.jobs.values() if j.status == "pending"]
    assert sorted(j.payload["template"] for j in pending) == ["reminder_1h", "reminder_24h"]
    assert not [j for j in state.jobs.store.jobs.values() if j.status == "failed"]


def test_booking_needs_contact_details_first(make_client):
    client, app, llm = make_client([
        [ToolCall("c", "book_discovery_call", {"start": "2030-01-08T14:00:00+00:00"})],
        ["Could I have your name and email first?"],
    ], now=NOW)
    cid, headers, _ = start(client)
    say(client, cid, headers, "Book me for Tuesday at 2pm")
    assert not app.state.booking.store.bookings
    assert "name and email" in llm.calls[1]["messages"][-1].text


def test_visitor_time_zone_comes_from_the_browser(make_client):
    client, app, llm = make_client([
        [ToolCall("c", "find_available_slots", {"date_from": "2030-01-08", "date_to": "2030-01-08"})],
        ["Here are some times."],
    ], now=NOW)
    r = client.post("/api/chat/sessions", json={"page_url": "", "timezone": "Asia/Karachi"})
    cid, headers = r.json()["conversation_id"], {"X-Chat-Token": r.json()["token"]}
    say(client, cid, headers, "When can we talk on Tuesday?")
    assert "Asia/Karachi" in llm.calls[0]["context"]
    # 09:00 London is 14:00 in Karachi
    offered = llm.calls[1]["messages"][-1].text
    assert "Tuesday 8 January, 14:00-14:30 (Karachi time) | start=2030-01-08T14:00:00+05:00" in offered
    bad = client.post("/api/chat/sessions", json={"timezone": "Mars/Olympus"}).json()
    assert asyncio.run(app.state.repo.get_conversation(bad["conversation_id"])).visitor_timezone == ""


def test_small_company_is_marked_not_a_fit(make_client):
    client, app, _ = make_client([[ToolCall("a", "update_lead", {"company_size": "3 people"})], ["Thanks!"]])
    cid, headers, _ = start(client)
    say(client, cid, headers, "We are 3 people")
    assert app.state.repo.leads[cid].status == "not_a_fit"


def test_stated_size_is_saved_in_code_even_if_the_model_does_not(make_client):
    client, app, llm = make_client([["We'd be glad to help."]])
    cid, headers, _ = start(client)
    say(client, cid, headers, "We're a team of 3, can you help us?")
    lead = app.state.repo.leads[cid]
    assert lead.company_size == "team of 3" and lead.status == "not_a_fit"
    assert "below the size" in llm.calls[0]["context"]   # the model is told in the same turn


def test_size_from_text():
    from app.assistant.tools import size_from_text
    assert size_from_text("We're a team of 3, can you help?") == "team of 3"
    assert size_from_text("We're about 40 people") == "40 people"
    assert size_from_text("we have 10-50 employees") == "10-50 employees"
    assert size_from_text("a 25-person firm") == "25-person"
    assert size_from_text("We have 3 on-prem Windows servers") is None
    assert size_from_text("Tuesday at 2pm") is None


def test_invalid_email_is_refused(make_client):
    client, app, llm = make_client([[ToolCall("a", "update_lead", {"email": "sam at example"})],
                                    ["Could you check it?"]])
    cid, headers, _ = start(client)
    say(client, cid, headers, "my email is sam at example")
    assert cid not in app.state.repo.leads and "not a valid email" in llm.calls[1]["messages"][-1].text


def test_security_incident_is_handed_over_by_rule(make_client):
    client, app, llm = make_client([["A consultant will contact you right away. What's the best email?"]])
    cid, headers, _ = start(client)
    reply, events = say(client, cid, headers, "We think we've been hacked, our files are encrypted")
    repo = app.state.repo
    assert events[0] == {"type": "handover", "reason": "security_incident"}
    assert repo.escalations[cid][0] == "security_incident"
    assert "already been passed to a consultant" in llm.calls[0]["context"]
    assert any(k == "handover" and d["by"] == "rule" for _, k, d in repo.events)


def test_handover_reaches_the_right_channel_at_once(make_client):
    client, app, llm = make_client([
        ["A consultant will contact you right away. What's the best email?"],
        ["Visitor reports ransomware; files encrypted; needs urgent help."],          # summary job
    ])
    cid, headers, _ = start(client)
    say(client, cid, headers, "We've been hit by ransomware <!channel>")
    jobs = list(app.state.jobs.store.jobs.values())
    assert jobs[0].kind == "slack.handover"           # first in line, not waiting for the summary
    asyncio.run(app.state.worker.run_due())
    posts = app.state.fakes.notifier.posts
    assert len(posts) == 1
    channel, text = posts[0]
    assert channel == "#handovers" and "Handover: security incident" in text and "Sam Patel" in text
    assert "ransomware" in text and "<!channel>" not in text and "&lt;!channel&gt;" in text
    assert "Assistant's note" not in text and text.count("ransomware") == 1     # said once, not twice
    assert "ransomware; files encrypted" in app.state.repo.conversations[cid].summary     # summary still made
    assert not app.state.fakes.crm.records            # no email yet: nothing for the CRM


def test_crm_gets_the_lead_once_there_is_an_email(make_client):
    client, app, _ = make_client([
        [ToolCall("a", "update_lead", {"name": "Jo", "email": "jo@example.com", "need": "Help desk"})],
        ["Thanks Jo."],
        [ToolCall("b", "update_lead", {"company": "Acme"})],
        ["Noted."],
    ])
    cid, headers, _ = start(client)
    say(client, cid, headers, "I'm Jo, jo@example.com, we need a help desk", "m-000001")
    say(client, cid, headers, "Company is Acme", "m-000002")
    pending = [j for j in app.state.jobs.store.jobs.values() if j.kind == "crm.sync" and j.status == "pending"]
    assert len(pending) == 1                           # two updates, one sync
    asyncio.run(app.state.worker.run_due())
    record = app.state.fakes.crm.records[-1]
    assert record.email == "jo@example.com" and record.company == "Acme" and not record.booked
    assert app.state.repo.leads[cid].crm_contact_id == "c-1"


def test_move_and_cancel_from_a_later_chat_need_reference_and_email(make_client):
    client, app, llm = make_client([
        [ToolCall("a", "update_lead", {"name": "Jo Smith", "email": "jo@example.com"})],
        [ToolCall("b", "book_discovery_call", {"start": "2030-01-08T09:00:00+00:00"})],
        ["Booked."],
    ], now=NOW)
    cid, headers, _ = start(client)
    say(client, cid, headers, "Jo Smith, jo@example.com, book Tuesday 9am")
    ref = asyncio.run(app.state.booking.store.active_for_conversation(cid)).reference

    later = make_client([
        [ToolCall("c", "reschedule_discovery_call", {"new_start": "2030-01-08T15:00:00+00:00", "reference": ref,
                                                     "email": "someone@else.com"})],
        [ToolCall("d", "reschedule_discovery_call", {"new_start": "2030-01-08T15:00:00+00:00",
                                                     "reference": ref.lower(), "email": "Jo@Example.com"})],
        [ToolCall("e", "cancel_discovery_call", {"reference": ref, "email": "jo@example.com"})],
        ["Done."],
    ], now=NOW)
    c2, app2, llm2 = later
    # share the first app's bookings with the second client (same database in real life)
    app2.state.booking.store = app.state.booking.store
    cid2, headers2, _ = start(c2)
    say(c2, cid2, headers2, "Please move my call DC-... to 3pm, then cancel it")
    results = [m.text for m in llm2.calls[3]["messages"] if m.role == "tool"]
    assert results[0].startswith("Error: no booked call found")
    assert results[1].startswith(f"Moved: the call ({ref}) is now Tuesday 8 January, 15:00-15:30")
    assert results[2].startswith("Cancelled the call on Tuesday 8 January, 15:00-15:30")
    assert asyncio.run(app.state.booking.store.by_reference(ref)).status == "cancelled"


def test_calendar_outage_falls_back_to_a_consultant(make_client):
    client, app, llm = make_client([
        [ToolCall("a", "find_available_slots", {})],
        ["A consultant will email you to arrange a time."],
    ], now=NOW)
    app.state.fakes.calendar.down = True
    cid, headers, _ = start(client)
    say(client, cid, headers, "When can we talk?")
    assert "cannot be checked right now" in llm.calls[1]["messages"][-1].text
    assert app.state.repo.call_requests[0]["conversation_id"] == cid
    assert any(j.payload.get("reason") == "cannot_help" for j in app.state.jobs.store.jobs.values())


def test_model_can_hand_over(make_client):
    client, app, _ = make_client([
        [ToolCall("h", "handover_to_human", {"reason": "enterprise_account", "summary": "Bank wants custom SLA."})],
        ["I've passed this to a consultant."],
    ])
    cid, headers, _ = start(client)
    say(client, cid, headers, "We're a bank with 5000 staff and need a custom SLA")
    assert app.state.repo.escalations[cid] == ("enterprise_account", "Bank wants custom SLA.")


def test_a_price_from_the_model_never_reaches_the_visitor(make_client):
    client, app, _ = make_client([["Migrations are quick. A small one is about $4,000. ", "Want a call?"]])
    cid, headers, _ = start(client)
    reply, _ = say(client, cid, headers, "How much is a migration?")
    assert "$4,000" not in reply and PRICE_LINE in reply
    guard_events = [d for _, k, d in app.state.repo.events if k == "guard"]
    assert guard_events == [{"reason": "price", "found": "$4,000", "at": guard_events[0]["at"]}]


def test_a_retried_message_is_answered_once(make_client):
    client, app, llm = make_client([["First and only answer."]])
    cid, headers, _ = start(client)
    first, _ = say(client, cid, headers, "Hello", "retry-0001")
    again, events = say(client, cid, headers, "Hello", "retry-0001")
    assert first == again == "First and only answer."
    assert events[-1]["replayed"] is True and len(llm.calls) == 1
    users = [m for m in app.state.repo.messages[cid] if m.role == "user"]
    assert len(users) == 1


def test_a_failed_reply_can_be_retried(make_client):
    client, app, llm = make_client(fail="Groq rate limit reached; try again shortly.")
    cid, headers, _ = start(client)
    _, events = say(client, cid, headers, "Hello", "fail-0001")
    assert events == [{"type": "error", "message": "Groq rate limit reached; try again shortly."}]
    llm.fail = None
    llm.rounds = [["Now it works."]]
    reply, _ = say(client, cid, headers, "Hello", "fail-0001")
    assert reply == "Now it works."


def test_transcript_restores_the_visible_conversation(make_client):
    client, app, _ = make_client([[ToolCall("t", "search_knowledge", {"query": "hours"})], ["Monday to Friday."]])
    cid, headers, _ = start(client)
    say(client, cid, headers, "When are you open?")
    t = client.get(f"/api/chat/sessions/{cid}/messages", headers=headers).json()
    assert t["messages"] == [{"role": "user", "text": "When are you open?"},
                             {"role": "assistant", "text": "Monday to Friday."}]


def test_access_needs_the_conversation_token(make_client):
    client, _, _ = make_client([["hi"]])
    cid, _, _ = start(client)
    for headers in ({}, {"X-Chat-Token": "wrong"}):
        assert client.get(f"/api/chat/sessions/{cid}/messages", headers=headers).status_code == 404
        r = client.post(f"/api/chat/sessions/{cid}/messages", headers=headers,
                        json={"client_message_id": "abcdefgh", "text": "hi"})
        assert r.status_code == 404
    assert client.get("/api/chat/sessions/not-a-uuid/messages", headers={"X-Chat-Token": "x"}).status_code == 404


def test_input_limits(make_client):
    client, _, _ = make_client([["ok"]])
    cid, headers, _ = start(client)
    url = f"/api/chat/sessions/{cid}/messages"
    def post(mid, text):
        return client.post(url, headers=headers, json={"client_message_id": mid, "text": text}).status_code
    assert post("abcdefgh", "x" * 2001) == 413
    assert post("bad id!", "hi") == 422
    assert post("abcdefgh", "   ") == 422


def test_message_rate_limit(make_client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "chat_messages_per_minute", 2)
    client, _, _ = make_client([["a"], ["b"], ["c"]])
    cid, headers, _ = start(client)
    say(client, cid, headers, "one", "rate-0001")
    say(client, cid, headers, "two", "rate-0002")
    r = client.post(f"/api/chat/sessions/{cid}/messages", headers=headers,
                    json={"client_message_id": "rate-0003", "text": "three"})
    assert r.status_code == 429


def test_history_sent_to_the_model_includes_earlier_turns(make_client):
    client, _, llm = make_client([["Hi Sam."], ["You said cloud."]])
    cid, headers, _ = start(client)
    say(client, cid, headers, "I'm Sam", "hist-0001")
    say(client, cid, headers, "What did I say?", "hist-0002")
    msgs = llm.calls[1]["messages"]
    assert [(m.role, m.text) for m in msgs] == [("user", "I'm Sam"), ("assistant", "Hi Sam."),
                                                ("user", "What did I say?")]


def test_a_model_stuck_calling_tools_is_made_to_answer(make_client):
    loop = [[ToolCall(f"t{i}", "search_knowledge", {"query": "x"})] for i in range(4)]
    client, _, llm = make_client(loop + [["Here is what I found."]])
    cid, headers, _ = start(client)
    reply, _ = say(client, cid, headers, "Tell me everything")
    assert reply == "Here is what I found."
    assert len(llm.calls) == 5 and llm.calls[-1]["tools"] == []


def test_an_empty_model_reply_gets_a_fallback(make_client):
    client, _, _ = make_client([[""]])
    cid, headers, _ = start(client)
    reply, _ = say(client, cid, headers, "hmm")
    assert reply.startswith("Sorry, I didn't catch that.")


def test_rate_limits_use_the_hosts_client_ip_header_when_set(monkeypatch):
    from starlette.requests import Request

    from app.api.chat import client_key
    from app.config import get_settings

    def req(headers):
        return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                        "client": ("10.0.0.1", 1234)})
    spoofed = {"x-forwarded-for": "1.2.3.4, 203.0.113.9", "cf-connecting-ip": "203.0.113.9"}
    assert client_key(req(spoofed)) == client_key(req({"x-forwarded-for": "1.2.3.4"}))   # no header set: first
    monkeypatch.setenv("CLIENT_IP_HEADER", "CF-Connecting-IP")
    get_settings.cache_clear()
    try:
        assert client_key(req(spoofed)) == client_key(req({"x-forwarded-for": "203.0.113.9"}))
        assert client_key(req({})) == client_key(req({"x-forwarded-for": "10.0.0.1"}))       # header missing
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_sentences_sent_without_a_space_are_kept_apart(make_client):
    client, app, _ = make_client([["Please send the 6-digit code.", "I'm waiting for it.", " Thanks. e.g.x"]])
    cid, headers, _ = start(client)
    reply, _ = say(client, cid, headers, "hello")
    assert reply == "Please send the 6-digit code. I'm waiting for it. Thanks. e.g.x"
