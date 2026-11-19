"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";

import { brand } from "@/config/brand";
import {
  ChatError,
  forgetSession,
  newMessageId,
  restoreSession,
  sendMessage,
  Session,
  startSession,
  Turn,
} from "@/lib/chat";

type Item = Turn & { id: string; failed?: string; pending?: boolean };

const TOOL_STATUS: Record<string, string> = {
  search_knowledge: "Looking that up",
  update_lead: "Noting that down",
  find_available_slots: "Checking the calendar",
  book_discovery_call: "Booking your call",
  reschedule_discovery_call: "Moving your call",
  cancel_discovery_call: "Cancelling your call",
  handover_to_human: "Passing this to a consultant",
  verify_client: "Checking your account",
  client_overview: "Looking up your account",
  raise_ticket: "Raising a ticket",
  update_ticket: "Updating your ticket",
};

function closeFrame() {
  window.parent?.postMessage({ type: "consult-chat:close" }, "*");
}

export default function ChatWidget({ pageUrl, embedded }: { pageUrl: string; embedded: boolean }) {
  const [session, setSession] = useState<Session | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [startError, setStartError] = useState("");
  const [handedOver, setHandedOver] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const begin = useCallback(async (fresh = false) => {
    setStartError("");
    try {
      const restored = fresh ? null : await restoreSession();
      if (restored && restored.turns.length) {
        setSession(restored.session);
        setItems([{ id: "greeting", role: "assistant", text: restored.session.greeting },
                  ...restored.turns.map((t, i) => ({ ...t, id: `r${i}` }))]);
        return;
      }
      const s = restored?.session ?? (await startSession(pageUrl));
      setSession(s);
      setItems([{ id: "greeting", role: "assistant", text: s.greeting }]);
    } catch (e) {
      setStartError(e instanceof ChatError ? e.message : "The chat could not start.");
    }
  }, [pageUrl]);

  useEffect(() => {
    void begin();
  }, [begin]);

  useEffect(() => {
    if (!embedded) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeFrame(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [embedded]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [items, status]);

  const deliver = useCallback(async (userItem: Item) => {
    if (!session) return;
    const replyId = `a-${userItem.id}`;
    setBusy(true);
    setStatus("Thinking");
    setItems((cur) => [...cur.filter((i) => i.id !== replyId).map((i) => (i.id === userItem.id
      ? { ...i, failed: undefined, pending: true } : i)), { id: replyId, role: "assistant", text: "" }]);
    try {
      await sendMessage(session, userItem.id, userItem.text, (e) => {
        if (e.type === "delta") {
          setStatus("");
          setItems((cur) => cur.map((i) => (i.id === replyId ? { ...i, text: i.text + e.text } : i)));
        } else if (e.type === "tool") {
          setStatus(TOOL_STATUS[e.name] ?? "Working on it");
        } else if (e.type === "handover") {
          setHandedOver(true);
        }
      });
      setItems((cur) => cur.map((i) => (i.id === userItem.id ? { ...i, pending: false } : i)));
    } catch (e) {
      const msg = e instanceof ChatError ? e.message : "Message not sent.";
      // Drop any half-streamed reply; a retry returns the complete one.
      setItems((cur) => cur.filter((i) => i.id !== replyId)
        .map((i) => (i.id === userItem.id ? { ...i, pending: false, failed: msg } : i)));
    } finally {
      setBusy(false);
      setStatus("");
      inputRef.current?.focus();
    }
  }, [session]);

  const submit = () => {
    const text = draft.trim();
    if (!text || busy || !session) return;
    const item: Item = { id: newMessageId(), role: "user", text };
    setDraft("");
    setItems((cur) => [...cur, item]);
    void deliver(item);
  };

  const restart = () => {
    forgetSession();
    setItems([]);
    setHandedOver(false);
    void begin(true);
  };

  const c = brand.colors;
  return (
    <div className="flex h-full flex-col" style={{ background: c.surface, color: c.ink }}>
      <header className="flex items-center gap-3 px-4 py-3" style={{ background: c.ink, color: "#fff" }}>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[15px] font-semibold leading-tight">
            {session?.assistantName ?? "Assistant"} <span className="font-normal opacity-75">· {brand.firmName}</span>
          </h1>
          <p className="truncate text-xs opacity-75">{brand.headerSubtitle}</p>
        </div>
        <button onClick={restart} className="rounded px-2 py-1 text-xs opacity-80 hover:opacity-100 focus-visible:outline focus-visible:outline-2"
                aria-label="Start a new conversation">New chat</button>
        {embedded && (
          <button onClick={closeFrame} aria-label="Close chat"
                  className="rounded p-1 text-lg leading-none opacity-80 hover:opacity-100 focus-visible:outline focus-visible:outline-2">
            ×
          </button>
        )}
      </header>

      <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4" aria-live="polite">
        {startError && (
          <div className="rounded-lg border p-3 text-sm" style={{ borderColor: c.line, color: c.alert }}>
            {startError}{" "}
            <button className="underline" onClick={() => void begin()}>Try again</button>
          </div>
        )}
        {items.map((m) => m.text || m.role === "user" ? (
          <div key={m.id} className={m.role === "user" ? "flex flex-col items-end" : "flex flex-col items-start"}>
            <div
              className="chat-text max-w-[85%] rounded-2xl px-3.5 py-2.5 text-[15px] leading-relaxed"
              style={m.role === "user"
                ? { background: c.accent, color: c.accentInk, borderBottomRightRadius: 6, opacity: m.pending ? 0.75 : 1 }
                : { background: c.soft, color: c.ink, borderBottomLeftRadius: 6 }}
            >
              {m.role === "assistant"
                ? <ReactMarkdown allowedElements={["p", "ul", "ol", "li", "strong", "em", "a", "br"]}
                                 components={{ a: (p) => <a {...p} target="_blank" rel="noopener noreferrer" /> }}>
                    {m.text}
                  </ReactMarkdown>
                : m.text}
            </div>
            {m.failed && (
              <p className="mt-1 text-xs" style={{ color: c.alert }}>
                {m.failed}{" "}
                <button className="font-semibold underline" disabled={busy} onClick={() => void deliver(m)}>Retry</button>
              </p>
            )}
          </div>
        ) : null)}
        {status && (
          <p className="text-xs" style={{ color: c.muted }}>
            <span className="typing" aria-hidden="true"><i /><i /><i /></span> {status}…
          </p>
        )}
        {handedOver && (
          <p className="rounded-lg px-3 py-2 text-xs" style={{ background: c.soft, color: c.muted }}>
            A consultant has been asked to follow up on this conversation.
          </p>
        )}
      </div>

      <form className="border-t px-3 pb-2 pt-3" style={{ borderColor: c.line }}
            onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <div className="flex items-end gap-2">
          <label htmlFor="chat-input" className="sr-only">Message</label>
          <textarea
            id="chat-input" ref={inputRef} rows={1} value={draft} maxLength={2000}
            placeholder={brand.inputPlaceholder} disabled={!session}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
            className="max-h-32 min-h-[44px] flex-1 resize-none rounded-xl border px-3 py-2.5 text-[15px] outline-none focus:ring-2"
            style={{ borderColor: c.line, ["--tw-ring-color" as string]: c.accent }}
          />
          <button type="submit" disabled={busy || !draft.trim() || !session}
                  className="h-11 rounded-xl px-4 text-sm font-semibold disabled:opacity-40 focus-visible:outline focus-visible:outline-2"
                  style={{ background: c.accent, color: c.accentInk }}>
            Send
          </button>
        </div>
        <p className="mt-2 text-[11px] leading-snug" style={{ color: c.muted }}>
          {brand.privacyNote}{" "}
          <a href={brand.privacyUrl} target="_blank" rel="noopener noreferrer" className="underline">Privacy</a>
        </p>
      </form>
    </div>
  );
}
