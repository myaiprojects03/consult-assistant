"""Read-only queries behind the admin dashboard (Postgres). Nothing here changes data.

Conversations of the internal assistant (channel 'team') are staff work, not customer traffic, and are
left out of every figure and list.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

PAGE = 50
CUSTOMER = "c.channel <> 'team'"


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(1, min(days, 365)))


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def _rows(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    """Rows as dicts; JSON columns (events.data, tool_calls) decoded."""
    out = []
    for r in rows:
        d = dict(r)
        for k in ("data", "tool_calls", "payload"):
            if k in d:
                d[k] = _json(d[k])
        out.append(d)
    return out


def _uuid(v: str) -> UUID | None:
    try:
        return UUID(v)
    except ValueError:
        return None


class AdminQueries:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ------------------------------------------------------------ overview
    async def overview(self, days: int = 30) -> dict[str, Any]:
        since = _since(days)
        q = self.pool.fetch
        by_channel = await q(f"SELECT c.channel, count(*) AS n FROM conversations c WHERE c.started_at >= $1 "
                             f"AND {CUSTOMER} GROUP BY 1", since)
        funnel = await self.pool.fetchrow(
            f"""SELECT count(*) AS conversations,
                       count(l.conversation_id) FILTER (WHERE l.email <> '' OR l.phone <> '') AS contacts,
                       count(l.conversation_id) FILTER (WHERE l.status = 'qualified') AS qualified,
                       count(l.conversation_id) FILTER (WHERE l.status = 'not_a_fit') AS not_a_fit,
                       count(b.id) AS booked
                FROM conversations c LEFT JOIN leads l ON l.conversation_id = c.id
                LEFT JOIN bookings b ON b.conversation_id = c.id AND b.status = 'confirmed'
                WHERE c.started_at >= $1 AND {CUSTOMER}""", since)
        handovers = await q("SELECT escalation_reason AS reason, count(*) AS n FROM conversations "
                            "WHERE escalated_at >= $1 GROUP BY 1 ORDER BY 2 DESC", since)
        bookings = await q("SELECT status, count(*) AS n FROM bookings WHERE created_at >= $1 GROUP BY 1", since)
        calls = await q("SELECT direction, count(*) AS n, coalesce(sum(duration_s), 0)::bigint AS seconds, "
                        "count(*) FILTER (WHERE outcome = 'transferred') AS transferred FROM calls "
                        "WHERE created_at >= $1 GROUP BY 1", since)
        tickets = await q("SELECT status, count(*) AS n FROM tickets WHERE created_at >= $1 GROUP BY 1", since)
        quality = await self.pool.fetchrow(
            """SELECT count(*) AS turns,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY (data->>'ms_first_text')::int)
                          AS first_text_p50,
                      percentile_cont(0.9) WITHIN GROUP (ORDER BY (data->>'ms_first_text')::int)
                          AS first_text_p90,
                      coalesce(sum((data->>'input')::bigint), 0)::bigint AS input_tokens,
                      coalesce(sum((data->>'output')::bigint), 0)::bigint AS output_tokens
               FROM events WHERE kind = 'turn' AND created_at >= $1""", since)
        guard = await self.pool.fetchval("SELECT count(*) FROM events WHERE kind = 'guard' AND created_at >= $1",
                                         since)
        failed_jobs = await self.pool.fetchval("SELECT count(*) FROM jobs WHERE status = 'failed' "
                                               "AND created_at >= $1", since)
        daily = await q(
            f"""SELECT d::date AS day,
                       (SELECT count(*) FROM conversations c WHERE c.started_at::date = d::date AND {CUSTOMER})
                           AS conversations,
                       (SELECT count(*) FROM leads l WHERE l.status = 'qualified'
                          AND l.updated_at::date = d::date) AS qualified,
                       (SELECT count(*) FROM bookings b WHERE b.created_at::date = d::date) AS bookings,
                       (SELECT count(*) FROM conversations c WHERE c.escalated_at::date = d::date)
                           AS handovers
                FROM generate_series($1::date, now()::date, interval '1 day') AS d ORDER BY 1""", since)
        return {"days": days, "since": since, "conversations_by_channel": _rows(by_channel),
                "funnel": dict(funnel) if funnel else {}, "handovers": _rows(handovers),
                "bookings": _rows(bookings), "calls": _rows(calls), "tickets": _rows(tickets),
                "quality": {**(dict(quality) if quality else {}), "guard_replacements": guard},
                "failed_jobs": failed_jobs, "daily": _rows(daily)}

    # ------------------------------------------------------------ lists
    async def conversations(self, *, channel: str = "", status: str = "", q: str = "", page: int = 0
                            ) -> list[dict[str, Any]]:
        where: list[str] = [CUSTOMER]
        args: list[Any] = []
        if channel in ("web", "voice"):
            args.append(channel)
            where.append(f"c.channel = ${len(args)}")
        if status == "handover":
            where.append("c.escalated_at IS NOT NULL")
        elif status == "booked":
            where.append("EXISTS (SELECT 1 FROM bookings b WHERE b.conversation_id = c.id "
                         "AND b.status = 'confirmed')")
        elif status in ("qualified", "not_a_fit", "new"):
            args.append(status)
            where.append(f"l.status = ${len(args)}")
        if q.strip():
            args.append(f"%{q.strip()[:100]}%")
            n = len(args)
            where.append(f"(l.name ILIKE ${n} OR l.email ILIKE ${n} OR l.company ILIKE ${n} "
                         f"OR c.summary ILIKE ${n})")
        args.append(page * PAGE)
        sql = f"""SELECT c.id, c.channel, c.status, c.started_at, c.last_message_at, c.turn_count,
                         c.escalation_reason, left(c.summary, 300) AS summary, l.name, l.email, l.company,
                         l.status AS lead_status, cl.name AS client,
                         (SELECT left(m.text, 160) FROM messages m WHERE m.conversation_id = c.id
                            AND m.role = 'user' ORDER BY m.id LIMIT 1) AS first_message,
                         (SELECT b.reference FROM bookings b WHERE b.conversation_id = c.id
                            AND b.status = 'confirmed' LIMIT 1) AS booking
                  FROM conversations c LEFT JOIN leads l ON l.conversation_id = c.id
                  LEFT JOIN clients cl ON cl.id = c.client_id
                  WHERE {' AND '.join(where)}
                  ORDER BY c.last_message_at DESC LIMIT {PAGE} OFFSET ${len(args)}"""
        return _rows(await self.pool.fetch(sql, *args))

    async def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        cid = _uuid(conversation_id)
        if cid is None:
            return None
        conv = await self.pool.fetchrow(
            "SELECT c.*, cl.name AS client FROM conversations c LEFT JOIN clients cl ON cl.id = c.client_id "
            "WHERE c.id = $1 AND c.channel <> 'team'", cid)
        if conv is None:
            return None
        messages = await self.pool.fetch(
            "SELECT id, role, text, tool_name, tool_calls, created_at FROM messages WHERE conversation_id = $1 "
            "ORDER BY id", cid)
        lead = await self.pool.fetchrow("SELECT * FROM leads WHERE conversation_id = $1", cid)
        out = dict(conv)
        out.pop("token_hash", None)
        return {"conversation": out, "lead": dict(lead) if lead else None,
                "messages": [{"role": m["role"], "text": m["text"], "tool": m["tool_name"],
                              "tools": [c.get("name") for c in (_json(m["tool_calls"]) or [])
                                        if isinstance(c, dict)],
                              "at": m["created_at"]} for m in messages],
                "bookings": _rows(await self.pool.fetch("SELECT * FROM bookings WHERE conversation_id = $1 "
                                                        "ORDER BY created_at", cid)),
                "calls": _rows(await self.pool.fetch("SELECT * FROM calls WHERE conversation_id = $1", cid)),
                "tickets": _rows(await self.pool.fetch("SELECT * FROM tickets WHERE conversation_id = $1", cid)),
                "events": _rows(await self.pool.fetch("SELECT kind, data, created_at FROM events "
                                                      "WHERE conversation_id = $1 ORDER BY id", cid))}

    async def leads(self, *, status: str = "", page: int = 0) -> list[dict[str, Any]]:
        args: list[Any] = [page * PAGE]
        where = ""
        if status in ("new", "qualified", "not_a_fit"):
            args.append(status)
            where = "WHERE l.status = $2"
        return _rows(await self.pool.fetch(
            f"""SELECT l.conversation_id, l.name, l.email, l.phone, l.company, l.need, l.company_size, l.budget,
                       l.timeline, l.status, l.crm_contact_id, l.updated_at, c.channel
                FROM leads l JOIN conversations c ON c.id = l.conversation_id {where}
                ORDER BY l.updated_at DESC LIMIT {PAGE} OFFSET $1""", *args))

    async def bookings(self, *, upcoming: bool = True, page: int = 0) -> list[dict[str, Any]]:
        order = "start_at ASC" if upcoming else "start_at DESC"
        where = "WHERE status = 'confirmed' AND end_at >= now()" if upcoming else ""
        return _rows(await self.pool.fetch(
            f"""SELECT id, reference, conversation_id, consultant_id, start_at, end_at, status, name, email,
                       phone, meeting_url, created_at FROM bookings {where}
                ORDER BY {order} LIMIT {PAGE} OFFSET $1""",
            page * PAGE))

    async def handovers(self, page: int = 0) -> list[dict[str, Any]]:
        return _rows(await self.pool.fetch(
            """SELECT c.id, c.channel, c.escalation_reason, c.escalation_summary, c.escalated_at, l.name, l.email,
                      l.phone, cl.name AS client
               FROM conversations c LEFT JOIN leads l ON l.conversation_id = c.id
               LEFT JOIN clients cl ON cl.id = c.client_id
               WHERE c.escalated_at IS NOT NULL ORDER BY c.escalated_at DESC LIMIT 50 OFFSET $1""", page * PAGE))

    async def calls(self, page: int = 0) -> list[dict[str, Any]]:
        return _rows(await self.pool.fetch(
            """SELECT k.id, k.direction, k.from_number, k.to_number, k.status, k.outcome, k.purpose,
                      k.contact_name, k.transfer_to, k.duration_s, k.created_at, k.conversation_id,
                      left(c.summary, 300) AS summary
               FROM calls k LEFT JOIN conversations c ON c.id = k.conversation_id
               ORDER BY k.created_at DESC LIMIT 50 OFFSET $1""", page * PAGE))

    async def events(self, *, kind: str = "", page: int = 0) -> list[dict[str, Any]]:
        # The staff member's name for entries made by one (data.user holds their id)
        rows = await self.pool.fetch(
            "SELECT e.id, e.conversation_id, e.kind, e.data, e.created_at, s.name AS by_name FROM events e "
            "LEFT JOIN staff_users s ON s.id::text = e.data->>'user' "
            "WHERE ($1 = '' OR e.kind = $1) ORDER BY e.id DESC LIMIT 100 OFFSET $2", kind, page * 100)
        return _rows(rows)

    async def jobs(self, status: str = "failed") -> list[dict[str, Any]]:
        return _rows(await self.pool.fetch(
            "SELECT id, kind, ref, status, attempts, max_attempts, last_error, run_at, created_at, finished_at "
            "FROM jobs WHERE status = $1 ORDER BY created_at DESC LIMIT 100",
            status if status in ("pending", "running", "failed", "done", "cancelled") else "failed"))
