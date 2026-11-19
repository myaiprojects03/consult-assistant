"""Voice calls: Twilio phone webhooks and WebSockets, and browser calls.

Phone (VOICE_PROVIDER=twilio; PUBLIC_API_URL must be this API's https address):
POST /api/voice/twilio/incoming      "A call comes in" webhook: answers with ConversationRelay TwiML
WS   /api/voice/twilio/ws            ConversationRelay session (text in, text out)
POST /api/voice/twilio/after         session ended: put the caller through to a consultant, or hang up
POST /api/voice/twilio/dial-done     result of a transfer (answered or not)
POST /api/voice/twilio/status        call status callbacks (outbound calls)

Browser (always available; uses the browser's own speech recognition and voice):
POST /api/voice/web/sessions         start a browser call: conversation id, token, greeting
WS   /api/voice/web/ws               same messages as the phone session
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.chat import client_key, conversation_for, hash_token, limit, valid_timezone
from app.config import get_settings
from app.db.repo import Repo
from app.integrations.slack import escape
from app.shared.store import SharedStore, get_store
from app.voice.session import VoiceSession
from app.voice.store import Call, CallStore
from app.voice.twilio import dial, say, twiml, valid_call_token, valid_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/voice", tags=["voice"])
SETUP_TIMEOUT_S = 15
MAX_WS_MESSAGE = 8000


def xml(doc: str) -> Response:
    return Response(doc, media_type="application/xml")


async def twilio_form(request: Request) -> dict[str, str]:
    """The posted fields, after checking the request really comes from Twilio (X-Twilio-Signature)."""
    s = get_settings()
    params = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
    url = s.public_api_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if s.voice_provider != "twilio" or not valid_signature(s.twilio_auth_token, url, params,
                                                           request.headers.get("X-Twilio-Signature")):
        log.warning("refused an unsigned or wrongly signed voice webhook to %s", request.url.path)
        raise PermissionError
    return params


def refused() -> Response:
    return Response("Forbidden", status_code=403)


# ---------------------------------------------------------------- phone: Twilio
@router.post("/twilio/incoming")
async def incoming(request: Request) -> Response:
    try:
        form = await twilio_form(request)
    except PermissionError:
        return refused()
    state = request.app.state
    voice = state.voice_config
    call, created = await state.calls.create(Call(id="", direction="inbound", call_sid=form.get("CallSid", ""),
                                                  from_number=form.get("From", ""), to_number=form.get("To", ""),
                                                  status="ringing"))
    if created or not call.conversation_id:
        conv = await state.repo.create_conversation("voice", hash_token(secrets.token_urlsafe(16)), "phone")
        await state.calls.update(call.id, conversation_id=conv.id)
    return xml(voice.relay_doc(call.id, state.assistant.voice_rules.greeting))


@router.websocket("/twilio/ws")
async def twilio_ws(ws: WebSocket) -> None:
    await ws.accept()
    state = ws.app.state
    s = get_settings()
    try:
        setup = json.loads(await asyncio.wait_for(ws.receive_text(), SETUP_TIMEOUT_S))
    except (TimeoutError, WebSocketDisconnect, ValueError):
        await _close(ws, 1008)
        return
    params = setup.get("customParameters") or {}
    call_id, token = str(params.get("call_id", "")), str(params.get("token", ""))
    call = await state.calls.get(call_id) if setup.get("type") == "setup" else None
    if (call is None or not valid_call_token(s.twilio_auth_token, call_id, token) or call.direction == "web"
            or (call.call_sid and call.call_sid != setup.get("callSid")) or not call.conversation_id):
        log.warning("refused a voice session (bad or unknown call token)")
        await _close(ws, 1008)
        return
    if not call.call_sid and setup.get("callSid"):
        await state.calls.update(call.id, call_sid=str(setup["callSid"]))
    await _serve(ws, state, call, transfer_possible=True)


@router.post("/twilio/after")
async def after(request: Request, call: str = "") -> Response:
    """ConversationRelay ended. HandoffData (from our 'end' message) says whether to put the caller through."""
    try:
        form = await twilio_form(request)
    except PermissionError:
        return refused()
    state = request.app.state
    s = get_settings()
    found = await state.calls.get(call)
    try:
        data = json.loads(form.get("HandoffData") or "{}")
    except ValueError:
        data = {}
    if found is None or data.get("action") != "transfer":
        return xml(twiml("<Hangup/>"))
    consultant = state.team.get(str(data.get("consultant_id", "")))
    if not consultant.phone:
        return xml(twiml(say(f"Sorry, I can't put you through right now. {consultant.name} will call you back.",
                             state.assistant.voice_rules.language), "<Hangup/>"))
    return xml(twiml(dial(consultant.phone, caller_id=s.twilio_phone_number,
                          action_url=state.voice_config.url(f"/api/voice/twilio/dial-done?call={found.id}"))))


@router.post("/twilio/dial-done")
async def dial_done(request: Request, call: str = "") -> Response:
    try:
        form = await twilio_form(request)
    except PermissionError:
        return refused()
    state = request.app.state
    found = await state.calls.get(call)
    answered = form.get("DialCallStatus") == "completed"
    if found:
        await state.calls.update(found.id, outcome="transferred" if answered else "transfer_missed",
                                 **({} if found.direction == "outbound" else {"status": "completed"}))
    if answered or found is None:
        return xml(twiml("<Hangup/>"))
    consultant = state.team.get(found.transfer_to)
    await state.jobs.enqueue("slack.note", f"note:missed:{found.id}", {"text": (
        f"*Missed call transfer* for {consultant.mention}: please call back {escape(found.caller_number)}. "
        f"_Conversation {found.conversation_id}_")}, ref=found.id)
    return xml(twiml(say(f"Sorry, {consultant.name} couldn't take the call just now. They'll call you back as "
                         "soon as possible. Goodbye.", state.assistant.voice_rules.language), "<Hangup/>"))


@router.post("/twilio/status")
async def status(request: Request, call: str = "") -> Response:
    try:
        form = await twilio_form(request)
    except PermissionError:
        return refused()
    state = request.app.state
    found = await state.calls.get(call)
    if found is None:
        return Response(status_code=204)
    new = form.get("CallStatus", "").replace("-", "_")
    fields: dict[str, Any] = {"status": new}
    if form.get("CallDuration", "").isdigit():
        fields["duration_s"] = int(form["CallDuration"])
    await state.calls.update(found.id, **fields)
    if found.direction == "outbound" and new in ("busy", "no_answer", "failed"):
        await state.jobs.enqueue("slack.note", f"note:outbound:{found.id}", {"channel": "leads", "text": (
            f"*Outbound call not answered* ({new.replace('_', ' ')}): {escape(found.contact_name or 'contact')}, "
            f"{escape(found.to_number)}, about {escape(found.purpose)}.")}, ref=found.id)
    return Response(status_code=204)


# ---------------------------------------------------------------- browser calls
class WebCallIn(BaseModel):
    page_url: str = Field("", max_length=500)
    timezone: str = Field("", max_length=64)


@router.post("/web/sessions")
async def web_session(body: WebCallIn, request: Request, store: SharedStore = Depends(get_store)) -> dict[str, Any]:
    s = get_settings()
    await limit(store, f"voice:start:{client_key(request)}", s.voice_calls_per_hour, 3600,
                "Too many calls from your network. Please try again later.")
    state = request.app.state
    token = secrets.token_urlsafe(32)
    conv = await state.repo.create_conversation("voice", hash_token(token), body.page_url,
                                                valid_timezone(body.timezone))
    call, _ = await state.calls.create(Call(id="", direction="web", conversation_id=conv.id, status="ringing"))
    rules = state.rules
    return {"conversation_id": conv.id, "call_id": call.id, "token": token,
            "greeting": state.assistant.voice_rules.greeting, "assistant_name": rules.assistant_name,
            "language": state.assistant.voice_rules.language}


@router.websocket("/web/ws")
async def web_ws(ws: WebSocket, conversation_id: str = "", call_id: str = "", token: str = "") -> None:
    s = get_settings()
    origin = (ws.headers.get("origin") or "").rstrip("/")
    if origin not in s.origins:          # browsers send their page's origin; only our sites may open calls
        await ws.close(code=1008)
        return
    state = ws.app.state
    try:
        conv = await conversation_for(state.repo, conversation_id, token, channel="voice")
    except Exception:
        await ws.close(code=1008)
        return
    call = await state.calls.get(call_id)
    if call is None or call.direction != "web" or call.conversation_id != conv.id:
        await ws.close(code=1008)
        return
    if not await state.calls.claim(call.id, ("ringing",), "connecting"):   # one connection per call
        await ws.close(code=1008)
        return
    await ws.accept()
    await _serve(ws, state, call, transfer_possible=False, greeting=state.assistant.voice_rules.greeting)


# ---------------------------------------------------------------- shared
async def _serve(ws: WebSocket, state: Any, call: Call, *, transfer_possible: bool, greeting: str = "") -> None:
    s = get_settings()
    repo: Repo = state.repo
    calls: CallStore = state.calls
    lock = asyncio.Lock()

    async def send(msg: dict[str, Any]) -> None:
        async with lock:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await ws.send_text(json.dumps(msg))

    assert call.conversation_id
    session = VoiceSession(state.assistant, repo, calls, state.jobs, call, call.conversation_id, send,
                           transfer_possible=transfer_possible)
    await session.start(speak_greeting=greeting)
    worker = asyncio.create_task(session.run())
    prompts = 0
    try:
        while not session.ended:
            receive = asyncio.create_task(ws.receive_text())
            done, _ = await asyncio.wait({receive, worker}, return_when=asyncio.FIRST_COMPLETED)
            if receive not in done:          # the session ended the call (transfer or time limit)
                receive.cancel()
                break
            raw = receive.result()
            if len(raw) > MAX_WS_MESSAGE:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "prompt":
                prompts += 1
                if prompts > s.chat_max_turns_per_session:
                    await send({"type": "text", "token": "This call has reached its limit. Goodbye.", "last": True})
                    await send({"type": "end", "handoffData": json.dumps({"action": "hangup", "reason": "limit"})})
                    break
            await session.handle(msg)
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker
        await _close(ws, 1000)


async def _close(ws: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code)
