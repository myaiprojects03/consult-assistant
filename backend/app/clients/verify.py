"""Proving a visitor speaks for an existing client: a 6-digit code sent to a known contact email.

- The answer is the same whether or not the email belongs to a client, so the chat cannot be used to
  find out who our clients are.
- The code is stored only as a hash, expires after 10 minutes, allows 5 tries, and sending is limited
  per conversation and per email address.
- Once confirmed, the conversation is bound to that client; every client tool reads the client from
  the conversation, never from what the visitor or the model typed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from app.clients.store import Client, ClientStore
from app.integrations.base import IntegrationError, Mailer
from app.shared.store import SharedStore

CODE_TTL_S = 600
MAX_TRIES = 5
SENDS_PER_CONVERSATION_HOUR = 3
SENDS_PER_EMAIL_DAY = 6

SENT = ("If that address belongs to one of our client accounts, a 6-digit code is on its way to it (valid for "
        "10 minutes). Ask the visitor to type the code here.")


def _digest(conversation_id: str, code: str) -> str:
    return hashlib.sha256(f"{conversation_id}:{code}".encode()).hexdigest()


@dataclass
class Verifier:
    store: SharedStore
    clients: ClientStore
    mailer: Mailer
    firm_name: str

    async def send(self, conversation_id: str, email: str) -> str:
        email = email.strip().lower()
        if await self.store.hit(f"verify:conv:{conversation_id}", 3600) > SENDS_PER_CONVERSATION_HOUR:
            return "Too many codes requested in this chat. Ask the visitor to try again later, or hand over."
        if await self.store.hit(f"verify:email:{email}", 86400) > SENDS_PER_EMAIL_DAY:
            return SENT                                # same answer: no hint either way
        client = await self.clients.client_for_email(email)
        if client is None:
            return SENT
        code = f"{secrets.randbelow(10 ** 6):06d}"
        await self.store.put(f"verify:code:{conversation_id}", json.dumps(
            {"email": email, "client_id": client.id, "digest": _digest(conversation_id, code), "tries": 0}),
            CODE_TTL_S)
        try:
            await self.mailer.send(
                to_email=email, to_name="", subject=f"Your {self.firm_name} verification code: {code}",
                text=(f"Your code is {code}\n\nType it into the chat on our website to continue. It expires in 10 "
                      f"minutes.\nIf you did not ask for it, you can ignore this email.\n\n{self.firm_name}\n"))
        except IntegrationError:
            await self.store.take(f"verify:code:{conversation_id}")
            return "The code could not be sent just now. Apologise and offer to connect them with a consultant."
        return SENT

    async def confirm(self, conversation_id: str, code: str) -> tuple[Client | None, str]:
        """(client, email) on success; (None, reason) otherwise."""
        raw = await self.store.take(f"verify:code:{conversation_id}")    # taken: no parallel guessing
        if raw is None:
            return None, "No code is waiting (none was sent, or it expired). Offer to send a new one."
        data = json.loads(raw)
        given = "".join(ch for ch in code if ch.isdigit())
        if hmac.compare_digest(_digest(conversation_id, given), data["digest"]):
            client = await self.clients.get_client(data["client_id"])
            return (client, data["email"]) if client else (None, "That account is no longer active.")
        data["tries"] += 1
        if data["tries"] >= MAX_TRIES:
            return None, "Too many wrong codes. The code is cancelled; offer to send a new one later."
        await self.store.put(f"verify:code:{conversation_id}", json.dumps(data), CODE_TTL_S)
        return None, f"That code is not right ({MAX_TRIES - data['tries']} tries left). Ask them to check it."
