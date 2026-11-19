"""Any OpenAI-compatible Chat Completions API with streaming tool calls: Groq (free), OpenRouter, OpenAI,
a local Ollama. Used as the free stand-in for Claude during the build."""
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

STOP_REASONS = {"stop": "end", "tool_calls": "tool_use", "length": "max_tokens"}


def to_openai(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                                     for c in m.tool_calls]
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
    return out


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, *, service: str, base_url: str, api_key: str, model: str, key_setting: str,
                 timeout_s: float = 30.0, reasoning_effort: str = ""):
        if not base_url or not model:
            raise LLMError(f"{service}: set the base URL and model (LLM_BASE_URL, LLM_CHAT_MODEL).")
        if not api_key and "localhost" not in base_url:
            raise LLMError(f"{service}: {key_setting} is not set.")
        self.name, self.model, self.key_setting = service, model, key_setting
        self.reasoning_effort = reasoning_effort
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers,
                                         timeout=httpx.Timeout(timeout_s, connect=10))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int,
                     context: str = "") -> AsyncIterator[Event]:
        full_system = f"{system}\n\n{context}" if context else system
        body: dict[str, Any] = {"model": self.model, "messages": to_openai(full_system, messages),
                                "max_tokens": max_tokens, "stream": True, "temperature": 0.3}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.input_schema}} for t in tools]
        calls: dict[int, dict[str, str]] = {}
        stop = "end"
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        r = await send_streaming(self._client, "/chat/completions", body, self.retry_total_s)
        try:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="replace")[:300]
                if r.status_code == 401:
                    raise LLMError(f"{self.name} rejected the API key: check {self.key_setting}.")
                if r.status_code == 429:
                    daily = "per day" in detail.lower() or "(tpd)" in detail.lower() or "(rpd)" in detail.lower()
                    what = "daily limit reached (resets within 24 hours)" if daily else "rate limit reached"
                    raise RateLimited(f"{self.name} {what}: {detail[:200]}", retry_after_s(r.headers), daily=daily)
                raise LLMError(f"{self.name} returned {r.status_code}: {detail}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                data = json.loads(payload)
                if data.get("usage"):
                    usage.update({k: data["usage"].get(k, 0) for k in usage})
                xg = data.get("x_groq", {}).get("usage")
                if xg:
                    usage.update({k: xg.get(k, 0) for k in usage})
                for choice in data.get("choices", []):
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        stop = STOP_REASONS.get(choice["finish_reason"], "end")
        finally:
            await r.aclose()
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"]) if c["args"].strip() else {}
            except json.JSONDecodeError as exc:
                raise LLMError(f"{self.name} sent invalid tool arguments: {exc}") from exc
            yield ToolCallEvent(ToolCall(c["id"] or f"call_{i}", c["name"], args))
        if calls:
            stop = "tool_use"
        yield Done(stop, usage["prompt_tokens"], usage["completion_tokens"])
