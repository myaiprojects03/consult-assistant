"""The internal knowledge assistant: a private chat for consultants.

It answers from internal documents (content/internal/) with numbered source references, looks up a
client's current state, and drafts proposals, meeting summaries and follow-up emails. What it can see is
decided here, from the signed-in user's access list, never by the model: a consultant only gets
documents and data of the clients they are assigned to; admins see all clients. Every search and every
answer is logged with the user and the sources used.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.assistant.agent import history_window
from app.booking.team import Team
from app.clients.store import Client, ClientStore
from app.db.repo import KBHit, Repo, StoredMessage
from app.kb.content import Rules
from app.kb.embeddings import Embedder
from app.llm.base import Done, LLMProvider, Message, TextDelta, ToolCall, ToolCallEvent, ToolSpec
from app.staff.store import StaffUser

log = logging.getLogger(__name__)
SOURCE_LINE = re.compile(r"^\[S(\d+)\] (.+?) \| (.+?) \| (.*?) \| (.+)$", re.M)

TEAM_PROMPT = """You are the internal knowledge assistant for the consultants of {firm}, an IT consulting firm.

# How to answer
- Each question starts with a search_internal result for it. Answer from it, and call search_internal
  again with a sharper query if it does not cover the question. This includes every draft.
- For a client's current state (projects, milestones, tickets, recent website conversations) also call
  client_overview.
- Cite what you used inline as [S1], [S2] (exactly this form: square brackets, no line numbers), using the
  numbers search_internal gives. In a draft meant for a client, put the citations in a short note after
  the draft, not inside the text the client will read. Never cite a number
  you were not given, and never invent document names. Facts from client_overview need no number.
- If the material does not cover the question, say so plainly; you may add general professional
  guidance, clearly labelled as general guidance, not firm material.

# Never invent
Use only facts from the tool results and from the user's message. Do not make up dates, deadlines, owners,
names, phone numbers, email addresses, prices, durations or results. Where a draft needs one you do not
have, write a placeholder in square brackets, e.g. [date to confirm], [time window to confirm].
Sign drafts with the signed-in user's name only, with no contact details.

# Drafting
When asked for a proposal, meeting summary or follow-up email, write a ready-to-edit draft in Markdown,
built from the sources and the facts the user gave. Short and plain; lists rather than tables.

# Confidentiality
The user can see only the clients listed in the context. Never put one client's information into
another client's work. If the context says the user has no access to a client they asked about, answer
only that they do not have access to that client and can ask an admin; do not search for it and do not
say whether documents about it exist.
Do not repeat secrets such as passwords or keys even if a document contains them.

Be concise. Use headings and lists only where they help."""

TEAM_TOOLS = [
    ToolSpec("search_internal",
             "Search the firm's internal documents (proposals, case studies, SOPs, templates, technical notes, "
             "client notes). Returns numbered sources to cite.",
             {"type": "object", "properties": {
                 "query": {"type": "string"},
                 "client": {"type": "string", "description": "optional: a client's name or id from the context, "
                                                             "to search that client's documents only"}},
              "required": ["query"]}),
    ToolSpec("client_overview",
             "A client's projects, recent tickets and recent website conversations.",
             {"type": "object", "properties": {"client": {"type": "string",
                                                          "description": "a client's name or id from the context"}},
              "required": ["client"]}),
]


@dataclass
class Source:
    n: int
    title: str
    kind: str
    client: str
    path: str


@dataclass
class TurnState:
    user: StaffUser
    allowed: list[Client]               # what this user may see (admins: all clients)
    sources: dict[str, Source] = field(default_factory=dict)   # slug -> source, numbered in order


# [S1], and the variants some models write: 【S1】, 【S1†L1-L4】, [S1†L3]
CITE = re.compile(r"[\[【]S(\d+)(?:†[^\]】]*)?[\]】]")


def only_cited(sources: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    """The sources the answer cites. Everything searched is still in the audit log (internal_search)."""
    cited = {int(n) for n in CITE.findall(answer)}
    return [s for s in sources if s["n"] in cited]


def sources_in(stored: list[StoredMessage]) -> list[dict[str, Any]]:
    """Sources of a stored turn, read back from the search tool results."""
    out: dict[int, dict[str, Any]] = {}
    for m in stored:
        if m.role == "tool" and m.tool_name == "search_internal":
            for n, title, kind, client, path in SOURCE_LINE.findall(m.text):
                out[int(n)] = {"n": int(n), "title": title, "kind": kind,
                               "client": "" if client == "general" else client, "path": path}
    answer = " ".join(m.text for m in stored if m.role == "assistant")
    return only_cited([out[k] for k in sorted(out)], answer)


@dataclass
class TeamAssistant:
    repo: Repo
    clients: ClientStore
    llm: LLMProvider
    embedder: Embedder
    rules: Rules
    team: Team
    max_tokens: int = 1800
    history_messages: int = 30
    max_tool_rounds: int = 4

    def __post_init__(self) -> None:
        self.system = TEAM_PROMPT.format(firm=self.rules.firm_name)

    def _match(self, state: TurnState, text: str) -> Client | None:
        t = text.strip().lower()
        return next((c for c in state.allowed if t in (c.id.lower(), c.slug.lower(), c.name.lower())), None)

    async def _search(self, state: TurnState, args: dict[str, Any], conv_id: str) -> str:
        query = str(args.get("query", "")).strip()[:500]
        if not query:
            return "Error: query is empty."
        allowed_ids = [c.id for c in state.allowed]
        scope: list[str] | None = None if state.user.is_admin else allowed_ids
        named: Client | None = None
        if args.get("client"):
            named = self._match(state, str(args["client"]))
            if named is None:
                return "No access to that client (or no such client). Search without a client, or ask an admin."
            scope = [named.id]
        vec = await self.embedder.embed([query])
        hits: list[KBHit] = await self.repo.kb_search(query=query, embedding=vec[0] if vec else None,
                                                      audience="internal", k=5, clients=scope)
        if named:   # only that client's documents when one was named
            hits = [h for h in hits if h.client_id == named.id]
        names = {c.id: c.name for c in state.allowed}
        blocks = []
        for h in hits:
            src = state.sources.get(h.slug)
            if src is None:
                src = Source(len(state.sources) + 1, h.title, h.kind, names.get(h.client_id or "", ""),
                             h.slug.removeprefix("internal/") + ".md")
                state.sources[h.slug] = src
            blocks.append(f"[S{src.n}] {src.title} | {src.kind} | {src.client or 'general'} | {src.path}\n"
                          f"{h.content}")
        await self.repo.log_event(conv_id, "internal_search", {"user": state.user.id, "query": query,
                                                               "client": args.get("client", ""),
                                                               "sources": [h.slug for h in hits]})
        return "\n\n".join(blocks) if blocks else "Nothing in the internal material matches this."

    async def _overview(self, state: TurnState, args: dict[str, Any], conv_id: str) -> str:
        client = self._match(state, str(args.get("client", "")))
        if client is None:
            return "No access to that client (or no such client)."
        manager = self.team.get(client.account_manager).name if client.account_manager else "unassigned"
        lines = [f"{client.name} ({client.tier}); account manager: {manager}."]
        for p in await self.clients.projects(client.id):
            lines.append(f"- Project {p.name}: {p.status.replace('_', ' ')}, {p.phase or 'no phase set'}; next: "
                         f"{p.next_milestone or 'none'}" + (f" by {p.milestone_date:%d %b %Y}" if p.milestone_date
                                                            else ""))
        for t in await self.clients.tickets(client.id, limit=8):
            lines.append(f"- Ticket {t.reference}{' / ' + t.external_key if t.external_key else ''} "
                         f"({t.priority}, {t.status.replace('_', ' ')}, {t.created_at:%d %b}): {t.summary}")
        for c in await self.repo.client_conversations(client.id, 5):
            lines.append(f"- Website chat {c['started']} with {c['contact']}"
                         + (f" (handed over: {c['handover']})" if c["handover"] else "")
                         + (f": {c['summary']}" if c["summary"] else ""))
        await self.repo.log_event(conv_id, "internal_client_view", {"user": state.user.id, "client": client.slug})
        return "\n".join(lines)

    def _context(self, state: TurnState, focus: Client | None, now: datetime, blocked: list[Client]) -> str:
        who = f"Signed in: {state.user.name} ({state.user.role}). Today: {now:%A %d %B %Y}."
        names = ", ".join(f"{c.name} (id {c.slug})" for c in state.allowed) or "none"
        scope = "all clients" if state.user.is_admin else "only these clients"
        parts = [who, f"Clients this user may see ({scope}): {names}."]
        if focus:
            parts.append(f"The user is working on {focus.name}: prefer its documents and data.")
        if blocked:
            parts.append("The message names a client this user has NO access to: "
                         + ", ".join(c.name for c in blocked) + ". Say they do not have access to it.")
        return "\n".join(parts)

    async def _blocked_mentions(self, state: TurnState, text: str) -> list[Client]:
        """Clients named in the message that this user may not see (decided here, not by the model)."""
        if state.user.is_admin:
            return []
        allowed = {c.id for c in state.allowed}
        low = text.lower()

        def named(c: Client) -> bool:
            first = c.name.split()[0].lower()
            return any(re.search(rf"\b{re.escape(w)}\b", low) for w in {c.slug.lower(), c.name.lower(), first}
                       if len(w) >= 4)
        return [c for c in await self.clients.list_clients(None) if c.id not in allowed and named(c)]

    async def run_turn(self, conv_id: str, user: StaffUser, allowed: list[Client], text: str,
                       user_message_id: int, focus_client: str = "") -> AsyncIterator[dict[str, Any]]:
        started = time.monotonic()
        state = TurnState(user, allowed)
        focus = self._match(state, focus_client) if focus_client else None
        messages = history_window(await self.repo.recent_messages(conv_id, self.history_messages))
        blocked = await self._blocked_mentions(state, text)
        if blocked:
            await self.repo.log_event(conv_id, "internal_access_denied",
                                      {"user": user.id, "clients": [c.slug for c in blocked]})
        context = self._context(state, focus, datetime.now(UTC), blocked)
        turn: list[Message] = []
        usage = {"input": 0, "output": 0}
        # Retrieval first: the model always starts with the internal documents for this question, instead of
        # relying on it to decide to search (it can still search again with its own query).
        if not blocked and len(text.split()) >= 3:
            query = f"{focus.name} {text}" if focus else text
            call = ToolCall("auto-search", "search_internal", {"query": query[:500]})
            yield {"type": "tool", "name": call.name}
            result = await self._search(state, call.args, conv_id)
            turn += [Message("assistant", "", tool_calls=[call]),
                     Message("tool", result, tool_call_id=call.id, tool_name=call.name)]
        for round_no in range(self.max_tool_rounds + 1):
            last = round_no == self.max_tool_rounds
            parts: list[str] = []
            calls = []
            async for ev in self.llm.stream(system=self.system, context=context, messages=messages + turn,
                                            tools=[] if last else TEAM_TOOLS, max_tokens=self.max_tokens):
                if isinstance(ev, TextDelta):
                    parts.append(ev.text)
                    yield {"type": "delta", "text": ev.text}
                elif isinstance(ev, ToolCallEvent):
                    calls.append(ev.call)
                elif isinstance(ev, Done):
                    usage["input"] += ev.input_tokens
                    usage["output"] += ev.output_tokens
            turn.append(Message("assistant", "".join(parts), tool_calls=calls))
            if not calls:
                break
            for call in calls:
                yield {"type": "tool", "name": call.name}
                try:
                    if call.name == "search_internal":
                        result = await self._search(state, call.args, conv_id)
                    elif call.name == "client_overview":
                        result = await self._overview(state, call.args, conv_id)
                    else:
                        result = f"Error: unknown tool '{call.name}'."
                except Exception:
                    log.exception("team tool %s failed", call.name)
                    result = "Error: this lookup failed. Say so and continue without it."
                turn.append(Message("tool", result, tool_call_id=call.id, tool_name=call.name))
            if parts:
                yield {"type": "delta", "text": "\n\n"}
        if not turn[-1].text.strip():
            turn[-1].text = "I couldn't put an answer together. Could you rephrase the question?"
            yield {"type": "delta", "text": turn[-1].text}
        reply_id = await self.repo.save_turn(conv_id, user_message_id, turn)
        cited = sorted(state.sources.values(), key=lambda s: s.n)
        found = [{"n": s.n, "title": s.title, "kind": s.kind, "client": s.client, "path": s.path} for s in cited]
        sources = only_cited(found, " ".join(m.text for m in turn if m.role == "assistant"))
        await self.repo.log_event(conv_id, "internal_turn", {
            "user": user.id, "model": self.llm.model, "ms_total": int((time.monotonic() - started) * 1000),
            "sources": [s["path"] for s in sources], **usage})
        yield {"type": "sources", "sources": sources}
        yield {"type": "done", "message_id": reply_id}
