"use client";

import { useEffect, useRef, useState } from "react";

import { brand } from "@/config/brand";

const c = brand.colors;

/** One series, so one hue (the accent): no legend to decode; the title names the series. */
const shortDay = (iso: string) => {
  const d = new Date(`${iso.slice(0, 10)}T12:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
};

export function DailyBars({ data, label }: {
  data: { day: string; value: number; detail: string }[];
  label: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(640);            // drawn at its real width, so text stays 11px
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(Math.max(280, el.clientWidth)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const H = 200, PAD_L = 28, PAD_B = 22, PAD_T = 8;
  const max = Math.max(1, ...data.map((d) => d.value));
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const plotW = W - PAD_L - 4, plotH = H - PAD_B - PAD_T;
  const slot = plotW / Math.max(1, data.length);
  const barW = Math.max(2, Math.min(28, slot - 2));      // 2px gap between bars
  const y = (v: number) => PAD_T + plotH - (v / top) * plotH;
  const ticks = Array.from({ length: Math.floor(top / step) + 1 }, (_, i) => i * step);
  const labelEvery = Math.ceil(data.length / 8);
  const h = hover === null ? null : data[hover];

  return (
    <figure className="relative" ref={box}>
      <figcaption className="mb-2 text-sm font-semibold">{label}</figcaption>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="block max-w-full" role="img"
           aria-label={`${label}: ${data.map((d) => `${d.day} ${d.value}`).join(", ")}`}
           onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD_L} x2={W} y1={y(t)} y2={y(t)} stroke={c.line} strokeWidth={1} />
            <text x={PAD_L - 6} y={y(t) + 3} textAnchor="end" fontSize={11} fill={c.muted}>{t}</text>
          </g>
        ))}
        {data.map((d, i) => {
          const x = PAD_L + i * slot + (slot - barW) / 2;
          const bh = Math.max(0, y(0) - y(d.value));
          return (
            <g key={d.day} onMouseEnter={() => setHover(i)}>
              <rect x={PAD_L + i * slot} y={PAD_T} width={slot} height={plotH} fill="transparent" />
              {bh > 0 && (
                <path d={roundedTop(x, y(d.value), barW, bh, Math.min(4, barW / 2))}
                      fill={c.accent} opacity={hover === null || hover === i ? 1 : 0.45} />
              )}
              {i % labelEvery === 0 && (
                <text x={x + barW / 2} y={H - 6} textAnchor="middle" fontSize={11} fill={c.muted}>
                  {shortDay(d.day)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {h && (
        <div className="pointer-events-none absolute right-0 top-0 rounded-md border bg-white px-3 py-2 text-xs shadow-sm"
             style={{ borderColor: c.line }}>
          <p className="font-semibold">{shortDay(h.day)}</p>
          <p>{h.detail}</p>
        </div>
      )}
    </figure>
  );
}

/** Horizontal bars with the value at the end (funnel steps, handover reasons). */
export function HBars({ rows, empty }: { rows: { label: string; value: number; note?: string }[]; empty: string }) {
  if (!rows.length) return <p className="text-sm" style={{ color: c.muted }}>{empty}</p>;
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="space-y-2">
      {rows.map((r) => (
        <li key={r.label} className="grid grid-cols-[minmax(7rem,11rem)_1fr_3rem] items-center gap-3 text-sm"
            title={`${r.label}: ${r.value}${r.note ? ` (${r.note})` : ""}`}>
          <span className="truncate">{r.label}</span>
          <span className="h-3 rounded-r" style={{ background: c.soft }}>
            <span className="block h-3 rounded-r" style={{ width: `${(r.value / max) * 100}%`, background: c.accent,
                                                            minWidth: r.value ? 4 : 0 }} />
          </span>
          <span className="text-right tabular-nums">{r.value}</span>
        </li>
      ))}
    </ul>
  );
}

function niceStep(max: number): number {
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(Math.max(1, raw)));
  return [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? pow * 10;
}

function roundedTop(x: number, y: number, w: number, h: number, r: number): string {
  const rr = Math.min(r, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}
