// Admin dashboard API client (admins only; same sign-in as the team assistant).

import { staffRequest } from "@/lib/team";

export type Row = Record<string, unknown>;

export async function adminGet<T = Row>(token: string, path: string): Promise<T> {
  return (await (await staffRequest(`/api/admin${path}`, token)).json()) as T;
}

export async function adminSend<T = Row>(token: string, path: string, method: "PUT" | "POST" | "DELETE",
                                         body?: unknown): Promise<T> {
  const r = await staffRequest(`/api/admin${path}`, token, {
    method, body: body === undefined ? undefined : JSON.stringify(body),
  });
  return (await r.json()) as T;
}

export async function placeCall(token: string, body: { client_message_id: string; to: string; name: string;
                                                        purpose: string }): Promise<Row> {
  return (await (await staffRequest("/api/team/calls", token, { method: "POST", body: JSON.stringify(body) }))
    .json()) as Row;
}

export type Overview = {
  days: number;
  conversations_by_channel: { channel: string; n: number }[];
  funnel: { conversations: number; contacts: number; qualified: number; not_a_fit: number; booked: number };
  handovers: { reason: string; n: number }[];
  bookings: { status: string; n: number }[];
  calls: { direction: string; n: number; seconds: number; transferred: number }[];
  tickets: { status: string; n: number }[];
  quality: { turns: number; first_text_p50: number | null; first_text_p90: number | null; input_tokens: number;
             output_tokens: number; guard_replacements: number };
  failed_jobs: number;
  daily: { day: string; conversations: number; qualified: number; bookings: number; handovers: number }[];
};

export type ContentItem = { path: string; title: string; help: string; group: "assistant" | "internal";
                            edited: boolean; version: number; updated_at: string | null };
export type Entry = { slug?: string; title: string; body: string; audience?: string };
export type ContentFile = { path: string; text: string; default: string; version: number; edited: boolean;
                            entries?: Entry[] | null;
                            history: { version: number; deleted: boolean; note: string; updated_by: string | null;
                                       created_at: string }[] };

export const when = (v: unknown): string => {
  if (!v) return "";
  const d = new Date(String(v));
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString(undefined, {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};

export const words = (v: unknown): string => String(v ?? "").replace(/_/g, " ");

/** An event's data as "key: value" pairs for people (nested values stay compact JSON). */
export const details = (data: unknown, byName?: unknown): string => {
  if (!data || typeof data !== "object") return String(data ?? "");
  const by = byName ? [`by ${String(byName)}`] : [];
  return by.concat(Object.entries(data as Record<string, unknown>)
    .filter(([k]) => !(byName && k === "user"))
    .map(([k, v]) => `${words(k)}: ${typeof v === "object" && v !== null
      ? (Array.isArray(v) ? v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(", ") : JSON.stringify(v))
      : String(v)}`))
    .join("; ");
};
