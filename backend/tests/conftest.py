from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Tests never read the developer's .env or call real services.
os.environ.setdefault("APP_ENV", "development")
os.environ["LLM_PROVIDER"] = "groq"
os.environ["GROQ_API_KEY"] = "test-key"
os.environ["EMBEDDING_PROVIDER"] = "none"
os.environ["DATABASE_URL"] = "postgresql://nobody@127.0.0.1:1/none"   # unreachable: in-memory storage
os.environ["REDIS_URL"] = ""
os.environ["JOB_RUNNER"] = "manual"           # tests run jobs explicitly
os.environ["CRM_PROVIDER"] = "none"
os.environ["CALENDAR_PROVIDER"] = "local"
os.environ["EMAIL_PROVIDER"] = "none"
os.environ["SLACK_BOT_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.config import CONTENT_DIR, get_settings  # noqa: E402
from app.kb.content import load_rules  # noqa: E402
from app.main import create_app, make_assistant, make_team_assistant  # noqa: E402
from tests.fakes import FakeIntegrations, ScriptedLLM  # noqa: E402

get_settings.cache_clear()


@pytest.fixture
def rules():
    return load_rules(CONTENT_DIR)


@pytest.fixture
def make_client() -> Iterator:
    """A started app whose model plays the given script."""
    clients = []

    def _make(rounds: list | None = None, fail: str | None = None, now=None):
        app = create_app()
        client = TestClient(app)
        client.__enter__()
        llm = ScriptedLLM(rounds or [], fail=fail)
        app.state.llm = llm
        app.state.job_context.llm = llm
        app.state.fakes = FakeIntegrations.install(app.state)
        app.state.assistant = make_assistant(app.state, llm)
        app.state.assistant.now = now
        app.state.team_assistant = make_team_assistant(app.state, llm)
        clients.append(client)
        return client, app, llm

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def pg_env(monkeypatch):
    """The app on a fresh, empty Postgres (TEST_DATABASE_URL)."""
    import asyncio

    import asyncpg
    test_db = os.environ["TEST_DATABASE_URL"]

    async def reset():
        conn = await asyncpg.connect(test_db)
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.close()
    asyncio.run(reset())
    monkeypatch.setenv("DATABASE_URL", test_db)
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()
