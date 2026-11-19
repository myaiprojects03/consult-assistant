"""Wire-format tests for HubSpot, Google Calendar, Gmail and Slack (no network: mocked HTTP)."""
import asyncio
import base64
import email
import email.policy
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.integrations.base import CalendarEvent, CRMRecord, IntegrationError
from app.integrations.google import GmailMailer, GoogleAuth, GoogleCalendar, build_mime
from app.integrations.hubspot import HubSpotCRM, contact_properties
from app.integrations.slack import SlackNotifier, escape


def run(coro):
    return asyncio.run(coro)


def mocked(handler, **kw):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(record), **kw), seen


# ---------------- HubSpot ----------------
def hubspot_api(state):
    def handler(r):
        path = r.url.path
        if path.endswith("/contacts/batch/upsert"):
            return httpx.Response(200, json={"status": "COMPLETE", "results": [{"id": "101"}]})
        if path == "/crm/v3/objects/deals" and r.method == "POST":
            return httpx.Response(201, json={"id": "201"})
        if path == "/crm/v3/objects/notes" and r.method == "POST":
            return httpx.Response(201, json={"id": "301"})
        if path.startswith("/crm/v3/objects/notes/") and r.method == "PATCH":
            return httpx.Response(404 if state.get("note_gone") else 200, json={})
        return httpx.Response(200, json={})
    return handler


def test_hubspot_first_sync_creates_contact_deal_and_note():
    client, seen = mocked(hubspot_api({}), base_url="https://api.hubapi.com")
    crm = HubSpotCRM("tok", client=client)
    res = run(crm.sync(CRMRecord(email="Jo@Example.com", name="Jo Smith", company="Acme", lead_status="qualified",
                                 booked=True, deal_name="Acme: security review", note_html="<p>hi</p>")))
    assert (res.contact_id, res.deal_id, res.note_id) == ("101", "201", "301")
    upsert = json.loads(seen[0].content)["inputs"][0]
    assert upsert["idProperty"] == "email" and upsert["id"] == "jo@example.com"
    assert upsert["properties"] == {"firstname": "Jo", "lastname": "Smith", "company": "Acme",
                                    "hs_lead_status": "OPEN_DEAL"}
    deal = json.loads(seen[1].content)
    assert deal["properties"]["dealstage"] == "appointmentscheduled"
    assert deal["associations"][0]["types"][0]["associationTypeId"] == 3
    note = json.loads(seen[2].content)
    assert [a["types"][0]["associationTypeId"] for a in note["associations"]] == [202, 214]
    assert note["properties"]["hs_note_body"] == "<p>hi</p>"


def test_hubspot_later_sync_updates_the_same_records():
    client, seen = mocked(hubspot_api({}), base_url="https://api.hubapi.com")
    crm = HubSpotCRM("tok", client=client)
    run(crm.sync(CRMRecord(email="jo@example.com", note_html="<p>v2</p>", contact_id="101", note_id="301")))
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/crm/v3/objects/contacts/batch/upsert"), ("PATCH", "/crm/v3/objects/notes/301")]


def test_hubspot_recreates_a_note_deleted_in_hubspot_and_refuses_bad_keys():
    client, seen = mocked(hubspot_api({"note_gone": True}), base_url="https://api.hubapi.com")
    res = run(HubSpotCRM("tok", client=client).sync(CRMRecord(email="a@b.co", note_id="999")))
    assert res.note_id == "301" and seen[-1].method == "POST"
    denied, _ = mocked(lambda r: httpx.Response(401, json={"message": "bad token"}), base_url="https://api.hubapi.com")
    with pytest.raises(IntegrationError) as err:
        run(HubSpotCRM("tok", client=denied).sync(CRMRecord(email="a@b.co")))
    assert not err.value.retryable


def test_lead_status_mapping():
    assert contact_properties(CRMRecord(email="a@b.co", lead_status="not_a_fit"))["hs_lead_status"] == "UNQUALIFIED"
    assert contact_properties(CRMRecord(email="a@b.co"))["hs_lead_status"] == "NEW"


# ---------------- Google ----------------
def google_api(state):
    def handler(r):
        url = str(r.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            state["tokens"] = state.get("tokens", 0) + 1
            if state.get("revoked"):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if url.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": [
                {"start": "2030-01-08T10:00:00Z", "end": "2030-01-08T11:00:00Z"}]}}})
        if r.method == "POST" and "/events" in url:
            if state.get("exists"):
                return httpx.Response(409, json={})
            return httpx.Response(200, json={"id": "dcabc123", "hangoutLink": "https://meet.google.com/abc-defg-hij"})
        if r.method == "PATCH":
            return httpx.Response(200, json={"id": "dcabc123", "hangoutLink": "https://meet.google.com/abc-defg-hij"})
        if r.method == "DELETE":
            return httpx.Response(410)
        if "gmail" in url:
            state["raw"] = json.loads(r.content)["raw"]
        return httpx.Response(200, json={})
    return handler


def google(state):
    client, seen = mocked(google_api(state))
    return GoogleAuth("cid", "secret", "refresh", client), seen


EVENT = CalendarEvent("primary", "dcabc123", "Discovery call: Jo", "details", datetime(2030, 1, 8, 9, tzinfo=UTC),
                      datetime(2030, 1, 8, 9, 30, tzinfo=UTC), "Europe/London", [("jo@example.com", "Jo")])


def test_calendar_busy_events_and_token_reuse():
    state: dict = {}
    auth, seen = google(state)
    cal = GoogleCalendar(auth)
    busy = run(cal.busy(["primary"], EVENT.start, EVENT.end))
    assert busy == {"primary": [(datetime(2030, 1, 8, 10, tzinfo=UTC), datetime(2030, 1, 8, 11, tzinfo=UTC))]}
    link = run(cal.create_event(EVENT))
    create = seen[-1]
    assert link == "https://meet.google.com/abc-defg-hij"
    assert create.url.params["sendUpdates"] == "all" and create.url.params["conferenceDataVersion"] == "1"
    body = json.loads(create.content)
    assert body["id"] == "dcabc123"
    assert body["conferenceData"]["createRequest"] == {"requestId": "dcabc123",
                                                       "conferenceSolutionKey": {"type": "hangoutsMeet"}}
    assert json.loads(create.content)["attendees"] == [{"email": "jo@example.com", "displayName": "Jo"}]
    run(cal.cancel_event("primary", "dcabc123"))           # already gone (410) is fine
    assert state["tokens"] == 1 and seen[1].headers["Authorization"] == "Bearer at"


def test_calendar_create_retry_updates_the_existing_event():
    state = {"exists": True}
    auth, seen = google(state)
    assert run(GoogleCalendar(auth).create_event(EVENT)) == "https://meet.google.com/abc-defg-hij"
    assert [r.method for r in seen[1:]] == ["POST", "PATCH"]
    assert seen[-1].url.path == "/calendar/v3/calendars/primary/events/dcabc123"


def test_revoked_google_sign_in_is_a_permanent_error():
    auth, _ = google({"revoked": True})
    with pytest.raises(IntegrationError) as err:
        run(GoogleCalendar(auth).busy(["primary"], EVENT.start, EVENT.end))
    assert not err.value.retryable and "google_auth" in str(err.value)
    with pytest.raises(IntegrationError):
        GoogleAuth("", "", "", httpx.AsyncClient())


def test_gmail_message_and_header_injection():
    state: dict = {}
    auth, _ = google(state)
    run(GmailMailer(auth, "Acme IT", "hello@acme.example").send(
        to_email="jo@example.com", to_name="Jo\r\nBcc: victim@x.com", subject="Booked\nBcc: x@y.z", text="Hi Jo"))
    msg = email.message_from_bytes(base64.urlsafe_b64decode(state["raw"]), policy=email.policy.default)
    assert msg["To"] == '"Jo Bcc: victim@x.com" <jo@example.com>' and msg["Bcc"] is None
    assert msg["Subject"] == "Booked Bcc: x@y.z" and msg["From"] == "Acme IT <hello@acme.example>"
    assert msg.get_content().strip() == "Hi Jo"
    no_from = email.message_from_bytes(build_mime(from_name="Acme", from_address="", to_email="a@b.co", to_name="",
                                                  subject="s", text="t"))
    assert no_from["From"] is None


# ---------------- Slack ----------------
def test_slack_posts_and_sorts_errors():
    client, seen = mocked(lambda r: httpx.Response(200, json={"ok": True}))
    run(SlackNotifier("xoxb", client=client).post("#leads", "hello"))
    assert json.loads(seen[0].content) == {"channel": "#leads", "text": "hello", "unfurl_links": False,
                                           "unfurl_media": False}
    missing, _ = mocked(lambda r: httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(IntegrationError) as err:
        run(SlackNotifier("xoxb", client=missing).post("#nope", "x"))
    assert not err.value.retryable and "invite the app" in str(err.value)
    limited, _ = mocked(lambda r: httpx.Response(429, headers={"Retry-After": "3"}))
    with pytest.raises(IntegrationError) as err:
        run(SlackNotifier("xoxb", client=limited).post("#leads", "x"))
    assert err.value.retryable


def test_visitor_text_cannot_ping_the_channel():
    assert escape("<!channel> hi <@U123> & <https://x|y>") == "&lt;!channel&gt; hi &lt;@U123&gt; &amp; &lt;https://x|y&gt;"
