"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Content from "@/components/admin/Content";
import Conversations, { ConversationDetail } from "@/components/admin/Conversations";
import { Bookings, Calls, Handovers, Leads, Log } from "@/components/admin/Lists";
import Overview from "@/components/admin/Overview";
import Privacy from "@/components/admin/Privacy";
import StaffSignIn from "@/components/StaffSignIn";
import { brand } from "@/config/brand";
import { adminGet, Overview as OverviewData } from "@/lib/admin";
import { ChatError } from "@/lib/chat";
import { me, savedToken, SignedOut, signOut, StaffUser } from "@/lib/team";

const c = brand.colors;
const TABS = [
  ["overview", "Overview"], ["conversations", "Conversations"], ["handovers", "Handovers"], ["bookings", "Bookings"],
  ["calls", "Calls"], ["leads", "Leads"], ["content", "Content and rules"], ["privacy", "Privacy"], ["log", "Log"],
] as const;
type Tab = (typeof TABS)[number][0];
const INTRO: Record<Tab, string> = {
  overview: "",
  conversations: "Every website chat and call, with its transcript.",
  handovers: "Conversations the assistant passed to a consultant, and why.",
  bookings: "Discovery calls booked by the assistant.",
  calls: "Phone and browser calls. Ask Ava to call someone from here.",
  leads: "Contact details and needs captured in conversations.",
  content: "What the assistant knows and how it behaves. Changes apply at once.",
  privacy: "Requests to see or delete someone's data, and the automatic clean-up.",
  log: "Everything the system did, newest first.",
};

export default function AdminApp() {
  const [ready, setReady] = useState(false);
  const [token, setToken] = useState("");
  const [user, setUser] = useState<StaffUser | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [days, setDays] = useState(30);
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [notice, setNotice] = useState("");
  const [drawer, setDrawer] = useState<string | null>(null);
  const nav = useRef<HTMLElement>(null);
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {        // the transcript panel: Escape closes it; focus moves in, and back out on close
    if (!drawer) return;
    const before = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setDrawer(null); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); before?.focus(); };
  }, [drawer]);
  useEffect(() => {        // phones: keep the current section's tab visible in the scrolling row
    nav.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [tab, user]);

  const onError = useCallback((e: unknown) => {
    if (e instanceof SignedOut) {
      setToken("");
      setUser(null);
      setNotice(e.message);
    } else {
      setNotice(e instanceof ChatError ? e.message : "Something went wrong loading this view.");
    }
  }, []);

  useEffect(() => {
    const t = savedToken();
    if (!t) { setReady(true); return; }
    me(t).then((d) => { setToken(t); setUser(d.user); }).catch(() => undefined).finally(() => setReady(true));
  }, []);

  useEffect(() => {
    if (!token || user?.role !== "admin" || tab !== "overview") return;
    setOverview(null);
    adminGet<OverviewData>(token, `/overview?days=${days}`).then(setOverview).catch(onError);
  }, [token, user, tab, days, onError]);

  const go = (t: Tab) => { setTab(t); setNotice(""); setDrawer(null); };

  if (!ready) return null;
  if (!user) {
    return (
      <>
        {notice && <p className="fixed inset-x-0 top-0 p-2 text-center text-sm" style={{ background: c.soft, color: c.alert }}>{notice}</p>}
        <StaffSignIn title={`${brand.firmName} dashboard`} note="For admins: leads, conversations, calls, bookings, handovers, and the assistant's content and rules."
                     onDone={(t, u) => { setNotice(""); setToken(t); setUser(u); }} />
      </>
    );
  }
  if (user.role !== "admin") {
    return (
      <main className="flex min-h-dvh items-center justify-center p-6 text-center" style={{ color: c.ink }}>
        <div>
          <p className="font-semibold">The dashboard is for admins.</p>
          <p className="mt-2 text-sm" style={{ color: c.muted }}>Your account can use the team assistant at <a className="underline" href="/team">/team</a>.</p>
        </div>
      </main>
    );
  }

  const shared = { token, onError, openConversation: (id: string) => setDrawer(id) };
  return (
    <div className="min-h-dvh" style={{ color: c.ink, background: c.surface }}>
      <header className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b px-4 py-3 lg:flex-nowrap lg:px-8" style={{ borderColor: c.line }}>
        <p className="shrink-0 font-semibold">{brand.firmName} <span className="font-normal" style={{ color: c.muted }}>dashboard</span></p>
        <nav ref={nav} aria-label="Sections" className="order-last -mx-1 flex basis-full gap-1 overflow-x-auto lg:order-none lg:min-w-0 lg:flex-1 lg:basis-auto">
          {TABS.map(([id, label]) => (
            <button key={id} onClick={() => go(id)} aria-current={tab === id ? "page" : undefined}
                    className="whitespace-nowrap rounded-md px-3 py-1.5 text-sm focus-visible:outline focus-visible:outline-2 lg:px-2.5"
                    style={tab === id ? { background: c.ink, color: "#fff" } : { color: c.ink }}>
              {label}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex shrink-0 items-center gap-3 text-sm lg:ml-0">
          <span style={{ color: c.muted }}>{user.name}</span>
          <button className="underline" onClick={async () => { try { await signOut(token); } catch { /* signed out here */ }
            setToken(""); setUser(null); }}>Sign out</button>
        </div>
      </header>

      <main className="px-4 py-6 lg:px-8">
        {notice && <p role="alert" className="mb-4 rounded-lg p-3 text-sm" style={{ background: "#FFF4ED", color: c.alert }}>{notice}</p>}
        {tab !== "overview" && (
          <div className="mb-4">
            <h1 className="text-xl font-semibold">{TABS.find(([id]) => id === tab)?.[1]}</h1>
            <p className="mt-0.5 text-sm" style={{ color: c.muted }}>{INTRO[tab]}</p>
          </div>
        )}
        {tab === "overview" && (
          <>
            <div className="mb-4 flex items-center gap-3">
              <h1 className="text-xl font-semibold">Overview</h1>
              <select aria-label="Period" value={days} onChange={(e) => setDays(Number(e.target.value))}
                      className="rounded-lg border px-2 py-1 text-sm" style={{ borderColor: c.line }}>
                <option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option>
              </select>
            </div>
            {overview ? <Overview data={overview} /> : !notice && <p className="text-sm" style={{ color: c.muted }}>Loading…</p>}
          </>
        )}
        {tab === "conversations" && <Conversations token={token} onError={onError} />}
        {tab === "handovers" && <Handovers {...shared} />}
        {tab === "bookings" && <Bookings {...shared} />}
        {tab === "calls" && <Calls {...shared} />}
        {tab === "leads" && <Leads {...shared} />}
        {tab === "content" && <Content token={token} onError={onError} />}
        {tab === "privacy" && <Privacy {...shared} />}
        {tab === "log" && <Log token={token} onError={onError} />}
      </main>

      {drawer && (
        <div className="fixed inset-0 z-20 flex justify-end" style={{ background: "rgba(16,40,44,0.35)" }}
             onClick={() => setDrawer(null)}>
          <aside ref={panel} tabIndex={-1} role="dialog" aria-modal="true" aria-label="Conversation"
                 className="h-full w-full max-w-2xl overflow-y-auto bg-white shadow-xl outline-none"
                 onClick={(e) => e.stopPropagation()}>
            <ConversationDetail token={token} id={drawer} onClose={() => setDrawer(null)} onError={onError} />
          </aside>
        </div>
      )}
    </div>
  );
}
