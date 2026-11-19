"""Twilio Programmable Voice with ConversationRelay, and Twilio SMS.

Twilio does speech-to-text, text-to-speech and interruption handling (the caller can talk over the
assistant); this server only exchanges text with it over a WebSocket (app/voice/session.py).

Security:
- Every webhook from Twilio is checked against its X-Twilio-Signature (HMAC-SHA1 with the auth token over
  the public URL and the posted fields). Unsigned or wrongly signed requests are refused.
- The WebSocket is opened by Twilio with a token we put in the TwiML (<Parameter>), bound to our call id
  with an HMAC; the session refuses to start without it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.integrations.base import IntegrationError, Telephony, TextMessages, call

API = "https://api.twilio.com/2010-04-01"
STATUS_EVENTS = ("initiated", "ringing", "answered", "completed")


def signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def valid_signature(auth_token: str, url: str, params: Mapping[str, str], given: str | None) -> bool:
    if not auth_token or not given:
        return False
    return hmac.compare_digest(signature(auth_token, url, params), given)


def call_token(secret: str, call_id: str) -> str:
    """Our own proof that a WebSocket session belongs to a call we answered or placed."""
    return hmac.new(secret.encode(), f"voice-call:{call_id}".encode(), hashlib.sha256).hexdigest()[:40]


def valid_call_token(secret: str, call_id: str, token: str) -> bool:
    return bool(secret and call_id and token) and hmac.compare_digest(call_token(secret, call_id), token)


def say(text: str, language: str = "en-GB") -> str:
    return f"<Say language={quoteattr(language)}>{escape(text)}</Say>"


def twiml(*verbs: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


def relay(*, ws_url: str, action_url: str, greeting: str, language: str, tts_provider: str, voice: str,
          transcription_provider: str, parameters: Mapping[str, str]) -> str:
    """<Connect><ConversationRelay>: Twilio speaks the greeting, then sends what the caller says to ws_url.
    When the session ends (hang-up, or our 'end' message for a transfer), Twilio posts to action_url."""
    attrs = {"url": ws_url, "welcomeGreeting": greeting, "welcomeGreetingInterruptible": "any",
             "language": language, "ttsProvider": tts_provider, "transcriptionProvider": transcription_provider,
             "interruptible": "any", "dtmfDetection": "true"}
    if voice:
        attrs["voice"] = voice
    params = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in parameters.items())
    attr_text = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
    return (f"<Connect action={quoteattr(action_url)}><ConversationRelay {attr_text}>{params}"
            "</ConversationRelay></Connect>")


def dial(number: str, *, caller_id: str, action_url: str, timeout_s: int = 20) -> str:
    return (f"<Dial timeout={quoteattr(str(timeout_s))} callerId={quoteattr(caller_id)} action={quoteattr(action_url)}>"
            f"<Number>{escape(number)}</Number></Dial>")


@dataclass(frozen=True)
class VoiceConfig:
    """What the TwiML and webhooks need: this API's public address and how Twilio should speak and listen."""
    public_url: str                    # https://api.example.com (PUBLIC_API_URL)
    auth_token: str
    from_number: str
    language: str = "en-GB"
    tts_provider: str = "ElevenLabs"
    voice: str = ""
    transcription_provider: str = "Deepgram"

    def url(self, path: str) -> str:
        return self.public_url.rstrip("/") + path

    @property
    def ws_url(self) -> str:
        base = self.public_url.rstrip("/")
        return base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/voice/twilio/ws"

    def relay_doc(self, call_id: str, greeting: str) -> str:
        return twiml(relay(ws_url=self.ws_url, action_url=self.url(f"/api/voice/twilio/after?call={call_id}"),
                           greeting=greeting, language=self.language, tts_provider=self.tts_provider,
                           voice=self.voice, transcription_provider=self.transcription_provider,
                           parameters={"call_id": call_id, "token": call_token(self.auth_token, call_id)}))


class TwilioClient:
    """The two REST calls we need: place a call, send an SMS."""
    name = "Twilio"

    def __init__(self, account_sid: str, auth_token: str, from_number: str,
                 client: httpx.AsyncClient | None = None):
        missing = [n for n, v in (("TWILIO_ACCOUNT_SID", account_sid), ("TWILIO_AUTH_TOKEN", auth_token),
                                  ("TWILIO_PHONE_NUMBER", from_number)) if not v]
        if missing:
            raise IntegrationError(f"Twilio: {', '.join(missing)} not set (see docs/SETUP.md).", retryable=False)
        self.sid, self.from_number = account_sid, from_number
        self.client = client or httpx.AsyncClient(timeout=20, auth=(account_sid, auth_token))

    async def create_call(self, *, to: str, twiml_doc: str, status_url: str, timeout_s: int = 30) -> str:
        data: list[tuple[str, Any]] = [("To", to), ("From", self.from_number), ("Twiml", twiml_doc),
                                       ("StatusCallback", status_url), ("StatusCallbackMethod", "POST"),
                                       ("Timeout", str(timeout_s))]
        data += [("StatusCallbackEvent", e) for e in STATUS_EVENTS]
        r = await call(self.client, "POST", f"{API}/Accounts/{self.sid}/Calls.json", service="Twilio", data=data)
        return str(r.json()["sid"])

    async def send_sms(self, to: str, body: str) -> str:
        r = await call(self.client, "POST", f"{API}/Accounts/{self.sid}/Messages.json", service="Twilio",
                       data={"To": to, "From": self.from_number, "Body": body[:1500]})
        return str(r.json()["sid"])

    async def aclose(self) -> None:
        await self.client.aclose()


class TwilioSMS(TextMessages):
    name = "Twilio SMS"

    def __init__(self, client: TwilioClient):
        self.twilio = client

    async def send(self, to: str, text: str) -> None:
        await self.twilio.send_sms(to, text)


class TwilioTelephony(Telephony):
    name = "Twilio"

    def __init__(self, client: TwilioClient):
        self.twilio = client

    async def create_call(self, *, to: str, twiml_doc: str, status_url: str) -> str:
        return await self.twilio.create_call(to=to, twiml_doc=twiml_doc, status_url=status_url)
