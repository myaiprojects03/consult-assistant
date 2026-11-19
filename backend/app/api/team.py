"""Private API for the internal knowledge assistant. Every route except login needs a staff session
(Authorization: Bearer <token>); each user only ever sees their own threads.

POST /api/team/login                       {email, password} -> {token, user, clients}
POST /api/team/logout
GET  /api/team/me                          the user and the clients they may see
GET  /api/team/threads                     the user's conversations
POST /api/team/threads                     start one
GET  /api/team/threads/{id}/messages       transcript, with the sources of each answer
POST /api/team/threads/{id}/messages       {client_message_id, text, focus_client?}; the answer streams
     as server-sent events: delta, tool, sources ([{n, title, kind, client, path}]), done, error
POST /api/team/calls                        {client_message_id, to, name, purpose}: the assistant phones someone
     (in calling hours; a retried request places one call)
"""
from __future__ import annotations

import logging
import re
import secrets
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.chat import client_key, hash_token, limit, sse
from app.clients.store import Client, ClientStore
from app.config import get_settings
from app.db.repo import Repo, join_texts
from app.llm.base import LLMError
from app.shared.store import SharedStore, get_store
from app.staff.store import DUMMY_HASH, StaffStore, StaffUser, new_session_token, token_hash, verify_password
from app.team.agent import TeamAssistant, sources_in
from app.voice.store import Call

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/team", tags=["team"])
TURN_LOCK_S = 180


class LoginIn(BaseModel):
    email: str = Field(..., max_length=200)
    password: str = Field(..., max_length=200)


class TeamMessageIn(BaseModel):
    client_message_id: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    text: str
    focus_client: str = Field("", max_length=64)


def staff_store(request: Request) -> StaffStore:
    return request.app.state.staff


def client_store(request: Request) -> ClientStore:
    return request.app.state.clients


def get_repo(request: Request) -> Repo:
    return request.app.state.repo


async def current_user(request: Request, authorization: str | None = Header(None)) -> StaffUser:
    token = (authorization or "").removeprefix("Bearer ").strip()
    user = await request.app.state.staff.session_user(token_hash(token)) if token else None
    if user is None:
        raise HTTPException(401, "Please sign in.")
    return user


async def visible_clients(staff: StaffStore, clients: ClientStore, user: StaffUser) -> list[Client]:
    return await clients.list_clients(await staff.allowed_clients(user))


def _clients_out(clients: list[Client]) -> list[dict[str, str]]:
    return [{"id": c.id, "slug": c.slug, "name": c.name, "tier": c.tier} for c in clients]


@router.post("/login")
async def login(body: LoginIn, request: Request, staff: StaffStore = Depends(staff_store),
                clients: ClientStore = Depends(client_store), store: SharedStore = Depends(get_store),
                repo: Repo = Depends(get_repo)) -> dict[str, Any]:
    email = body.email.strip().lower()
    await limit(store, f"login:ip:{client_key(request)}", 20, 900, "Too many sign-in attempts. Try again later.")
    await limit(store, f"login:email:{email}", 8, 900, "Too many sign-in attempts. Try again later.")
    found = await staff.user_with_hash(email)
    ok = verify_password(body.password, found[1] if found else DUMMY_HASH) and found is not None
    if not ok or not found or not found[0].active:
        await repo.log_event(None, "staff_login_failed", {"email": email})
        raise HTTPException(401, "Wrong email or password.")
    user = found[0]
    token, digest = new_session_token()
    await staff.create_session(user.id, digest)
    await repo.log_event(None, "staff_login", {"user": user.id})
    return {"token": token, "user": {"name": user.name, "email": user.email, "role": user.role},
            "clients": _clients_out(await visible_clients(staff, clients, user))}


@router.post("/logout")
async def logout(user: StaffUser = Depends(current_user), staff: StaffStore = Depends(staff_store),
                 authorization: str | None = Header(None)) -> dict[str, bool]:
    await staff.end_session(token_hash((authorization or "").removeprefix("Bearer ").strip()))
    return {"ok": True}


@router.get("/me")
async def me(user: StaffUser = Depends(current_user), staff: StaffStore = Depends(staff_store),
             clients: ClientStore = Depends(client_store)) -> dict[str, Any]:
    return {"user": {"name": user.name, "email": user.email, "role": user.role},
            "clients": _clients_out(await visible_clients(staff, clients, user))}


@router.get("/threads")
async def threads(user: StaffUser = Depends(current_user), repo: Repo = Depends(get_repo)) -> dict[str, Any]:
    return {"threads": await repo.staff_threads(user.id)}


@router.post("/threads")
async def new_thread(user: StaffUser = Depends(current_user), repo: Repo = Depends(get_repo)) -> dict[str, str]:
    conv = await repo.create_conversation("team", "", "", staff_user_id=user.id)
    return {"id": conv.id}


async def own_thread(repo: Repo, thread_id: str, user: StaffUser) -> str:
    conv = await repo.get_conversation(thread_id)
    if conv is None or conv.channel != "team" or conv.staff_user_id != user.id:
        raise HTTPException(404, "Conversation not found.")
    return conv.id


@router.get("/threads/{thread_id}/messages")
async def thread_messages(thread_id: str, user: StaffUser = Depends(current_user),
                          repo: Repo = Depends(get_repo)) -> dict[str, Any]:
    cid = await own_thread(repo, thread_id, user)
    stored = await repo.recent_messages(cid, 300)
    out: list[dict[str, Any]] = []
    turn: list[Any] = []

    def close_turn() -> None:
        texts = [m.text for m in turn if m.role == "assistant"]
        if texts:
            out.append({"role": "assistant", "text": join_texts(texts), "sources": sources_in(turn)})
    for m in stored:
        if m.role == "user":
            close_turn()
            turn = []
            out.append({"role": "user", "text": m.text})
        else:
            turn.append(m)
    close_turn()
    return {"messages": out}


@router.post("/threads/{thread_id}/messages")
async def ask(thread_id: str, body: TeamMessageIn, request: Request, user: StaffUser = Depends(current_user),
              repo: Repo = Depends(get_repo), staff: StaffStore = Depends(staff_store),
              clients: ClientStore = Depends(client_store), store: SharedStore = Depends(get_store)
              ) -> StreamingResponse:
    s = get_settings()
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Message is empty.")
    if len(text) > s.team_max_message_chars:
        raise HTTPException(413, f"Messages can be up to {s.team_max_message_chars} characters.")
    cid = await own_thread(repo, thread_id, user)
    await limit(store, f"team:{user.id}", s.team_messages_per_minute, 60, "Too many questions at once. Wait a moment.")
    user_msg_id, _ = await repo.add_user_message(cid, text, body.client_message_id)
    existing = await repo.find_reply(cid, user_msg_id)
    if existing is not None:
        async def replay() -> AsyncIterator[str]:
            yield sse({"type": "delta", "text": existing.text})
            yield sse({"type": "done", "message_id": existing.id, "replayed": True})
        return StreamingResponse(replay(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
    lock = f"team-turn:{cid}:{user_msg_id}"
    if not await store.add(lock, "1", TURN_LOCK_S):
        raise HTTPException(409, "Still answering this message.")
    allowed = await visible_clients(staff, clients, user)
    assistant: TeamAssistant = request.app.state.team_assistant

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in assistant.run_turn(cid, user, allowed, text, user_msg_id, body.focus_client):
                yield sse(event)
        except LLMError as exc:
            log.error("model error in team thread %s: %s", cid, exc)
            msg = str(exc) if s.is_development else "The assistant is unavailable right now. Please try again."
            yield sse({"type": "error", "message": msg})
        except Exception:
            log.exception("team turn failed in %s", cid)
            yield sse({"type": "error", "message": "Something went wrong. Please try again."})
        finally:
            await store.clear(lock)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- outbound calls
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class CallIn(BaseModel):
    client_message_id: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    to: str = Field(..., max_length=20)
    name: str = Field("", max_length=100)
    purpose: str = Field(..., min_length=3, max_length=200)


@router.post("/calls")
async def place_call(body: CallIn, request: Request, user: StaffUser = Depends(current_user),
                     repo: Repo = Depends(get_repo), store: SharedStore = Depends(get_store)) -> dict[str, Any]:
    s = get_settings()
    state = request.app.state
    if state.integrations.telephony is None:
        raise HTTPException(409, "Phone calls are not set up (VOICE_PROVIDER=twilio, see docs/SETUP.md).")
    to = re.sub(r"[\s()-]", "", body.to)
    if not E164.match(to):
        raise HTTPException(422, "Use the international format, e.g. +441234567890.")
    await limit(store, f"calls:{user.id}", 20, 3600, "Too many calls requested. Try again later.")
    call, created = await state.calls.create(Call(
        id="", direction="outbound", request_key=f"{user.id}:{body.client_message_id}", to_number=to,
        from_number=s.twilio_phone_number, purpose=" ".join(body.purpose.split()),
        contact_name=" ".join(body.name.split()), requested_by=user.id))
    if created:
        conv = await repo.create_conversation("voice", hash_token(secrets.token_urlsafe(16)), "outbound call")
        await state.calls.update(call.id, conversation_id=conv.id)
        await state.jobs.enqueue("voice.outbound", f"call:{call.id}", {"call_id": call.id}, ref=call.id)
        await repo.log_event(conv.id, "call_requested", {"user": user.id, "call": call.id})
    return {"call_id": call.id, "status": call.status if not created else "queued",
            "note": "The call is placed now in calling hours, otherwise at the next allowed time."}
