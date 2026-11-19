"""Public chat API used by the website widget.

POST /api/chat/sessions                 start a conversation: returns its id, a secret token and the greeting
GET  /api/chat/sessions/{id}/messages   the visible transcript (to restore the widget after a page reload)
POST /api/chat/sessions/{id}/messages   send a message; the reply streams back as server-sent events:
     data: {"type": "delta", "text": "..."}     reply text, already checked by the output guard
     data: {"type": "tool", "name": "..."}      the assistant is looking something up / saving
     data: {"type": "handover", "reason": ...}  the conversation was passed to a consultant
     data: {"type": "done", "message_id": n}    finished (with "replayed": true for a retried message)
     data: {"type": "error", "message": "..."}  failed; resending the same client_message_id retries it

Every message carries a client_message_id chosen by the widget. A retry after a network error reuses it,
so the message is stored once and answered once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.assistant.agent import Assistant
from app.config import get_settings
from app.db.repo import Conversation, Repo, join_texts
from app.llm.base import LLMError
from app.shared.store import SharedStore, get_store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])
TURN_LOCK_S = 90


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_key(request: Request) -> str:
    """Hashed client network address (no raw IPs in Redis), used for rate limits.

    CLIENT_IP_HEADER names a header the host's edge sets and visitors cannot (on Render: cf-connecting-ip,
    set by Cloudflare). Without it, the first X-Forwarded-For address is used, which a visitor can fake."""
    header = get_settings().client_ip_header.strip().lower()
    ip = request.headers.get(header, "").strip() if header else ""
    if not ip:
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    return hashlib.sha256(ip.encode()).hexdigest()[:24]


def get_repo(request: Request) -> Repo:
    return request.app.state.repo


def get_assistant(request: Request) -> Assistant:
    return request.app.state.assistant


async def limit(store: SharedStore, key: str, max_hits: int, window_s: int, message: str) -> None:
    try:
        if await store.hit(key, window_s) > max_hits:
            raise HTTPException(429, message)
    except HTTPException:
        raise
    except Exception:  # Redis briefly unreachable: do not block conversations over a counter
        log.exception("rate limit check failed")


async def conversation_for(repo: Repo, conversation_id: str, token: str | None,
                           channel: str = "web") -> Conversation:
    conv = await repo.get_conversation(conversation_id)
    if (conv is None or conv.channel != channel or not token
            or not secrets.compare_digest(conv.token_hash, hash_token(token))):
        raise HTTPException(404, "Conversation not found.")
    return conv


class StartIn(BaseModel):
    channel: str = Field("web", pattern="^(web)$")
    page_url: str = Field("", max_length=500)
    timezone: str = Field("", max_length=64)     # the visitor's browser time zone, e.g. "Europe/London"


def valid_timezone(name: str) -> str:
    try:
        return ZoneInfo(name).key if name else ""
    except (ZoneInfoNotFoundError, ValueError):
        return ""


class MessageIn(BaseModel):
    client_message_id: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(..., min_length=1)


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


@router.post("/sessions")
async def start(body: StartIn, request: Request, repo: Repo = Depends(get_repo),
                store: SharedStore = Depends(get_store)) -> dict[str, str]:
    s = get_settings()
    await limit(store, f"sessions:{client_key(request)}", s.chat_sessions_per_hour, 3600,
                "Too many new conversations from this network. Please try again later.")
    token = secrets.token_urlsafe(32)
    conv = await repo.create_conversation(body.channel, hash_token(token), body.page_url,
                                          valid_timezone(body.timezone))
    rules = request.app.state.rules
    return {"conversation_id": conv.id, "token": token, "greeting": rules.greeting.strip(),
            "assistant_name": rules.assistant_name}


@router.get("/sessions/{conversation_id}/messages")
async def transcript(conversation_id: str, repo: Repo = Depends(get_repo),
                     x_chat_token: str | None = Header(None)) -> dict[str, Any]:
    conv = await conversation_for(repo, conversation_id, x_chat_token)
    stored = await repo.recent_messages(conv.id, 200)
    out: list[dict[str, str]] = []
    turn_texts: list[str] = []
    for m in stored:
        if m.role == "user":
            if turn_texts:
                out.append({"role": "assistant", "text": join_texts(turn_texts)})
                turn_texts = []
            out.append({"role": "user", "text": m.text})
        elif m.role == "assistant":
            turn_texts.append(m.text)
    if turn_texts:
        out.append({"role": "assistant", "text": join_texts(turn_texts)})
    return {"messages": out, "status": conv.status}


@router.post("/sessions/{conversation_id}/messages")
async def send(conversation_id: str, body: MessageIn, request: Request, repo: Repo = Depends(get_repo),
               assistant: Assistant = Depends(get_assistant), store: SharedStore = Depends(get_store),
               x_chat_token: str | None = Header(None)) -> StreamingResponse:
    s = get_settings()
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Message is empty.")
    if len(text) > s.chat_max_message_chars:
        raise HTTPException(413, f"Messages can be up to {s.chat_max_message_chars} characters.")
    conv = await conversation_for(repo, conversation_id, x_chat_token)
    if conv.turn_count >= s.chat_max_turns_per_session:
        raise HTTPException(429, "This conversation has reached its length limit. Please start a new one.")
    await limit(store, f"messages:{client_key(request)}", s.chat_messages_per_minute, 60,
                "You're sending messages too quickly. Please wait a moment.")

    user_id, _created = await repo.add_user_message(conv.id, text, body.client_message_id)
    existing = await repo.find_reply(conv.id, user_id)
    if existing is not None:   # a retry of a message already answered: same answer, no second reply
        async def replay() -> AsyncIterator[str]:
            yield sse({"type": "delta", "text": existing.text})
            yield sse({"type": "done", "message_id": existing.id, "replayed": True})
        return StreamingResponse(replay(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    lock = f"turn:{conv.id}:{user_id}"
    if not await store.add(lock, "1", TURN_LOCK_S):
        raise HTTPException(409, "Still answering this message.")

    async def stream() -> AsyncIterator[str]:
        try:
            async for event in assistant.run_turn(conv, text, user_id):
                yield sse(event)
        except LLMError as exc:
            log.error("model error in conversation %s: %s", conv.id, exc)
            msg = str(exc) if s.is_development else "Sorry, I'm having trouble answering right now. Please try again."
            yield sse({"type": "error", "message": msg})
        except Exception:
            log.exception("turn failed in conversation %s", conv.id)
            yield sse({"type": "error", "message": "Sorry, something went wrong. Please try again."})
        finally:
            await store.clear(lock)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
