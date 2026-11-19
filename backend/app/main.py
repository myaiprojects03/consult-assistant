"""FastAPI application: startup wiring, security headers, health check."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, chat, jobs, team, voice
from app.assistant.agent import Assistant, Services
from app.booking.emails import load_sms, load_templates
from app.booking.service import BookingService
from app.booking.slots import BookingConfig
from app.booking.store import BookingStore, MemoryBookingStore, PgBookingStore
from app.booking.team import load_team
from app.clients.store import MemoryClientStore, PgClientStore
from app.clients.tickets import TicketService
from app.clients.verify import Verifier
from app.config import CONTENT_DIR, Settings, get_settings
from app.content.live import check_folder, materialize
from app.content.store import MemoryContentStore, PgContentStore
from app.db.migrations import connect
from app.db.repo import MemoryRepo, PostgresRepo, Repo
from app.integrations.base import IntegrationError
from app.integrations.registry import build_integrations
from app.jobs.handlers import HANDLERS, JobContext, report_failure, schedule_daily
from app.jobs.qstash import QStash
from app.jobs.runner import Jobs, Worker, local_loop
from app.jobs.store import Job, JobStore, MemoryJobStore, PgJobStore
from app.kb.content import load_rules, sync_content
from app.kb.embeddings import build_embedder
from app.llm.base import LLMError, LLMProvider
from app.llm.registry import build_llm
from app.shared.store import build_store
from app.staff.store import MemoryStaffStore, PgStaffStore
from app.team.agent import TeamAssistant
from app.voice.store import MemoryCallStore, PgCallStore
from app.voice.twilio import VoiceConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}


async def build_repo() -> Repo:
    s = get_settings()
    try:
        return PostgresRepo(await connect(s.database_url))
    except Exception as exc:
        if not s.is_development:
            raise RuntimeError(f"Database is not reachable: {exc}") from exc
        log.warning("database unreachable (%s): using in-memory storage, nothing is saved on restart", exc)
        return MemoryRepo()


class MissingLLM(LLMProvider):
    """Keeps the server up when the model is not configured; every reply explains what to set."""
    name, model = "not configured", ""

    def __init__(self, reason: str):
        self.reason = reason

    async def stream(self, **_):
        raise LLMError(self.reason)
        yield  # pragma: no cover


def make_assistant(state: Any, llm: LLMProvider, s: Settings | None = None) -> Assistant:
    s = s or get_settings()
    services = Services(state.booking, state.jobs, state.team, state.clients, state.verifier, state.tickets,
                        sends_email=s.email_provider != "none")
    return Assistant(state.repo, llm, state.embedder, state.rules, services,
                     max_tokens=s.llm_max_output_tokens, history_messages=s.chat_history_messages,
                     max_tool_rounds=s.chat_max_tool_rounds, voice_max_tokens=s.voice_max_output_tokens)


def make_team_assistant(state: Any, llm: LLMProvider, s: Settings | None = None) -> TeamAssistant:
    s = s or get_settings()
    return TeamAssistant(state.repo, state.clients, llm, state.embedder, state.rules, state.team,
                         max_tokens=s.team_max_output_tokens)


def build_people_stores(state: Any) -> None:
    """Clients (with their projects and tickets) and staff accounts share the chat database."""
    pool = state.repo.pool if isinstance(state.repo, PostgresRepo) else None
    state.clients = PgClientStore(pool) if pool else MemoryClientStore()
    state.staff = PgStaffStore(pool) if pool else MemoryStaffStore()
    state.calls = PgCallStore(pool) if pool else MemoryCallStore()
    state.content = PgContentStore(pool) if pool else MemoryContentStore()


def build_llm_or_placeholder(s: Settings, purpose: str = "chat") -> LLMProvider:
    try:
        return build_llm(s, purpose)
    except LLMError as exc:
        if not s.is_development:
            raise
        log.warning("AI model not configured: %s", exc)
        return MissingLLM(str(exc))


def voice_config(s: Settings, rules: Any) -> VoiceConfig | None:
    """Phone calls need this API's public https address (Twilio calls it) and the Twilio account."""
    if s.voice_provider != "twilio":
        return None
    if not s.public_api_url.startswith("https://"):
        raise RuntimeError("VOICE_PROVIDER=twilio needs PUBLIC_API_URL set to this API's https address "
                           "(on your computer: the tunnel address, see docs/SETUP.md).")
    language = str((rules.voice or {}).get("language", "en-GB"))
    return VoiceConfig(s.public_api_url, s.twilio_auth_token, s.twilio_phone_number, language,
                       s.voice_tts_provider, s.voice_tts_voice, s.voice_transcription_provider)


async def start_services(state: Any, s: Settings) -> None:
    """Bookings, background jobs and integrations (Phase 2)."""
    repo = state.repo
    pool = repo.pool if isinstance(repo, PostgresRepo) else None
    job_store: JobStore = PgJobStore(pool) if pool else MemoryJobStore()
    booking_store: BookingStore = PgBookingStore(pool) if pool else MemoryBookingStore()
    try:
        state.integrations = build_integrations(s, repo, state.rules.firm_name)
        state.qstash = QStash(s.qstash_url, s.qstash_token, s.public_api_url) if s.job_runner == "qstash" else None
    except IntegrationError as exc:
        raise RuntimeError(f"Integration settings: {exc}") from exc
    state.jobs = Jobs(job_store, s.job_runner, state.qstash)
    state.team = load_team(state.content_dir)
    state.booking_cfg = BookingConfig.from_rules(state.rules.booking)
    state.booking = BookingService(booking_store, state.integrations.calendar, state.team, state.booking_cfg,
                                   state.jobs)
    state.tickets = TicketService(state.clients, state.integrations.ticketing, state.jobs)
    state.verifier = Verifier(state.store, state.clients, state.integrations.mailer, state.rules.firm_name)
    state.voice_config = voice_config(s, state.rules)
    ctx = JobContext(repo, booking_store, state.integrations, getattr(state, "background_llm", state.llm),
                     state.rules, state.team,
                     state.booking_cfg, load_templates(state.content_dir), state.jobs, state.clients,
                     calls=state.calls, voice=state.voice_config, sms_templates=load_sms(state.content_dir))
    state.job_context = ctx

    async def on_failure(job: Job, error: str) -> None:
        await report_failure(ctx, job, error)
    state.worker = Worker(state.jobs, HANDLERS, ctx, on_failure)
    state.worker_task = asyncio.create_task(local_loop(state.worker)) if s.job_runner == "local" else None


async def stop_services(state: Any) -> None:
    if state.worker_task:
        state.worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.worker_task
    await state.integrations.aclose()
    if state.qstash:
        await state.qstash.aclose()


async def apply_content(state: Any, s: Settings | None = None) -> None:
    """Rebuild everything that reads content, from content/ plus the dashboard edits. Called after an edit
    (and by the watcher when another server saved one). Live chats and calls pick it up on their next turn."""
    s = s or get_settings()
    stamp = await state.content.stamp()
    folder = materialize(CONTENT_DIR, await state.content.all())
    try:
        check_folder(folder)
    except ValueError:
        log.exception("content in the database does not load; keeping the current content")
        shutil.rmtree(folder, ignore_errors=True)
        return
    old_folder = state.content_dir
    state.rules, state.team = load_rules(folder), load_team(folder)
    state.booking_cfg = BookingConfig.from_rules(state.rules.booking)
    state.booking.team, state.booking.cfg = state.team, state.booking_cfg
    state.verifier.firm_name = state.rules.firm_name
    ctx = state.job_context
    ctx.rules, ctx.team, ctx.booking_cfg = state.rules, state.team, state.booking_cfg
    ctx.templates, ctx.sms_templates = load_templates(folder), load_sms(folder)
    old_assistant, old_team = state.assistant, state.team_assistant
    state.assistant = make_assistant(state, old_assistant.llm, s)
    state.assistant.now = old_assistant.now
    state.team_assistant = make_team_assistant(state, old_team.llm, s)
    client_ids = {c.slug: c.id for c in await state.clients.list_clients(None)}
    state.kb_stats = await sync_content(state.repo, state.embedder, folder, client_ids)
    state.content_dir, state.content_stamp = folder, stamp
    shutil.rmtree(old_folder, ignore_errors=True)
    log.info("content applied: %s", state.kb_stats)


async def watch_content(state: Any, s: Settings, every_s: float = 30.0) -> None:
    """Another server saved an edit: apply it here too."""
    while True:
        await asyncio.sleep(every_s)
        try:
            if await state.content.stamp() != state.content_stamp:
                await apply_content(state, s)
        except Exception:
            log.exception("checking for content changes failed")


async def daily_tasks(state: Any, every_s: float = 3600.0) -> None:
    """Queues the nightly privacy clean-up (once a day; checked hourly). On serverless hosting the hourly
    QStash sweep does this instead."""
    while True:
        try:
            await schedule_daily(state.jobs)
        except Exception:
            log.exception("queuing the daily tasks failed")
        await asyncio.sleep(every_s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.store = await build_store(s)
    app.state.repo = await build_repo()
    app.state.embedder = build_embedder(s)
    build_people_stores(app.state)
    # content/ plus the edits made in the admin dashboard (app/content/live.py)
    app.state.content_stamp = await app.state.content.stamp()
    app.state.content_dir = materialize(CONTENT_DIR, await app.state.content.all())
    app.state.rules = load_rules(app.state.content_dir)
    client_ids = {c.slug: c.id for c in await app.state.clients.list_clients(None)}
    app.state.kb_stats = await sync_content(app.state.repo, app.state.embedder, app.state.content_dir, client_ids)
    llm = build_llm_or_placeholder(s)
    app.state.llm = llm
    app.state.team_llm = build_llm_or_placeholder(s, "draft")
    app.state.background_llm = build_llm_or_placeholder(s, "background")
    await start_services(app.state, s)
    app.state.assistant = make_assistant(app.state, llm, s)
    app.state.team_assistant = make_team_assistant(app.state, app.state.team_llm, s)
    app.state.content_watch = asyncio.create_task(watch_content(app.state, s))
    app.state.daily = (asyncio.create_task(daily_tasks(app.state))
                       if s.job_runner == "local" and isinstance(app.state.repo, PostgresRepo) else None)
    log.info("ready: model=%s (%s), storage=%s, jobs=%s, integrations=%s", llm.name, llm.model,
             type(app.state.repo).__name__, s.job_runner, app.state.integrations.describe())
    yield
    for task in (app.state.content_watch, app.state.daily):
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    shutil.rmtree(app.state.content_dir, ignore_errors=True)
    await stop_services(app.state)
    await llm.aclose()
    for other in (app.state.team_llm, app.state.background_llm):
        if other is not llm:
            await other.aclose()
    await app.state.repo.aclose()
    await app.state.store.aclose()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Consult Assistant API", lifespan=lifespan,
                  docs_url="/docs" if s.is_development else None, redoc_url=None,
                  openapi_url="/openapi.json" if s.is_development else None)
    app.add_middleware(CORSMiddleware, allow_origins=s.origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Content-Type", "X-Chat-Token", "Authorization"],
                       allow_credentials=False, max_age=600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(chat.router)
    app.include_router(jobs.router)
    app.include_router(team.router)
    app.include_router(voice.router)
    app.include_router(admin.router)

    @app.get("/health")
    async def health(request: Request, response: Response) -> dict:
        """Uptime check: 503 when the database is down. Details (model, integrations, jobs) only in
        development; in production they are not public."""
        repo = request.app.state.repo
        db_ok = True
        if isinstance(repo, PostgresRepo):
            try:
                await repo.pool.fetchval("SELECT 1")
            except Exception:
                db_ok = False
        if not db_ok:
            response.status_code = 503
        if not s.is_development:
            return {"status": "ok" if db_ok else "degraded", "database": db_ok}
        llm = request.app.state.llm
        try:
            job_counts = await request.app.state.jobs.store.counts()
        except Exception:
            job_counts = {}
        return {"status": "ok" if db_ok else "degraded", "env": s.app_env,
                "storage": "postgres" if isinstance(repo, PostgresRepo) else "memory", "database": db_ok,
                "model": f"{llm.name} {llm.model}".strip(), "knowledge": request.app.state.kb_stats,
                "integrations": request.app.state.integrations.describe(),
                "jobs": {"runner": s.job_runner, "pending": job_counts.get("pending", 0),
                         "failed": job_counts.get("failed", 0)}}

    return app


app = create_app()
