"""Client verification, tickets, handover rules, staff sign-in and the internal assistant's access
rules, through the HTTP API with a scripted model and in-memory storage."""
import asyncio
import json
import re

import httpx

from app.config import CONTENT_DIR
from app.integrations.base import TicketInfo, TicketState
from app.integrations.jira import JiraTicketing
from app.kb.content import sync_content
from app.llm.base import ToolCall
from app.staff.store import hash_password
from scripts.seed_demo import seed
from tests.test_chat_api import say, start

PASSWORD = "correct horse battery"


def setup_clients(app, contact="jo@northwind.example"):
    made = asyncio.run(seed(app.state.clients, contact))
    ids = {slug: c.id for slug, c in made.items()}
    asyncio.run(sync_content(app.state.repo, app.state.embedder, CONTENT_DIR, ids))
    return made


def code_from(fakes):
    return re.search(r"(\d{6})", fakes.mailer.sent[-1]["subject"]).group(1)


def verified_chat(client, app, llm_rounds_after: list):
    """A website conversation verified as Northwind (via a real emailed code)."""
    cid, headers, _ = start(client)
    say(client, cid, headers, "I'm an existing client", "msg-00001")
    llm = app.state.assistant.llm
    llm.rounds += [[ToolCall("v1", "verify_client", {"email": "jo@northwind.example"})], ["Code sent."]]
    say(client, cid, headers, "jo@northwind.example", "msg-00002")
    code = code_from(app.state.fakes)
    llm.rounds += [[ToolCall("v2", "verify_client", {"code": code})], ["You're verified."]]
    say(client, cid, headers, code, "msg-00003")
    llm.rounds += llm_rounds_after
    return cid, headers


# ---------------------------------------------------------------- verification
def test_verification_does_not_reveal_who_is_a_client(make_client):
    client, app, llm = make_client([
        ["Sure."],
        [ToolCall("a", "verify_client", {"email": "stranger@example.com"})], ["ok"],
        [ToolCall("b", "verify_client", {"email": "jo@northwind.example"})], ["ok"],
    ])
    setup_clients(app)
    cid, headers, _ = start(client)
    say(client, cid, headers, "hi", "msg-00001")
    say(client, cid, headers, "stranger@example.com", "msg-00002")
    unknown = llm.calls[-1]["messages"][-1].text
    say(client, cid, headers, "jo@northwind.example", "msg-00003")
    known = llm.calls[-1]["messages"][-1].text
    assert unknown == known                                    # same answer either way
    assert [m["to"] for m in app.state.fakes.mailer.sent] == ["jo@northwind.example"]
    # before verification the client tools are not even offered
    assert "client_overview" not in llm.calls[0]["tools"] and "verify_client" in llm.calls[0]["tools"]


def test_wrong_codes_then_lockout(make_client):
    client, app, llm = make_client([[ToolCall("a", "verify_client", {"email": "jo@northwind.example"})], ["ok"]])
    setup_clients(app)
    cid, headers, _ = start(client)
    say(client, cid, headers, "jo@northwind.example", "msg-00001")
    for i in range(5):
        llm.rounds += [[ToolCall(f"c{i}", "verify_client", {"code": "000000"})], ["no"]]
        say(client, cid, headers, "000000", f"msg-0001{i}")
    assert "Too many wrong codes" in llm.calls[-1]["messages"][-1].text
    right = code_from(app.state.fakes)
    llm.rounds += [[ToolCall("r", "verify_client", {"code": right})], ["no"]]
    say(client, cid, headers, right, "msg-00020")
    assert "No code is waiting" in llm.calls[-1]["messages"][-1].text   # the code was cancelled
    conv = asyncio.run(app.state.repo.get_conversation(cid))
    assert conv.client_id is None


def test_verified_client_sees_projects_and_live_ticket_status(make_client):
    client, app, llm = make_client([["Welcome back."]])
    made = setup_clients(app)
    cid, headers = verified_chat(client, app, [[ToolCall("o", "client_overview", {})], ["Here's where things are."]])
    conv = asyncio.run(app.state.repo.get_conversation(cid))
    assert conv.client_id == made["northwind"].id and conv.verified_email == "jo@northwind.example"
    # the client tools appear in the same turn the code is confirmed
    assert "client_overview" in llm.calls[-1]["tools"] and "verify_client" not in llm.calls[-1]["tools"]
    # a ticket in Jira that a consultant has picked up
    from app.clients.store import Ticket
    t = asyncio.run(app.state.clients.create_ticket(Ticket(
        id="", reference="", client_id=made["northwind"].id, contact_email="jo@northwind.example",
        summary="VPN drops every hour")))
    asyncio.run(app.state.clients.update_ticket(t.id, external_key="SUP-9"))
    app.state.fakes.ticketing.states["SUP-9"] = TicketState("In Progress", "in_progress", "Sam Patel")
    say(client, cid, headers, "Where are we on things?", "msg-00004")
    overview = llm.calls[-1]["messages"][-1].text
    assert "Azure landing zone" in overview and "Brightwater" not in overview
    assert f"{t.reference}" in overview and "In Progress, with Sam Patel" in overview
    assert asyncio.run(app.state.clients.get_ticket(t.id)).status == "in_progress"


def test_normal_ticket_goes_to_jira_and_the_support_channel(make_client):
    client, app, llm = make_client([["Welcome back."]])
    made = setup_clients(app)
    made["brightwater"]  # noqa: B018
    cid, headers = verified_chat(client, app, [
        [ToolCall("t", "raise_ticket", {"summary": "New starter needs a laptop", "details": "Starts Monday",
                                        "priority": "normal", "category": "access"})],
        ["Done, your reference is in the message."]])
    say(client, cid, headers, "Can you set up a laptop for a new starter?", "msg-00004")
    result = llm.calls[-1]["messages"][-1].text
    ref = re.search(r"TK-[A-Z0-9]{6}", result).group(0)
    asyncio.run(app.state.worker.run_due())
    fakes = app.state.fakes
    assert [t.reference for t in fakes.ticketing.created] == [ref]
    info = fakes.ticketing.created[0]
    assert info.client_slug == "northwind" and info.contact_email == "jo@northwind.example"
    ticket = asyncio.run(app.state.clients.ticket_by_reference(made["northwind"].id, ref))
    assert ticket.external_key == "SUP-1"
    channels = [c for c, _ in fakes.notifier.posts]
    assert channels == [app.state.integrations.channels["support"]]
    assert app.state.repo.escalations.get(cid) is None             # a normal ticket is not a handover
    # the client adds information later: one Jira comment, even if the job runs twice
    llm.rounds += [[ToolCall("u", "update_ticket", {"reference": ref.lower(), "message": "Her name is Kim"})],
                   ["Added."]]
    say(client, cid, headers, f"For {ref}: her name is Kim", "msg-00005")
    asyncio.run(app.state.worker.run_due())
    assert len(fakes.ticketing.comments) == 1 and "Her name is Kim" in fakes.ticketing.comments[0][1]


def test_enterprise_high_priority_ticket_is_handed_to_the_account_manager(make_client):
    client, app, llm = make_client([["Welcome back."]])
    setup_clients(app)
    cid, headers = verified_chat(client, app, [
        [ToolCall("t", "raise_ticket", {"summary": "ERP is very slow for all users", "priority": "high",
                                        "category": "outage"})], ["Sam will call you."]])
    say(client, cid, headers, "Our ERP is crawling for everyone", "msg-00004")
    assert "enterprise account" in llm.calls[-1]["messages"][-1].text
    assert app.state.repo.escalations[cid][0] == "enterprise_account"
    asyncio.run(app.state.worker.run_due())
    posts = app.state.fakes.notifier.posts
    handover = [t for c, t in posts if c == app.state.integrations.channels["handovers"]]
    assert handover and "Northwind Traders" in handover[0] and "jo@northwind.example" in handover[0]
    assert not [c for c, _ in posts if c == app.state.integrations.channels["support"]
                and app.state.integrations.channels["support"] != app.state.integrations.channels["handovers"]]


def test_a_client_cannot_reach_another_clients_ticket(make_client):
    client, app, llm = make_client([["Welcome back."]])
    made = setup_clients(app)
    from app.clients.store import Ticket
    other = asyncio.run(app.state.clients.create_ticket(Ticket(
        id="", reference="", client_id=made["brightwater"].id, contact_email="office@brightwater.example",
        summary="Printer offline")))
    cid, headers = verified_chat(client, app, [
        [ToolCall("u", "update_ticket", {"reference": other.reference, "message": "close it"})], ["Hmm."]])
    say(client, cid, headers, f"Update {other.reference}", "msg-00004")
    assert "No ticket with that reference" in llm.calls[-1]["messages"][-1].text
    asyncio.run(app.state.worker.run_due())
    assert app.state.fakes.ticketing.comments == []


def test_client_tools_refuse_an_unverified_conversation(make_client):
    client, app, llm = make_client([[ToolCall("o", "client_overview", {})], ["Please verify first."]])
    setup_clients(app)
    cid, headers, _ = start(client)
    say(client, cid, headers, "Show me my tickets", "msg-00001")
    assert "Not verified" in llm.calls[-1]["messages"][-1].text


# ---------------------------------------------------------------- staff sign-in
def add_staff(app, email, role="consultant", grant=()):
    user = asyncio.run(app.state.staff.create_user(email, email.split("@")[0].title(), role,
                                                   hash_password(PASSWORD), "sam"))
    for slug in grant:
        c = next(c for c in asyncio.run(app.state.clients.list_clients(None)) if c.slug == slug)
        asyncio.run(app.state.staff.grant(user.id, c.id))
    return user


def login(client, email, password=PASSWORD):
    return client.post("/api/team/login", json={"email": email, "password": password})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_staff_login_scope_and_logout(make_client):
    client, app, _ = make_client()
    setup_clients(app)
    add_staff(app, "sam@acme.example", grant=["brightwater"])
    add_staff(app, "boss@acme.example", role="admin")
    assert login(client, "sam@acme.example", "wrong password!!").status_code == 401
    assert login(client, "nobody@acme.example").status_code == 401          # same answer as a wrong password
    assert client.get("/api/team/me").status_code == 401
    r = login(client, "Sam@Acme.example")
    assert r.status_code == 200 and [c["slug"] for c in r.json()["clients"]] == ["brightwater"]
    token = r.json()["token"]
    admin = login(client, "boss@acme.example").json()
    assert {c["slug"] for c in admin["clients"]} == {"northwind", "brightwater"}
    assert client.get("/api/team/me", headers=auth(token)).json()["user"]["role"] == "consultant"
    assert client.post("/api/team/logout", headers=auth(token)).status_code == 200
    assert client.get("/api/team/me", headers=auth(token)).status_code == 401
    kinds = [k for _, k, _ in app.state.repo.events]
    assert kinds.count("staff_login_failed") == 2 and kinds.count("staff_login") == 2


def test_disabled_user_and_password_change_end_sessions(make_client):
    client, app, _ = make_client()
    user = add_staff(app, "sam@acme.example")
    t1 = login(client, "sam@acme.example").json()["token"]
    asyncio.run(app.state.staff.set_password(user.id, hash_password("another long password")))
    assert client.get("/api/team/me", headers=auth(t1)).status_code == 401
    t2 = login(client, "sam@acme.example", "another long password").json()["token"]
    asyncio.run(app.state.staff.set_active(user.id, False))
    assert client.get("/api/team/me", headers=auth(t2)).status_code == 401
    assert login(client, "sam@acme.example", "another long password").status_code == 401


def test_login_is_rate_limited(make_client):
    client, app, _ = make_client()
    add_staff(app, "sam@acme.example")
    codes = [login(client, "sam@acme.example", "wrong password!!").status_code for _ in range(9)]
    assert codes[:8] == [401] * 8 and codes[8] == 429


# ---------------------------------------------------------------- the team assistant
def ask(client, token, thread, text, mid="team-0001", focus=""):
    r = client.post(f"/api/team/threads/{thread}/messages", headers=auth(token),
                    json={"client_message_id": mid, "text": text, "focus_client": focus})
    assert r.status_code == 200, r.text
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def test_team_answer_cites_sources_and_threads_are_private(make_client):
    client, app, llm = make_client([
        ["Start DMARC at p=none, then quarantine after two weeks [S1][S2][S3][S4][S5]."],
    ])
    setup_clients(app)
    add_staff(app, "sam@acme.example", grant=["brightwater"])
    add_staff(app, "kim@acme.example")
    token = login(client, "sam@acme.example").json()["token"]
    thread = client.post("/api/team/threads", headers=auth(token)).json()["id"]
    events = ask(client, token, thread, "What's our DMARC approach in M365 migrations?")
    sources = next(e for e in events if e["type"] == "sources")["sources"]
    assert sources and sources[0]["n"] == 1
    assert any(s["path"] == "technical-m365-migration-runbook.md" for s in sources)
    assert events[-1]["type"] == "done"
    # the search ran before the model's first call, without the model asking for it
    first = llm.calls[0]["messages"]
    assert first[-1].role == "tool" and first[-1].tool_name == "search_internal" and "[S1]" in first[-1].text
    # the model ran with the stronger drafting settings and saw the user's scope
    assert "Brightwater Dental" in llm.calls[0]["context"] and "Northwind" not in llm.calls[0]["context"]
    # transcript comes back with its sources; another consultant cannot open it
    msgs = client.get(f"/api/team/threads/{thread}/messages", headers=auth(token)).json()["messages"]
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["sources"] == sources
    other = login(client, "kim@acme.example").json()["token"]
    assert client.get(f"/api/team/threads/{thread}/messages", headers=auth(other)).status_code == 404
    assert client.get("/api/team/threads", headers=auth(other)).json()["threads"] == []
    assert client.get("/api/team/threads", headers=auth(token)).json()["threads"][0]["id"] == thread
    # a public website conversation is not a team thread
    cid, _, _ = start(client)
    assert client.get(f"/api/team/threads/{cid}/messages", headers=auth(token)).status_code == 404


def test_consultant_never_gets_another_clients_documents(make_client):
    client, app, llm = make_client([
        [ToolCall("s", "search_internal", {"query": "Northwind account notes scanners SMTP CFO"})], ["n/a"],
        [ToolCall("s", "search_internal", {"query": "account notes", "client": "northwind"})], ["n/a"],
        [ToolCall("o", "client_overview", {"client": "Northwind Traders"})], ["n/a"],
        [ToolCall("s", "search_internal", {"query": "Brightwater SMTP relay reminders"})], ["n/a"],
    ])
    setup_clients(app)
    add_staff(app, "sam@acme.example", grant=["brightwater"])
    token = login(client, "sam@acme.example").json()["token"]
    thread = client.post("/api/team/threads", headers=auth(token)).json()["id"]
    results = []
    for i, q in enumerate(["northwind notes", "northwind only", "northwind overview", "brightwater relay"]):
        ask(client, token, thread, q, f"team-000{i}")
        results.append(llm.calls[-1]["messages"][-1].text)
    assert "Northwind Traders account notes" not in results[0] and "CFO" not in results[0]
    assert results[1].startswith("No access") and results[2].startswith("No access")
    assert "Brightwater Dental account notes" in results[3]
    audit = [d for _, k, d in app.state.repo.events if k == "internal_search"]
    assert all("internal/clients/northwind-account-notes" not in d["sources"] for d in audit)


def test_admin_sees_every_client_and_public_chat_sees_no_internal_material(make_client):
    client, app, llm = make_client([
        [ToolCall("s", "search_internal", {"query": "warehouse scanners legacy SMTP", "client": "northwind"})],
        ["The scanners use legacy SMTP auth [S1]."],
        [ToolCall("k", "search_knowledge", {"query": "warehouse scanners legacy SMTP Northwind CFO pricing day rate"})],
        ["I don't have that."],
    ])
    setup_clients(app)
    add_staff(app, "boss@acme.example", role="admin")
    token = login(client, "boss@acme.example").json()["token"]
    thread = client.post("/api/team/threads", headers=auth(token)).json()["id"]
    ask(client, token, thread, "What's the Northwind scanner issue?")
    assert "Northwind Traders account notes" in llm.calls[1]["messages"][-1].text
    cid, headers, _ = start(client)
    say(client, cid, headers, "Tell me about Northwind's scanners")
    public = llm.calls[-1]["messages"][-1].text
    assert "Northwind" not in public and "850" not in public and "Pricing guidance" not in public


def test_team_message_limits(make_client):
    client, app, _ = make_client([["ok"]])
    add_staff(app, "sam@acme.example")
    token = login(client, "sam@acme.example").json()["token"]
    thread = client.post("/api/team/threads", headers=auth(token)).json()["id"]
    r = client.post(f"/api/team/threads/{thread}/messages", headers=auth(token),
                    json={"client_message_id": "team-0001", "text": "x" * 12001})
    assert r.status_code == 413
    first = ask(client, token, thread, "hello", "team-0002")
    again = ask(client, token, thread, "hello", "team-0002")              # a retry replays, no second answer
    assert again[-1].get("replayed") and "".join(e.get("text", "") for e in first if e["type"] == "delta") == "ok"


# ---------------------------------------------------------------- Jira wire format
def jira(handler):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)
    http = httpx.AsyncClient(base_url="https://acme.atlassian.net", transport=httpx.MockTransport(record))
    return JiraTicketing("https://acme.atlassian.net", "me@acme.example", "tok", "SUP", client=http), seen


INFO = TicketInfo(reference="TK-ABC234", client_name="Northwind Traders", client_slug="northwind",
                  contact_email="jo@northwind.example", summary="VPN down", details="All users\nsince 9am",
                  priority="urgent", category="outage")


def test_jira_create_labels_and_retry_safety():
    def handler(r):
        if r.url.path == "/rest/api/3/search/jql":
            return httpx.Response(200, json={"issues": []})
        return httpx.Response(201, json={"id": "1", "key": "SUP-7"})
    j, seen = jira(handler)
    key, url = asyncio.run(j.create(INFO))
    assert (key, url) == ("SUP-7", "https://acme.atlassian.net/browse/SUP-7")
    search = json.loads(seen[0].content)
    assert search["jql"] == 'project = "SUP" AND labels = "assistant-tk-abc234"'
    fields = json.loads(seen[1].content)["fields"]
    assert fields["project"] == {"key": "SUP"} and fields["summary"] == "[Urgent] Northwind Traders: VPN down"
    assert fields["labels"] == ["assistant-tk-abc234", "client-northwind", "priority-urgent", "category-outage"]
    assert fields["priority"] == {"name": "Highest"}
    texts = [p["content"][0]["text"] for p in fields["description"]["content"]]
    assert "All users" in texts and "since 9am" in texts and fields["description"]["type"] == "doc"

    # an earlier attempt already created it: found by label, not created again
    j2, seen2 = jira(lambda r: httpx.Response(200, json={"issues": [{"key": "SUP-7"}]}))
    assert asyncio.run(j2.create(INFO))[0] == "SUP-7" and len(seen2) == 1


def test_jira_comment_once_and_status_mapping():
    comments: list = []

    def handler(r):
        if r.url.path.endswith("/comment") and r.method == "GET":
            return httpx.Response(200, json={"comments": comments})
        if r.url.path.endswith("/comment"):
            comments.append(json.loads(r.content))
            return httpx.Response(201, json={"id": "c1"})
        if r.url.path == "/rest/api/3/issue/SUP-404":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"fields": {
            "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            "assignee": {"displayName": "Sam Patel"}, "updated": "2030-01-07T10:00:00.000+0000"}})
    j, _ = jira(handler)
    asyncio.run(j.comment("SUP-7", "Update from jo: more info", "upd-1"))
    asyncio.run(j.comment("SUP-7", "Update from jo: more info", "upd-1"))
    assert len(comments) == 1
    st = asyncio.run(j.state("SUP-7"))
    assert (st.status, st.stage, st.assignee) == ("In Progress", "in_progress", "Sam Patel")
    assert asyncio.run(j.state("SUP-404")) is None


def test_naming_an_inaccessible_client_is_flagged_in_code_and_uncited_sources_are_not_shown(make_client):
    client, app, llm = make_client([
        [ToolCall("s", "search_internal", {"query": "scanners"})],
        ["You don't have access to Northwind Traders; ask an admin."],
    ])
    setup_clients(app)
    add_staff(app, "sam@acme.example", grant=["brightwater"])
    token = login(client, "sam@acme.example").json()["token"]
    thread = client.post("/api/team/threads", headers=auth(token)).json()["id"]
    events = ask(client, token, thread, "What do the Northwind notes say about their scanners?")
    assert "NO access to: Northwind Traders" in llm.calls[0]["context"]
    assert next(e for e in events if e["type"] == "sources")["sources"] == []     # nothing cited
    assert any(k == "internal_access_denied" for _, k, _ in app.state.repo.events)
    # an admin is never flagged
    add_staff(app, "boss@acme.example", role="admin")
    llm.rounds += [["ok"]]
    boss = login(client, "boss@acme.example").json()["token"]
    t2 = client.post("/api/team/threads", headers=auth(boss)).json()["id"]
    ask(client, boss, t2, "Northwind scanners?")
    assert "NO access" not in llm.calls[-1]["context"]


def test_citation_variants_some_models_write_are_recognised():
    from app.team.agent import only_cited
    found = [{"n": 1, "path": "a.md"}, {"n": 2, "path": "b.md"}, {"n": 3, "path": "c.md"}]
    assert only_cited(found, "per the runbook【S1†L1-L4】 and notes 【S3】") == [found[0], found[2]]
    assert only_cited(found, "see [S2†L3]") == [found[1]]
    assert only_cited(found, "no citations here") == []


def test_jira_project_without_priority_field_still_gets_the_issue():
    posts: list = []

    def handler(r):
        if r.url.path == "/rest/api/3/search/jql":
            return httpx.Response(200, json={"issues": []})
        body = json.loads(r.content)["fields"]
        posts.append(body)
        if "priority" in body:
            return httpx.Response(400, json={"errors": {"priority": "Field 'priority' cannot be set."}})
        return httpx.Response(201, json={"key": "SUP-8"})
    j, _ = jira(handler)
    assert asyncio.run(j.create(INFO))[0] == "SUP-8"
    assert "priority" in posts[0] and "priority" not in posts[1]


def test_status_question_always_reads_the_live_status(make_client):
    client, app, llm = make_client([["Welcome back."]])
    made = setup_clients(app)
    from app.clients.store import Ticket
    t = asyncio.run(app.state.clients.create_ticket(Ticket(
        id="", reference="", client_id=made["northwind"].id, contact_email="jo@northwind.example",
        summary="Shared calendars")))
    asyncio.run(app.state.clients.update_ticket(t.id, external_key="SUP-4"))
    app.state.fakes.ticketing.states["SUP-4"] = TicketState("To Do", "open")
    cid, headers = verified_chat(client, app, [["It's To Do. I'll let you know when it moves."]])
    reply, events = say(client, cid, headers, "What's the status of my ticket?", "msg-00004")
    assert "To Do" in llm.calls[-1]["messages"][-1].text                  # looked up without being asked
    assert "let you know" not in reply and "ask me here any time" in reply
    # a consultant picks it up in Jira; the next question sees it, whatever was said before
    app.state.fakes.ticketing.states["SUP-4"] = TicketState("In Progress", "in_progress", "Hiba Jamal")
    llm.rounds += [["It's In Progress with Hiba Jamal."]]
    say(client, cid, headers, "Any update on my ticket?", "msg-00005")
    assert "In Progress, with Hiba Jamal" in llm.calls[-1]["messages"][-1].text
