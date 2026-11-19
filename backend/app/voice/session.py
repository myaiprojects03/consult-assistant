"""One live call: what the caller says (as text) in, the assistant's reply (as text to speak) out.

The message format is Twilio ConversationRelay's, so the same session serves phone calls (Twilio turns
speech into text and back) and browser calls (the browser does it; frontend/src/lib/voice.ts).

In:   {"type": "prompt", "voicePrompt": "...", "last": true}    what the caller said
      {"type": "interrupt", "utteranceUntilInterrupt": "..."}    the caller talked over the reply
      {"type": "dtmf", "digit": "0"}                              keypad (0 = ask for a person)
Out:  {"type": "text", "token": "...", "last": false|true}       reply to speak, streamed
      {"type": "end", "handoffData": "{...}"}                    finish: transfer to a consultant, or hang up

Rules:
- Turns run one at a time, in order. A reply is never cancelled half way: a tool may be booking a call.
  When the caller interrupts, the rest of that reply is simply not spoken, and the next turn is told
  what they heard.
- Everything goes through the same assistant as the chat (knowledge, guard, tools, logs), with the
  voice instructions and the transfer_call tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.assistant.agent import Assistant
from app.assistant.tools import after_handover, assess
from app.db.repo import Lead, Repo
from app.jobs.runner import Jobs
from app.llm.base import LLMError
from app.voice.speech import speakable
from app.voice.store import Call, CallStore

log = logging.getLogger(__name__)
Send = Callable[[dict[str, Any]], Awaitable[None]]
HOLD_AFTER_S = 4.0              # silence before Ava says "One moment." (e.g. waiting out a rate limit)
SECONDS_PER_WORD = 0.4          # time to speak a reply before a transfer starts (about 150 words a minute)
# When the model cannot answer, the call ends politely instead of repeating an apology every turn.
UNAVAILABLE_PHONE = ("Sorry, I'm having trouble on my side right now. I've asked a consultant to call you back "
                     "on this number as soon as possible. Goodbye.")
UNAVAILABLE_WEB = ("Sorry, I'm having trouble on my side right now. Please try again in a little while, or use the "
                   "chat on our website. Goodbye.")
TIME_LIMIT = "We've reached the time limit for this call. A consultant will follow up with you. Goodbye."


@dataclass
class VoiceSession:
    assistant: Assistant
    repo: Repo
    calls: CallStore
    jobs: Jobs
    call: Call
    conversation_id: str
    send: Send
    transfer_possible: bool = False          # phone calls; a browser call cannot be put through
    speak_delay: Callable[[float], Awaitable[None]] = asyncio.sleep
    queue: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    turn_no: int = 0
    speaking: bool = False
    interrupted: bool = False
    note: str = ""
    ended: bool = False
    outcome: str = ""
    started: float = field(default_factory=time.monotonic)

    # ------------------------------------------------------------ lifecycle
    async def start(self, *, speak_greeting: str = "") -> None:
        """Mark the call live and keep the caller's number on the lead. Browser calls also get the
        greeting from here (Twilio speaks it itself, from the TwiML)."""
        await self.calls.update(self.call.id, status="in_progress", started_at=datetime.now(UTC),
                                conversation_id=self.conversation_id)
        number = self.call.caller_number
        if number:
            lead = await self.repo.get_lead(self.conversation_id) or Lead(self.conversation_id)
            if not lead.phone:
                lead.phone = number
                if self.call.contact_name and not lead.name:
                    lead.name = self.call.contact_name
                await self.repo.save_lead(assess(lead, self.assistant.rules))
        await self.repo.log_event(self.conversation_id, "call_started",
                                  {"call": self.call.id, "direction": self.call.direction})
        if speak_greeting:
            await self.send({"type": "text", "token": speak_greeting, "last": True})

    async def run(self) -> None:
        """Answer turns until the call ends (None in the queue) or the session ends it."""
        while not self.ended:
            text = await self.queue.get()
            if text is None:
                break
            await self._turn(text)

    async def close(self) -> None:
        """The connection is gone: stop, then file the call (summary, CRM) in the background."""
        self.ended = True
        await self.queue.put(None)
        duration = int(time.monotonic() - self.started)
        # Outbound calls get their final status from Twilio's status callback; a transferred call is still
        # going on (the dial-done webhook finishes it). Everything else ends here.
        done = self.call.direction != "outbound" and self.outcome != "transferring"
        await self.calls.update(self.call.id, ended_at=datetime.now(UTC), duration_s=duration,
                                **({"status": "completed"} if done else {}))
        await self.repo.log_event(self.conversation_id, "call_ended", {"call": self.call.id, "seconds": duration})
        if self.turn_no:
            await self.jobs.enqueue("conversation.summarize", f"summary:call:{self.call.id}",
                                    {"conversation_id": self.conversation_id, "purpose": "call"},
                                    ref=self.conversation_id)

    # ------------------------------------------------------------ messages from the caller's side
    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "prompt":
            text = str(msg.get("voicePrompt", "")).strip()
            if text and msg.get("last", True):
                await self.queue.put(text[:2000])
        elif kind == "interrupt":
            if self.speaking:
                self.interrupted = True
            heard = str(msg.get("utteranceUntilInterrupt", "")).strip()[-300:]
            self.note = (f'The caller interrupted your last reply; they only heard: "{heard}".' if heard else
                         "The caller interrupted your last reply before hearing it.")
            await self.repo.log_event(self.conversation_id, "call_interrupted", {"heard": heard})
        elif kind == "dtmf":
            if str(msg.get("digit")) == "0":
                await self.queue.put("I'd like to speak to a person, please.")
        elif kind == "error":
            log.warning("voice provider error on call %s: %s", self.call.id, msg.get("description"))
            await self.repo.log_event(self.conversation_id, "call_error", {"description": str(msg)[:300]})

    # ------------------------------------------------------------ one turn
    async def _say(self, text: str) -> int:
        await self.send({"type": "text", "token": text, "last": True})
        return len(text.split())

    async def _turn(self, text: str) -> None:
        self.turn_no += 1
        conv = await self.repo.get_conversation(self.conversation_id)
        if conv is None:
            return
        uid, _ = await self.repo.add_user_message(conv.id, text, f"call-{self.call.id[:8]}-{self.turn_no:04d}")
        info = {"caller_number": self.call.caller_number, "transfer_possible": self.transfer_possible,
                "note": self._context_note()}
        self.note = ""
        self.interrupted, self.speaking = False, True
        transfer: dict[str, Any] | None = None
        words = 0
        failed = False
        holding = asyncio.create_task(self._hold_line())    # "One moment." if the answer is slow
        # Only the turn's final answer is spoken. Text the model writes before using a tool ("What do you
        # need help with?" + save the name + "May I have your email?") would otherwise reach the caller as
        # several questions in a row. It is kept as a fallback in case the final round has no words.
        this_round: list[str] = []
        before_tools: list[str] = []
        try:
            async for ev in self.assistant.run_turn(conv, text, uid, call_info=info):
                if ev["type"] == "delta":
                    spoken = speakable(ev["text"])
                    if spoken.strip():
                        this_round.append(spoken)
                elif ev["type"] == "tool":
                    if this_round:
                        before_tools, this_round = this_round, []
                elif ev["type"] == "transfer":
                    transfer = ev
            holding.cancel()
            for spoken in this_round or before_tools:
                if self.interrupted:
                    break
                await self.send({"type": "text", "token": spoken, "last": False})
                words += len(spoken.split())
        except LLMError as exc:
            log.error("model error on call %s: %s", self.call.id, exc)
            failed = True
            await self.repo.log_event(conv.id, "call_model_error", {"error": str(exc)[:300]})
            if self.call.caller_number:      # someone can call them back
                await self.repo.escalate(conv.id, "cannot_help", "The assistant was unavailable during a call.")
                await after_handover(self.jobs, conv.id, "cannot_help")
            words += await self._say(UNAVAILABLE_PHONE if self.call.caller_number else UNAVAILABLE_WEB)
        finally:
            holding.cancel()
            self.speaking = False
        if not self.interrupted:
            await self.send({"type": "text", "token": "", "last": True})
        if failed:
            await self._end({"action": "hangup", "reason": "unavailable"}, words, outcome="assistant_unavailable")
        elif transfer:
            await self._end({"action": "transfer", "consultant_id": transfer["consultant_id"],
                             "reason": transfer["reason"]}, words, outcome="transferring",
                            transfer_to=transfer["consultant_id"])
        elif time.monotonic() - self.started > self.assistant.voice_rules.max_call_minutes * 60:
            words += await self._say(TIME_LIMIT)
            await self._end({"action": "hangup", "reason": "time_limit"}, words, outcome="time_limit")

    async def _hold_line(self) -> None:
        await self.speak_delay(HOLD_AFTER_S)
        if not self.interrupted:
            await self.send({"type": "text", "token": "One moment.", "last": False})

    def _context_note(self) -> str:
        parts = []
        if self.call.direction == "outbound":
            parts.append(f"You called them (outbound call) about: {self.call.purpose or 'their enquiry'}.")
        parts.append(f"Caller's phone number: {self.call.caller_number}." if self.call.caller_number else
                     "This is a browser call: their phone number is not known.")
        if self.note:
            parts.append(self.note)
        return " ".join(parts)

    async def _end(self, data: dict[str, Any], words: int, *, outcome: str, transfer_to: str = "") -> None:
        self.outcome = outcome
        await self.calls.update(self.call.id, outcome=outcome, **({"transfer_to": transfer_to} if transfer_to else {}))
        await self.repo.log_event(self.conversation_id, "call_" + outcome, data)
        await self.speak_delay(min(words * SECONDS_PER_WORD + 0.5, 12.0))   # let the last words be heard
        await self.send({"type": "end", "handoffData": json.dumps(data)})
        self.ended = True
