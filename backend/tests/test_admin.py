"""Phase 5: the admin dashboard. Content edits (checked, versioned, applied live, admins only) and the
figures and lists (on real Postgres)."""
import asyncio
import os

import pytest

from app.llm.base import ToolCall
from app.staff.store import hash_password
from tests.test_chat_api import say, start

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
PASSWORD = "admin password 1"


def staff(client, app, email, role):
    # on the app's own event loop: its database pool belongs to it
    client.portal.call(app.state.staff.create_user, email, email.split("@")[0].title(), role,
                       hash_password(PASSWORD))
    token = client.post("/api/team/login", json={"email": email, "password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- content editing
def test_only_admins_can_use_the_dashboard(make_client):
    client, app, _ = make_client()
    consultant = staff(client, app, "sam@acme.example", "consultant")
    assert client.get("/api/admin/content").status_code == 401
    assert client.get("/api/admin/content", headers=consultant).status_code == 403
    admin = staff(client, app, "boss@acme.example", "admin")
    items = client.get("/api/admin/content", headers=admin).json()["items"]
    paths = [i["path"] for i in items]
    assert "faqs.yaml" in paths and "rules.yaml" in paths and "internal/sop-support-and-escalation.md" in paths
    titles = {i["path"]: i["title"] for i in items}                          # documents by their own title
    assert titles["internal/sop-support-and-escalation.md"] == "Support and escalation procedure"
    assert client.get("/api/admin/overview", headers=admin).status_code == 503       # no database here


def test_an_faq_edit_is_checked_saved_and_used_at_once(make_client):
    client, app, llm = make_client()
    admin = staff(client, app, "boss@acme.example", "admin")
    current = client.get("/api/admin/content/faqs.yaml", headers=admin).json()
    assert current["version"] == 0 and current["text"] == current["default"]
    new_faq = ("\n- slug: faq-parking\n  title: Is there parking at your office?\n  body: |\n"
               "    Yes, two visitor spaces are free behind the building.\n")
    r = client.put("/api/admin/content/faqs.yaml", headers=admin,
                   json={"text": current["text"] + new_faq, "base_version": 0, "note": "parking"})
    assert r.status_code == 200 and r.json()["version"] == 1
    # the assistant finds it straight away
    llm.rounds += [[ToolCall("k", "search_knowledge", {"query": "visitor parking spaces office"})], ["Yes."]]
    cid, headers, _ = start(client)
    say(client, cid, headers, "Can I park at your office?", "msg-00001")
    assert "two visitor spaces" in llm.calls[-1]["messages"][-1].text
    # someone else saved meanwhile: refused, nothing lost
    stale = client.put("/api/admin/content/faqs.yaml", headers=admin, json={"text": current["text"], "base_version": 0})
    assert stale.status_code == 409
    got = client.get("/api/admin/content/faqs.yaml", headers=admin).json()
    assert got["version"] == 1 and got["history"][0]["note"] == "parking"
    # back to the default
    r = client.post("/api/admin/content/faqs.yaml/reset", headers=admin, json={"base_version": 1})
    assert r.status_code == 200
    assert client.get("/api/admin/content/faqs.yaml", headers=admin).json()["text"] == current["default"]
    events = [k for _, k, _ in app.state.repo.events]
    assert "admin_content_saved" in events and "admin_content_reset" in events


def test_edits_that_would_break_the_assistant_are_refused(make_client):
    client, app, _ = make_client()
    admin = staff(client, app, "boss@acme.example", "admin")
    bad_yaml = client.put("/api/admin/content/faqs.yaml", headers=admin,
                          json={"text": "- slug: x\n  title: [unclosed\n", "base_version": 0})
    assert bad_yaml.status_code == 422 and "line" in bad_yaml.json()["detail"]
    rules = client.get("/api/admin/content/rules.yaml", headers=admin).json()["text"]
    unknown = client.put("/api/admin/content/rules.yaml", headers=admin,
                         json={"text": rules + "\nnot_a_setting: true\n", "base_version": 0})
    assert unknown.status_code == 422 and "rules.yaml" in unknown.json()["detail"]
    dupe = client.put("/api/admin/content/services.yaml", headers=admin, json={
        "text": "- slug: faq-pricing\n  title: Clash\n  body: Same slug as an FAQ.\n", "base_version": 0})
    assert dupe.status_code == 422 and "Duplicate" in dupe.json()["detail"]
    assert client.put("/api/admin/content/../app/main.py", headers=admin,
                      json={"text": "x", "base_version": 0}).status_code == 404
    assert client.get("/api/admin/content/rules.yaml", headers=admin).json()["version"] == 0   # nothing saved


def test_rule_changes_apply_to_new_calls_and_chats(make_client):
    client, app, _ = make_client()
    admin = staff(client, app, "boss@acme.example", "admin")
    rules = client.get("/api/admin/content/rules.yaml", headers=admin).json()["text"]
    old = app.state.assistant.voice_rules.greeting
    changed = rules.replace("How can I help you today?", "What can I do for you?", 1)
    assert changed != rules
    r = client.put("/api/admin/content/rules.yaml", headers=admin, json={"text": changed, "base_version": 0})
    assert r.status_code == 200
    assert app.state.assistant.voice_rules.greeting != old and "What can I do for you?" in \
        app.state.assistant.voice_rules.greeting
    call = client.post("/api/voice/web/sessions", json={}).json()
    assert "What can I do for you?" in call["greeting"]


def test_internal_documents_can_be_added_and_removed(make_client):
    client, app, llm = make_client()
    admin = staff(client, app, "boss@acme.example", "admin")
    doc = "---\ntitle: Laptop refresh policy\nkind: policy\n---\n## Rule\nLaptops are replaced every four years.\n"
    r = client.put("/api/admin/content/internal/laptop-refresh.md", headers=admin,
                   json={"text": doc, "base_version": 0})
    assert r.status_code == 200
    paths = [i["path"] for i in client.get("/api/admin/content", headers=admin).json()["items"]]
    assert "internal/laptop-refresh.md" in paths
    no_header = client.put("/api/admin/content/internal/oops.md", headers=admin,
                           json={"text": "# hi", "base_version": 0})
    assert no_header.status_code == 422
    # the team assistant can now cite it; the public chat never sees it
    hits = asyncio.run(app.state.repo.kb_search(query="laptops replaced every four years", embedding=None,
                                                audience="internal", k=5, clients=None))
    assert any(h.slug == "internal/laptop-refresh" for h in hits)
    public = asyncio.run(app.state.repo.kb_search(query="laptops replaced every four years", embedding=None,
                                                  audience="public", k=5))
    assert not any(h.slug.startswith("internal/") for h in public)
    r = client.delete("/api/admin/content/internal/laptop-refresh.md?base_version=1", headers=admin)
    assert r.status_code == 200
    paths = [i["path"] for i in client.get("/api/admin/content", headers=admin).json()["items"]]
    assert "internal/laptop-refresh.md" not in paths
    assert client.delete("/api/admin/content/faqs.yaml?base_version=0", headers=admin).status_code == 400


def test_faqs_can_be_edited_as_a_form(make_client):
    client, app, llm = make_client()
    admin = staff(client, app, "boss@acme.example", "admin")
    f = client.get("/api/admin/content/faqs.yaml", headers=admin).json()
    entries = f["entries"]
    assert entries[0]["slug"] == "faq-pricing" and entries[0]["title"] == "How much do your services cost?"
    assert "\n" not in entries[0]["body"]                                          # file line wraps joined
    entries[1]["body"] = "Companies from 5 people.\n\nWe also help sole traders."    # edit, keep its slug
    entries.append({"title": "Is there parking: for visitors?", "body": "Yes, two spaces behind the building."})
    del entries[2]                                                                 # remove one
    r = client.put("/api/admin/content-form/faqs.yaml", headers=admin,
                   json={"entries": entries, "base_version": 0, "note": "form edit"})
    assert r.status_code == 200, r.text
    saved = client.get("/api/admin/content/faqs.yaml", headers=admin).json()
    assert saved["text"].startswith("# SAMPLE CONTENT")                          # header comment kept
    slugs = [e["slug"] for e in saved["entries"]]
    assert "faq-locations" not in slugs and slugs[1] == "faq-company-size"
    assert slugs[-1] == "faq-is-there-parking-for-visitors"
    assert saved["entries"][-1]["title"] == "Is there parking: for visitors?"       # quoted safely
    assert saved["entries"][1]["body"] == "Companies from 5 people.\n\nWe also help sole traders."
    hits = asyncio.run(app.state.repo.kb_search(query="parking spaces behind the building", embedding=None,
                                                audience="public", k=3))
    assert hits and hits[0].slug == "faq-is-there-parking-for-visitors"
    empty = client.put("/api/admin/content-form/faqs.yaml", headers=admin,
                       json={"entries": [{"title": "No answer", "body": " "}], "base_version": 1})
    assert empty.status_code == 422 and "Entry 1" in empty.json()["detail"]
    assert client.put("/api/admin/content-form/rules.yaml", headers=admin,
                      json={"entries": [], "base_version": 0}).status_code == 404


# ---------------------------------------------------------------- figures and lists (Postgres)
pg = pytest.mark.skipif(not TEST_DB, reason="set TEST_DATABASE_URL to a Postgres with pgvector")


@pg
def test_dashboard_figures_lists_and_transcript(pg_env, make_client):
    client, app, llm = make_client([
        [ToolCall("a", "update_lead", {"name": "Jo Smith", "email": "jo@example.com", "need": "Azure migration",
                                       "company_size": "40 people"})],
        ["Thanks Jo."],
        [ToolCall("h", "handover_to_human", {"reason": "pricing_negotiation", "summary": "Wants a discount"})],
        ["A consultant will be in touch."],
    ])
    cid, headers, _ = start(client)
    say(client, cid, headers, "Hi, I'm Jo (jo@example.com) from a 40 person firm, we need an Azure migration",
        "msg-00001")
    say(client, cid, headers, "Can you do it 20% cheaper?", "msg-00002")
    admin = staff(client, app, "boss@acme.example", "admin")
    o = client.get("/api/admin/overview?days=7", headers=admin)
    assert o.status_code == 200, o.text
    o = o.json()
    assert o["funnel"]["conversations"] == 1 and o["funnel"]["contacts"] == 1
    assert o["handovers"] == [{"reason": "pricing_negotiation", "n": 1}]
    assert len(o["daily"]) == 8 and sum(d["conversations"] for d in o["daily"]) == 1
    assert o["quality"]["turns"] == 2
    convs = client.get("/api/admin/conversations?status=handover", headers=admin).json()["items"]
    assert [c["id"] for c in convs] == [cid] and convs[0]["name"] == "Jo Smith"
    assert convs[0]["first_message"].startswith("Hi, I'm Jo")
    assert client.get("/api/admin/conversations?q=nobody", headers=admin).json()["items"] == []
    detail = client.get(f"/api/admin/conversations/{cid}", headers=admin).json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles[0] == "user" and "assistant" in roles and "token_hash" not in detail["conversation"]
    assert detail["lead"]["email"] == "jo@example.com"
    assert any(e["kind"] == "handover" for e in detail["events"])
    assert client.get("/api/admin/leads", headers=admin).json()["items"][0]["name"] == "Jo Smith"
    assert client.get("/api/admin/handovers", headers=admin).json()["items"][0]["escalation_reason"] == \
        "pricing_negotiation"
    for path in ("bookings", "calls", "events?kind=handover", "jobs?status=pending"):
        assert client.get(f"/api/admin/{path}", headers=admin).status_code == 200, path
    # looking at a transcript is itself logged
    logged = client.get("/api/admin/events?kind=admin_viewed_conversation", headers=admin).json()["items"]
    assert logged and logged[0]["data"]["conversation"] == cid
    assert logged[0]["by_name"] == "Boss"
    # team threads are staff work, not traffic
    sam = staff(client, app, "sam@acme.example", "consultant")
    client.post("/api/team/threads", headers=sam)
    assert client.get("/api/admin/overview?days=7", headers=admin).json()["funnel"]["conversations"] == 1


@pg
def test_content_edits_are_kept_in_the_database_across_restarts(pg_env, make_client):
    client, app, _ = make_client()
    admin = staff(client, app, "boss@acme.example", "admin")
    sms = client.get("/api/admin/content/sms.yaml", headers=admin).json()["text"]
    client.put("/api/admin/content/sms.yaml", headers=admin,
               json={"text": sms.replace("is booked for", "is confirmed for"), "base_version": 0})
    client2, app2, _ = make_client()                     # a second server (or a restart)
    assert "is confirmed for" in app2.state.job_context.sms_templates["confirmation"]
    assert app2.state.content_stamp == app.state.content_stamp
