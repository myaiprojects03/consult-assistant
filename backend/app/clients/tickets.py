"""Client tickets: raised and followed up in the chat, worked on in the ticketing system (Jira).

The ticket is stored at once with our reference (TK-...), so the client has it immediately; the Jira
issue, comments and Slack messages follow as background jobs. Anything urgent, any security issue, and
any high-priority request from an enterprise account is handed straight to the client's consultant.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from app.clients.store import Client, ClientStore, Ticket
from app.integrations.base import IntegrationError, Ticketing
from app.jobs.runner import Jobs

log = logging.getLogger(__name__)
CATEGORIES = ("outage", "security", "access", "change_request", "question", "billing", "other")


def handover_reason(client: Client, priority: str, category: str) -> str | None:
    if category == "security":
        return "security_incident"
    if client.tier == "enterprise" and priority in ("high", "urgent"):
        return "enterprise_account"
    if priority == "urgent":
        return "urgent_ticket"
    return None


@dataclass
class TicketService:
    store: ClientStore
    ticketing: Ticketing
    jobs: Jobs

    async def create(self, *, client: Client, conversation_id: str | None, contact_email: str, summary: str,
                     details: str, priority: str, category: str) -> tuple[Ticket, str | None]:
        """Returns (ticket, handover reason or None)."""
        t = await self.store.create_ticket(Ticket(
            id="", reference="", client_id=client.id, contact_email=contact_email, summary=summary,
            details=details, priority=priority, category=category, conversation_id=conversation_id))
        await self.jobs.enqueue("ticket.create", f"ticket:create:{t.id}", {"ticket_id": t.id}, ref=t.id)
        reason = handover_reason(client, priority, category)
        if reason is None:
            await self.jobs.enqueue("slack.ticket", f"slack:ticket:{t.id}", {"ticket_id": t.id}, ref=t.id)
        return t, reason

    async def add_update(self, t: Ticket, message: str, author: str) -> None:
        marker = "upd-" + hashlib.sha256(f"{t.id}:{message}".encode()).hexdigest()[:10]
        await self.jobs.enqueue("ticket.comment", f"ticket:comment:{t.id}:{marker}",
                                {"ticket_id": t.id, "message": f"Update from {author}: {message}", "marker": marker},
                                ref=t.id)
        await self.store.update_ticket(t.id)

    async def refresh(self, t: Ticket) -> tuple[Ticket, str]:
        """Live status from the ticketing system (falls back to the stored one). Returns (ticket, wording)."""
        if not t.external_key:
            return t, t.status.replace("_", " ")
        try:
            state = await self.ticketing.state(t.external_key)
        except IntegrationError as exc:
            log.warning("ticket %s status unavailable: %s", t.reference, exc)
            return t, t.status.replace("_", " ")
        if state is None:
            return t, t.status.replace("_", " ")
        if state.stage != t.status and not (t.status in ("waiting", "closed") and state.stage != "resolved"):
            await self.store.update_ticket(t.id, status=state.stage)
            t.status = state.stage
        wording = state.status or t.status
        if state.assignee:
            wording += f", with {state.assignee}"
        return t, wording
