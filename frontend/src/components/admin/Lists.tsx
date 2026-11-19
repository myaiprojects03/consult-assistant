"use client";

import { FormEvent, ReactNode, useEffect, useState } from "react";

import { brand } from "@/config/brand";
import { ChatError, newMessageId } from "@/lib/chat";
import { adminGet, details, placeCall, Row, when, words } from "@/lib/admin";

const c = brand.colors;

type Col = { key: string; label: string; render?: (r: Row) => ReactNode };

export function Table({ rows, cols, empty, onRow }: {
  rows: Row[] | null; cols: Col[]; empty: string; onRow?: (r: Row) => void;
}) {
  if (rows === null) return <p className="text-sm" style={{ color: c.muted }}>Loading…</p>;
  if (!rows.length) return <p className="text-sm" style={{ color: c.muted }}>{empty}</p>;
  return (
    <div className="overflow-x-auto rounded-xl border" style={{ borderColor: c.line }}>
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead style={{ background: c.soft }}>
          <tr>{cols.map((col) => <th key={col.key} className="px-3 py-2 font-medium">{col.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={String(r.id ?? r.conversation_id ?? i)} className={`border-t ${onRow ? "cursor-pointer hover:bg-[#F6FAF9]" : ""}`}
                style={{ borderColor: c.line }} onClick={onRow ? () => onRow(r) : undefined}
                tabIndex={onRow ? 0 : undefined} onKeyDown={onRow ? (e) => { if (e.key === "Enter") onRow(r); } : undefined}>
              {cols.map((col) => (
                <td key={col.key} className={`px-3 py-2 align-top ${col.key.endsWith("_at") ? "whitespace-nowrap" : ""}`}>
                  {col.render ? col.render(r) : String(r[col.key] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function useList(token: string, path: string, onError: (e: unknown) => void, deps: unknown[] = []) {
  const [rows, setRows] = useState<Row[] | null>(null);
  useEffect(() => {
    setRows(null);
    adminGet<{ items: Row[] }>(token, path).then((r) => setRows(r.items)).catch(onError);
  }, [token, path, ...deps]);   // eslint-disable-line react-hooks/exhaustive-deps
  return rows;
}

type P = { token: string; onError: (e: unknown) => void; openConversation: (id: string) => void };

export function Handovers({ token, onError, openConversation }: P) {
  const rows = useList(token, "/handovers", onError);
  return <Table rows={rows} empty="No handovers yet." onRow={(r) => openConversation(String(r.id))} cols={[
    { key: "escalated_at", label: "When", render: (r) => when(r.escalated_at) },
    { key: "reason", label: "Why", render: (r) => words(r.escalation_reason) },
    { key: "who", label: "Who", render: (r) => [r.name, r.email || r.phone, r.client].filter(Boolean).map(String).join(", ") || "Anonymous" },
    { key: "escalation_summary", label: "Summary" },
  ]} />;
}

export function Bookings({ token, onError, openConversation }: P) {
  const [upcoming, setUpcoming] = useState(true);
  const rows = useList(token, `/bookings?upcoming=${upcoming}`, onError, [upcoming]);
  return (
    <div>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={upcoming} onChange={(e) => setUpcoming(e.target.checked)} /> Upcoming only
      </label>
      <Table rows={rows} empty="No discovery calls." onRow={(r) => r.conversation_id && openConversation(String(r.conversation_id))} cols={[
        { key: "start_at", label: "When", render: (r) => when(r.start_at) },
        { key: "reference", label: "Reference" },
        { key: "name", label: "Who", render: (r) => `${String(r.name)}, ${String(r.email)}` },
        { key: "consultant", label: "Consultant" },
        { key: "status", label: "Status", render: (r) => words(r.status) },
        { key: "meeting_url", label: "Video", render: (r) => r.meeting_url ? (
          <a href={String(r.meeting_url)} target="_blank" rel="noopener noreferrer" className="underline"
             onClick={(e) => e.stopPropagation()}>Join link</a>) : <span style={{ color: c.muted }}>None yet</span> },
      ]} />
    </div>
  );
}

export function Leads({ token, onError, openConversation }: P) {
  const [status, setStatus] = useState("");
  const rows = useList(token, `/leads?status=${status}`, onError, [status]);
  return (
    <div>
      <select aria-label="Lead status" value={status} onChange={(e) => setStatus(e.target.value)}
              className="mb-3 rounded-lg border px-2 py-1.5 text-sm" style={{ borderColor: c.line }}>
        <option value="">All leads</option><option value="qualified">Qualified</option>
        <option value="new">Not yet qualified</option><option value="not_a_fit">Not a fit</option>
      </select>
      <Table rows={rows} empty="No leads." onRow={(r) => openConversation(String(r.conversation_id))} cols={[
        { key: "updated_at", label: "Updated", render: (r) => when(r.updated_at) },
        { key: "name", label: "Name" },
        { key: "contact", label: "Contact", render: (r) => [r.email, r.phone].filter(Boolean).map(String).join(", ") },
        { key: "company", label: "Company", render: (r) => [r.company, r.company_size].filter(Boolean).map(String).join(", ") },
        { key: "need", label: "Need" },
        { key: "status", label: "Status", render: (r) => words(r.status) },
        { key: "crm", label: "In CRM", render: (r) => (r.crm_contact_id ? "Yes" : "Not yet") },
      ]} />
    </div>
  );
}

export function Calls({ token, onError, openConversation }: P) {
  const [reload, setReload] = useState(0);
  const rows = useList(token, "/calls", onError, [reload]);
  const [form, setForm] = useState({ to: "", name: "", purpose: "" });
  const [note, setNote] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setNote("");
    try {
      await placeCall(token, { client_message_id: newMessageId(), ...form });
      setNote("Call requested. Ava calls now if it is within calling hours, otherwise at the next allowed time.");
      setForm({ to: "", name: "", purpose: "" });
      setReload((n) => n + 1);
    } catch (err) {
      setNote(err instanceof ChatError ? err.message : "The call could not be requested.");
    }
  };
  return (
    <div className="space-y-5">
      <form onSubmit={submit} className="rounded-xl border p-4" style={{ borderColor: c.line }}>
        <h2 className="text-sm font-semibold">Ask Ava to call someone</h2>
        <div className="mt-3 grid gap-2 sm:grid-cols-[10rem_10rem_1fr_auto]">
          <input required aria-label="Phone number" placeholder="+44 7700 900123" value={form.to}
                 onChange={(e) => setForm({ ...form, to: e.target.value })} className="rounded-lg border px-3 py-1.5 text-sm" style={{ borderColor: c.line }} />
          <input aria-label="Name" placeholder="Their name" value={form.name}
                 onChange={(e) => setForm({ ...form, name: e.target.value })} className="rounded-lg border px-3 py-1.5 text-sm" style={{ borderColor: c.line }} />
          <input required minLength={3} aria-label="Purpose" placeholder="What the call is about (Ava says this)" value={form.purpose}
                 onChange={(e) => setForm({ ...form, purpose: e.target.value })} className="rounded-lg border px-3 py-1.5 text-sm" style={{ borderColor: c.line }} />
          <button className="rounded-lg px-3 py-1.5 text-sm font-semibold" style={{ background: c.accent, color: c.accentInk }}>Request call</button>
        </div>
        {note && <p className="mt-2 text-sm" style={{ color: c.muted }}>{note}</p>}
      </form>
      <Table rows={rows} empty="No calls yet." onRow={(r) => r.conversation_id && openConversation(String(r.conversation_id))} cols={[
        { key: "created_at", label: "When", render: (r) => when(r.created_at) },
        { key: "direction", label: "Type", render: (r) => ({ inbound: "Incoming", outbound: "Outgoing", web: "Browser" } as Record<string, string>)[String(r.direction)] },
        { key: "who", label: "Number", render: (r) => String(r.direction === "outbound" ? r.to_number : r.from_number || "") },
        { key: "status", label: "Status", render: (r) => words(r.status) + (r.outcome && r.outcome !== r.status ? `, ${words(r.outcome)}` : "") },
        { key: "duration_s", label: "Length", render: (r) => (r.duration_s ? `${Math.max(1, Math.round(Number(r.duration_s) / 60))} min` : "") },
        { key: "summary", label: "Summary", render: (r) => String(r.summary || (r.purpose
          ? `${r.contact_name ? `${String(r.contact_name)}: ` : ""}${String(r.purpose)}` : "")) },
      ]} />
    </div>
  );
}

const KINDS = ["", "turn", "handover", "guard", "admin_content_saved", "admin_viewed_conversation", "staff_login",
  "staff_login_failed", "internal_search", "internal_access_denied", "call_started", "call_interrupted",
  "integration", "sms", "privacy_erased", "privacy_exported", "privacy_retention", "admin_booking_cancelled"];

export function Log({ token, onError }: { token: string; onError: (e: unknown) => void }) {
  const [kind, setKind] = useState("");
  const events = useList(token, `/events?kind=${kind}`, onError, [kind]);
  const failed = useList(token, "/jobs?status=failed", onError);
  return (
    <div className="space-y-6">
      <section>
        <h2 className="mb-2 text-sm font-semibold">Failed background jobs</h2>
        <Table rows={failed} empty="None. Every email, calendar, CRM, Slack and Jira update went through." cols={[
          { key: "created_at", label: "When", render: (r) => when(r.created_at) },
          { key: "kind", label: "Job" },
          { key: "attempts", label: "Tries" },
          { key: "last_error", label: "Last error" },
        ]} />
      </section>
      <section>
        <div className="mb-2 flex items-center gap-3">
          <h2 className="text-sm font-semibold">Event log</h2>
          <select aria-label="Event type" value={kind} onChange={(e) => setKind(e.target.value)}
                  className="rounded-lg border px-2 py-1 text-sm" style={{ borderColor: c.line }}>
            {KINDS.map((k) => <option key={k} value={k}>{k ? words(k) : "All events"}</option>)}
          </select>
        </div>
        <Table rows={events} empty="No events." cols={[
          { key: "created_at", label: "When", render: (r) => when(r.created_at) },
          { key: "kind", label: "Event", render: (r) => words(r.kind) },
          { key: "data", label: "Details", render: (r) => <span className="break-words text-xs">{details(r.data, r.by_name)}</span> },
        ]} />
      </section>
    </div>
  );
}
