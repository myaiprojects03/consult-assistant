"""Claude through the Anthropic Messages API (streaming, tool use, prompt caching)."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.llm.base import (
    Done,
    Event,
    LLMError,
    LLMProvider,
    Message,
    RateLimited,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolSpec,
    retry_after_s,
    send_streaming,
)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
STOP_REASONS = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_use", "max_tokens": "max_tokens"}


def to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
    """Neutral turns -> Anthropic messages. Consecutive tool results become one user turn of
    tool_result blocks, as the API requires."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            block = {"type": "text", "text": m.text}
            if out and out[-1]["role"] == "user":   # e.g. a message whose earlier reply failed
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.tool_calls]
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": " "}]})
        else:
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text}
            if out and out[-1]["role"] == "user" and out[-1]["content"][0]["type"] == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicProvider(LLMProvider):
    name = "Claude"

    def __init__(self, api_key: str, model: str, timeout_s: float = 30.0):
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set (get one at console.anthropic.com).")
        self.model = model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10), headers={
            "x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int,
                     context: str = "") -> AsyncIterator[Event]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # The fixed instructions are identical every turn: cached (10x cheaper to re-read).
            # The per-turn context comes after the cache point so it does not invalidate it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            + ([{"type": "text", "text": context}] if context else []),
            "messages": to_anthropic(messages),
            "stream": True,
        }
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                             for t in tools]
        blocks: dict[int, dict[str, Any]] = {}
        usage = {"input": 0, "output": 0}
        stop = "end"
        r = await send_streaming(self._client, API_URL, body, self.retry_total_s)
        try:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="replace")[:300]
                if r.status_code == 401:
                    raise LLMError("Claude rejected the API key: check ANTHROPIC_API_KEY.")
                if r.status_code == 429:
                    raise RateLimited("Claude rate limit reached; try again shortly.", retry_after_s(r.headers))
                raise LLMError(f"Claude returned {r.status_code}: {detail}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                kind = data.get("type")
                if kind == "message_start":
                    usage["input"] = data["message"].get("usage", {}).get("input_tokens", 0)
                elif kind == "content_block_start":
                    cb = data["content_block"]
                    blocks[data["index"]] = {"type": cb["type"], "id": cb.get("id", ""),
                                             "name": cb.get("name", ""), "json": ""}
                elif kind == "content_block_delta":
                    d = data["delta"]
                    if d["type"] == "text_delta" and d["text"]:
                        yield TextDelta(d["text"])
                    elif d["type"] == "input_json_delta":
                        blocks[data["index"]]["json"] += d.get("partial_json", "")
                elif kind == "content_block_stop":
                    b = blocks.get(data["index"])
                    if b and b["type"] == "tool_use":
                        try:
                            args = json.loads(b["json"]) if b["json"].strip() else {}
                        except json.JSONDecodeError as exc:
                            raise LLMError(f"Claude sent invalid tool arguments: {exc}") from exc
                        yield ToolCallEvent(ToolCall(b["id"], b["name"], args))
                elif kind == "message_delta":
                    stop = STOP_REASONS.get(data["delta"].get("stop_reason") or "", "end")
                    usage["output"] = data.get("usage", {}).get("output_tokens", usage["output"])
                elif kind == "error":
                    raise LLMError(f"Claude stream error: {data.get('error', {}).get('message', data)}")
        finally:
            await r.aclose()
        yield Done(stop, usage["input"], usage["output"])
