"""Tools the model can call. Every input is validated here; the model never writes to storage directly,
and every tool returns text (errors included) instead of raising, so a failure never ends the chat."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from app.booking.service import BookingService, CalendarUnavailable, NotBookable
from app.booking.slots import describe, zone
from app.booking.store import AlreadyBooked, Booking
from app.booking.team import Team
from app.clients.store import PRIORITIES, ClientStore
from app.clients.tickets import CATEGORIES, TicketService
from app.clients.verify import Verifier
from app.db.repo import LEAD_FIELDS, Lead, Repo
from app.jobs.runner import Jobs
from app.kb.content import Rules
from app.kb.embeddings import Embedder
from app.llm.base import ToolSpec
from app.voice.rules import VoiceRules

log = logging.getLogger(__name__)
MAX_FIELD_CHARS = 300
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
SPOKEN_EMAIL = [   # how people say an address out loud (speech recognition writes the words)
    (r"\s*\b(?:at the rate of|at the rate|at sign|at symbol|at)\b\s*", "@"),
    (r"\s*\b(?:dot|full stop|period|point)\b\s*", "."),
    (r"\s*\b(?:underscore|under score)\b\s*", "_"),
    (r"\s*\b(?:dash|hyphen|minus)\b\s*", "-"),
]


def email_from_speech(value: str) -> str:
    """'tanya dot k at the rate gmail dot com' -> 'tanya.k@gmail.com'. Unchanged if it is already an address."""
    v = value.strip().lower()
    if EMAIL_RE.match(v):
        return v
    for pattern, sub in SPOKEN_EMAIL:
        v = re.sub(pattern, sub, v)
    return re.sub(r"\s+", "", v).strip(".")
HANDOVER_REASONS = ["security_incident", "pricing_negotiation", "legal_or_complaint", "enterprise_account",
                    "urgent_ticket", "human_requested", "cannot_help", "other"]
_STR = {"type": "string"}

TOOLS = [
    ToolSpec("search_knowledge",
             "Search the firm's approved information (services, FAQs, policies, company facts). "
             "Use it before answering any question about the firm.",
             {"type": "object", "properties": {"query": {"type": "string", "description": "What to look up"}},
              "required": ["query"]}),
    ToolSpec("update_lead",
             "Save what the visitor has told you about themselves and their needs, as soon as they say it. "
             "A broad answer counts; update it later if they add detail. Send only new or changed fields.",
             {"type": "object", "properties": {f: _STR for f in LEAD_FIELDS}}),
    ToolSpec("find_available_slots",
             "Free times for a discovery call, shown in the visitor's time zone. Use before offering times.",
             {"type": "object", "properties": {
                 "date_from": {"type": "string", "description": "YYYY-MM-DD, first day the visitor wants"},
                 "date_to": {"type": "string", "description": "YYYY-MM-DD, last day (same as date_from for one day)"},
                 "preferred_time": {"type": "string", "description": "HH:MM the visitor prefers, if any"},
                 "timezone": {"type": "string", "description": "IANA time zone the visitor named, e.g. "
                                                               "Europe/London; leave out to use their browser's"}}}),
    ToolSpec("book_discovery_call",
             "Book the discovery call at a time from find_available_slots. Needs the visitor's name and email "
             "saved with update_lead first.",
             {"type": "object", "properties": {
                 "start": {"type": "string", "description": "the slot's start value, copied exactly"},
                 "notes": {"type": "string", "description": "anything the consultant should know"}},
              "required": ["start"]}),
    ToolSpec("reschedule_discovery_call",
             "Move a booked discovery call to a new time from find_available_slots. For a call booked in an "
             "earlier chat, give the booking reference and the email it was booked with.",
             {"type": "object", "properties": {"new_start": _STR, "reference": _STR, "email": _STR},
              "required": ["new_start"]}),
    ToolSpec("cancel_discovery_call",
             "Cancel a booked discovery call. For a call booked in an earlier chat, give the booking reference "
             "and the email it was booked with.",
             {"type": "object", "properties": {"reference": _STR, "email": _STR, "reason": _STR}}),
    ToolSpec("handover_to_human",
             "Hand the conversation to a consultant. Use for the situations listed in your instructions.",
             {"type": "object", "properties": {
                 "reason": {"type": "string", "enum": HANDOVER_REASONS},
                 "summary": {"type": "string", "description": "2 to 4 factual sentences for the consultant"}},
              "required": ["reason", "summary"]}),
]

# Existing clients. Before verification only verify_client is offered; the others appear once the
# conversation is bound to a client (see tools_for), so an unverified visitor cannot even ask for them.
VERIFY_TOOL = ToolSpec(
    "verify_client",
    "For an existing client: with their email, send a one-time code to it; with the code they type, confirm it. "
    "Required before discussing or changing anything about their account.",
    {"type": "object", "properties": {"email": _STR, "code": _STR}})
CLIENT_TOOLS = [
    ToolSpec("client_overview", "The verified client's projects and recent tickets, with live ticket status.",
             {"type": "object", "properties": {}}),
    ToolSpec("raise_ticket", "Raise a support ticket for the verified client.",
             {"type": "object", "properties": {
                 "summary": {"type": "string", "description": "one line"},
                 "details": {"type": "string", "description": "what happened, what is affected, since when"},
                 "priority": {"type": "string", "enum": list(PRIORITIES)},
                 "category": {"type": "string", "enum": list(CATEGORIES)}},
              "required": ["summary", "priority", "category"]}),
    ToolSpec("update_ticket", "Add information from the client to one of their tickets.",
             {"type": "object", "properties": {"reference": _STR, "message": _STR},
              "required": ["reference", "message"]}),
]


# Phone and browser calls only: hand the caller to a consultant live (or arrange a call back).
TRANSFER_TOOL = ToolSpec(
    "transfer_call",
    "On a call: connect the caller to a consultant now (in office hours, when a direct line exists), or arrange a "
    "call back. Use when they ask for a person, or a handover situation applies. Also records the handover.",
    {"type": "object", "properties": {
        "reason": {"type": "string", "enum": HANDOVER_REASONS},
        "summary": {"type": "string", "description": "short factual summary for the consultant"}},
     "required": ["reason", "summary"]})


def tools_for(client_verified: bool, voice: bool = False) -> list[ToolSpec]:
    tools = TOOLS + (CLIENT_TOOLS if client_verified else [VERIFY_TOOL])
    return tools + [TRANSFER_TOOL] if voice else tools


def _clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip()[:MAX_FIELD_CHARS]


def employees(size: str) -> int | None:
    """'about 45 people' -> 45; '10-50' -> 10 (the lower bound decides fit); None if no number."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", size)]
    return min(nums) if nums else None


SIZE_PATTERNS = [
    re.compile(r"\bteam of (?:about |around |roughly )?(\d[\d,]*)\b", re.I),
    re.compile(r"\b(\d[\d,]*(?:\s*(?:-|to)\s*\d[\d,]*)?)[\s-]*(?:people|persons|employees|staff|person)\b", re.I),
]


def size_from_text(text: str) -> str | None:
    """Company size stated in a visitor message ('a team of 3', 'about 40 people', '10-50 employees').
    Read in code so fit is known even when the model does not save it; the model can still correct it."""
    for pattern in SIZE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return None


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", text)}


async def stated_by_visitor(ctx: ToolContext, value: str) -> bool:
    """A number the model saves (company size) must be one the visitor wrote or said. Seen live: the model
    asked for the size, then answered its own question with a made-up one."""
    wanted = _numbers(value)
    if not wanted:          # a broad answer ("a small team") has nothing to check
        return True
    said = await ctx.repo.recent_messages(ctx.conversation_id, 60)
    return wanted <= _numbers(" ".join(m.text for m in said if m.role == "user"))


def assess(lead: Lead, rules: Rules) -> Lead:
    lead.missing = [f for f in rules.qualification_fields if not getattr(lead, f, "")]
    size = employees(lead.company_size) if lead.company_size else None
    min_emp = int(rules.fit.get("min_employees", 0) or 0)
    if size is not None and size < min_emp:
        lead.status = "not_a_fit"
    elif not lead.missing:
        lead.status = "qualified"
    else:
        lead.status = "new"
    return lead


@dataclass
class ToolContext:
    repo: Repo
    embedder: Embedder
    rules: Rules
    conversation_id: str
    booking: BookingService
    jobs: Jobs
    team: Team
    visitor_timezone: str = ""
    now: datetime | None = None          # fixed in tests
    clients: ClientStore | None = None
    verifier: Verifier | None = None
    tickets: TicketService | None = None
    client_id: str | None = None         # set when the conversation is verified as an existing client
    verified_email: str = ""
    channel: str = "web"                 # web | voice
    caller_number: str = ""              # voice: the other party's phone number
    transfer_possible: bool = False      # voice over the phone network (not browser calls)
    voice_rules: VoiceRules | None = None
    transfer: dict[str, str] | None = None   # set by transfer_call; the call session carries it out
    sends_email: bool = True             # False: email and invitations are logged stand-ins (a demo)


# ---------------------------------------------------------------- follow-up work (background jobs)
async def after_lead_saved(ctx: ToolContext, before_status: str, lead: Lead) -> None:
    cid = ctx.conversation_id
    if lead.status == "qualified" and before_status != "qualified":
        await ctx.jobs.enqueue("conversation.summarize", f"summary:qualified:{cid}",
                               {"conversation_id": cid, "purpose": "qualified"}, ref=cid)
    elif lead.email:
        await ctx.jobs.enqueue("crm.sync", f"crm:{cid}:{datetime.now().timestamp()}", {"conversation_id": cid},
                               ref=cid, coalesce=True)


async def after_handover(jobs: Jobs, conversation_id: str, reason: str, ticket_id: str | None = None) -> None:
    # The consultant is told at once (urgent issues must not wait for an AI summary); the summary follows
    # for the CRM.
    payload = {"conversation_id": conversation_id, "reason": reason}
    if ticket_id:
        payload["ticket_id"] = ticket_id
    await jobs.enqueue("slack.handover", f"slack.handover:{conversation_id}:{reason}:{ticket_id or ''}",
                       payload, ref=conversation_id, max_attempts=8)
    await jobs.enqueue("conversation.summarize", f"summary:handover:{conversation_id}:{reason}",
                       {"conversation_id": conversation_id, "purpose": "handover", "reason": reason},
                       ref=conversation_id)


# ---------------------------------------------------------------- booking helpers
def _visitor_tz(ctx: ToolContext, args: dict[str, Any], booking: Booking | None = None) -> ZoneInfo:
    named = _clean(args.get("timezone", ""))
    return zone(named or ctx.visitor_timezone or (booking.visitor_timezone if booking else ""),
                ctx.booking.cfg.timezone)


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(_clean(v)[:10]) if v else None
    except ValueError:
        return None


def _time(v: Any) -> time | None:
    try:
        return time.fromisoformat(_clean(v)[:5]) if v else None
    except ValueError:
        return None


def _start(v: Any, tz: ZoneInfo) -> datetime | None:
    try:
        dt = datetime.fromisoformat(_clean(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _hours(ctx: ToolContext) -> str:
    c = ctx.booking.cfg
    return (f"Calls can be booked up to {c.days_ahead} days ahead, at least {c.min_notice_hours} hours' notice, "
            f"{c.start:%H:%M}-{c.end:%H:%M} office time ({c.timezone}) on working days.")


async def _find_booking(ctx: ToolContext, args: dict[str, Any]) -> Booking | None:
    own = await ctx.booking.store.active_for_conversation(ctx.conversation_id)
    if own:
        return own
    ref, email = _clean(args.get("reference", "")), _clean(args.get("email", "")).lower()
    if not ref or not email:
        return None
    b = await ctx.booking.store.by_reference(ref)
    # both must match: a reference alone is not enough to change someone's booking
    return b if b and b.status == "confirmed" and b.email.lower() == email else None


NO_BOOKING = ("Error: no booked call found. Ask for the booking reference (it looks like DC-7K3M9Q, in the "
              "confirmation email) and the email address it was booked with.")
CALENDAR_DOWN = ("The calendar cannot be checked right now. Do not offer or confirm times. Tell the visitor a "
                 "consultant will email them to arrange the call (make sure their name and email are saved).")


async def _calendar_down(ctx: ToolContext, requested: str) -> str:
    await ctx.repo.add_call_request(ctx.conversation_id, requested, ctx.visitor_timezone, "calendar unavailable")
    await ctx.jobs.enqueue("conversation.summarize", f"summary:handover:{ctx.conversation_id}:cannot_help",
                           {"conversation_id": ctx.conversation_id, "purpose": "handover",
                            "reason": "cannot_help"}, ref=ctx.conversation_id)
    return CALENDAR_DOWN


async def _client_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    assert ctx.client_id and ctx.clients and ctx.tickets
    client = await ctx.clients.get_client(ctx.client_id)
    if client is None:
        return "This client account is no longer active; offer to connect them with a consultant."
    if name == "client_overview":
        manager = ctx.team.get(client.account_manager).name if client.account_manager else "our team"
        lines = [f"Client: {client.name}. Their consultant: {manager}."]
        projects = await ctx.clients.projects(client.id)
        lines.append("Projects:" if projects else "Projects: none on record.")
        for p in projects:
            nxt = f"; next: {p.next_milestone}" + (f" by {p.milestone_date:%d %b %Y}" if p.milestone_date else "")
            update = f". Update: {p.client_update}" if p.client_update else ""
            lines.append(f"- {p.name}: {p.status.replace('_', ' ')}" + (f", {p.phase}" if p.phase else "")
                         + (nxt if p.next_milestone else "") + update)
        tickets = await ctx.clients.tickets(client.id, limit=6)
        lines.append("Recent tickets:" if tickets else "Recent tickets: none.")
        for t in tickets:
            if t.status in ("open", "in_progress", "waiting"):
                t, wording = await ctx.tickets.refresh(t)
            else:
                wording = t.status.replace("_", " ")
            lines.append(f"- {t.reference} ({t.created_at:%d %b}): {t.summary}. Status: {wording}.")
        return "\n".join(lines) + "\nShare only what they ask about, in plain words."
    if name == "raise_ticket":
        summary = _clean(args.get("summary", ""))[:200]
        if not summary:
            return "Error: give a one-line summary."
        priority = args.get("priority") if args.get("priority") in PRIORITIES else "normal"
        category = args.get("category") if args.get("category") in CATEGORIES else "other"
        details = str(args.get("details", "")).strip()[:2000]
        ticket, reason = await ctx.tickets.create(client=client, conversation_id=ctx.conversation_id,
                                                  contact_email=ctx.verified_email, summary=summary, details=details,
                                                  priority=str(priority), category=str(category))
        await ctx.repo.log_event(ctx.conversation_id, "ticket_raised",
                                 {"ticket": ticket.reference, "priority": priority, "handover": reason})
        if reason:
            await ctx.repo.escalate(ctx.conversation_id, reason, f"Ticket {ticket.reference}: {summary}")
            await after_handover(ctx.jobs, ctx.conversation_id, reason, ticket.id)
            who = ctx.team.get(client.account_manager).name if client.account_manager else "a consultant"
            return (f"Ticket {ticket.reference} raised and sent straight to {who} as {reason.replace('_', ' ')}. "
                    "Tell the visitor someone will contact them shortly, and give the reference.")
        return (f"Ticket {ticket.reference} raised ({priority}). Give the visitor the reference; they can ask for "
                "its status here any time.")
    # update_ticket
    ref = _clean(args.get("reference", ""))
    message = str(args.get("message", "")).strip()[:2000]
    found = await ctx.clients.ticket_by_reference(client.id, ref) if ref else None
    if found is None:
        return "No ticket with that reference on this account. Use client_overview to see their tickets."
    if not message:
        return "Error: say what to add."
    await ctx.tickets.add_update(found, message, ctx.verified_email)
    return f"Added their update to {found.reference}."


async def _transfer(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.channel != "voice":
        return "Error: transfer_call is only for calls; use handover_to_human."
    reason = str(args.get("reason")) if args.get("reason") in HANDOVER_REASONS else "human_requested"
    summary = _clean(args.get("summary", ""))[:MAX_FIELD_CHARS]
    consultant = ctx.team.for_handover(reason)
    client = await ctx.clients.get_client(ctx.client_id) if ctx.client_id and ctx.clients else None
    if client and client.account_manager:
        consultant = ctx.team.get(client.account_manager)           # clients go to their own consultant
    first = ctx.transfer is None
    await ctx.repo.escalate(ctx.conversation_id, reason, summary)
    await ctx.repo.log_event(ctx.conversation_id, "handover", {"reason": reason, "by": "model", "via": "call"})
    if first:
        await after_handover(ctx.jobs, ctx.conversation_id, reason)
    rules = ctx.voice_rules or VoiceRules()
    open_now = rules.transfer_hours.is_open(ctx.now or datetime.now(UTC), ctx.booking.cfg.tz)
    if ctx.transfer_possible and open_now and consultant.phone:
        ctx.transfer = {"consultant_id": consultant.id, "name": consultant.name, "phone": consultant.phone,
                        "reason": reason}
        await ctx.repo.log_event(ctx.conversation_id, "transfer_requested", {"consultant": consultant.id})
        return (f"Transfer ready. Say one short sentence that you are connecting them to {consultant.name} now, "
                "and nothing else: the call is put through when you finish speaking.")
    why = ("it is outside office hours" if not open_now else
           "this is a browser call" if not ctx.transfer_possible else "they have no direct line set up")
    number = f" on {ctx.caller_number}" if ctx.caller_number else ""
    return (f"No live transfer ({why}). {consultant.name} has been told and will call them back{number}. Tell the "
            "caller this plainly, make sure you have their name, and ask if anything else is needed meanwhile.")


# ---------------------------------------------------------------- the tools
async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    """Execute one tool call and return its result as text for the model. Never raises."""
    try:
        if name == "search_knowledge":
            query = _clean(args.get("query", ""))
            if not query:
                return "Error: query is empty."
            vec = await ctx.embedder.embed([query])
            hits = await ctx.repo.kb_search(query=query, embedding=vec[0] if vec else None, audience="public", k=4)
            await ctx.repo.log_event(ctx.conversation_id, "kb_search",
                                     {"query": query, "hits": [h.slug for h in hits]})
            if not hits:
                return "No approved information found for this question."
            return "\n\n".join(f"[{h.title}]\n{h.content}" for h in hits)

        if name == "update_lead":
            lead: Lead = await ctx.repo.get_lead(ctx.conversation_id) or Lead(ctx.conversation_id)
            before = lead.status
            changed, refused = [], []
            for f in LEAD_FIELDS:
                if f in args and _clean(args[f]):
                    value = _clean(args[f])
                    if f == "company_size" and not await stated_by_visitor(ctx, value):
                        refused.append(f)
                        continue
                    if f == "email":
                        value = email_from_speech(value)
                        if not EMAIL_RE.match(value):
                            return f"Error: '{value}' is not a valid email address. Ask the visitor to check it."
                    setattr(lead, f, value)
                    changed.append(f)
            await ctx.repo.save_lead(assess(lead, ctx.rules))
            await ctx.repo.log_event(ctx.conversation_id, "lead_updated", {"fields": changed, "status": lead.status})
            await after_lead_saved(ctx, before, lead)
            note = ""
            if refused:
                note += (" Company size NOT saved: the visitor has not said how many people work there. Ask them and"
                         " wait for their answer; never assume it or answer for them.")
            if lead.status == "not_a_fit":
                note = f" Note: {ctx.rules.fit.get('note', '')} Be polite and still answer their questions."
            if ctx.channel == "voice" and "email" in changed:
                local, _, domain = lead.email.partition("@")
                note += (f" On a call, first read the email back before anything else: \"{' '.join(local)} at "
                         f"{domain.replace('.', ' dot ')}\", and ask if that's right. Speech recognition often "
                         "gets emails wrong (for example 'one' for '1'); save the corrected one if they say no.")
            return (f"Saved {', '.join(changed) or 'nothing new'}. Status: {lead.status}. "
                    f"Still missing: {', '.join(lead.missing) or 'nothing'}.{note}")

        if name == "find_available_slots":
            existing = await ctx.booking.store.active_for_conversation(ctx.conversation_id)
            tz = _visitor_tz(ctx, args, existing)
            visitor = await ctx.repo.get_lead(ctx.conversation_id)
            consultant = (ctx.team.get(existing.consultant_id) if existing
                          else ctx.team.for_need(visitor.need if visitor else ""))
            try:
                offers = await ctx.booking.offers(
                    consultant=consultant, visitor_tz=tz, day_from=_date(args.get("date_from")),
                    day_to=_date(args.get("date_to")), preferred=_time(args.get("preferred_time")),
                    exclude_id=existing.id if existing else None, now=ctx.now)
            except CalendarUnavailable:
                wanted = _clean(f"{args.get('date_from', '')} {args.get('preferred_time', '')}")
                return await _calendar_down(ctx, wanted)
            if not offers:
                return f"No free times in that range. {_hours(ctx)} Ask which other days suit them."
            lines = [f"- {o.label} | start={o.start.astimezone(tz).isoformat()}" for o in offers]
            already = (f"Note: this visitor already has a call booked ({existing.reference}); use "
                       "reschedule_discovery_call to move it.\n") if existing else ""
            return (f"{already}Free times in the visitor's time zone ({tz.key}):\n" + "\n".join(lines)
                    + "\nOffer two or three of these in plain words (the label only; never show a start value). "
                    "When they choose, pass that start value exactly.")

        if name == "book_discovery_call":
            person = await ctx.repo.get_lead(ctx.conversation_id)
            if ctx.client_id and ctx.verified_email and (not person or not person.email):
                person = person or Lead(ctx.conversation_id)
                person.email = ctx.verified_email          # verified clients do not repeat their email
                await ctx.repo.save_lead(assess(person, ctx.rules))
            if not person or not person.name or not person.email:
                return "Error: ask for the visitor's name and email and save them with update_lead first."
            tz = _visitor_tz(ctx, args)
            start = _start(args.get("start", ""), tz)
            if start is None:
                return "Error: 'start' must be a start value from find_available_slots."
            consultant = ctx.team.for_need(person.need)
            client = await ctx.clients.get_client(ctx.client_id) if ctx.client_id and ctx.clients else None
            if client and client.account_manager:
                consultant = ctx.team.get(client.account_manager)   # clients meet their own consultant
            try:
                b, created = await ctx.booking.book(
                    conversation_id=ctx.conversation_id, consultant=consultant, start=start, visitor_tz=tz,
                    name=person.name, email=person.email, phone=person.phone, notes=_clean(args.get("notes", "")),
                    now=ctx.now)
            except NotBookable:
                return f"That time is not available. Call find_available_slots and offer other times. {_hours(ctx)}"
            except AlreadyBooked as exc:
                e = exc.existing
                return (f"This visitor already has a call booked: {describe(e.start_at, e.end_at, tz)} "
                        f"(reference {e.reference}). To change it, use reschedule_discovery_call.")
            except CalendarUnavailable:
                return await _calendar_down(ctx, start.isoformat())
            await ctx.repo.log_event(ctx.conversation_id, "call_booked", {"booking": b.reference, "created": created})
            text_too = " and a text message to their phone" if ctx.channel == "voice" and b.phone else ""
            sent = (f"A confirmation email{text_too} and a calendar invitation are on their way to {b.email}."
                    if ctx.sends_email else
                    "This is a demonstration: no confirmation email or calendar invitation is sent, so say that "
                    "and do not promise one.")
            return (f"Booked: {describe(b.start_at, b.end_at, tz)} with {ctx.team.get(b.consultant_id).name}, "
                    f"reference {b.reference}. {sent} Tell the visitor these details.")

        if name == "reschedule_discovery_call":
            target = await _find_booking(ctx, args)
            if target is None:
                return NO_BOOKING
            tz = _visitor_tz(ctx, args, target)
            start = _start(args.get("new_start", ""), tz)
            if start is None:
                return "Error: 'new_start' must be a start value from find_available_slots."
            try:
                moved = await ctx.booking.reschedule(target, start, now=ctx.now)
            except NotBookable:
                return f"That time is not available. Call find_available_slots and offer other times. {_hours(ctx)}"
            except CalendarUnavailable:
                return await _calendar_down(ctx, f"move {target.reference} to {start.isoformat()}")
            await ctx.repo.log_event(ctx.conversation_id, "call_moved", {"booking": target.reference})
            sent = (f"An updated invitation and email are on their way to {moved.email}." if ctx.sends_email else
                    "This is a demonstration: no updated email or invitation is sent.")
            return (f"Moved: the call ({moved.reference}) is now {describe(moved.start_at, moved.end_at, tz)}. "
                    f"{sent}")

        if name == "cancel_discovery_call":
            target = await _find_booking(ctx, args)
            if target is None:
                return NO_BOOKING
            await ctx.booking.cancel(target)
            await ctx.repo.log_event(ctx.conversation_id, "call_cancelled",
                                     {"booking": target.reference, "reason": _clean(args.get("reason", ""))})
            tz = _visitor_tz(ctx, args, target)
            when = describe(target.start_at, target.end_at, tz)
            sent = (f"A confirmation email is on its way to {target.email}." if ctx.sends_email else
                    "This is a demonstration: no confirmation email is sent.")
            return (f"Cancelled the call on {when} ({target.reference}). {sent} "
                    "Offer to book a new time if they want one.")

        if name == "verify_client":
            if ctx.client_id:
                return "This visitor is already verified."
            if ctx.verifier is None:
                return "Client accounts are not available here; offer to connect them with a consultant."
            code = _clean(args.get("code", ""))
            if code:
                client, info = await ctx.verifier.confirm(ctx.conversation_id, code)
                if client is None:
                    return info
                await ctx.repo.bind_client(ctx.conversation_id, client.id, info)
                await ctx.repo.log_event(ctx.conversation_id, "client_verified", {"client": client.slug})
                ctx.client_id, ctx.verified_email = client.id, info
                return (f"Verified: the visitor speaks for {client.name}. You can now use client_overview, "
                        "raise_ticket and update_ticket, and book a call with their consultant.")
            email = email_from_speech(_clean(args.get("email", "")))
            if not EMAIL_RE.match(email):
                return "Ask for the email address they use with us."
            return await ctx.verifier.send(ctx.conversation_id, email)

        if name in ("client_overview", "raise_ticket", "update_ticket"):
            if not (ctx.client_id and ctx.clients and ctx.tickets):
                return "Not verified. Ask for the email they use with us and call verify_client first."
            return await _client_tool(ctx, name, args)

        if name == "transfer_call":
            return await _transfer(ctx, args)

        if name == "handover_to_human":
            reason = str(args.get("reason")) if args.get("reason") in HANDOVER_REASONS else "other"
            summary = _clean(args.get("summary", ""))[:MAX_FIELD_CHARS]
            await ctx.repo.escalate(ctx.conversation_id, reason, summary)
            await ctx.repo.log_event(ctx.conversation_id, "handover", {"reason": reason, "by": "model"})
            await after_handover(ctx.jobs, ctx.conversation_id, reason)
            known = await ctx.repo.get_lead(ctx.conversation_id)
            contact = "" if known and known.email else " Ask for their name and email so the consultant can reach them."
            return f"Handed over to a consultant ({reason}).{contact}"

        return f"Error: unknown tool '{name}'."
    except Exception as exc:  # a failing tool must not end the conversation
        log.exception("tool %s failed", name)
        try:
            await ctx.repo.log_event(ctx.conversation_id, "tool_error", {"tool": name, "error": str(exc)[:300]})
        except Exception:
            log.exception("could not log the tool error")
        return "Error: this action failed. Apologise briefly and offer to connect the visitor with a consultant."
