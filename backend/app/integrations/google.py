"""Google Calendar and Gmail through their REST APIs, signed in as the firm's Google account with an
OAuth refresh token (created once with `python -m scripts.google_auth`).

Calendar: free/busy before offering or booking a time, and events created with a fixed id per booking,
so a retried create is recognised (HTTP 409) instead of producing a second event. Each event gets a
Google Meet link. Guests get Google's own invitation and updates (sendUpdates=all); Google does not send
one to the calendar's own account, so test with a different address.
Gmail: plain-text messages sent from the signed-in account.
"""
from __future__ import annotations

import base64
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.base import Calendar, CalendarEvent, IntegrationError, Interval, Mailer, call

TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
SCOPES = ["https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.freebusy",
          "https://www.googleapis.com/auth/gmail.send"]


class GoogleAuth:
    """Exchanges the refresh token for short-lived access tokens and reuses each until shortly before expiry."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, client: httpx.AsyncClient):
        missing = [n for n, v in (("GOOGLE_CLIENT_ID", client_id), ("GOOGLE_CLIENT_SECRET", client_secret),
                                  ("GOOGLE_REFRESH_TOKEN", refresh_token)) if not v]
        if missing:
            raise IntegrationError(f"Google: {', '.join(missing)} not set (see docs/SETUP.md).", retryable=False)
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.client = client
        self._token = ""
        self._expires = 0.0

    async def headers(self) -> dict[str, str]:
        if not self._token or time.monotonic() > self._expires:
            try:
                r = await self.client.post(TOKEN_URL, data={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
            except httpx.HTTPError as exc:
                raise IntegrationError(f"Google: could not connect ({type(exc).__name__})") from exc
            if r.status_code != 200:
                if "invalid_grant" in r.text:
                    raise IntegrationError("Google: the sign-in has expired or was revoked; run "
                                           "`python -m scripts.google_auth` again.", retryable=False)
                raise IntegrationError(f"Google: token refresh failed (HTTP {r.status_code}): {r.text[:200]}",
                                       retryable=r.status_code >= 500)
            data = r.json()
            self._token = data["access_token"]
            self._expires = time.monotonic() + int(data.get("expires_in", 3600)) - 120
        return {"Authorization": f"Bearer {self._token}"}


def _event_body(e: CalendarEvent) -> dict[str, Any]:
    return {"summary": e.title, "description": e.description,
            "start": {"dateTime": e.start.isoformat(), "timeZone": e.timezone},
            "end": {"dateTime": e.end.isoformat(), "timeZone": e.timezone},
            "attendees": [{"email": em, "displayName": nm} for em, nm in e.attendees],
            "reminders": {"useDefault": True}}


class GoogleCalendar(Calendar):
    name = "Google Calendar"

    def __init__(self, auth: GoogleAuth):
        self.auth = auth

    async def _call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201, 204), **kw: Any) -> httpx.Response:
        return await call(self.auth.client, method, CALENDAR_API + path, service="Google Calendar", ok=ok,
                          headers=await self.auth.headers(), **kw)

    async def busy(self, calendar_ids: list[str], start: datetime, end: datetime) -> dict[str, list[Interval]]:
        r = await self._call("POST", "/freeBusy", json={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": c} for c in calendar_ids]})
        out: dict[str, list[Interval]] = {}
        for cid, info in (r.json().get("calendars") or {}).items():
            if info.get("errors"):
                reason = info["errors"][0].get("reason", "error")
                raise IntegrationError(f"Google Calendar: cannot read calendar '{cid}' ({reason}); check "
                                       "GOOGLE calendar ids in content/team.yaml and sharing.", retryable=False)
            out[cid] = [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                         datetime.fromisoformat(b["end"].replace("Z", "+00:00"))) for b in info.get("busy", [])]
        # "primary" may come back under the account's address; map unanswered ids to the only answer
        for cid in calendar_ids:
            if cid not in out and len(out) == 1:
                out[cid] = next(iter(out.values()))
        return out

    async def create_event(self, event: CalendarEvent) -> str:
        cal = quote(event.calendar_id, safe="")
        meet = {"createRequest": {"requestId": event.event_id, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        r = await self._call("POST", f"/calendars/{cal}/events", ok=(200, 201, 409),
                             params={"sendUpdates": "all", "conferenceDataVersion": "1"},
                             json={"id": event.event_id, **_event_body(event), "conferenceData": meet})
        if r.status_code == 409:      # created by an earlier attempt: make sure it has the current details
            return await self.move_event(event)
        return str(r.json().get("hangoutLink", ""))

    async def move_event(self, event: CalendarEvent) -> str:
        cal, eid = quote(event.calendar_id, safe=""), quote(event.event_id, safe="")
        r = await self._call("PATCH", f"/calendars/{cal}/events/{eid}", params={"sendUpdates": "all"},
                             json={**_event_body(event), "status": "confirmed"})
        return str(r.json().get("hangoutLink", "")) if r.content else ""

    async def cancel_event(self, calendar_id: str, event_id: str) -> None:
        cal, eid = quote(calendar_id, safe=""), quote(event_id, safe="")
        await self._call("DELETE", f"/calendars/{cal}/events/{eid}", ok=(200, 204, 404, 410),
                         params={"sendUpdates": "all"})


def build_mime(*, from_name: str, from_address: str, to_email: str, to_name: str, subject: str,
               text: str) -> bytes:
    msg = EmailMessage()
    # Header values never contain line breaks (header injection).
    clean = {k: " ".join(v.split()) for k, v in (("to_name", to_name), ("subject", subject), ("from", from_name))}
    msg["To"] = formataddr((clean["to_name"], to_email.strip()))
    if from_address:      # without it Gmail uses the signed-in account's address and name
        msg["From"] = formataddr((clean["from"], from_address.strip()))
    msg["Subject"] = clean["subject"]
    msg.set_content(text)
    return msg.as_bytes()


class GmailMailer(Mailer):
    name = "Gmail"

    def __init__(self, auth: GoogleAuth, from_name: str, from_address: str = ""):
        self.auth, self.from_name, self.from_address = auth, from_name, from_address

    async def send(self, *, to_email: str, to_name: str, subject: str, text: str) -> None:
        raw = base64.urlsafe_b64encode(build_mime(from_name=self.from_name, from_address=self.from_address,
                                                  to_email=to_email, to_name=to_name, subject=subject,
                                                  text=text)).decode()
        await call(self.auth.client, "POST", f"{GMAIL_API}/users/me/messages/send", service="Gmail",
                   headers=await self.auth.headers(), json={"raw": raw})
