"""The API with a stand-in model, for browser tests (frontend/e2e) and screenshots. No AI key, no outside
service: the model always looks the question up in the knowledge base and answers with what it found, so a
content edit made in the dashboard shows up in the chat.

    E2E_DATABASE_URL=postgresql://.../consult_e2e python -m tests.e2e_server

The database is emptied at start, so its name must end in _e2e or _test. An admin account is created:
admin@acme.example / "e2e admin password".
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import uvicorn

ADMIN_EMAIL, ADMIN_PASSWORD = "admin@acme.example", "e2e admin password"
DB = os.environ.get("E2E_DATABASE_URL", "")
if not DB or not urlsplit(DB).path.rstrip("/").endswith(("_e2e", "_test")):
    sys.exit("Set E2E_DATABASE_URL to a database whose name ends in _e2e or _test (it is emptied).")

os.environ.update({"APP_ENV": "development", "DATABASE_URL": DB, "REDIS_URL": "", "JOB_RUNNER": "local",
                   "LLM_PROVIDER": "groq", "GROQ_API_KEY": "e2e", "EMBEDDING_PROVIDER": "none",
                   "CRM_PROVIDER": "none", "CALENDAR_PROVIDER": "local", "EMAIL_PROVIDER": "none",
                   "SLACK_BOT_TOKEN": "", "TICKETING_PROVIDER": "none", "VOICE_PROVIDER": "none",
                   "SMS_PROVIDER": "none"})
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")

from app.llm.base import Done, Event, LLMProvider, Message, TextDelta, ToolCall, ToolCallEvent, ToolSpec  # noqa: E402
from app.main import create_app  # noqa: E402
from app.staff.store import hash_password  # noqa: E402


class CannedLLM(LLMProvider):
    name, model = "Stand-in", "e2e"

    async def stream(self, *, system: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int,
                     context: str = "") -> AsyncIterator[Event]:
        last = messages[-1] if messages else None
        names = {t.name for t in tools}
        if last is not None and last.role == "user" and "search_knowledge" in names:
            yield ToolCallEvent(ToolCall("e2e-search", "search_knowledge", {"query": last.text}))
            yield Done("tool_use", 10, 5)
            return
        asked = next((m.text for m in reversed(messages) if m.role == "user"), "")
        if last is not None and last.role == "tool" and "formatting check" in asked:
            text = "Here is what we offer:\n\n- **Cloud migration** to Azure or AWS\n- **Managed IT** support"
            yield TextDelta(text)
            yield Done("end", 10, 5)
            return
        if last is not None and last.role == "tool":
            text = "I don't have that information, but a consultant can help on a free discovery call."
            if last.text.startswith("["):                   # "[title]\nchunk": answer with the chunk
                block = last.text.split("\n[", 1)[0].split("\n", 1)[-1]
                text = " ".join(line for line in block.split("\n") if not line.startswith("#"))
                text = " ".join(text.split())[:400]
                if last.text.startswith("[S1]"):           # team assistant: cite the source it used
                    text += " [S1]"
        else:
            text = "Here is a first draft based on the documents you can see."
        for word in text.split(" "):
            yield TextDelta(word + " ")
        yield Done("end", 10, 5)


async def reset() -> None:
    conn = await asyncpg.connect(DB)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()


if os.environ.get("E2E_KEEP_DATA") != "1":       # 1: keep what is there (e.g. after scripts.seed_demo)
    asyncio.run(reset())
app = create_app()
inner = app.router.lifespan_context


@asynccontextmanager
async def lifespan(a: Any) -> AsyncIterator[None]:
    async with inner(a):
        llm = CannedLLM()
        a.state.assistant.llm = a.state.team_assistant.llm = a.state.job_context.llm = llm
        if await a.state.staff.user_with_hash(ADMIN_EMAIL) is None:
            await a.state.staff.create_user(ADMIN_EMAIL, "Alex Admin", "admin", hash_password(ADMIN_PASSWORD))
        yield

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), log_level="warning")
