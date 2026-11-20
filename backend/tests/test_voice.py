"""Phase 4: voice calls. Browser calls and Twilio phone calls through the real API (scripted model,
fake Twilio), the call session's interruption and transfer rules, outbound calls and text messages."""
import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from app.config import get_settings
from app.integrations.base import IntegrationError
from app.jobs import handlers
from app.llm.base import Done, LLMProvider, TextDelta, ToolCall
from app.voice.rules import VoiceRules, Window
from app.voice.session import VoiceSession
from app.voice.speech import speakable
from app.voice.store import Call
from app.voice.twilio import call_token, signature, valid_signature
from tests.test_chat_api import NOW, start

ORIGIN = {"origin": "http://localhost:3000"}
LONDON = ZoneInfo("Europe/London")
PUBLIC = "https://voice.example.com"
TOKEN = "twilio-test-auth-token"


# ---------------------------------------------------------------- pure parts
def test_speakable_removes_markdown_and_spells_references():
    assert speakable("Your reference is **DC-7K3M9Q**.") == "Your reference is D C, 7 K 3 M 9 Q."
    assert speakable("- Cloud migration\n- Security") == "Cloud migration, Security"
    assert speakable("Email jo.smith@north.example please") == "Email jo dot smith at north dot example please"
    assert speakable("See [our page](https://acme.example/x) — it explains.") == "See our page, it explains."
    assert speakable("Visit https://acme.example now") == "Visit the link we'll send you now"
    assert speakable(" Booked. ") == " Booked. "
    assert speakable("Could you spell it?(Waiting for email)") == "Could you spell it?"
    assert speakable("(Waiting for email)") == ""


def test_calling_windows():
    w = Window.parse({"days": ["mon", "tue", "wed", "thu", "fri"], "start": "10:00", "end": "17:00"}, Window())
    mon_9 = datetime(2030, 1, 7, 9, 0, tzinfo=UTC)      # 09:00 London in winter
    assert not w.is_open(mon_9, LONDON) and w.next_open(mon_9, LONDON) == datetime(2030, 1, 7, 10, 0, tzinfo=LONDON)
    assert w.is_open(datetime(2030, 1, 7, 12, 0, tzinfo=UTC), LONDON)
    fri_late = datetime(2030, 1, 11, 17, 0, tzinfo=UTC)
    assert w.next_open(fri_late, LONDON) == datetime(2030, 1, 14, 10, 0, tzinfo=LONDON)   # Monday
    rules = VoiceRules.from_rules({"outbound_greeting": "Hello {name}, about {purpose}."})
    assert rules.outbound_text("", "") == "Hello there, about your enquiry."


def test_twilio_signature_matches_twilio_docs_example():
    # The example from Twilio's "Webhooks security" documentation.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    assert signature("12345", url, params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert valid_signature("12345", url, params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
    assert not valid_signature("12345", url, {**params, "Digits": "9"}, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
    assert not valid_signature("", url, params, "x")


# ---------------------------------------------------------------- browser calls
def web_call(client):
    r = client.post("/api/voice/web/sessions", json={"timezone": "Europe/London"})
    assert r.status_code == 200, r.text
    d = r.json()
    url = f"/api/voice/web/ws?conversation_id={d['conversation_id']}&call_id={d['call_id']}&token={d['token']}"
    return d, url


def reply(ws):
    """Collect one spoken reply: tokens until last=true (or an end message)."""
    tokens, end = [], None
    while True:
        msg = ws.receive_json()
        if msg["type"] == "end":
            end = json.loads(msg["handoffData"])
            break
        tokens.append(msg["token"])
        if msg["last"]:
            break
    return "".join(tokens), end


def test_browser_call_answers_and_files_the_call(make_client):
    client, app, llm = make_client([
        [ToolCall("k", "search_knowledge", {"query": "Microsoft 365"})],
        ["Yes, we migrate **Microsoft 365**. Your reference is DC-7K3M9Q."],
    ], now=NOW)
    d, url = web_call(client)
    with client.websocket_connect(url, headers=ORIGIN) as ws:
        greeting, _ = reply(ws)
        assert "Ava" in greeting
        ws.send_json({"type": "prompt", "voicePrompt": "Do you do Microsoft 365?", "last": True})
        said, end = reply(ws)
        assert said.startswith("Yes, we migrate Microsoft 365.") and "D C, 7 K 3 M 9 Q" in said and end is None
    # the voice instructions and the transfer tool were used; the browser call cannot be transferred
    assert "# On a call" in llm.calls[0]["system"] and "transfer_call" in llm.calls[0]["tools"]
    assert "browser call" in llm.calls[0]["context"]
    call = asyncio.run(app.state.calls.get(d["call_id"]))
    assert call.status == "completed" and call.duration_s is not None
    conv = asyncio.run(app.state.repo.get_conversation(d["conversation_id"]))
    assert conv.channel == "voice"
    assert [j.kind for j in app.state.jobs.store.jobs.values() if j.ref == d["conversation_id"]] == \
        ["conversation.summarize"]


def test_browser_call_needs_its_token_and_our_origin(make_client):
    client, app, _ = make_client([["hi"]])
    d, url = web_call(client)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(url, headers={"origin": "https://evil.example"}):
        pass
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(url.replace(d["token"], "x" * 43),
                                                                      headers=ORIGIN):
        pass
    with client.websocket_connect(url, headers=ORIGIN) as ws:
        reply(ws)
        # a second connection to the same call is refused
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url, headers=ORIGIN):
            pass
    # a voice conversation's token does not open it in the text chat
    r = client.get(f"/api/chat/sessions/{d['conversation_id']}/messages", headers={"X-Chat-Token": d["token"]})
    assert r.status_code == 404


def test_keypad_zero_asks_for_a_person_and_browser_calls_get_a_call_back(make_client):
    client, app, llm = make_client([
        [ToolCall("t", "transfer_call", {"reason": "human_requested", "summary": "Caller wants a person"})],
        ["Sam will call you back."],
    ], now=NOW)
    app.state.team.get("sam").phone = "+441111111111"
    d, url = web_call(client)
    with client.websocket_connect(url, headers=ORIGIN) as ws:
        reply(ws)
        ws.send_json({"type": "dtmf", "digit": "0"})
        said, end = reply(ws)
    assert end is None and said == "Sam will call you back."         # browser calls are never put through
    assert "browser call" in llm.calls[-1]["messages"][-1].text
    assert app.state.repo.escalations[d["conversation_id"]][0] == "human_requested"


# ---------------------------------------------------------------- the session's rules
class GatedLLM(LLMProvider):
    """Speaks one sentence, waits for the test, then speaks another (to interrupt in between)."""
    name, model = "Gated", "gated-1"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.spoke = asyncio.Event()          # the first sentence has been passed on
        self.contexts: list[str] = []

    async def stream(self, *, system, messages, tools, max_tokens, context=""):
        self.contexts.append(context)
        yield TextDelta("First sentence here. ")
        self.spoke.set()
        await self.gate.wait()
        yield TextDelta("Second sentence here.")
        yield Done("end", 1, 1)


def session_for(app, llm, call: Call, transfer_possible=False):
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def no_wait(_s):
        return None
    app.state.assistant.llm = llm
    conv = asyncio.run(app.state.repo.create_conversation("voice", "x", ""))
    call, _ = asyncio.run(app.state.calls.create(call))
    s = VoiceSession(app.state.assistant, app.state.repo, app.state.calls, app.state.jobs, call, conv.id, send,
                     transfer_possible=transfer_possible, speak_delay=no_wait)
    return s, sent


def test_interrupted_turn_is_not_spoken_and_the_next_turn_knows_what_was_heard(make_client):
    _, app, _ = make_client()
    llm = GatedLLM()
    s, sent = session_for(app, llm, Call(id="", direction="inbound", call_sid="CA1", from_number="+447700900123"))

    async def go():
        await s.start()
        turn = asyncio.create_task(s._turn("Tell me about your services"))
        await llm.spoke.wait()               # the answer is being written: only the hold line so far
        assert [m["token"] for m in sent if m["type"] == "text"] == ["One moment."]
        await s.handle({"type": "interrupt", "utteranceUntilInterrupt": "First sentence"})
        llm.gate.set()
        await turn
        llm.gate = asyncio.Event()
        llm.gate.set()
        await s._turn("Actually, do you do security?")
    asyncio.run(go())
    spoken = [m["token"].strip() for m in sent if m["type"] == "text"]
    # turn 1 was overtaken by the caller and is dropped; turn 2 is spoken in full
    assert [t for t in spoken if t != "One moment."] == ["First sentence here.", "Second sentence here.", ""]
    assert sum(1 for m in sent if m.get("last")) == 1
    assert 'they only heard: "First sentence"' in llm.contexts[1]
    assert "+447700900123" in llm.contexts[0]                                    # caller number in context
    lead = asyncio.run(app.state.repo.get_lead(s.conversation_id))
    assert lead.phone == "+447700900123"                                         # kept from caller ID


def test_phone_transfer_in_office_hours_ends_with_handoff(make_client):
    client, app, llm = make_client([
        [ToolCall("t", "transfer_call", {"reason": "human_requested", "summary": "Wants to discuss a quote"})],
        ["Connecting you to Sam Patel now."],
    ], now=NOW)                                                    # Monday 10:00 London: open
    app.state.team.get("sam").phone = "+441111111111"
    s, sent = session_for(app, app.state.assistant.llm,
                          Call(id="", direction="inbound", call_sid="CA2", from_number="+447700900123"),
                          transfer_possible=True)
    asyncio.run(s.start())
    asyncio.run(s._turn("Can I talk to a person?"))
    assert sent[-1]["type"] == "end" and json.loads(sent[-1]["handoffData"]) == {
        "action": "transfer", "consultant_id": "sam", "reason": "human_requested"}
    assert any(m.get("token") == "Connecting you to Sam Patel now." for m in sent) and s.ended
    asyncio.run(s.close())
    call = asyncio.run(app.state.calls.get(s.call.id))
    assert (call.outcome, call.transfer_to) == ("transferring", "sam")
    assert call.status == "in_progress"           # still on the line, being put through
    assert app.state.repo.escalations[s.conversation_id][0] == "human_requested"


def test_no_transfer_outside_office_hours(make_client):
    sunday = datetime(2030, 1, 6, 12, 0, tzinfo=UTC)
    client, app, llm = make_client([
        [ToolCall("t", "transfer_call", {"reason": "human_requested", "summary": "Wants a person"})],
        ["Sam will call you back tomorrow."],
    ], now=sunday)
    app.state.team.get("sam").phone = "+441111111111"
    s, sent = session_for(app, app.state.assistant.llm,
                          Call(id="", direction="inbound", call_sid="CA3", from_number="+447700900123"),
                          transfer_possible=True)
    asyncio.run(s._turn("Can I talk to a person?"))
    asyncio.run(s.close())
    assert asyncio.run(app.state.calls.get(s.call.id)).status == "completed"
    assert not any(m["type"] == "end" for m in sent)
    assert "outside office hours" in llm.calls[-1]["messages"][-1].text
    assert "+447700900123" in llm.calls[-1]["messages"][-1].text


# ---------------------------------------------------------------- phone: Twilio
@pytest.fixture
def twilio_env(monkeypatch):
    for k, v in {"VOICE_PROVIDER": "twilio", "SMS_PROVIDER": "twilio", "PUBLIC_API_URL": PUBLIC,
                 "TWILIO_ACCOUNT_SID": "AC" + "0" * 32, "TWILIO_AUTH_TOKEN": TOKEN,
                 "TWILIO_PHONE_NUMBER": "+441000000000"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def twilio_post(client, path, params):
    url = PUBLIC + path
    return client.post(path, content=urlencode(params),
                       headers={"Content-Type": "application/x-www-form-urlencoded",
                                "X-Twilio-Signature": signature(TOKEN, url, params)})


def test_incoming_call_is_answered_with_conversation_relay(make_client, twilio_env):
    client, app, _ = make_client()
    params = {"CallSid": "CA" + "1" * 32, "From": "+447700900123", "To": "+441000000000"}
    r = twilio_post(client, "/api/voice/twilio/incoming", params)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    call = asyncio.run(app.state.calls.by_sid(params["CallSid"]))
    assert 'url="wss://voice.example.com/api/voice/twilio/ws"' in r.text
    assert f'<Parameter name="call_id" value="{call.id}"/>' in r.text
    assert f'value="{call_token(TOKEN, call.id)}"' in r.text and "welcomeGreeting=" in r.text
    assert f'action="{PUBLIC}/api/voice/twilio/after?call={call.id}"' in r.text
    # Twilio retries the webhook: same call, same conversation
    twilio_post(client, "/api/voice/twilio/incoming", params)
    assert len(app.state.calls.calls) == 1
    # unsigned or wrongly signed: refused
    bad = client.post("/api/voice/twilio/incoming", content=urlencode(params),
                      headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "x"})
    assert bad.status_code == 403


def test_twilio_session_needs_our_token_then_talks(make_client, twilio_env):
    client, app, llm = make_client([["Hello, how can I help?"]], now=NOW)
    sid = "CA" + "2" * 32
    twilio_post(client, "/api/voice/twilio/incoming", {"CallSid": sid, "From": "+447700900123", "To": "+44100"})
    call = asyncio.run(app.state.calls.by_sid(sid))
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect("/api/voice/twilio/ws") as ws:
        ws.send_json({"type": "setup", "callSid": sid, "customParameters": {"call_id": call.id, "token": "nope"}})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with client.websocket_connect("/api/voice/twilio/ws") as ws:
        ws.send_json({"type": "setup", "callSid": sid,
                      "customParameters": {"call_id": call.id, "token": call_token(TOKEN, call.id)}})
        ws.send_json({"type": "prompt", "voicePrompt": "Hi there", "last": True})
        said, end = reply(ws)
    assert said == "Hello, how can I help?" and end is None
    assert "Caller's phone number: +447700900123" in llm.calls[0]["context"]


def test_after_transfer_dials_the_consultant_and_reports_a_missed_one(make_client, twilio_env):
    client, app, _ = make_client()
    app.state.team.get("sam").phone = "+441111111111"
    sid = "CA" + "3" * 32
    twilio_post(client, "/api/voice/twilio/incoming", {"CallSid": sid, "From": "+447700900123", "To": "+44100"})
    call = asyncio.run(app.state.calls.by_sid(sid))
    asyncio.run(app.state.calls.update(call.id, transfer_to="sam"))
    r = twilio_post(client, f"/api/voice/twilio/after?call={call.id}",
                    {"CallSid": sid, "HandoffData": json.dumps({"action": "transfer", "consultant_id": "sam"})})
    assert "<Dial" in r.text and "<Number>+441111111111</Number>" in r.text and 'callerId="+441000000000"' in r.text
    r = twilio_post(client, f"/api/voice/twilio/dial-done?call={call.id}",
                    {"CallSid": sid, "DialCallStatus": "no-answer"})
    assert "call you back" in r.text and "<Hangup/>" in r.text
    missed = asyncio.run(app.state.calls.get(call.id))
    assert (missed.outcome, missed.status) == ("transfer_missed", "completed")
    asyncio.run(app.state.worker.run_due())
    assert any("Missed call transfer" in t and "+447700900123" in t for _, t in app.state.fakes.notifier.posts)
    # a caller who simply hung up: nothing to do
    r = twilio_post(client, f"/api/voice/twilio/after?call={call.id}", {"CallSid": sid})
    assert "<Hangup/>" in r.text and "<Dial" not in r.text


# ---------------------------------------------------------------- outbound calls
def staff_token(client, app):
    from app.staff.store import hash_password
    asyncio.run(app.state.staff.create_user("sam@acme.example", "Sam", "consultant", hash_password("p" * 12)))
    return client.post("/api/team/login", json={"email": "sam@acme.example", "password": "p" * 12}).json()["token"]


def test_outbound_call_in_calling_hours_is_placed_once(make_client, twilio_env, monkeypatch):
    client, app, _ = make_client()
    monkeypatch.setattr(handlers, "utcnow", lambda: datetime(2030, 1, 7, 11, 0, tzinfo=UTC))   # Mon 11:00
    auth = {"Authorization": f"Bearer {staff_token(client, app)}"}
    body = {"client_message_id": "call-0001", "to": "+44 7700 900123", "name": "Jordan",
            "purpose": "the discovery call you asked for"}
    r = client.post("/api/team/calls", json=body, headers=auth)
    assert r.status_code == 200, r.text
    again = client.post("/api/team/calls", json=body, headers=auth)              # retried request
    assert again.json()["call_id"] == r.json()["call_id"]
    asyncio.run(app.state.worker.run_due())
    asyncio.run(app.state.worker.run_due())
    placed = app.state.fakes.telephony.calls
    assert len(placed) == 1 and placed[0]["to"] == "+447700900123"
    assert "Hello Jordan" in placed[0]["twiml"] and "the discovery call you asked for" in placed[0]["twiml"]
    call = asyncio.run(app.state.calls.get(r.json()["call_id"]))
    assert call.call_sid and call.status == "initiated"
    assert placed[0]["status_url"] == f"{PUBLIC}/api/voice/twilio/status?call={call.id}"
    # no answer: the team hears about it
    twilio_post(client, f"/api/voice/twilio/status?call={call.id}", {"CallSid": call.call_sid,
                                                                      "CallStatus": "no-answer"})
    asyncio.run(app.state.worker.run_due())
    assert asyncio.run(app.state.calls.get(call.id)).status == "no_answer"
    assert any("Outbound call not answered" in t for _, t in app.state.fakes.notifier.posts)


def test_outbound_call_waits_for_calling_hours(make_client, twilio_env, monkeypatch):
    client, app, _ = make_client()
    monkeypatch.setattr(handlers, "utcnow", lambda: datetime(2030, 1, 7, 8, 0, tzinfo=UTC))    # Mon 08:00
    auth = {"Authorization": f"Bearer {staff_token(client, app)}"}
    r = client.post("/api/team/calls", json={"client_message_id": "call-0002", "to": "+447700900123",
                                             "purpose": "your enquiry"}, headers=auth)
    asyncio.run(app.state.worker.run_due())
    assert app.state.fakes.telephony.calls == []
    call = asyncio.run(app.state.calls.get(r.json()["call_id"]))
    assert call.status == "scheduled"
    later = [j for j in app.state.jobs.store.jobs.values() if j.kind == "voice.outbound" and j.status == "pending"]
    assert len(later) == 1 and later[0].run_at == datetime(2030, 1, 7, 10, 0, tzinfo=UTC)


def test_failed_outbound_call_is_not_retried(make_client, twilio_env, monkeypatch):
    client, app, _ = make_client()
    monkeypatch.setattr(handlers, "utcnow", lambda: datetime(2030, 1, 7, 11, 0, tzinfo=UTC))
    app.state.fakes.telephony.fail_with = IntegrationError("Twilio: HTTP 503")
    auth = {"Authorization": f"Bearer {staff_token(client, app)}"}
    r = client.post("/api/team/calls", json={"client_message_id": "call-0003", "to": "+447700900123",
                                             "purpose": "your enquiry"}, headers=auth)
    asyncio.run(app.state.worker.run_due())
    app.state.fakes.telephony.fail_with = None
    asyncio.run(app.state.worker.run_due())
    assert app.state.fakes.telephony.calls == []                               # never rung twice
    call = asyncio.run(app.state.calls.get(r.json()["call_id"]))
    assert call.status == "failed" and "unknown whether" in call.error
    assert any("Outbound call failed" in t for _, t in app.state.fakes.notifier.posts)


def test_calls_need_phone_calls_switched_on_and_a_valid_number(make_client):
    client, app, _ = make_client()
    auth = {"Authorization": f"Bearer {staff_token(client, app)}"}
    r = client.post("/api/team/calls", json={"client_message_id": "call-0004", "to": "+447700900123",
                                             "purpose": "x" * 5}, headers=auth)
    assert r.status_code == 409


def test_bad_numbers_are_refused(make_client, twilio_env):
    client, app, _ = make_client()
    auth = {"Authorization": f"Bearer {staff_token(client, app)}"}
    for to in ("07700900123", "+0123", "call me"):
        r = client.post("/api/team/calls", json={"client_message_id": "call-0005", "to": to, "purpose": "hello"},
                        headers=auth)
        assert r.status_code == 422, to


# ---------------------------------------------------------------- text messages
def test_booking_with_a_phone_number_sends_text_messages(make_client):
    client, app, llm = make_client([
        [ToolCall("a", "update_lead", {"need": "Azure", "company_size": "40 people", "name": "Jo Smith",
                                       "email": "jo@example.com", "phone": "+447700900123"})],
        [ToolCall("b", "find_available_slots", {})],
        ["Pick a time."],
    ], now=NOW)
    cid, headers, _ = start(client)
    from tests.test_chat_api import say
    say(client, cid, headers, "Book me in: Jo Smith, jo@example.com, 40 people, Azure", "msg-00001")
    slot = next(line.split("start=")[1].split()[0] for m in llm.calls[-1]["messages"] if m.role == "tool"
                for line in m.text.splitlines() if "start=" in line)
    llm.rounds += [[ToolCall("c", "book_discovery_call", {"start": slot})], ["Booked."]]
    say(client, cid, headers, "The first one", "msg-00002")
    sms_jobs = sorted(j.key.split(":")[1] for j in app.state.jobs.store.jobs.values() if j.kind == "sms.booking")
    assert sms_jobs == ["confirmation", "reminder_24h"]
    asyncio.run(app.state.worker.run_due())
    to, text = app.state.fakes.sms.sent[0]
    assert to == "+447700900123" and "booked" in text and "DC-" in text and len(text) <= 200


class DownLLM(LLMProvider):
    """A model that is out of quota."""
    name, model = "Down", "down-1"

    async def stream(self, *, system, messages, tools, max_tokens, context=""):
        from app.llm.base import RateLimited
        raise RateLimited("Groq daily limit reached", 3600, daily=True)
        yield  # pragma: no cover


def test_unavailable_model_ends_a_browser_call_without_promising_a_call_back(make_client):
    _, app, _ = make_client()
    s, sent = session_for(app, DownLLM(), Call(id="", direction="web"))
    asyncio.run(s._turn("Hello?"))
    said = " ".join(m.get("token", "") for m in sent if m["type"] == "text")
    assert "try again in a little while" in said and "call you back" not in said
    assert json.loads(sent[-1]["handoffData"]) == {"action": "hangup", "reason": "unavailable"} and s.ended
    assert s.conversation_id not in app.state.repo.escalations          # nobody to call back


def test_unavailable_model_on_a_phone_call_asks_a_consultant_to_call_back(make_client):
    _, app, _ = make_client()
    s, sent = session_for(app, DownLLM(), Call(id="", direction="inbound", call_sid="CA9",
                                               from_number="+447700900123"))
    asyncio.run(s._turn("Hello?"))
    said = " ".join(m.get("token", "") for m in sent if m["type"] == "text")
    assert "call you back on this number" in said and sent[-1]["type"] == "end"
    assert app.state.repo.escalations[s.conversation_id][0] == "cannot_help"


class SlowLLM(LLMProvider):
    """Takes its time before the first word (e.g. waiting out a rate limit)."""
    name, model = "Slow", "slow-1"

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def stream(self, *, system, messages, tools, max_tokens, context=""):
        await self.gate.wait()
        yield TextDelta("Here's the answer.")
        yield Done("end", 1, 1)


def test_a_slow_answer_gets_one_moment_so_the_line_is_not_silent(make_client):
    _, app, _ = make_client()
    llm = SlowLLM()
    s, sent = session_for(app, llm, Call(id="", direction="web"))

    async def go():
        turn = asyncio.create_task(s._turn("What do you do?"))
        for _ in range(20):
            await asyncio.sleep(0)
        llm.gate.set()
        await turn
    asyncio.run(go())
    assert [m["token"] for m in sent if m["type"] == "text"] == ["One moment.", "Here's the answer.", ""]


def test_only_the_final_answer_of_a_turn_is_spoken(make_client):
    _, app, _ = make_client([
        ["What do you need help with?", ToolCall("a", "update_lead", {"name": "Tanya"})],
        ["Thanks Tanya. What's your email address?"],
    ], now=NOW)
    s, sent = session_for(app, app.state.assistant.llm, Call(id="", direction="web"))
    asyncio.run(s._turn("My name is Tanya"))
    said = "".join(m["token"] for m in sent if m["type"] == "text" and m["token"] != "One moment.")
    assert said == "Thanks Tanya. What's your email address?"


def test_spoken_email_addresses_become_real_ones():
    from app.assistant.tools import email_from_speech
    assert email_from_speech("tanya dot k at the rate gmail dot com") == "tanya.k@gmail.com"
    assert email_from_speech("Tanya at gmail.com") == "tanya@gmail.com"
    assert email_from_speech("jo underscore smith at north dash wind dot co dot uk") == "jo_smith@north-wind.co.uk"
    assert email_from_speech("t a n y a at outlook dot com") == "tanya@outlook.com"
    assert email_from_speech("jo@example.com") == "jo@example.com"


def test_a_spoken_email_is_read_back_before_anything_else(make_client):
    _, app, llm = make_client([
        [ToolCall("a", "update_lead", {"email": "one click writer 47 at gmail dot com"})],
        ["Thanks."],
    ], now=NOW)
    s, sent = session_for(app, llm, Call(id="", direction="web"))
    asyncio.run(s._turn("my email is one click writer 47 at gmail dot com"))
    result = llm.calls[-1]["messages"][-1].text
    assert "o n e c l i c k w r i t e r 4 7 at gmail dot com" in result and "ask if that's right" in result
    assert asyncio.run(app.state.repo.get_lead(s.conversation_id)).email == "oneclickwriter47@gmail.com"
