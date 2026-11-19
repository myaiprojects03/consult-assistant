"use client";

import { DailyBars, HBars } from "@/components/admin/charts";
import { brand } from "@/config/brand";
import { Overview as Data, words } from "@/lib/admin";

const c = brand.colors;

function Stat({ label, value, note }: { label: string; value: string | number; note?: string }) {
  return (
    <div className="rounded-xl border p-4" style={{ borderColor: c.line }}>
      <p className="text-sm" style={{ color: c.muted }}>{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {note && <p className="mt-1 text-xs" style={{ color: c.muted }}>{note}</p>}
    </div>
  );
}

const pct = (a: number, b: number) => (b ? `${Math.round((a / b) * 100)}% of conversations` : "");
const secs = (ms: number | null) => (ms == null ? "n/a" : `${(ms / 1000).toFixed(1)} s`);

export default function Overview({ data }: { data: Data }) {
  const f = data.funnel;
  const calls = data.calls.reduce((n, x) => n + x.n, 0);
  const minutes = Math.round(data.calls.reduce((n, x) => n + x.seconds, 0) / 60);
  const handovers = data.handovers.reduce((n, x) => n + x.n, 0);
  const voice = data.conversations_by_channel.find((x) => x.channel === "voice")?.n ?? 0;
  const q = data.quality;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <Stat label="Conversations" value={f.conversations} note={voice ? `${voice} by voice` : undefined} />
        <Stat label="Contact details captured" value={f.contacts} note={pct(f.contacts, f.conversations)} />
        <Stat label="Qualified leads" value={f.qualified} note={pct(f.qualified, f.conversations)} />
        <Stat label="Discovery calls booked" value={f.booked} note={pct(f.booked, f.conversations)} />
        <Stat label="Handed to a consultant" value={handovers} />
        <Stat label="Phone and browser calls" value={calls} note={minutes ? `${minutes} min in all` : undefined} />
      </div>

      <section className="rounded-xl border p-4" style={{ borderColor: c.line }}>
        <DailyBars label="Conversations per day" data={data.daily.map((d) => ({
          day: String(d.day).slice(0, 10), value: d.conversations,
          detail: `${d.conversations} conversations, ${d.qualified} qualified, ${d.bookings} booked, ${d.handovers} handed over`,
        }))} />
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="rounded-xl border p-4" style={{ borderColor: c.line }}>
          <h2 className="mb-3 text-sm font-semibold">From conversation to booked call</h2>
          <HBars empty="No conversations in this period." rows={f.conversations ? [
            { label: "Conversations", value: f.conversations },
            { label: "Gave contact details", value: f.contacts },
            { label: "Qualified", value: f.qualified },
            { label: "Booked a call", value: f.booked },
          ] : []} />
        </section>
        <section className="rounded-xl border p-4" style={{ borderColor: c.line }}>
          <h2 className="mb-3 text-sm font-semibold">Why conversations were handed over</h2>
          <HBars empty="No handovers in this period." rows={data.handovers.map((h) => ({
            label: words(h.reason) || "other", value: h.n }))} />
        </section>
      </div>

      <section className="rounded-xl border p-4" style={{ borderColor: c.line }}>
        <h2 className="mb-3 text-sm font-semibold">Health</h2>
        <dl className="grid grid-cols-2 gap-4 text-sm lg:grid-cols-5">
          <div><dt style={{ color: c.muted }}>First words of a reply (median)</dt><dd className="text-lg font-semibold">{secs(q.first_text_p50)}</dd></div>
          <div><dt style={{ color: c.muted }}>Slowest 10% of replies</dt><dd className="text-lg font-semibold">{secs(q.first_text_p90)}</dd></div>
          <div><dt style={{ color: c.muted }}>Prices or promises blocked</dt><dd className="text-lg font-semibold">{q.guard_replacements}</dd></div>
          <div><dt style={{ color: c.muted }}>Failed background jobs</dt>
            <dd className="text-lg font-semibold" style={{ color: data.failed_jobs ? c.alert : c.ink }}>{data.failed_jobs}</dd></div>
          <div><dt style={{ color: c.muted }}>AI tokens used</dt>
            <dd className="text-lg font-semibold">{(Number(q.input_tokens) + Number(q.output_tokens)).toLocaleString()}</dd></div>
        </dl>
      </section>
    </div>
  );
}
