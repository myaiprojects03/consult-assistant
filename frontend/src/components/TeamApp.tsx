"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { brand } from "@/config/brand";
import StaffSignIn from "@/components/StaffSignIn";
import { ChatError } from "@/lib/chat";
import {
  ask,
  ClientRef,
  listThreads,
  me,
  newMessageId,
  newThread,
  savedToken,
  SignedOut,
  signOut,
  Source,
  StaffUser,
  TeamTurn,
  Thread,
  threadMessages,
} from "@/lib/team";

type Item = TeamTurn & { id: string; failed?: string; pending?: boolean };

const TOOL_STATUS: Record<string, string> = {
  search_internal: "Searching internal documents",
  client_overview: "Reading the client's projects and tickets",
};

const KIND_LABEL: Record<string, string> = {
  proposal: "Proposal", case_study: "Case study", sop: "Procedure", template: "Template",
  technical: "Technical note", notes: "Account notes", policy: "Policy",
};

const c = brand.colors;

/** [S2] in an answer becomes a link to that source's row under the answer. */
// Also accepts the forms some models write instead: 【S1】, 【S1†L1-L4】, [S1†L3].
const CITATION = /[\[【]S(\d+)(?:†[^\]】]*)?[\]】](?!\()/g;

function linkCitations(text: string, itemId: string): string {
  return text.replace(CITATION, (_, n) => `[S${n}](#src-${itemId}-${n})`);
}

/** What Copy puts on the clipboard: the draft without source markers, ready to paste into an email. */
function withoutCitations(text: string): string {
  return text.replace(/ ?[\[【]S\d+(?:†[^\]】]*)?[\]】]/g, "").replace(/[ \t]+\n/g, "\n");
}

function Sources({ itemId, sources }: { itemId: string; sources: Source[] }) {
  if (!sources.length) return null;
  return (
    <ol className="mt-2 space-y-1 border-t pt-2 text-xs" style={{ borderColor: c.line, color: c.muted }}
        aria-label="Sources">
      {sources.map((s) => (
        <li key={s.n} id={`src-${itemId}-${s.n}`} className="flex gap-2 scroll-mt-24">
          <span className="w-6 shrink-0 font-semibold" style={{ color: c.accent }}>S{s.n}</span>
          <span className="min-w-0">
            <span className="font-medium" style={{ color: c.ink }}>{s.title}</span>
            <span className="block">
              {KIND_LABEL[s.kind] ?? s.kind}{s.client ? `, ${s.client}` : ", general"}. content/internal/{s.path}
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="rounded px-1.5 py-0.5 text-xs hover:underline focus-visible:outline focus-visible:outline-2"
            style={{ color: c.muted }}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(text);
                setDone(true);
                setTimeout(() => setDone(false), 1500);
              } catch {
                /* clipboard blocked */
              }
            }}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export default function TeamApp() {
  const [ready, setReady] = useState(false);
  const [token, setToken] = useState("");
  const [user, setUser] = useState<StaffUser | null>(null);
  const [clients, setClients] = useState<ClientRef[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [thread, setThread] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [focus, setFocus] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [notice, setNotice] = useState("");
  const [navOpen, setNavOpen] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const ended = useCallback((e: unknown) => {
    if (e instanceof SignedOut) {
      setToken("");
      setUser(null);
      setNotice(e.message);
      return true;
    }
    return false;
  }, []);

  const refreshThreads = useCallback(async (t: string) => {
    try {
      setThreads(await listThreads(t));
    } catch (e) {
      ended(e);
    }
  }, [ended]);

  useEffect(() => {
    const t = savedToken();
    if (!t) {
      setReady(true);
      return;
    }
    me(t).then((d) => {
      setToken(t);
      setUser(d.user);
      setClients(d.clients);
      void refreshThreads(t);
    }).catch(() => undefined).finally(() => setReady(true));
  }, [refreshThreads]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [items, status]);

  const open = async (id: string) => {
    setNavOpen(false);
    setThread(id);
    setItems([]);
    try {
      const turns = await threadMessages(token, id);
      setItems(turns.map((t, i) => ({ ...t, id: `h${i}` })));
    } catch (e) {
      if (!ended(e)) setNotice(e instanceof ChatError ? e.message : "Could not open that conversation.");
    }
  };

  const startNew = () => {
    setNavOpen(false);
    setThread("");
    setItems([]);
    inputRef.current?.focus();
  };

  const deliver = async (userItem: Item, threadId: string) => {
    const replyId = `a-${userItem.id}`;
    setBusy(true);
    setStatus("Thinking");
    setItems((cur) => [...cur.filter((i) => i.id !== replyId).map((i) => (i.id === userItem.id
      ? { ...i, failed: undefined, pending: true } : i)), { id: replyId, role: "assistant", text: "", sources: [] }]);
    try {
      await ask(token, threadId, userItem.id, userItem.text, focus, (e) => {
        if (e.type === "delta") {
          setStatus("");
          setItems((cur) => cur.map((i) => (i.id === replyId ? { ...i, text: i.text + e.text } : i)));
        } else if (e.type === "tool") {
          setStatus(TOOL_STATUS[e.name] ?? "Working on it");
        } else if (e.type === "sources") {
          setItems((cur) => cur.map((i) => (i.id === replyId ? { ...i, sources: e.sources } : i)));
        }
      });
      setItems((cur) => cur.map((i) => (i.id === userItem.id ? { ...i, pending: false } : i)));
      void refreshThreads(token);
    } catch (e) {
      if (ended(e)) return;
      const msg = e instanceof ChatError ? e.message : "Message not sent.";
      setItems((cur) => cur.filter((i) => i.id !== replyId)
        .map((i) => (i.id === userItem.id ? { ...i, pending: false, failed: msg } : i)));
    } finally {
      setBusy(false);
      setStatus("");
      inputRef.current?.focus();
    }
  };

  const submit = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    let id = thread;
    if (!id) {
      try {
        id = await newThread(token);
        setThread(id);
      } catch (e) {
        if (!ended(e)) setNotice(e instanceof ChatError ? e.message : "Could not start a conversation.");
        return;
      }
    }
    const item: Item = { id: newMessageId(), role: "user", text };
    setDraft("");
    setItems((cur) => [...cur, item]);
    await deliver(item, id);
  };

  const leave = async () => {
    try {
      await signOut(token);
    } catch {
      /* signed out locally either way */
    }
    setToken("");
    setUser(null);
    setThreads([]);
    setItems([]);
    setThread("");
  };

  if (!ready) return null;
  if (!user) {
    return (
      <>
        {notice && <p className="fixed inset-x-0 top-0 p-2 text-center text-sm" style={{ background: c.soft, color: c.alert }}>{notice}</p>}
        <StaffSignIn onDone={(t, u, cl) => {
          setNotice("");
          setToken(t);
          setUser(u);
          setClients(cl);
          void refreshThreads(t);
        }} />
      </>
    );
  }

  const scope = user.role === "admin" ? "All clients (admin)"
    : clients.length ? clients.map((x) => x.name).join(", ") : "No clients assigned yet";

  return (
    <div className="flex h-dvh" style={{ color: c.ink }}>
      <aside className={`${navOpen ? "fixed inset-0 z-20 flex" : "hidden"} w-full flex-col md:static md:flex md:w-72 md:shrink-0`}
             style={{ background: c.ink, color: "#fff" }}>
        <div className="flex items-center justify-between px-4 pb-3 pt-4">
          <h1 className="text-sm font-semibold">{brand.firmName}<span className="block text-xs font-normal opacity-70">Team assistant</span></h1>
          <button className="md:hidden text-sm opacity-80" onClick={() => setNavOpen(false)} aria-label="Close menu">Close</button>
        </div>
        <div className="px-3">
          <button onClick={startNew}
                  className="w-full rounded-lg px-3 py-2 text-left text-sm font-semibold focus-visible:outline focus-visible:outline-2"
                  style={{ background: c.accent, color: c.accentInk }}>
            New conversation
          </button>
          <label htmlFor="focus" className="mt-4 block text-xs opacity-70">Working on</label>
          <select id="focus" value={focus} onChange={(e) => setFocus(e.target.value)}
                  className="mt-1 w-full rounded-lg border-0 px-2 py-2 text-sm" style={{ color: c.ink }}>
            <option value="">General (no client)</option>
            {clients.map((x) => <option key={x.id} value={x.slug}>{x.name}</option>)}
          </select>
        </div>
        <nav className="mt-4 flex-1 overflow-y-auto px-2" aria-label="Conversations">
          {threads.length === 0 && <p className="px-2 text-xs opacity-60">Your conversations will appear here.</p>}
          {threads.map((t) => (
            <button key={t.id} onClick={() => void open(t.id)}
                    className="block w-full truncate rounded-md px-2 py-1.5 text-left text-sm hover:bg-white/10 focus-visible:outline focus-visible:outline-2"
                    style={t.id === thread ? { background: "rgba(255,255,255,0.14)" } : undefined}
                    aria-current={t.id === thread ? "page" : undefined}>
              {t.title}
            </button>
          ))}
        </nav>
        <div className="border-t border-white/15 px-4 py-3 text-xs">
          <p className="truncate font-medium">{user.name}</p>
          <p className="truncate opacity-70">{user.email}</p>
          <button onClick={() => void leave()} className="mt-2 underline opacity-80 hover:opacity-100">Sign out</button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col" style={{ background: c.surface }}>
        <header className="flex items-center gap-3 border-b px-4 py-3" style={{ borderColor: c.line }}>
          <button className="rounded border px-2 py-1 text-sm md:hidden" style={{ borderColor: c.line }}
                  onClick={() => setNavOpen(true)}>Menu</button>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold">{threads.find((t) => t.id === thread)?.title ?? "New conversation"}</p>
            <p className="truncate text-xs" style={{ color: c.muted }}>You can see: {scope}</p>
          </div>
        </header>

        <div ref={listRef} className="flex-1 overflow-y-auto" aria-live="polite">
          <div className="mx-auto max-w-3xl space-y-5 px-4 py-6">
            {notice && <p className="text-sm" style={{ color: c.alert }}>{notice}</p>}
            {items.length === 0 && (
              <div className="pt-10 text-sm" style={{ color: c.muted }}>
                <p className="text-base font-medium" style={{ color: c.ink }}>Ask about our methods, past work or a client.</p>
                <p className="mt-2">Answers cite the internal documents they come from. You can also ask for a proposal outline,
                  a meeting summary or a follow-up email; drafts mark anything that needs confirming.</p>
              </div>
            )}
            {items.map((m) => m.role === "user" ? (
              <div key={m.id} className="flex flex-col items-end">
                <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl px-3.5 py-2.5 text-[15px] leading-relaxed"
                     style={{ background: c.accent, color: c.accentInk, opacity: m.pending ? 0.75 : 1 }}>{m.text}</div>
                {m.failed && (
                  <p className="mt-1 text-xs" style={{ color: c.alert }}>
                    {m.failed}{" "}
                    <button className="font-semibold underline" disabled={busy} onClick={() => void deliver(m, thread)}>Retry</button>
                  </p>
                )}
              </div>
            ) : m.text ? (
              <article key={m.id} className="rounded-xl border px-4 py-3" style={{ borderColor: c.line }}>
                <div className="chat-text team-text text-[15px] leading-relaxed">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} allowedElements={["p", "ul", "ol", "li", "strong", "em", "a", "br", "h1", "h2", "h3", "h4",
                                                   "blockquote", "code", "pre", "hr", "table", "thead", "tbody", "tr", "th", "td"]}
                                 components={{
                                   a: ({ href, ...p }) => href?.startsWith("#src-")
                                     ? <a href={href} {...p} className="cite" />
                                     : <a href={href} {...p} target="_blank" rel="noopener noreferrer" />,
                                 }}>
                    {linkCitations(m.text, m.id)}
                  </ReactMarkdown>
                </div>
                <Sources itemId={m.id} sources={m.sources ?? []} />
                <div className="mt-1 flex justify-end"><CopyButton text={withoutCitations(m.text)} /></div>
              </article>
            ) : null)}
            {status && (
              <p className="text-xs" style={{ color: c.muted }}>
                <span className="typing" aria-hidden="true"><i /><i /><i /></span> {status}…
              </p>
            )}
          </div>
        </div>

        <form className="border-t px-4 py-3" style={{ borderColor: c.line }}
              onSubmit={(e) => { e.preventDefault(); void submit(); }}>
          <div className="mx-auto flex max-w-3xl items-end gap-2">
            <label htmlFor="team-input" className="sr-only">Question</label>
            <textarea id="team-input" ref={inputRef} rows={2} value={draft} maxLength={12000}
                      placeholder="Ask a question or describe the draft you need"
                      onChange={(e) => setDraft(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submit(); } }}
                      className="max-h-48 min-h-[52px] flex-1 resize-y rounded-xl border px-3 py-2.5 text-[15px] outline-none focus:ring-2"
                      style={{ borderColor: c.line, ["--tw-ring-color" as string]: c.accent }} />
            <button type="submit" disabled={busy || !draft.trim()}
                    className="h-11 rounded-xl px-4 text-sm font-semibold disabled:opacity-40 focus-visible:outline focus-visible:outline-2"
                    style={{ background: c.accent, color: c.accentInk }}>
              Send
            </button>
          </div>
          <p className="mx-auto mt-2 max-w-3xl text-[11px]" style={{ color: c.muted }}>
            Every question and the documents it used are logged. Check drafts before sending them to a client.
          </p>
        </form>
      </main>
    </div>
  );
}
