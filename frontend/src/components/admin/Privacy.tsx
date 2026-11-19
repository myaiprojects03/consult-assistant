"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import { Table } from "@/components/admin/Lists";
import { brand } from "@/config/brand";
import { ChatError } from "@/lib/chat";
import { adminGet, adminSend, Row, when, words } from "@/lib/admin";

const c = brand.colors;

type Found = {
  conversations: Row[]; bookings: Row[]; calls: Row[]; tickets: Row[]; call_requests: number;
  client_contact: Row[]; outside: string[]; blockers: string[];
};
type Erased = { counts: Record<string, number>; outside: string[]; kept: string[] };
type History = { kind: string; at: string; by: string; data: Record<string, unknown> };

const PERIODS: [string, string][] = [
  ["conversations_days", "Chats and calls (transcripts, lead details)"],
  ["bookings_days", "Discovery call bookings, from the day of the call"],
  ["logs_days", "Activity log"],
  ["finished_jobs_days", "Finished background tasks"],
];

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;
const NAMES: [string, string, string][] = [         // key, one, many (fixed order: the log keeps no order)
  ["conversations", "conversation", "conversations"], ["bookings", "booking", "bookings"], ["calls", "call", "calls"],
  ["tickets_anonymised", "ticket anonymised", "tickets anonymised"],
  ["background_tasks", "background task", "background tasks"], ["log_entries", "log entry", "log entries"],
  ["expired_sign_ins", "expired sign-in", "expired sign-ins"],
];
const counts = (d: Record<string, unknown> | undefined) =>
  NAMES.filter(([k]) => Number(d?.[k]) > 0).map(([k, one, many]) => `${d?.[k]} ${Number(d?.[k]) === 1 ? one : many}`)
    .join(", ") || "nothing";

function describe(h: History): string {
  if (h.kind === "privacy_erased") return `Erased one person's data: ${counts(h.data.counts as Record<string, unknown>)}`;
  if (h.kind === "privacy_exported") return "Downloaded a copy of one person's data";
  return `Clean-up: deleted ${counts(h.data.deleted as Record<string, unknown>)}`;
}

export default function Privacy({ token, onError, openConversation }: {
  token: string; onError: (e: unknown) => void; openConversation: (id: string) => void;
}) {
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [asked, setAsked] = useState<{ email: string; phone: string } | null>(null);
  const [found, setFound] = useState<Found | null>(null);
  const [confirm, setConfirm] = useState("");
  const [erased, setErased] = useState<Erased | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<{ retention: Record<string, number>; history: History[] } | null>(null);

  const loadInfo = useCallback(() => {
    adminGet<{ retention: Record<string, number>; history: History[] }>(token, "/privacy").then(setInfo).catch(onError);
  }, [token, onError]);
  useEffect(() => { loadInfo(); }, [loadInfo]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
    } catch (e) {
      if (e instanceof ChatError) setMessage({ ok: false, text: e.message });
      else onError(e);
    } finally {
      setBusy(false);
    }
  };
  const find = (who = asked) => who && run(async () => {
    setFound(await adminSend<Found>(token, "/privacy/find", "POST", who));
  });
  const onFind = (e: FormEvent) => {
    e.preventDefault();
    const who = { email: email.trim(), phone: phone.trim() };
    setAsked(who);
    setErased(null);
    setConfirm("");
    void find(who);
  };
  const download = () => asked && run(async () => {
    const data = await adminSend(token, "/privacy/export", "POST", asked);
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `personal-data-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    setMessage({ ok: true, text: "Downloaded. Send it to the person through a secure channel." });
    loadInfo();
  });
  const cancelBooking = (b: Row) => {
    if (!window.confirm(`Cancel booking ${b.reference}? The person gets the cancellation email.`)) return;
    void run(async () => {
      await adminSend(token, `/bookings/${b.id}/cancel`, "POST");
      setFound(await adminSend<Found>(token, "/privacy/find", "POST", asked));
      setMessage({ ok: true, text: `Booking ${b.reference} cancelled. The calendar and email updates run in the background; erasing is possible once they are done.` });
    });
  };
  const erase = () => asked && run(async () => {
    setErased(await adminSend<Erased>(token, "/privacy/erase", "POST", { ...asked, confirm }));
    setFound(null);
    setConfirm("");
    loadInfo();
  });
  const cleanUp = () => run(async () => {
    const r = await adminSend<{ deleted: Record<string, number> }>(token, "/privacy/retention", "POST");
    setMessage({ ok: true, text: `Clean-up done: deleted ${counts(r.deleted)}.` });
    loadInfo();
  });

  const total = found ? found.conversations.length + found.bookings.length + found.calls.length + found.tickets.length : 0;
  const field = "rounded-lg border px-3 py-1.5 text-sm";

  return (
    <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <section className="min-w-0">
        <h2 className="text-base font-semibold">A person&apos;s data</h2>
        <p className="mt-1 max-w-prose text-sm" style={{ color: c.muted }}>
          For a request to see or delete what we hold about someone. Search by the email address or phone number they
          used. Everything here is logged without the email or number itself.
        </p>
        <form onSubmit={onFind} className="mt-4 flex flex-wrap items-end gap-2">
          <label className="text-xs font-medium" style={{ color: c.muted }}>Email
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off"
                   className={`mt-1 block w-64 ${field}`} style={{ borderColor: c.line, color: c.ink }} />
          </label>
          <label className="text-xs font-medium" style={{ color: c.muted }}>Phone
            <input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} autoComplete="off"
                   className={`mt-1 block w-48 ${field}`} style={{ borderColor: c.line, color: c.ink }} />
          </label>
          <button disabled={busy || (!email.trim() && !phone.trim())}
                  className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-40"
                  style={{ background: c.accent, color: c.accentInk }}>Find</button>
        </form>

        {message && (
          <p role="status" className="mt-4 rounded-lg p-3 text-sm"
             style={{ background: message.ok ? c.soft : "#FFF4ED", color: message.ok ? c.ink : c.alert }}>{message.text}</p>
        )}

        {erased && (
          <div className="mt-6 space-y-3 rounded-xl border p-4 text-sm" style={{ borderColor: c.line }}>
            <p className="font-semibold">Erased: {counts(erased.counts)}.</p>
            {erased.outside.length > 0 && (
              <div>
                <p className="font-medium">Remove these by hand in the other systems:</p>
                <ul className="mt-1 list-disc pl-5">{erased.outside.map((o) => <li key={o}>{o}</li>)}</ul>
              </div>
            )}
            {erased.kept.length > 0 && (
              <div>
                <p className="font-medium">Kept:</p>
                <ul className="mt-1 list-disc pl-5">{erased.kept.map((o) => <li key={o}>{o}</li>)}</ul>
              </div>
            )}
          </div>
        )}

        {found && (
          <div className="mt-6 space-y-5">
            <p className="text-sm font-medium">
              {total === 0 && !found.client_contact.length ? "Nothing found for this email or number."
                : [plural(found.conversations.length, "conversation"), plural(found.bookings.length, "booking"),
                   plural(found.calls.length, "call"), plural(found.tickets.length, "ticket")].join(", ") + "."}
            </p>
            {found.client_contact.map((cc) => (
              <p key={String(cc.client_name)} className="rounded-lg p-3 text-sm" style={{ background: c.soft }}>
                This is a contact of the client {String(cc.client_name)}. That entry is kept on erasure, because it is how
                they sign in as a client.
              </p>
            ))}
            {found.conversations.length > 0 && (
              <Table rows={found.conversations} empty="" onRow={(r) => openConversation(String(r.id))} cols={[
                { key: "started_at", label: "Started", render: (r) => when(r.started_at) },
                { key: "channel", label: "Channel" },
                { key: "name", label: "Name" },
                { key: "company", label: "Company" },
                { key: "lead_status", label: "Lead", render: (r) => words(r.lead_status) },
              ]} />
            )}
            {found.bookings.length > 0 && (
              <Table rows={found.bookings} empty="" cols={[
                { key: "reference", label: "Booking" },
                { key: "start_at", label: "Call", render: (r) => when(r.start_at) },
                { key: "status", label: "Status" },
                { key: "action", label: "", render: (r) => r.upcoming ? (
                  <button onClick={() => cancelBooking(r)} disabled={busy} className="underline" style={{ color: c.alert }}>
                    Cancel booking</button>) : null },
              ]} />
            )}
            {found.tickets.length > 0 && (
              <Table rows={found.tickets} empty="" cols={[
                { key: "reference", label: "Ticket" },
                { key: "client_name", label: "Client" },
                { key: "summary", label: "Summary" },
                { key: "status", label: "Status", render: (r) => words(r.status) },
              ]} />
            )}
            {found.outside.length > 0 && (
              <div className="text-sm">
                <p className="font-medium">Also held in other systems (not changed from here):</p>
                <ul className="mt-1 list-disc pl-5" style={{ color: c.muted }}>{found.outside.map((o) => <li key={o}>{o}</li>)}</ul>
              </div>
            )}
            {total > 0 && (
              <div className="flex flex-wrap gap-2">
                <button onClick={() => void download()} disabled={busy} className="rounded-lg border px-3 py-1.5 text-sm"
                        style={{ borderColor: c.line }}>Download a copy</button>
              </div>
            )}
            {total > 0 && (
              <div className="rounded-xl border p-4" style={{ borderColor: c.alert }}>
                <h2 className="text-sm font-semibold" style={{ color: c.alert }}>Erase this person&apos;s data</h2>
                {found.blockers.length > 0 ? (
                  <ul className="mt-2 list-disc pl-5 text-sm">{found.blockers.map((b) => <li key={b}>{b}</li>)}</ul>
                ) : (
                  <>
                    <p className="mt-1 text-sm" style={{ color: c.muted }}>
                      Deletes the conversations, bookings and calls above for good. Tickets stay with the client company,
                      without the contact email. This cannot be undone.
                    </p>
                    <label className="mt-3 block text-xs font-medium" style={{ color: c.muted }}>
                      Type the email address or phone number again to confirm
                      <input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off"
                             className={`mt-1 block w-72 ${field}`} style={{ borderColor: c.line, color: c.ink }} />
                    </label>
                    <button onClick={() => void erase()} disabled={busy || !confirm.trim()}
                            className="mt-3 rounded-lg px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-40"
                            style={{ background: c.alert }}>Erase for good</button>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </section>

      <aside className="space-y-6 text-sm">
        <section>
          <h2 className="font-semibold">Automatic clean-up</h2>
          <p className="mt-1" style={{ color: c.muted }}>Every night, records older than these periods are deleted.</p>
          <dl className="mt-3 divide-y rounded-xl border" style={{ borderColor: c.line }}>
            {PERIODS.map(([key, label]) => (
              <div key={key} className="flex justify-between gap-3 px-3 py-2" style={{ borderColor: c.line }}>
                <dt>{label}</dt>
                <dd className="whitespace-nowrap tabular-nums font-medium">
                  {info ? (info.retention[key] ? plural(info.retention[key], "day") : "Kept") : "…"}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2" style={{ color: c.muted }}>Change these in Content and rules, Assistant rules (the privacy part).</p>
          <button onClick={() => void cleanUp()} disabled={busy} className="mt-3 rounded-lg border px-3 py-1.5"
                  style={{ borderColor: c.line }}>Run the clean-up now</button>
        </section>
        <section>
          <h2 className="font-semibold">Privacy log</h2>
          {!info ? <p className="mt-2" style={{ color: c.muted }}>Loading…</p>
            : !info.history.length ? <p className="mt-2" style={{ color: c.muted }}>No requests or clean-ups yet.</p> : (
              <ul className="mt-2 space-y-2">
                {info.history.map((h, i) => (
                  <li key={i}>
                    <p>{describe(h)}</p>
                    <p className="text-xs" style={{ color: c.muted }}>{when(h.at)}{h.by ? `, by ${h.by}` : ", automatic"}</p>
                  </li>
                ))}
              </ul>
            )}
        </section>
      </aside>
    </div>
  );
}
