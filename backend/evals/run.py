"""Run the behaviour checks in cases.yaml against the configured model (uses the keys in .env).

    python -m evals.run            all cases
    python -m evals.run -k price   only cases whose name contains "price"

Writes evals/report.md and exits non-zero if any case fails.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.assistant.guard import PRICE_RE, PROMISE_RE
from app.clients.store import Client, ClientStore, MemoryClientStore
from app.config import CONTENT_DIR, Settings, get_settings
from app.db.repo import MemoryRepo
from app.kb.content import load_rules, sync_content
from app.kb.embeddings import build_embedder
from app.llm.base import Event, LLMProvider, Message, RateLimited, ToolSpec
from app.llm.registry import build_llm
from app.main import make_assistant, make_team_assistant, start_services
from app.shared.store import MemoryStore
from app.staff.store import StaffUser
from app.team.agent import CITE
from app.voice.store import MemoryCallStore
from scripts.seed_demo import seed

HERE = Path(__file__).parent
# Free plans limit tokens per minute (Groq: about two chat turns a minute with the full instructions and
# tools). A rate-limited case waits, longer each time, and runs again.
ATTEMPTS = 6
MAX_WAIT_S = 65.0
PAUSE_S = 6.0         # between cases


class DailyLimit(Exception):
    """The provider's daily quota is used up: stop the run instead of retrying for hours."""


class PatientLLM(LLMProvider):
    """Waits out a free-plan rate limit on the one model call that was refused and carries on, instead of
    restarting the whole conversation (long cases use more tokens than Groq allows per minute)."""

    def __init__(self, inner: LLMProvider, attempts: int = 10):
        self.inner, self.attempts = inner, attempts
        self.name, self.model = inner.name, inner.model

    async def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int,
                     context: str = "") -> AsyncIterator[Event]:
        for attempt in range(1, self.attempts + 1):
            started = False
            try:
                async for event in self.inner.stream(system=system, messages=messages, tools=tools,
                                                     max_tokens=max_tokens, context=context):
                    started = True
                    yield event
                return
            except RateLimited as exc:
                if exc.daily or exc.retry_after > 15 * 60:
                    raise DailyLimit(str(exc)) from exc
                if started or attempt == self.attempts:
                    raise
                wait = min(max(exc.retry_after, 10.0) + 5.0 * attempt, MAX_WAIT_S)
                print(f"      rate limit: waiting {wait:.0f} s, then continuing")
                await asyncio.sleep(wait)

    async def aclose(self) -> None:
        await self.inner.aclose()


def offline(s: Settings) -> Settings:
    """Evals talk to the real model only: no CRM, calendar, email or Slack, and jobs are not run."""
    return s.model_copy(update={"job_runner": "manual", "crm_provider": "none", "calendar_provider": "local",
                                "email_provider": "none", "slack_bot_token": "",
                                "ticketing_provider": "none"})


async def run_case(case: dict, llm, embedder, rules) -> tuple[bool, list[str], str]:
    """Returns (passed, problems or notes, last reply)."""
    repo = MemoryRepo()
    clients = MemoryClientStore()
    made = await seed(clients)               # Northwind Traders (enterprise), Brightwater Dental (SME)
    await sync_content(repo, embedder, CONTENT_DIR, {slug: c.id for slug, c in made.items()})
    s = offline(get_settings())
    state = SimpleNamespace(repo=repo, rules=rules, llm=llm, embedder=embedder, store=MemoryStore(), clients=clients,
                            calls=MemoryCallStore())
    await start_services(state, s)
    if case.get("team"):
        return await run_team_case(case, state, made)
    assistant = make_assistant(state, llm, s)
    voice = bool(case.get("voice"))
    conv = await repo.create_conversation("voice" if voice else "web", "eval", "", case.get("timezone", ""))
    call_info = {"caller_number": "+447700900123", "transfer_possible": True,
                 "note": "Caller's phone number: +447700900123."} if voice else None
    if case.get("client"):                   # a conversation already verified as this client
        await repo.bind_client(conv.id, made[case["client"]].id, "it.lead@northwind.example")
    reply = ""
    for i, text in enumerate(case["say"]):
        uid, _ = await repo.add_user_message(conv.id, text, f"eval-{i:04d}")
        current = await repo.get_conversation(conv.id)
        assert current is not None
        reply = "".join([e["text"] async for e in assistant.run_turn(current, text, uid, call_info=call_info)
                         if e["type"] == "delta"])
    tools = {c.name for m in repo.messages[conv.id] for c in m.tool_calls}
    guard_hits = [d["reason"] for _, k, d in repo.events if k == "guard"]
    problems: list[str] = []
    # models often write don\u2019t and DC\u2011ABC123 (curly quote, non-breaking hyphen)
    low = reply.lower().replace("\u2019", "'").replace("\u2018", "'")
    low = re.sub("[\u2010-\u2014\u2212]", "-", low)
    for t in case.get("tools", []):
        if t not in tools:
            problems.append(f"did not call {t}")
    if case.get("any") and not any(w.lower() in low for w in case["any"]):
        problems.append(f"reply has none of {case['any']}")
    for w in case.get("none", []):
        if w.lower() in low:
            problems.append(f"reply contains '{w}'")
    # The guard stepping in still passes (the visitor never saw it) but is noted in the report.
    if case.get("no_price") and PRICE_RE.search(reply):
        problems.append("a price reached the visitor")
    if case.get("no_promise") and PROMISE_RE.search(reply):
        problems.append("a promise reached the visitor")
    notes = [f"guard replaced a {r}" for r in guard_hits]
    if case.get("max_words") and len(reply.split()) > case["max_words"]:
        problems.append(f"reply has {len(reply.split())} words, more than {case['max_words']} for a call")
    if case.get("plain") and re.search(r"\*\*|^\s*[-*\u2022]\s|^#|\]\(", reply, re.M):
        problems.append("reply has Markdown (bold, list, heading or link), which a voice would read out")
    if case.get("handover"):
        got = repo.escalations.get(conv.id, ("none", ""))[0]
        if got != case["handover"]:
            problems.append(f"handover {got}, expected {case['handover']}")
    if case.get("lead"):
        lead = repo.leads.get(conv.id)
        missing = [f for f in case["lead"] if not (lead and getattr(lead, f))]
        if missing:
            problems.append(f"lead missing {missing}")
    if case.get("ticket"):
        tickets = await clients.tickets(made[case["client"]].id)
        if not tickets:
            problems.append("no ticket raised")
        elif tickets[0].priority not in case["ticket"]:
            problems.append(f"ticket priority {tickets[0].priority}, expected one of {case['ticket']}")
    booking = await state.booking.store.active_for_conversation(conv.id)
    if case.get("booked") and not booking:
        problems.append("no call booked")
    if case.get("booked_on") and booking:
        day = booking.start_at.astimezone(state.booking_cfg.tz).strftime("%A")
        if day != case["booked_on"]:
            problems.append(f"call is on {day}, expected {case['booked_on']}")
    return not problems, problems + notes if problems else notes, reply


async def run_team_case(case: dict, state, made: dict[str, Client]) -> tuple[bool, list[str], str]:
    """The internal assistant, signed in as an admin or as a consultant with access to some clients."""
    clients: ClientStore = state.clients
    admin = case["team"] == "admin"
    user = StaffUser("eval-user", "eval@acme.example", "Eval Consultant", "admin" if admin else "consultant")
    allowed = await clients.list_clients(None if admin else [made[g].id for g in case.get("grant", [])])
    assistant = make_team_assistant(state, state.llm)
    conv = await state.repo.create_conversation("team", "", "")
    reply, sources = "", []
    for i, text in enumerate(case["say"]):
        uid, _ = await state.repo.add_user_message(conv.id, text, f"eval-{i:04d}")
        reply = ""
        async for e in assistant.run_turn(conv.id, user, allowed, text, uid, case.get("focus", "")):
            if e["type"] == "delta":
                reply += e["text"]
            elif e["type"] == "sources":
                sources = [x["path"] for x in e["sources"]]
    tools = {c.name for m in state.repo.messages[conv.id] for c in m.tool_calls}
    searched = [p for _, k, d in state.repo.events if k == "internal_search" for p in d["sources"]]
    low = reply.lower()
    problems = [f"did not call {t}" for t in case.get("tools", []) if t not in tools]
    if case.get("any") and not any(w.lower() in low for w in case["any"]):
        problems.append(f"reply has none of {case['any']}")
    problems += [f"reply contains '{w}'" for w in case.get("none", []) if w.lower() in low]
    problems += [f"source {p} not cited" for p in case.get("sources", []) if p not in sources]
    problems += [f"saw {p}" for p in case.get("never_sees", []) if any(p in x for x in searched)]
    if case.get("cites") and not CITE.search(reply):
        problems.append("no [S1]-style source reference in the reply")
    return not problems, problems, reply


async def main(filter_: str) -> int:
    logging.getLogger().setLevel(logging.WARNING)   # only the results, not every HTTP call
    s = get_settings()
    llm = PatientLLM(build_llm(s))
    embedder = build_embedder(s)
    rules = load_rules(CONTENT_DIR)
    cases = [c for c in yaml.safe_load((HERE / "cases.yaml").read_text()) if filter_ in c["name"]]
    rows, passed = [], 0
    print(f"Model: {llm.name} {llm.model}, {len(cases)} cases")
    for case in cases:
        t0 = time.monotonic()
        for attempt in range(1, ATTEMPTS + 1):
            try:
                ok, problems, reply = await run_case(case, llm, embedder, rules)
                break
            except DailyLimit as exc:
                print(f"\nStopped: {exc}\nThe free plan's daily quota is used up. Run the checks again tomorrow, "
                      "or set another provider (e.g. LLM_PROVIDER=anthropic) in .env.")
                await llm.aclose()
                return 2
            except RateLimited as exc:
                ok, problems, reply = False, [f"error: {exc}"], ""
                if attempt == ATTEMPTS:
                    break
                wait = min(max(exc.retry_after, 15.0 * attempt), MAX_WAIT_S)
                print(f"      rate limit: waiting {wait:.0f} s, then running '{case['name']}' again")
                await asyncio.sleep(wait)
            except Exception as exc:
                ok, problems, reply = False, [f"error: {exc}"], ""
                break
        passed += ok
        secs = time.monotonic() - t0
        detail = f"  -> {'; '.join(problems)}" if problems else ""
        print(f"{'PASS' if ok else 'FAIL'}  {case['name']}  ({secs:.1f} s){detail}")
        if not ok and reply:
            print(f"      reply: {re.sub(r'\s+', ' ', reply)[:240]}")
        rows.append(f"| {'pass' if ok else '**fail**'} | {case['name']} | {'; '.join(problems)} | "
                    f"{re.sub(r'\s+', ' ', reply)[:300].replace('|', '/')} |")
        await asyncio.sleep(PAUSE_S)   # stay under free-plan limits
    await llm.aclose()
    report = (f"# Behaviour checks\n\nModel: {llm.name} {llm.model}. Passed {passed} of {len(cases)}.\n\n"
              "| Result | Case | Problems | Last reply |\n|---|---|---|---|\n" + "\n".join(rows) + "\n")
    (HERE / "report.md").write_text(report)
    print(f"\nPassed {passed} of {len(cases)}. Report: evals/report.md")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only cases whose name contains this text")
    sys.exit(asyncio.run(main(ap.parse_args().k)))
