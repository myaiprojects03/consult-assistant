"""Wire-format tests for Claude and OpenAI-compatible (Groq) streaming with tool calls."""
import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.llm.anthropic_llm import AnthropicProvider, to_anthropic
from app.llm.base import Done, LLMError, Message, RateLimited, TextDelta, ToolCall, ToolCallEvent, ToolSpec
from app.llm.openai_compat_llm import OpenAICompatibleProvider, to_openai
from app.llm.registry import build_llm

TOOL = ToolSpec("search_knowledge", "Search", {"type": "object", "properties": {"query": {"type": "string"}}})


def sse(*events):
    return "".join(f"event: {e.get('type', 'x')}\ndata: {json.dumps(e)}\n\n" for e in events)


def run(provider, **kw):
    async def go():
        return [e async for e in provider.stream(system="SYS", context="CTX", messages=[Message("user", "hi")],
                                                 tools=[TOOL], max_tokens=200, **kw)]
    return asyncio.run(go())


def mock(provider, handler):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)
    provider._client = httpx.AsyncClient(base_url=str(getattr(provider._client, "base_url", "")) or "https://x",
                                         headers=provider._client.headers, transport=httpx.MockTransport(record))
    return seen


# ---------------- Claude ----------------
CLAUDE_STREAM = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me "}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "check."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search_knowledge", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"que'}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'ry": "cloud"}'}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 17}},
    {"type": "message_stop"},
)


def test_claude_streams_text_then_complete_tool_call():
    p = AnthropicProvider("sk-ant-test", "claude-haiku-5-5")
    seen = mock(p, lambda r: httpx.Response(200, text=CLAUDE_STREAM))
    events = run(p)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Let me ", "check."]
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    assert calls == [ToolCall("toolu_1", "search_knowledge", {"query": "cloud"})]
    assert events[-1] == Done("tool_use", 42, 17)
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "sk-ant-test" and req.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-haiku-5-5" and body["stream"] is True
    # fixed instructions cached, per-turn context after the cache point
    assert body["system"][0] == {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    assert body["system"][1] == {"type": "text", "text": "CTX"}
    assert body["tools"][0]["name"] == "search_knowledge" and "input_schema" in body["tools"][0]


def test_claude_message_format_groups_tool_results_and_merges_user_turns():
    msgs = [
        Message("user", "a"), Message("user", "b (retry after a failed reply)"),
        Message("assistant", "Looking", tool_calls=[ToolCall("t1", "x", {"q": 1}), ToolCall("t2", "y", {})]),
        Message("tool", "r1", tool_call_id="t1", tool_name="x"),
        Message("tool", "r2", tool_call_id="t2", tool_name="y"),
        Message("assistant", "Done."),
    ]
    out = to_anthropic(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert [b["text"] for b in out[0]["content"]] == ["a", "b (retry after a failed reply)"]
    assert out[1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "x", "input": {"q": 1}}
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["t1", "t2"]


@pytest.mark.parametrize("status,match", [(401, "ANTHROPIC_API_KEY"), (429, "rate limit"), (500, "500")])
def test_claude_errors_name_the_cause(status, match, monkeypatch):
    async def no_wait(_s):
        return None
    monkeypatch.setattr("app.llm.base.asyncio.sleep", no_wait)      # rate limits retry before failing
    p = AnthropicProvider("k", "m")
    mock(p, lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMError, match=match):
        run(p)


def test_claude_needs_a_key():
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider("", "m")


# ---------------- OpenAI-compatible (Groq) ----------------
def chunk(delta=None, finish=None, usage=None):
    d = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        d["x_groq"] = {"usage": usage}
    return "data: " + json.dumps(d) + "\n\n"


OPENAI_STREAM = (
    chunk({"role": "assistant", "content": "Sure"}) + chunk({"content": "."})
    + chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                             "function": {"name": "update_lead", "arguments": ""}}]})
    + chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"company_size": '}}]})
    + chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"40"}'}}]})
    + chunk({"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "search_knowledge",
                                                                       "arguments": '{"query":"m365"}'}}]})
    + chunk(finish="tool_calls", usage={"prompt_tokens": 30, "completion_tokens": 9})
    + "data: [DONE]\n\n"
)


def test_openai_compatible_assembles_streamed_tool_calls():
    p = OpenAICompatibleProvider(service="Groq", base_url="https://api.groq.com/openai/v1", api_key="gk",
                                 model="openai/gpt-oss-120b", key_setting="GROQ_API_KEY", reasoning_effort="low")
    seen = mock(p, lambda r: httpx.Response(200, text=OPENAI_STREAM))
    events = run(p)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Sure."
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    assert calls == [ToolCall("call_a", "update_lead", {"company_size": "40"}),
                     ToolCall("call_b", "search_knowledge", {"query": "m365"})]
    assert events[-1] == Done("tool_use", 30, 9)
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.groq.com/openai/v1/chat/completions"
    assert body["messages"][0] == {"role": "system", "content": "SYS\n\nCTX"}
    assert body["reasoning_effort"] == "low" and body["tools"][0]["function"]["name"] == "search_knowledge"


def test_openai_message_format():
    out = to_openai("S", [Message("user", "q"), Message("assistant", "", tool_calls=[ToolCall("c1", "x", {"a": 1})]),
                          Message("tool", "res", tool_call_id="c1", tool_name="x")])
    assert out[2] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "x", "arguments": '{"a": 1}'}}]}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "res"}


def test_openai_compatible_errors():
    p = OpenAICompatibleProvider(service="Groq", base_url="https://api.groq.com/openai/v1", api_key="bad",
                                 model="m", key_setting="GROQ_API_KEY")
    mock(p, lambda r: httpx.Response(401, json={"error": "x"}))
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        run(p)


def groq():
    return OpenAICompatibleProvider(service="Groq", base_url="https://api.groq.com/openai/v1", api_key="gk",
                                    model="m", key_setting="GROQ_API_KEY")


def test_short_rate_limit_waits_once_and_retries(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)
    monkeypatch.setattr("app.llm.base.asyncio.sleep", fake_sleep)
    p = groq()
    replies = iter([httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, text=OPENAI_STREAM)])
    seen = mock(p, lambda r: next(replies))
    events = run(p)
    assert len(seen) == 2 and waits == [3.25]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Sure."


def test_long_rate_limit_is_reported_with_the_wait():
    p = groq()
    seen = mock(p, lambda r: httpx.Response(429, headers={"retry-after": "40"}))
    with pytest.raises(RateLimited) as err:
        run(p)
    assert len(seen) == 1 and err.value.retry_after == 40.0


# ---------------- switching by settings ----------------
def test_model_is_chosen_by_settings_only():
    async def names():
        out = []
        for kw in ({"llm_provider": "anthropic", "anthropic_api_key": "k"},
                   {"llm_provider": "groq", "groq_api_key": "k"},
                   {"llm_provider": "openai_compatible", "llm_base_url": "http://localhost:11434/v1",
                    "llm_chat_model": "llama3.2"}):
            p = build_llm(Settings(_env_file=None, **kw))
            out.append((p.name, p.model))
            await p.aclose()
        return out
    assert asyncio.run(names()) == [("Claude", "claude-haiku-5-5"), ("Groq", "openai/gpt-oss-120b"),
                                    ("OpenAI-compatible API", "llama3.2")]
    with pytest.raises(LLMError, match="Available"):
        build_llm(Settings(_env_file=None, llm_provider="nope"))


def test_per_minute_limit_retries_within_the_total_budget_then_fails(monkeypatch):
    from app.llm.base import INLINE_RETRY_TOTAL_S
    waits: list[float] = []

    async def record(sec):
        waits.append(sec)
    monkeypatch.setattr("app.llm.base.asyncio.sleep", record)
    p = AnthropicProvider("k", "m")
    mock(p, lambda r: httpx.Response(429, headers={"retry-after": "6"}, json={"error": {"message": "slow down"}}))
    with pytest.raises(LLMError, match="rate limit"):
        run(p)
    assert waits == [6.25, 6.25, 6.25] and sum(waits) <= INLINE_RETRY_TOTAL_S


def test_fallback_model_answers_when_the_main_one_is_rate_limited():
    import asyncio as aio

    from app.llm.base import FallbackLLM, RateLimited, TextDelta
    from tests.fakes import ScriptedLLM

    class Limited(ScriptedLLM):
        async def stream(self, **kw):
            raise RateLimited("Groq rate limit reached", 9)
            yield  # pragma: no cover
    backup = ScriptedLLM([["From the backup model."]])
    f = FallbackLLM(Limited([]), backup)

    async def go():
        return [e.text async for e in f.stream(system="", messages=[], tools=[], max_tokens=10)
                if isinstance(e, TextDelta)]
    assert aio.run(go()) == ["From the backup model."] and f.primary.retry_total_s == 3.0


def test_after_a_daily_limit_the_main_model_is_left_alone_for_a_while():
    import asyncio as aio

    from app.llm.base import FallbackLLM, RateLimited, TextDelta
    from tests.fakes import ScriptedLLM

    class Daily(ScriptedLLM):
        tries = 0

        async def stream(self, **kw):
            Daily.tries += 1
            raise RateLimited("Groq daily limit reached", 30000, daily=True)
            yield  # pragma: no cover
    f = FallbackLLM(Daily([]), ScriptedLLM([["one"], ["two"]]))

    async def go():
        out = []
        for _ in range(2):
            out += [e.text async for e in f.stream(system="", messages=[], tools=[], max_tokens=10)
                    if isinstance(e, TextDelta)]
        return out
    assert aio.run(go()) == ["one", "two"] and Daily.tries == 1
