// Browser voice call: the browser turns speech into text and back (Web Speech API); the server's call
// session (same messages as a Twilio phone call) does the rest. Free, no telephony account needed.
//
// Interruptions: while Ava speaks, the microphone keeps listening. If the caller talks (and it is not
// Ava's own voice coming back through the speakers), speech stops at once and the server is told what
// the caller heard, so the next reply does not repeat it.

import { API_URL, ChatError } from "@/lib/chat";

type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((e: RecognitionEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
};
type RecognitionEvent = { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> };
type RecognitionCtor = new () => Recognition;

export type CallState = "connecting" | "listening" | "thinking" | "speaking" | "ended";
export type Line = { role: "caller" | "assistant"; text: string };
export type CallEvents = {
  state: (s: CallState) => void;
  line: (l: Line) => void;
  interim: (text: string) => void;
  ended: (reason: string) => void;
};

export function voiceSupported(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return Boolean((w.SpeechRecognition || w.webkitSpeechRecognition) && window.speechSynthesis && window.WebSocket);
}

const END_OF_SPEECH_MS = 1500;     // pause that ends what the caller is saying
const MID_SENTENCE_MS = 3500;      // ... unless they stopped mid-sentence ("my name is", "at gmail dot")
const DANGLING = /\b(is|are|my|the|a|an|and|or|at|dot|to|of|for|with|from|it's|its|name|email|number|um|uh)$/i;

function words(s: string): string[] {
  return s.toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean);
}

/** True if `heard` is mostly words Ava is saying right now: her own voice picked up by the microphone. */
function isEcho(heard: string, speaking: string): boolean {
  const h = words(heard);
  if (!h.length) return true;
  const s = new Set(words(speaking));
  return h.filter((w) => s.has(w)).length / h.length >= 0.6;
}

export class VoiceCall {
  private ws: WebSocket | null = null;
  private rec: Recognition | null = null;
  private active = false;
  private muted = false;
  private queue: string[] = [];          // sentences waiting to be spoken
  private current = "";                  // sentence being spoken
  private currentChar = 0;               // how far into it (from boundary events)
  private heard = "";                    // what the caller has heard of this reply
  private replyText = "";                // everything Ava said in this reply (for the transcript)
  private replyDone = false;             // server sent last=true
  private endAfterSpeech: string | null = null;
  private voice: SpeechSynthesisVoice | null = null;
  private lang = "en-GB";

  constructor(private on: CallEvents) {}

  async start(pageUrl: string): Promise<void> {
    this.active = true;
    this.on.state("connecting");
    let r: Response;
    try {
      r = await fetch(`${API_URL}/api/voice/web/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page_url: pageUrl.slice(0, 500), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone ?? "" }),
      });
    } catch {
      throw new ChatError("Can't reach the call service. Check your connection and try again.");
    }
    if (!r.ok) {
      let detail = "The call could not start.";
      try { detail = (await r.json()).detail ?? detail; } catch { /* not JSON */ }
      throw new ChatError(detail, r.status);
    }
    const d = await r.json();
    this.lang = d.language || "en-GB";
    this.pickVoice();
    const base = API_URL.replace(/^http/, "ws");
    const q = new URLSearchParams({ conversation_id: d.conversation_id, call_id: d.call_id, token: d.token });
    this.ws = new WebSocket(`${base}/api/voice/web/ws?${q}`);
    this.ws.onmessage = (ev) => this.onServer(JSON.parse(ev.data));
    this.ws.onclose = () => { if (this.active) this.finish(this.endAfterSpeech ?? "The call has ended."); };
    this.ws.onerror = () => { if (this.active) this.finish("The connection was lost."); };
    this.listen();
  }

  hangUp(): void {
    this.finish("You ended the call.");
  }

  setMuted(m: boolean): void {
    this.muted = m;
  }

  // ------------------------------------------------------------ speech out
  private pickVoice(): void {
    const pick = () => {
      const all = window.speechSynthesis.getVoices();
      this.voice = all.find((v) => v.lang === this.lang && /natural|google|premium|enhanced/i.test(v.name))
        ?? all.find((v) => v.lang === this.lang) ?? all.find((v) => v.lang.startsWith(this.lang.slice(0, 2))) ?? null;
    };
    pick();
    window.speechSynthesis.onvoiceschanged = pick;
  }

  private onServer(msg: { type: string; token?: string; last?: boolean; handoffData?: string }): void {
    if (msg.type === "text") {
      const token = msg.token ?? "";
      if (token.trim()) {
        this.queue.push(token.trim());
        this.replyText += (this.replyText ? " " : "") + token.trim();
        if (!this.current) this.speakNext();
      }
      if (msg.last) {
        if (this.replyText) this.on.line({ role: "assistant", text: this.replyText });   // its own line
        this.replyText = "";
        this.replyDone = true;
        if (!this.current && !this.queue.length) this.replyFinished();
      }
    } else if (msg.type === "end") {
      let reason = "The call has ended.";
      try {
        const h = JSON.parse(msg.handoffData || "{}");
        if (h.action === "transfer") reason = "You're being connected to a consultant.";
        else if (h.reason === "time_limit" || h.reason === "limit") reason = "The call reached its time limit.";
        else if (h.reason === "unavailable") reason = "Ava is unavailable right now. Please try again later, or use the chat.";
      } catch { /* keep the default */ }
      this.endAfterSpeech = reason;
      if (!this.current && !this.queue.length) this.finish(reason);
    }
  }

  private speakNext(): void {
    const next = this.queue.shift();
    if (!next) {
      this.current = "";
      if (this.replyDone) this.replyFinished();
      return;
    }
    this.current = next;
    this.currentChar = 0;
    this.on.state("speaking");
    const u = new SpeechSynthesisUtterance(next);
    u.lang = this.lang;
    if (this.voice) u.voice = this.voice;
    u.onboundary = (e) => { if (u.text === this.current) this.currentChar = e.charIndex; };
    u.onend = () => {
      if (u.text !== this.current) return;           // cancelled by an interruption
      this.heard += (this.heard ? " " : "") + next;
      this.speakNext();
    };
    window.speechSynthesis.speak(u);
  }

  private replyFinished(): void {
    if (this.replyText) this.on.line({ role: "assistant", text: this.replyText });
    this.replyText = "";
    this.heard = "";
    this.replyDone = false;
    if (this.endAfterSpeech) {
      this.finish(this.endAfterSpeech);
      return;
    }
    if (this.active) this.on.state("listening");
  }

  private interrupt(): void {
    const partial = this.current.slice(0, this.currentChar).trim();
    const heard = [this.heard, partial].filter(Boolean).join(" ");
    this.queue = [];
    this.current = "";
    window.speechSynthesis.cancel();
    if (this.replyText) this.on.line({ role: "assistant", text: `${heard || this.replyText} …` });
    this.replyText = "";
    this.heard = "";
    this.replyDone = false;
    this.send({ type: "interrupt", utteranceUntilInterrupt: heard, durationUntilInterruptMs: 0 });
  }

  // ------------------------------------------------------------ speech in
  // Chrome ends a recognition session after silence or errors, and splits slow speech (an email spelled
  // letter by letter) into several "final" pieces. So: pieces are collected until the caller pauses
  // (END_OF_SPEECH_MS), a new session starts whenever one ends, and a watchdog restarts a stuck one.
  private pending = "";
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private running = false;
  private lastHeard = 0;

  private listen(): void {
    const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
    const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!Ctor) return;
    const startSession = () => {
      if (!this.active || this.running) return;
      const rec = new Ctor();
      rec.lang = this.lang;
      rec.continuous = true;
      rec.interimResults = true;
      rec.onresult = (e) => this.onHeard(e);
      rec.onend = () => {
        this.running = false;
        if (this.active) setTimeout(startSession, 150);
      };
      rec.onerror = (e) => {
        if (e.error === "not-allowed" || e.error === "service-not-allowed") this.finish("Microphone access was blocked.");
      };
      this.rec = rec;
      try {
        rec.start();
        this.running = true;
        this.lastHeard = Date.now();
      } catch {
        setTimeout(startSession, 500);
      }
    };
    startSession();
    this.watchdog = setInterval(() => {
      // a session that has heard nothing for a long time may be stuck: replace it
      if (this.active && this.running && Date.now() - this.lastHeard > 20000 && !this.current) {
        try { this.rec?.abort(); } catch { /* onend restarts */ }
      }
      if (this.active && !this.running) startSession();
    }, 5000);
  }

  private onHeard(e: RecognitionEvent): void {
    this.lastHeard = Date.now();
    if (this.muted) return;
    let interim = "";
    const speakingNow = this.current || this.queue.length ? `${this.current} ${this.queue.join(" ")}` : "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const text = e.results[i][0].transcript.trim();
      if (!text || (speakingNow && isEcho(text, speakingNow))) continue;   // Ava's own voice through the speakers
      if (speakingNow && (e.results[i].isFinal || words(text).length >= 3)) this.interrupt();   // talking over Ava
      if (e.results[i].isFinal) this.pending = `${this.pending} ${text}`.trim();
      else interim += text + " ";
    }
    this.on.interim(`${this.pending} ${interim}`.trim());
    if (this.flushTimer) clearTimeout(this.flushTimer);
    if (this.pending) {
      const wait = DANGLING.test(this.pending) ? MID_SENTENCE_MS : END_OF_SPEECH_MS;
      this.flushTimer = setTimeout(() => this.flush(), wait);
    }
  }

  private flush(): void {
    const text = this.pending.trim();
    this.pending = "";
    this.flushTimer = null;
    if (!text) return;
    this.on.interim("");
    this.sendText(text, false);
  }

  /** Send what the caller said (or typed, e.g. an email address that speech recognition gets wrong). */
  sendText(text: string, typed = true): void {
    const t = text.trim();
    if (!t || !this.active) return;
    if (typed && (this.current || this.queue.length)) this.interrupt();
    this.on.line({ role: "caller", text: t });
    this.on.state("thinking");
    this.send({ type: "prompt", voicePrompt: t, last: true });
  }

  private send(msg: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private finish(reason: string): void {
    if (!this.active) return;
    this.active = false;
    if (this.watchdog) clearInterval(this.watchdog);
    if (this.flushTimer) clearTimeout(this.flushTimer);
    try { this.rec?.abort(); } catch { /* ignore */ }
    window.speechSynthesis.cancel();
    if (this.replyText) this.on.line({ role: "assistant", text: this.replyText });
    try { this.ws?.close(); } catch { /* ignore */ }
    this.on.state("ended");
    this.on.ended(reason);
  }
}
