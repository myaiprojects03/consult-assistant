"""One visitor message -> one streamed, checked reply, with tool calls in between.

Flow per turn:
1. Rules in code: a stated company size is saved to the lead (so fit is always known), and
   handover rules: a message about a security incident, a price negotiation, a legal matter or a
   request for a person is handed to a consultant before the model runs.
2. The model gets the fixed instructions, the per-turn context and the recent conversation.
3. Text streams through the output guard (no prices, no promises); tool calls run between rounds.
4. The whole turn is stored once it finishes, so a retried message is answered from storage.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.assistant.guard import StreamGuard
from app.assistant.prompts import system_prompt, turn_context, voice_prompt
from app.assistant.tools import (
    ToolContext,
    after_handover,
    after_lead_saved,
    assess,
    run_tool,
    size_from_text,
    tools_for,
)
from app.booking.service import BookingService
from app.booking.slots import describe, zone
from app.booking.team import Team
from app.clients.store import ClientStore
from app.clients.tickets import TicketService
from app.clients.verify import Verifier
from app.db.repo import Conversation, Lead, Repo, StoredMessage
from app.jobs.runner import Jobs
from app.kb.content import Rules
from app.kb.embeddings import Embedder
from app.llm.base import Done, LLMProvider, Message, TextDelta, ToolCall, ToolCallEvent
from app.voice.rules import VoiceRules

log = logging.getLogger(__name__)
VOICE_HISTORY_MESSAGES = 14      # spoken turns are short; a smaller window keeps each call request small
STATUS_QUESTION = re.compile(r"\b(?:status|update|updates|progress|news|where are we|how is|how's|any word)\b"
                             r"|\bTK-", re.IGNORECASE)


def triggered_topic(text: str, rules: Rules) -> str | None:
    lowered = text.lower()
    for reason, phrases in rules.escalate_topics.items():
        for p in phrases:
            if re.search(rf"(?<![a-z]){re.escape(p.lower())}(?![a-z])", lowered):
                return reason
    return None


def history_window(stored: list[StoredMessage]) -> list[Message]:
    """Recent messages, starting at a visitor message so no tool result is cut off from its call."""
    start = next((i for i, m in enumerate(stored) if m.role == "user"), len(stored))
    return [m.to_message() for m in stored[start:]]


SENTENCE_GLUE = re.compile(r"[.!?]$")


def spaced(before: str, text: str) -> str:
    """Some models (gpt-oss on Groq) send two sentences with nothing between them ("email.I'm waiting").
    A new piece of text that starts a sentence right after a full stop gets a space."""
    if before and SENTENCE_GLUE.search(before) and text[:1].isupper():
        return " " + text
    return text


@dataclass
class Services:
    """What the tools act on besides storage: bookings, background jobs and the team's routing."""
    booking: BookingService
    jobs: Jobs
    team: Team
    clients: ClientStore | None = None
    verifier: Verifier | None = None
    tickets: TicketService | None = None


@dataclass
class Assistant:
    repo: Repo
    llm: LLMProvider
    embedder: Embedder
    rules: Rules
    services: Services
    max_tokens: int = 700
    history_messages: int = 24
    max_tool_rounds: int = 4
    now: datetime | None = None          # fixed in tests and evals; the real clock otherwise
    voice_max_tokens: int = 300

    def __post_init__(self) -> None:
        self.system = system_prompt(self.rules)
        self.voice_system = voice_prompt(self.rules)
        self.voice_rules = VoiceRules.from_rules(self.rules.voice)

    async def run_turn(self, conv: Conversation, user_text: str, user_message_id: int, *,
                       call_info: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        """call_info (voice only): {"caller_number", "transfer_possible", "note"} from the call session."""
        started = time.monotonic()
        triggered = triggered_topic(user_text, self.rules)
        if triggered:
            await self.repo.escalate(conv.id, triggered, f"Visitor wrote: {user_text[:280]}")
            await self.repo.log_event(conv.id, "handover", {"reason": triggered, "by": "rule"})
            await after_handover(self.services.jobs, conv.id, triggered)
            yield {"type": "handover", "reason": triggered}

        svc = self.services
        voice = conv.channel == "voice"
        tool_ctx = ToolContext(self.repo, self.embedder, self.rules, conv.id, svc.booking, svc.jobs, svc.team,
                               conv.visitor_timezone, self.now, svc.clients, svc.verifier, svc.tickets,
                               conv.client_id, conv.verified_email, channel=conv.channel,
                               caller_number=(call_info or {}).get("caller_number", ""),
                               transfer_possible=bool((call_info or {}).get("transfer_possible")),
                               voice_rules=self.voice_rules)
        client = await svc.clients.get_client(conv.client_id) if conv.client_id and svc.clients else None
        client_text = ""
        if client:
            manager = svc.team.get(client.account_manager).name if client.account_manager else "our team"
            client_text = (f"ALREADY VERIFIED existing client: {conv.verified_email} of {client.name} ({client.tier}); "
                           f"their consultant is {manager}. Do not ask for their email or a code again: use "
                           "client_overview, raise_ticket and update_ticket directly. When they describe a problem "
                           "or ask for a ticket, call raise_ticket in this reply.")
        lead = await self.repo.get_lead(conv.id)
        size = size_from_text(user_text)
        if size and not (lead and lead.company_size):   # fit must not depend on the model remembering to save it
            lead = lead or Lead(conv.id)
            before = lead.status
            lead.company_size = size
            await self.repo.save_lead(assess(lead, self.rules))
            await self.repo.log_event(conv.id, "lead_updated",
                                      {"fields": ["company_size"], "status": lead.status, "by": "rule"})
            await after_lead_saved(tool_ctx, before, lead)
        booked = await svc.booking.store.active_for_conversation(conv.id)
        booked_text = ""
        if booked:
            vt = zone(conv.visitor_timezone, svc.booking.cfg.timezone)
            booked_text = (f"{describe(booked.start_at, booked.end_at, vt)} with "
                           f"{svc.team.get(booked.consultant_id).name}, reference {booked.reference}")
        context = turn_context(lead, self.rules, conv.escalation_reason, triggered, now=self.now,
                               office_tz=svc.booking.cfg.timezone, visitor_tz=conv.visitor_timezone,
                               booking=booked_text, client=client_text)
        if voice and call_info and call_info.get("note"):
            context += "\n" + str(call_info["note"])
        history = (min(self.history_messages, VOICE_HISTORY_MESSAGES) if conv.channel == "voice"
                   else self.history_messages)
        messages = history_window(await self.repo.recent_messages(conv.id, history))
        guard = StreamGuard(self.rules.allowed_price_figures)
        turn: list[Message] = []
        first_text_ms: int | None = None
        usage = {"input": 0, "output": 0}

        # A verified client asking about status always gets a fresh lookup (live from Jira) before the model
        # answers; otherwise it may repeat a status from earlier in the chat that has since changed.
        if tool_ctx.client_id and STATUS_QUESTION.search(user_text):
            call = ToolCall("auto-status", "client_overview", {})
            yield {"type": "tool", "name": call.name}
            result = await run_tool(tool_ctx, call.name, call.args)
            turn += [Message("assistant", "", tool_calls=[call]),
                     Message("tool", result, tool_call_id=call.id, tool_name=call.name)]

        emitted = ""        # the model's last characters this turn, to keep sentences apart (see spaced)
        for round_no in range(self.max_tool_rounds + 1):
            last_round = round_no == self.max_tool_rounds
            text_parts: list[str] = []
            calls = []
            async for ev in self.llm.stream(system=self.voice_system if voice else self.system, context=context,
                                            messages=messages + turn,
                                            tools=[] if last_round else tools_for(tool_ctx.client_id is not None,
                                                                                  voice),
                                            max_tokens=self.voice_max_tokens if voice else self.max_tokens):
                if isinstance(ev, TextDelta):
                    piece = spaced(emitted, ev.text)
                    emitted = (emitted + piece)[-2:]
                    safe = guard.feed(piece)
                    if safe:
                        text_parts.append(safe)
                        if first_text_ms is None:
                            first_text_ms = int((time.monotonic() - started) * 1000)
                        yield {"type": "delta", "text": safe}
                elif isinstance(ev, ToolCallEvent):
                    calls.append(ev.call)
                elif isinstance(ev, Done):
                    usage["input"] += ev.input_tokens
                    usage["output"] += ev.output_tokens
            tail = guard.flush()
            if tail:
                text_parts.append(tail)
                yield {"type": "delta", "text": tail}
            turn.append(Message("assistant", "".join(text_parts), tool_calls=calls))
            if not calls:
                break
            for call in calls:
                yield {"type": "tool", "name": call.name}
                result = await run_tool(tool_ctx, call.name, call.args)
                turn.append(Message("tool", result, tool_call_id=call.id, tool_name=call.name))
            # Text before a tool call and after it are separate paragraphs.
            if text_parts:
                emitted = "\n\n"
                yield {"type": "delta", "text": "\n\n"}

        if not turn[-1].text.strip():   # the model ended without words: never leave the visitor hanging
            fallback = ("Sorry, I didn't catch that. Could you rephrase, or would you like me to connect you "
                        "with a consultant?")
            turn[-1].text = fallback
            yield {"type": "delta", "text": fallback}

        for v in guard.blocked:
            await self.repo.log_event(conv.id, "guard", {"reason": v.reason, "found": v.found})
        reply_id = await self.repo.save_turn(conv.id, user_message_id, turn)
        await self.repo.log_event(conv.id, "turn", {
            "model": self.llm.model, "ms_total": int((time.monotonic() - started) * 1000),
            "ms_first_text": first_text_ms, "rounds": round_no + 1,
            "tools": [c.name for m in turn for c in m.tool_calls], **usage})
        if tool_ctx.transfer:
            yield {"type": "transfer", **tool_ctx.transfer}
        yield {"type": "done", "message_id": reply_id}
