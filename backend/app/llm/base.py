"""Provider-neutral chat interface with tool use.

The assistant code talks only to these types. Each provider (Claude, Groq, any OpenAI-compatible API)
converts them to its own wire format, so the model can be changed in settings without code changes.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Message:
    """One turn. role "tool" carries the result of one tool call."""
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)   # assistant only
    tool_call_id: str = ""                                     # tool only
    tool_name: str = ""                                        # tool only


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


# ---- streamed events ----
@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallEvent:
    call: ToolCall


@dataclass
class Done:
    stop_reason: str            # "end" | "tool_use" | "max_tokens"
    input_tokens: int = 0
    output_tokens: int = 0


Event = TextDelta | ToolCallEvent | Done


class LLMError(RuntimeError):
    """A provider failure with a message that names the cause (shown in development only)."""


class RateLimited(LLMError):
    """The provider asked us to slow down; `retry_after` is its suggested wait in seconds."""

    def __init__(self, message: str, retry_after: float, daily: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily          # a per-day quota: waiting a minute will not help


# A visitor can wait this long once for a reply; a longer suggested wait is reported as an error instead.
INLINE_RETRY_MAX_S = 15.0         # one wait the provider asks for (per-minute limits)
INLINE_RETRY_TOTAL_S = 22.0       # all waits for one request


def retry_after_s(headers: httpx.Headers) -> float:
    try:
        return max(0.0, float(headers.get("retry-after", "")))
    except ValueError:
        return 2.0


async def send_streaming(client: httpx.AsyncClient, url: str, body: dict[str, Any],
                         total_s: float | None = None) -> httpx.Response:
    """POST and open the streamed response. On a per-minute rate limit, wait as the provider asks and retry,
    up to INLINE_RETRY_TOTAL_S in all (a daily quota asks for much longer and fails at once).
    The caller must close the response."""
    waited = 0.0
    r = await client.send(client.build_request("POST", url, json=body), stream=True)
    while (r.status_code == 429 and (wait := retry_after_s(r.headers)) <= INLINE_RETRY_MAX_S
           and waited + wait <= (INLINE_RETRY_TOTAL_S if total_s is None else total_s)):
        await r.aclose()
        await asyncio.sleep(wait + 0.25)
        waited += wait + 0.25
        r = await client.send(client.build_request("POST", url, json=body), stream=True)
    return r


class LLMProvider(ABC):
    name: str = "llm"
    model: str = ""
    retry_total_s: float | None = None     # how long to wait out rate limits (None: INLINE_RETRY_TOTAL_S)

    @abstractmethod
    def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int,
               context: str = "") -> AsyncIterator[Event]:
        """Stream text deltas, then any tool calls (complete, with parsed arguments), then Done.

        `system` is the fixed instruction text (identical every turn, so providers can cache it);
        `context` is the per-turn part (what is known about the visitor, handover state)."""

    async def aclose(self) -> None:
        return None


class FallbackLLM(LLMProvider):
    """Use `primary`; if it is rate limited before saying anything, answer with `fallback` straight away.
    On Groq each model has its own allowance, so a second model roughly doubles the room for live traffic."""

    DAILY_PAUSE_S = 3600.0      # after a daily-quota answer, leave the main model alone for an hour

    def __init__(self, primary: LLMProvider, fallback: LLMProvider, wait_s: float = 3.0):
        self.primary, self.fallback = primary, fallback
        self.primary.retry_total_s = wait_s          # don't sit out a long wait when another model is free
        self.name, self.model = primary.name, primary.model
        self.primary_paused_until = 0.0

    async def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int,
                     context: str = "") -> AsyncIterator[Event]:
        started = False
        loop_time = asyncio.get_running_loop().time
        try:
            if loop_time() < self.primary_paused_until:
                raise RateLimited("main model paused after its daily limit", 0, daily=True)
            async for ev in self.primary.stream(system=system, messages=messages, tools=tools,
                                                max_tokens=max_tokens, context=context):
                started = True
                yield ev
            return
        except RateLimited as exc:
            if started:
                raise
            if exc.daily and loop_time() >= self.primary_paused_until:
                self.primary_paused_until = loop_time() + self.DAILY_PAUSE_S
            log.warning("%s %s is rate limited; answering with %s (%s)", self.primary.name, self.primary.model,
                        self.fallback.model, str(exc)[:120])
        async for ev in self.fallback.stream(system=system, messages=messages, tools=tools, max_tokens=max_tokens,
                                             context=context):
            yield ev

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()
