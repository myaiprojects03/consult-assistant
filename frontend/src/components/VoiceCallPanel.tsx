"use client";

import { useEffect, useRef, useState } from "react";

import { brand } from "@/config/brand";
import { ChatError } from "@/lib/chat";
import { CallState, Line, VoiceCall, voiceSupported } from "@/lib/voice";

const c = brand.colors;

const STATE_TEXT: Record<CallState, string> = {
  connecting: "Connecting…",
  listening: "Listening. Go ahead and speak.",
  thinking: "Thinking…",
  speaking: "Ava is speaking. You can interrupt at any time.",
  ended: "Call ended",
};

export default function VoiceCallPanel() {
  const [supported, setSupported] = useState<boolean | null>(null);
  const [state, setState] = useState<CallState | "idle">("idle");
  const [lines, setLines] = useState<Line[]>([]);
  const [interim, setInterim] = useState("");
  const [notice, setNotice] = useState("");
  const [muted, setMuted] = useState(false);
  const [typed, setTyped] = useState("");
  const call = useRef<VoiceCall | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => setSupported(voiceSupported()), []);
  useEffect(() => () => call.current?.hangUp(), []);
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [lines, interim]);

  const begin = async () => {
    setLines([]);
    setNotice("");
    setMuted(false);
    const vc = new VoiceCall({
      state: setState,
      line: (l) => setLines((cur) => [...cur, l]),
      interim: setInterim,
      ended: (reason) => { setNotice(reason); setInterim(""); },
    });
    call.current = vc;
    try {
      await vc.start(window.location.href);
    } catch (e) {
      setState("idle");
      setNotice(e instanceof ChatError ? e.message : "The call could not start.");
    }
  };

  const live = state !== "idle" && state !== "ended";

  if (supported === false) {
    return (
      <p className="rounded-lg p-4 text-sm" style={{ background: c.soft, color: c.ink }}>
        Voice calls need Chrome, Edge or Safari with microphone access. You can still use the chat.
      </p>
    );
  }

  return (
    <section className="flex flex-col items-center" aria-label="Voice call with Ava">
      <button
        type="button"
        onClick={() => (live ? call.current?.hangUp() : void begin())}
        disabled={supported === null || state === "connecting"}
        className={`call-button relative flex h-36 w-36 items-center justify-center rounded-full text-base font-semibold shadow-sm focus-visible:outline focus-visible:outline-4 focus-visible:outline-offset-4 ${live ? `is-${state}` : ""}`}
        style={{ background: live ? c.alert : c.accent, color: c.accentInk, ["--ring" as string]: c.accent }}
        aria-label={live ? "Hang up" : "Start a voice call with Ava"}
      >
        {live ? "Hang up" : "Call Ava"}
      </button>

      <p className="mt-6 min-h-[1.5rem] text-center text-sm" style={{ color: c.muted }} aria-live="polite">
        {state === "idle" ? "Free, from your browser. Headphones help Ava hear only you." : STATE_TEXT[state]}
      </p>
      {live && (
        <button type="button" onClick={() => { const m = !muted; setMuted(m); call.current?.setMuted(m); }}
                className="mt-3 rounded-full border px-4 py-1.5 text-sm focus-visible:outline focus-visible:outline-2"
                style={{ borderColor: c.line, color: c.ink }} aria-pressed={muted}>
          {muted ? "Unmute microphone" : "Mute microphone"}
        </button>
      )}
      {live && (
        <form className="mt-5 flex w-full max-w-sm gap-2"
              onSubmit={(e) => { e.preventDefault(); call.current?.sendText(typed); setTyped(""); }}>
          <label htmlFor="type-instead" className="sr-only">Type instead</label>
          <input id="type-instead" value={typed} onChange={(e) => setTyped(e.target.value)} maxLength={300}
                 placeholder="Or type, e.g. your email address" autoComplete="email"
                 className="min-w-0 flex-1 rounded-full border px-4 py-2 text-sm outline-none focus:ring-2"
                 style={{ borderColor: c.line, ["--tw-ring-color" as string]: c.accent }} />
          <button type="submit" disabled={!typed.trim()}
                  className="rounded-full px-4 text-sm font-semibold disabled:opacity-40 focus-visible:outline focus-visible:outline-2"
                  style={{ background: c.accent, color: c.accentInk }}>Send</button>
        </form>
      )}
      {notice && <p className="mt-3 text-center text-sm font-medium" style={{ color: c.ink }}>{notice}</p>}

      {(lines.length > 0 || interim) && (
        <div ref={listRef} className="mt-8 max-h-[45vh] w-full space-y-3 overflow-y-auto rounded-xl border p-4"
             style={{ borderColor: c.line }} aria-label="Call transcript">
          {lines.map((l, i) => (
            <p key={i} className="text-[15px] leading-relaxed">
              <span className="mr-2 text-xs font-semibold" style={{ color: l.role === "caller" ? c.accent : c.ink }}>
                {l.role === "caller" ? "You" : "Ava"}
              </span>
              {l.text}
            </p>
          ))}
          {interim && <p className="text-[15px] italic" style={{ color: c.muted }}>{interim}</p>}
        </div>
      )}
    </section>
  );
}
