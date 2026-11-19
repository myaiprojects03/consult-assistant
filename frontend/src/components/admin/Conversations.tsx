"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { brand } from "@/config/brand";
import { adminGet, details, Row, when, words } from "@/lib/admin";

const c = brand.colors;

const TOOL_WORDS: Record<string, string> = {
  search_knowledge: "looked up the firm's information", update_lead: "saved details",
  find_available_slots: "checked the calendar", book_discovery_call: "booked a call",
  reschedule_discovery_call: "moved a call", cancel_discovery_call: "cancelled a call",
  handover_to_human: "handed over", transfer_call: "transferred the call", verify_client: "checked a client",
  client_overview: "read the client's account", raise_ticket: "raised a ticket", update_ticket: "updated a ticket",
};

type Detail = {
  conversation: Row; lead: Row | null; bookings: Row[]; calls: Row[]; tickets: Row[]; events: Row[];
  messages: { role: string; text: string; tool: string; tools: string[]; at: string }[];
};

export function ConversationDetail({ token, id, onClose, onError }: {
  token: string; id: string; onClose: () => void; onError: (e: unknown) => void;
}) {
  const [d, setD] = useState<Detail | null>(null);
  const [showLog, setShowLog] = useState(false);
  useEffect(() => {
    adminGet<Detail>(token, `/conversations/${id}`).then(setD).catch(onError);
  }, [token, id, onError]);
  if (!d) return <p className="p-6 text-sm" style={{ color: c.muted }}>Loading…</p>;
  const conv = d.conversation, lead = d.lead;
  return (
    <div className="space-y-5 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">{String(lead?.name || "Anonymous visitor")}</h2>
          <p className="text-sm" style={{ color: c.muted }}>
            {conv.channel === "voice" ? "Call" : "Website chat"}, started {when(conv.started_at)}
            {conv.client ? `, verified client: ${String(conv.client)}` : ""}
          </p>
        </div>
        <button onClick={onClose} className="rounded border px-3 py-1 text-sm" style={{ borderColor: c.line }}>Close</button>
      </div>

      {Boolean(conv.escalation_reason) && (
        <p className="rounded-lg p-3 text-sm" style={{ background: "#FFF4ED", color: c.alert }}>
          Handed over: {words(conv.escalation_reason)}. {String(conv.escalation_summary || "")}
        </p>
      )}
      {Boolean(conv.summary) && <p className="rounded-lg p-3 text-sm" style={{ background: c.soft }}>{String(conv.summary)}</p>}

      {lead && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          {["email", "phone", "company", "company_size", "need", "current_systems", "budget", "timeline", "status"]
            .filter((k) => lead[k]).map((k) => (
              <div key={k} className="contents">
                <dt style={{ color: c.muted }}>{words(k)}</dt><dd>{words(lead[k])}</dd>
              </div>
            ))}
        </dl>
      )}
      {d.bookings.map((b) => (
        <p key={String(b.id)} className="text-sm">Discovery call {String(b.reference)}: {when(b.start_at)} with {String(b.consultant ?? b.consultant_id)} ({String(b.status)})</p>
      ))}
      {d.tickets.map((t) => (
        <p key={String(t.id)} className="text-sm">Ticket {String(t.reference)} ({String(t.priority)}, {words(t.status)}): {String(t.summary)}</p>
      ))}
      {d.calls.map((k) => (
        <p key={String(k.id)} className="text-sm">{words(k.direction)} call, {words(k.status)}{k.outcome ? `, ${words(k.outcome)}` : ""}
          {k.duration_s ? `, ${Math.round(Number(k.duration_s) / 60)} min` : ""}</p>
      ))}

      <section aria-label="Transcript" className="space-y-2">
        <h3 className="text-sm font-semibold">Transcript</h3>
        {d.messages.map((m, i) => m.role === "tool" ? null : m.role === "user" ? (
          <p key={i} className="ml-auto max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm"
             style={{ background: c.accent, color: c.accentInk }}>{m.text}</p>
        ) : (
          <div key={i} className="max-w-[85%]">
            {m.text && <p className="whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm" style={{ background: c.soft }}>{m.text}</p>}
            {m.tools.length > 0 && <p className="mt-1 text-xs" style={{ color: c.muted }}>
              Ava {m.tools.map((t) => TOOL_WORDS[t] ?? words(t)).join(", ")}</p>}
          </div>
        ))}
      </section>

      <button onClick={() => setShowLog(!showLog)} className="text-sm underline" style={{ color: c.muted }}>
        {showLog ? "Hide" : "Show"} the event log ({d.events.length})
      </button>
      {showLog && (
        <ul className="space-y-1 text-xs" style={{ color: c.muted }}>
          {d.events.map((e, i) => (
            <li key={i}><span className="font-medium" style={{ color: c.ink }}>{words(e.kind)}</span> {when(e.created_at)}{" "}
              <span className="break-words">{details(e.data)}</span></li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function Conversations({ token, onError, initialStatus = "" }: {
  token: string; onError: (e: unknown) => void; initialStatus?: string;
}) {
  const [items, setItems] = useState<Row[] | null>(null);
  const [channel, setChannel] = useState("");
  const [status, setStatus] = useState(initialStatus);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const detail = useRef<HTMLDivElement>(null);
  useEffect(() => {        // on a phone the transcript is below the list: bring it into view
    if (open && window.innerWidth < 1024) detail.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open]);
  const load = useCallback(() => {
    const p = new URLSearchParams({ channel, status, q });
    adminGet<{ items: Row[] }>(token, `/conversations?${p}`).then((r) => setItems(r.items)).catch(onError);
  }, [token, channel, status, q, onError]);
  useEffect(() => { load(); }, [channel, status]);    // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <div>
        <form className="mb-3 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); load(); }}>
          <select aria-label="Channel" value={channel} onChange={(e) => setChannel(e.target.value)}
                  className="rounded-lg border px-2 py-1.5 text-sm" style={{ borderColor: c.line }}>
            <option value="">Chat and calls</option><option value="web">Website chat</option><option value="voice">Calls</option>
          </select>
          <select aria-label="Show" value={status} onChange={(e) => setStatus(e.target.value)}
                  className="rounded-lg border px-2 py-1.5 text-sm" style={{ borderColor: c.line }}>
            <option value="">All</option><option value="handover">Handed over</option><option value="booked">Booked a call</option>
            <option value="qualified">Qualified</option><option value="not_a_fit">Not a fit</option>
          </select>
          <input aria-label="Search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search name, email or company"
                 className="min-w-0 flex-1 rounded-lg border px-3 py-1.5 text-sm" style={{ borderColor: c.line }} />
          <button className="rounded-lg px-3 py-1.5 text-sm font-semibold" style={{ background: c.accent, color: c.accentInk }}>Search</button>
        </form>
        {items === null ? <p className="text-sm" style={{ color: c.muted }}>Loading…</p> : items.length === 0 ? (
          <p className="text-sm" style={{ color: c.muted }}>No conversations match.</p>
        ) : (
          <ul className="divide-y rounded-xl border" style={{ borderColor: c.line }}>
            {items.map((r) => (
              <li key={String(r.id)}>
                <button onClick={() => setOpen(String(r.id))} aria-current={open === r.id ? "true" : undefined}
                        className="block w-full px-4 py-3 text-left hover:bg-[#F6FAF9] focus-visible:outline focus-visible:outline-2"
                        style={open === r.id ? { background: c.soft } : undefined}>
                  <span className="flex items-baseline justify-between gap-3">
                    <span className="truncate font-medium">{String(r.name || r.email || "Anonymous visitor")}</span>
                    <span className="shrink-0 text-xs" style={{ color: c.muted }}>{when(r.last_message_at)}</span>
                  </span>
                  <span className="mt-0.5 block text-xs" style={{ color: c.muted }}>
                    {r.channel === "voice" ? "Call" : "Chat"}{r.client ? `, ${String(r.client)}` : ""}
                    {r.booking ? `, booked ${String(r.booking)}` : ""}
                    {r.escalation_reason ? `, handed over (${words(r.escalation_reason)})` : ""}
                    {r.lead_status && r.lead_status !== "new" ? `, ${words(r.lead_status)}` : ""}
                  </span>
                  {Boolean(r.first_message) && !r.summary && (
                    <span className="mt-0.5 block truncate text-sm">&ldquo;{String(r.first_message)}&rdquo;</span>
                  )}
                  {Boolean(r.summary) && <span className="mt-1 line-clamp-2 block text-sm">{String(r.summary)}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div ref={detail} className="scroll-mt-4 rounded-xl border lg:sticky lg:top-4 lg:max-h-[calc(100dvh-8rem)] lg:overflow-y-auto"
           style={{ borderColor: c.line }}>
        {open ? <ConversationDetail token={token} id={open} onClose={() => setOpen(null)} onError={onError} /> : (
          <p className="p-6 text-sm" style={{ color: c.muted }}>Choose a conversation to read its transcript.</p>
        )}
      </div>
    </div>
  );
}
